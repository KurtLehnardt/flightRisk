"""Amber Drone web dashboard.

Real-time web UI showing drone video feed, detection overlays,
match alerts, drone telemetry, and search controls.

Runs on http://localhost:5555
"""

import base64
import hmac
import json
import logging
import os
import queue
import secrets
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

from amber.vision.detector import PersonDetector
from amber.vision.reid import PersonReID
from amber.vision.quality import ImageQualityScorer
from amber.vision.scorer import MatchScorer
from amber.vision.threshold_tuner import ThresholdTuner
from amber.vision.tracker import DetectionTracker
from amber.recorder import SessionRecorder
from amber.observability import StructuredLogger, MetricsCollector
from amber.persistence import SessionDB
from amber.drone.fleet import DroneFleet
from amber.drone.controller import DroneController
from amber.canon import TargetCanon

try:
    from amber.telemetry import init_telemetry, get_tracer, get_meter, AmberMetrics
    _HAS_TELEMETRY = True
except ImportError:
    _HAS_TELEMETRY = False

# ---------------------------------------------------------------------------
# Import decomposed modules
# ---------------------------------------------------------------------------
from amber.dashboard.state import (  # noqa: F401 — re-exported for backward compat
    SourceConfig,
    app_state,
    _state,
    fleet_lock,
    match_history_lock as _match_history_lock,
    gemma_thread_lock as _gemma_thread_lock,
    gemma_queue as _gemma_queue,
    gemma_last_call as _gemma_last_call,
    alerted_tracks as _alerted_tracks,
    ALERT_COOLDOWN,
    GEMMA_RATE_LIMIT,
)
from amber.dashboard.alerts import (  # noqa: F401 — re-exported for backward compat
    _compute_track_key,
    _is_within_alert_cooldown,
    _save_match_snapshot,
    _gemma_worker,
    CAPTURES_DIR,
)
from amber.dashboard.pipeline import (  # noqa: F401 — re-exported for backward compat
    _build_track_id_by_bbox,
    _frame_loop,
)

# ---------------------------------------------------------------------------
# Flask / SocketIO app
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)
app.config["SECRET_KEY"] = os.environ.get("AMBER_SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB upload limit

# CORS: default "*" for dev, restrict via AMBER_CORS_ORIGINS env var in production
cors_origins = os.environ.get("AMBER_CORS_ORIGINS", "*")
_cors_allowed = cors_origins if cors_origins == "*" else cors_origins.split(",")
socketio = SocketIO(app, cors_allowed_origins=_cors_allowed, async_mode="threading", max_http_buffer_size=10 * 1024 * 1024)

# --- API key authentication ---
_AMBER_API_KEY = os.environ.get("AMBER_API_KEY")

log = logging.getLogger(__name__)
if not os.environ.get("AMBER_API_KEY"):
    log.warning("AMBER_API_KEY not set — API endpoints are unauthenticated")
if not os.environ.get("AMBER_ENCRYPTION_KEY"):
    log.warning("AMBER_ENCRYPTION_KEY not set — biometric data stored unencrypted")


@app.before_request
def _check_api_key():
    """API key auth for REST endpoints. SocketIO auth is handled separately in on_connect()."""
    if _AMBER_API_KEY is None:
        return  # auth disabled — dev mode
    if request.path == "/api/health":
        return  # exempt
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth.encode(), f"Bearer {_AMBER_API_KEY}".encode()):
        return jsonify({"error": "unauthorized"}), 401

# Flask auto-instrumentation (optional)
try:
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    FlaskInstrumentor().instrument_app(app)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Pipeline initialisation
# ---------------------------------------------------------------------------

