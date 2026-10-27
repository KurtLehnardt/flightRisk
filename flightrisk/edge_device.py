"""Drone-side edge-device program.

This is the edge half of the edge/ground compute split: it runs on the
drone (e.g. a Jetson Orin Nano), captures frames from a camera or video
file, runs the local detection/embedding pipeline via ``EdgeRunner``, and
streams the resulting ``DetectionMessage``s to the ground station over an
``EdgeTransportSync`` WebSocket connection. It also pulls ground->edge
control messages off the same connection and applies them:

    * ``{"type": "stream_video", "enabled": bool}`` toggles full-frame
      thumbnail streaming at runtime (``EdgeRunner.set_stream_video``).
    * ``{"type": "set_target", "reid_embedding": ..., "face_embedding": ...}``
      updates the target the ground station is looking for. The edge does
      not score against a target today, so this is simply retained (stashed
      on the runner) and logged; it never raises.

With ``--source tello`` (a real Tello) or ``--source fake`` (an in-process
simulated Tello) the edge also flies. A ``LaneExecutor`` handles the fleet
control messages (``takeoff``, ``land``, ``emergency_land_all``,
``assign_lanes``, ``start_at``, ``ping``, ``clock``, ``heartbeat``) inside a
``SafetyEnvelope`` and sends ``pong``, ``ack``, ``nack`` and ``telemetry``
back. A refused lane assignment is always answered with a ``nack`` naming
the reason, so the ground can give those lanes to another drone. In this
mode a dedicated thread receives control messages, so a landing never waits
behind frame processing. Safety controls on the edge laptop:

    * typing ``l`` and Enter on stdin lands the drone at once;
    * Ctrl+C (SIGINT) lands the drone, then exits;
    * no ground message for ``FLIGHTRISK_LINK_LOSS_LAND_S`` seconds lands it;
    * any exit from the edge loop lands the drone first.

A flying edge stamps every detection message with its ``drone_id`` and its
dead-reckoned pose (``rel_x_cm``, ``rel_y_cm``, ``heading_deg``) from the
safety envelope, so the ground can place it on the shared fleet map.

Appearance-only: with ``--source tello`` or ``--source fake`` the edge never
loads face recognition and never sends a ``face_embedding``, unless
``--with-face`` is passed or ``FLIGHTRISK_APPEARANCE_ONLY=0`` is set. Person
crops (JPEG) and ReID appearance embeddings are still sent to the ground over
the edge WebSocket, which is authenticated with ``FLIGHTRISK_EDGE_TOKEN``.
For ``--source webcam`` and ``--source file`` face recognition stays on by
default; ``--appearance-only`` or ``FLIGHTRISK_APPEARANCE_ONLY=1`` turns it
off there too. An explicit flag wins over the environment variable.

Run it with::

    python -m flightrisk.edge_device --source webcam
    python -m flightrisk.edge_device --source file --video path/to/clip.mp4
    FLIGHTRISK_EDGE_TOKEN=secret python -m flightrisk.edge_device --ws-url ws://ground:9000
    python -m flightrisk.edge_device --source tello --tello-host 192.168.10.1 \
        --drone-id drone-1 --alt-slot 100
    python -m flightrisk.edge_device --source fake --drone-id drone-2 --alt-slot 160

The core loop (``run_edge_device``) is dependency-injected -- the frame
source, transport, and runner are all passed in -- so it can be unit-tested
without a real camera, network, or vision model. The heavy real-object
construction (cv2 capture, YOLO/CLIP/InsightFace) lives in ``main()`` and is
imported lazily, keeping ``import flightrisk.edge_device`` cheap.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Callable, Iterable, TextIO

from flightrisk.drone.lane_executor import CONTROL_TYPES, LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.edge import EdgeRunner
from flightrisk.observability import StructuredLogger
from flightrisk.transport import EdgeTransportSync

__all__ = [
    "run_edge_device",
    "CV2FrameSource",
    "DroneFrameSource",
    "start_local_land_key",
    "main",
]

# Default WebSocket URL, matching the dashboard's --edge-ws default.
_DEFAULT_WS_URL = "ws://localhost:9000"
# Environment variable carrying the shared-secret auth token.
_TOKEN_ENV_VAR = "FLIGHTRISK_EDGE_TOKEN"
# Environment variable that turns face recognition off (1) or on (0).
_APPEARANCE_ONLY_ENV_VAR = "FLIGHTRISK_APPEARANCE_ONLY"
_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")
# Sources that fly a drone (and therefore run the lane executor).
_FLIGHT_SOURCES = ("tello", "fake")
# Simulated speed of the fake drone, so lane progress is visible on the map.
_FAKE_SPEED_CM_S = 100.0
# How long the control thread waits in each ``recv`` call.
_CONTROL_RECV_TIMEOUT_S = 0.1


# ---------------------------------------------------------------------------
# Frame sources
# ---------------------------------------------------------------------------


class CV2FrameSource:
    """Iterable adapter over an OpenCV ``VideoCapture``.

    Yields BGR frames until the capture is exhausted (a webcam that drops
    off, or the end of a video file), then stops. Exposes ``release()`` so
    the core loop can tear the capture down on exit. Kept deliberately thin
    so the heavy cv2 capture object itself is created in ``main()``.
    """

    def __init__(self, capture: Any):
        self._cap = capture

    def __iter__(self) -> "CV2FrameSource":
        return self

    def __next__(self):
        if self._cap is None:
            raise StopIteration
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise StopIteration
        return frame

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class DroneFrameSource:
    """Iterable over a drone controller's video frames.

    Calls ``drone.get_frame()`` at most once per ``min_interval_s`` and skips
    ``None`` (no new frame yet, or the stream is recovering) instead of
    stopping, because losing video must not end the edge loop while the
    drone is flying. Iteration stops only when ``stop_event`` is set. The
    drone itself is disconnected by ``main()``, so ``release()`` does nothing.
    """

    def __init__(self, drone: Any, stop_event: threading.Event | None = None, min_interval_s: float = 1 / 30):
        self._drone = drone
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._min_interval_s = min_interval_s
        self._next_at = 0.0

    def __iter__(self) -> "DroneFrameSource":
        return self

    def __next__(self):
        while not self._stop.is_set():
            wait = self._next_at - time.monotonic()
            if wait > 0 and self._stop.wait(wait):
                break
            self._next_at = time.monotonic() + self._min_interval_s
            frame = self._drone.get_frame()
            if frame is not None:
                return frame
        raise StopIteration

    def release(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Control-message dispatch
# ---------------------------------------------------------------------------


def _handle_control(
    msg: dict[str, Any],
    runner: EdgeRunner,
    log: StructuredLogger,
    flight: LaneExecutor | None = None,
    recv_t: float | None = None,
) -> None:
    """Apply a single ground->edge control message.

    Never raises: an unexpected/malformed control message must not take the
    frame loop down. With a ``flight`` executor, every message resets the
    link-loss watchdog, fleet control types (``CONTROL_TYPES``) go to the
    executor, and an unexpected exception lands the drone (fail safe).
    ``recv_t`` is the edge clock reading taken when the message arrived.
    """
    try:
        mtype = msg.get("type")
        if flight is not None:
            # Any message from the ground proves the link is alive.
            flight.note_ground_message()
        if mtype in CONTROL_TYPES:
            if flight is None:
                log.debug("edge_control_no_flight", type=mtype)
            else:
                flight.handle_message(msg, recv_t=recv_t)
        elif mtype == "stream_video":
            enabled = bool(msg.get("enabled"))
            runner.set_stream_video(enabled)
            log.info("edge_control_stream_video", enabled=enabled)
        elif mtype == "set_target":
            # The edge doesn't score against a target today; just retain the
            # embeddings (stashed on the runner) for future use and log it.
            try:
                runner._edge_target = {  # type: ignore[attr-defined]
                    "reid_embedding": msg.get("reid_embedding"),
                    "face_embedding": msg.get("face_embedding"),
                }
            except Exception:
                pass
            log.debug(
                "edge_control_set_target",
                has_reid=msg.get("reid_embedding") is not None,
                has_face=msg.get("face_embedding") is not None,
            )
        else:
            log.debug("edge_control_unknown", type=mtype)
    except Exception as exc:
        log.warning(
            "edge_control_error",
            type=msg.get("type") if isinstance(msg, dict) else None,
            error=repr(exc),
        )
        if flight is not None:
            try:
                flight.fail_safe("control_error")
            except Exception as land_exc:
                log.error("edge_control_fail_safe_error", error=repr(land_exc))


def _control_pump(
    transport: Any,
    runner: EdgeRunner,
    log: StructuredLogger,
    flight: LaneExecutor,
    stop_event: threading.Event,
    recv_timeout: float = _CONTROL_RECV_TIMEOUT_S,
) -> None:
    """Receive and apply control messages on a dedicated thread.

    Used when the edge flies a drone, so ``land``/``emergency_land_all`` and
    ``ping`` are handled as soon as they arrive rather than after the next
    frame is processed. This thread is the only caller of ``transport.recv``
    in that mode.
    """
    log.info("edge_control_thread_start")
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            ctrl = transport.recv(recv_timeout=recv_timeout)
        except Exception as exc:
            log.warning("edge_control_recv_error", error=repr(exc))
            stop_event.wait(recv_timeout)
            continue
        if not ctrl:
            # A transport that is disconnected returns at once; wait a
            # little rather than spinning.
            if time.monotonic() - started < recv_timeout / 2:
                stop_event.wait(recv_timeout)
            continue
        recv_t = flight.now()
        if isinstance(ctrl, dict):
            _handle_control(ctrl, runner, log, flight=flight, recv_t=recv_t)
        else:
            log.debug("edge_control_non_dict", value=ctrl)
    log.info("edge_control_thread_stop")


def _make_sender(transport: Any, log: StructuredLogger) -> Callable[[dict[str, Any]], Any]:
    """Return a function that sends one JSON control message to the ground.

    Uses the transport's generic JSON send method when it has one
    (``send_json``, ``send_message``, ``send_control`` or ``send``, checked
    in that order). For an ``EdgeTransportSync`` without such a method it
    falls back to writing on the wrapped transport's WebSocket through the
    wrapper's event-loop bridge. If neither exists, messages are dropped
    with a single error log, so the ground never receives acks or nacks and
    will not start this drone.
    """
    for name in ("send_json", "send_message", "send_control", "send"):
        method = getattr(transport, name, None)
        if callable(method):
            return method

    bridge = getattr(transport, "_bridge", None)
    inner = getattr(transport, "_transport", None)
    if bridge is not None and inner is not None:
        async def _send(msg: dict[str, Any]) -> None:
            ws = getattr(inner, "_ws", None)
            if ws is None or not getattr(inner, "connected", False):
                return
            await ws.send(json.dumps(msg))

        def _bridge_send(msg: dict[str, Any]) -> None:
            bridge.run(_send(msg), timeout=5.0)

        return _bridge_send

    reported = threading.Event()

    def _unavailable(msg: dict[str, Any]) -> None:
        if not reported.is_set():
            reported.set()
            log.error("edge_send_unavailable", transport=type(transport).__name__, type=msg.get("type"))

    return _unavailable


def start_local_land_key(
    flight: LaneExecutor,
    stream: TextIO | None = None,
    log: StructuredLogger | None = None,
) -> threading.Thread:
    """Start a thread that lands the drone when ``l`` and Enter are typed.

    Reads lines from ``stream`` (``sys.stdin`` by default). The land call
    goes straight to the executor from this thread and does not queue. The
    thread ends quietly when the stream closes.
    """
    source = sys.stdin if stream is None else stream
    log = log if log is not None else StructuredLogger(component="edge-device")

    def _loop() -> None:
        try:
            for line in source:
                if line.strip().lower() == "l":
                    log.warning("edge_local_land_key")
                    try:
                        flight.land_now("local_key")
                    except Exception as exc:
                        log.error("edge_local_land_error", error=repr(exc))
        except Exception as exc:
            log.warning("edge_land_key_stopped", error=repr(exc))

    thread = threading.Thread(target=_loop, daemon=True, name="edge-land-key")
    thread.start()
    log.info("edge_land_key_ready", hint="type l then Enter to land")
    return thread


def _wire_emergency_callback(transport: Any, flight: LaneExecutor) -> bool:
    """Land straight from the transport's emergency callback when it has one.

    A transport exposing an ``on_emergency_land`` attribute calls it on its
    own thread the moment ``emergency_land_all`` arrives, ahead of
    ``recv()``. The same message still reaches the control thread through
    ``recv()``; the executor ignores the second land while the first is in
    progress. Returns True if the callback was installed.
    """
    if not hasattr(transport, "on_emergency_land"):
        return False
    transport.on_emergency_land = lambda msg: flight.land_now("emergency_land_all")
    return True


def _install_sigint_land(flight: LaneExecutor, log: StructuredLogger) -> Any:
    """Make Ctrl+C land the drone before the process exits.

    Returns the previous handler, or None if handlers cannot be installed
    (not on the main thread).
    """
    def _on_sigint(signum, frame):
        log.warning("edge_sigint_land")
        try:
            flight.land_now("sigint")
        except Exception as exc:
            log.error("edge_sigint_land_error", error=repr(exc))
        raise KeyboardInterrupt

    try:
        return signal.signal(signal.SIGINT, _on_sigint)
    except ValueError:
        return None


def _drain_control(
    transport: Any,
    runner: EdgeRunner,
    log: StructuredLogger,
    recv_timeout: float,
) -> None:
    """Pull and dispatch any pending control messages without blocking.

    Calls ``transport.recv(recv_timeout=<small>)`` until it returns a falsy
    value (``None`` -- nothing waiting), so multiple queued control messages
    are all applied within one frame.
    """
    while True:
        ctrl = transport.recv(recv_timeout=recv_timeout)
        if not ctrl:
            break
        if isinstance(ctrl, dict):
            _handle_control(ctrl, runner, log)
        else:
            log.debug("edge_control_non_dict", value=ctrl)


# ---------------------------------------------------------------------------
# Core loop (dependency-injected, unit-testable)
# ---------------------------------------------------------------------------


def run_edge_device(
    source: Iterable,
    transport: Any,
    runner: EdgeRunner,
    poll_interval: float = 0.0,
    recv_timeout: float = 0.01,
    logger: StructuredLogger | None = None,
    stop_event: Any = None,
    flight: LaneExecutor | None = None,
) -> int:
    """Run the edge capture/detect/stream loop.

    Args:
        source: Iterable yielding BGR ``numpy`` frames. Iteration ending
            (source exhausted) stops the loop. If it exposes ``release()``,
            that is called on exit.
        transport: An ``EdgeTransportSync``-like object exposing
            ``send_detections(msg)``, ``recv(recv_timeout=...)`` and
            ``disconnect()``.
        runner: An ``EdgeRunner`` producing a ``DetectionMessage`` per frame.
        poll_interval: Optional sleep (seconds) between frames.
        recv_timeout: How long ``recv`` may wait for a control message per
            drain, keeping the poll effectively non-blocking.
        logger: Optional ``StructuredLogger``; one is created if omitted.
        stop_event: Optional object with ``is_set()`` to request a graceful
            stop between frames.
        flight: Optional ``LaneExecutor`` for a flying edge. When given, a
            dedicated thread receives and applies control messages (the
            frame loop no longer drains them), and the drone is landed
            before the transport is disconnected on any exit.

    Returns:
        The number of frames processed and sent.
    """
    log = logger if logger is not None else StructuredLogger(component="edge-device")
    frame_count = 0

    log.info("edge_device_start", stream_video=getattr(runner, "stream_video", None))
    pump_stop = threading.Event()
    pump: threading.Thread | None = None
    if flight is not None:
        pump = threading.Thread(
            target=_control_pump,
            args=(transport, runner, log, flight, pump_stop),
            daemon=True,
            name="edge-control",
        )
        pump.start()
    try:
        for frame in source:
            if stop_event is not None and stop_event.is_set():
                log.info("edge_device_stop_requested", frames=frame_count)
                break

            msg = runner.process_frame(frame)
            transport.send_detections(msg)
            frame_count += 1

            # Apply any ground->edge control messages queued since last frame.
            # A flying edge receives them on its own control thread instead.
            if flight is None:
                _drain_control(transport, runner, log, recv_timeout)

            if poll_interval:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("edge_device_interrupted", frames=frame_count)
    finally:
        if flight is not None:
            # Stop taking control messages, then land before the ground link
            # goes away. shutdown() lands only if the drone is flying, and
            # refuses any takeoff the control thread might still deliver.
            pump_stop.set()
            if pump is not None:
                pump.join(timeout=2.0)
            try:
                flight.shutdown()
            except Exception as exc:
                log.error("edge_device_flight_shutdown_error", error=repr(exc))
        try:
            transport.disconnect()
        except Exception:
            log.warning("edge_device_disconnect_error")
        release = getattr(source, "release", None)
        if callable(release):
            try:
                release()
            except Exception:
                log.warning("edge_device_source_release_error")
        log.info("edge_device_stop", frames=frame_count)

    return frame_count


# ---------------------------------------------------------------------------
# CLI entry point (real camera / network / models)
# ---------------------------------------------------------------------------


def _build_source(args: argparse.Namespace, parser: argparse.ArgumentParser, log: StructuredLogger) -> CV2FrameSource:
    """Construct a real cv2-backed frame source from parsed args."""
    import cv2  # lazy: keep module import light for tests

    if args.source == "webcam":
        log.info("edge_source_webcam", index=0)
        return CV2FrameSource(cv2.VideoCapture(0))

    # args.source == "file"
    if not os.path.exists(args.video):
        parser.error(f"--video path does not exist: {args.video}")
    log.info("edge_source_file", video=args.video)
    return CV2FrameSource(cv2.VideoCapture(args.video))


def _resolve_appearance_only(args: argparse.Namespace, environ: Any = None) -> bool:
    """Decide whether this edge runs appearance-only (no face recognition).

    ``--appearance-only`` or ``--with-face`` wins. Otherwise an explicit
    ``FLIGHTRISK_APPEARANCE_ONLY`` decides. Otherwise a flying source
    (``tello``, ``fake``) is appearance-only and ``webcam``/``file`` are not.
    Raises ``ValueError`` when the environment variable is not a boolean.
    """
    explicit = getattr(args, "appearance_only", None)
    if explicit is not None:
        return bool(explicit)
    env = os.environ if environ is None else environ
    raw = env.get(_APPEARANCE_ONLY_ENV_VAR, "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    if raw:
        raise ValueError(
            f"{_APPEARANCE_ONLY_ENV_VAR}={raw!r} is not a boolean (use 1/0, true/false, yes/no, on/off)"
        )
    return args.source in _FLIGHT_SOURCES


def _envelope_pose_provider(envelope: SafetyEnvelope) -> Callable[[], dict[str, float]]:
    """Return a pose provider for ``EdgeRunner`` that reads the envelope's dead-reckoned pose."""

    def _pose() -> dict[str, float]:
        pose = envelope.pose_snapshot()
        return {
            "rel_x_cm": round(pose.x_cm, 1),
            "rel_y_cm": round(pose.y_cm, 1),
            "heading_deg": round(pose.heading_deg, 1),
        }

    return _pose


