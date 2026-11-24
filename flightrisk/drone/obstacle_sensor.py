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
  and capture times, so a sensor can insist on a frame captured after the
  drone settled. It never stamps the same source frame object twice, so a
  stalled video decoder cannot pass an old frame off as a new one, and it
  refuses a frame its decoder marks stale (``FrameStamp``).
* ``to_sensor_frame``, the one place a frame is converted to the channel
  order the sensor judges (``SENSOR_CHANNEL_ORDER``).
* Constants for every knob, cut reason, telemetry field and event name.

A sensor is evaluated while the drone hovers, so a slow sensor costs mission
time and never stopping distance. A result is ``clear`` only when the sensor
actually looked at a fresh view and saw nothing; every failure to look is
``unavailable``, and the executor never moves after a result that is not
``clear``.
"""
from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from flightrisk.params import table as _registry
from flightrisk.params.spec import Bound, ParamSpec

# ---------------------------------------------------------------------------
# Knobs (design section 3.2 and section 4)
# ---------------------------------------------------------------------------
#
# Each knob that duplicates a parameter registry row reads that row
# (``flightrisk.params.table``), so the module keeps no copy that only happens
# to match. Today's indoor values are the indoor reference profile column; the
# field step is the generic multirotor column. The allowed ranges stay here
# until C5 replaces them with the registry bounds.


def _registry_row(param_id: str) -> ParamSpec:
    """The registry row ``param_id``."""
    for spec in _registry.TABLE:
        if spec.id == param_id:
            return spec
    raise KeyError(f"no registry row {param_id!r}")


def _registry_default(param_id: str, profile: str | None = None) -> Any:
    """The registry default of ``param_id`` in ``profile``, the indoor reference column when none is given."""
    return _registry_row(param_id).defaults[_registry.T if profile is None else profile]


def _registry_bound(param_id: str, profile: str | None = None) -> Bound:
    """The registry bound of ``param_id`` in ``profile``, the indoor reference column when none is given."""
    column = _registry.T if profile is None else profile
    bound = _registry_row(param_id).bounds[column]
    if bound is None:
        raise LookupError(f"registry row {param_id!r} has no bound in {column!r}")
    return bound


#: Length of one sensed step in centimetres (the room profile default).
OBSTACLE_STEP_CM = _registry_default("sensing.step_cm")
#: Allowed step range for the room profile (the Tello).
OBSTACLE_STEP_MIN_CM = _registry_bound("sensing.step_cm").lo
OBSTACLE_STEP_MAX_CM = _registry_bound("sensing.step_cm").hi
#: The field profile (MAVLink) steps as far as its reposition granularity
#: (the generic multirotor column of the row).
OBSTACLE_STEP_FIELD_CM = _registry_default("sensing.step_cm", _registry.G)
#: Seconds after a move's reply before a frame counts as settled: a view must have
#: been captured at least this long after the end of the last move or turn. The
#: default; ``FLIGHTRISK_OBSTACLE_SETTLE_S`` changes it. It must cover the camera's
#: own latency (capture to arrival at the edge), so it must not be below the p95
#: video latency that hardware item H5 measures (``docs/obstacle-sensing.md``).
OBSTACLE_SETTLE_S = _registry_default("sensing.settle_s")
#: Allowed ``FLIGHTRISK_OBSTACLE_SETTLE_S``.
OBSTACLE_SETTLE_RANGE_S = (0.1, 3.0)
#: How long ``LatestFrame.wait_for`` waits for a frame newer than ``after + settle``,
#: counted from ``after + settle`` (or from the call, when that is later).
OBSTACLE_FRAME_WAIT_S = _registry_default("sensing.view_wait_s")
#: Longest a single ``sense`` call may take in all (frame wait, inference and
#: person wait together) before it counts as a timeout.
OBSTACLE_SENSE_TIMEOUT_S = _registry_default("sensing.sense_timeout_s")
#: Attempts at the gate before a stale or failing sensor cuts the lane.
OBSTACLE_SENSE_RETRIES = _registry_default("sensing.sense_retries")
#: A sensor unusable for this long with lanes remaining ends the lanes early.
OBSTACLE_SENSOR_DOWN_S = _registry_default("sensing.sensor_down_s")
#: A person box taller than this fraction of the frame height is a close person.
OBSTACLE_PERSON_FRACTION = _registry_default("sensing.person_fraction")
#: Allowed ``FLIGHTRISK_OBSTACLE_PERSON_FRACTION``. A box can never be taller
#: than the frame, so a fraction near 1.0 would switch the cue off without a
#: word; at 0.9 a standing adult still fires at about 1.8 m and a crouching one
#: at about 1.1 m, beyond the 100 cm stop margin (design sections 2.4, 3.2).
OBSTACLE_PERSON_FRACTION_RANGE = (0.2, 0.9)
#: The person box must be centred inside the middle fraction of the frame width.
OBSTACLE_PERSON_CENTRAL_FRACTION = 1.0 / 3.0
#: The depth guard's threshold for "the centre is close" (``ObstacleGuard``).
OBSTACLE_MIN_SAFE_DEPTH = _registry_default("sensing.min_safe_depth")
#: Allowed ``FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH``. The guard divides the map by
#: its 95th percentile, so the closest thing in view scores about 1.0: a
#: threshold at or above that would never block, and one at 0 divides by zero
#: in the guard's confidence.
OBSTACLE_MIN_SAFE_DEPTH_RANGE = (0.05, 0.9)
#: How long the person cue waits for a detection result newer than ``after + settle``.
OBSTACLE_DETECTION_WAIT_S = _registry_default("sensing.person_wait_s")
#: This many ``unknown`` person cues in a row log ``EVENT_PERSON_CUE_DOWN`` once.
OBSTACLE_PERSON_CUE_DOWN_AFTER = 5
#: A frame that the bound Tello decoder (``tello_bound.BoundFrameRead``) decodes more
#: than this many seconds after the first datagram of its video data arrived is
#: stale (``STALE_DECODER_BACKLOG``): the decoder is behind the video. The default;
#: ``FLIGHTRISK_MAX_DECODE_LAG_S`` changes it.
MAX_DECODE_LAG_S = _registry_default("sensing.max_decode_lag_s")
#: Allowed ``FLIGHTRISK_MAX_DECODE_LAG_S``.
MAX_DECODE_LAG_RANGE_S = (0.1, 2.0)

# Avoidance at a blocked gate (``flightrisk.drone.lane_avoid``) and clear votes.
# The edge's defaults are below; ``LaneExecutor`` itself defaults to the
# behaviour before them (one clear view clears a step, no re-look, no
# sidestep), so an executor built without these settings flies as it did.

#: Consecutive clear views a gate needs before a step is flown, each judged
#: on its own frame captured after the previous view. The edge's default;
#: ``FLIGHTRISK_OBSTACLE_CLEAR_VOTES`` changes it, and 1 is the behaviour
#: before votes existed (one clear view clears a step).
OBSTACLE_CLEAR_VOTES = _registry_default("avoid.clear_votes")
#: Allowed ``FLIGHTRISK_OBSTACLE_CLEAR_VOTES``.
OBSTACLE_CLEAR_VOTES_RANGE = (1, 5)
#: Extra gates after a gate that saw an obstacle, each after a short hover;
#: the step is flown only if every one of them is clear. The edge's default;
#: ``FLIGHTRISK_OBSTACLE_RELOOK`` changes it, and 0 switches the re-look off.
OBSTACLE_RELOOK = _registry_default("avoid.relook")
#: Allowed ``FLIGHTRISK_OBSTACLE_RELOOK``.
OBSTACLE_RELOOK_RANGE = (0, 5)
#: Sideways distance in centimetres of a sidestep around an obstacle on the
#: lane. 0, the edge's default, means the sidestep is off (Tello obstacle
#: tuning is tabled, and with an uncalibrated depth threshold every gate reads
#: blocked). ``FLIGHTRISK_OBSTACLE_SIDESTEP_CM`` switches it on: any other
#: value must be a move the drone can fly (at least ``OBSTACLE_STEP_MIN_CM``).
OBSTACLE_SIDESTEP_CM = _registry_default("avoid.sidestep_cm")
#: Allowed ``FLIGHTRISK_OBSTACLE_SIDESTEP_CM`` besides 0.
OBSTACLE_SIDESTEP_RANGE_CM = (OBSTACLE_STEP_MIN_CM, OBSTACLE_STEP_MAX_CM)
#: Sidestep attempts allowed on one lane. The edge's default;
#: ``FLIGHTRISK_OBSTACLE_SIDESTEPS`` changes it, and 0 switches the sidestep off.
OBSTACLE_SIDESTEPS = _registry_default("avoid.sidesteps")
#: Allowed ``FLIGHTRISK_OBSTACLE_SIDESTEPS``.
OBSTACLE_SIDESTEPS_RANGE = (0, 4)
#: How far along the lane a sidestep flies on the offset line before it goes
#: back to the lane: the range within which the depth cue is expected to see
#: an obstacle (the calibration target of hardware item H1).
OBSTACLE_SIDESTEP_PASS_CM = _registry_default("avoid.sidestep_pass_cm")
#: Wall-clock seconds the re-look and the sidestep may spend on one blocked
#: gate. Once they are spent, no further re-look round, sidestep attempt or
#: step beside the lane is started, and the lane is cut as it would be without
#: them (a drone beside the lane flies back to it first). The edge's default;
#: ``FLIGHTRISK_OBSTACLE_AVOID_BUDGET_S`` changes it.
#: It has no registry row yet, so it stays here; C1, which owns avoidance, decides its row.
OBSTACLE_AVOID_BUDGET_S = 45.0
#: Allowed ``FLIGHTRISK_OBSTACLE_AVOID_BUDGET_S``.
OBSTACLE_AVOID_BUDGET_RANGE_S = (10.0, 300.0)
#: Re-look rounds allowed on one lane. A lane that keeps reading blocked is
#: not looked at again for ever: once its rounds are used, a blocked gate on it
#: goes straight to the sidestep (if one is allowed) or is cut.
#: It has no registry row yet, so it stays here; C1, which owns avoidance, decides its row.
OBSTACLE_RELOOKS_PER_LANE = 3
#: The executor reports at most this many of the latest offset segments
#: (steps flown beside the lane) in ``TELEMETRY_AVOID`` (12 numbers).
MAX_OFFSETS_LISTED = 4

# ---------------------------------------------------------------------------
# Environment variables read by ``edge_device`` (all ``FLIGHTRISK_`` prefixed)
# ---------------------------------------------------------------------------

ENV_OBSTACLE_SENSING = "FLIGHTRISK_OBSTACLE_SENSING"
ENV_OBSTACLE_STEP_CM = "FLIGHTRISK_OBSTACLE_STEP_CM"
ENV_OBSTACLE_TURN = "FLIGHTRISK_OBSTACLE_TURN"
ENV_OBSTACLE_MIN_SAFE_DEPTH = "FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH"
ENV_OBSTACLE_PERSON_FRACTION = "FLIGHTRISK_OBSTACLE_PERSON_FRACTION"
ENV_OBSTACLE_SETTLE_S = "FLIGHTRISK_OBSTACLE_SETTLE_S"
#: The bound Tello decoder's stale limit (``MAX_DECODE_LAG_S``); read by
#: ``tello_bound.max_decode_lag_s`` and validated at the edge's start.
ENV_MAX_DECODE_LAG_S = "FLIGHTRISK_MAX_DECODE_LAG_S"
ENV_OBSTACLE_CLEAR_VOTES = "FLIGHTRISK_OBSTACLE_CLEAR_VOTES"
ENV_OBSTACLE_RELOOK = "FLIGHTRISK_OBSTACLE_RELOOK"
ENV_OBSTACLE_SIDESTEP_CM = "FLIGHTRISK_OBSTACLE_SIDESTEP_CM"
ENV_OBSTACLE_SIDESTEPS = "FLIGHTRISK_OBSTACLE_SIDESTEPS"
ENV_OBSTACLE_AVOID_BUDGET_S = "FLIGHTRISK_OBSTACLE_AVOID_BUDGET_S"
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
#: ``SenseResult.detail["cause"]`` of a ``stale`` result whose fresh frame had
#: every pixel the same (for example the black frame a restarted video decoder
#: starts on). Such a frame shows nothing, so there was no view to judge.
CAUSE_BLANK_FRAME = "blank frame"

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
#: Cut reasons the ground offers once to another drone on its own (design section 3.4).
REFLIGHT_CUT_REASONS = frozenset({CUT_OBSTACLE, CUT_SENSOR_STALE})
#: Cut reasons offered once to another drone only after the operator confirms
#: it on the dashboard. A person is not a fixed obstacle: a drone sent to fly
#: the rest of the lane from the far side would fly toward the same person.
CONFIRM_REFLIGHT_CUT_REASONS = frozenset({CUT_PERSON})

#: Segment reasons that are not cut reasons.
SEGMENT_SKIPPED = "skipped"
SEGMENT_TOO_SHORT = "too_short"
#: A stretch of a lane that a sidestep flew beside the lane instead of on it
#: (``lane_avoid``): the drone looked along a line ``obstacle_sidestep_cm``
#: away from the planned one, so the ground offers it for a re-flight like a
#: cut segment (``TELEMETRY_AVOID``'s ``AVOID_OFFSETS_KEY``).
SEGMENT_OFFSET = "offset"

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
#: The person cue's health, for a sensor that has one: an object
#: ``{"person_cue_down": bool, "detection_age_s": number or null}``. One field
#: holds both, to keep the telemetry's field count down
#: (``fleet_protocol.MAX_FIELDS``).
TELEMETRY_PERSON_CUE = "obstacle_person_cue"
#: Keys of ``TELEMETRY_PERSON_CUE``.
PERSON_CUE_DOWN_KEY = "person_cue_down"
PERSON_CUE_AGE_KEY = "detection_age_s"
#: What the re-look and the sidestep at blocked gates did in the current plan,
#: for an edge that has either switched on (``lane_avoid``): an object with
#: the counts ``AVOID_COUNT_KEYS``, ``AVOID_PHASE_KEY`` (the step under way:
#: ``relook``, ``sidestep``, ``return``, ``held`` while the drone hovers under
#: manual control because it could not get back to its lane, or null) and
#: ``AVOID_OFFSETS_KEY`` (with ``AVOID_OFFSETS_SEEN_KEY``), ``AVOID_HELD_REASON_KEY``
#: and ``AVOID_SIDESTEP_CM_KEY``. One field holds all of it, to keep the
#: telemetry's field count down (``fleet_protocol.MAX_FIELDS``). It is sent by an
#: edge that may sidestep, and by one that only looks again once the plan has
#: something to report (a count above 0, a phase or an offset).
TELEMETRY_AVOID = "obstacle_avoid"
#: Counts in ``TELEMETRY_AVOID``: re-look rounds started, re-look rounds that
#: let the step fly, sidestep attempts, and sidesteps that got past.
AVOID_RELOOKS_KEY = "relooks"
AVOID_RELOOK_CLEARS_KEY = "relook_clears"
AVOID_SIDESTEPS_KEY = "sidesteps"
AVOID_SIDESTEP_PASSES_KEY = "sidestep_passes"
AVOID_COUNT_KEYS = (AVOID_RELOOKS_KEY, AVOID_RELOOK_CLEARS_KEY, AVOID_SIDESTEPS_KEY, AVOID_SIDESTEP_PASSES_KEY)
AVOID_PHASE_KEY = "phase"
#: ``AVOID_PHASE_KEY`` values.
AVOID_PHASE_RELOOK = "relook"
AVOID_PHASE_SIDESTEP = "sidestep"
AVOID_PHASE_RETURN = "return"
AVOID_PHASE_HELD = "held"
#: In ``TELEMETRY_AVOID``: the plan's latest offset segments (steps flown
#: beside the lane), oldest first, as a flat list ``[lane, from_cm, length_cm,
#: ...]`` of at most ``MAX_OFFSETS_LISTED`` triples. ``from_cm`` is how far
#: along the lane the segment starts, measured from where the drone started
#: the lane; left out while there is none.
AVOID_OFFSETS_KEY = "offsets"
#: In ``TELEMETRY_AVOID``, next to ``AVOID_OFFSETS_KEY`` and only while one of
#: the listed offsets has one: a list with one string per listed offset, the
#: reason a gate of that sidestep stopped for (``person`` or ``sensor_down``),
#: or ``""``. The ground never offers an offset whose sidestep saw a person
#: unless the operator confirms it, because its re-flight would start at the
#: far end of the offset, where the person was seen.
AVOID_OFFSETS_SEEN_KEY = "offsets_seen"
#: In ``TELEMETRY_AVOID`` while ``AVOID_PHASE_KEY`` is ``held``: why the way
#: back to the lane stopped (``obstacle``, ``person``, ``sensor_stale``,
#: ``sensor_down`` or ``geofence``).
AVOID_HELD_REASON_KEY = "held_reason"
#: In ``TELEMETRY_AVOID``: how far this edge may sidestep (``obstacle_sidestep_cm``),
#: or 0 when it never sidesteps. The ground keeps its sidestep margin
#: (``coordinator.SIDESTEP_MARGIN_CM``) only from the paths of drones that report
#: more than 0.
AVOID_SIDESTEP_CM_KEY = "sidestep_cm"
TELEMETRY_FIELDS = (
    TELEMETRY_SENSING,
    TELEMETRY_SENSOR_OK,
    TELEMETRY_STATE,
    TELEMETRY_LATENCY_MS,
    TELEMETRY_SEQ,
    TELEMETRY_CUTS,
    TELEMETRY_CUTS_TOTAL,
    TELEMETRY_CUT_REASONS,
    TELEMETRY_PERSON_CUE,
    TELEMETRY_AVOID,
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
#: A re-look round at a gate that saw an obstacle (``lane_avoid``), with its ``phase``.
EVENT_OBSTACLE_RELOOK = "edge_obstacle_relook"
#: A sidestep around an obstacle on the lane (``lane_avoid``), with its ``phase``.
EVENT_OBSTACLE_SIDESTEP = "edge_obstacle_sidestep"
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

    A sensor whose ``sense`` also takes the keyword ``person_wait`` is passed
    ``person_wait=True`` by the gates of the re-look and the sidestep
    (``lane_avoid``): it should then wait, within ``timeout_s``, for a fresh
    person detection even when the depth cue already blocks, so that its
    ``person_cue`` is ``clear`` or ``blocked`` rather than ``unknown``. The
    camera sensor does; a sensor without the keyword is called as before.
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
    entries; the first one matching the current pose blocks the view. Its
    simulated person detector always answers: a zone with ``cue="person"``
    reports ``person_cue`` ``blocked``, every other view ``clear``.

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
                    person_cue=PERSON_CUE_BLOCKED if zone.cue == CUE_PERSON else PERSON_CUE_CLEAR,
                    detail={"x_cm": x, "y_cm": y, "heading_deg": heading},
                )
                record(STATE_BLOCKED, result)
                return result
        result = SenseResult.make_clear(cue=CUE_FAKE, latency_ms=self.latency_ms, person_cue=PERSON_CUE_CLEAR)
        record(STATE_CLEAR, result)
        return result

    def status(self) -> dict[str, Any]:
        return {"sensor": "fake", "mode": SENSING_STEPPED, "calibrated": False, "calls": len(self.calls)}


# ---------------------------------------------------------------------------
# Frame stamps and the channel order of the sensor's frames
# ---------------------------------------------------------------------------

#: ``FrameStamp.stale_cause`` values: why a decoded frame is not a usable view.
STALE_DECODER_BACKLOG = "decoder backlog"
STALE_DECODE_ERROR = "decode error"
STALE_CORRUPT_FRAME = "corrupt frame"
STALE_UNTIL_KEY_FRAME = "damaged reference, waiting for a key frame"
STALE_DATA_DROPPED = "video data dropped"
STALE_CAPTURE_UNKNOWN = "capture time unknown"


@dataclass(frozen=True)
class FrameStamp:
    """What a video decoder knows about one decoded frame.

    ``captured_at`` is the latest time the frame can have been captured, on the
    ``time.monotonic()`` clock: the arrival of the first datagram of its video
    data, stamped by a receiver that does not wait for the decoder, so a decoder
    that falls behind cannot make an old view look new. It is ``None`` when the
    decoder cannot tell. ``stale_cause`` is set (one of ``STALE_*``) when the
    frame must not be judged at all: it was decoded behind a backlog, it is
    corrupt or partial after lost video data, or it is built on such a frame.
    """

    captured_at: float | None = None
    stale_cause: str | None = None

    @property
    def stale(self) -> bool:
        return self.stale_cause is not None


#: Channel orders of a colour frame.
CHANNEL_ORDER_RGB = "rgb"
CHANNEL_ORDER_BGR = "bgr"
#: The channel order of every frame the sensor holds and judges. ``ObstacleGuard``
#: takes BGR (OpenCV's order) and converts it to the RGB that MiDaS expects, and a
#: video file read by OpenCV for a replay is BGR too, so in flight and in a replay
#: the model gets the same input. The Tello's decoder delivers RGB, so its frames
#: are converted once, where they enter the sensor (``to_sensor_frame``).
SENSOR_CHANNEL_ORDER = CHANNEL_ORDER_BGR


def to_sensor_frame(frame: Any, channel_order: str) -> Any:
    """The sensor's own copy of ``frame``, in ``SENSOR_CHANNEL_ORDER``.

    ``channel_order`` is the order of ``frame`` as its source delivers it:
    ``CHANNEL_ORDER_RGB`` (the Tello decoders) is reversed, ``CHANNEL_ORDER_BGR``
    (an OpenCV capture or a file read with OpenCV) is copied as it is. A frame
    without three colour channels has no order and is copied. The result is
    always a new array, so later changes to the source cannot reach it; an
    object that is not an array is returned as it is (``copy()`` when it has
    one). Every feed of the sensor and the replay script go through this one
    function. Raises ``ValueError`` for an unknown order.

    The channels are reversed with OpenCV's ``cvtColor`` when it can take the
    array (a contiguous SIMD swap, several times cheaper than NumPy's strided
    copy of a reversed view, which matters at 30 frames a second on the edge laptop);
    otherwise with NumPy. Both give the same pixels.
    """
    if channel_order not in (CHANNEL_ORDER_RGB, CHANNEL_ORDER_BGR):
        raise ValueError(f"unknown channel order: {channel_order!r}")
    shape = getattr(frame, "shape", None)
    if (
        channel_order != SENSOR_CHANNEL_ORDER
        and shape is not None
        and len(shape) == 3
        and shape[2] == 3
    ):
        return _reverse_channels(frame)
    copy = getattr(frame, "copy", None)
    return copy() if callable(copy) else frame


def _reverse_channels(frame: Any) -> Any:
    """A new C-contiguous array of ``frame`` (height x width x 3) with its channels reversed."""
    if str(getattr(frame, "dtype", "")) in ("uint8", "uint16", "float32"):
        # No OpenCV, or an array layout it refuses (negative strides): NumPy below.
        with contextlib.suppress(Exception):
            import cv2  # lazy: keep this module importable without OpenCV

            return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    # A reversed view, then a C-contiguous copy of it.
    return frame[..., ::-1].copy()


# ---------------------------------------------------------------------------
# The frame holder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimedFrame:
    """A camera frame with its arrival time on the holder's clock and its capture time.

    ``arrived_at`` is when the holder stored the frame. ``captured_at`` is the
    latest time the frame can have been captured: the decoder's stamp when it
    gave one (``FrameStamp``), never later than ``arrived_at``, and otherwise
    ``arrived_at`` itself. Freshness is judged on ``captured_at``.
    """

    frame: Any
    arrived_at: float
    captured_at: float | None = None

    def __post_init__(self) -> None:
        if self.captured_at is None:
            object.__setattr__(self, "captured_at", self.arrived_at)


#: The source object of a holder that has not been offered a frame yet.
_NO_SOURCE = object()


class LatestFrame:
    """Holds the newest usable camera frame, when it arrived and when it was captured.

    Every feed of the holder (the drone's frame callback, a decoder poller, a
    capture source) goes through ``offer``. It is fed for every new frame
    object the controller hands over, independent of any frame stride the
    detector uses, and never for the same object twice: a stalled video
    decoder keeps handing over the frame it last decoded, and that frame must
    not be stamped again as if it had just arrived. A frame the decoder marks
    stale (``FrameStamp``) is refused and counted, never stored, so the sensor
    keeps judging only frames that show a real, current view, and with none
    fails closed. The holder itself can be registered as the drone's frame
    callback (``drone.on_frame(holder)``), which offers each frame without a
    copy. ``clock`` is the monotonic clock that stamps arrivals
    (``time.monotonic`` by default); the executor's ``after`` stamps and a
    decoder's capture stamps must come from the same clock.

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
        self._stale_count = 0
        self._last_stale_cause: str | None = None
        self._listeners: list[Callable[[TimedFrame], None]] = []
        # The last source object any feed offered. Holding the reference keeps
        # the object alive, so ``is`` can never match a new object that reuses
        # its id; ``id()`` alone could.
        self._source: Any = _NO_SOURCE

    def __call__(self, frame: Any) -> None:
        self.offer(frame)

    def offer(
        self, source: Any, copy: Callable[[Any], Any] | None = None, stamp: FrameStamp | None = None,
    ) -> bool:
        """Store ``source`` as the newest frame unless it is the source object offered last or is stale.

        The check runs on ``source`` itself, before ``copy`` is called, and
        compares it with ``is`` against the last source object that any feed
        of this holder offered, so every feed shares the one record: a frame
        the frame loop already handed over is not stamped again when a poller
        reads it, and the other way round. A frame whose ``stamp`` is stale is
        recorded as offered, counted (``stale_frames``) and not stored. Any
        other new object is stored (as ``copy(source)`` when ``copy`` is given),
        stamped with the current time as its arrival and with the stamp's
        capture time (no later than the arrival) as its capture time, and
        handed to the listeners. Returns True when the frame was stored.

        A controller that reused one buffer object for every frame would
        therefore feed the holder only once, and the sensor would report the
        view as stale: it fails closed. djitellopy's decoder, the bound
        decoder, the fake controllers and OpenCV's ``read`` all return a new
        array per frame.
        """
        with self._cond:
            if source is self._source:
                return False
            self._source = source
            if stamp is not None and stamp.stale:
                self._stale_count += 1
                self._last_stale_cause = stamp.stale_cause
                return False
            frame = source if copy is None else copy(source)
            now = self._clock()
            captured = None if stamp is None or stamp.captured_at is None else min(stamp.captured_at, now)
            timed = TimedFrame(frame, now, captured)
            self._latest = timed
            self._count += 1
            listeners = list(self._listeners)
            self._cond.notify_all()
        for listener in listeners:
            listener(timed)
        return True

    def update(self, frame: Any) -> None:
        """Store ``frame`` as the newest, stamped with the current time, whatever it is.

        This bypasses the check of ``offer``; the feeds use ``offer``.
        """
        with self._cond:
            self._latest = TimedFrame(frame, self._clock())
            self._count += 1
            self._cond.notify_all()

    def add_listener(self, listener: Callable[[TimedFrame], None]) -> None:
        """Call ``listener(timed_frame)`` with every frame ``offer`` stores, after storing it.

        For a recorder that must save exactly the frames the sensor can judge
        (``scripts/obstacle_record.py``). A listener runs on the feeding thread
        and must be quick; an exception it raises reaches the feed.
        """
        with self._cond:
            self._listeners.append(listener)

    @property
    def frames_seen(self) -> int:
        with self._cond:
            return self._count

    @property
    def stale_frames(self) -> int:
        """Frames refused because their decoder marked them stale."""
        with self._cond:
            return self._stale_count

    @property
    def last_stale_cause(self) -> str | None:
        with self._cond:
            return self._last_stale_cause

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
        """Return the newest frame captured at or after ``after + settle_s``.

        Waits until ``wait_s`` past the later of now and ``after + settle_s``,
        so a call made before the settle time is over still gets the whole
        ``wait_s`` once a frame can count, and returns ``None`` when no such
        frame came. ``limit_s``, when given, caps the wait counted from now
        (the caller's remaining time budget). A ``wait_s`` or ``limit_s`` of 0
        looks once and does not wait. A frame counts by its capture time
        (``TimedFrame.captured_at``), so one that a slow decoder hands over
        late does not count as a view of the present.
        """
        threshold = after + settle_s
        now = self._clock()
        deadline = max(now, threshold) + wait_s if wait_s > 0 else now
        if limit_s is not None:
            deadline = min(deadline, now + max(0.0, limit_s))
        with self._cond:
            while True:
                latest = self._latest
                if latest is not None and latest.captured_at >= threshold:
                    return latest
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, self._POLL_S))
