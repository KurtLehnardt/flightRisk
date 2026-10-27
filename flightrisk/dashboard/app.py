"""FlightRisk web dashboard.

Real-time web UI showing drone video feed, detection overlays,
match alerts, drone telemetry, and search controls.

Runs on http://127.0.0.1:5555. It binds the loopback address unless
``--host`` (or ``FLIGHTRISK_DASHBOARD_HOST``) names another one, and it
refuses a non-loopback bind unless ``FLIGHTRISK_API_KEY`` is set, because
anyone who can reach the dashboard can start and land the drones.
"""

import base64
import hmac
import logging
import math
import os
import secrets
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

from flightrisk.vision.detector import PersonDetector
from flightrisk.vision.reid import PersonReID
from flightrisk.vision.quality import ImageQualityScorer
from flightrisk.vision.scorer import MatchScorer
from flightrisk.vision.signal_registry import SignalRegistry
from flightrisk.vision.threshold_tuner import ThresholdTuner
from flightrisk.vision.tracker import DetectionTracker
from flightrisk.config import get_config
from flightrisk.recorder import SessionRecorder
from flightrisk.observability import StructuredLogger, MetricsCollector
from flightrisk.persistence import SessionDB
from flightrisk.drone.fleet import DroneFleet
from flightrisk.drone.controller import DroneController
from flightrisk.drone.coordinator import FleetCoordinator
from flightrisk.drone.safety import DEFAULT_GEOFENCE_CM, GEOFENCE_CM_ENV
from flightrisk import fleet_protocol as fp
from flightrisk.canon import TargetCanon

try:
    from flightrisk.telemetry import init_telemetry, get_tracer, get_meter, FlightRiskMetrics
    _HAS_TELEMETRY = True
except ImportError:
    _HAS_TELEMETRY = False

# ---------------------------------------------------------------------------
# Import decomposed modules
# ---------------------------------------------------------------------------
from flightrisk.dashboard.state import (  # noqa: F401 — re-exported for backward compat
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
from flightrisk.dashboard.alerts import (  # noqa: F401 — re-exported for backward compat
    _compute_track_key,
    _is_within_alert_cooldown,
    _save_match_snapshot,
    _gemma_worker,
    CAPTURES_DIR,
)
from flightrisk.dashboard.pipeline import (  # noqa: F401 — re-exported for backward compat
    _build_track_id_by_bbox,
    _frame_loop,
)
from flightrisk.dashboard.alert_latch import AlertPhase, alert_latch
from flightrisk.dashboard.recovery import handle_alert_response, reset_alert_flow

# ---------------------------------------------------------------------------
# Flask / SocketIO app
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)
app.config["SECRET_KEY"] = os.environ.get("FLIGHTRISK_SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB upload limit

# --- Socket.IO origins (CORS) ---
#: Environment variable that replaces the Socket.IO allowed origins for
#: development: a comma-separated list of origins, or ``*`` for any origin.
CORS_ORIGINS_ENV = "FLIGHTRISK_CORS_ORIGINS"

#: Bind addresses that listen on every interface of this machine.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})

#: The host and port the dashboard serves, recorded by ``run_dashboard``.
#: Until then the configured defaults are used.
_dashboard_bind: tuple[str, int] | None = None


def cors_origins_override() -> list[str] | None:
    """The origins named in ``FLIGHTRISK_CORS_ORIGINS``, or ``None`` when it is unset or empty."""
    raw = os.environ.get(CORS_ORIGINS_ENV, "")
    origins = [part.strip() for part in raw.split(",") if part.strip()]
    return origins or None


def dashboard_origins(host: str, port: int) -> frozenset[str]:
    """The browser origins of the dashboard page when it is served on ``host``:``port``.

    They are the loopback spellings ``http://127.0.0.1:<port>``,
    ``http://localhost:<port>`` and ``http://[::1]:<port>``, plus
    ``http://<host>:<port>`` when ``host`` names one specific non-loopback
    address.
    """
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
    if host not in _LOOPBACK_HOSTS and host not in _WILDCARD_HOSTS:
        url_host = f"[{host}]" if ":" in host else host
        origins.add(f"http://{url_host}:{port}")
    return frozenset(origins)


def socketio_origin_allowed(origin: str | None, environ: dict | None = None) -> bool:
    """Whether a browser page from ``origin`` may open a Socket.IO connection.

    By default only the dashboard's own origins are accepted (see
    ``dashboard_origins``), so a page from another site that is open in this
    machine's browser cannot connect and emit ``start_fleet`` or any other
    control event. The list is fixed rather than taken from the request's
    Host header, so a DNS-rebinding page cannot pass as the dashboard either.
    A non-loopback bind is the one exception: there the page may be reached
    under any name of this machine, so the origin of the Host the request was
    sent to is also accepted. Such a bind always requires
    ``FLIGHTRISK_API_KEY``, and every Socket.IO connection must present it
    (``on_connect``).

    ``FLIGHTRISK_CORS_ORIGINS`` replaces this check for development: its
    listed origins are accepted, and ``*`` accepts any origin. Engine.IO
    checks the origin only for requests that carry an ``Origin`` header,
    which browsers send on every cross-origin request and WebSocket upgrade.
    """
    if not origin:
        return False
    override = cors_origins_override()
    if override is not None:
        return "*" in override or origin in override
    host, port = _dashboard_bind or (resolve_dashboard_host(), get_config().dashboard.port)
    if origin in dashboard_origins(host, port):
        return True
    if host in _LOOPBACK_HOSTS or not environ:
        return False
    request_host = environ.get("HTTP_HOST")
    scheme = environ.get("wsgi.url_scheme", "http")
    return bool(request_host) and origin == f"{scheme}://{request_host}"


socketio = SocketIO(
    app,
    cors_allowed_origins=lambda origin, environ=None: socketio_origin_allowed(origin, environ),
    async_mode="threading",
    max_http_buffer_size=10 * 1024 * 1024,
)

# --- API key authentication ---
# Empty string is treated as "unset" so an accidental FLIGHTRISK_API_KEY="" never
# half-enables auth with a blank key.
_FLIGHTRISK_API_KEY = os.environ.get("FLIGHTRISK_API_KEY") or None

log = logging.getLogger(__name__)
if not _FLIGHTRISK_API_KEY:
    log.warning("FLIGHTRISK_API_KEY not set — API endpoints are unauthenticated")
if not os.environ.get("FLIGHTRISK_ENCRYPTION_KEY"):
    log.warning("FLIGHTRISK_ENCRYPTION_KEY not set — biometric data stored unencrypted")


# Public paths that stay reachable even when an API key is configured, so the
# dashboard still loads in a browser. Everything else is deny-by-default.
_PUBLIC_PATHS = frozenset({"/", "/api/health"})


@app.before_request
def _require_api_key():
    """Fail-closed API-key gate applied to every request (Option C).

    Deny-by-default: when ``FLIGHTRISK_API_KEY`` is configured, EVERY request must
    carry a matching ``Authorization: Bearer <key>`` header, except the small
    set of public paths (the root HTML page ``/``, the ``/api/health`` ping, and
    static assets) that must stay reachable so the dashboard loads in a browser.
    Because gating happens here rather than via a per-route allow-list, any route
    added in the future is protected automatically -- there is nothing to keep in
    sync.

    No-op passthrough when ``FLIGHTRISK_API_KEY`` is unset/empty (local dev keeps
    working with no key). The SocketIO control channel is authenticated
    separately at connect time (see ``on_connect``); individual SocketIO events
    are not covered by this check.
    """
    if not _FLIGHTRISK_API_KEY:
        return None
    # Static assets: match both the Flask endpoint and the URL prefix so a
    # custom static route or direct /static/... path both stay open.
    if request.endpoint == "static" or request.path.startswith("/static/"):
        return None
    if request.path in _PUBLIC_PATHS:
        return None
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth.encode(), f"Bearer {_FLIGHTRISK_API_KEY}".encode()):
        return jsonify({"error": "unauthorized"}), 401
    return None