def _build_runner(
    args: argparse.Namespace,
    log: StructuredLogger,
    pose_provider: Callable[[], Any] | None = None,
) -> EdgeRunner:
    """Construct detector/reid/face + EdgeRunner the way the dashboard does.

    A flying source tags every detection message with ``args.drone_id`` and
    the pose from ``pose_provider``. Face recognition is constructed only
    when the edge is not appearance-only (see ``_resolve_appearance_only``).
    """
    # Lazy imports: these pull in ultralytics / torch / insightface, which are
    # heavy and unnecessary for importing this module or unit-testing the loop.
    from flightrisk.vision.detector import PersonDetector

    detector = PersonDetector(model_name="yolo11n.pt", confidence=0.4)

    reid = None
    try:
        from flightrisk.vision.reid import PersonReID
        reid = PersonReID(match_threshold=0.55)
    except Exception as e:
        log.warning("reid_unavailable", error=str(e))

    appearance_only = _resolve_appearance_only(args)
    face = None
    if appearance_only:
        log.info("edge_appearance_only", source=args.source, face_embedding=False)
    else:
        try:
            from flightrisk.vision.face import FaceRecognizer
            face = FaceRecognizer(match_threshold=0.35)
        except Exception as e:
            log.warning("insightface_unavailable", error=str(e))

    flying = args.source in _FLIGHT_SOURCES
    return EdgeRunner(
        detector=detector,
        reid=reid,
        face=face,
        stream_video=args.stream_video,
        drone_id=args.drone_id if flying else None,
        pose_provider=pose_provider if flying else None,
        appearance_only=appearance_only,
    )


