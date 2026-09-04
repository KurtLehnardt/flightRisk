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

Run it with::

    python -m flightrisk.edge_device --source webcam
    python -m flightrisk.edge_device --source file --video path/to/clip.mp4
    FLIGHTRISK_EDGE_TOKEN=secret python -m flightrisk.edge_device --ws-url ws://ground:9000

The core loop (``run_edge_device``) is dependency-injected -- the frame
source, transport, and runner are all passed in -- so it can be unit-tested
without a real camera, network, or vision model. The heavy real-object
construction (cv2 capture, YOLO/CLIP/InsightFace) lives in ``main()`` and is
imported lazily, keeping ``import flightrisk.edge_device`` cheap.
"""
from __future__ import annotations

import argparse
import os
import time
from typing import Any, Iterable

from flightrisk.edge import EdgeRunner
from flightrisk.observability import StructuredLogger
from flightrisk.transport import EdgeTransportSync

__all__ = [
    "run_edge_device",
    "CV2FrameSource",
    "main",
]

# Default WebSocket URL, matching the dashboard's --edge-ws default.
_DEFAULT_WS_URL = "ws://localhost:9000"
# Environment variable carrying the shared-secret auth token.
_TOKEN_ENV_VAR = "FLIGHTRISK_EDGE_TOKEN"


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


# ---------------------------------------------------------------------------
# Control-message dispatch
# ---------------------------------------------------------------------------


def _handle_control(msg: dict[str, Any], runner: EdgeRunner, log: StructuredLogger) -> None:
    """Apply a single ground->edge control message.

    Never raises: an unexpected/malformed control message must not take the
    frame loop down.
    """
    try:
        mtype = msg.get("type")
        if mtype == "stream_video":
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
    except Exception:
        log.warning("edge_control_error", type=msg.get("type") if isinstance(msg, dict) else None)


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

    Returns:
        The number of frames processed and sent.
    """
    log = logger if logger is not None else StructuredLogger(component="edge-device")
    frame_count = 0

    log.info("edge_device_start", stream_video=getattr(runner, "stream_video", None))
    try:
        for frame in source:
            if stop_event is not None and stop_event.is_set():
                log.info("edge_device_stop_requested", frames=frame_count)
                break

            msg = runner.process_frame(frame)
            transport.send_detections(msg)
            frame_count += 1

            # Apply any ground->edge control messages queued since last frame.
            _drain_control(transport, runner, log, recv_timeout)

            if poll_interval:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("edge_device_interrupted", frames=frame_count)
    finally:
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


def _build_runner(args: argparse.Namespace, log: StructuredLogger) -> EdgeRunner:
    """Construct detector/reid/face + EdgeRunner the way the dashboard does."""
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

    face = None
    try:
        from flightrisk.vision.face import FaceRecognizer
        face = FaceRecognizer(match_threshold=0.35)
    except Exception as e:
        log.warning("insightface_unavailable", error=str(e))

    return EdgeRunner(detector=detector, reid=reid, face=face, stream_video=args.stream_video)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python -m flightrisk.edge_device``."""
    parser = argparse.ArgumentParser(description="FlightRisk edge-device program")
    parser.add_argument(
        "--source", choices=("webcam", "file"), default="webcam",
        help="Frame source: local webcam (index 0) or a video file",
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
    args = parser.parse_args(argv)

    if args.source == "file" and not args.video:
        parser.error("--video is required when --source=file")

    log = StructuredLogger(component="edge-device")

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