# --- Bind address ---
#: Environment variable naming the dashboard's bind address (``--host``).
DASHBOARD_HOST_ENV = "FLIGHTRISK_DASHBOARD_HOST"

#: The dashboard serves this machine only unless another host is named.
DEFAULT_DASHBOARD_HOST = "127.0.0.1"

#: Hosts that keep the dashboard on this machine: the same set the edge
#: transport treats as loopback before it requires a token
#: (``flightrisk/transport.py``).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def resolve_dashboard_host(host: str | None = None) -> str:
    """The dashboard bind address: ``host``, else ``FLIGHTRISK_DASHBOARD_HOST``, else 127.0.0.1."""
    for candidate in (host, os.environ.get(DASHBOARD_HOST_ENV)):
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return DEFAULT_DASHBOARD_HOST


def check_dashboard_bind(host: str) -> None:
    """Refuse a non-loopback bind unless the dashboard API key is configured.

    This mirrors the edge transport, which refuses a non-loopback bind
    without ``FLIGHTRISK_EDGE_TOKEN``. Anyone who can reach the dashboard
    can press Start, which makes real drones take off, so exposing it to a
    venue or hotspot network requires ``FLIGHTRISK_API_KEY``. With the key
    set, every REST request (``_require_api_key``) and every Socket.IO
    connection (``on_connect``) must present it. Raises ``ValueError`` with
    an operator-facing message.
    """
    if host in _LOOPBACK_HOSTS or _FLIGHTRISK_API_KEY:
        return
    raise ValueError(
        f"Refusing to bind the dashboard to non-loopback host {host!r} without "
        f"authentication: anyone on that network could start or land the drones. "
        f"Set FLIGHTRISK_API_KEY so every client must present the key, or bind a "
        f"loopback host (the default, {DEFAULT_DASHBOARD_HOST})."
    )


# Flask auto-instrumentation (optional)
try:
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    FlaskInstrumentor().instrument_app(app)
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Edge (ground-station) detection consumer
# ---------------------------------------------------------------------------

def _sr_get(score_result, key, default=None):
    """Read ``key`` from a MatchScorer score result.

    ``MatchScorer.score()`` returns a plain ``dict`` (keys ``combined_score``,
    ``is_match``, ...), but this stays tolerant of an attribute-style object
    too, so a future scorer returning a dataclass/namedtuple keeps working.
    """
    if score_result is None:
        return default
    if isinstance(score_result, dict):
        return score_result.get(key, default)
    return getattr(score_result, key, default)


def _maybe_emit_edge_match(socketio, det, result, score_result, now):
    """Emit a ``match_alert`` for one matched edge detection via the alert latch.

    Uses the same single-alert latch as the local frame-loop match path, so
    only one alert is outstanding at a time and nothing new is emitted (or
    written to the DB) until the operator confirms or dismisses it.
    """
    # ``now`` is kept for call-site compatibility; the latch keeps its own clock.
    bbox = result["bbox"]
    track_key = _compute_track_key(bbox)
    alert_id = alert_latch.try_open([f"grid:{track_key}"])
    if alert_id is None:
        return
    try:
        _emit_edge_alert(socketio, det, result, score_result, track_key, alert_id)
    except Exception:
        alert_latch.abort(alert_id)
        raise


def _emit_edge_alert(socketio, det, result, score_result, track_key, alert_id):
    """Build, persist and emit the single edge match alert claimed by ``alert_id``."""
    snapshot_b64 = None
    crop_jpeg = getattr(det, "crop_jpeg", None)
    if crop_jpeg:
        snapshot_b64 = base64.b64encode(crop_jpeg).decode("utf-8")

    reid_score = float(result.get("reid_score", 0.0) or 0.0)
    face_score = float(result.get("face_score", 0.0) or 0.0)
    if score_result is not None:
        combined = float(_sr_get(score_result, "combined_score", 0.0) or 0.0)
    else:
        combined = max(reid_score, face_score)

    alert_level = "possible_match"
    scorer = app_state.scorer
    if scorer is not None and isinstance(score_result, dict):
        try:
            alert_level = scorer.alert_level(score_result)
        except Exception:
            alert_level = "possible_match"

    match_entry = {
        "time": time.strftime("%H:%M:%S"),
        "score": round(combined, 3),
        "gemma_match": None,
        "gemma_confidence": None,
        "reasoning": None,
        "snapshot": snapshot_b64,
        "type": "edge",
        "face_score": round(face_score, 3),
        "reid_score": round(reid_score, 3),
        "alert_level": alert_level,
        "track_id": track_key,
        "alert_id": alert_id,
    }

    if app_state.db and app_state.session_id:
        try:
            match_id = app_state.db.add_match(
                session_id=app_state.session_id,
                match_type="edge",
                reid_score=reid_score,
                face_score=face_score,
                combined_score=combined,
                gemma_match=False,
                gemma_confidence=None,
                reasoning=None,
            )
            match_entry["match_id"] = match_id
        except Exception:
            if app_state.logger:
                app_state.logger.warning("edge_match_db_write_failed", exc_info=True)

    with _match_history_lock:
        app_state.match_history.append(match_entry)
        app_state.match_history = app_state.match_history[-50:]

    alert_latch.attach(alert_id, match_entry)
    socketio.emit("match_alert", match_entry)


def _edge_process_and_emit(socketio, msg, fps_state=None):
    """Score one edge ``DetectionMessage`` and emit ``frame`` (+ ``match_alert``).

    Runs off the asyncio loop in a ``GroundTransport`` executor thread, so it
    uses plain (thread-safe) ``socketio.emit(...)`` rather than the
    request-context ``emit()``. See ``flightrisk/transport.py`` for the dispatch.
    """
    station = app_state.ground_station
    if station is None:
        return
    results = station.process_message(msg)

    w = msg.frame_width or 0
    h = msg.frame_height or 0
    now = time.time()

    boxes = []
    any_matched = False
    max_score = 0.0

    for det, result in zip(msg.detections, results):
        score_result = result.get("score_result")
        if score_result is not None:
            score = float(_sr_get(score_result, "combined_score", 0.0) or 0.0)
            matched = bool(_sr_get(score_result, "is_match", False))
        else:
            score = max(
                float(result.get("reid_score", 0.0) or 0.0),
                float(result.get("face_score", 0.0) or 0.0),
            )
            matched = False

        x1, y1, x2, y2 = result["bbox"]
        if w > 0 and h > 0:
            bbox_norm = [x1 / w, y1 / h, x2 / w, y2 / h]
        else:
            # Guard divide-by-zero when the edge device didn't report frame
            # dimensions -- emit zeros rather than crash the callback.
            bbox_norm = [0.0, 0.0, 0.0, 0.0]

        boxes.append({
            "bbox_norm": bbox_norm,
            "score": round(score, 3),
            "matched": matched,
            "track_id": None,
        })

        if score > max_score:
            max_score = score
        if matched:
            any_matched = True
            _maybe_emit_edge_match(socketio, det, result, score_result, now)

    # Best-effort FPS from consecutive message timestamps.
    fps = 0.0
    if fps_state is not None:
        last = fps_state.get("last")
        if last is not None:
            dt = msg.timestamp - last
            if dt > 0:
                fps = round(1.0 / dt, 1)
        fps_state["last"] = msg.timestamp

    image_b64 = (
        base64.b64encode(msg.thumbnail_jpeg).decode("utf-8")
        if msg.thumbnail_jpeg
        else None
    )

    socketio.emit("frame", {
        "image": image_b64,
        "boxes": boxes,
        "fps": fps,
        "persons": len(results),
        "match": any_matched,
        "match_score": round(max_score, 3),
        "stream_video": app_state.stream_video,
        "telemetry": {},
        "recording": False,
    })


# ---------------------------------------------------------------------------
# Multi-drone fleet (edge source with --fleet)
# ---------------------------------------------------------------------------

#: Seconds one fleet control send may wait on the ground transport.
FLEET_SEND_TIMEOUT_S = 2.0

#: Seconds one clock-sync round may wait for an edge's pong.
FLEET_PING_TIMEOUT_S = 1.0

#: Seconds the LAND ALL broadcast may wait on the ground transport.
LAND_ALL_TIMEOUT_S = 2.0