def _init_pipeline(source_config: SourceConfig, target_path=None):
    """Initialize the detection pipeline."""
    source = source_config.source
    mavlink_address = source_config.mavlink_address
    rtsp_url = source_config.rtsp_url
    edge_ws = source_config.edge_ws
    video_path = source_config.video_path

    if app_state.logger is None:
        app_state.logger = StructuredLogger(component="dashboard")
    if app_state.metrics is None:
        app_state.metrics = MetricsCollector()

    log = app_state.logger
    log.info("pipeline_init", source=source, target_path=target_path)

    if app_state.detector is None:
        app_state.detector = PersonDetector(model_name="yolo11n.pt", confidence=0.4)

    if app_state.reid is None:
        try:
            app_state.reid = PersonReID(match_threshold=0.55)
        except Exception as e:
            log.warning("reid_unavailable", error=str(e))

    if app_state.face is None:
        try:
            from amber.vision.face import FaceRecognizer
            app_state.face = FaceRecognizer(match_threshold=0.35)
        except Exception as e:
            log.warning("insightface_unavailable", error=str(e))

    if app_state.scorer is None:
        app_state.scorer = MatchScorer(match_threshold=0.45)

    if app_state.tracker is None:
        app_state.tracker = DetectionTracker(max_age=30, iou_threshold=0.3)

    if app_state.reasoning is None:
        try:
            from amber.reasoning.agent import AmberAgent
            app_state.reasoning = AmberAgent(model="gemma4:latest")
        except Exception as e:
            log.warning("gemma4_unavailable", error=str(e))

    # Start the async Gemma worker thread (only if reasoning is available and
    # not already running from a previous init call, e.g. restart_dashboard).
    if app_state.reasoning is not None:
        with _gemma_thread_lock:
            existing = app_state.gemma_thread
            if existing is None or not existing.is_alive():
                t = threading.Thread(target=_gemma_worker, args=(socketio,), daemon=True)
                t.start()
                app_state.gemma_thread = t

    if target_path and os.path.exists(target_path):
        app_state.reid.set_target_from_file(target_path)
        app_state.target_photo_path = target_path
        img = cv2.imread(target_path)
        _, buf = cv2.imencode(".jpg", img)
        app_state.target_photo = base64.b64encode(buf).decode("utf-8")
        # Also set face recognition target
        if app_state.face:
            app_state.face.set_target_from_file(target_path)

    # Initialize session persistence
    if app_state.db is None:
        app_state.db = SessionDB()
        log.info("session_db_initialized")

    # Initialize obstacle guard
    if app_state.obstacle_guard is None:
        try:
            from amber.drone.obstacle import ObstacleGuard
            app_state.obstacle_guard = ObstacleGuard()
            log.info("obstacle_guard_initialized")
        except Exception as e:
            log.warning("obstacle_guard_unavailable", error=str(e))

    # Initialize target canon
    if app_state.canon is None:
        app_state.canon = TargetCanon()
        log.info("target_canon_initialized")

    # OpenTelemetry
    if _HAS_TELEMETRY:
        otel_enabled = init_telemetry()
        if otel_enabled:
            app_state.tracer = get_tracer()
            app_state.otel_metrics = AmberMetrics(get_meter())
            log.info("opentelemetry_enabled")

    app_state.source_config = source_config
    app_state.source = source
    app_state.mavlink_address = mavlink_address
    app_state.rtsp_url = rtsp_url
    app_state.edge_ws = edge_ws
    app_state.video_path = video_path

    # Stop any auto-connect loop thread left running from a previous
    # _init_pipeline() call (e.g. via restart_dashboard) before starting a
    # new one. Without this, the old thread keeps polling in the
    # background and can register/reconnect a stale drone concurrently
    # with the new pipeline's own auto-connect loop.
    old_stop = app_state.auto_connect_stop
    if old_stop is not None:
        old_stop.set()
    stop_event = threading.Event()
    app_state.auto_connect_stop = stop_event

    if source == "tello":
        from amber.drone.tello import TelloController
        fleet = DroneFleet(factory=lambda n, h: TelloController(n, h))
        app_state.fleet = fleet
        def _auto_connect_loop():
            while not stop_event.is_set() and app_state.running:
                with fleet_lock:
                    primary: DroneController | None = fleet.primary
                if primary and primary.state.is_connected:
                    if stop_event.wait(3):
                        break
                    continue
                # Drone missing or disconnected -- clean up and retry
                with fleet_lock:
                    has_drone = "drone-1" in fleet.drone_ids
                if has_drone:
                    log.info("tello_disconnected", hint="cleaning up for reconnect")
                    with fleet_lock:
                        fleet.deregister("drone-1")
                    if stop_event.wait(2):  # let UDP sockets release
                        break
                with fleet_lock:
                    registered = fleet.register("drone-1")
                if registered:
                    log.info("tello_connected")
                    socketio.emit("drone_registered", {"drone_id": "drone-1", "success": True})
                else:
                    log.info("tello_waiting", hint="retrying in 5s")
                if stop_event.wait(5):
                    break
        threading.Thread(target=_auto_connect_loop, daemon=True).start()
    elif source == "mavlink":
        # Imported lazily so `mavsdk` is only required when actually used.
        from amber.drone.mavlink import MavlinkController
        fleet = DroneFleet(factory=lambda n, h: MavlinkController(n, h, rtsp_url=rtsp_url))
        app_state.fleet = fleet
        def _auto_connect_loop():
            while not stop_event.is_set() and app_state.running:
                with fleet_lock:
                    primary: DroneController | None = fleet.primary
                if primary and primary.state.is_connected:
                    if stop_event.wait(3):
                        break
                    continue
                # Drone missing or disconnected -- clean up and retry
                with fleet_lock:
                    has_drone = "drone-1" in fleet.drone_ids
                if has_drone:
                    log.info("mavlink_disconnected", hint="cleaning up for reconnect")
                    with fleet_lock:
                        fleet.deregister("drone-1")
                    if stop_event.wait(2):
                        break
                with fleet_lock:
                    registered = fleet.register("drone-1", host=mavlink_address)
                if registered:
                    log.info("mavlink_connected")
                    socketio.emit("drone_registered", {"drone_id": "drone-1", "success": True})
                else:
                    log.info("mavlink_waiting", hint="retrying in 5s")
                if stop_event.wait(5):
                    break
        threading.Thread(target=_auto_connect_loop, daemon=True).start()
    elif source == "webcam":
        app_state.fleet = None
        app_state.cap = cv2.VideoCapture(0)
    elif source == "file":
        app_state.fleet = None
        if not video_path:
            log.error(
                "file_source_missing_video",
                hint="--source=file requires a video path (--video); no frames will be produced",
            )
            app_state.cap = None
        elif not os.path.exists(video_path):
            log.error(
                "file_source_invalid_path",
                video_path=video_path,
                hint="video file not found; no frames will be produced",
            )
            app_state.cap = None
        else:
            app_state.cap = cv2.VideoCapture(video_path)
    elif source == "edge":
        # No local drone fleet or capture device -- frames are expected to
        # arrive via the EdgeRunner/GroundStation WebSocket bridge
        # (amber/edge.py, amber/ground.py). Wiring the frame loop to
        # consume from ``edge_ws`` is tracked separately.
        log.warning(
            "edge_source_stub",
            hint="Edge source mode is not yet fully implemented -- dashboard will not show live video until edge transport is connected",
        )
        app_state.fleet = None
        app_state.cap = None
    else:
        # Backward-compat: treat any other source string as a video path
        # (e.g. direct callers of run_dashboard()/_init_pipeline() that
        # predate the --source enum, such as amber/main.py --dashboard).
        app_state.fleet = None
        app_state.cap = cv2.VideoCapture(source)

    # Create a new search session
    app_state.session_id = app_state.db.create_session(
        source=app_state.source,
        target_photo_path=app_state.target_photo_path,
        target_description=app_state.target_description,
    )

    log.info("pipeline_ready", source=app_state.source, session_id=app_state.session_id)


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload-target", methods=["POST"])
def upload_target():
    """HTTP fallback for target photo upload (bypasses WebSocket size limits)."""
    from flask import request
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    img_data = file.read()
    nparr = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Invalid image"}), 400

    app_state.reid.set_target(img)
    path = Path(__file__).parent.parent.parent / "target_reference.jpg"
    cv2.imwrite(str(path), img)
    app_state.target_photo_path = str(path)
    _, buf = cv2.imencode(".jpg", img)
    app_state.target_photo = base64.b64encode(buf).decode("utf-8")
    if app_state.canon:
        app_state.canon.set_target(img, operator_id="dashboard")
    face_ok = False
    if app_state.face:
        face_ok = app_state.face.set_target(img)
    print(f"[upload-target] Target saved via HTTP POST ({len(img_data)//1024}KB)")
    return jsonify({"success": True, "face_detected": face_ok})


