"""The forward camera obstacle sensor of the fleet lane path.

``ForwardCameraSensor`` implements the ``ObstacleSensor`` protocol of
``flightrisk.drone.obstacle_sensor`` for a drone with one forward camera (the
Tello). It combines two cues, both evaluated on a view taken while the drone
hovers (design ``.claude/build/obstacle/design.md``, section 2):

* Depth, the primary cue: MiDaS Small through ``ObstacleGuard`` with
  ``center_stat="p90"``, run on a frame that arrived after the last stop plus
  the settle time (``LatestFrame.wait_for``). Only this cue can make a result
  ``clear``.
* A close person, the secondary cue: a box from the runner's newest detection
  result that is taller than ``OBSTACLE_PERSON_FRACTION`` of the frame height
  and centred in the middle third of the frame width. The cue can add a
  ``blocked`` and never a ``clear``. When the runner has produced no result
  since ``after + settle`` (a frame stride makes that likely), the cue is
  ``unknown``: it neither blocks nor clears and the depth cue decides.

Every failure to look (no fresh frame, a model that takes too long, an
exception) is an ``unavailable`` result and never ``clear``. Inference runs on
one reusable helper thread that ``sense`` joins with a timeout, so a hung model
never blocks the caller and no thread is created per call. While a timed-out
inference is still running, later calls report ``timeout`` at once instead of
queueing behind it.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from flightrisk import obs_events
from flightrisk.drone.obstacle_sensor import (
    ACTION_PERSON,
    ACTION_PERSON_UNKNOWN,
    ACTION_STALE,
    ACTION_TIMEOUT,
    CUE_DEPTH,
    CUE_PERSON,
    EVENT_PERSON_CUE_DOWN,
    EVENT_SENSOR_ERROR,
    OBSTACLE_DETECTION_WAIT_S,
    OBSTACLE_FRAME_WAIT_S,
    OBSTACLE_MIN_SAFE_DEPTH,
    OBSTACLE_PERSON_CENTRAL_FRACTION,
    OBSTACLE_PERSON_CUE_DOWN_AFTER,
    OBSTACLE_PERSON_FRACTION,
    OBSTACLE_SENSE_TIMEOUT_S,
    OBSTACLE_SETTLE_S,
    OBSTACLE_STATE_UNAVAILABLE,
    PERSON_CUE_BLOCKED,
    PERSON_CUE_CLEAR,
    PERSON_CUE_UNKNOWN,
    REASON_ERROR,
    REASON_STALE,
    REASON_TIMEOUT,
    SENSING_STEPPED,
    TELEMETRY_LATENCY_MS,
    TELEMETRY_SENSOR_OK,
    TELEMETRY_STATE,
    LatestFrame,
    SenseResult,
)
from flightrisk.obs_log import log_event

logger = logging.getLogger(__name__)

#: ``flightrisk_obstacle_checks`` action of a sensor failure (the existing name).
ACTION_UNAVAILABLE = "unavailable"

#: Size of the blank frame that warms the model at construction (the Tello's).
_WARMUP_FRAME_SHAPE = (720, 960, 3)


class _InferenceWorker:
    """One reusable daemon thread that runs one job at a time.

    ``submit`` returns ``None`` while a previous job has not finished (a hung
    model), so a stuck inference is never queued behind and the thread count
    stays at one.
    """

    class Job:
        def __init__(self, fn: Callable[[], Any]):
            self.fn = fn
            self.done = threading.Event()
            self.result: Any = None
            self.error: BaseException | None = None

    def __init__(self, name: str):
        self._jobs: queue.SimpleQueue[_InferenceWorker.Job | None] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._busy = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def submit(self, fn: Callable[[], Any]) -> _InferenceWorker.Job | None:
        with self._lock:
            if self._busy:
                return None
            self._busy = True
        job = self.Job(fn)
        self._jobs.put(job)
        return job

    def stop(self) -> None:
        self._jobs.put(None)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job.result = job.fn()
            except BaseException as exc:  # reported to the caller through the job
                job.error = exc
            finally:
                with self._lock:
                    self._busy = False
                job.done.set()


def close_person_in_lane(
    detections: Any,
    frame_width: int,
    frame_height: int,
    fraction: float = OBSTACLE_PERSON_FRACTION,
    central_fraction: float = OBSTACLE_PERSON_CENTRAL_FRACTION,
) -> bool:
    """Whether any detection is a close person straight ahead.

    A box counts when it is taller than ``fraction`` of the frame height and
    its horizontal centre lies inside the middle ``central_fraction`` of the
    frame width.
    """
    if frame_width <= 0 or frame_height <= 0:
        return False
    half_band = central_fraction / 2.0
    for det in detections:
        x1, y1, x2, y2 = (float(v) for v in det.bbox)
        if (y2 - y1) / frame_height <= fraction:
            continue
        centre = ((x1 + x2) / 2.0) / frame_width
        if abs(centre - 0.5) <= half_band:
            return True
    return False


class ForwardCameraSensor:
    """MiDaS depth plus a person cue behind the ``ObstacleSensor`` protocol.

    ``frames`` is the ``LatestFrame`` holder the caller registers as the
    drone's frame callback (``drone.on_frame(frames)``). ``runner`` is the
    ``EdgeRunner`` whose ``last_detections`` and ``last_detections_at`` feed
    the person cue; without one the cue is always ``unknown``. ``guard`` is an
    already built ``ObstacleGuard`` (for tests and for sharing); by default one
    is built here with ``check_interval=0.0`` and ``center_stat="p90"``.

    The model is built and warmed in the constructor, which raises when torch,
    the MiDaS weights or the first inference fail, so the caller can report
    the sensor as missing before the drone connects.

    ``timeout_s`` of ``sense`` bounds the inference only; waiting for a fresh
    frame is bounded separately by ``frame_wait_s``.
    """

    def __init__(
        self,
        frames: LatestFrame,
        runner: Any = None,
        *,
        guard: Any = None,
        min_safe_depth: float = OBSTACLE_MIN_SAFE_DEPTH,
        person_fraction: float = OBSTACLE_PERSON_FRACTION,
        settle_s: float = OBSTACLE_SETTLE_S,
        frame_wait_s: float = OBSTACLE_FRAME_WAIT_S,
        detection_wait_s: float = OBSTACLE_DETECTION_WAIT_S,
        drone_id: str | None = None,
        calibrated: bool = False,
        warm: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._frames = frames
        self._runner = runner
        self.min_safe_depth = float(min_safe_depth)
        self.person_fraction = float(person_fraction)
        self.settle_s = settle_s
        self.frame_wait_s = frame_wait_s
        self.detection_wait_s = detection_wait_s
        self.drone_id = drone_id
        self.calibrated = bool(calibrated)
        self._clock = clock
        self._sleep = sleep

        if guard is None:
            from flightrisk.drone.obstacle import ObstacleGuard

            guard = ObstacleGuard(
                min_safe_depth=self.min_safe_depth, check_interval=0.0, center_stat="p90",
            )
        self._guard = guard
        if warm:
            guard.check_path(np.zeros(_WARMUP_FRAME_SHAPE, dtype=np.uint8))

        self._worker = _InferenceWorker(f"obstacle-sense-{drone_id or 'drone'}")
        self._lock = threading.Lock()
        self._last_state: str | None = None
        self._last_latency_ms: float | None = None
        self._last_reason = ""
        self._last_error_key: str | None = None
        self._unknown_streak = 0
        self._person_down_logged = False

    # -- protocol -----------------------------------------------------------

    def sense(self, after: float, timeout_s: float = OBSTACLE_SENSE_TIMEOUT_S) -> SenseResult:
        started = self._clock()

        def latency() -> float:
            return round((self._clock() - started) * 1000.0, 1)

        try:
            timed = self._frames.wait_for(after, self.settle_s, self.frame_wait_s)
        except Exception as exc:
            return self._failed(REASON_ERROR, latency(), exc)
        if timed is None:
            self._note_person_cue(PERSON_CUE_UNKNOWN)
            return self._finish(
                SenseResult.make_unavailable(REASON_STALE, latency_ms=latency()),
                None, ACTION_STALE,
            )

        frame = timed.frame
        job = self._worker.submit(lambda: self._guard.check_path(frame))
        if job is None or not job.done.wait(timeout_s):
            return self._finish(
                SenseResult.make_unavailable(
                    REASON_TIMEOUT, latency_ms=latency(),
                    detail={"worker_busy": job is None, "timeout_s": timeout_s},
                ),
                None, ACTION_TIMEOUT,
            )
        if job.error is not None:
            return self._failed(REASON_ERROR, latency(), job.error)
        check = job.result
        try:
            depth_blocked = not bool(check["safe"])
            depth_score = float(check["center_depth"])
        except Exception as exc:
            return self._failed(REASON_ERROR, latency(), exc)

        person = self._person_cue(after, wait=not depth_blocked)
        self._note_person_cue(person)
        detail = {
            "depth_action": check.get("action"),
            "depth_confidence": check.get("confidence"),
            "center_stat": check.get("center_stat"),
            "min_safe_depth": self.min_safe_depth,
        }
        score = {"person_cue": person, "depth_score": depth_score, "detail": detail}
        if person == PERSON_CUE_BLOCKED:
            result = SenseResult.make_blocked(CUE_PERSON, latency_ms=latency(), **score)
            action = ACTION_PERSON
        elif depth_blocked:
            result = SenseResult.make_blocked(CUE_DEPTH, latency_ms=latency(), **score)
            action = None
        else:
            result = SenseResult.make_clear(cue=CUE_DEPTH, latency_ms=latency(), **score)
            action = ACTION_PERSON_UNKNOWN if person == PERSON_CUE_UNKNOWN else None
        return self._finish(result, check, action)

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._last_state
            latency = self._last_latency_ms
            reason = self._last_reason
        return {
            "sensor": "forward_camera",
            "mode": SENSING_STEPPED,
            "calibrated": self.calibrated,
            "center_stat": "p90",
            "min_safe_depth": self.min_safe_depth,
            "person_fraction": self.person_fraction,
            "frames_seen": self._frames.frames_seen,
            "worker_busy": self._worker.busy,
            "person_cue_down": self._unknown_streak >= OBSTACLE_PERSON_CUE_DOWN_AFTER,
            "reason": reason,
            TELEMETRY_STATE: state,
            TELEMETRY_LATENCY_MS: latency,
            TELEMETRY_SENSOR_OK: state != OBSTACLE_STATE_UNAVAILABLE,
        }

    def close(self) -> None:
        """Stop the helper thread. A hung inference keeps its daemon thread."""
        self._worker.stop()

    # -- person cue ---------------------------------------------------------

    def _person_cue(self, after: float, wait: bool) -> str:
        runner = self._runner
        if runner is None:
            return PERSON_CUE_UNKNOWN
        threshold = after + self.settle_s
        deadline = self._clock() + (self.detection_wait_s if wait else 0.0)
        while True:
            # The stamp is read before the detections: the runner stores the
            # detections first, so a fresh stamp has fresh detections.
            at = getattr(runner, "last_detections_at", None)
            if at is not None and at >= threshold:
                snapshot = getattr(runner, "last_detections", None)
                if snapshot is None:
                    return PERSON_CUE_UNKNOWN
                detections, width, height = snapshot
                if close_person_in_lane(detections, width, height, self.person_fraction):
                    return PERSON_CUE_BLOCKED
                return PERSON_CUE_CLEAR
            if self._clock() >= deadline:
                return PERSON_CUE_UNKNOWN
            self._sleep(0.02)

    def _note_person_cue(self, person: str) -> None:
        if person != PERSON_CUE_UNKNOWN:
            self._unknown_streak = 0
            self._person_down_logged = False
            return
        self._unknown_streak += 1
        if self._unknown_streak >= OBSTACLE_PERSON_CUE_DOWN_AFTER and not self._person_down_logged:
            self._person_down_logged = True
            log_event(
                logger, logging.WARNING, EVENT_PERSON_CUE_DOWN,
                "the person cue has been unknown for several gates in a row",
                category="flight_safety", component="edge",
                drone_id=self.drone_id, unknown_in_a_row=self._unknown_streak,
            )

    # -- bookkeeping --------------------------------------------------------

    def _failed(self, reason: str, latency_ms: float, exc: BaseException) -> SenseResult:
        key = f"{type(exc).__name__}: {exc}"
        if key != self._last_error_key:
            self._last_error_key = key
            log_event(
                logger, logging.ERROR, EVENT_SENSOR_ERROR,
                f"obstacle sensor failed: {key}",
                category="error", component="edge", drone_id=self.drone_id,
                error=key, exc_info=exc,
            )
        return self._finish(
            SenseResult.make_unavailable(reason, latency_ms=latency_ms, detail={"error": key}),
            None, ACTION_UNAVAILABLE, error=True,
        )

    def _finish(
        self, result: SenseResult, check: dict | None, action: str | None, error: bool = False,
    ) -> SenseResult:
        if not error:
            self._last_error_key = None
        with self._lock:
            self._last_state = result.state
            self._last_latency_ms = result.latency_ms
            self._last_reason = result.reason
        obs_events.obstacle_check(
            check, action=action, min_safe_depth=self.min_safe_depth, drone_id=self.drone_id,
        )
        return result
