"""The forward camera obstacle sensor of the fleet lane path.

``ForwardCameraSensor`` implements the ``ObstacleSensor`` protocol of
``flightrisk.drone.obstacle_sensor`` for a drone with one forward camera (the
Tello). It combines two cues, both evaluated on a view taken while the drone
hovers (design ``.claude/build/obstacle/design.md``, section 2):

* Depth, the primary cue: MiDaS Small through ``ObstacleGuard`` with
  ``center_stat="p90"``, run on a frame captured after the last stop plus
  the settle time (``LatestFrame.wait_for``; the capture time is the video
  decoder's stamp when it gives one, else the frame's arrival). The holder's
  frames are BGR (``SENSOR_CHANNEL_ORDER``), which the guard turns into the
  RGB that MiDaS expects. Only this cue can make a result ``clear``.
* A close person, the secondary cue: a box from the runner's newest detection
  result that is taller than ``OBSTACLE_PERSON_FRACTION`` of the frame height
  and centred in the middle third of the frame width. The cue can add a
  ``blocked`` and never a ``clear``. When the runner has produced no result
  for a view since ``after + settle`` (a slow detector makes that likely; the
  edge refuses a frame stride above 1 while sensing is required), the cue is
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

``metric`` chooses what decides the depth cue: ``score`` (the default, the
guard's p90 centre score against ``min_safe_depth``, unchanged) or the opt-in
``floor_ratio`` (``obstacle.centre_floor_ratio`` against ``floor_ratio_max``,
``FLIGHTRISK_OBSTACLE_METRIC``). Under ``floor_ratio`` a view without a usable
floor reference is ``blocked`` by the depth cue (cut reason ``obstacle``, depth
state ``no_floor_reference``), never ``clear`` and never ``unavailable``. Every
failure to look above stays ``unavailable`` under either metric, and the person
cue is the same.
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
    DEPTH_METRIC_KEY,
    DEPTH_METRIC_MAX_KEY,
    DEPTH_METRIC_VALUE_KEY,
    DEPTH_STATE_BLOCKED,
    DEPTH_STATE_CLEAR,
    DEPTH_STATE_KEY,
    DEPTH_STATE_NO_FLOOR_REFERENCE,
    EVENT_OBSTACLE_METRIC,
    EVENT_PERSON_CUE_DOWN,
    EVENT_SENSOR_ERROR,
    OBSTACLE_DETECTION_WAIT_S,
    OBSTACLE_FLOOR_RATIO_MAX,
    OBSTACLE_FRAME_WAIT_S,
    OBSTACLE_METRIC,
    OBSTACLE_METRIC_FLOOR_RATIO,
    OBSTACLE_METRIC_SCORE,
    OBSTACLE_METRICS,
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
    valid_floor_ratio_max,
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

    ``metric`` and ``floor_ratio_max`` choose the depth cue's decision (see the
    module docstring). A guard built here gets them; an injected guard must
    report the same metric in its results, and a result that does not is a
    failure to look (``unavailable``), never a decision by the other metric.
    """

    def __init__(
        self,
        frames: LatestFrame,
        runner: Any = None,
        *,
        guard: Any = None,
        min_safe_depth: float = OBSTACLE_MIN_SAFE_DEPTH,
        person_fraction: float = OBSTACLE_PERSON_FRACTION,
        metric: str = OBSTACLE_METRIC,
        floor_ratio_max: float = OBSTACLE_FLOOR_RATIO_MAX,
        settle_s: float = OBSTACLE_SETTLE_S,
        frame_wait_s: float = OBSTACLE_FRAME_WAIT_S,
        detection_wait_s: float = OBSTACLE_DETECTION_WAIT_S,
        drone_id: str | None = None,
        calibrated: bool = False,
        warm: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if metric not in OBSTACLE_METRICS:
            raise ValueError(f"metric must be one of {OBSTACLE_METRICS}, got {metric!r}")
        if not valid_floor_ratio_max(floor_ratio_max):
            raise ValueError(f"floor_ratio_max must be a number from 0.5 to 1.0, got {floor_ratio_max!r}")
        self._frames = frames
        self._runner = runner
        self.min_safe_depth = float(min_safe_depth)
        self.person_fraction = float(person_fraction)
        self.metric = metric
        self.floor_ratio_max = float(floor_ratio_max)
        self.settle_s = settle_s
        self.frame_wait_s = frame_wait_s
        self.detection_wait_s = detection_wait_s
        self.drone_id = drone_id
        self.calibrated = bool(calibrated)
        self._clock = clock
        self._sleep = sleep

        if guard is None:
            from flightrisk.drone.obstacle import ObstacleGuard

            # The default builds the guard exactly as before the metric option existed.
            opt_in = {} if metric == OBSTACLE_METRIC_SCORE else {
                "metric": metric, "floor_ratio_max": self.floor_ratio_max,
            }
            guard = ObstacleGuard(
                min_safe_depth=self.min_safe_depth, check_interval=0.0, center_stat="p90", **opt_in,
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
        # The opt-in metric at the latest gate (``status``): its value and the depth state.
        self._last_metric_value: float | None = None
        self._last_depth_state: str | None = None

    # -- protocol -----------------------------------------------------------

    def sense(
        self, after: float, timeout_s: float = OBSTACLE_SENSE_TIMEOUT_S, *, person_wait: bool = False,
    ) -> SenseResult:
        """Judge a view captured at or after ``after + settle_s`` within ``timeout_s`` (see the class docstring).

        The person cue waits for a fresh detection only while the depth cue
        does not block, unless *person_wait* asks for it anyway: the gates of
        the re-look and the sidestep (``lane_avoid``) need a person cue that
        is known on a blocked view too, because a sidestep is allowed only
        when it is ``clear``. The wait is the same ``detection_wait_s`` and
        never runs past the call's budget.
        """
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
        metric_detail: dict[str, Any] = {}
        if self.metric == OBSTACLE_METRIC_FLOOR_RATIO:
            try:
                ratio, problem = self._floor_ratio_of(check)
            except Exception as exc:
                return self._failed(REASON_ERROR, latency(), exc)
            if problem is not None:
                return self._failed(REASON_ERROR, latency(), problem)
            # Blocked when either the guard or this sensor's limit says so; no floor reference is blocked.
            depth_blocked = depth_blocked or ratio is None or ratio > self.floor_ratio_max
            if ratio is None:
                depth_state = DEPTH_STATE_NO_FLOOR_REFERENCE
            else:
                depth_state = DEPTH_STATE_BLOCKED if depth_blocked else DEPTH_STATE_CLEAR
            metric_detail = {
                "metric": self.metric,
                "floor_ratio": ratio,
                "floor_ratio_max": self.floor_ratio_max,
                "depth_state": depth_state,
                "score": depth_score,
            }
            # The statistic compared with the limit (``SenseResult.depth_score``) is the ratio.
            depth_score = ratio

        person = self._person_cue(after, wait=person_wait or not depth_blocked, budget_end=budget_end)
        self._note_person_cue(person)
        detail = {
            "depth_action": check.get("action"),
            "depth_confidence": check.get("confidence"),
            "center_stat": check.get("center_stat"),
            "min_safe_depth": self.min_safe_depth,
            **metric_detail,
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
        if metric_detail:
            self._log_metric(result, metric_detail)
        return self._finish(result, check, action)

    @staticmethod
    def _floor_ratio_of(check: dict) -> tuple[float | None, str | None]:
        """``(ratio, problem)``: the guard's floor ratio, and why the result cannot be used (None when it can).

        A result that was not judged with the floor ratio (a guard built for the
        other metric) and a ratio that is not a finite number are failures to
        look. A ratio of None is a usable answer: the view has no floor reference.
        """
        if check.get("metric") != OBSTACLE_METRIC_FLOOR_RATIO:
            return None, f"the depth guard did not judge with the {OBSTACLE_METRIC_FLOOR_RATIO} metric"
        ratio = check.get("floor_ratio")
        if ratio is None:
            return None, None
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio):
            return None, f"floor_ratio is not a finite number: {ratio!r}"
        return float(ratio), None

    def _log_metric(self, result: SenseResult, metric_detail: dict[str, Any]) -> None:
        """Log the opt-in metric of one gate (``EVENT_OBSTACLE_METRIC``) and record its histogram.

        ``_finish`` keeps the value for ``status``. ``device`` is the depth
        model's compute device: the same frame can give a slightly different
        map on another device, which matters for a limit this close to the data.
        """
        ratio = metric_detail["floor_ratio"]
        # The comparison is spelled out, so a value shown equal to the limit at four decimals is unambiguous.
        comparison = None if ratio is None else ("<=" if ratio <= self.floor_ratio_max else ">")
        shown = "no floor reference" if ratio is None else f"{ratio:.4f} {comparison} {self.floor_ratio_max:g}"
        log_event(
            logger, logging.INFO, EVENT_OBSTACLE_METRIC,
            f"{self.metric} {shown}: {metric_detail['depth_state']}",
            component="edge", drone_id=self.drone_id,
            metric=self.metric, value=ratio, comparison=comparison, limit=self.floor_ratio_max,
            depth_state=metric_detail["depth_state"], state=result.state, score=metric_detail["score"],
            device=getattr(self._guard, "device_name", None),
        )
        obs_events.obstacle_floor_ratio(ratio, depth_state=metric_detail["depth_state"])

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
        ``person_cue_down`` is true while the person cue has been ``unknown``
        for ``OBSTACLE_PERSON_CUE_DOWN_AFTER`` gates in a row, and
        ``person_detection_age_s`` is how old the view of the newest detection
        result is (seconds since its frame was captured, or obtained when the
        decoder cannot tell), or None before the first result. The lane
        executor puts both in telemetry (``obstacle_person_cue``).
        ``stale_frames`` counts frames refused because their decoder marked
        them stale, and ``settle_s`` is the settle time in force.

        Under the opt-in ``floor_ratio`` metric it also has ``depth_metric``,
        ``depth_metric_value`` (the ratio at the latest gate, None when that
        gate had no floor reference or could not look), ``depth_metric_max``
        and ``depth_state``, which the executor adds to ``obstacle_person_cue``.
        Under the default score none of them is present.
        """
        with self._lock:
            state = self._last_state
            latency = self._last_latency_ms
            reason = self._last_reason
            metric_value = self._last_metric_value
            depth_state = self._last_depth_state
        status = {
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
            "person_detection_age_s": self.person_detection_age_s(),
            "stale_frames": getattr(self._frames, "stale_frames", 0),
            "settle_s": self.settle_s,
            "reason": reason,
            TELEMETRY_STATE: state,
            TELEMETRY_LATENCY_MS: latency,
            TELEMETRY_SENSOR_OK: state != OBSTACLE_STATE_UNAVAILABLE,
        }
        if self.metric != OBSTACLE_METRIC_SCORE:
            status.update({
                DEPTH_METRIC_KEY: self.metric,
                DEPTH_METRIC_VALUE_KEY: metric_value,
                DEPTH_METRIC_MAX_KEY: self.floor_ratio_max,
                DEPTH_STATE_KEY: depth_state,
            })
        return status

    def person_detection_age_s(self) -> float | None:
        """Seconds since the frame of the runner's newest detection result was captured, or None.

        None without a runner or before its first result. Never negative.
        """
        at = getattr(self._runner, "last_detections_at", None) if self._runner is not None else None
        if at is None:
            return None
        try:
            age = self._clock() - float(at)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(age):
            return None
        return round(max(0.0, age), 2)

    def non_default_thresholds(self) -> dict[str, float]:
        """The thresholds that differ from their defaults, by name."""
        changed: dict[str, float] = {}
        if self.min_safe_depth != OBSTACLE_MIN_SAFE_DEPTH:
            changed["min_safe_depth"] = self.min_safe_depth
        if self.person_fraction != OBSTACLE_PERSON_FRACTION:
            changed["person_fraction"] = self.person_fraction
        if self.floor_ratio_max != OBSTACLE_FLOOR_RATIO_MAX:
            changed["floor_ratio_max"] = self.floor_ratio_max
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
            if self.metric != OBSTACLE_METRIC_SCORE:
                # A gate that did not judge a view (stale, timeout, error) leaves no metric value.
                judged = DEPTH_STATE_KEY in result.detail
                self._last_metric_value = result.detail.get("floor_ratio") if judged else None
                self._last_depth_state = result.detail.get(DEPTH_STATE_KEY) if judged else None
        # The score-to-threshold histogram is recorded only while the score decides.
        obs_events.obstacle_check(
            check, action=action, drone_id=self.drone_id,
            min_safe_depth=self.min_safe_depth if self.metric == OBSTACLE_METRIC_SCORE else None,
        )
        return result