@app.route("/api/clear-target", methods=["POST"])
def clear_target():
    """Clear the current target photo."""
    app_state.target_photo = None
    app_state.target_photo_path = None
    if app_state.reid:
        app_state.reid.clear_target()
    if app_state.face:
        app_state.face.clear_target()
    path = Path(__file__).parent.parent.parent / "target_reference.jpg"
    if path.exists():
        path.unlink()
    print("[clear-target] Target photo cleared")
    return jsonify({"success": True})


@app.route("/api/health")
def health():
    return jsonify({
        "status": "healthy",
        "version": "1.0.0",
        "components": {
            "detector": app_state.detector is not None,
            "reid": app_state.reid is not None,
            "face": app_state.face is not None,
            "reasoning": app_state.reasoning is not None,
            "db": app_state.db is not None,
        }
    })


@app.route("/api/metrics")
def metrics_endpoint():
    if app_state.metrics:
        return jsonify(app_state.metrics.snapshot())
    return jsonify({})


@app.route("/api/status")
def status():
    return jsonify({
        "running": app_state.running,
        "source": app_state.source,
        "fps": app_state.fps,
        "persons_detected": app_state.persons_detected,
        "has_target": app_state.target_photo is not None,
        "has_description": app_state.target_description is not None,
        "has_reasoning": app_state.reasoning is not None,
        "has_face": app_state.face is not None and app_state.face.has_target if app_state.face else False,
        "match_history": app_state.match_history[-10:],
        "telemetry": app_state.drone_telemetry,
    })