def _parse_alt_slot(raw: str) -> float:
    """Parse ``--alt-slot`` as centimetres.

    Values below 10 are read as metres (``1.6`` means 160 cm), because a
    slot under 10 cm cannot be flown and the fleet docs quote slots both
    ways. The result must be a valid slot for ``SafetyEnvelope``.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--alt-slot must be a number, got {raw!r}") from exc
    if 0 < value < 10:
        value *= 100.0
    try:
        return SafetyEnvelope.validate_alt_slot(round(value, 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FlightRisk edge-device program")
    parser.add_argument(
        "--source", choices=("webcam", "file", "tello", "fake"), default="webcam",
        help="Frame source: local webcam (index 0), a video file, a Tello drone "
             "(flies), or an in-process fake Tello (flies, no hardware)",
    )
    parser.add_argument("--video", type=str, help="Video file path (when --source=file)")
    parser.add_argument(
        "--ws-url", type=str,
        default=os.environ.get("FLIGHTRISK_EDGE_WS", _DEFAULT_WS_URL),
        help=f"Ground-station WebSocket URL (default: {_DEFAULT_WS_URL})",
    )
    parser.add_argument(
        "--stream-video", action="store_true",
        help="Stream full-frame thumbnails from the start (default: off)",
    )
    parser.add_argument(
        "--tello-host", type=str, default=None,
        help="Tello IP address (when --source=tello; default from config, 192.168.10.1)",
    )
    parser.add_argument(
        "--drone-id", type=str, default="drone-1",
        help="Drone id used in fleet messages (default: drone-1)",
    )
    parser.add_argument(
        "--alt-slot", type=_parse_alt_slot, default=100.0,
        help="Altitude slot in cm (default 100); values below 10 are metres, so 1.6 means 160 cm",
    )
    face = parser.add_mutually_exclusive_group()
    face.add_argument(
        "--appearance-only", dest="appearance_only", action="store_true",
        help="Never load face recognition or send face embeddings (the default for "
             "--source tello and fake unless FLIGHTRISK_APPEARANCE_ONLY=0)",
    )
    face.add_argument(
        "--with-face", dest="appearance_only", action="store_false",
        help="Also compute and send face embeddings (close-range use only; faces are "
             "not resolvable at search altitude)",
    )
    parser.set_defaults(appearance_only=None)
    return parser


def _build_drone(args: argparse.Namespace, log: StructuredLogger) -> Any:
    """Construct the drone controller for a flying source."""
    if args.source == "fake":
        from flightrisk.drone.tello import FakeTelloController

        log.info("edge_source_fake_tello", drone_id=args.drone_id)
        return FakeTelloController(name=args.drone_id, speed_cm_s=_FAKE_SPEED_CM_S)

    from flightrisk.drone.tello import TelloController

    log.info("edge_source_tello", drone_id=args.drone_id, tello_host=args.tello_host)
    return TelloController(name=args.drone_id, host=args.tello_host)


def _run_flight_edge(
    args: argparse.Namespace,
    log: StructuredLogger,
    stop_event: threading.Event | None = None,
) -> None:
    """Run a flying edge: drone, lane executor, safety controls and the edge loop.

    Setting ``stop_event`` ends the edge loop; the drone lands first.
    """
    envelope = SafetyEnvelope.from_env(alt_slot_cm=args.alt_slot)
    runner = _build_runner(args, log, pose_provider=_envelope_pose_provider(envelope))
    drone = _build_drone(args, log)

    token = os.environ.get(_TOKEN_ENV_VAR)
    transport = EdgeTransportSync(ws_url=args.ws_url, runner=runner, token=token)
    flight = LaneExecutor(drone, envelope, args.drone_id, send=_make_sender(transport, log), logger=log)
    if stop_event is None:
        stop_event = threading.Event()

    if not drone.connect():
        log.error("edge_drone_connect_failed", drone_id=args.drone_id)
        raise SystemExit(1)

    previous_sigint = None
    try:
        flight.start()
        _wire_emergency_callback(transport, flight)
        start_local_land_key(flight, sys.stdin, log)
        previous_sigint = _install_sigint_land(flight, log)
        log.info(
            "edge_connect",
            ws_url=args.ws_url,
            authenticated=token is not None,
            drone_id=args.drone_id,
            alt_slot_cm=envelope.alt_slot_cm,
            geofence_cm=envelope.geofence_cm,
            link_loss_land_s=envelope.link_watchdog.timeout_s,
            appearance_only=runner.appearance_only,
        )
        transport.connect()
        run_edge_device(
            # The fake drone only yields blank frames, so a low rate saves CPU.
            DroneFrameSource(drone, stop_event, min_interval_s=0.2 if args.source == "fake" else 1 / 30),
            transport,
            runner,
            logger=log,
            stop_event=stop_event,
            flight=flight,
        )
    finally:
        # Belt and braces: land (if flying) before anything else is torn down.
        try:
            flight.shutdown()
        except Exception as exc:
            log.error("edge_flight_shutdown_error", error=repr(exc))
        try:
            transport.disconnect()
        except Exception:
            log.warning("edge_disconnect_error")
        try:
            drone.disconnect()
        except Exception as exc:
            log.error("edge_drone_disconnect_error", error=repr(exc))
        if previous_sigint is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint)
            except ValueError:
                pass


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python -m flightrisk.edge_device``."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.source == "file" and not args.video:
        parser.error("--video is required when --source=file")
    try:
        _resolve_appearance_only(args)
    except ValueError as exc:
        parser.error(str(exc))

    log = StructuredLogger(component="edge-device")

    if args.source in _FLIGHT_SOURCES:
        _run_flight_edge(args, log)
        return

    source = _build_source(args, parser, log)
    runner = _build_runner(args, log)

    token = os.environ.get(_TOKEN_ENV_VAR)
    transport = EdgeTransportSync(ws_url=args.ws_url, runner=runner, token=token)

    log.info("edge_connect", ws_url=args.ws_url, authenticated=token is not None)
    transport.connect()
    try:
        run_edge_device(source, transport, runner, logger=log)
    finally:
        # run_edge_device already disconnects + releases in its own finally;
        # this is a belt-and-suspenders teardown in case it raised before
        # reaching that block. Both disconnect() calls are idempotent.
        try:
            transport.disconnect()
        except Exception:
            log.warning("edge_disconnect_error")


if __name__ == "__main__":
    main()
