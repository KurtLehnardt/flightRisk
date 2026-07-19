"""Amber Drone web dashboard.

Real-time web UI showing drone video feed, detection overlays,
match alerts, drone telemetry, and search controls.

Runs on http://localhost:5555
"""

import base64
import json
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

from amber.vision.detector import PersonDetector
from amber.vision.reid import PersonReID

# Match screenshots directory
CAPTURES_DIR = Path(__file__).parent.parent.parent / "captures"
CAPTURES_DIR.mkdir(exist_ok=True)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)
app.config["SECRET_KEY"] = "amber-drone-2026"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Global state
_state = {
    "drone": None,
    "detector": None,
    "reid": None,
    "reasoning": None,
    "source": None,
    "cap": None,
    "running": False,
    "target_photo": None,
    "target_photo_path": None,
    "target_description": None,
    "match_history": [],
    "drone_telemetry": {},
    "fps": 0,
    "persons_detected": 0,
    "search_active": False,
}


def _init_pipeline(source="webcam", target_path=None):
    """Initialize the detection pipeline."""
    print("[dashboard] Initializing pipeline...")

    if _state["detector"] is None:
        _state["detector"] = PersonDetector(model_name="yolo11n.pt", confidence=0.4)

    if _state["reid"] is None:
        _state["reid"] = PersonReID(match_threshold=0.55)

    if _state["reasoning"] is None:
        try:
            from amber.reasoning.agent import AmberAgent
            _state["reasoning"] = AmberAgent(model="gemma4:latest")
        except Exception as e:
            print(f"[dashboard] Gemma 4 not available: {e}")

    if target_path and os.path.exists(target_path):
        _state["reid"].set_target_from_file(target_path)
        _state["target_photo_path"] = target_path
        img = cv2.imread(target_path)
        _, buf = cv2.imencode(".jpg", img)
        _state["target_photo"] = base64.b64encode(buf).decode("utf-8")

    _state["source"] = source
    if source == "tello":
        from amber.drone.tello import TelloController
        drone = TelloController()
        if drone.connect():
            _state["drone"] = drone
        else:
            print("[dashboard] Tello connection failed, falling back to webcam")
            _state["source"] = "webcam"
            _state["cap"] = cv2.VideoCapture(0)
    elif source == "webcam":
        _state["cap"] = cv2.VideoCapture(0)
    else:
        _state["cap"] = cv2.VideoCapture(source)

    print("[dashboard] Pipeline ready.")