@app.route("/api/sessions")
def api_sessions():
    """Return recent search sessions."""
    db = app_state.db
    if not db:
        return jsonify([])
    limit = request.args.get("limit", 20, type=int)
    return jsonify(db.get_recent_sessions(limit=limit))


@app.route("/api/sessions/<session_id>")
def api_session_detail(session_id):
    """Return a single session with its matches."""
    db = app_state.db
    if not db:
        return jsonify({"error": "no database"}), 500
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "not found"}), 404
    session["matches"] = db.get_session_matches(session_id)
    return jsonify(session)


@app.route("/api/match-stats")
def api_match_stats():
    """Return aggregate match statistics."""
    db = app_state.db
    if not db:
        return jsonify({})
    return jsonify(db.get_match_stats())


@app.route("/api/matches/<int:match_id>/feedback", methods=["POST"])
def api_match_feedback(match_id):
    """Record operator feedback for a match."""
    db = app_state.db
    if not db:
        return jsonify({"error": "no database"}), 500
    data = request.get_json(silent=True) or {}
    feedback = data.get("feedback")
    if feedback not in ("confirmed", "rejected"):
        return jsonify({"error": "feedback must be 'confirmed' or 'rejected'"}), 400
    session_id = app_state.session_id or "unknown"
    notes = data.get("notes")
    db.add_feedback(match_id, session_id, feedback, notes)
    return jsonify({"ok": True, "match_id": match_id, "feedback": feedback})


@app.route("/api/feedback-stats")
def api_feedback_stats():
    """Return aggregate feedback statistics."""
    db = app_state.db
    if not db:
        return jsonify({})
    return jsonify(db.get_feedback_stats())


@app.route("/api/export-eval-dataset", methods=["POST"])
def api_export_eval_dataset():
    """Export feedback as evaluation dataset JSON."""
    db = app_state.db
    if not db:
        return jsonify({"error": "no database"}), 500
    output_path = str(Path(__file__).parent.parent.parent / "eval_dataset.json")
    count = db.export_eval_dataset(output_path)
    return jsonify({"ok": True, "path": output_path, "count": count})


@app.route("/api/target-history")
def target_history():
    canon = app_state.canon
    if not canon:
        return jsonify([])
    return jsonify(canon.get_history())


@app.route("/api/threshold-suggestion")
def api_threshold_suggestion():
    """Analyze feedback and suggest an optimal match threshold."""
    db = app_state.db
    if not db:
        return jsonify({"error": "no database"}), 500
    scorer = app_state.scorer
    current = scorer.match_threshold if scorer else 0.50
    tuner = ThresholdTuner(db)
    return jsonify(tuner.analyze(current_threshold=current))


# --- WebSocket Events ---

@socketio.on("connect")
def on_connect(auth=None):
    """SocketIO auth is handled here, separate from REST auth in _check_api_key."""
    if _AMBER_API_KEY is not None:
        provided = (auth or {}).get("api_key") if isinstance(auth, dict) else None
        if provided is None or not hmac.compare_digest(provided.encode(), _AMBER_API_KEY.encode()):
            return False  # reject connection
    emit("status", {"connected": True, "source": app_state.source})
    if app_state.target_photo:
        emit("target_photo", {"image": app_state.target_photo})