#: Seconds ``_land_all`` waits for all its paths together. Longer than the
#: local fleet's own wait for its drones (``_CALL_DEADLINE_S`` in
#: flightrisk/drone/fleet.py, 30 s), so the local path reports each drone that
#: did not land itself. A path still running then is reported as not having
#: reached any drone, so LAND ALL always answers.
LAND_ALL_PATHS_DEADLINE_S = 35.0

#: Interval of the fleet monitor, which refreshes ``fleet_status`` and
#: checks for lost drones.
FLEET_MONITOR_INTERVAL_S = 0.5

#: A drone in a started mission that has sent nothing for this long is
#: declared lost. It is longer than the edge's own link-loss watchdog
#: (``FLIGHTRISK_LINK_LOSS_LAND_S``, 3 s), so a silent edge has already
#: landed its drone by the time its lanes are handed to another drone.
FLEET_LOSS_TIMEOUT_S = 5.0

#: A drone in a started mission that has not reported flying this long after
#: the scheduled start is declared lost (for example when its battery gate
#: refused the takeoff).
FLEET_TAKEOFF_GRACE_S = 15.0

#: Seconds a re-task waits for the receiving drones to accept or refuse
#: their new lanes before the ``retask`` event is sent. Later answers are
#: reported in further ``retask`` or ``retask_failed`` events.
FLEET_RETASK_WAIT_S = 2.0


def fleet_geofence_cm() -> float:
    """The edges' geofence half-width in cm, read like ``SafetyEnvelope.from_env``.

    The ground station plans re-tasked lanes against the same
    ``FLIGHTRISK_GEOFENCE_CM`` the edges enforce, so it only offers lanes a
    drone can accept. Raises ``ValueError`` for a value that is not a
    positive number.
    """
    raw = os.environ.get(GEOFENCE_CM_ENV, "").strip()
    if not raw:
        return float(DEFAULT_GEOFENCE_CM)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{GEOFENCE_CM_ENV} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{GEOFENCE_CM_ENV} must be a positive number, got {raw!r}")
    return value


class TransportEdgeLink:
    """``EdgeLink`` for one drone over the dashboard's ``GroundTransportSync``.

    ``FleetCoordinator`` reaches each drone through ``send(dict)`` and
    receives that drone's messages through ``on_message``. Control messages
    go out with ``GroundTransportSync.send_to_drone``. A send that reaches no
    connection raises ``ConnectionError``, so the coordinator records the
    failure instead of assuming the drone got the message.

    ``ping`` is handled differently. The edge transport answers pings itself
    and the ground transport consumes the pongs, so a ping forwarded as-is
    would never come back to the coordinator. Each coordinator ping is
    therefore run as one real ping/pong round with
    ``GroundTransportSync.sync_clock(drone_id, rounds=1)``, and the edge's
    receive and send times from that round are returned to the coordinator
    as a ``pong`` for the coordinator's own ``t1``. The coordinator stamps
    ``t4`` itself when that pong arrives, so its offset and round-trip
    estimates also include this adapter's short local hop in each direction.
    ``sync_clock`` also sends the edge a provisional ``clock`` message after
    each round; the coordinator sends the median offset last, and that is
    the offset the edge keeps.
    """

    def __init__(
        self,
        transport,
        drone_id: str,
        send_timeout_s: float = FLEET_SEND_TIMEOUT_S,
        ping_timeout_s: float = FLEET_PING_TIMEOUT_S,
    ):
        self.on_message = None
        self.drone_id = drone_id
        self._transport = transport
        self._send_timeout_s = send_timeout_s
        self._ping_timeout_s = ping_timeout_s

    def send(self, msg: dict) -> None:
        if msg.get("type") == fp.PING:
            self._ping(msg)
            return
        if not self._transport.send_to_drone(self.drone_id, msg, timeout=self._send_timeout_s):
            raise ConnectionError(
                f"{msg.get('type')!r} was not delivered: drone {self.drone_id!r} "
                f"is not connected to the ground transport"
            )

    def _ping(self, msg: dict) -> None:
        estimate = self._transport.sync_clock(
            self.drone_id,
            rounds=1,
            round_timeout_s=self._ping_timeout_s,
            timeout=self._ping_timeout_s + self._send_timeout_s,
        )
        if not estimate.samples:
            raise RuntimeError(f"clock sync with drone {self.drone_id!r} returned no sample")
        sample = estimate.samples[-1]
        self.deliver({"type": fp.PONG, "t1": msg["t1"], "t2": sample.t2, "t3": sample.t3})

    def deliver(self, msg: dict) -> None:
        """Hand one message from this drone's edge to the coordinator."""
        callback = self.on_message
        if callback is not None:
            callback(msg)


def _best_detection_thumbnail(msg) -> str | None:
    """Base64 JPEG for a fleet map pin: the most confident crop, else the frame thumbnail."""
    crops = [d for d in msg.detections if getattr(d, "crop_jpeg", None)]
    if crops:
        best = max(crops, key=lambda d: d.confidence or 0.0)
        return base64.b64encode(best.crop_jpeg).decode("utf-8")
    if msg.thumbnail_jpeg:
        return base64.b64encode(msg.thumbnail_jpeg).decode("utf-8")
    return None


