"""The obstacle sensing contract of the fleet lane path.

Every other part of the per-lane obstacle sensing (the step loop, the camera
sensor, the ground, the MAVLink drone) imports its names, constants and types
from this module, so a string such as a cut reason or a telemetry field is
spelled in exactly one place. The design is ``.claude/build/obstacle/design.md``
(sections 3, 4 and 6).

The module holds:

* ``ObstacleSensor``, the protocol the executor calls before every step, and
  ``SenseResult``, what one call returns.
* ``NullSensor`` (always clear, used when sensing is switched off) and
  ``FakeObstacleSensor`` (scripted by the dead-reckoned pose and heading, for
  deterministic tests and the fake fleet demo).
* ``LatestFrame``, the holder of the newest camera frame with its arrival
  time, so a sensor can insist on a frame that arrived after the drone settled.
* Constants for every knob, cut reason, telemetry field and event name.

A sensor is evaluated while the drone hovers, so a slow sensor costs mission
time and never stopping distance. A result is ``clear`` only when the sensor
actually looked at a fresh view and saw nothing; every failure to look is
``unavailable``, and the executor never moves after a result that is not
``clear``.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Knobs (design section 3.2 and section 4)
# ---------------------------------------------------------------------------

#: Length of one sensed step in centimetres (the room profile default).
OBSTACLE_STEP_CM = 50
#: Allowed step range for the room profile (the Tello).
OBSTACLE_STEP_MIN_CM = 20
OBSTACLE_STEP_MAX_CM = 100
#: The field profile (MAVLink) steps as far as its reposition granularity.
OBSTACLE_STEP_FIELD_CM = 500
#: Seconds to wait after a move's reply before a frame counts as settled.
OBSTACLE_SETTLE_S = 0.3
#: How long ``LatestFrame.wait_for`` waits for a frame newer than ``after + settle``,
#: counted from ``after + settle`` (or from the call, when that is later).
OBSTACLE_FRAME_WAIT_S = 0.7
#: Longest a single ``sense`` call may take in all (frame wait, inference and
#: person wait together) before it counts as a timeout.
OBSTACLE_SENSE_TIMEOUT_S = 2.0
#: Attempts at the gate before a stale or failing sensor cuts the lane.
OBSTACLE_SENSE_RETRIES = 3
#: A sensor unusable for this long with lanes remaining ends the lanes early.
OBSTACLE_SENSOR_DOWN_S = 10.0
#: A person box taller than this fraction of the frame height is a close person.
OBSTACLE_PERSON_FRACTION = 0.55
#: Allowed ``FLIGHTRISK_OBSTACLE_PERSON_FRACTION``. A box can never be taller
#: than the frame, so a fraction near 1.0 would switch the cue off without a
#: word; at 0.9 a standing adult still fires at about 1.8 m and a crouching one
#: at about 1.1 m, beyond the 100 cm stop margin (design sections 2.4, 3.2).
OBSTACLE_PERSON_FRACTION_RANGE = (0.2, 0.9)
#: The person box must be centred inside the middle fraction of the frame width.
OBSTACLE_PERSON_CENTRAL_FRACTION = 1.0 / 3.0
#: The depth guard's threshold for "the centre is close" (``ObstacleGuard``).
OBSTACLE_MIN_SAFE_DEPTH = 0.35
#: Allowed ``FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH``. The guard divides the map by
#: its 95th percentile, so the closest thing in view scores about 1.0: a
#: threshold at or above that would never block, and one at 0 divides by zero
#: in the guard's confidence.
OBSTACLE_MIN_SAFE_DEPTH_RANGE = (0.05, 0.9)
#: How long the person cue waits for a detection result newer than ``after + settle``.
OBSTACLE_DETECTION_WAIT_S = 1.0
#: This many ``unknown`` person cues in a row log ``EVENT_PERSON_CUE_DOWN`` once.
OBSTACLE_PERSON_CUE_DOWN_AFTER = 5

# ---------------------------------------------------------------------------
# Environment variables read by ``edge_device`` (all ``FLIGHTRISK_`` prefixed)
# ---------------------------------------------------------------------------

ENV_OBSTACLE_SENSING = "FLIGHTRISK_OBSTACLE_SENSING"
ENV_OBSTACLE_STEP_CM = "FLIGHTRISK_OBSTACLE_STEP_CM"
ENV_OBSTACLE_TURN = "FLIGHTRISK_OBSTACLE_TURN"
ENV_OBSTACLE_MIN_SAFE_DEPTH = "FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH"
ENV_OBSTACLE_PERSON_FRACTION = "FLIGHTRISK_OBSTACLE_PERSON_FRACTION"
#: Fake drone only: the plan-frame y (cm) at which the fake sensor reports blocked.
ENV_FAKE_OBSTACLE_Y_CM = "FLIGHTRISK_FAKE_OBSTACLE_Y_CM"

#: Values of ``FLIGHTRISK_OBSTACLE_SENSING``.
SENSING_ENV_REQUIRED = "required"
SENSING_ENV_OFF = "off"

# ---------------------------------------------------------------------------
# Sense results
# ---------------------------------------------------------------------------

#: ``SenseResult.state`` values.
STATE_CLEAR = "clear"
STATE_BLOCKED = "blocked"
#: The sensor could not look (stale frame, timeout, error). Never clears a step.
STATE_UNAVAILABLE = "unavailable"
SENSE_STATES = frozenset({STATE_CLEAR, STATE_BLOCKED, STATE_UNAVAILABLE})

#: ``SenseResult.cue``: which cue decided the result.
CUE_DEPTH = "depth"
CUE_PERSON = "person"
CUE_AUTOPILOT = "autopilot"
CUE_FAKE = "fake"
CUE_NONE = "none"

#: ``SenseResult.person_cue`` values. ``unknown`` never blocks and never clears.
PERSON_CUE_CLEAR = "clear"
PERSON_CUE_BLOCKED = "blocked"
PERSON_CUE_UNKNOWN = "unknown"

#: ``SenseResult.reason`` of an ``unavailable`` result.
REASON_STALE = "stale"
REASON_TIMEOUT = "timeout"
REASON_ERROR = "error"

#: ``flightrisk_obstacle_checks`` actions added by the fleet path (the existing
#: ones are written by ``obs_events.obstacle_check``).
ACTION_PERSON = "person"
ACTION_STALE = "stale"
ACTION_TIMEOUT = "timeout"
ACTION_PERSON_UNKNOWN = "person_unknown"

# ---------------------------------------------------------------------------
# Cut reasons and uncovered-segment reasons (design sections 3.3, 3.4, 6)
# ---------------------------------------------------------------------------

#: Why a lane was cut (``obstacle_cut_reasons`` in telemetry).
CUT_OBSTACLE = "obstacle"
CUT_PERSON = "person"
CUT_SENSOR_STALE = "sensor_stale"
CUT_SENSOR_DOWN = "sensor_down"
CUT_AUTOPILOT = "autopilot"
CUT_REASONS = (CUT_OBSTACLE, CUT_PERSON, CUT_SENSOR_STALE, CUT_SENSOR_DOWN, CUT_AUTOPILOT)
#: Cut reasons the ground offers once to another drone (design section 3.4).
REFLIGHT_CUT_REASONS = frozenset({CUT_OBSTACLE, CUT_PERSON, CUT_SENSOR_STALE})

#: Segment reasons that are not cut reasons.
SEGMENT_SKIPPED = "skipped"
SEGMENT_TOO_SHORT = "too_short"

# ---------------------------------------------------------------------------
# Sensing modes and states in telemetry (design section 6)
# ---------------------------------------------------------------------------

SENSING_STEPPED = "stepped"
SENSING_FORWARD_ONLY = "forward_only"
SENSING_AUTOPILOT = "autopilot"
SENSING_OFF = "off"
SENSING_MODES = (SENSING_STEPPED, SENSING_FORWARD_ONLY, SENSING_AUTOPILOT, SENSING_OFF)

#: ``obstacle_state`` in telemetry.
OBSTACLE_STATE_CLEAR = "clear"
OBSTACLE_STATE_BLOCKED = "blocked"
OBSTACLE_STATE_UNAVAILABLE = "unavailable"
OBSTACLE_STATES = (OBSTACLE_STATE_CLEAR, OBSTACLE_STATE_BLOCKED, OBSTACLE_STATE_UNAVAILABLE)

# Telemetry field names, edge to ground. All optional and sticky for the plan.
TELEMETRY_SENSING = "obstacle_sensing"
TELEMETRY_SENSOR_OK = "obstacle_sensor_ok"
TELEMETRY_STATE = "obstacle_state"
TELEMETRY_LATENCY_MS = "obstacle_latency_ms"
TELEMETRY_SEQ = "obstacle_seq"
#: A flat list ``[lane, flown_cm, lane, flown_cm, ...]`` of at most
#: ``MAX_CUTS_LISTED`` pairs (16 numbers, the privacy screen's list limit).
TELEMETRY_CUTS = "obstacle_cuts"
TELEMETRY_CUTS_TOTAL = "obstacle_cuts_total"
#: Comma separated reasons of the listed cuts, at most 200 characters.
TELEMETRY_CUT_REASONS = "obstacle_cut_reasons"
TELEMETRY_FIELDS = (
    TELEMETRY_SENSING,
    TELEMETRY_SENSOR_OK,
    TELEMETRY_STATE,
    TELEMETRY_LATENCY_MS,
    TELEMETRY_SEQ,
    TELEMETRY_CUTS,
    TELEMETRY_CUTS_TOTAL,
    TELEMETRY_CUT_REASONS,
)
#: The executor keeps and reports this many of the latest cuts.
MAX_CUTS_LISTED = 8
#: Longest ``obstacle_cut_reasons`` string.
MAX_CUT_REASONS_CHARS = 200

# ---------------------------------------------------------------------------
# Edge log events and the nack detail (design sections 3.3, 4)
# ---------------------------------------------------------------------------

EVENT_OBSTACLE_STOP = "edge_obstacle_stop"
EVENT_LANE_CUT = "edge_lane_cut"
EVENT_OBSTACLE_GATE = "edge_obstacle_gate"
EVENT_SENSE_STALE = "edge_obstacle_sense_stale"
EVENT_SENSE_TIMEOUT = "edge_obstacle_sense_timeout"
EVENT_SENSOR_ERROR = "edge_obstacle_sensor_error"
EVENT_LANES_ENDED_SENSOR_DOWN = "edge_lanes_ended_sensor_down"
EVENT_PERSON_CUE_DOWN = "edge_obstacle_person_cue_down"
EVENT_OBSTACLE_TURN = "edge_obstacle_turn"
EVENT_START_REFUSED = "edge_start_refused"
#: Logged once at startup, at warning level, for every obstacle setting that
#: is not its default (``variable``, ``value``, ``default``).
EVENT_SETTING_NON_DEFAULT = "edge_obstacle_setting_non_default"
#: ``reason`` of ``edge_start_refused`` when the sensor is missing.
START_REFUSED_REASON = "obstacle_sensor"
#: Prefix of the ``drone_unreachable`` nack detail: ``"<prefix> <error>"``.
SENSOR_UNAVAILABLE_DETAIL = "obstacle sensor unavailable:"


# ---------------------------------------------------------------------------
# The protocol and its result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SenseResult:
    """What one ``ObstacleSensor.sense`` call found.

    ``state`` is one of ``SENSE_STATES``. ``cue`` names the cue that decided a
    ``blocked`` result. ``reason`` explains an ``unavailable`` result (one of
    ``REASON_*``). ``person_cue`` is the secondary cue's own answer, and
    ``depth_score`` the depth statistic that was compared with the threshold.
    ``detail`` carries extra numbers for logs and must stay JSON friendly.
    """

    state: str
    cue: str = CUE_NONE
    reason: str = ""
    latency_ms: float = 0.0
    person_cue: str = PERSON_CUE_UNKNOWN
    depth_score: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in SENSE_STATES:
            raise ValueError(f"unknown sense state: {self.state!r}")

    @property
    def clear(self) -> bool:
        return self.state == STATE_CLEAR

    @property
    def blocked(self) -> bool:
        return self.state == STATE_BLOCKED

    @property
    def unavailable(self) -> bool:
        return self.state == STATE_UNAVAILABLE

    @property
    def cut_reason(self) -> str:
        """The cut reason this result gives when it stops a lane."""
        if self.blocked:
            return CUT_PERSON if self.cue == CUE_PERSON else (
                CUT_AUTOPILOT if self.cue == CUE_AUTOPILOT else CUT_OBSTACLE
            )
        return CUT_SENSOR_STALE

    @classmethod
    def make_clear(cls, **kw: Any) -> SenseResult:
        return cls(STATE_CLEAR, **kw)

    @classmethod
    def make_blocked(cls, cue: str = CUE_DEPTH, **kw: Any) -> SenseResult:
        return cls(STATE_BLOCKED, cue=cue, **kw)

    @classmethod
    def make_unavailable(cls, reason: str = REASON_STALE, **kw: Any) -> SenseResult:
        return cls(STATE_UNAVAILABLE, reason=reason, **kw)


@runtime_checkable
class ObstacleSensor(Protocol):
    """Looks at the space ahead of a hovering drone.

    ``sense(after, timeout_s)`` judges a view that arrived after ``after``
    (a ``time.monotonic()`` stamp, normally the end of the last move) plus the
    settle time. It returns a ``SenseResult`` and never moves the drone.

    A sensor reports a missing fresh frame, a timeout or its own failure as an
    ``unavailable`` result. It may also raise; the executor treats an exception
    like ``unavailable`` with ``REASON_ERROR``. ``status()`` returns a small
    JSON friendly dict for telemetry and logs (for example ``calibrated``).
    """

    def sense(self, after: float, timeout_s: float = OBSTACLE_SENSE_TIMEOUT_S) -> SenseResult: ...

    def status(self) -> dict[str, Any]: ...


class NullSensor:
    """A sensor that is switched off: every view is clear and nothing is looked at.

    Used for the explicit ``FLIGHTRISK_OBSTACLE_SENSING=off`` override. The
    result carries ``CUE_NONE`` so logs can tell it from a real clear.
    """

    def sense(self, after: float, timeout_s: float = OBSTACLE_SENSE_TIMEOUT_S) -> SenseResult:
        return SenseResult.make_clear(cue=CUE_NONE)

    def status(self) -> dict[str, Any]:
        return {"sensor": "null", "mode": SENSING_OFF, "calibrated": False}


# ---------------------------------------------------------------------------
# The scripted sensor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockedZone:
    """A region of the plan frame that the fake sensor reports as blocked.

    The zone matches when the pose lies in the closed box ``x_min..x_max`` by
    ``y_min..y_max`` (a bound of ``None`` is open) and, if ``heading_deg`` is
    given, the pose heading is within ``heading_tol_deg`` of it (modulo 360).
    """

    heading_deg: float | None = 0.0
    x_min: float | None = None
    x_max: float | None = None
    y_min: float | None = None
    y_max: float | None = None
    heading_tol_deg: float = 1.0
    cue: str = CUE_DEPTH

    def matches(self, x_cm: float, y_cm: float, heading_deg: float) -> bool:
        if self.heading_deg is not None:
            diff = abs((heading_deg - self.heading_deg + 180.0) % 360.0 - 180.0)
            if diff > self.heading_tol_deg:
                return False
        if self.x_min is not None and x_cm < self.x_min:
            return False
        if self.x_max is not None and x_cm > self.x_max:
            return False
        if self.y_min is not None and y_cm < self.y_min:
            return False
        if self.y_max is not None and y_cm > self.y_max:
            return False
        return True


@dataclass(frozen=True)
class SenseCall:
    """One entry of ``FakeObstacleSensor.calls``."""

    after: float
    timeout_s: float
    x_cm: float
    y_cm: float
    heading_deg: float
    outcome: str  # "scripted", "stale", "timeout", "error" or a SenseResult.state
    result: SenseResult | None


#: Names accepted by ``FakeObstacleSensor.queue`` for a one-call override.
OVERRIDE_STALE = "stale"
OVERRIDE_TIMEOUT = "timeout"


class FakeObstacleSensor:
    """A deterministic sensor scripted by the dead-reckoned pose and heading.

    ``pose_provider`` returns an object with ``x_cm``, ``y_cm`` and
    ``heading_deg`` (a ``RelativePose``). ``blocked`` lists ``BlockedZone``
    entries; the first one matching the current pose blocks the view.

    ``queue(*overrides)`` scripts the next calls, one entry per call, ahead of
    the zones: ``"stale"`` and ``"timeout"`` return an ``unavailable`` result
    with that reason, an exception instance is raised, a ``SenseResult`` is
    returned as given, and ``None`` falls through to the zones. Every call is
    appended to ``calls`` with its ``after`` stamp, so a test can assert what
    the executor asked for.
    """

    def __init__(
        self,
        pose_provider: Callable[[], Any],
        blocked: Iterable[BlockedZone] = (),
        latency_ms: float = 1.0,
    ):
        self._pose = pose_provider
        self.blocked: list[BlockedZone] = list(blocked)
        self.latency_ms = latency_ms
        self.calls: list[SenseCall] = []
        self._script: list[Any] = []
        self._lock = threading.Lock()

    def queue(self, *overrides: Any) -> None:
        with self._lock:
            self._script.extend(overrides)

    def sense(self, after: float, timeout_s: float = OBSTACLE_SENSE_TIMEOUT_S) -> SenseResult:
        pose = self._pose()
        x, y, heading = float(pose.x_cm), float(pose.y_cm), float(pose.heading_deg)
        with self._lock:
            override = self._script.pop(0) if self._script else None

        def record(outcome: str, result: SenseResult | None) -> None:
            with self._lock:
                self.calls.append(SenseCall(after, timeout_s, x, y, heading, outcome, result))

        if isinstance(override, BaseException):
            record("error", None)
            raise override
        if override == OVERRIDE_STALE or override == OVERRIDE_TIMEOUT:
            result = SenseResult.make_unavailable(
                REASON_STALE if override == OVERRIDE_STALE else REASON_TIMEOUT,
                latency_ms=self.latency_ms,
            )
            record(str(override), result)
            return result
        if isinstance(override, SenseResult):
            record("scripted", override)
            return override
        if override is not None:
            raise ValueError(f"unknown fake sensor override: {override!r}")
        for zone in self.blocked:
            if zone.matches(x, y, heading):
                result = SenseResult.make_blocked(
                    zone.cue, latency_ms=self.latency_ms,
                    detail={"x_cm": x, "y_cm": y, "heading_deg": heading},
                )
                record(STATE_BLOCKED, result)
                return result
        result = SenseResult.make_clear(cue=CUE_FAKE, latency_ms=self.latency_ms)
        record(STATE_CLEAR, result)
        return result

    def status(self) -> dict[str, Any]:
        return {"sensor": "fake", "mode": SENSING_STEPPED, "calibrated": False, "calls": len(self.calls)}


# ---------------------------------------------------------------------------
# The frame holder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimedFrame:
    """A camera frame with its arrival time on the holder's clock."""

    frame: Any
    arrived_at: float