@socketio.on("set_target")
def on_set_target(data):
    """Receive a target photo as base64."""
    print(f"[set_target] Received photo upload ({len(data.get('image', ''))//1024}KB)")
    img_data = base64.b64decode(data["image"])
    nparr = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is not None:
        app_state.reid.set_target(img)
        app_state.target_photo = data["image"]
        path = Path(__file__).parent.parent.parent / "target_reference.jpg"
        cv2.imwrite(str(path), img)
        app_state.target_photo_path = str(path)
        print(f"[set_target] Target saved to {path}, ReID target set")
        # Store in target canon
        if app_state.canon:
            app_state.canon.set_target(img, operator_id=data.get("operator_id", "dashboard"))
        # Set face recognition target
        face_ok = False
        if app_state.face:
            face_ok = app_state.face.set_target(img)
        # Score input image quality
        quality_scorer = ImageQualityScorer()
        quality_report = quality_scorer.score(img)
        emit("target_set", {
            "success": True,
            "face_detected": face_ok,
            "quality": {
                "overall_score": quality_report.overall_score,
                "grade": quality_report.grade,
                "issues": quality_report.issues,
                "suggestions": quality_report.suggestions,
                "dimensions": quality_report.dimensions,
            },
        })


@socketio.on("revert_target")
def on_revert_target(data):
    version_id = data.get("version_id")
    canon = app_state.canon
    if not canon or not version_id:
        emit("error", {"message": "Cannot revert target"})
        return
    img = canon.revert_to(version_id)
    if img is None:
        emit("error", {"message": "Target version not found"})
        return
    _, buf = cv2.imencode(".jpg", img)
    app_state.target_photo = base64.b64encode(buf).decode("utf-8")
    if app_state.reid:
        app_state.reid.set_target(img)
    face_ok = False
    if app_state.face:
        face_ok = app_state.face.set_target(img)
    emit("target_set", {"success": True, "face_detected": face_ok, "reverted_to": version_id})


@socketio.on("set_description")
def on_set_description(data):
    """Set a text description of the child to find."""
    desc = data.get("description", "").strip()
    if desc:
        app_state.target_description = desc
        if app_state.logger:
            app_state.logger.info("target_description_set", description=desc)
        emit("description_set", {"description": desc})


@socketio.on("set_threshold")
def on_set_threshold(data):
    """Update the ReID match threshold."""
    threshold = data.get("threshold", 0.55)
    threshold = max(0.1, min(0.99, float(threshold)))
    if app_state.reid:
        app_state.reid.match_threshold = threshold
    if app_state.scorer:
        app_state.scorer.match_threshold = threshold
    if app_state.logger:
        app_state.logger.info("threshold_updated", threshold=threshold)
    emit("threshold_updated", {"threshold": threshold})


@socketio.on("drone_command")
def on_drone_command(data):
    """Send a command to the drone."""
    drone_id = data.get("drone_id")
    fleet = app_state.fleet
    if not fleet or not fleet.primary:
        emit("error", {"message": "No drones connected"})
        return
    drone: DroneController | None = fleet.get(drone_id) if drone_id else fleet.primary
    if not drone:
        emit("error", {"message": f"Drone '{drone_id}' not found"})
        return

    cmd = data.get("command")

    commands = {
        "takeoff": lambda: drone.takeoff(),
        "land": lambda: drone.land(),
        "hover": lambda: drone.hover(),
        "up": lambda: drone.move("up", data.get("distance", 30)),
        "down": lambda: drone.move("down", data.get("distance", 30)),
        "forward": lambda: drone.move("forward", data.get("distance", 30)),
        "back": lambda: drone.move("back", data.get("distance", 30)),
        "left": lambda: drone.move("left", data.get("distance", 30)),
        "right": lambda: drone.move("right", data.get("distance", 30)),
        "cw": lambda: drone.rotate(data.get("degrees", 45)),
        "ccw": lambda: drone.rotate(-data.get("degrees", 45)),
    }

    action = commands.get(cmd)
    if action:
        try:
            action()
        except Exception as e:
            emit("error", {"message": f"Command '{cmd}' failed: {e}"})
            return
    if app_state.logger:
        app_state.logger.drone_command(command=cmd)
    emit("command_ack", {"command": cmd})