class FleetSupervisor:
    """Runs one ``FleetCoordinator`` over the dashboard's ground transport.

    The supervisor builds one ``TransportEdgeLink`` per configured drone id
    and a coordinator over those links. It routes each edge's ``ack``,
    ``nack`` and ``telemetry`` messages (the transport's ``on_control``) and its
    detections (the transport's ``on_message``) to the coordinator, forwards
    the coordinator's ``fleet_detection`` and ``fleet_event`` events to the
    browser, emits ``fleet_status`` and ``fleet_plan``, declares a drone lost
    when it drops out of a started mission so its lanes are re-tasked, and
    carries the coordinator half of LAND ALL.

    ``plan()`` and ``start()`` block on clock sync, and ``check_losses()``
    blocks while a re-task waits for answers, so callers run them on a
    worker thread, never on a transport delivery thread. Re-tasked lanes are
    planned against ``geofence_cm`` (by default ``FLIGHTRISK_GEOFENCE_CM``,
    the value the edges enforce).
    """

    def __init__(
        self,
        transport,
        drone_ids,
        socketio,
        *,
        area_cm=(400, 400),
        strip_cm: int = 75,
        net: str = "station",
        alt_slots_cm: tuple[int, ...] = (100, 160),
        clock=None,
        loss_timeout_s: float = FLEET_LOSS_TIMEOUT_S,
        takeoff_grace_s: float = FLEET_TAKEOFF_GRACE_S,
        send_timeout_s: float = FLEET_SEND_TIMEOUT_S,
        ping_timeout_s: float = FLEET_PING_TIMEOUT_S,
        geofence_cm: float | None = None,
        retask_wait_s: float = FLEET_RETASK_WAIT_S,
    ):
        self.net = net
        self._transport = transport
        self._socketio = socketio
        self._clock = clock if clock is not None else transport.now
        self._loss_timeout_s = loss_timeout_s
        self._takeoff_grace_s = takeoff_grace_s
        self.links = {
            drone_id: TransportEdgeLink(transport, drone_id, send_timeout_s, ping_timeout_s)
            for drone_id in drone_ids
        }
        self.coordinator = FleetCoordinator(
            self.links,
            area_cm=area_cm,
            strip_cm=strip_cm,
            alt_slots_cm=alt_slots_cm,
            clock=self._clock,
            on_event=self._on_coordinator_event,
            geofence_cm=geofence_cm if geofence_cm is not None else fleet_geofence_cm(),
            retask_wait_s=retask_wait_s,
        )
        self._lock = threading.Lock()
        self._busy = threading.Lock()  # one plan() or start() at a time
        self._flown: set[str] = set()  # reported flying since the start
        self._flying: set[str] = set()  # flying in their latest telemetry
        self._landed_early: set[str] = set()  # landed with lanes still to fly
        self._start_t_ground_s: float | None = None
        self._landed_all = False
        self._stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    # -- Browser events -----------------------------------------------------

    def _emit(self, name: str, payload: dict) -> None:
        try:
            self._socketio.emit(name, payload)
        except Exception:
            log.warning("fleet emit %s failed", name, exc_info=True)

    def _on_coordinator_event(self, name: str, payload: dict) -> None:
        if name == "fleet_event" and payload.get("event") == "land_all":
            with self._lock:
                self._landed_all = True
        self._emit(name, payload)

    def status_payload(self) -> dict:
        """The ``fleet_status`` event: per-drone telemetry, clock offset, RTT and skew."""
        return {"drones": self.coordinator.status(), "net": self.net, "synced": self.synced}

    def emit_status(self) -> None:
        self._emit("fleet_status", self.status_payload())

    def emit_plan(self) -> None:
        plan = self.coordinator.current_plan()
        if plan is not None:
            self._emit("fleet_plan", plan)

    @property
    def synced(self) -> bool:
        ids = self.coordinator.drone_ids
        return bool(ids) and all(self.coordinator.clock_sync(d).synced for d in ids)

    def summary(self) -> dict:
        """The ``fleet`` field of the dashboard status."""
        return {"drones": len(self.coordinator.drone_ids), "net": self.net, "synced": self.synced}

    # -- Messages from the edges --------------------------------------------

    def handle_control(self, msg: dict) -> None:
        """Route one ``ack``, ``nack`` or ``telemetry`` message to its drone's link."""
        drone_id = msg.get("drone_id")
        link = self.links.get(drone_id)
        if link is None:
            log.warning("Ignoring %s from drone %r, which is not in the fleet", msg.get("type"), drone_id)
            return
        if msg.get("type") == fp.TELEMETRY and self.coordinator.started:
            self._note_flight(drone_id, msg)
        link.deliver(msg)

    def _note_flight(self, drone_id: str, telemetry: dict) -> None:
        """Track takeoffs and landings during a started mission.

        Only a landing reported while lanes remain (``lane_index`` below
        ``lanes_total`` in that telemetry) counts as landing early. A drone
        that finished its lanes and landed, then receives more lanes, is not
        treated as lost.
        """
        lane_index = telemetry.get("lane_index")
        lanes_total = telemetry.get("lanes_total")
        unfinished = (
            isinstance(lane_index, int) and isinstance(lanes_total, int) and lane_index < lanes_total
        )
        with self._lock:
            if telemetry.get("is_flying") is True:
                self._flown.add(drone_id)
                self._flying.add(drone_id)
                self._landed_early.discard(drone_id)
            elif drone_id in self._flying:
                self._flying.discard(drone_id)
                if unfinished:
                    self._landed_early.add(drone_id)

    def handle_detections(self, msg) -> None:
        """Place one edge ``DetectionMessage`` on the shared map.

        A message with at least one person and the drone's relative position
        becomes one detection at that position; the coordinator merges it
        with any detection within its dedup radius and emits
        ``fleet_detection`` only for a new one.
        """
        drone_id = getattr(msg, "drone_id", None)
        if drone_id not in self.links or not msg.detections:
            return
        payload = {
            "type": "detections",
            "drone_id": drone_id,
            "rel_x_cm": msg.rel_x_cm,
            "rel_y_cm": msg.rel_y_cm,
        }
        if msg.lat is not None and msg.lon is not None:
            payload["lat"] = msg.lat
            payload["lon"] = msg.lon
        thumbnail = _best_detection_thumbnail(msg)
        if thumbnail is not None:
            payload["thumbnail"] = thumbnail
        self.coordinator.on_detection(drone_id, payload)

    # -- Operator actions ---------------------------------------------------

    def plan(self):
        """Sync every clock, split the area and send ``assign_lanes``.

        Raises ``RuntimeError`` while a drone reports flying.
        """
        with self._busy:
            self.coordinator.sync()
            plan = self.coordinator.plan()
            with self._lock:
                self._flown.clear()
                self._flying.clear()
                self._landed_early.clear()
                self._start_t_ground_s = None
                self._landed_all = False
        self.emit_plan()
        self.emit_status()
        return plan

    def start(self, lead_s: float = 2.0):
        """Re-sync every clock, then schedule one common start.

        The coordinator refuses the start unless every drone is synced, has
        acked its lanes and reports enough battery. The outcome is sent to
        the browser as a ``fleet_event``.
        """
        with self._busy:
            if not self.coordinator.started:
                self.coordinator.sync()
            result = self.coordinator.start(lead_s=lead_s)
        if result.ok:
            with self._lock:
                self._start_t_ground_s = result.t_ground_s
            self._emit("fleet_event", {"event": "start", "detail": f"start_at in {lead_s:g} s"})
        else:
            detail = "; ".join(f"{who}: {why}" for who, why in sorted(result.refusals.items()))
            log.warning("Fleet start refused: %s", detail)
            self._emit("fleet_event", {"event": "start_refused", "detail": detail})
        self.emit_status()
        return result

    def land_all(self) -> dict:
        """Send ``emergency_land_all`` to every drone through the coordinator.

        Returns the coordinator's per-drone results (``None`` means sent).
        """
        with self._lock:
            self._landed_all = True
        return self.coordinator.land_all()

    # -- Loss detection -----------------------------------------------------

    def check_losses(self) -> list[str]:
        """Declare lost every drone that dropped out of the started mission.

        A drone is lost when its connection is gone, when it has sent nothing
        for ``loss_timeout_s``, when it reports landed before finishing its
        lanes (for example after its local land key), or when it has not
        taken off ``takeoff_grace_s`` after the scheduled start. Its unflown
        lanes are offered to the remaining drones by
        ``FleetCoordinator.on_drone_lost``, which also emits the
        ``drone_lost``, ``retask`` and ``retask_failed`` events and waits up
        to ``retask_wait_s`` for the drones' answers. Returns the drones declared
        lost by this call. The check is skipped while ``plan()`` or
        ``start()`` runs, so it never marks a drone of a new plan lost.
        """
        if not self._busy.acquire(blocking=False):
            return []
        try:
            return self._check_losses()
        finally:
            self._busy.release()

    def _check_losses(self) -> list[str]:
        with self._lock:
            if self._landed_all or self._start_t_ground_s is None:
                return []
            start_t = self._start_t_ground_s
            flown = set(self._flown)
            landed_early = set(self._landed_early)
        if not self.coordinator.started:
            return []
        links = self._transport.fleet_status(timeout=FLEET_SEND_TIMEOUT_S)
        status = self.coordinator.status()
        now = self._clock()
        lost = []
        for drone_id in self.links:
            entry = status.get(drone_id) or {}
            if entry.get("lost"):
                continue
            reason = self._loss_reason(
                links.get(drone_id), drone_id in flown, drone_id in landed_early, now - start_t,
            )
            if reason is None:
                continue
            with self._lock:
                if self._landed_all:
                    break
            log.warning("Fleet drone %s lost (%s); re-tasking its lanes", drone_id, reason)
            self.coordinator.on_drone_lost(drone_id)
            lost.append(drone_id)
        return lost

    def _loss_reason(
        self, link: dict | None, flown: bool, landed_early: bool, since_start_s: float,
    ) -> str | None:
        if link is None or not link.get("connected"):
            return "disconnected"
        age = link.get("link_age_s")
        if age is not None and age > self._loss_timeout_s:
            return f"silent for {age:.1f} s"
        if landed_early:
            return "landed before finishing its lanes"
        if not flown and since_start_s > self._takeoff_grace_s:
            return f"not flying {since_start_s:.0f} s after the scheduled start"
        return None

    # -- Monitor thread -----------------------------------------------------

    def start_monitor(self, interval_s: float = FLEET_MONITOR_INTERVAL_S) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor, args=(interval_s,), daemon=True, name="fleet-monitor",
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _monitor(self, interval_s: float) -> None:
        while not self._stop.wait(interval_s):
            try:
                self.check_losses()
            except Exception:
                log.error("Fleet loss check failed", exc_info=True)
            self.emit_status()


