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

Every failure to look (no fresh frame, a blank frame, a model that takes too
long, an exception, a depth map the guard reports as degenerate or a score that
is not finite) is an ``unavailable`` result and never ``clear``. A blank frame
is one whose pixels all have the same value, such as the black frame a
restarted video decoder starts on; it shows nothing, so it is reported like a
missing frame (``stale``, cause ``blank frame``) without running the model.
Inference runs on one reusable helper thread that ``sense`` joins with a
timeout, so a hung model never blocks the caller and no thread is created per
call. While a timed-out inference is still running, later calls report
``timeout`` at once instead of queueing behind it.

``sense(after, timeout_s)`` spends at most ``timeout_s`` in all: the wait for a
fresh frame, the inference and the wait for a fresh person result share that
one budget, so the executor, which abandons a call shortly after
``timeout_s``, never gives up on a call that was still within its time.
"""
from __future__ import annotations

import logging
import math
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
    CAUSE_BLANK_FRAME,
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
#: ``blank_frame`` samples at most this many rows and this many columns.
_BLANK_SAMPLE_PER_AXIS = 64


def blank_frame(frame: Any) -> bool:
    """Whether every sampled pixel of ``frame`` has the same value, so the frame shows nothing.

    A restarted video decoder starts on such a frame (djitellopy's is all
    black). The check reads a strided grid of at most
    ``_BLANK_SAMPLE_PER_AXIS`` rows and columns, so it costs a few thousand
    comparisons whatever the frame size. A pixel's value is all of its
    channels, so a frame of one colour is blank even when its channels differ.
    A frame with no pixels is blank. A real camera view has sensor noise, so
    it is not blank; a view the sample happens to call blank is refused,
    which fails closed. Raises ``ValueError`` for something that is not an
    image with rows and columns.
    """
    pixels = np.asarray(frame)
    if pixels.ndim < 2:
        raise ValueError(f"a camera frame needs rows and columns, got shape {pixels.shape}")
    if pixels.size == 0:
        return True
    rows, cols = pixels.shape[0], pixels.shape[1]
    step_rows = -(-rows // _BLANK_SAMPLE_PER_AXIS)
    step_cols = -(-cols // _BLANK_SAMPLE_PER_AXIS)
    sample = pixels[::step_rows, ::step_cols]
    flat = sample.reshape(sample.shape[0] * sample.shape[1], -1)
    return bool(np.all(flat.min(axis=0) == flat.max(axis=0)))


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

    ``timeout_s`` of ``sense`` is the budget of the whole call. The wait for a
    fresh frame takes at most ``frame_wait_s`` counted from ``after + settle_s``
    (or from the call, when that is later) and never more than the budget; the
    inference gets what is left and times out when it runs out; the wait for a
    fresh person result (``detection_wait_s``) gets only what is left after
    that, and a cue still without a fresh result when the budget is spent is
    ``unknown``. A detection result already there is still read, because that
    costs no time and can only add a ``blocked``: the person cue never clears
    a step.
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
        budget_end = started + max(0.0, float(timeout_s))

        def latency() -> float:
            return round((self._clock() - started) * 1000.0, 1)

        def remaining() -> float:
            return budget_end - self._clock()

        def timed_out(worker_busy: bool) -> SenseResult:
            return self._finish(
                SenseResult.make_unavailable(
                    REASON_TIMEOUT, latency_ms=latency(),
                    detail={"worker_busy": worker_busy, "timeout_s": timeout_s},
                ),
                None, ACTION_TIMEOUT,
            )

        try:
            timed = self._frames.wait_for(
                after, self.settle_s, self.frame_wait_s, limit_s=max(0.0, remaining()),
            )
        except Exception as exc:
            return self._failed(REASON_ERROR, latency(), exc)
        if timed is None:
            self._note_person_cue(PERSON_CUE_UNKNOWN)
            return self._finish(
                SenseResult.make_unavailable(REASON_STALE, latency_ms=latency()),
                None, ACTION_STALE,
            )
        if remaining() <= 0:
            # The frame came at the very end of the budget: there is no time left to judge it.
            return timed_out(False)

        frame = timed.frame
        try:
            blank = blank_frame(frame)
        except Exception as exc:  # noqa: BLE001 - a frame that cannot be checked is a failure to look
            return self._failed(REASON_ERROR, latency(), exc)
        if blank:
            # Nothing to look at (a restarted decoder's black frame): the same as no fresh frame.
            self._note_person_cue(PERSON_CUE_UNKNOWN)
            return self._finish(
                SenseResult.make_unavailable(
                    REASON_STALE, latency_ms=latency(), detail={"cause": CAUSE_BLANK_FRAME},
                ),
                None, ACTION_STALE,
            )
        job = self._worker.submit(lambda: self._guard.check_path(frame))
        if job is None or not job.done.wait(max(0.0, remaining())):
            return timed_out(job is None)
        if job.error is not None:
            return self._failed(REASON_ERROR, latency(), job.error)
        if remaining() < 0:
            return timed_out(False)
        check = job.result
        try:
            depth_blocked = not bool(check["safe"])
            depth_score = float(check["center_depth"])
            problem = self._unusable_check(check, depth_score)
        except Exception as exc:
            return self._failed(REASON_ERROR, latency(), exc)
        if problem is not None:
            detail = {"degenerate": check.get("degenerate_reason")} if check.get("degenerate") else None
            return self._failed(REASON_ERROR, latency(), problem, detail=detail)

        person = self._person_cue(after, wait=not depth_blocked, budget_end=budget_end)
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

    @staticmethod
    def _unusable_check(check: dict, depth_score: float) -> str | None:
        """Why the guard's result cannot be trusted, or None when it can.

        A result the guard flags as degenerate (a NaN or infinite value in the
        map, a uniform map, no positive scale) and a score that is not finite
        are failures to look, whatever ``safe`` says.
        """
        if check.get("degenerate"):
            return f"degenerate depth map: {check.get('degenerate_reason') or 'unspecified'}"
        if not math.isfinite(depth_score):
            return f"depth score is not finite: {depth_score!r}"
        for key in ("center_p90", "left_depth", "right_depth"):
            value = check.get(key)
            if value is not None and not math.isfinite(float(value)):
                return f"{key} is not finite: {value!r}"
        return None

    def status(self) -> dict[str, Any]:
        """A JSON friendly summary for telemetry and logs.

        ``non_default_thresholds`` names each threshold that differs from its
        default (``OBSTACLE_MIN_SAFE_DEPTH``, ``OBSTACLE_PERSON_FRACTION``) with
        its value, and is empty when both are the defaults.
        """
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
            "non_default_thresholds": self.non_default_thresholds(),
            "frames_seen": self._frames.frames_seen,
            "worker_busy": self._worker.busy,
            "person_cue_down": self._unknown_streak >= OBSTACLE_PERSON_CUE_DOWN_AFTER,
            "reason": reason,
            TELEMETRY_STATE: state,
            TELEMETRY_LATENCY_MS: latency,
            TELEMETRY_SENSOR_OK: state != OBSTACLE_STATE_UNAVAILABLE,
        }

    def non_default_thresholds(self) -> dict[str, float]:
        """The thresholds that differ from their defaults, by name."""
        changed: dict[str, float] = {}
        if self.min_safe_depth != OBSTACLE_MIN_SAFE_DEPTH:
            changed["min_safe_depth"] = self.min_safe_depth
        if self.person_fraction != OBSTACLE_PERSON_FRACTION:
            changed["person_fraction"] = self.person_fraction
        return changed

    def close(self) -> None:
        """Stop the helper thread. A hung inference keeps its daemon thread."""
        self._worker.stop()

    # -- person cue ---------------------------------------------------------

    def _person_cue(self, after: float, wait: bool, budget_end: float | None = None) -> str:
        """The person cue for a view after ``after + settle_s``.

        Waits up to ``detection_wait_s`` (none when ``wait`` is false) for a
        detection result whose frame was obtained at or after that time, and
        never past ``budget_end``. Without such a result the cue is ``unknown``.
        """
        runner = self._runner
        if runner is None:
            return PERSON_CUE_UNKNOWN
        threshold = after + self.settle_s
        deadline = self._clock() + (self.detection_wait_s if wait else 0.0)
        if budget_end is not None:
            deadline = min(deadline, budget_end)
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
            now = self._clock()
            if now >= deadline:
                return PERSON_CUE_UNKNOWN
            self._sleep(min(0.02, deadline - now))

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

    def _failed(
        self, reason: str, latency_ms: float, error: BaseException | str, detail: dict[str, Any] | None = None,
    ) -> SenseResult:
        """An ``unavailable`` result for a failure; the error is logged once per change.

        ``error`` is the exception, or a text for a result that came back but
        cannot be used (a degenerate depth map). ``detail`` adds to the
        result's ``detail``, which always holds ``error``.
        """
        exc = error if isinstance(error, BaseException) else None
        key = f"{type(exc).__name__}: {exc}" if exc is not None else str(error)
        if key != self._last_error_key:
            self._last_error_key = key
            log_event(
                logger, logging.ERROR, EVENT_SENSOR_ERROR,
                f"obstacle sensor failed: {key}",
                category="error", component="edge", drone_id=self.drone_id,
                error=key, exc_info=exc,
            )
        return self._finish(
            SenseResult.make_unavailable(reason, latency_ms=latency_ms, detail={**(detail or {}), "error": key}),
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