@socketio.on("start_search")
def on_start_search(data):
    """Start an autonomous search pattern."""
    fleet = app_state.fleet
    drone: DroneController | None = fleet.primary if fleet else None
    if not drone or not drone.state.is_flying:
        emit("error", {"message": "Drone must be flying to start search"})
        return

    from amber.drone.search import get_search_pattern, PatternType

    pattern_name = data.get("pattern", "expanding_square")
    pattern_type = PatternType(pattern_name)
    waypoints = get_search_pattern(pattern_type)

    app_state.search_active = True
    emit("search_started", {"pattern": pattern_name, "waypoints": len(waypoints)})

    def _execute_search():
        MAX_AVOIDANCE_RETRIES = 5
        obstacle_guard = app_state.obstacle_guard

        i = 0
        while i < len(waypoints):
            if not app_state.search_active:
                break

            wp = waypoints[i]
            avoidance_retries = 0
            frame_warned = False
            skip_waypoint = False

            # Obstacle check loop for this waypoint
            while avoidance_retries < MAX_AVOIDANCE_RETRIES:
                if not app_state.search_active:
                    break
                if not (obstacle_guard and drone):
                    break

                frame = drone.get_frame()
                if frame is None:
                    if not frame_warned:
                        socketio.emit("warning", {"message": "No video frame -- obstacle check skipped"})
                        frame_warned = True
                    break

                check = obstacle_guard.check_path(frame)
                if check["safe"]:
                    break

                avoidance_retries += 1
                socketio.emit("obstacle_detected", {
                    "action": check["action"],
                    "center_depth": round(check["center_depth"], 3),
                    "waypoint": i + 1,
                    "retry": avoidance_retries,
                })

                if avoidance_retries >= MAX_AVOIDANCE_RETRIES:
                    socketio.emit("warning", {
                        "message": f"Max avoidance retries ({MAX_AVOIDANCE_RETRIES}) reached at waypoint {i + 1}, skipping"
                    })
                    skip_waypoint = True
                    break

                try:
                    if check["action"] == "go_left":
                        drone.move("left", 30)
                        time.sleep(0.5)
                    elif check["action"] == "go_right":
                        drone.move("right", 30)
                        time.sleep(0.5)
                    elif check["action"] == "reverse":
                        drone.move("back", 50)
                        time.sleep(0.5)
                        drone.rotate(90)
                        time.sleep(0.5)
                except Exception as e:
                    socketio.emit("error", {"message": f"Avoidance error: {e}"})
                    break

            if skip_waypoint:
                i += 1
                continue

            socketio.emit("search_progress", {
                "waypoint": i + 1,
                "total": len(waypoints),
                "action": str(wp),
            })
            try:
                drone.move(wp.direction, wp.distance_cm)
                if wp.rotate_degrees:
                    drone.rotate(wp.rotate_degrees)
                time.sleep(0.5)
            except Exception as e:
                socketio.emit("error", {"message": f"Search error: {e}"})
                break

            i += 1

        app_state.search_active = False
        socketio.emit("search_complete", {})

    threading.Thread(target=_execute_search, daemon=True).start()


@socketio.on("stop_search")
def on_stop_search():
    app_state.search_active = False
    fleet = app_state.fleet
    if fleet and fleet.primary:
        fleet.primary.hover()


@socketio.on("start_recording")
def on_start_recording():
    """Start recording the session."""
    if app_state.recorder is None:
        app_state.recorder = SessionRecorder()
    path = app_state.recorder.start()
    emit("recording_started", {"path": path})


@socketio.on("stop_recording")
def on_stop_recording():
    """Stop recording."""
    if app_state.recorder:
        path = app_state.recorder.stop()
        emit("recording_stopped", {"path": path})