def _run_land_all_path(report: dict, interrupts: list, name: str, what: str, send) -> None:
    """Run one LAND ALL path and store how many drones it reached in ``report[name]``.

    A failure is logged and leaves ``report[name]`` as ``None``. A
    ``KeyboardInterrupt`` or ``SystemExit`` is logged and kept in
    ``interrupts``, for ``_land_all`` to re-raise once every path has run.
    """
    try:
        report[name] = send()
    except Exception:
        log.error("LAND ALL: %s failed", what, exc_info=True)
    except BaseException as exc:
        log.error("LAND ALL: %s was interrupted: %r", what, exc)
        interrupts.append(exc)


def _land_all() -> dict:
    """Order every drone to land by every path available, whatever fails.

    The paths are the ground transport's ``emergency_land_all`` broadcast,
    written to every connected edge at once; the fleet coordinator, which
    sends ``emergency_land_all`` to each fleet link in turn as belt and
    braces; and ``land`` on every drone of a local fleet (Tello or MAVLink
    source), all drones at once, so a drone whose land is never confirmed
    does not delay the land to the others (``DroneFleet.broadcast_command``
    still reports each drone's failure). The paths run at the same time, each on its own thread, and the
    broadcast is started first. A coordinator send that waits out its
    timeout on one stalled link therefore never delays the land order to any
    other drone, and a failure on one path never stops another. This waits
    for the paths at most ``LAND_ALL_PATHS_DEADLINE_S``; a path still
    running then is logged and reported as ``None``, and its thread keeps
    running in the background. A ``KeyboardInterrupt`` or ``SystemExit`` is
    re-raised only after every path has finished or that wait has ended.
    When no path reaches any drone, the failure is logged at CRITICAL and
    sent to every browser.

    Returns how many drones each path reached (``None`` for a path that is
    not configured or that failed) and whether any path reached a drone.
    """
    supervisor = app_state.fleet_supervisor
    transport = app_state.ground_transport
    fleet = app_state.fleet
    # Each path's thread stores its count here; the report returned is a
    # copy taken when the wait ends, so a path that finishes later changes
    # nothing the caller holds.
    reached: dict = {"coordinator": None, "transport": None, "local": None}
    interrupts: list[BaseException] = []

    paths = []
    if transport is not None:
        paths.append((
            "transport", "the ground transport emergency_land_all broadcast",
            lambda: int(transport.emergency_land_all(timeout=LAND_ALL_TIMEOUT_S)),
        ))
    if supervisor is not None:
        paths.append((
            "coordinator", "the fleet coordinator path",
            lambda: sum(1 for exc in supervisor.land_all().values() if exc is None),
        ))
    if fleet is not None:
        paths.append((
            "local", "landing the local drones",
            lambda: sum(1 for exc in fleet.broadcast_command("land").values() if exc is None),
        ))

    deadline = time.monotonic() + LAND_ALL_PATHS_DEADLINE_S
    threads = []
    for name, what, send in paths:
        thread = threading.Thread(
            target=_run_land_all_path,
            args=(reached, interrupts, name, what, send),
            name=f"land-all-{name}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            # No thread could be started: run this path here rather than skip it.
            log.error("LAND ALL: could not start a thread for %s; running it inline", what, exc_info=True)
            _run_land_all_path(reached, interrupts, name, what, send)
            continue
        threads.append((name, what, thread))
    for _, _, thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    # Which paths are still running is read before the copy, so a path that
    # finished is always counted.
    unfinished = [(name, what) for name, what, thread in threads if thread.is_alive()]
    report: dict = dict(reached)
    for name, what in unfinished:
        report[name] = None
        log.error("LAND ALL: %s did not finish within %g s", what, LAND_ALL_PATHS_DEADLINE_S)

    report["reached_any"] = any(n for n in (report["coordinator"], report["transport"], report["local"]))
    if report["reached_any"]:
        log.warning("LAND ALL sent: %s", report)
        if app_state.logger:
            app_state.logger.warning("land_all_sent", **report)
    else:
        log.critical(
            "LAND ALL REACHED NO DRONE (%s). Land every drone with its local control now.", report,
        )
        if app_state.logger:
            app_state.logger.error("land_all_reached_no_drone", **report)
        socketio.emit("error", {
            "message": "LAND ALL reached no drone. Land every drone with its local control now.",
        })

    if interrupts:
        raise interrupts[0]
    return report


def _detector_tiled() -> bool:
    tiled = getattr(app_state.detector, "tiled", None)
    if isinstance(tiled, bool):
        return tiled
    return bool(get_config().vision.detector_tiled)


def _privacy_and_fleet_status() -> dict:
    """Status fields shared by ``/api/status`` and the socket ``status`` event."""
    supervisor = app_state.fleet_supervisor
    if supervisor is not None:
        fleet = supervisor.summary()
    else:
        net = getattr(app_state.source_config, "fleet_net", None)
        fleet = {"drones": 0, "net": net if isinstance(net, str) else "station", "synced": False}
    return {
        "appearance_only": bool(app_state.appearance_only),
        "tiled": _detector_tiled(),
        "fleet": fleet,
    }


# ---------------------------------------------------------------------------
# Pipeline initialisation
# ---------------------------------------------------------------------------

def _reasoning_state() -> dict:
    """Current reasoning backend, as shown in the dashboard.

    ``unavailable`` maps each provider that can't be selected right now (for
    example Claude without the cloud opt-in) to the reason, so the selector
    can show it greyed out with an explanation.
    """
    from flightrisk.reasoning import (
        PROVIDERS,
        cloud_llm_allowed,
        provider_unavailable_reason,
    )

    agent = app_state.reasoning
    unavailable = {}
    for name in PROVIDERS:
        reason = provider_unavailable_reason(name)
        if reason:
            unavailable[name] = reason
    return {
        "provider": agent.provider if agent else "off",
        "label": agent.label if agent else None,
        "model": agent.model if agent else None,
        "available": [name for name in PROVIDERS if name not in unavailable],
        "unavailable": unavailable,
        "cloud_allowed": cloud_llm_allowed(),
    }


def _ensure_reasoning_worker():
    """Start the async reasoning worker if reasoning is on and it isn't running.

    Safe to call repeatedly (restart_dashboard, provider switches): the worker
    reads ``app_state.reasoning`` per item, so swapping backends needs no restart.
    """
    if app_state.reasoning is None:
        return
    with _gemma_thread_lock:
        existing = app_state.gemma_thread
        if existing is None or not existing.is_alive():
            t = threading.Thread(target=_gemma_worker, args=(socketio,), daemon=True)
            t.start()
            app_state.gemma_thread = t


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

    # Appearance-only mode: faces are not resolvable at search altitude, so
    # face recognition is never constructed and the face signal is disabled
    # before the signal registry can load its model.
    appearance_only = source_config.appearance_only
    if appearance_only is None:
        appearance_only = get_config().vision.appearance_only
    appearance_only = bool(appearance_only)
    app_state.appearance_only = appearance_only
    if appearance_only:
        app_state.face = None
        get_config().vision.signal_insightface_face.enabled = False
        log.info("appearance_only_mode", face_recognition=False)
    elif app_state.face is None:
        try:
            from flightrisk.vision.face import FaceRecognizer
            app_state.face = FaceRecognizer(match_threshold=0.35)
        except Exception as e:
            log.warning("insightface_unavailable", error=str(e))

    if app_state.scorer is None:
        app_state.scorer = MatchScorer(match_threshold=0.45)

    if app_state.signal_registry is None:
        try:
            app_state.signal_registry = SignalRegistry()
            app_state.signal_registry.register_all(app_state.scorer)
            log.info(
                "signal_registry_initialized",
                active_signals=app_state.signal_registry.active_signals,
            )
        except Exception as e:
            log.warning("signal_registry_init_failed", error=str(e))
    if appearance_only and app_state.signal_registry is not None:
        app_state.signal_registry.disable_signal("insightface_face")

    if app_state.tracker is None:
        app_state.tracker = DetectionTracker(max_age=30, iou_threshold=0.3)

    if not source_config.enable_llm:
        # --no-llm: no reasoning agent and no Gemma worker thread.
        app_state.reasoning = None
        log.info("reasoning_disabled", reason="no_llm")
    elif app_state.reasoning is None:
        try:
            from flightrisk.reasoning import create_reasoning_agent
            app_state.reasoning = create_reasoning_agent(source_config.reasoning_provider)
            if app_state.reasoning is not None:
                log.info(
                    "reasoning_enabled",
                    provider=app_state.reasoning.provider,
                    model=app_state.reasoning.model,
                )
        except Exception as e:
            log.warning("reasoning_unavailable", error=str(e))

    # Start the async reasoning worker thread (only if reasoning is available
    # and not already running from a previous init call, e.g. restart_dashboard).
    _ensure_reasoning_worker()

    if target_path and os.path.exists(target_path):
        app_state.reid.set_target_from_file(target_path)
        app_state.target_photo_path = target_path
        img = cv2.imread(target_path)
        _, buf = cv2.imencode(".jpg", img)
        app_state.target_photo = base64.b64encode(buf).decode("utf-8")
        # Also set face recognition target
        if app_state.face:
            app_state.face.set_target_from_file(target_path)
        # Set configurable signal targets
        if app_state.signal_registry:
            app_state.signal_registry.set_target(img)

    # Initialize session persistence
    if app_state.db is None:
        app_state.db = SessionDB()
        log.info("session_db_initialized")

    # Initialize obstacle guard
    if app_state.obstacle_guard is None:
        try:
            from flightrisk.drone.obstacle import ObstacleGuard
            app_state.obstacle_guard = ObstacleGuard()
            log.info("obstacle_guard_initialized")
        except Exception as e:
            log.warning("obstacle_guard_unavailable", error=str(e))

    if app_state.canon is None:
        app_state.canon = TargetCanon(session_db=app_state.db)
        log.info("target_canon_initialized")

    # OpenTelemetry
    if _HAS_TELEMETRY:
        otel_enabled = init_telemetry()
        if otel_enabled:
            app_state.tracer = get_tracer()
            app_state.otel_metrics = FlightRiskMetrics(get_meter())
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

    # Likewise stop the fleet monitor of a previous edge pipeline. Its
    # coordinator belongs to the transport that the edge branch below stops.
    old_supervisor = app_state.fleet_supervisor
    if old_supervisor is not None:
        old_supervisor.stop()
        app_state.fleet_supervisor = None

    if source == "tello":
        from flightrisk.drone.tello import TelloController
        fleet = DroneFleet(factory=lambda n, h: TelloController(n, h))
        app_state.fleet = fleet
        def _auto_connect_loop():
            retry_interval = get_config().drone.auto_connect_interval
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
                    log.info("tello_waiting", hint=f"retrying in {retry_interval:.0f}s")
                if stop_event.wait(retry_interval):
                    break
        threading.Thread(target=_auto_connect_loop, daemon=True).start()
    elif source == "mavlink":
        # Imported lazily so `mavsdk` is only required when actually used.
        from flightrisk.drone.mavlink import MavlinkController
        fleet = DroneFleet(factory=lambda n, h: MavlinkController(n, h, rtsp_url=rtsp_url))
        app_state.fleet = fleet
        def _auto_connect_loop():
            retry_interval = get_config().drone.auto_connect_interval
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
                    log.info("mavlink_waiting", hint=f"retrying in {retry_interval:.0f}s")
                if stop_event.wait(retry_interval):
                    break
        threading.Thread(target=_auto_connect_loop, daemon=True).start()
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
        # Ground-station mode: no local drone fleet or capture device. Edge
        # devices (flightrisk/edge.py) connect to the WebSocket *server* we start
        # here and stream DetectionMessages; GroundStation scores them and
        # the adapter below streams results to the browser. The normal
        # _frame_loop idles harmlessly (fleet/cap are None).
        from flightrisk.ground import GroundStation
        from flightrisk.transport import GroundTransportSync

        app_state.fleet = None
        app_state.cap = None
        app_state.stream_video = source_config.stream_video

        # Stop any ground transport left running from a previous
        # _init_pipeline() call (e.g. via restart_dashboard) before binding a
        # new server on the same port -- otherwise start() fails with an
        # address-already-in-use error.
        if source_config.fleet_net == "ap":
            # AP mode joins each laptop to a Tello's open access point, so no
            # wildcard or name may be bound, whoever built the config.
            from flightrisk.transport import check_literal_hosts, parse_bind_hosts

            check_literal_hosts(parse_bind_hosts(source_config.edge_host))

        old_transport = app_state.ground_transport
        if old_transport is not None:
            try:
                old_transport.stop()
            except Exception:
                log.warning("edge_transport_stop_failed")
            app_state.ground_transport = None

        station = GroundStation(scorer=app_state.scorer)
        app_state.ground_station = station

        _fps_state = {"last": None}
        # The fleet supervisor needs the transport and the transport's
        # callbacks need the supervisor, so the callbacks look it up here.
        _fleet: dict = {"supervisor": None}

        def _on_edge_message(msg):
            # Runs in a GroundTransport executor thread -- plain socketio.emit
            # is thread-safe (async_mode="threading").
            try:
                _edge_process_and_emit(socketio, msg, _fps_state)
            except Exception:
                if app_state.logger:
                    app_state.logger.error(
                        "edge_message_error", traceback=traceback.format_exc()
                    )
            supervisor = _fleet["supervisor"]
            if supervisor is not None:
                try:
                    supervisor.handle_detections(msg)
                except Exception:
                    if app_state.logger:
                        app_state.logger.error(
                            "fleet_detection_error", traceback=traceback.format_exc()
                        )

        def _on_edge_control(msg):
            # Edge ack, nack and telemetry messages, on a transport executor thread.
            supervisor = _fleet["supervisor"]
            if supervisor is not None:
                supervisor.handle_control(msg)

        # Heartbeats are on in edge mode: a fleet edge lands its drone when
        # ground messages stop for FLIGHTRISK_LINK_LOSS_LAND_S.
        transport = GroundTransportSync(
            host=source_config.edge_host,
            port=source_config.edge_port,
            on_message=_on_edge_message,
            token=os.environ.get("FLIGHTRISK_EDGE_TOKEN"),
            on_control=_on_edge_control,
            heartbeat_interval_s=fp.HEARTBEAT_INTERVAL_S,
        )
        supervisor = None
        if source_config.fleet_drones:
            supervisor = FleetSupervisor(
                transport,
                source_config.fleet_drones,
                socketio,
                area_cm=source_config.fleet_area_cm,
                strip_cm=source_config.fleet_strip_cm,
                net=source_config.fleet_net,
            )
            _fleet["supervisor"] = supervisor
        transport.start()
        app_state.ground_transport = transport
        if supervisor is not None:
            app_state.fleet_supervisor = supervisor
            supervisor.start_monitor()
        log.info(
            "edge_ground_station_started",
            host=source_config.edge_host,
            bound_hosts=list(transport.bound_hosts),
            unbound_hosts=list(transport.unbound_hosts),
            port=transport.port,
            stream_video=app_state.stream_video,
            fleet=list(source_config.fleet_drones),
            fleet_net=source_config.fleet_net,
        )
    else:
        # Backward-compat: treat any other source string as a video path
        # (e.g. direct callers of run_dashboard()/_init_pipeline() that
        # predate the --source enum, such as flightrisk/main.py --dashboard).
        app_state.fleet = None
        app_state.cap = cv2.VideoCapture(source)

    # A new session starts with a fresh alert latch (no pending or confirmed
    # match carried over from the previous session).
    reset_alert_flow()

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
    if app_state.signal_registry:
        app_state.signal_registry.set_target(img)
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
    if app_state.signal_registry:
        app_state.signal_registry.clear_targets()
    path = Path(__file__).parent.parent.parent / "target_reference.jpg"
    if path.exists():
        path.unlink()
    print("[clear-target] Target photo cleared")
    return jsonify({"success": True})


@app.route("/api/health")
def health():
    registry = app_state.signal_registry
    return jsonify({
        "status": "healthy",
        "version": "1.0.0",
        "components": {
            "detector": app_state.detector is not None,
            "reid": app_state.reid is not None,
            "face": app_state.face is not None,
            "reasoning": app_state.reasoning is not None,
            "db": app_state.db is not None,
            "signal_registry": registry is not None,
        },
        "reasoning_provider": _reasoning_state()["provider"],
        "active_signals": registry.active_signals if registry else [],
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
        "reasoning_provider": _reasoning_state()["provider"],
        "has_face": app_state.face is not None and app_state.face.has_target if app_state.face else False,
        "match_history": app_state.match_history[-10:],
        "telemetry": app_state.drone_telemetry,
        **_privacy_and_fleet_status(),
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


@app.route("/api/alerts/<alert_id>/respond", methods=["POST"])
def api_alert_respond(alert_id):
    """Operator Confirm/Dismiss for the outstanding match alert (REST path).

    Gated by the fail-closed ``before_request`` API-key check like every other
    non-public route. Body: ``{"action": "confirm" | "dismiss", "notes": str?}``.
    """
    data = request.get_json(silent=True) or {}
    body, status_code = handle_alert_response(
        alert_id, data.get("action"), socketio, notes=data.get("notes"),
    )
    return jsonify(body), status_code


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
    current = scorer.match_threshold if scorer else get_config().vision.scorer_match_threshold
    tuner = ThresholdTuner(db)
    return jsonify(tuner.analyze(current_threshold=current))


# --- WebSocket Events ---

@socketio.on("connect")
def on_connect(auth=None):
    """SocketIO auth is handled here, separate from the REST before_request gate."""
    if _FLIGHTRISK_API_KEY is not None:
        provided = (auth or {}).get("api_key") if isinstance(auth, dict) else None
        if provided is None or not hmac.compare_digest(provided.encode(), _FLIGHTRISK_API_KEY.encode()):
            return False  # reject connection
    emit("status", {"connected": True, "source": app_state.source, **_privacy_and_fleet_status()})
    if app_state.target_photo:
        emit("target_photo", {"image": app_state.target_photo})
    # A browser that (re)connects while an alert is pending gets it straight
    # away instead of waiting for the next re-notification.
    pending = alert_latch.pending_alert()
    if pending is not None:
        emit("match_alert_reminder", pending)


@socketio.on("match_alert_response")
def on_match_alert_response(data):
    """Operator Confirm/Dismiss for the outstanding match alert (SocketIO path).

    The SocketIO channel is authenticated at connect time (``on_connect``), so
    only an authenticated dashboard can reach this handler.
    """
    data = data if isinstance(data, dict) else {}
    body, _status = handle_alert_response(
        data.get("alert_id"), data.get("action"), socketio, notes=data.get("notes"),
    )
    emit("match_alert_response_ack", body)


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
        # Set configurable signal targets
        if app_state.signal_registry:
            app_state.signal_registry.set_target(img)
        # Edge mode: extract target embeddings from the reference photo and
        # push them down to the local GroundStation scorer + every connected
        # edge device. Mirrors how EdgeRunner extracts per-crop embeddings
        # (extract_embedding -> .tolist()); here the whole photo is the crop.
        if app_state.ground_transport is not None:
            reid_emb = None
            face_emb = None
            if app_state.reid is not None:
                _r = app_state.reid.extract_embedding(img)
                reid_emb = _r.tolist() if _r is not None else None
            if app_state.face is not None:
                _f = app_state.face.extract_embedding(img)
                face_emb = _f.tolist() if _f is not None else None
            if app_state.ground_station is not None:
                app_state.ground_station.set_target(reid_emb, face_emb)
            app_state.ground_transport.broadcast_target(reid_emb, face_emb)
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
    if app_state.signal_registry:
        app_state.signal_registry.set_target(img)
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


@socketio.on("get_reasoning_provider")
def on_get_reasoning_provider():
    emit("reasoning_provider", _reasoning_state())


@socketio.on("set_reasoning_provider")
def on_set_reasoning_provider(data):
    """Switch the reasoning backend at runtime (claude / gemma / off).

    Like every SocketIO handler, this is only reachable by a client that
    passed the ``on_connect`` API-key check. A cloud backend is refused
    unless cloud use was explicitly allowed at startup
    (``FLIGHTRISK_ALLOW_CLOUD_LLM=1``); the dashboard cannot opt itself in.
    """
    from flightrisk.reasoning import (
        cloud_block_reason,
        create_reasoning_agent,
        normalize_provider,
    )

    requested = data.get("provider") if isinstance(data, dict) else None
    try:
        if not requested:
            raise ValueError("provider is required")
        provider = normalize_provider(requested)
        blocked = cloud_block_reason(provider)
        if blocked:
            if app_state.logger:
                app_state.logger.warning("reasoning_provider_refused", provider=provider, reason="cloud_not_allowed")
            raise PermissionError(blocked)
        agent = create_reasoning_agent(provider)
    except Exception as e:
        # Keep the current backend and tell the operator why the switch failed.
        emit("reasoning_provider", {**_reasoning_state(), "error": str(e)})
        return

    app_state.reasoning = agent
    _ensure_reasoning_worker()
    if app_state.logger:
        app_state.logger.info("reasoning_provider_changed", provider=provider)
    socketio.emit("reasoning_provider", _reasoning_state())


@socketio.on("set_threshold")
def on_set_threshold(data):
    """Update the ReID match threshold."""
    threshold = data.get("threshold", get_config().vision.reid_threshold)
    threshold = max(0.1, min(0.99, float(threshold)))
    if app_state.reid:
        app_state.reid.match_threshold = threshold
    if app_state.scorer:
        app_state.scorer.match_threshold = threshold
    if app_state.face:
        app_state.face.match_threshold = threshold
    if app_state.logger:
        app_state.logger.info("threshold_updated", threshold=threshold)
    emit("threshold_updated", {"threshold": threshold})


@socketio.on("set_sensitivity_preset")
def on_set_sensitivity_preset(data):
    """Apply a named sensitivity preset that sets all thresholds at once."""
    PRESETS = {
        "more_alerts": {"reid": 0.40, "scorer": 0.35, "face": 0.30},
        "balanced": {"reid": 0.55, "scorer": 0.45, "face": 0.45},
        "fewer_alerts": {"reid": 0.70, "scorer": 0.60, "face": 0.55},
    }
    preset_name = data.get("preset", "balanced")
    preset = PRESETS.get(preset_name)
    if not preset:
        emit("error", {"message": f"Unknown preset: {preset_name}"})
        return
    if app_state.reid:
        app_state.reid.match_threshold = preset["reid"]
    if app_state.scorer:
        app_state.scorer.match_threshold = preset["scorer"]
    if app_state.face:
        app_state.face.match_threshold = preset["face"]
    if app_state.logger:
        app_state.logger.info("sensitivity_preset_applied", preset=preset_name, **preset)
    emit("threshold_updated", {"threshold": preset["scorer"], "preset": preset_name})


@socketio.on("get_signal_config")
def on_get_signal_config():
    """Return the current signal configuration."""
    cfg = get_config().vision
    signal_names = [
        "reid", "face", "reasoning", "clothing_color",
        "height_ratio", "osnet_reid", "insightface_face",
    ]
    signals = {}
    for name in signal_names:
        sig_cfg = getattr(cfg, f"signal_{name}")
        # Check actual runtime availability
        runtime_available = True
        if app_state.signal_registry:
            if name in ("clothing_color", "height_ratio", "osnet_reid", "insightface_face"):
                provider = app_state.signal_registry.get_provider(name)
                runtime_available = provider is not None
        signals[name] = {
            "enabled": sig_cfg.enabled,
            "weight": sig_cfg.weight,
            "available": runtime_available,
        }
    emit("signal_config", {"signals": signals})


#: Signals that match faces. Appearance-only mode refuses to enable them.
_FACE_SIGNALS = frozenset({"face", "insightface_face"})


@socketio.on("set_signal_config")
def on_set_signal_config(data):
    """Update signal enable/weight at runtime.

    Args:
        data: Dict with 'signal' (name), optional 'enabled' (bool),
              optional 'weight' (float).
    """
    name = data.get("signal")
    if not name:
        emit("error", {"message": "Missing 'signal' name"})
        return

    cfg = get_config().vision
    attr_name = f"signal_{name}"
    if not hasattr(cfg, attr_name):
        emit("error", {"message": f"Unknown signal: {name}"})
        return

    sig_cfg = getattr(cfg, attr_name)

    if app_state.appearance_only and name in _FACE_SIGNALS and data.get("enabled"):
        emit("error", {"message": f"Appearance-only mode: the {name!r} signal stays disabled"})
        return

    if "enabled" in data:
        sig_cfg.enabled = bool(data["enabled"])
    if "weight" in data:
        sig_cfg.weight = max(0.0, min(1.0, float(data["weight"])))

    # Update the scorer's registered weight (built-in or custom signal)
    if app_state.scorer:
        try:
            app_state.scorer.update_weight(name, sig_cfg.weight)
        except ValueError:
            pass  # signal not registered

    registry = app_state.signal_registry
    if registry:
        if sig_cfg.enabled:
            if registry.enable_signal(name):
                if app_state.scorer and name not in ("reid", "face", "reasoning"):
                    scorer_cfg = getattr(get_config().vision, f"signal_{name}", None)
                    if scorer_cfg:
                        try:
                            app_state.scorer.register_signal(name, weight=scorer_cfg.weight)
                        except ValueError:
                            pass
        else:
            registry.disable_signal(name)

    if app_state.logger:
        app_state.logger.info(
            "signal_config_updated",
            signal=name,
            enabled=sig_cfg.enabled,
            weight=sig_cfg.weight,
        )
    emit("signal_config_updated", {
        "signal": name,
        "enabled": sig_cfg.enabled,
        "weight": sig_cfg.weight,
    })


@socketio.on("set_stream_video")
def on_set_stream_video(data):
    """Toggle live full-frame video streaming from edge devices.

    Mirrors ``on_set_threshold``: update local state, push the toggle down to
    connected edge devices (edge mode only), then ack the new state.
    """
    enabled = bool((data or {}).get("enabled", False))
    app_state.stream_video = enabled
    if app_state.ground_transport is not None:
        try:
            app_state.ground_transport.broadcast_stream_video(enabled)
        except Exception:
            if app_state.logger:
                app_state.logger.warning("broadcast_stream_video_failed", exc_info=True)
    if app_state.logger:
        app_state.logger.info("stream_video_updated", enabled=enabled)
    emit("stream_video_state", {"enabled": app_state.stream_video})


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


@socketio.on("land_all")
def on_land_all(data=None):
    """LAND ALL (button ``id="land-all"`` and Shift+L): land every drone now.

    Runs ``_land_all()``, which tries the fleet coordinator, the ground
    transport broadcast and the local fleet independently, and acks with
    what each path reached.
    """
    report = _land_all()
    emit("land_all_ack", report)


def _run_fleet_action(action: str) -> None:
    """Run the fleet ``plan`` or ``start`` action on a worker thread.

    Both block on clock sync, which must never run on a transport delivery
    thread or hold up other socket events such as ``land_all``.
    """
    supervisor = app_state.fleet_supervisor
    if supervisor is None:
        emit("error", {"message": "No fleet is configured (start the dashboard with --source edge --fleet ...)"})
        return
    if alert_latch.phase is AlertPhase.RESOLVED:
        emit("error", {"message": "Match already confirmed; restart the dashboard to start a new search"})
        return

    def _work():
        try:
            getattr(supervisor, action)()
        except Exception as exc:
            log.error("Fleet %s failed", action, exc_info=True)
            socketio.emit("error", {"message": f"Fleet {action} failed: {exc}"})

    threading.Thread(target=_work, daemon=True, name=f"fleet-{action}").start()


@socketio.on("plan_fleet")
def on_plan_fleet(data=None):
    """Sync clocks, split the search area and send each drone its lanes."""
    _run_fleet_action("plan")


@socketio.on("start_fleet")
def on_start_fleet(data=None):
    """Re-sync clocks and schedule one common start for the fleet."""
    _run_fleet_action("start")


@socketio.on("start_search")
def on_start_search(data):
    """Start an autonomous search pattern."""
    fleet = app_state.fleet
    drone: DroneController | None = fleet.primary if fleet else None
    if not drone or not drone.state.is_flying:
        emit("error", {"message": "Drone must be flying to start search"})
        return
    if alert_latch.phase is AlertPhase.RESOLVED:
        emit("error", {"message": "Match already confirmed; restart the dashboard to start a new search"})
        return

    from flightrisk.drone.search import get_search_pattern, PatternType

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
            # Hold position between waypoints while a match alert awaits the
            # operator. Dismiss resumes the pattern; Confirm clears
            # search_active, which ends it.
            if alert_latch.phase is AlertPhase.AWAITING_RESPONSE:
                time.sleep(0.2)
                continue

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
    host = data.get("host", get_config().drone.tello_default_host)
    current_source = app_state.source
    with fleet_lock:
        fleet = app_state.fleet
        if not fleet:
            # Build a fleet whose backend matches the currently configured
            # source, instead of always defaulting to Tello -- a manual
            # registration on a mavlink deployment must build a
            # MavlinkController, not silently create a Tello-backed fleet.
            if current_source == "mavlink":
                from flightrisk.drone.mavlink import MavlinkController
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
            # non-drone fallback (file/edge/unset). Never stomp an
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
    supervisor = app_state.fleet_supervisor
    if supervisor is not None:
        emit("fleet_status", supervisor.status_payload())
        plan = supervisor.coordinator.current_plan()
        if plan is not None:
            emit("fleet_plan", plan)
        return
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
    # it open leaks the video-file handle across restarts.
    cap = app_state.cap
    if cap:
        cap.release()
        app_state.cap = None
    source_config = app_state.source_config or SourceConfig(
        source=app_state.source or "tello",
        mavlink_address=app_state.mavlink_address or get_config().drone.mavlink_default_address,
        rtsp_url=app_state.rtsp_url,
        edge_ws=app_state.edge_ws or "ws://localhost:9000",
        video_path=app_state.video_path,
        stream_video=app_state.stream_video,
    )
    _init_pipeline(source_config, target_path=app_state.target_photo_path)
    emit("dashboard_restarted", {})


def run_dashboard(source_config: SourceConfig, target_path=None, port=None, host=None):
    """Start the dashboard server.

    Args:
        source_config: Video/telemetry source configuration -- see
            ``SourceConfig`` (source mode, MAVLink address, RTSP URL, edge
            WebSocket URL, video path).
        target_path: Path to a reference photo of the target.
        port: Dashboard HTTP/WebSocket port. Defaults to `config.dashboard.port`.
        host: Bind address. Defaults to ``FLIGHTRISK_DASHBOARD_HOST``, then
            127.0.0.1. A non-loopback address requires ``FLIGHTRISK_API_KEY``.

    Raises:
        ValueError: The bind address is not loopback and no API key is
            configured. Nothing is started in that case.
    """
    global _dashboard_bind
    host = resolve_dashboard_host(host)
    # Checked before anything starts, so a refused bind connects to no drone.
    check_dashboard_bind(host)
    if host not in _LOOPBACK_HOSTS:
        log.warning(
            "Dashboard bound to non-loopback host %s; every client must present FLIGHTRISK_API_KEY",
            host,
        )
    if port is None:
        port = get_config().dashboard.port
    # Socket.IO accepts browser pages from the origins of this host and port.
    _dashboard_bind = (host, int(port))
    cors_override = cors_origins_override()
    if cors_override is not None and "*" in cors_override:
        log.warning(
            "%s=* lets a page from any web site connect to the dashboard and control the drones; "
            "use it for development only", CORS_ORIGINS_ENV,
        )
    elif cors_override is not None:
        log.warning("%s replaces the dashboard's own Socket.IO origins with %s", CORS_ORIGINS_ENV, cors_override)
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
        url_host = f"[{host}]" if ":" in host else host
        app_state.logger.info("dashboard_started", url=f"http://{url_host}:{port}", host=host)
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