def _save_match_snapshot(frame, crop, match_score, reasoning_result):
    """Save a match screenshot and crop to disk."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    frame_path = CAPTURES_DIR / f"match_{ts}_frame.jpg"
    crop_path = CAPTURES_DIR / f"match_{ts}_crop.jpg"

    cv2.imwrite(str(frame_path), frame)
    if crop is not None and crop.size > 0:
        cv2.imwrite(str(crop_path), crop)

    # Save metadata
    meta_path = CAPTURES_DIR / f"match_{ts}_meta.json"
    meta = {
        "timestamp": ts,
        "score": match_score,
        "reasoning": reasoning_result,
        "frame_file": frame_path.name,
        "crop_file": crop_path.name,
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"[captures] Saved match snapshot: {frame_path.name}")


def _frame_loop():
    """Main frame processing loop — runs in a background thread."""
    frame_count = 0
    fps_start = time.time()
    last_reasoning_time = 0
    REASONING_INTERVAL = 5

    while _state["running"]:
        frame = None
        if _state["drone"]:
            frame = _state["drone"].get_frame()
        elif _state["cap"] and _state["cap"].isOpened():
            ret, frame = _state["cap"].read()
            if not ret:
                time.sleep(0.01)
                continue

        if frame is None:
            time.sleep(0.01)
            continue

        detections = _state["detector"].detect(frame)
        _state["persons_detected"] = len(detections)

        # ReID matching (photo-based)
        match_idx = None
        match_score = 0.0
        has_target = _state["target_photo"] is not None

        if _state["reid"] and has_target and detections:
            match_idx, match_score = _state["reid"].find_match(detections)

        # Description-based matching via Gemma 4 (when no photo but description exists)
        description_match = False
        if (
            match_idx is None
            and _state["target_description"]
            and _state["reasoning"]
            and detections
            and time.time() - last_reasoning_time > REASONING_INTERVAL
        ):
            # Ask Gemma 4 to check each detected person against description
            best_candidate = None
            if len(detections) > 0:
                # Pick the largest detection (most prominent person)
                areas = [(d["bbox"][2]-d["bbox"][0]) * (d["bbox"][3]-d["bbox"][1]) for d in detections]
                best_candidate = int(np.argmax(areas))

            if best_candidate is not None:
                crop = detections[best_candidate]["crop"]
                if crop is not None and crop.size > 0:
                    result = _state["reasoning"].match_description(
                        crop, _state["target_description"]
                    )
                    last_reasoning_time = time.time()

                    if result.get("match"):
                        match_idx = best_candidate
                        match_score = 0.8  # synthetic score for description match
                        description_match = True

                        snapshot_b64 = None
                        _, sbuf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
                        snapshot_b64 = base64.b64encode(sbuf).decode("utf-8")

                        match_entry = {
                            "time": time.strftime("%H:%M:%S"),
                            "score": round(match_score, 3),
                            "gemma_match": True,
                            "gemma_confidence": result.get("confidence", "medium"),
                            "reasoning": result.get("reasoning", "Description match"),
                            "snapshot": snapshot_b64,
                            "type": "description",
                        }
                        _state["match_history"].append(match_entry)
                        _state["match_history"] = _state["match_history"][-50:]
                        socketio.emit("match_alert", match_entry)
                        _save_match_snapshot(frame, crop, match_score, result)

        # Photo-based ReID + Gemma 4 reasoning
        if (
            match_idx is not None
            and not description_match
            and _state["reasoning"]
            and _state["target_photo_path"]
            and time.time() - last_reasoning_time > REASONING_INTERVAL
        ):
            ref_img = cv2.imread(_state["target_photo_path"])
            candidate_crop = detections[match_idx]["crop"]
            result = _state["reasoning"].analyze_match(ref_img, candidate_crop)
            last_reasoning_time = time.time()

            snapshot_b64 = None
            if candidate_crop is not None and candidate_crop.size > 0:
                _, sbuf = cv2.imencode(".jpg", candidate_crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
                snapshot_b64 = base64.b64encode(sbuf).decode("utf-8")

            match_entry = {
                "time": time.strftime("%H:%M:%S"),
                "score": round(match_score, 3),
                "gemma_match": result["match"],
                "gemma_confidence": result["confidence"],
                "reasoning": result["reasoning"],
                "snapshot": snapshot_b64,
                "type": "photo",
            }
            _state["match_history"].append(match_entry)
            _state["match_history"] = _state["match_history"][-50:]
            socketio.emit("match_alert", match_entry)
            _save_match_snapshot(frame, candidate_crop, match_score, result)

        # Annotate frame
        annotated = _state["detector"].annotate(frame, detections, match_idx)

        if match_idx is not None:
            h, w = annotated.shape[:2]
            cv2.rectangle(annotated, (0, 0), (w, 45), (0, 0, 200), -1)
            label = "CHILD FOUND" if not description_match else "DESCRIPTION MATCH"
            cv2.putText(
                annotated, f"{label} — Score: {match_score:.2f}",
                (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2,
            )

        # FPS
        frame_count += 1
        elapsed = time.time() - fps_start
        if elapsed >= 1.0:
            _state["fps"] = round(frame_count / elapsed, 1)
            frame_count = 0
            fps_start = time.time()

        # Encode and emit
        _, buffer = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 70])
        frame_b64 = base64.b64encode(buffer).decode("utf-8")

        telemetry = {}
        if _state["drone"]:
            s = _state["drone"].state
            telemetry = {
                "battery": s.battery,
                "height": s.height,
                "temperature": s.temperature,
                "flight_time": s.flight_time,
                "is_flying": s.is_flying,
            }
        _state["drone_telemetry"] = telemetry

        socketio.emit("frame", {
            "image": frame_b64,
            "fps": _state["fps"],
            "persons": _state["persons_detected"],
            "match": match_idx is not None,
            "match_score": round(match_score, 3),
            "telemetry": telemetry,
        })

        time.sleep(0.05)


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    return jsonify({
        "running": _state["running"],
        "source": _state["source"],
        "fps": _state["fps"],
        "persons_detected": _state["persons_detected"],
        "has_target": _state["target_photo"] is not None,
        "has_description": _state["target_description"] is not None,
        "has_reasoning": _state["reasoning"] is not None,
        "match_history": _state["match_history"][-10:],
        "telemetry": _state["drone_telemetry"],
    })


# --- WebSocket Events ---

@socketio.on("connect")
def on_connect():
    emit("status", {"connected": True, "source": _state["source"]})
    if _state["target_photo"]:
        emit("target_photo", {"image": _state["target_photo"]})


@socketio.on("set_target")
def on_set_target(data):
    """Receive a target photo as base64."""
    img_data = base64.b64decode(data["image"])
    nparr = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is not None:
        _state["reid"].set_target(img)
        _state["target_photo"] = data["image"]
        path = Path(__file__).parent.parent.parent / "target_reference.jpg"
        cv2.imwrite(str(path), img)
        _state["target_photo_path"] = str(path)
        emit("target_set", {"success": True})


@socketio.on("set_description")
def on_set_description(data):
    """Set a text description of the child to find."""
    desc = data.get("description", "").strip()
    if desc:
        _state["target_description"] = desc
        print(f"[dashboard] Target description set: {desc}")
        emit("description_set", {"description": desc})


@socketio.on("set_threshold")
def on_set_threshold(data):
    """Update the ReID match threshold."""
    threshold = data.get("threshold", 0.55)
    threshold = max(0.1, min(0.99, float(threshold)))
    if _state["reid"]:
        _state["reid"].match_threshold = threshold
        print(f"[dashboard] Match threshold set to {threshold:.2f}")
    emit("threshold_updated", {"threshold": threshold})


@socketio.on("drone_command")
def on_drone_command(data):
    """Send a command to the drone."""
    if not _state["drone"]:
        emit("error", {"message": "No drone connected"})
        return

    cmd = data.get("command")
    drone = _state["drone"]

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
        action()
    emit("command_ack", {"command": cmd})


@socketio.on("start_search")
def on_start_search(data):
    """Start an autonomous search pattern."""
    if not _state["drone"] or not _state["drone"].state.is_flying:
        emit("error", {"message": "Drone must be flying to start search"})
        return

    from amber.drone.search import get_search_pattern, PatternType

    pattern_name = data.get("pattern", "expanding_square")
    pattern_type = PatternType(pattern_name)
    waypoints = get_search_pattern(pattern_type)

    _state["search_active"] = True
    emit("search_started", {"pattern": pattern_name, "waypoints": len(waypoints)})

    def _execute_search():
        drone = _state["drone"]
        for i, wp in enumerate(waypoints):
            if not _state["search_active"]:
                break
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

        _state["search_active"] = False
        socketio.emit("search_complete", {})

    threading.Thread(target=_execute_search, daemon=True).start()


@socketio.on("stop_search")
def on_stop_search():
    _state["search_active"] = False
    if _state["drone"]:
        _state["drone"].hover()


def run_dashboard(source="webcam", target_path=None, port=5555):
    """Start the dashboard server."""
    _init_pipeline(source=source, target_path=target_path)
    _state["running"] = True

    frame_thread = threading.Thread(target=_frame_loop, daemon=True)
    frame_thread.start()

    print(f"\n  Amber Drone Dashboard: http://localhost:{port}\n")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