@socketio.on("register_drone")
def on_register_drone(data):
    drone_id = data.get("drone_id", f"drone-{(app_state.fleet.count if app_state.fleet else 0) + 1}")
    host = data.get("host", "192.168.10.1")
    current_source = app_state.source
    with fleet_lock:
        fleet = app_state.fleet
        if not fleet:
            # Build a fleet whose backend matches the currently configured
            # source, instead of always defaulting to Tello -- a manual
            # registration on a mavlink deployment must build a
            # MavlinkController, not silently create a Tello-backed fleet.
            if current_source == "mavlink":
                from amber.drone.mavlink import MavlinkController
                rtsp_url = app_state.rtsp_url
                fleet = DroneFleet(factory=lambda n, h: MavlinkController(n, h, rtsp_url=rtsp_url))
            else:
                fleet = DroneFleet()
            app_state.fleet = fleet

    # Check for duplicate host before attempting connection
    if fleet.has_host(host):
        emit("drone_registered", {"drone_id": drone_id, "success": False, "error": f"A drone is already connected at {host}"})
        return

    # Notify client that registration is in progress
    emit("drone_registered", {"drone_id": drone_id, "success": None, "pending": True})

    # Connect in background thread so we don't block the socket
    def _bg_register():
        success = fleet.register(drone_id, host)
        if success:
            socketio.emit("drone_registered", {"drone_id": drone_id, "success": True})
            # Switch source to a drone-backed source if we were on a
            # non-drone fallback (webcam/file/edge/unset). Never stomp an
            # already-correct drone source -- unconditionally forcing
            # "tello" here previously flipped mavlink deployments back to
            # tello on every manual registration.
            if app_state.source not in ("tello", "mavlink"):
                new_source = "mavlink" if current_source == "mavlink" else "tello"
                app_state.source = new_source
                if app_state.source_config is not None:
                    app_state.source_config.source = new_source
                cap = app_state.cap
                if cap:
                    cap.release()
                    app_state.cap = None
        else:
            socketio.emit("drone_registered", {"drone_id": drone_id, "success": False, "error": "Connection timed out"})
        socketio.emit("fleet_status", {"drones": fleet.get_all_telemetry(), "count": fleet.count})
    threading.Thread(target=_bg_register, daemon=True).start()


@socketio.on("deregister_drone")
def on_deregister_drone(data):
    drone_id = data.get("drone_id")
    with fleet_lock:
        fleet = app_state.fleet
        if fleet and drone_id:
            fleet.deregister(drone_id)
    emit("fleet_status", {
        "drones": fleet.get_all_telemetry() if fleet else {},
        "count": fleet.count if fleet else 0
    })


@socketio.on("remove_all_drones")
def on_remove_all_drones():
    fleet = app_state.fleet
    if fleet:
        fleet.disconnect_all()
    emit("fleet_status", {
        "drones": fleet.get_all_telemetry() if fleet else {},
        "count": fleet.count if fleet else 0
    })


@socketio.on("get_fleet_status")
def on_get_fleet_status():
    fleet = app_state.fleet
    emit("fleet_status", {
        "drones": fleet.get_all_telemetry() if fleet else {},
        "count": fleet.count if fleet else 0
    })


@socketio.on("set_primary_drone")
def on_set_primary_drone(data):
    fleet = app_state.fleet
    drone_id = data.get("drone_id")
    if fleet and drone_id and fleet.set_primary(drone_id):
        emit("primary_set", {"drone_id": drone_id, "success": True})
    else:
        emit("primary_set", {"drone_id": drone_id, "success": False})


@socketio.on("restart_dashboard")
def on_restart_dashboard():
    fleet = app_state.fleet
    if fleet:
        fleet.disconnect_all()
    # Release any existing capture device before re-initializing -- leaving
    # it open leaks the webcam/video-file handle across restarts.
    cap = app_state.cap
    if cap:
        cap.release()
        app_state.cap = None
    source_config = app_state.source_config or SourceConfig(
        source=app_state.source or "tello",
        mavlink_address=app_state.mavlink_address or "udp://:14540",
        rtsp_url=app_state.rtsp_url,
        edge_ws=app_state.edge_ws or "ws://localhost:9000",
        video_path=app_state.video_path,
    )
    _init_pipeline(source_config, target_path=app_state.target_photo_path)
    emit("dashboard_restarted", {})


def run_dashboard(source_config: SourceConfig, target_path=None, port=5555):
    """Start the dashboard server.

    Args:
        source_config: Video/telemetry source configuration -- see
            ``SourceConfig`` (source mode, MAVLink address, RTSP URL, edge
            WebSocket URL, video path).
        target_path: Path to a reference photo of the target.
        port: Dashboard HTTP/WebSocket port.
    """
    app_state.running = True
    # Auto-load previous target photo if none specified
    if target_path is None:
        default_target = Path(__file__).parent.parent.parent / "target_reference.jpg"
        if default_target.exists():
            target_path = str(default_target)
    _init_pipeline(source_config, target_path=target_path)

    frame_thread = threading.Thread(target=_frame_loop, args=(socketio,), daemon=True)
    frame_thread.start()

    if app_state.logger:
        app_state.logger.info("dashboard_started", url=f"http://localhost:{port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