class LatestFrame:
    """Holds the newest camera frame and when it arrived.

    Register the holder itself as the drone's frame callback
    (``drone.on_frame(holder)``): it is fed for every frame the controller
    returns, independent of any frame stride the detector uses. ``clock`` is
    the monotonic clock that stamps arrivals (``time.monotonic`` by default);
    the executor's ``after`` stamps must come from the same clock.

    ``wait_for`` waits in real time on a condition but measures the deadline on
    ``clock``. A test that injects a clock which never advances must use
    ``wait_s=0`` or have a producer thread advance it.
    """

    #: Longest real-time sleep between clock checks while waiting.
    _POLL_S = 0.05

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._cond = threading.Condition()
        self._latest: TimedFrame | None = None
        self._count = 0

    def __call__(self, frame: Any) -> None:
        self.update(frame)

    def update(self, frame: Any) -> None:
        """Store ``frame`` as the newest, stamped with the current time."""
        with self._cond:
            self._latest = TimedFrame(frame, self._clock())
            self._count += 1
            self._cond.notify_all()

    @property
    def frames_seen(self) -> int:
        with self._cond:
            return self._count

    def latest(self) -> TimedFrame | None:
        """The newest frame whatever its age, or ``None`` before the first."""
        with self._cond:
            return self._latest

    def wait_for(
        self,
        after: float,
        settle_s: float = OBSTACLE_SETTLE_S,
        wait_s: float = OBSTACLE_FRAME_WAIT_S,
        limit_s: float | None = None,
    ) -> TimedFrame | None:
        """Return the newest frame that arrived at or after ``after + settle_s``.

        Waits until ``wait_s`` past the later of now and ``after + settle_s``,
        so a call made before the settle time is over still gets the whole
        ``wait_s`` once a frame can count, and returns ``None`` when no such
        frame came. ``limit_s``, when given, caps the wait counted from now
        (the caller's remaining time budget). A ``wait_s`` or ``limit_s`` of 0
        looks once and does not wait.
        """
        threshold = after + settle_s
        now = self._clock()
        deadline = max(now, threshold) + wait_s if wait_s > 0 else now
        if limit_s is not None:
            deadline = min(deadline, now + max(0.0, limit_s))
        with self._cond:
            while True:
                latest = self._latest
                if latest is not None and latest.arrived_at >= threshold:
                    return latest
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, self._POLL_S))
