"""Ground-side fleet coordinator for a multi-drone search.

The coordinator splits the search area into one zone per drone, gives
neighbouring zones different altitude slots, synchronises clocks with every
edge, schedules a common start, merges detections from all drones onto one
map, re-tasks lanes when a drone drops out and lands every drone on demand.

It depends on no transport code. Each edge is reached through an
``EdgeLink``: an object with a ``send(dict)`` method and an ``on_message``
attribute that the coordinator sets to a callback taking one message dict.
The dashboard adapts its WebSocket transport to this protocol and tests use
in-process fakes. The coordinator never calls ``send`` while holding its
state lock, so a link may deliver replies synchronously from inside ``send``
or from another thread.

Message shapes follow the binding contracts in design section 3:

* ground to edge: ``ping``, ``clock``, ``assign_lanes``, ``start_at``,
  ``land`` and ``emergency_land_all``;
* edge to ground: ``pong``, ``ack`` (for ``assign_lanes`` and ``start_at``),
  ``nack`` (a refused ``assign_lanes``), ``telemetry`` and ``detections``.

Re-tasking never reports lanes as covered before a drone has accepted them.
When a drone is lost, its unflown lanes are offered only to drones whose
geofence contains them, a ``nack`` sends the refused lanes on to another
eligible drone, and lanes that no drone can take are reported as uncovered
in a ``retask_failed`` event.

What an obstacle cut leaves unsearched rides on the same machinery: once
the cutting drone has finished its lanes, the segments of each cut are
offered once to another drone that senses obstacles, which flies them
reversed at its own altitude slot (``lane_reflight``); the ``reflight_*``
events follow each offer to ``reflown`` or ``blocked_again``. A stretch a
drone flew beside its lane to get past an obstacle (an offset segment of a
sidestep, ``lane_avoid``, reported in ``obstacle_avoid``) is listed, announced
(``lane_offset``) and offered the same way, except that one tied to a person
(seen during its sidestep, or a person cut on its lane) waits for the
operator's confirmation like a person cut. A drone that could not get back
to its lane after a sidestep and hovers under manual control is announced as
``sidestep_held``.

Clock convention: the offset of a link is the median over the sync rounds of
``((t2 - t1) + (t3 - t4)) / 2``, which is the edge clock minus the ground
clock. It is sent unchanged in ``clock{offset_s}``. An edge fires a
``start_at`` when its own clock reaches ``t_ground_s + offset_s`` and reports
``executed_at_ground_s`` already converted back to ground time.

Coordinate frame: the shared grid has its origin at the near-left corner of
the search area, ``x`` grows to the right and ``y`` grows forward, in
centimetres. Each drone takes off at the near-left corner of its own zone, so
the lanes it receives are relative moves from that point and the
``rel_x_cm``/``rel_y_cm`` it reports are relative to that point. The
coordinator adds the zone origin to place telemetry and detections on the
shared grid. All drones face the same forward direction at takeoff.

That frame is per flight: an edge makes the spot each takeoff starts from its
origin again, and its geofence is a square around that spot, because a Tello
lands where it is and nothing tells the edge whether the operator carried it
back to its start spot. A plan is therefore only right if every drone is back
on its zone origin before each ``start()`` (see ``docs/fleet-setup.md``,
"Placement and heading"). After a flight the reported position is where the
drone landed, relative to where it took off.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from flightrisk import obs_events
from flightrisk.drone.lane_reflight import leg_rest, plan_reflight
from flightrisk.drone.lane_reflight import transit as _transit
from flightrisk.drone.lane_steps import (
    Cut,
    LaneReplay,
    UncoveredSegment,
    decode_cuts,
    replay_lanes,
    uncovered_segments,  # noqa: F401 - the replay the edge uses; kept importable from here
)
from flightrisk.drone.obstacle_sensor import (
    AVOID_HELD_REASON_KEY,
    AVOID_OFFSETS_KEY,
    AVOID_OFFSETS_SEEN_KEY,
    AVOID_PHASE_HELD,
    AVOID_PHASE_KEY,
    AVOID_SIDESTEP_CM_KEY,
    CONFIRM_REFLIGHT_CUT_REASONS,
    CUT_PERSON,
    CUT_SENSOR_DOWN,
    MAX_CUTS_LISTED,
    MAX_OFFSETS_LISTED,
    PERSON_CUE_AGE_KEY,
    PERSON_CUE_DOWN_KEY,
    REFLIGHT_CUT_REASONS,
    SEGMENT_OFFSET,
    SEGMENT_SKIPPED,
    SEGMENT_TOO_SHORT,
    SENSING_AUTOPILOT,
    SENSING_OFF,
    SENSING_STEPPED,
    SENSOR_UNAVAILABLE_DETAIL,
    TELEMETRY_AVOID,
    TELEMETRY_CUT_REASONS,
    TELEMETRY_CUTS,
    TELEMETRY_CUTS_TOTAL,
    TELEMETRY_FIELDS,
    TELEMETRY_LATENCY_MS,
    TELEMETRY_PERSON_CUE,
    TELEMETRY_SENSING,
    TELEMETRY_SENSOR_OK,
    TELEMETRY_SEQ,
    TELEMETRY_STATE,
)
from flightrisk.drone.plan_paths import closest_approach
from flightrisk.drone.safety import (
    DEFAULT_ALT_SLOTS_CM,
    DEFAULT_MIN_TAKEOFF_BATTERY,
    min_takeoff_battery_from_env,
)
from flightrisk.drone.temp_preflight import TELEMETRY_TEMP, live_refusal, temp_status
from flightrisk.drone.search import split_parallel_track_zones
from flightrisk.fleet_protocol import MAX_TEXT_LEN

logger = logging.getLogger(__name__)

SYNC_ROUNDS = 8
"""Ping/pong rounds per link in one ``sync()``."""

DEFAULT_ZONE_GAP_CM = 100
"""Empty corridor between neighbouring zones."""

# DEFAULT_ALT_SLOTS_CM (100 and 130 cm, in ``flightrisk.drone.safety``) are the altitude slots given to
# zones in turn when no per-drone slot is set. The second slot was 160 cm until a Tello at that slot hit
# a ceiling (docs/ceiling-and-altitude.md).

# Deprecated alias, kept so an importer that still uses the old name (the single-drone start in
# flightrisk/dashboard/single_search.py from PR #200) keeps working. It is only the default of the
# minimum; the value that applies, with the FLIGHTRISK_MIN_TAKEOFF_BATTERY override, comes from
# ``flightrisk.drone.safety.min_takeoff_battery_from_env``.
MIN_START_BATTERY = DEFAULT_MIN_TAKEOFF_BATTERY

TELEMETRY_FRESH_S = 2.0
# Edge refusals of an assign_lanes that are only true for now: the chunk waits
# and is offered again once the drone's telemetry shows they are over.
TRANSIENT_NACKS = frozenset({"landing", "busy"})
"""Telemetry older than this is ignored by the start battery gate."""

# The Tello move range. Transits (``lane_reflight.transit``) are split with the
# same limits, ``MIN_MOVE_CM`` and ``MAX_MOVE_CM`` of ``flightrisk.drone.safety``.
TELLO_MIN_MOVE_CM = 20
TELLO_MAX_MOVE_CM = 500

RETASK_WAIT_S = 2.0
"""How long ``on_drone_lost`` waits for the receiving drones to answer
before it reports the re-task. Later answers are reported as they arrive."""

MAX_REFLIGHTS_PER_DRONE = 4
"""Most cuts of one drone per plan whose segments are offered to another drone (design section 3.4).

Later cuts are only reported, so a drone with a flapping sensor cannot flood
the other drones with re-flights. The offset segments of its sidesteps have
their own limit (``MAX_OFFSET_REFLIGHTS_PER_DRONE``)."""

MAX_OFFSET_REFLIGHTS_PER_DRONE = 4
"""Most offset segments of one drone per plan offered to another drone (stretches flown beside a lane).

Kept apart from ``MAX_REFLIGHTS_PER_DRONE``, so poles a drone got past can
never use up the re-flights of the lanes it had to cut."""

SIDESTEP_MARGIN_CM = 50
"""Extra distance the re-task separation check keeps from the path of a drone that may sidestep, beyond the gap.

An edge that has the sidestep switched on (it is off by default) may sidestep
an obstacle by up to ``FLIGHTRISK_OBSTACLE_SIDESTEP_CM`` inside its own zone
(``lane_avoid``). This margin is 50 cm, the usual sidestep, and is independent
of the edge default of 0. Zones are kept
``gap_cm`` apart, but a re-task or a re-flight can give a drone a path that
runs ``gap_cm`` from another drone's path outside the zones, and a sidestep of
that other drone then brings the two closer by the sidestep. The margin is
kept only from a drone whose telemetry says it may sidestep
(``obstacle_avoid`` ``sidestep_cm`` above 0). The dashboard reads it from
``FLIGHTRISK_SIDESTEP_MARGIN_CM`` (``SIDESTEP_MARGIN_ENV``), to match edges set
to another sidestep."""

SIDESTEP_MARGIN_ENV = "FLIGHTRISK_SIDESTEP_MARGIN_CM"
"""The ground's sidestep margin in cm (``SIDESTEP_MARGIN_CM`` by default, 0 to ``SIDESTEP_MARGIN_MAX_CM``)."""

SIDESTEP_MARGIN_MAX_CM = 500
"""Largest ``FLIGHTRISK_SIDESTEP_MARGIN_CM`` accepted."""

LOST_OBSTACLE_S = 20.0
"""How long a lost drone's last reported position stays an obstacle for re-task paths, in seconds.

A drone is declared lost when its edge stops answering, but the drone itself
may still be in the air: a Tello that gets no commands hovers up to 15 s
before it starts to land by itself, and its auto-land descent takes another
2 to 4 s. Until this long after the loss (the 15 s hover, the descent and a
margin), or until its telemetry says it is not flying, a re-task or re-flight
path that would come within the zone gap of that position waits instead of
being offered."""

MAX_OBSTACLE_SEQ = 2**31
"""Highest ``obstacle_seq`` the ground believes. An edge counts one per cut, so
a higher count is a corrupted or buggy report: its obstacle fields are
ignored rather than trusted."""

# The fleet_event each re-flight chunk status is announced with (design section 6).
_REFLIGHT_EVENTS = {
    "offered": "reflight_offered",
    "acked": "reflight_accepted",
    "reflown": "reflight_done",
    "blocked_again": "reflight_blocked",
    "failed": "reflight_failed",
}

# Floating-point slack in the geofence check, matching SafetyEnvelope.
_FENCE_TOLERANCE_CM = 1e-6

_DIRECTION_VECTORS = {
    "forward": (0, 1),
    "back": (0, -1),
    "right": (1, 0),
    "left": (-1, 0),
}

# The telemetry fields ``status()`` copies for a drone that reports obstacle sensing.
# ``TELEMETRY_PERSON_CUE`` is copied through ``_person_cue`` instead.
_STATUS_OBSTACLE_KEYS = (TELEMETRY_SENSING, TELEMETRY_SENSOR_OK, TELEMETRY_STATE, TELEMETRY_LATENCY_MS)

_PASSTHROUGH_DETECTION_KEYS = ("thumbnail", "lat", "lon")


class EdgeLink(Protocol):
    """One ground-to-edge connection.

    ``send`` delivers one JSON-serialisable message to the edge and may raise
    if the link is down. The coordinator assigns ``on_message``; the link
    calls it with every message dict that arrives from its edge.
    """

    on_message: Callable[[dict], None] | None

    def send(self, msg: dict) -> None: ...


@dataclass(frozen=True)
class ZoneAssignment:
    """One drone's zone, altitude slot and initial lanes."""

    drone_id: str
    x_cm: int
    y_cm: int
    w_cm: int
    h_cm: int
    alt_cm: int
    lanes: tuple[dict, ...]
    # Where the drone takes off on the shared grid, when that is not the
    # zone's near-left corner (a plan edited on the map). The drone's lanes
    # and reported position are relative to this point. The zone carries it
    # as ``takeoff_x_cm``/``takeoff_y_cm``, which the fleet protocol allows.
    takeoff_x_cm: int | None = None
    takeoff_y_cm: int | None = None

    @property
    def origin(self) -> tuple[int, int]:
        """The takeoff point on the shared grid: the explicit one, else the zone corner."""
        if self.takeoff_x_cm is not None and self.takeoff_y_cm is not None:
            return self.takeoff_x_cm, self.takeoff_y_cm
        return self.x_cm, self.y_cm

    def zone_dict(self) -> dict:
        zone = {"x_cm": self.x_cm, "y_cm": self.y_cm, "w_cm": self.w_cm, "h_cm": self.h_cm}
        if self.takeoff_x_cm is not None and self.takeoff_y_cm is not None:
            zone["takeoff_x_cm"], zone["takeoff_y_cm"] = self.takeoff_x_cm, self.takeoff_y_cm
        return zone

    def to_dict(self) -> dict:
        return {
            "drone_id": self.drone_id,
            **self.zone_dict(),
            "alt_cm": self.alt_cm,
            "lanes": [dict(lane) for lane in self.lanes],
        }


@dataclass(frozen=True)
class FleetPlan:
    """The zone split produced by ``FleetCoordinator.plan()``."""

    zones: tuple[ZoneAssignment, ...]
    width_cm: int
    depth_cm: int
    gap_cm: int
    strip_cm: int
    # A plan made on the map (``plan_zones``): the search area's near-left
    # corner on the shared grid, the rectangles of the area no camera sees
    # (``uncovered`` entries of ``kind: "area"``) and the map's own details
    # (pattern, drawn and generated paths, coverage), shown by the browser.
    grid_x_cm: int = 0
    grid_y_cm: int = 0
    uncovered_areas: tuple[dict, ...] = ()
    details: Mapping[str, Any] | None = None

    def zone_for(self, drone_id: str) -> ZoneAssignment | None:
        for zone in self.zones:
            if zone.drone_id == drone_id:
                return zone
        return None

    def to_dict(self) -> dict:
        """Shape of the dashboard ``fleet_plan`` event."""
        out = {
            "zones": [zone.to_dict() for zone in self.zones],
            "grid": {"x_cm": self.grid_x_cm, "y_cm": self.grid_y_cm, "w_cm": self.width_cm, "h_cm": self.depth_cm},
            "gap_cm": self.gap_cm,
            "strip_cm": self.strip_cm,
        }
        if self.details is not None:
            out["details"] = dict(self.details)
        return out


@dataclass(frozen=True)
class ClockSync:
    """Result of synchronising one link's clock."""

    drone_id: str
    offset_s: float | None
    rtt_s: float | None
    samples: int

    @property
    def synced(self) -> bool:
        return self.offset_s is not None


@dataclass(frozen=True)
class StartResult:
    """Outcome of ``FleetCoordinator.start()``.

    ``refusals`` maps a drone id (or ``"fleet"`` for fleet-wide reasons) to
    the reason the start was refused. It is empty when ``ok`` is true.
    """

    ok: bool
    t_ground_s: float | None
    refusals: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FleetDetection:
    """One merged detection on the shared grid."""

    id: int
    drone_id: str
    x_cm: float
    y_cm: float
    drone_ids: tuple[str, ...]
    count: int
    first_seen_s: float
    last_seen_s: float
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Shape of the dashboard ``fleet_detection`` event.

        ``rel_x_cm``/``rel_y_cm`` repeat ``x_cm``/``y_cm`` because the map
        plots pins on the relative grid under those names.
        """
        out = {
            "id": self.id,
            "drone_id": self.drone_id,
            "drone_ids": list(self.drone_ids),
            "count": self.count,
            "x_cm": self.x_cm,
            "y_cm": self.y_cm,
            "rel_x_cm": self.x_cm,
            "rel_y_cm": self.y_cm,
        }
        out.update(self.extra)
        return out


@dataclass(eq=False)
class _Chunk:
    """A contiguous run of a lost drone's unflown lanes, or one cut's re-flight, re-tasked as one piece.

    ``points`` are the shared-grid positions along the run: its start, then
    the end of each lane. ``status`` is ``queued`` (waiting for a drone),
    ``offered`` (sent to ``receiver``, no answer yet), ``acked`` (accepted
    by ``receiver``) or ``failed`` (no drone can take it, see ``reason``).
    ``appended`` is what the offer added to the receiver's lanes for this
    run: the transit to its start, then its lanes. ``refusals`` maps each
    drone that refused the run to its reason, and ``reported`` is the status
    last sent to the dashboard. ``deferred`` maps a drone that refused only
    for now (``landing`` or ``busy``, see ``TRANSIENT_NACKS``) to that
    reason: the run waits and is offered to it again once its telemetry
    shows the refusal is over.

    ``kind`` is ``lost`` for a lost drone's lanes and ``reflight`` for the
    segments one lane cut left unsearched (design section 3.4). A re-flight
    chunk's ``lost_id`` is the cutting drone, ``first_index`` the cut lane
    and ``cut_seq`` the cut's ``obstacle_seq``; its lanes come from
    ``lane_reflight.plan_reflight`` and its status may also become
    ``flying``, ``reflown`` or ``blocked_again``. Its other fields are
    described where they are declared.
    """

    lost_id: str
    first_index: int
    lanes: list[dict]
    points: list[tuple[int, int]]
    status: str = "queued"
    receiver: str | None = None
    appended: list[dict] = field(default_factory=list)
    refusals: dict[str, str] = field(default_factory=dict)
    deferred: dict[str, str] = field(default_factory=dict)
    reason: str | None = None
    reported: str | None = None
    # Drones the run could not go to because their new path would come
    # closer than the zone gap to another drone's path: rid -> why.
    too_close: dict[str, str] = field(default_factory=dict)
    kind: str = "lost"
    # Re-flight only. The segments the lanes re-fly, on the shared grid and in
    # flight order; ``legs[k]`` is the index in ``segments`` of the segment
    # lane k flies, or None for a transit between two segments. ``too_short``
    # are the cut's segments under the minimum move, which are not offered.
    cut_seq: int | None = None
    segments: list[UncoveredSegment] = field(default_factory=list)
    legs: list[int | None] = field(default_factory=list)
    too_short: list[UncoveredSegment] = field(default_factory=list)
    # Re-flight only. ``gate_open`` turns True once the cutting drone has
    # finished its lanes, landed or been lost; until then the chunk waits for
    # it. ``cutter_lanes`` is how many lanes the cutting drone had then: its
    # path over them is not checked for separation, any lane it is given
    # later is. ``crowded`` is why the chunk waits while the cutting drone,
    # still in the air, is too close to the only path that would take it.
    gate_open: bool = False
    cutter_lanes: int = 0
    crowded: str | None = None
    # Why the chunk waits for a receiver that may take it later: one that
    # senses obstacles and has not finished its own lanes, has no fresh
    # telemetry or is not flying yet (both kinds, see
    # ``_own_lanes_left_locked``), or, for a re-flight, one without fresh
    # telemetry or taking off (review N7). Shown as the waiting segment's
    # ``detail``, in the ``retask`` event's ``awaiting`` lanes and, for a
    # lost drone's lanes, as the ``reason`` of their ``waiting`` entries in
    # ``current_plan()["uncovered"]``.
    held: str | None = None
    # Re-flight only. Where ``appended`` starts in the receiver's lanes (set
    # with the offer), the drone that accepted it (kept when the chunk fails
    # afterwards), every drone it was offered to, and the (status, receiver)
    # last announced in a ``reflight_*`` event.
    appended_start: int | None = None
    flown_by: str | None = None
    offered_to: list[str] = field(default_factory=list)
    announced: tuple[str, str | None] | None = None
    # True while the chunk waits for the operator to confirm it: a re-flight
    # of a cut whose reason is in ``CONFIRM_REFLIGHT_CUT_REASONS`` (a person),
    # until ``confirm_reflight``, or a lost drone's lanes re-tasked with
    # ``on_drone_lost(confirm=...)``, until ``confirm_retask``. It is never
    # offered before that.
    confirm: bool = False
    # Re-flight only. True once the receiver flew the appended lanes, or
    # some of them, and the chunk ended without re-flying every segment
    # (``blocked_again``, or ``failed`` because the receiver's sensor went
    # down on them, it flew them off the planned path or it landed before
    # it finished them): what it left unsearched is listed instead of the
    # whole segments.
    flown_rest: bool = False
    # Re-flight only. The receiver's ``lane_index`` when its telemetry showed
    # it on the ground before it had flown the appended lanes (the chunk then
    # failed): the lanes from there on were not flown, so the legs on them
    # are listed whole.
    landed_at: int | None = None
    # Re-flight of an offset segment only (a stretch a sidestep flew beside
    # the lane, ``lane_avoid``): ``(lane_index, from_cm)`` of that segment.
    # Such a chunk has no ``cut_seq`` and keeps the segment it was made with.
    # It waits for the operator (``confirm``) when a person was seen during
    # that sidestep (``seen``) or the drone cut the same lane for a person;
    # ``confirm_cut`` is then that cut's number, whose ``confirm_reflight``
    # releases it too.
    offset: tuple[int, int] | None = None
    seen: str | None = None
    confirm_cut: int | None = None

    @property
    def start(self) -> tuple[int, int]:
        return self.points[0]

    @property
    def appended_range(self) -> tuple[int, int] | None:
        """``(first, end)`` of the receiver's lanes this offer appended (transit included), or None."""
        if self.appended_start is None:
            return None
        return self.appended_start, self.appended_start + len(self.appended)

    def lane_entries(self) -> list[dict]:
        """One entry per lane: its index in the lost drone's lanes, move and grid ends.

        A re-flight chunk lists the segments it re-flies instead: each with
        the cutting drone's lane index, that lane's direction and the
        segment's two ends on the shared grid (``x_cm, y_cm`` next to the
        ground the cutting drone flew), the shape of a
        ``current_plan()["uncovered"]`` segment entry.
        """
        if self.kind == "reflight":
            return [_segment_entry(seg) for seg in self.segments]
        entries = []
        for k, lane in enumerate(self.lanes):
            (x0, y0), (x1, y1) = self.points[k], self.points[k + 1]
            entries.append({
                "index": self.first_index + k,
                "direction": lane["direction"],
                "distance_cm": lane["distance_cm"],
                "x_cm": x0,
                "y_cm": y0,
                "end_x_cm": x1,
                "end_y_cm": y1,
            })
        return entries


@dataclass(eq=False)
class _Offer:
    """An ``assign_lanes`` that appends re-tasked chunks to one drone, awaiting its answer.

    ``base_lanes`` and the saved ack fields restore the drone's state if it
    refuses the offer, because the edge then keeps flying its previous plan.
    """

    receiver: str
    chunks: list[_Chunk]
    base_lanes: list[dict]
    lanes_total: int
    acked: bool
    ack_battery: float | None
    ack_lanes_total: int | None


@dataclass(eq=False)
class _Retask:
    """The re-tasking of one lost drone's unflown lanes.

    ``confirm`` is set when the lanes wait for the operator before any drone
    is offered them (``on_drone_lost(confirm=...)``): why they wait, as the
    operator reads it. Its chunks are marked ``confirm`` until
    ``confirm_retask``, which splits the lanes again for the drones that can
    take them then: ``path`` is the lost drone's lane list at the loss and
    ``index`` the first unflown lane in it.
    """

    lost_id: str
    chunks: list[_Chunk]
    settled: threading.Event = field(default_factory=threading.Event)
    reported: bool = False
    confirm: str | None = None
    path: list[dict] = field(default_factory=list)
    index: int = 0


@dataclass
class _DroneState:
    """Mutable per-drone state, guarded by the coordinator lock."""

    zone: ZoneAssignment | None = None
    lanes: list[dict] = field(default_factory=list)
    offset_s: float | None = None
    rtt_s: float | None = None
    sync_samples: int = 0
    acked: bool = False
    ack_battery: float | None = None
    ack_lanes_total: int | None = None
    telemetry: dict | None = None
    telemetry_at: float | None = None
    executed_at_ground_s: float | None = None
    lost: bool = False
    # Ground clock when the drone was declared lost (``LOST_OBSTACLE_S``).
    lost_at: float | None = None
    offer: _Offer | None = None
    nack_reason: str | None = None
    # The smallest fence half-width this edge reported in a geofence nack; later re-tasks are planned
    # against the smaller of it and the coordinator's own fence.
    reported_fence_cm: float | None = None
    ping_t1: float | None = None
    ping_event: threading.Event | None = None
    pong: tuple[float, float, float] | None = None
    # Obstacle sensing (design section 6). ``obstacle_seq`` is the last
    # ``obstacle_seq`` the edge reported, so each increase is announced once.
    # The edge counts every cut it makes while it runs and never restarts the
    # count for a new plan, so the k-th last listed cut is cut number
    # ``obstacle_seq - k``. ``obstacle_plan_seq`` is the count when the
    # current plan was installed: cuts numbered up to it belong to an earlier
    # plan. ``obstacle_cuts`` maps the number of every cut of the current
    # plan the ground has seen to the cut, because telemetry lists only the
    # last ``MAX_CUTS_LISTED``.
    obstacle_seq: int = 0
    obstacle_plan_seq: int = 0
    obstacle_cuts: dict[int, Cut] = field(default_factory=dict)
    # The ground numbers a cut ``obstacle_key_base + obstacle_seq``. The base
    # grows by the last count each time the edge restarts (its count starts
    # again at 0), so a cut number is never used twice: cut numbers key
    # ``obstacle_cuts`` and name re-flights (``cut_seq``). ``obstacle_plan_seq``
    # is in the ground's numbering. ``obstacle_seen`` turns True with the
    # first count the edge reports.
    obstacle_key_base: int = 0
    obstacle_seen: bool = False
    # Indexes in ``lanes`` where an accepted re-task appended lanes. The
    # ground plans appended lanes from where the drone actually is, and the
    # edge ends the deficit of its earlier cuts there, so the replay of the
    # drone's lanes restarts its deficit at each of them (``lane_steps``).
    retask_starts: list[int] = field(default_factory=list)
    # True once the drone's telemetry has said ``is_flying: true`` since the
    # current plan was installed. A drone that reports it is not flying after
    # that has landed, and one that never flew may still be taking off.
    seen_flying: bool = False
    # Lost drones only: True once a ``land`` was sent again because the
    # drone's telemetry showed it flying after the loss, or once telemetry
    # showed a person flying it by hand (``_reland_lost_locked``). False
    # again when that ``land`` could not be sent.
    relanded: bool = False
    # The offset segments of the current plan the edge reported
    # (``obstacle_avoid`` ``offsets``): ``(lane_index, from_cm) -> length_cm``
    # along the lane, in the order they arrived; and whether the edge last
    # said it holds under manual control after a sidestep (phase ``held``).
    avoid_offsets: dict[tuple[int, int], int] = field(default_factory=dict)
    avoid_held: bool = False
    # The reason a gate of the sidestep stopped for (``offsets_seen``), by offset.
    avoid_offset_seen: dict[tuple[int, int], str] = field(default_factory=dict)


def _number(value: Any) -> float | None:
    """Return ``value`` as a float if it is a real number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return float(value)


def _drone_link_refusal(msg: dict, key: str = "drone_link_detail") -> str:
    """The operator text for an edge that cannot reach its drone, from telemetry or a nack.

    The edge's own text (for example ``Tello unreachable (no state for 12 s)``)
    when it sent a usable one, else ``drone unreachable``.
    """
    detail = msg.get(key)
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:200]
    return "drone unreachable"


def _person_cue(value: Any) -> dict | None:
    """An edge's ``obstacle_person_cue`` telemetry as ``status()`` shows it, or None when it sent none.

    ``{"person_cue_down": bool, "detection_age_s": seconds or None}``: whether the
    camera sensor's person cue has been ``unknown`` for several gates in a row,
    and how old the view of its newest detection result is. Anything else in
    the field is dropped, and a value that is not that shape is None.
    """
    if not isinstance(value, dict) or not isinstance(value.get(PERSON_CUE_DOWN_KEY), bool):
        return None
    age = _number(value.get(PERSON_CUE_AGE_KEY))
    return {PERSON_CUE_DOWN_KEY: value[PERSON_CUE_DOWN_KEY], PERSON_CUE_AGE_KEY: age if age is None or age >= 0 else None}


def _obstacle_sensor_refusal(msg: dict) -> str:
    """Why a start is refused for a stepped drone whose telemetry says its obstacle sensor is down."""
    state = msg.get(TELEMETRY_STATE)
    suffix = f" (obstacle_state {state})" if isinstance(state, str) else ""
    return f"{SENSOR_UNAVAILABLE_DETAIL} the edge reports it cannot sense obstacles{suffix}"


def _lane(direction: str, distance_cm: int) -> dict:
    return {"direction": direction, "distance_cm": int(distance_cm)}


def _segment_entry(seg: UncoveredSegment) -> dict:
    """A segment already on the shared grid in the ``lane_entries()`` shape."""
    return {
        "index": seg.lane_index,
        "direction": seg.direction,
        "distance_cm": seg.length_cm,
        "x_cm": seg.x_cm,
        "y_cm": seg.y_cm,
        "end_x_cm": seg.end_x_cm,
        "end_y_cm": seg.end_y_cm,
    }


def _on_grid(seg: UncoveredSegment, origin: tuple[int, int]) -> UncoveredSegment:
    """*seg*, given relative to a drone's takeoff point, moved onto the shared grid."""
    return replace(
        seg,
        x_cm=origin[0] + seg.x_cm,
        y_cm=origin[1] + seg.y_cm,
        end_x_cm=origin[0] + seg.end_x_cm,
        end_y_cm=origin[1] + seg.end_y_cm,
    )


# The keys of a ``lane_entries()`` entry.
_LANE_ENTRY_KEYS = ("index", "direction", "distance_cm", "x_cm", "y_cm", "end_x_cm", "end_y_cm")


def _uncovered_entry(seg: UncoveredSegment, drone_id: str, reason: str, cut_seq: int | None) -> dict:
    """A shared-grid segment as a ``current_plan()["uncovered"]`` entry of ``kind`` ``segment``."""
    return {"kind": "segment", **_segment_entry(seg), "drone_id": drone_id, "reason": reason, "cut_seq": cut_seq}


def _chunk_what(chunk: _Chunk) -> str:
    """How a re-flight chunk is named in events and logs."""
    if chunk.offset is not None:
        return f"{chunk.lost_id}'s offset segment (lane {chunk.offset[0]}, flown beside the lane)"
    return f"{chunk.lost_id}'s blocked segment (cut {chunk.cut_seq})"


def _decode_offsets(flat: Any, seen: Any = None) -> list[tuple[int, int, int, str]]:
    """``(lane_index, from_cm, length_cm, seen)`` of ``obstacle_avoid`` ``offsets``; malformed ones dropped.

    *seen* is ``offsets_seen``: one string per listed triple, the reason a
    gate of that sidestep stopped for (``person`` or ``sensor_down``) or
    ``""``. A missing or malformed list leaves every reason ``""``.
    """
    if not isinstance(flat, (list, tuple)):
        return []
    reasons = list(seen) if isinstance(seen, (list, tuple)) else []
    out = []
    for k in range(0, min(len(flat), 3 * MAX_OFFSETS_LISTED) - 2, 3):
        triple = flat[k:k + 3]
        if all(isinstance(v, int) and not isinstance(v, bool) for v in triple):
            lane, start, length = triple
            reason = reasons[k // 3] if k // 3 < len(reasons) and isinstance(reasons[k // 3], str) else ""
            if lane >= 0 and start >= 0 and length > 0:
                out.append((lane, start, length, reason[:MAX_TEXT_LEN]))
    return out


def _offset_key(offset: Any) -> tuple[int, int] | None:
    """``(lane_index, from_cm)`` from a ``[lane_index, from_cm]`` pair, or None when malformed."""
    if not isinstance(offset, (list, tuple)) or len(offset) != 2:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in offset):
        return None
    return int(offset[0]), int(offset[1])


def _segment_key(seg: UncoveredSegment, cut_seq: int) -> tuple:
    """What identifies one shared-grid segment of a drone's replay, whatever its index among the cuts."""
    return (cut_seq, seg.lane_index, seg.direction, seg.x_cm, seg.y_cm, seg.end_x_cm, seg.end_y_cm, seg.reason)


def _path_point(origin: tuple[int, int], lanes: list[dict], upto: int | None = None) -> tuple[int, int]:
    """Position on the shared grid after flying ``lanes[:upto]`` from ``origin``."""
    x, y = origin
    for lane in lanes[:upto]:
        dx, dy = _DIRECTION_VECTORS.get(lane["direction"], (0, 0))
        x += dx * lane["distance_cm"]
        y += dy * lane["distance_cm"]
    return x, y


def _split_contiguous(items: list[dict], parts: int) -> list[tuple[int, list[dict]]]:
    """Split ``items`` into ``parts`` contiguous chunks of near-equal length.

    Returns ``(start_index, chunk)`` pairs and never returns an empty chunk.
    """
    parts = max(1, min(parts, len(items)))
    base, remainder = divmod(len(items), parts)
    chunks = []
    start = 0
    for i in range(parts):
        size = base + (1 if i < remainder else 0)
        chunks.append((start, items[start:start + size]))
        start += size
    return chunks


class FleetCoordinator:
    """Plans, synchronises, starts and supervises a fleet of edge drones.

    Args:
        links: One ``EdgeLink`` per drone id. The coordinator sets each
            link's ``on_message``.
        area_cm: Search area as ``(width, depth)`` in centimetres, or one
            number for a square area.
        strip_cm: Lane spacing inside a zone.
        alt_slots_cm: Altitude slots assigned to zones in turn, so
            neighbouring zones never share a slot when two or more are given.
        alt_slot_of: Optional ``(drone_id, zone_index) -> slot_cm`` that
            picks each drone's altitude slot at ``plan()`` instead (the
            dashboard's per-drone slot settings). The coordinator does not
            check slots against a ceiling; its caller and every edge do.
        dedup_radius_cm: Detections closer than this on the shared grid are
            merged, whichever drones reported them.
        zone_gap_cm: Empty corridor between neighbouring zones.
        min_battery: Lowest battery percentage allowed at ``start()`` and for
            a re-flight receiver. ``None`` (the default) reads
            ``FLIGHTRISK_MIN_TAKEOFF_BATTERY`` through
            ``min_takeoff_battery_from_env``, like the edges; a malformed
            value raises ``ValueError``, so a ground that cannot tell the
            edges' minimum does not start.
        clock: Monotonic ground clock in seconds.
        on_event: Optional callback ``(event_name, payload)`` for the
            dashboard. It receives ``fleet_detection`` for each new merged
            detection and ``fleet_event`` for ``drone_lost``, ``retask``,
            ``retask_failed``, ``land_all``, ``lane_blocked``,
            ``lost_drone_relanded`` and the re-flight events
            ``reflight_offered``, ``reflight_accepted``, ``reflight_done``,
            ``reflight_blocked`` and ``reflight_failed``.
            Exceptions it raises are logged and ignored.
        geofence_cm: Half-width of the square geofence each edge keeps
            around its takeoff point (``FLIGHTRISK_GEOFENCE_CM`` on the
            edge). Re-tasked lanes are offered only to drones whose geofence
            contains their whole new path. ``None`` plans without a fence;
            the edges still enforce their own and answer with a ``nack``.
        retask_wait_s: How long ``on_drone_lost`` waits for the receiving
            drones to answer before reporting the re-task.
        retask_excluded: Optional ``drone_id -> bool``; a drone for which it
            returns True is never offered a lost drone's lanes. The dashboard
            passes the drones held for manual control
            (``docs/manual-takeover.md``), whose lanes would otherwise wait
            for the operator.
        manually_controlled: Optional ``drone_id -> bool``; True for the
            drone a person flies by hand from the ground right now. A lost
            drone that reports flying again is not sent ``land`` again while
            it returns True (``_reland_lost_locked``). The dashboard passes
            the drone its manual control has (``docs/manual-takeover.md``).
        retask_separation: Offer re-tasked lanes only to a drone whose new
            path stays the zone gap away from every other drone's path (the
            default). False switches that check off; only tests of the offer
            and refusal mechanics use it.
        sidestep_margin_cm: How much farther than the zone gap that path
            must stay from the path of another drone that reports it may
            sidestep an obstacle toward it (``SIDESTEP_MARGIN_CM``, the edge's
            default sidestep). 0 keeps exactly the gap.
    """

    def __init__(
        self,
        links: Mapping[str, EdgeLink],
        area_cm: int | tuple[int, int],
        strip_cm: int,
        alt_slots_cm: tuple[int, ...] = DEFAULT_ALT_SLOTS_CM,
        dedup_radius_cm: float = 80,
        *,
        zone_gap_cm: int = DEFAULT_ZONE_GAP_CM,
        min_battery: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, dict], None] | None = None,
        geofence_cm: float | None = None,
        retask_wait_s: float = RETASK_WAIT_S,
        alt_slot_of: Callable[[str, int], int] | None = None,
        retask_excluded: Callable[[str], bool] | None = None,
        retask_separation: bool = True,
        sidestep_margin_cm: int = SIDESTEP_MARGIN_CM,
        manually_controlled: Callable[[str], bool] | None = None,
    ) -> None:
        if not links:
            raise ValueError("FleetCoordinator needs at least one link")
        if not alt_slots_cm:
            raise ValueError("alt_slots_cm must contain at least one altitude")
        if isinstance(area_cm, (int, float)):
            width_cm = depth_cm = int(area_cm)
        else:
            width_cm, depth_cm = (int(v) for v in area_cm)
        if width_cm <= 0 or depth_cm <= 0:
            raise ValueError(f"area_cm must be positive, got {area_cm!r}")
        if strip_cm <= 0:
            raise ValueError(f"strip_cm must be positive, got {strip_cm!r}")
        if zone_gap_cm < 0:
            raise ValueError(f"zone_gap_cm must not be negative, got {zone_gap_cm!r}")
        if geofence_cm is not None and (_number(geofence_cm) is None or geofence_cm <= 0):
            raise ValueError(f"geofence_cm must be a positive number or None, got {geofence_cm!r}")
        if _number(retask_wait_s) is None or retask_wait_s < 0:
            raise ValueError(f"retask_wait_s must not be negative, got {retask_wait_s!r}")
        if _number(sidestep_margin_cm) is None or sidestep_margin_cm < 0:
            raise ValueError(f"sidestep_margin_cm must not be negative, got {sidestep_margin_cm!r}")

        # The link set is fixed for the coordinator's lifetime, so land_all()
        # can iterate it without taking the state lock.
        self._links: Mapping[str, EdgeLink] = MappingProxyType(dict(links))
        self._width_cm = width_cm
        self._depth_cm = depth_cm
        self._strip_cm = int(strip_cm)
        self._alt_slots_cm = tuple(int(a) for a in alt_slots_cm)
        self._alt_slot_of = alt_slot_of
        self._dedup_radius_cm = float(dedup_radius_cm)
        self._zone_gap_cm = int(zone_gap_cm)
        self._min_battery = float(min_takeoff_battery_from_env() if min_battery is None else min_battery)
        self._clock = clock
        self._on_event = on_event
        self._geofence_cm = float(geofence_cm) if geofence_cm is not None else None
        self._retask_wait_s = float(retask_wait_s)
        self._retask_excluded = retask_excluded
        self._retask_separation = bool(retask_separation)
        self._sidestep_margin_cm = int(sidestep_margin_cm)
        self._manually_controlled = manually_controlled

        self._lock = threading.RLock()
        self._drones: dict[str, _DroneState] = {drone_id: _DroneState() for drone_id in self._links}
        self._plan: FleetPlan | None = None
        self._started_at: float | None = None
        self._landed_all = False
        self._detections: list[FleetDetection] = []
        self._next_detection_id = 1
        self._retasks: list[_Retask] = []
        # One chunk per lane cut offered once to another drone (design section 3.4).
        self._reflights: list[_Chunk] = []

        for drone_id, link in self._links.items():
            link.on_message = self._make_callback(drone_id)

    def _make_callback(self, drone_id: str) -> Callable[[dict], None]:
        def callback(msg: dict) -> None:
            self.handle_message(drone_id, msg)
        return callback

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    @property
    def drone_ids(self) -> list[str]:
        return list(self._links)

    @property
    def plan_result(self) -> FleetPlan | None:
        """The plan from the last ``plan()`` call, before any re-tasking."""
        return self._plan

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def is_lost(self, drone_id: str) -> bool:
        """True once *drone_id* has been declared lost in the current mission (``on_drone_lost``)."""
        with self._lock:
            state = self._drones.get(drone_id)
            return state is not None and state.lost

    def lanes(self, drone_id: str) -> list[dict]:
        """The drone's current full lane list, including re-tasked lanes."""
        with self._lock:
            return [dict(lane) for lane in self._drones[drone_id].lanes]

    def current_plan(self) -> dict | None:
        """The plan as a ``fleet_plan`` payload, with lanes as re-tasked so far.

        ``uncovered`` lists every re-tasked lane that no drone could take, in
        the shape of the ``retask_failed`` event's lanes plus the lost
        drone's id, the plan's unseen areas, and every segment a lane cut
        left unsearched (``kind`` ``segment``, see
        ``_obstacle_uncovered_locked``), with its re-flight ``status`` when
        it is offered to another drone. A re-tasked lane that waits for a
        drone that senses obstacles (``held``, see
        ``_own_lanes_left_locked``) is listed in the same shape with
        ``status`` ``waiting`` and the waiting text as its ``reason``; a
        fleet without obstacle fields never has one, so its list is
        unchanged. A lost drone's lane that waits for the operator's
        ``confirm_retask`` is listed in the same shape with ``status``
        ``needs_confirmation`` and why it waits as its ``reason``.
        """
        with self._lock:
            if self._plan is None:
                return None
            out = self._plan.to_dict()
            for zone in out["zones"]:
                state = self._drones[zone["drone_id"]]
                zone["lanes"] = [dict(lane) for lane in state.lanes]
                zone["lost"] = state.lost
            out["uncovered"] = [
                dict(entry, drone_id=chunk.lost_id, reason=chunk.reason)
                for retask in self._retasks
                for chunk in retask.chunks
                if chunk.status == "failed"
                for entry in chunk.lane_entries()
            ] + [
                dict(entry, drone_id=chunk.lost_id, reason=chunk.held, status="waiting")
                for retask in self._retasks
                for chunk in retask.chunks
                if chunk.status == "queued" and chunk.held
                for entry in chunk.lane_entries()
            ] + [
                dict(entry, drone_id=chunk.lost_id, reason=retask.confirm, status="needs_confirmation")
                for retask in self._retasks
                for chunk in retask.chunks
                if chunk.status == "queued" and chunk.confirm
                for entry in chunk.lane_entries()
            ] + [dict(area) for area in self._plan.uncovered_areas]
            out["uncovered"] += self._obstacle_uncovered_locked()
            return out

    def detections(self) -> list[FleetDetection]:
        with self._lock:
            return list(self._detections)

    def clock_sync(self, drone_id: str) -> ClockSync:
        with self._lock:
            state = self._drones[drone_id]
            return ClockSync(drone_id, state.offset_s, state.rtt_s, state.sync_samples)

    def skew_ms(self) -> float | None:
        """Spread between the earliest and latest ``start_at`` execution, in ms.

        Computed from the ``executed_at_ground_s`` values in the edges'
        ``start_at`` acks, which are already in ground time. ``None`` until
        at least one ack has arrived.
        """
        with self._lock:
            executed = [s.executed_at_ground_s for s in self._drones.values() if s.executed_at_ground_s is not None]
        if not executed:
            return None
        return (max(executed) - min(executed)) * 1000.0

    def status(self) -> dict[str, dict]:
        """Per-drone summary for the dashboard ``fleet_status`` event."""
        with self._lock:
            out = {}
            uncovered: list[dict] | None = None
            for drone_id, state in self._drones.items():
                telemetry = state.telemetry or {}
                entry: dict[str, Any] = {
                    "drone_id": drone_id,
                    "alt_cm": state.zone.alt_cm if state.zone else None,
                    "battery": telemetry.get("battery", state.ack_battery),
                    "height_cm": telemetry.get("height_cm"),
                    "is_flying": telemetry.get("is_flying"),
                    "heading_deg": telemetry.get("heading_deg"),
                    "lane_index": telemetry.get("lane_index"),
                    "lanes_total": telemetry.get("lanes_total", len(state.lanes)),
                    "offset_ms": state.offset_s * 1000.0 if state.offset_s is not None else None,
                    "rtt_ms": state.rtt_s * 1000.0 if state.rtt_s is not None else None,
                    "skew_ms": (
                        (state.executed_at_ground_s - self._started_at) * 1000.0
                        if state.executed_at_ground_s is not None and self._started_at is not None
                        else None
                    ),
                    "synced": state.offset_s is not None,
                    "acked": state.acked,
                    "lost": state.lost,
                    "drone_link_ok": telemetry.get("drone_link_ok"),
                    "drone_link_detail": telemetry.get("drone_link_detail"),
                }
                if "move_refusals" in telemetry:
                    # Moves the drone refused (flightrisk/drone/flight_guards.py),
                    # only once there was one, so other status is unchanged.
                    entry["move_refusals"] = telemetry.get("move_refusals")
                if state.obstacle_cuts or any(key in telemetry for key in TELEMETRY_FIELDS):
                    # Only for a drone that reports obstacle sensing, so the
                    # status of a fleet without it (and the mobile contract
                    # fixtures built from it) is unchanged.
                    entry.update({key: telemetry.get(key) for key in _STATUS_OBSTACLE_KEYS})
                    # Whether the drone's gates still have the person cue, or rely on depth alone.
                    entry[TELEMETRY_PERSON_CUE] = _person_cue(telemetry.get(TELEMETRY_PERSON_CUE))
                    # What current_plan() lists for the drone: a re-flown
                    # segment no longer counts, and what a re-flight left of
                    # one counts for the drone that cut it.
                    if uncovered is None:
                        uncovered = self._obstacle_uncovered_locked()
                    entry["uncovered_cm"] = sum(u["distance_cm"] for u in uncovered if u["drone_id"] == drone_id)
                    entry["reflights"] = self._reflight_counts_locked(drone_id)
                # The temperature check before the drone's last takeoff and the live reading, only from an edge that reports them.
                temp = temp_status(telemetry.get(TELEMETRY_TEMP))
                if temp is not None:
                    entry[TELEMETRY_TEMP] = temp
                rel_x = _number(telemetry.get("rel_x_cm"))
                rel_y = _number(telemetry.get("rel_y_cm"))
                if rel_x is not None and rel_y is not None:
                    origin = self._origin(state)
                    entry["rel_x_cm"] = origin[0] + rel_x
                    entry["rel_y_cm"] = origin[1] + rel_y
                out[drone_id] = entry
            return out

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan(self) -> FleetPlan:
        """Split the area into one zone per link and send ``assign_lanes``.

        Zones are side by side across the width, separated by
        ``zone_gap_cm``, and each takes the next altitude slot in turn. The
        lanes come from ``split_parallel_track_zones``; the leading sideways
        offset it adds for shared-launch fleets is dropped because each drone
        takes off at its own zone origin.

        Planning starts a new mission: acks, start state, loss marks,
        detections and a previous ``land_all`` are cleared. Clock sync
        results are kept. Planning is refused with ``RuntimeError`` while any
        drone's telemetry from the last ``TELEMETRY_FRESH_S`` seconds says it
        is flying, because replacing a flying drone's lanes is unsafe.
        """
        self._refuse_plan_while_flying()
        drone_ids = list(self._links)
        n = len(drone_ids)
        zone_w = (self._width_cm - self._zone_gap_cm * (n - 1)) // n
        if zone_w < TELLO_MIN_MOVE_CM:
            raise ValueError(
                f"area width {self._width_cm} cm is too narrow for {n} zones with "
                f"{self._zone_gap_cm} cm gaps"
            )
        zone_waypoints = split_parallel_track_zones(
            n, width_cm=zone_w * n, depth_cm=self._depth_cm, strip_width_cm=self._strip_cm
        )
        # Zone 0 carries no sideways offset, so its length is the length of
        # one zone's pattern; later zones carry an offset prefix to drop.
        pattern_len = len(zone_waypoints[0])
        zones = []
        for i, drone_id in enumerate(drone_ids):
            waypoints = zone_waypoints[i][len(zone_waypoints[i]) - pattern_len:]
            lanes = tuple(_lane(wp.direction, wp.distance_cm) for wp in waypoints)
            zones.append(ZoneAssignment(
                drone_id=drone_id,
                x_cm=i * (zone_w + self._zone_gap_cm),
                y_cm=0,
                w_cm=zone_w,
                h_cm=self._depth_cm,
                alt_cm=self._zone_alt_cm(drone_id, i),
                lanes=lanes,
            ))
        plan = FleetPlan(
            zones=tuple(zones),
            width_cm=self._width_cm,
            depth_cm=self._depth_cm,
            gap_cm=self._zone_gap_cm,
            strip_cm=self._strip_cm,
        )
        return self._install_plan(plan)

    def plan_zones(
        self,
        zones: tuple[ZoneAssignment, ...] | list[ZoneAssignment],
        *,
        grid_cm: tuple[int, int, int, int],
        gap_cm: int,
        strip_cm: int,
        uncovered_areas: tuple[dict, ...] | list[dict] = (),
        details: Mapping[str, Any] | None = None,
    ) -> FleetPlan:
        """Install zones made elsewhere (a plan built on the map) and send their ``assign_lanes``.

        The caller has already checked the zones (``flightrisk.drone.plan_edit``).
        There must be exactly one zone per link. ``grid_cm`` is the search
        area ``(x, y, w, h)`` on the shared grid. Otherwise this behaves like
        ``plan()``: it is refused while a drone flies, starts a new mission
        and sends each drone its lanes.
        """
        self._refuse_plan_while_flying()
        zones = tuple(zones)
        ids = [zone.drone_id for zone in zones]
        if sorted(ids) != sorted(self._links) or len(set(ids)) != len(ids):
            raise ValueError(f"the zones must cover exactly the fleet's drones {sorted(self._links)}, got {ids}")
        x, y, w, h = (int(v) for v in grid_cm)
        plan = FleetPlan(
            zones=zones,
            width_cm=w,
            depth_cm=h,
            gap_cm=int(gap_cm),
            strip_cm=int(strip_cm),
            grid_x_cm=x,
            grid_y_cm=y,
            uncovered_areas=tuple(dict(area) for area in uncovered_areas),
            details=details,
        )
        return self._install_plan(plan)

    def zone_alt_cm(self, drone_id: str, index: int) -> int:
        """The altitude slot ``plan()`` would give *drone_id* as zone *index*."""
        return self._zone_alt_cm(drone_id, index)

    def _refuse_plan_while_flying(self) -> None:
        """Raise ``RuntimeError`` while fresh telemetry says a drone flies."""
        with self._lock:
            now = self._clock()
            flying = [
                drone_id for drone_id, state in self._drones.items()
                if state.telemetry_at is not None
                and now - state.telemetry_at <= TELEMETRY_FRESH_S
                and (state.telemetry or {}).get("is_flying") is True
            ]
        if flying:
            raise RuntimeError(f"cannot plan while drones are flying: {', '.join(flying)}")

    def _install_plan(self, plan: FleetPlan) -> FleetPlan:
        """Make *plan* the mission and send every drone its ``assign_lanes``."""
        zones = plan.zones
        messages = {}
        with self._lock:
            self._plan = plan
            self._started_at = None
            self._landed_all = False
            self._detections = []
            self._next_detection_id = 1
            for retask in self._retasks:
                # A re-task of the previous mission is never reported again.
                retask.reported = True
                retask.settled.set()
            self._retasks = []
            # The previous mission's re-flights are dropped with their cuts.
            self._reflights = []
            for zone in zones:
                state = self._drones[zone.drone_id]
                state.zone = zone
                state.lanes = [dict(lane) for lane in zone.lanes]
                state.lost = False
                state.lost_at = None
                state.offer = None
                state.nack_reason = None
                state.executed_at_ground_s = None
                state.retask_starts = []
                state.seen_flying = False
                self._reset_obstacles_locked(state)
                self._clear_ack(state)
                messages[zone.drone_id] = self._assign_message(zone.drone_id, state)
        for drone_id, msg in messages.items():
            self._send_logged(drone_id, msg)
        return plan

    def _zone_alt_cm(self, drone_id: str, index: int) -> int:
        """The altitude slot of zone *index*, flown by *drone_id*."""
        if self._alt_slot_of is not None:
            return int(self._alt_slot_of(drone_id, index))
        return self._alt_slots_cm[index % len(self._alt_slots_cm)]

    @staticmethod
    def _clear_ack(state: _DroneState) -> None:
        state.acked = False
        state.ack_battery = None
        state.ack_lanes_total = None

    @staticmethod
    def _assign_message(drone_id: str, state: _DroneState) -> dict:
        zone = state.zone
        assert zone is not None
        return {
            "type": "assign_lanes",
            "drone_id": drone_id,
            "alt_cm": zone.alt_cm,
            "lanes": [dict(lane) for lane in state.lanes],
            "zone": zone.zone_dict(),
        }

    @staticmethod
    def _origin(state: _DroneState) -> tuple[int, int]:
        if state.zone is None:
            return 0, 0
        return state.zone.origin

    # ------------------------------------------------------------------
    # Clock sync
    # ------------------------------------------------------------------

    def sync(self, rounds: int = SYNC_ROUNDS, timeout_s: float = 1.0) -> dict[str, ClockSync]:
        """Run ``rounds`` ping/pong exchanges with each link and send ``clock``.

        Each round yields ``offset = ((t2 - t1) + (t3 - t4)) / 2`` (edge clock
        minus ground clock) and ``rtt = (t4 - t1) - (t3 - t2)``. The medians
        over the rounds that answered within ``timeout_s`` are stored and the
        offset is sent unchanged as ``{"type": "clock", "offset_s": ...}``.
        A link with no answered round, or whose ``clock`` message cannot be
        sent, is left unsynced, which blocks ``start()``.

        This call blocks; it must not run on the thread that delivers the
        links' incoming messages unless the links reply from inside ``send``.
        """
        return {
            drone_id: self._sync_link(drone_id, link, rounds, timeout_s)
            for drone_id, link in self._links.items()
        }

    def _sync_link(self, drone_id: str, link: EdgeLink, rounds: int, timeout_s: float) -> ClockSync:
        state = self._drones[drone_id]
        offsets: list[float] = []
        rtts: list[float] = []
        for _ in range(rounds):
            event = threading.Event()
            with self._lock:
                t1 = self._clock()
                state.ping_t1 = t1
                state.ping_event = event
                state.pong = None
            try:
                link.send({"type": "ping", "t1": t1})
            except Exception:
                logger.warning("Ping to %s failed", drone_id, exc_info=True)
                continue
            if not event.wait(timeout_s):
                logger.debug("Ping to %s timed out after %.3f s", drone_id, timeout_s)
                continue
            with self._lock:
                pong = state.pong
            if pong is None:
                continue
            t2, t3, t4 = pong
            offsets.append(((t2 - t1) + (t3 - t4)) / 2.0)
            rtts.append((t4 - t1) - (t3 - t2))

        with self._lock:
            state.ping_t1 = None
            state.ping_event = None
            state.pong = None
            state.offset_s = None
            state.rtt_s = None
            state.sync_samples = len(offsets)
        if not offsets:
            logger.warning("Clock sync with %s failed: no pong received", drone_id)
            return ClockSync(drone_id, None, None, 0)

        offset_s = statistics.median(offsets)
        rtt_s = statistics.median(rtts)
        if not self._send_logged(drone_id, {"type": "clock", "offset_s": offset_s}):
            return ClockSync(drone_id, None, None, len(offsets))
        with self._lock:
            state.offset_s = offset_s
            state.rtt_s = rtt_s
        logger.info(
            "Clock sync %s: offset %.3f ms, rtt %.3f ms over %d rounds",
            drone_id, offset_s * 1000.0, rtt_s * 1000.0, len(offsets),
        )
        return ClockSync(drone_id, offset_s, rtt_s, len(offsets))

    # ------------------------------------------------------------------
    # Scheduled start
    # ------------------------------------------------------------------

    def start(self, lead_s: float = 2.0) -> StartResult:
        """Send one ``start_at`` to every active drone, ``lead_s`` from now.

        Refused unless a plan exists, the fleet has not already started, no
        ``land_all`` has been issued since the plan, and every drone not
        marked lost is clock-synced, has acked its current ``assign_lanes``
        and has a battery of at least ``min_battery`` percent. The battery is
        taken from telemetry no older than ``TELEMETRY_FRESH_S`` when there is
        some, and otherwise from the ``assign_lanes`` ack. A drone whose
        telemetry from that window says ``drone_link_ok: false`` (its edge
        cannot reach the drone) is refused with the edge's text, for example
        ``Tello unreachable (no state for 12 s)``; telemetry without the
        field changes nothing. So is a drone whose telemetry from that window
        says ``obstacle_sensor_ok: false``, unless its ``obstacle_sensing`` is
        ``off`` (it flies unsensed on purpose, with a warning) or
        ``autopilot`` (the autopilot avoids obstacles itself).

        If a ``start_at`` cannot be sent, no further ``start_at`` is sent,
        ``land_all()`` is issued to abort and the result is not ok.
        """
        if lead_s <= 0:
            raise ValueError(f"lead_s must be positive, got {lead_s!r}")
        with self._lock:
            refusals = self._start_refusals()
            if refusals:
                logger.warning("Start refused: %s", refusals)
                return StartResult(False, None, refusals)
            t_ground_s = self._clock() + lead_s
            self._started_at = t_ground_s
            targets = [drone_id for drone_id, state in self._drones.items() if not state.lost]

        failures = {}
        for drone_id in targets:
            try:
                self._links[drone_id].send({"type": "start_at", "t_ground_s": t_ground_s})
            except Exception as exc:
                logger.error("start_at to %s failed: %s", drone_id, exc)
                failures[drone_id] = f"start_at send failed ({exc}); land_all sent to abort"
                break

        if failures:
            self.land_all()
            return StartResult(False, t_ground_s, failures)
        if self._landed_all:
            # land_all() ran while start_at was being sent. Repeat it so the
            # emergency landing is the last command every edge receives.
            self.land_all()
            return StartResult(False, t_ground_s, {"fleet": "land_all issued during start"})
        return StartResult(True, t_ground_s, {})

    def _start_refusals(self) -> dict[str, str]:
        if self._plan is None:
            return {"fleet": "no plan; call plan() first"}
        if self._landed_all:
            return {"fleet": "land_all issued; plan again before starting"}
        if self._started_at is not None:
            return {"fleet": "already started"}
        active = {drone_id: state for drone_id, state in self._drones.items() if not state.lost}
        if not active:
            return {"fleet": "no active drones"}
        now = self._clock()
        refusals = {}
        for drone_id, state in active.items():
            if state.offset_s is None:
                refusals[drone_id] = "clock not synced"
                continue
            if state.nack_reason is not None:
                refusals[drone_id] = f"assign_lanes refused ({state.nack_reason})"
                continue
            if not state.acked:
                refusals[drone_id] = "no assign_lanes ack"
                continue
            battery = state.ack_battery
            if state.telemetry_at is not None and now - state.telemetry_at <= TELEMETRY_FRESH_S:
                telemetry = state.telemetry or {}
                if telemetry.get("drone_link_ok") is False:
                    # The edge cannot reach its drone, so its battery is a
                    # cached value and a start_at would be refused anyway.
                    refusals[drone_id] = _drone_link_refusal(telemetry)
                    continue
                too_hot = live_refusal(temp_status(telemetry.get(TELEMETRY_TEMP)))
                if too_hot is not None:
                    # The drone's live temperature (its newest fresh state
                    # packet) is at or above its edge's refusal level. A drone
                    # on the ground would be refused its takeoff by the edge
                    # anyway; one already in the air (after a manual takeoff)
                    # is refused here too, so a start never sends a drone that
                    # hot on its lanes.
                    refusals[drone_id] = too_hot
                    continue
                sensing = telemetry.get(TELEMETRY_SENSING)
                if telemetry.get(TELEMETRY_SENSOR_OK) is False and sensing not in (SENSING_OFF, SENSING_AUTOPILOT):
                    # The edge needs its sensor (stepped or forward-only
                    # sensing, or a mode this ground does not know) and
                    # cannot sense: refuse here with the reason.
                    refusals[drone_id] = _obstacle_sensor_refusal(telemetry)
                    continue
                if sensing == SENSING_OFF:
                    logger.warning("%s flies its lanes unsensed (obstacle sensing is off)", drone_id)
                fresh = _number(telemetry.get("battery"))
                if fresh is not None:
                    battery = fresh
            if battery is None:
                refusals[drone_id] = "no battery report"
            elif battery < self._min_battery:
                refusals[drone_id] = f"battery {battery:g}% below {self._min_battery:g}%"
        return refusals

    # ------------------------------------------------------------------
    # Incoming messages
    # ------------------------------------------------------------------

    def handle_message(self, drone_id: str, msg: dict) -> None:
        """Dispatch one message from ``drone_id``'s edge.

        Messages whose ``drone_id`` field names a different drone are
        ignored, so a misrouted ack can never satisfy another drone's start
        gate. Unknown types are ignored.
        """
        if drone_id not in self._drones:
            logger.warning("Ignoring message for unknown drone %r", drone_id)
            return
        if not isinstance(msg, dict):
            logger.warning("Ignoring non-object message from %s: %r", drone_id, msg)
            return
        claimed = msg.get("drone_id")
        if claimed is not None and claimed != drone_id:
            logger.warning("Ignoring message on %s's link that claims drone_id %r", drone_id, claimed)
            return
        msg_type = msg.get("type")
        if msg_type == "pong":
            self._on_pong(drone_id, msg)
        elif msg_type == "ack":
            self._on_ack(drone_id, msg)
        elif msg_type == "nack":
            self._on_nack(drone_id, msg)
        elif msg_type == "telemetry":
            messages: list[tuple[str, dict]] = []
            events: list[tuple[str, dict]] = []
            # Asked before the lock: the answer comes from the dashboard's manual control.
            by_hand = self._flown_by_hand(drone_id)
            with self._lock:
                state = self._drones[drone_id]
                cue_before = _person_cue((state.telemetry or {}).get(TELEMETRY_PERSON_CUE))
                reland = self._reland_lost_locked(drone_id, state, msg, by_hand=by_hand)
                state.telemetry = dict(msg)
                state.telemetry_at = self._clock()
                if msg.get("is_flying") is True:
                    state.seen_flying = True
                events.extend(self._note_cuts_locked(drone_id, state, state.telemetry))
                events.extend(self._note_avoid_locked(drone_id, state, state.telemetry))
                # A re-flight this drone flies may have ended, or one of its
                # own cuts may now be offered (design section 3.4).
                reflight = self._track_reflights_locked(drone_id, state)
                held = any(chunk.status == "queued" and chunk.held
                           for retask in self._retasks for chunk in retask.chunks)
                if self._release_deferred_locked(drone_id, state.telemetry) or reflight or held:
                    messages = self._dispatch_locked()
                    events.extend(self._settle_and_collect_locked())
                else:
                    # A re-flight may have changed without a dispatch (an
                    # edge restart fails its cutter's waiting chunks).
                    for chunk in self._reflights:
                        events.extend(self._reflight_events_locked(chunk))
            if reland is not None:
                self._send_reland(drone_id, reland)
            self._emit_all(events)
            self._send_offers(messages)
            self._log_person_cue(drone_id, cue_before, _person_cue(msg.get(TELEMETRY_PERSON_CUE)))
        elif msg_type == "detections":
            self.on_detection(drone_id, msg)
        else:
            logger.debug("Ignoring %r message from %s", msg_type, drone_id)

    def _flown_by_hand(self, drone_id: str) -> bool:
        """True while ``manually_controlled`` says a person flies *drone_id* from the ground. Call it without the lock.

        A check that fails counts as False and is logged: the drone's own
        ``control_mode`` still shows a drone flown by hand
        (``_reland_lost_locked``).
        """
        check = self._manually_controlled
        if check is None:
            return False
        try:
            return bool(check(drone_id))
        except Exception:
            logger.warning("manually_controlled check failed for %s", drone_id, exc_info=True)
            return False

    def _reland_lost_locked(self, drone_id: str, state: _DroneState, msg: dict, *, by_hand: bool) -> str | None:
        """Why a ``land`` must be sent again to lost *drone_id*, whose new telemetry *msg* shows it flying, or None.

        A lost drone was sent one best-effort ``land`` (``on_drone_lost``),
        which a dead link may have dropped. When its link comes back while it
        is still in the air, it could fly lanes that now belong to another
        drone, or wait for the operator's Re-task, so it is sent ``land``
        again, and the reason is returned:

        * ``flying_again``: the first telemetry after the loss that shows it
          flying;
        * ``link_back``: such telemetry after a gap longer than
          ``TELEMETRY_FRESH_S`` (the link came back once more);
        * ``autonomous_again``: such telemetry that reports autonomous
          control after one that did not (its edge went back to its search
          by itself).

        Never while a person flies it by hand: while *msg* reports ``manual``
        control, or while *by_hand* (it is the drone the ground's manual
        control has, ``manually_controlled``). A ``land`` then could put it
        down in the middle of a manoeuvre, over water or a road. Telemetry
        that reports it ``manual``, or ``held`` while *by_hand* (the edge's
        dead-man between two steps), counts as the landing being taken care
        of, so telemetry that follows it without a gap sends nothing either:
        for example ``held`` after the person's browser went away, where the
        orphan countdown of the manual control lands the drone. Telemetry
        that says ``autonomous`` while *by_hand* (sent before the edge took
        the hold, a hold that never arrived, or an edge that left manual
        control) sends nothing, but the next flying report once the
        dashboard has let go of the drone is sent ``land`` (``flying_again``).
        A drone nobody here controls that reports ``held`` or
        ``autonomous`` control, or no control mode, is sent ``land`` as
        above. Telemetry that keeps arriving from a drone still flying sends
        nothing more. Caller holds the lock; the caller sends the ``land``
        without it (``_send_reland``).
        """
        if not state.lost or msg.get("is_flying") is not True:
            return None
        mode = msg.get("control_mode")
        if by_hand or mode == "manual":
            if mode in ("manual", "held"):
                state.relanded = True
            elif mode == "autonomous":
                # The edge left manual control while the dashboard still has
                # it: the first flying report after the dashboard lets go lands it.
                state.relanded = False
            return None
        gap = state.telemetry_at is None or self._clock() - state.telemetry_at > TELEMETRY_FRESH_S
        if not state.relanded:
            reason = "flying_again"
        elif gap:
            reason = "link_back"
        elif mode == "autonomous" and (state.telemetry or {}).get("control_mode") != "autonomous":
            reason = "autonomous_again"
        else:
            return None
        state.relanded = True
        return reason

    def _send_reland(self, drone_id: str, reason: str) -> None:
        """Send lost *drone_id* the ``land`` that ``_reland_lost_locked`` asked for, and tell the operator why.

        The ``lost_drone_land_resent`` flight-safety event is logged after
        the send, with ``outcome`` ``sent`` or ``not_delivered``. The
        ``lost_drone_relanded`` fleet event names the drone and says why the
        ground landed it. When the ``land`` cannot be sent, nothing is
        announced and the next telemetry that shows the drone flying tries
        again. Call it without the lock.
        """
        why = {
            "flying_again": "reports flying again",
            "link_back": "reports flying again after its link dropped once more",
            "autonomous_again": "reports autonomous control again while it flies",
        }[reason]
        detail = (f"{drone_id} was declared lost and {why}; the ground sent it land, because its unflown lanes "
                  f"are no longer its own")
        sent = self._send_logged(drone_id, {"type": "land"})
        obs_events.flight_safety(
            "lost_drone_land_resent", reason=reason, drone_id=drone_id, component="coordinator",
            outcome="sent" if sent else "not_delivered", detail=detail,
        )
        if not sent:
            with self._lock:
                state = self._drones[drone_id]
                if state.lost:
                    state.relanded = False
            return
        self._emit("fleet_event", {"event": "lost_drone_relanded", "drone_id": drone_id, "reason": reason,
                                   "detail": detail})

    @staticmethod
    def _log_person_cue(drone_id: str, before: dict | None, after: dict | None) -> None:
        """Log once when a drone's obstacle person cue goes down, and once when it works again."""
        was_down = bool(before and before[PERSON_CUE_DOWN_KEY])
        is_down = bool(after and after[PERSON_CUE_DOWN_KEY])
        if is_down and not was_down:
            age = after[PERSON_CUE_AGE_KEY] if after else None
            logger.warning(
                "%s's obstacle person cue is down (newest detection %s): its gates rely on the depth cue alone",
                drone_id, "none yet" if age is None else f"{age:.1f} s old",
            )
        elif was_down and after is not None and not is_down:
            logger.info("%s's obstacle person cue works again", drone_id)

    def _on_pong(self, drone_id: str, msg: dict) -> None:
        t4 = self._clock()
        t1, t2, t3 = (_number(msg.get(key)) for key in ("t1", "t2", "t3"))
        if t1 is None or t2 is None or t3 is None:
            logger.warning("Malformed pong from %s: %r", drone_id, msg)
            return
        with self._lock:
            state = self._drones[drone_id]
            if state.ping_t1 is None or abs(t1 - state.ping_t1) > 1e-6:
                return  # A late answer to a round that already timed out.
            state.pong = (t2, t3, t4)
            event = state.ping_event
        if event is not None:
            event.set()

    def _on_ack(self, drone_id: str, msg: dict) -> None:
        kind = msg.get("for")
        messages: list[tuple[str, dict]] = []
        events: list[tuple[str, dict]] = []
        with self._lock:
            state = self._drones[drone_id]
            if kind == "assign_lanes":
                lanes_total = msg.get("lanes_total")
                if isinstance(lanes_total, int) and not isinstance(lanes_total, bool) \
                        and lanes_total != len(state.lanes):
                    logger.warning(
                        "Ignoring stale assign_lanes ack from %s: %d lanes acked, %d assigned",
                        drone_id, lanes_total, len(state.lanes),
                    )
                    return
                state.acked = True
                state.ack_battery = _number(msg.get("battery"))
                state.ack_lanes_total = lanes_total if isinstance(lanes_total, int) else None
                state.nack_reason = None
                if state.offer is not None:
                    # The acked plan is the one that carries the pending
                    # re-task offer, so its lanes are now covered.
                    offer, state.offer = state.offer, None
                    if len(offer.base_lanes) < len(state.lanes):
                        # The edge flies the appended lanes from where it is
                        # when it reaches them: its replay restarts there.
                        state.retask_starts.append(len(offer.base_lanes))
                    for chunk in offer.chunks:
                        chunk.status = "acked"
                        if chunk.kind == "reflight":
                            # Accepted once: this chunk is never queued again.
                            chunk.flown_by = drone_id
                    logger.info("%s accepted %d re-tasked lanes", drone_id,
                                sum(len(chunk.lanes) for chunk in offer.chunks))
                    messages = self._dispatch_locked()
                    events = self._settle_and_collect_locked()
            elif kind == "start_at":
                executed = _number(msg.get("executed_at_ground_s"))
                if executed is None:
                    logger.warning("start_at ack from %s has no executed_at_ground_s", drone_id)
                    return
                state.executed_at_ground_s = executed
            else:
                logger.debug("Ignoring ack for %r from %s", kind, drone_id)
        self._emit_all(events)
        self._send_offers(messages)

    def _on_nack(self, drone_id: str, msg: dict) -> None:
        """A drone refused an ``assign_lanes``.

        A refused re-task offer is withdrawn: the drone keeps its previous
        lanes and the offered lanes go to another eligible drone, or are
        reported uncovered. A refused plan from ``plan()`` blocks ``start()``
        with the edge's reason.
        """
        if msg.get("for") in ("start_at", "takeoff") and msg.get("reason") == "drone_unreachable":
            # The edge refused to take off because it cannot reach its drone
            # (a v2 edge only; see docs/fleet-protocol-v2.md). The drone
            # stays on the ground; loss detection re-tasks its lanes as for
            # any drone that does not take off.
            detail = f"{drone_id}: {_drone_link_refusal(msg, key='detail')}"
            logger.warning("%s refused %s: %s", drone_id, msg.get("for"), detail)
            self._emit("fleet_event", {"event": "start_refused", "detail": detail})
            return
        if msg.get("for") != "assign_lanes":
            logger.debug("Ignoring nack for %r from %s", msg.get("for"), drone_id)
            return
        reason = msg.get("reason")
        reason = reason if isinstance(reason, str) and reason else "refused"
        described = self._describe_refusal(drone_id, reason, msg)
        lanes_total = msg.get("lanes_total")
        messages: list[tuple[str, dict]] = []
        events: list[tuple[str, dict]] = []
        with self._lock:
            state = self._drones[drone_id]
            if isinstance(lanes_total, int) and not isinstance(lanes_total, bool) \
                    and lanes_total != len(state.lanes):
                logger.warning(
                    "Ignoring stale assign_lanes nack from %s: %d lanes refused, %d assigned",
                    drone_id, lanes_total, len(state.lanes),
                )
                return
            logger.warning("%s refused its assign_lanes (%s)", drone_id, described)
            reported = _number(msg.get("geofence_cm")) if reason == "geofence" else None
            if reported is not None and reported > 0:
                state.reported_fence_cm = (
                    reported if state.reported_fence_cm is None else min(state.reported_fence_cm, reported)
                )
            if state.offer is not None:
                # Whether the refusal is transient depends on the bare reason;
                # what is recorded and shown is the described one.
                self._withdraw_offer_locked(drone_id, described, transient=reason in TRANSIENT_NACKS)
                messages = self._dispatch_locked()
                events = self._settle_and_collect_locked()
            else:
                self._clear_ack(state)
                state.nack_reason = described
        self._emit_all(events)
        self._send_offers(messages)

    @staticmethod
    def _describe_refusal(drone_id: str, reason: str, msg: dict) -> str:
        """The nack *reason*, followed by what a ``geofence`` nack says about the fence.

        A ``geofence`` nack may carry the optional ``NACK_GEOFENCE_FIELDS``
        (all relative to the drone's takeoff point): where the refused path
        starts, the first point of it outside the fence and the fence size.
        They are shown so the operator sees why, for example a drone that is
        far from its takeoff point. Without them the bare reason is returned.
        """
        if reason != "geofence":
            return reason
        x, y = _number(msg.get("x_cm")), _number(msg.get("y_cm"))
        fence = _number(msg.get("geofence_cm"))
        here_x, here_y = _number(msg.get("rel_x_cm")), _number(msg.get("rel_y_cm"))
        parts = []
        if x is not None and y is not None:
            outside = f"the path would reach ({x:g}, {y:g}) cm from {drone_id}'s takeoff point"
            if fence is not None:
                outside += f", outside its {fence:g} cm geofence"
            parts.append(outside)
        elif fence is not None:
            parts.append(f"{drone_id}'s geofence is {fence:g} cm")
        if here_x is not None and here_y is not None:
            parts.append(f"{drone_id} is at ({here_x:g}, {here_y:g}) cm from its takeoff point")
        return reason + (": " + "; ".join(parts) if parts else "")

    # ------------------------------------------------------------------
    # Obstacle cuts
    # ------------------------------------------------------------------

    @staticmethod
    def _reset_obstacles_locked(state: _DroneState) -> None:
        """Forget the cuts of the previous plan.

        The edge keeps counting cuts across plans, so the count last reported
        becomes the plan's starting point: a telemetry message from before the
        edge accepted the new plan still lists the old plan's cuts, and those
        are numbered at or below it.
        """
        state.obstacle_cuts = {}
        state.obstacle_plan_seq = state.obstacle_key_base + state.obstacle_seq
        state.avoid_offsets = {}
        state.avoid_held = False
        state.avoid_offset_seen = {}

    @staticmethod
    def _listed_cuts(telemetry: dict, seq: int) -> list[tuple[int, Cut]]:
        """The cuts a telemetry message lists, as (cut number, cut), oldest first.

        The listed cuts are the edge's last ones, so the last is number
        *seq* and each earlier one is one less. A pair that is not two
        numbers is dropped without shifting the numbers of the others. An
        edge lists at most ``MAX_CUTS_LISTED`` cuts, so only that many of
        the last pairs are read.
        """
        flat = telemetry.get(TELEMETRY_CUTS)
        if not isinstance(flat, (list, tuple)):
            return []
        text = telemetry.get(TELEMETRY_CUT_REASONS)
        names = text.split(",") if isinstance(text, str) else []
        count = len(flat) // 2
        listed = []
        for k in range(max(0, count - MAX_CUTS_LISTED), count):
            name = names[k] if k < len(names) else None
            decoded = decode_cuts(flat[2 * k:2 * k + 2], name)
            if decoded:
                listed.append((seq - (count - 1 - k), decoded[0]))
        return listed

    @staticmethod
    def _retask_starts_locked(state: _DroneState, appended_at: int | None = None) -> list[int]:
        """Where re-tasks appended lanes to the drone's lanes: accepted, pending, and *appended_at*.

        A pending offer already put its lanes in ``state.lanes``, so its
        start counts too; *appended_at* is where lanes a dispatch is about to
        offer would start.
        """
        starts = list(state.retask_starts)
        if state.offer is not None:
            starts.append(len(state.offer.base_lanes))
        if appended_at is not None:
            starts.append(appended_at)
        return starts

    def _replay_locked(
        self, state: _DroneState, lanes: list[dict] | None = None, appended_at: int | None = None,
    ) -> LaneReplay | None:
        """``lane_steps.replay_lanes`` of the drone's lanes (or *lanes*) with its cuts and re-task starts.

        This is exactly the replay the edge makes of the same lanes, cuts
        and re-task starts. None when the lanes cannot be read (no edge
        accepts such lanes, so they never carry cuts).
        """
        lanes = state.lanes if lanes is None else lanes
        cuts = [state.obstacle_cuts[n] for n in sorted(state.obstacle_cuts)]
        try:
            return replay_lanes(lanes, cuts, retask_starts=self._retask_starts_locked(state, appended_at))
        except (KeyError, TypeError, ValueError):
            return None

    def _obstacle_replay_locked(self, state: _DroneState) -> tuple[list[int], list[UncoveredSegment]]:
        """The drone's cuts of this plan and the segments nobody searched, in the drone's frame.

        Returns the cut numbers in the order the cuts happened and
        ``lane_steps.uncovered_segments`` of the drone's current lanes
        (appended lanes included), those cuts and its re-task starts, which
        is exactly what the edge derives. A segment's ``cut`` indexes the cut
        numbers, so ``numbers[segment.cut]`` is the number of the cut that
        produced it or, for a ``skipped`` segment, caused it. Positions are
        relative to the drone's takeoff point; add ``_origin(state)`` for
        the shared grid. Lanes the replay cannot read give no segments rather
        than an error in the telemetry handler.
        """
        numbers = sorted(state.obstacle_cuts)
        replay = self._replay_locked(state)
        return numbers, ([] if replay is None else list(replay.segments))

    def _actual_path_locked(self, drone_id: str, lanes: list[dict] | None = None) -> list[tuple[int, int]]:
        """The shared-grid points of the path *drone_id* actually flies, from its takeoff point.

        *lanes* are the drone's lanes (the default), or its lanes followed by
        lanes a dispatch would append to them. The path is the drone's
        replay (``LaneReplay.path``): a cut lane ends where the drone
        stopped, a lane the deficit rule shortens or skips is flown that
        way, and appended lanes start where the drone actually is. One point
        for the start and one after each lane. A drone without cuts flies
        its lanes as planned, which is also the fallback for lanes the replay
        cannot read.
        """
        state = self._drones[drone_id]
        lanes = state.lanes if lanes is None else lanes
        origin = self._origin(state)
        replay = None
        if state.obstacle_cuts:
            appended_at = len(state.lanes) if len(lanes) > len(state.lanes) else None
            replay = self._replay_locked(state, lanes, appended_at)
        if replay is None:
            points = [origin]
            for lane in lanes:
                points.append(_path_point(points[-1], [lane]))
            return points
        return [(origin[0] + x, origin[1] + y) for x, y in replay.path]

    def _planned_points_locked(self, drone_id: str, lanes: list[dict]) -> list[tuple[int, int]]:
        """Where each of *lanes* (the drone's lanes, maybe before a loss truncated them) is planned on the grid.

        One point per lane start and one for the end (``LaneReplay.planned``):
        the takeoff point plus the planned lanes, except that lanes after a
        re-task start are planned from where the drone was when it got
        there. Without cuts that is simply the planned path.
        """
        state = self._drones[drone_id]
        origin = self._origin(state)
        replay = self._replay_locked(state, lanes) if state.obstacle_cuts else None
        if replay is None:
            points = [origin]
            for lane in lanes:
                points.append(_path_point(points[-1], [lane]))
            return points
        return [(origin[0] + x, origin[1] + y) for x, y in replay.planned]

    def _edge_restarted_locked(self, drone_id: str, state: _DroneState, seq: int) -> None:
        """The edge's cut count went down from ``state.obstacle_seq`` to *seq*: it restarted.

        What it reported before no longer applies, so its cuts are dropped.
        Later cut numbers follow on from the earlier ones
        (``obstacle_key_base``), so the edge's reused counts never name a cut
        the ground already knows. The drone's re-flights still waiting are
        failed: their cuts are no longer reported, and a reused number must
        never be offered twice (review N12). Offers already out keep going.
        """
        logger.warning(
            "%s reports obstacle_seq %d after %d: its edge restarted, so its earlier cuts are dropped",
            drone_id, seq, state.obstacle_seq,
        )
        state.obstacle_key_base += state.obstacle_seq
        state.obstacle_cuts = {}
        state.obstacle_seq = 0
        state.obstacle_plan_seq = state.obstacle_key_base
        for chunk in self._reflights:
            if chunk.lost_id == drone_id and chunk.status == "queued":
                self._fail_reflight_locked(chunk, f"{drone_id}'s edge restarted, so its cuts are no longer known")

    @staticmethod
    def _rebase_cut_numbers_locked(state: _DroneState, listed: list[tuple[int, Cut]], seq: int) -> int:
        """The ``obstacle_key_base`` that numbers *listed* (edge numbering, last one *seq*) after a suspect jump.

        The listed cuts are the last ones of the edge's plan. Those at their
        front that repeat, in order, the last cuts the ground knows are those
        cuts and keep their numbers; the others follow on from the last
        number seen. So a jump in the count alone neither renames nor
        repeats a cut.
        """
        last_seen = state.obstacle_key_base + state.obstacle_seq
        if not listed:
            return last_seen - seq
        known = [(n, state.obstacle_cuts[n]) for n in sorted(state.obstacle_cuts)]
        cuts = [cut for _, cut in listed]
        for overlap in range(min(len(known), len(cuts)), 0, -1):
            if [cut for _, cut in known[-overlap:]] == cuts[:overlap]:
                return known[-overlap][0] - listed[0][0]
        return last_seen + 1 - listed[0][0]

    def _note_cuts_locked(self, drone_id: str, state: _DroneState, telemetry: dict) -> list[tuple[str, dict]]:
        """Record a drone's reported cuts and return one ``lane_blocked`` event per new cut.

        Does nothing for telemetry without a whole, non-negative
        ``obstacle_seq``, and ignores one above ``MAX_OBSTACLE_SEQ``. The
        edge counts cuts for as long as it runs, so a lower count than before
        means it restarted (``_edge_restarted_locked``). ``obstacle_cuts_total``
        says how many cuts the edge's current plan has, which are the last
        ones it counted, so any older cut the ground still holds belongs to a
        plan the edge has left. Every listed cut is kept by its number, so a
        plan with more cuts than one message lists is still replayed in full.

        Only the cuts listed and the cuts already known are ever walked,
        never the range of numbers between two counts, so no count can stall
        the ground (review B4). A count that rose by more than the cuts listed
        plus ``obstacle_cuts_total`` can explain is suspect: it is logged and
        only the listed cuts are used, numbered so that a cut the ground
        already knows keeps its number (``_rebase_cut_numbers_locked``) and
        is never announced or offered twice. Otherwise cuts that no message
        listed are counted and logged.

        A ``lane_blocked`` event names the point where the drone stopped and
        everything the cut left unsearched (the rest of its lane, a
        same-direction run after it, or for ``sensor_down`` every later
        lane), in shared-grid coordinates.
        """
        raw = _number(telemetry.get(TELEMETRY_SEQ))
        if raw is None or raw < 0 or raw != int(raw):
            return []
        if raw > MAX_OBSTACLE_SEQ:
            logger.warning("%s reports obstacle_seq %.0f, above %d: its obstacle cuts are ignored",
                           drone_id, raw, MAX_OBSTACLE_SEQ)
            return []
        seq = int(raw)
        if seq < state.obstacle_seq:
            self._edge_restarted_locked(drone_id, state, seq)
        last_seen = state.obstacle_key_base + state.obstacle_seq
        total_raw = _number(telemetry.get(TELEMETRY_CUTS_TOTAL))
        total = int(total_raw) if total_raw is not None and total_raw >= 0 and total_raw == int(total_raw) else None
        listed = self._listed_cuts(telemetry, seq)
        suspect = state.obstacle_seen and seq - state.obstacle_seq > len(listed) + (total or 0)
        if suspect:
            logger.warning(
                "%s's obstacle_seq jumped from %d to %d, more than its %d listed cuts and obstacle_cuts_total %s "
                "explain; only the listed cuts are used",
                drone_id, state.obstacle_seq, seq, len(listed), total,
            )
            state.obstacle_key_base = self._rebase_cut_numbers_locked(state, listed, seq)
        base = state.obstacle_key_base
        number = base + seq
        oldest = state.obstacle_plan_seq
        if total is not None:
            oldest = max(oldest, number - total)
        for known in [n for n in state.obstacle_cuts if n <= oldest]:
            del state.obstacle_cuts[known]
        for n, cut in listed:
            if base + n > oldest:
                state.obstacle_cuts[base + n] = cut
        state.obstacle_seq = seq
        state.obstacle_seen = True
        floor = max(last_seen, oldest)
        new = sorted(((n, cut) for n, cut in state.obstacle_cuts.items() if n > floor), key=lambda item: item[0])
        if not suspect and number - floor > len(new):
            logger.warning(
                "%s made %d cuts that no telemetry listed; its uncovered segments may be incomplete",
                drone_id, number - floor - len(new),
            )
        if not new:
            return []
        numbers, segments = self._obstacle_replay_locked(state)
        origin_x, origin_y = self._origin(state)
        events = []
        for n, cut in new:
            index = numbers.index(n)
            own = [s for s in segments if s.cut == index and s.reason == cut.reason]
            if not own:
                logger.warning("%s cut lane %d, which its current lanes do not fly; not announced",
                               drone_id, cut.lane_index)
                continue
            x_cm, y_cm = origin_x + own[0].x_cm, origin_y + own[0].y_cm
            unflown = sum(s.length_cm for s in own)
            events.append(("fleet_event", {
                "event": "lane_blocked",
                "drone_id": drone_id,
                "lane_index": cut.lane_index,
                "x_cm": x_cm,
                "y_cm": y_cm,
                "unflown_cm": unflown,
                "reason": cut.reason,
                "detail": (
                    f"{drone_id}: lane {cut.lane_index} stopped at ({x_cm:g}, {y_cm:g}) cm, "
                    f"{unflown:g} cm not searched ({cut.reason})"
                ),
            }))
        self._on_new_cuts_locked(drone_id, state, new)
        return events

    def _on_new_cuts_locked(self, drone_id: str, state: _DroneState, new_cuts: list[tuple[int, Cut]]) -> None:
        """Queue a re-flight for each new cut that is offered (design section 3.4). Caller holds the lock.

        Called once for each telemetry message that reports cuts the ground
        has not seen. ``new_cuts`` are those cuts as ``(obstacle_seq of the
        cut, lane_steps.Cut)``, oldest first; ``state.obstacle_cuts`` already
        holds them, so ``_obstacle_replay_locked(state)`` includes their
        segments.

        A cut is offered when its reason is in ``REFLIGHT_CUT_REASONS``
        (``obstacle`` and ``sensor_stale``; never ``sensor_down``,
        ``autopilot`` or a lone ``skipped`` shadow, which rides with its cut).
        A cut whose reason is in ``CONFIRM_REFLIGHT_CUT_REASONS`` (``person``)
        is taken up the same way but marked ``confirm``: it is never offered
        on its own, because the receiver would fly toward the same person
        from the far side, and waits for the operator's ``confirm_reflight``.
        Either way a cut is taken up only when it is not on lanes this drone
        flies to re-fly another drone's cut
        (that cut makes that re-flight ``blocked_again``, and a segment is
        never offered twice), and while the drone has had fewer than
        ``MAX_REFLIGHTS_PER_DRONE`` cuts taken up in this plan; later cuts are
        only reported. The new chunk waits for the drone to finish its lanes.
        Every waiting chunk of the drone is derived again, because a later
        cut can change which shadows an earlier cut has.

        A re-flight re-tasks a running mission, like the recovery of a lost
        drone, so nothing is offered for a cut reported before ``start()``
        (which no edge flying the plan can make): it is only reported.
        """
        if state.zone is None or self._started_at is None:
            return
        taken = sum(1 for chunk in self._reflights if chunk.lost_id == drone_id and chunk.offset is None)
        for number, cut in new_cuts:
            confirm = cut.reason in CONFIRM_REFLIGHT_CUT_REASONS
            if cut.reason not in REFLIGHT_CUT_REASONS and not confirm:
                continue
            if self._in_reflight_lanes_locked(drone_id, cut.lane_index):
                continue
            if taken >= MAX_REFLIGHTS_PER_DRONE:
                logger.warning(
                    "%s cut lane %d (cut %d), but %d of its cuts are already offered for a re-flight; "
                    "this one is only reported",
                    drone_id, cut.lane_index, number, MAX_REFLIGHTS_PER_DRONE,
                )
                continue
            chunk = _Chunk(
                lost_id=drone_id, first_index=cut.lane_index, lanes=[], points=[],
                kind="reflight", cut_seq=number, confirm=confirm,
            )
            self._refresh_reflight_locked(chunk)
            if not chunk.segments and not chunk.too_short:
                continue  # Nothing of it in the replay; _note_cuts_locked has said so.
            self._reflights.append(chunk)
            taken += 1
            if confirm:
                logger.warning(
                    "%s's cut %d on lane %d (%s) is offered to another drone only once the operator confirms it",
                    drone_id, number, cut.lane_index, cut.reason,
                )
            else:
                logger.info("%s's cut %d on lane %d waits to be offered to another drone",
                            drone_id, number, cut.lane_index)
        for number, cut in new_cuts:
            if cut.reason in CONFIRM_REFLIGHT_CUT_REASONS:
                self._hold_offsets_for_cut_locked(drone_id, cut.lane_index, number)
        for chunk in self._reflights:
            if chunk.lost_id == drone_id and chunk.status == "queued":
                self._refresh_reflight_locked(chunk)

    def _hold_offsets_for_cut_locked(self, drone_id: str, lane: int, number: int) -> None:
        """Make the waiting offset re-flights on *drone_id*'s lane *lane* wait for the person cut *number*.

        A re-flight flies reversed, from the far end of the offset, which is
        where the sidestep met the person. Confirming the cut
        (``confirm_reflight``) releases them with it. Caller holds the lock.
        """
        for chunk in self._reflights:
            if (chunk.lost_id == drone_id and chunk.offset is not None and chunk.offset[0] == lane
                    and chunk.status == "queued"):
                chunk.confirm = True
                chunk.confirm_cut = number
                logger.warning("%s's offset segment on lane %d waits for the operator: the lane was cut for a person",
                               drone_id, lane)

    def _confirm_cut_on_lane_locked(self, state: _DroneState, lane: int) -> int | None:
        """The newest cut of the plan on *lane* whose reason needs the operator's confirmation, or None."""
        numbers = [n for n, cut in state.obstacle_cuts.items()
                   if cut.lane_index == lane and cut.reason in CONFIRM_REFLIGHT_CUT_REASONS]
        return max(numbers) if numbers else None

    def _note_avoid_locked(self, drone_id: str, state: _DroneState, telemetry: dict) -> list[tuple[str, dict]]:
        """Read a drone's ``obstacle_avoid`` (``lane_avoid``): its sidestep hold and its offset segments.

        Returns the fleet events to announce. A ``held`` phase, a drone that
        could not fly back to its lane after a sidestep and now hovers under
        manual control, is announced once as ``sidestep_held``, so the
        operator knows to take control or press Resume search before the
        dashboard's orphan countdown lands it. Each offset segment, a stretch
        of a lane the drone flew beside the lane to get past an obstacle, is
        announced once as ``lane_offset``, listed in
        ``current_plan()["uncovered"]`` with reason ``offset`` and, like a cut
        segment (design section 3.4), taken up for a re-flight by another
        drone. Offsets are read only once the edge acknowledged the current
        plan, so a message from before that never brings the old plan's.

        A person seen during a sidestep reaches the ground two ways, and
        either keeps its offset from being offered without the operator's
        confirmation (a re-flight would start at the far end of the offset,
        where the person was): the lane's ``person`` cut
        (``_hold_offsets_for_cut_locked``), or, when the lane had already
        ended and could not be cut, the offset's own ``offsets_seen``. The
        ``sidestep_held`` banner also says when the way back stopped for a
        person (``held_reason``).
        """
        avoid = telemetry.get(TELEMETRY_AVOID)
        if not isinstance(avoid, dict):
            state.avoid_held = False
            return []
        events: list[tuple[str, dict]] = []
        held = avoid.get(AVOID_PHASE_KEY) == AVOID_PHASE_HELD
        if held and not state.avoid_held:
            x_cm, y_cm = _number(telemetry.get("rel_x_cm")), _number(telemetry.get("rel_y_cm"))
            origin = self._origin(state)
            where = "" if x_cm is None or y_cm is None else (
                f" at ({origin[0] + x_cm:g}, {origin[1] + y_cm:g}) cm")
            held_reason = avoid.get(AVOID_HELD_REASON_KEY)
            seen = " A person was seen on its way back." if held_reason == CUT_PERSON else ""
            logger.warning("%s could not fly back to its lane after a sidestep (%s) and hovers under manual control",
                           drone_id, held_reason or "reason not reported")
            payload = {
                "event": "sidestep_held",
                "drone_id": drone_id,
                "detail": (
                    f"{drone_id} could not fly back to its lane after a sidestep and hovers under manual "
                    f"control{where}.{seen} Take control or press Resume search, or the ground lands it when "
                    "its manual-control countdown runs out."
                ),
            }
            if isinstance(held_reason, str) and held_reason:
                payload["reason"] = held_reason[:MAX_TEXT_LEN]
            events.append(("fleet_event", payload))
        state.avoid_held = held
        if not state.acked:
            return events
        listed = _decode_offsets(avoid.get(AVOID_OFFSETS_KEY), avoid.get(AVOID_OFFSETS_SEEN_KEY))
        for lane, start, _length, seen in listed:
            if seen and (lane, start) in state.avoid_offsets and state.avoid_offset_seen.get((lane, start)) != seen:
                # The sidestep's way back saw it after the offset was first reported.
                state.avoid_offset_seen[(lane, start)] = seen
                self._hold_offset_for_seen_locked(drone_id, lane, start, seen)
        new = [entry for entry in listed if (entry[0], entry[1]) not in state.avoid_offsets]
        if not new:
            return events
        origin = self._origin(state)
        for lane, start, length, seen in new:
            state.avoid_offsets[(lane, start)] = length
            if seen:
                state.avoid_offset_seen[(lane, start)] = seen
            seg = self._offset_segment_locked(state, lane, start, length)
            if seg is None:
                logger.warning("%s flew beside lane %d, which its current lanes do not fly; not announced",
                               drone_id, lane)
                continue
            grid = _on_grid(seg, origin)
            noticed = f"; a {seen.replace('_', ' ')} was seen during that sidestep" if seen == CUT_PERSON else ""
            payload = {
                "event": "lane_offset",
                "drone_id": drone_id,
                "lane_index": lane,
                "x_cm": grid.x_cm,
                "y_cm": grid.y_cm,
                "unflown_cm": grid.length_cm,
                "reason": SEGMENT_OFFSET,
                "detail": (
                    f"{drone_id}: {grid.length_cm} cm of lane {lane} from ({grid.x_cm:g}, {grid.y_cm:g}) cm "
                    f"flown beside the lane to pass an obstacle, not on it{noticed}"
                ),
            }
            if seen:
                payload["seen"] = seen
            events.append(("fleet_event", payload))
            self._offset_reflight_locked(drone_id, state, lane, start, grid, seen)
        return events

    def _hold_offset_for_seen_locked(self, drone_id: str, lane: int, start: int, seen: str) -> None:
        """Make a waiting offset re-flight wait for the operator once its sidestep is known to have seen a person."""
        if seen not in CONFIRM_REFLIGHT_CUT_REASONS:
            return
        for chunk in self._reflights:
            if chunk.lost_id == drone_id and chunk.offset == (lane, start) and chunk.status == "queued":
                chunk.seen = seen
                chunk.confirm = True
                logger.warning("%s's offset segment on lane %d waits for the operator: a %s was seen during its "
                               "sidestep", drone_id, lane, seen)

    def _offset_segment_locked(
        self, state: _DroneState, lane: int, start: int, length: int,
    ) -> UncoveredSegment | None:
        """An offset segment relative to the drone's takeoff point, on the line the lane is flown on, or None.

        The lane starts where the drone's replay says it starts flying it
        (``LaneReplay.path``, the same replay the edge makes), and the
        segment runs from *start* to *start* + *length* along it. None when
        the lanes cannot be replayed or have no such lane.
        """
        replay = self._replay_locked(state)
        if replay is None or not 0 <= lane < len(state.lanes) or lane >= len(replay.path):
            return None
        direction = state.lanes[lane].get("direction") if isinstance(state.lanes[lane], dict) else None
        unit = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}.get(direction)
        if unit is None:
            return None
        x0, y0 = replay.path[lane]
        return UncoveredSegment(
            lane_index=lane, direction=direction,
            x_cm=int(x0 + unit[0] * start), y_cm=int(y0 + unit[1] * start),
            end_x_cm=int(x0 + unit[0] * (start + length)), end_y_cm=int(y0 + unit[1] * (start + length)),
            length_cm=int(length), reason=SEGMENT_OFFSET, cut=-1,
        )

    def _offset_reflight_locked(
        self, drone_id: str, state: _DroneState, lane: int, start: int, grid: UncoveredSegment, seen: str = "",
    ) -> None:
        """Queue a re-flight of one offset segment (*grid*, on the shared grid), as for a cut. Caller holds the lock.

        The limits of a cut's (``_on_new_cuts_locked``): only once the mission
        has started and never for a lane the drone flies to re-fly another
        drone's segments; offsets have their own
        ``MAX_OFFSET_REFLIGHTS_PER_DRONE``. The re-flight flies the offset
        reversed, from its far end, which is where the sidestep met whatever
        ended it, so it needs the operator's confirmation, exactly like a
        person cut, when that sidestep saw a person (*seen*) or the drone
        cut the same lane for a person (the chunk then waits for that cut's
        ``confirm_reflight``, and a person cut that arrives later holds it
        the same way, ``_hold_offsets_for_cut_locked``).
        """
        if state.zone is None or self._started_at is None:
            return
        if self._in_reflight_lanes_locked(drone_id, lane):
            return
        taken = sum(1 for chunk in self._reflights if chunk.lost_id == drone_id and chunk.offset is not None)
        if taken >= MAX_OFFSET_REFLIGHTS_PER_DRONE:
            logger.warning("%s flew %d cm beside lane %d, but %d of its offset segments are already offered for "
                           "a re-flight; this one is only reported", drone_id, grid.length_cm, lane,
                           MAX_OFFSET_REFLIGHTS_PER_DRONE)
            return
        plan = plan_reflight([grid])
        person_cut = self._confirm_cut_on_lane_locked(state, lane)
        confirm = seen in CONFIRM_REFLIGHT_CUT_REASONS or person_cut is not None
        chunk = _Chunk(lost_id=drone_id, first_index=lane, lanes=[dict(x) for x in plan.lanes],
                       points=list(plan.points), kind="reflight", offset=(lane, start), confirm=confirm,
                       seen=seen or None, confirm_cut=person_cut)
        chunk.legs = list(plan.legs)
        chunk.segments = list(plan.segments)
        chunk.too_short = list(plan.too_short)
        self._reflights.append(chunk)
        if confirm:
            logger.warning("%s's offset segment on lane %d (%d cm) is offered to another drone only once the "
                           "operator confirms it: a person was seen there", drone_id, lane, grid.length_cm)
        else:
            logger.info("%s's offset segment on lane %d (%d cm) waits to be offered to another drone",
                        drone_id, lane, grid.length_cm)

    def confirm_reflight(self, drone_id: str, cut_seq: Any, offset: Any = None) -> tuple[bool, str]:
        """The operator confirms that *drone_id*'s cut *cut_seq* may be re-flown by another drone.

        Only a re-flight still waiting for confirmation (``confirm``: a cut
        whose reason is in ``CONFIRM_REFLIGHT_CUT_REASONS``, a person) can be
        confirmed. Confirming a cut also releases the offset segments held
        for it (a sidestep on the same lane, ``_hold_offsets_for_cut_locked``).
        An offset segment whose sidestep saw a person but that has no such
        cut (the lane had ended) is confirmed on its own: *cut_seq* None and
        *offset* ``[lane_index, from_cm]`` (its ``uncovered`` entry's
        ``index`` and ``offset_from_cm``). From then on it is handled exactly
        like an obstacle cut's re-flight: it waits for the cutting drone to
        finish its lanes, then goes to the nearest drone that can take it,
        or fails with the reason (``_offer_reflight_locked``). The offer, if
        any, is sent before this returns, so do not call it on a link's
        delivery thread. Returns ``(ok, detail)``: what happens next, or why
        nothing was confirmed.
        """
        if cut_seq is None and offset is not None:
            key = _offset_key(offset)
            if key is None:
                return False, f"invalid offset segment {offset!r}"
            with self._lock:
                chunk = next((c for c in self._reflights if c.lost_id == drone_id and c.offset == key), None)
                if chunk is None:
                    return False, f"{drone_id} has no offset segment at {key[1]} cm on lane {key[0]} to re-fly"
                if not chunk.confirm or chunk.status != "queued":
                    return False, f"{_chunk_what(chunk)} needs no confirmation"
                chunk.confirm = False
                logger.warning("Operator confirmed the re-flight of %s", _chunk_what(chunk))
                messages = self._dispatch_locked()
                events = self._settle_and_collect_locked()
                status, reason, receiver = chunk.status, chunk.reason, chunk.receiver or chunk.flown_by
            self._emit_all(events)
            self._send_offers(messages)
            what = _chunk_what(chunk)
            if status == "failed":
                return True, f"{what} confirmed, but it cannot be re-flown: {reason}"
            if status == "queued":
                return True, f"{what} confirmed; it is offered once a drone can take it"
            return True, f"{what} confirmed and offered to {receiver}"
        if isinstance(cut_seq, bool) or not isinstance(cut_seq, int):
            return False, f"invalid cut number {cut_seq!r}"
        with self._lock:
            chunk = next(
                (c for c in self._reflights if c.lost_id == drone_id and c.cut_seq == cut_seq), None,
            )
            held = [c for c in self._reflights if c.lost_id == drone_id and c.confirm_cut == cut_seq
                    and c.confirm and c.status == "queued"]
            if chunk is None and not held:
                return False, f"{drone_id} has no cut {cut_seq} that could be re-flown"
            if not held and (not chunk.confirm or chunk.status != "queued"):
                return False, f"{drone_id}'s cut {cut_seq} needs no confirmation"
            for waiting in held:
                waiting.confirm = False
            if chunk is None:
                chunk = held[0]
            chunk.confirm = False
            logger.warning("Operator confirmed the re-flight of %s's cut %d", drone_id, cut_seq)
            messages = self._dispatch_locked()
            events = self._settle_and_collect_locked()
            status, reason, receiver = chunk.status, chunk.reason, chunk.receiver or chunk.flown_by
        self._emit_all(events)
        self._send_offers(messages)
        if status == "failed":
            return True, f"{drone_id}'s cut {cut_seq} confirmed, but it cannot be re-flown: {reason}"
        if status == "queued":
            return True, f"{drone_id}'s cut {cut_seq} confirmed; it is offered once a drone can take it"
        return True, f"{drone_id}'s cut {cut_seq} confirmed and offered to {receiver}"

    def confirm_retask(self, drone_id: str) -> tuple[bool, str]:
        """The operator confirms that the lost *drone_id*'s waiting lanes may be offered to another drone.

        Only lanes re-tasked with ``on_drone_lost(confirm=...)`` and not yet
        confirmed can be confirmed. They are split again into chunks for the
        drones that can take lanes now (``_lost_chunks_locked``), not for
        those there were at the loss, and from then on handled exactly like
        a lost drone's lanes: each chunk goes to the nearest drone that can
        take it, waits for one, or fails with the reason. The offers, if any,
        are sent before this returns, so do not call it on a link's delivery
        thread. A ``retask`` event lists the lanes offered or still waiting,
        and a ``retask_failed`` event those no drone can take.

        Refused, with nothing offered:

        * after LAND ALL: the waiting lanes fail with ``LAND ALL issued``;
        * while the lost drone's last telemetry shows it flying, until
          ``LOST_OBSTACLE_S`` after the loss or after that report, whichever
          is later, or while its telemetry from the last
          ``TELEMETRY_FRESH_S`` shows it under autonomous control (it could
          fly those lanes itself): it must be landed, or taken control of,
          first (``_flying_again_locked``).

        Returns ``(ok, detail)``: what happens next, or why nothing was
        confirmed and what to do.
        """
        with self._lock:
            retasks = [
                retask for retask in self._retasks
                if retask.lost_id == drone_id and any(c.confirm and c.status == "queued" for c in retask.chunks)
            ]
            if not retasks:
                return False, (f"{drone_id} has no lanes waiting for confirmation; the NOT SEARCHED list shows "
                               "what happened to its lanes")
            if self._landed_all:
                for retask in retasks:
                    for chunk in retask.chunks:
                        if chunk.confirm and chunk.status == "queued":
                            chunk.confirm = False
                            self._fail_locked(chunk, "LAND ALL issued")
                # Their re-task was reported at the loss, so this lists them as failed.
                landed_all_events = self._settle_and_collect_locked()
            else:
                landed_all_events = None
                flying = self._flying_again_locked(drone_id)
                if flying is not None:
                    return False, flying
                chunks: list[_Chunk] = []
                for retask in retasks:
                    waiting = [c for c in retask.chunks if c.confirm and c.status == "queued"]
                    if len(waiting) == len(retask.chunks) and retask.path:
                        # Split for the drones that can take lanes now.
                        retask.chunks = self._lost_chunks_locked(drone_id, retask.path, retask.index)
                        waiting = list(retask.chunks)
                    for chunk in waiting:
                        chunk.confirm = False
                    chunks.extend(waiting)
                count = sum(len(chunk.lanes) for chunk in chunks)
                logger.warning("Operator confirmed the re-task of %s's %d unflown lanes", drone_id, count)
                messages = self._dispatch_locked()
                events = self._settle_and_collect_locked()
                pending = [chunk for chunk in chunks if chunk.status in ("queued", "offered")]
                if pending:
                    events.append(("fleet_event", self._retask_payload(drone_id, [], pending)))
                offered = sorted({chunk.receiver for chunk in chunks if chunk.status == "offered"})
                waiting_lanes = sum(len(chunk.lanes) for chunk in chunks if chunk.status == "queued")
                reasons = list(dict.fromkeys(chunk.reason for chunk in chunks if chunk.status == "failed"))
        if landed_all_events is not None:
            self._emit_all(landed_all_events)
            return False, "LAND ALL was pressed, so nothing is re-tasked; make a new plan to search these lanes"
        self._emit_all(events)
        self._send_offers(messages)
        parts = []
        if offered:
            parts.append(f"offered to {', '.join(offered)}")
        if waiting_lanes:
            parts.append(f"{waiting_lanes} lanes are offered once a drone can take them")
        if reasons:
            lead = "some cannot be re-tasked" if offered or waiting_lanes else "no drone can take them"
            parts.append(f"{lead}: {'; '.join(reasons)}")
        return True, f"{drone_id}'s {count} unflown lanes confirmed; " + "; ".join(parts)

    def _flying_again_locked(self, drone_id: str) -> str | None:
        """Why the lost *drone_id*'s lanes must not be confirmed now, or None. Caller holds the lock.

        Another drone given those lanes could meet it there:

        * its last telemetry shows it flying, until ``LOST_OBSTACLE_S``
          after the loss or after that report, whichever is later: the
          window ``_lost_obstacle_locked`` keeps its position as an obstacle
          for. Once its link drops it may still be in the air until its edge
          lands it by itself, so a silence after a report that it flies
          does not count as landed;
        * its telemetry from the last ``TELEMETRY_FRESH_S`` shows it under
          autonomous control, which may fly its own lanes. On the ground it
          must be taken control of so it stays there; in the air (or when
          its telemetry does not say) also landed.
        """
        state = self._drones[drone_id]
        telemetry = state.telemetry or {}
        if telemetry.get("is_flying") is True and state.telemetry_at is not None:
            now = self._clock()
            age = now - state.telemetry_at
            if age <= TELEMETRY_FRESH_S:
                return f"Land {drone_id} first; it is flying again. Confirm the re-task once it reports landed."
            since = state.telemetry_at if state.lost_at is None else max(state.lost_at, state.telemetry_at)
            wait = LOST_OBSTACLE_S - (now - since)
            if wait > 0:
                return (f"{drone_id} may still be in the air: its last report, {age:.0f} s ago, said it was flying, "
                        f"and nothing has come from it since. Wait {math.ceil(wait)} s, or confirm the re-task once "
                        f"it reports landed.")
        if not self._fresh_locked(state):
            return None
        if telemetry.get("control_mode") == "autonomous":
            if telemetry.get("is_flying") is False:
                return (f"{drone_id} reports autonomous control again, so it may fly these lanes itself; take "
                        f"control of it so it stays on the ground, then confirm the re-task.")
            return (f"{drone_id} reports autonomous control again, so it may fly these lanes itself; take control "
                    f"of it and land it, then confirm the re-task.")
        return None

    def _reflight_counts_locked(self, drone_id: str) -> dict[str, int]:
        """The re-flights *drone_id* received: offered to it, re-flown by it and blocked again on it."""
        return {
            "offered": sum(1 for chunk in self._reflights if drone_id in chunk.offered_to),
            "reflown": sum(1 for chunk in self._reflights if chunk.flown_by == drone_id and chunk.status == "reflown"),
            "blocked_again": sum(
                1 for chunk in self._reflights if chunk.flown_by == drone_id and chunk.status == "blocked_again"
            ),
        }

    def _obstacle_uncovered_locked(self) -> list[dict]:
        """Every drone's obstacle segments in the shape of ``current_plan()["uncovered"]``, on the shared grid.

        Each entry has the keys of a ``retask_failed`` lane (``index``,
        ``direction``, ``distance_cm`` and the two ends), plus ``kind``
        ``segment``, ``drone_id``, ``reason`` and ``cut_seq``, the
        ``obstacle_seq`` of the cut that produced or caused it, which groups
        the segments of one cut.

        A segment of a cut taken up for a re-flight (design section 3.4) also
        has a ``status`` (see ``_reflight_fields``). A segment under the
        minimum move has status and reason ``too_short``. A re-flown segment
        is left out. A segment the receiver flew only in part (blocked again,
        its sensor down on it, flown off the planned path, or the receiver
        landed before it finished the re-flight) is replaced by what the
        receiver left unsearched of it (``_reflight_rest_locked``).
        The receiver's own replay of the lanes a re-flight appended to it
        (the transit included) is not listed: those lanes re-fly the cutting
        drone's segments, whose entries say what became of them.
        """
        marks: dict[tuple, tuple[_Chunk, bool]] = {}
        appended: dict[str, set[int]] = {}
        for chunk in self._reflights:
            for seg in chunk.segments:
                marks[(chunk.lost_id, _segment_key(seg, chunk.cut_seq))] = (chunk, False)
            for seg in chunk.too_short:
                marks[(chunk.lost_id, _segment_key(seg, chunk.cut_seq))] = (chunk, True)
            span = chunk.appended_range
            if chunk.flown_by is not None and span is not None:
                appended.setdefault(chunk.flown_by, set()).update(range(*span))
        out = []
        for drone_id, state in self._drones.items():
            if not state.obstacle_cuts and not state.avoid_offsets:
                continue
            numbers, segments = self._obstacle_replay_locked(state) if state.obstacle_cuts else ([], [])
            origin = self._origin(state)
            reflight_lanes = appended.get(drone_id, set())
            for (lane, start), length in state.avoid_offsets.items():
                offset = self._offset_segment_locked(state, lane, start, length)
                if offset is None or lane in reflight_lanes:
                    continue
                grid = _on_grid(offset, origin)
                held_by = next((c.confirm_cut for c in self._reflights
                                if c.lost_id == drone_id and c.offset == (lane, start)), None)
                # ``cut_seq`` names the person cut whose confirmation releases it, if any.
                entry = _uncovered_entry(grid, drone_id, SEGMENT_OFFSET, held_by)
                entry["offset_from_cm"] = start
                if state.avoid_offset_seen.get((lane, start)):
                    entry["seen"] = state.avoid_offset_seen[(lane, start)]
                mark = marks.get((drone_id, _segment_key(grid, None)))
                if mark is not None:
                    chunk, short = mark
                    if short:
                        entry.update(reason=SEGMENT_TOO_SHORT, status=SEGMENT_TOO_SHORT)
                    elif chunk.status == "reflown" or chunk.flown_rest:
                        continue
                    else:
                        entry.update(self._reflight_fields(chunk))
                out.append(entry)
            for seg in segments:
                if seg.lane_index in reflight_lanes:
                    continue
                grid = _on_grid(seg, origin)
                cut_seq = numbers[seg.cut]
                entry = _uncovered_entry(grid, drone_id, seg.reason, cut_seq)
                mark = marks.get((drone_id, _segment_key(grid, cut_seq)))
                if mark is not None:
                    chunk, short = mark
                    if short:
                        entry.update(reason=SEGMENT_TOO_SHORT, status=SEGMENT_TOO_SHORT)
                    elif chunk.status == "reflown" or chunk.flown_rest:
                        continue
                    else:
                        entry.update(self._reflight_fields(chunk))
                out.append(entry)
        for chunk in self._reflights:
            if chunk.flown_rest:
                out.extend(self._reflight_rest_locked(chunk))
        return out

    @staticmethod
    def _reflight_fields(chunk: _Chunk) -> dict:
        """The re-flight fields of an uncovered segment of *chunk* that is still waiting, in flight or failed.

        ``needs_confirmation``: a person cut, or an offset segment its sidestep
        or a person cut on its lane tied to a person, that waits for the
        operator's ``confirm_reflight``; it is not offered to any drone before
        that.
        ``waiting``: the chunk is queued. ``waiting_for`` is the cutting drone
        while the chunk waits for it to finish its lanes, or while it is
        still in the air too close to the path (then ``detail`` says how
        close), and None while the chunk waits for a free receiver (then
        ``detail`` names a receiver that may take it once it reports fresh
        telemetry or has taken off, when that is why).
        ``offered`` with ``receiver``; ``acked`` (accepted, the receiver has
        not reached it yet) and ``flying`` (the receiver is on its appended
        lanes), both with ``receiver``; ``failed`` with ``reason`` replaced by
        why it will not be re-flown (and ``receiver`` when it was accepted).
        """
        if chunk.status == "queued" and chunk.confirm:
            return {"status": "needs_confirmation"}
        if chunk.status == "queued":
            fields: dict[str, Any] = {
                "status": "waiting",
                "waiting_for": chunk.lost_id if not chunk.gate_open or chunk.crowded else None,
            }
            if chunk.crowded or chunk.held:
                fields["detail"] = chunk.crowded or chunk.held
            return fields
        if chunk.status == "offered":
            return {"status": "offered", "receiver": chunk.receiver}
        if chunk.status in ("acked", "flying"):
            return {"status": chunk.status, "receiver": chunk.flown_by}
        fields = {"status": "failed", "reason": chunk.reason}
        if chunk.flown_by is not None:
            fields["receiver"] = chunk.flown_by
        return fields

    # ------------------------------------------------------------------
    # Detection dedup
    # ------------------------------------------------------------------

    def on_detection(self, drone_id: str, msg: dict) -> tuple[FleetDetection, bool] | None:
        """Place a detection on the shared grid and merge it with nearby ones.

        ``msg`` carries the reporting drone's ``rel_x_cm``/``rel_y_cm``
        relative to its takeoff point. A detection within
        ``dedup_radius_cm`` of an existing one, from any drone, is merged
        into it; otherwise a new merged detection is created and a
        ``fleet_detection`` event is emitted.

        Returns the merged detection and whether it is new, or ``None`` when
        the message has no usable position.
        """
        rel_x = _number(msg.get("rel_x_cm"))
        rel_y = _number(msg.get("rel_y_cm"))
        if rel_x is None or rel_y is None:
            logger.debug("Detection from %s has no relative position; not placed", drone_id)
            return None
        with self._lock:
            state = self._drones.get(drone_id)
            if state is None:
                return None
            origin = self._origin(state)
            x, y = origin[0] + rel_x, origin[1] + rel_y
            now = self._clock()
            nearest_index = None
            nearest_distance = math.inf
            for index, existing in enumerate(self._detections):
                distance = math.hypot(existing.x_cm - x, existing.y_cm - y)
                if distance <= self._dedup_radius_cm and distance < nearest_distance:
                    nearest_index, nearest_distance = index, distance
            if nearest_index is not None:
                existing = self._detections[nearest_index]
                drone_ids = existing.drone_ids if drone_id in existing.drone_ids else existing.drone_ids + (drone_id,)
                record = replace(existing, drone_ids=drone_ids, count=existing.count + 1, last_seen_s=now)
                self._detections[nearest_index] = record
                is_new = False
            else:
                extra = {key: msg[key] for key in _PASSTHROUGH_DETECTION_KEYS if key in msg}
                record = FleetDetection(
                    id=self._next_detection_id,
                    drone_id=drone_id,
                    x_cm=x,
                    y_cm=y,
                    drone_ids=(drone_id,),
                    count=1,
                    first_seen_s=now,
                    last_seen_s=now,
                    extra=MappingProxyType(extra),
                )
                self._next_detection_id += 1
                self._detections.append(record)
                is_new = True
        if is_new:
            self._emit("fleet_detection", record.to_dict())
        return record, is_new

    # ------------------------------------------------------------------
    # Loss recovery
    # ------------------------------------------------------------------

    def on_drone_lost(
        self, drone_id: str, wait_s: float | None = None, *, confirm: str | None = None,
    ) -> dict[str, list[dict]]:
        """Mark a drone lost and offer its unflown lanes to the remaining drones.

        The unflown lanes start at the ``lane_index`` from the drone's last
        telemetry (0 without telemetry), so the lane it was flying is flown
        again in full. They are split into contiguous chunks, one per
        remaining drone, and a chunk is split further where the set of drones
        whose geofence contains it changes. Each chunk is offered to the
        nearest remaining drone (by its planned end point) whose geofence
        contains its whole new path, preferring drones not yet given a chunk.
        That drone receives an ``assign_lanes`` whose lanes are its previous
        lanes unchanged, then a transit to the chunk's start, then the chunk,
        so an edge keeps its lane index and flies the appended lanes after its
        own. A drone answers one offer at a time; further chunks for it wait.
        A drone that reports obstacle sensing is offered lanes only once it
        has finished its own (``_own_lanes_left_locked``): a later cut of its
        own would move where it flies the appended lanes. Until then a chunk
        it would take waits, the ``retask`` event says why, and
        ``current_plan()["uncovered"]`` lists its lanes as ``waiting`` with
        that reason. Such a drone that landed before it finished its own
        lanes never takes the chunk (``_unfit_receiver_locked``), so a chunk
        that only lost, landed or unplanned drones could take fails at once.

        An ``ack`` makes the offered lanes covered. A ``nack`` (or a failed
        send) withdraws the offer, and the chunk is offered to another
        eligible drone. A chunk that no remaining drone can take is uncovered.
        Any offer the lost drone itself had pending is withdrawn and offered
        again.

        Re-flights (design section 3.4): a re-flight the lost drone had
        accepted and not finished fails with reason ``receiver lost``, and
        its appended lanes (transit included) are not re-tasked, so no third
        drone is handed the segment. A waiting re-flight of the lost drone's
        own cuts drops its segments on lanes now re-tasked whole (they are
        superseded) and offers the rest, the cut's own segment included,
        without waiting for the lost drone; only a re-flight left with
        nothing fails, with reason ``superseded``.

        The lost drone is also sent a best-effort ``land`` so it cannot keep
        flying lanes that now belong to another drone. The call emits
        ``drone_lost``, then waits up to ``wait_s`` (default
        ``retask_wait_s``) for the offers to be answered, and then emits one
        ``retask`` event listing only the lanes that were accepted (with the
        lanes still awaiting an answer under ``awaiting``) and, if any lanes
        are uncovered, one ``retask_failed`` event listing them. Answers that
        arrive later emit their own ``retask`` or ``retask_failed`` events.
        Like ``sync()``, this call must not run on the thread that delivers
        the links' incoming messages.

        With *confirm* (why, in the operator's words), the unflown lanes are
        not offered to any drone until the operator confirms it with
        ``confirm_retask``. Like a person cut's re-flight, they are then
        handled exactly as above. Until then they are listed in
        ``current_plan()["uncovered"]`` with ``status``
        ``needs_confirmation`` and *confirm* as their ``reason``, and the
        call emits one ``retask_needs_confirmation`` event listing them
        instead of a ``retask`` event, without waiting. The ground uses this
        for a drone that landed, or was lost, while held with nobody
        controlling it (``FleetSupervisor``): none of its unflown lanes is
        given to another drone unless the operator confirms it. That is all
        it holds back. Everything else (``drone_lost``, the best-effort
        ``land``, the lost drone's re-flights, the other drones' own lanes
        and the transits to lanes they are given) is as without *confirm*,
        so another drone may still pass near where it was held.

        Returns the lanes appended to each drone that accepted re-tasked
        lanes by the time the call returns.
        """
        wait_s = self._retask_wait_s if wait_s is None else max(0.0, float(wait_s))
        retask = None
        with self._lock:
            state = self._drones.get(drone_id)
            if state is None or state.lost:
                return {}
            state.lost = True
            state.lost_at = self._clock()
            state.relanded = False
            if confirm is None:
                logger.warning("Drone %s lost; re-tasking its unflown lanes", drone_id)
            else:
                logger.warning("Drone %s lost; its unflown lanes wait for the operator's confirmation (%s)",
                               drone_id, confirm)
            self._fail_lost_receiver_locked(drone_id)
            if state.offer is not None:
                self._withdraw_offer_locked(drone_id, None)
            if state.zone is not None:
                index = 0
                lane_index = (state.telemetry or {}).get("lane_index")
                if isinstance(lane_index, int) and not isinstance(lane_index, bool):
                    index = max(0, min(lane_index, len(state.lanes)))
                full_path = state.lanes
                state.lanes = full_path[:index]
                if full_path[index:]:
                    chunks = self._lost_chunks_locked(drone_id, full_path, index)
                    if chunks:
                        retask = _Retask(drone_id, chunks, confirm=confirm,
                                         path=[dict(lane) for lane in full_path], index=index)
                        if confirm is not None:
                            for chunk in chunks:
                                chunk.confirm = True
                        self._retasks.append(retask)
            messages = self._dispatch_locked()
            events = self._settle_and_collect_locked()

        self._send_logged(drone_id, {"type": "land"})
        self._emit("fleet_event", {"event": "drone_lost", "drone_id": drone_id})
        self._emit_all(events)
        self._send_offers(messages)
        if retask is None:
            return {}
        if wait_s > 0 and retask.confirm is None:
            retask.settled.wait(wait_s)
        with self._lock:
            events, appended = self._summarise_locked(retask)
        self._emit_all(events)
        return appended

    # ------------------------------------------------------------------
    # Re-task allocation (callers hold the lock unless noted)
    # ------------------------------------------------------------------

    def _receivers(self) -> list[str]:
        """Drones that can still be given lanes: not lost, part of the plan, and not held for manual control."""
        return [
            rid for rid, other in self._drones.items()
            if not other.lost and other.zone is not None and not self._excluded(rid)
        ]

    def _excluded(self, drone_id: str) -> bool:
        """True when ``retask_excluded`` keeps *drone_id* from receiving re-tasked lanes. Fails closed."""
        check = self._retask_excluded
        if check is None:
            return False
        try:
            return bool(check(drone_id))
        except Exception:  # an unreadable state never gets lanes
            logger.warning("retask_excluded check failed for %s", drone_id, exc_info=True)
            return True

    def _unfit_receiver_locked(self, rid: str) -> str | None:
        """Why *rid* would accept re-tasked lanes of any kind and not fly them, or None.

        A drone that flies sensed steps and reports in its last telemetry
        that its obstacle sensor is not ok flies no step (review N6). A drone
        whose lanes a ``sensor_down`` cut ended never flies a lane appended
        after it either (``lane_steps``). A drone that reports obstacle
        sensing and whose fresh telemetry shows it on the ground with lanes
        of its own left, after it had flown in this plan, has landed: it
        never finishes its own lanes, so it never takes appended lanes
        (``_own_lanes_left_locked``; review R2-N2). Telemetry without the
        obstacle fields changes nothing.
        """
        state = self._drones[rid]
        telemetry = state.telemetry or {}
        if telemetry.get(TELEMETRY_SENSING) == SENSING_STEPPED and telemetry.get(TELEMETRY_SENSOR_OK) is False:
            return "reports its obstacle sensor is not ok"
        if any(cut.reason == CUT_SENSOR_DOWN for cut in state.obstacle_cuts.values()):
            return "ended its lanes (obstacle sensor down)"
        if (
            telemetry.get(TELEMETRY_SENSING) is not None
            and self._fresh_locked(state)
            and state.seen_flying
            and telemetry.get("is_flying") is False
            and telemetry.get("state") != "taking_off"
            and not self._own_lanes_done_locked(state)
        ):
            return "landed before it finished its own lanes"
        return None

    def _fresh_locked(self, state: _DroneState) -> bool:
        """True when the drone's last telemetry is no older than ``TELEMETRY_FRESH_S``."""
        return state.telemetry_at is not None and self._clock() - state.telemetry_at <= TELEMETRY_FRESH_S

    @staticmethod
    def _own_lanes_done_locked(state: _DroneState) -> bool:
        """True when the drone's last telemetry shows it past every lane: ``lanes_total`` and every lane the ground gave it."""
        telemetry = state.telemetry or {}
        lane_index, lanes_total = telemetry.get("lane_index"), telemetry.get("lanes_total")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (lane_index, lanes_total)):
            return False
        return lane_index >= lanes_total and lane_index >= len(state.lanes)

    def _own_lanes_left_locked(self, rid: str) -> str | None:
        """Why *rid* may not be given appended lanes yet: it senses obstacles and has lanes of its own left.

        A drone that reports ``obstacle_sensing`` (any mode) may cut one of
        its own lanes, and a cut moves where it starts lanes appended after
        them, away from the path whose separation and fence were checked. So
        it gets appended lanes, of a lost drone or of a re-flight, only once
        its telemetry from the last ``TELEMETRY_FRESH_S`` seconds shows it
        has finished its own lanes (``lane_index`` at or past ``lanes_total``
        and past every lane the ground gave it); its cuts are then all known
        and its replayed position is where it is. Until then the chunk waits
        with a text that says why (review R2-N2):

        * ``waiting for <rid>, which has no fresh telemetry`` while its last
          telemetry is older than ``TELEMETRY_FRESH_S``. It may report again;
          if it is declared lost instead, it is no longer a receiver.
        * ``waiting for <rid>, which is taking off`` or ``waiting for <rid>,
          which is not flying`` while its fresh telemetry shows it on the
          ground with lanes left before it has flown in this plan.
        * ``waiting for <rid> to finish its own lanes`` while it flies them.

        A drone that landed with lanes of its own left never takes the chunk
        and is left out before this check (``_unfit_receiver_locked``). A
        drone without the obstacle fields is not held back, as before.
        Returns None when *rid* may take lanes now.
        """
        state = self._drones[rid]
        telemetry = state.telemetry or {}
        if telemetry.get(TELEMETRY_SENSING) is None:
            return None
        if not self._fresh_locked(state):
            return f"waiting for {rid}, which has no fresh telemetry"
        if self._own_lanes_done_locked(state):
            return None
        if telemetry.get("is_flying") is False:
            if telemetry.get("state") == "taking_off":
                return f"waiting for {rid}, which is taking off"
            return f"waiting for {rid}, which is not flying"
        return f"waiting for {rid} to finish its own lanes"

    def _fence_cm(self, drone_id: str) -> float | None:
        """The fence half-width to plan against: the smaller of the configured one and the edge's reported one."""
        known = [f for f in (self._geofence_cm, self._drones[drone_id].reported_fence_cm) if f is not None]
        return min(known) if known else None

    def _inside_fence(self, drone_id: str, point: tuple[float, float]) -> bool:
        fence = self._fence_cm(drone_id)
        if fence is None:
            return True
        origin = self._origin(self._drones[drone_id])
        limit = fence + _FENCE_TOLERANCE_CM
        return abs(point[0] - origin[0]) <= limit and abs(point[1] - origin[1]) <= limit

    def _path_inside_fence(self, drone_id: str, lanes: list[dict]) -> bool:
        """True if every point of ``lanes`` as the drone actually flies them is inside its geofence.

        The path is ``_actual_path_locked`` (from the drone's takeoff point,
        with its cuts). Lanes are straight moves and the geofence is a
        square, so checking the end of every move covers the whole path.
        """
        if self._fence_cm(drone_id) is None:
            return True
        return all(self._inside_fence(drone_id, point) for point in self._actual_path_locked(drone_id, lanes)[1:])

    def _make_chunks(
        self, lost_id: str, origin: tuple[int, int], full_path: list[dict], index: int,
    ) -> list[_Chunk]:
        """Split ``full_path[index:]`` into chunks to offer.

        The lanes are first split into one contiguous piece per remaining
        drone. A piece is split again wherever no single drone's geofence
        contains all of it, and lanes outside every geofence are kept
        together so they are reported as one uncovered chunk. Each lane is
        placed where it is planned (``_planned_points_locked``): from the
        lost drone's takeoff point *origin* along its lanes, and, after a
        re-task start, from where the lost drone was then.
        """
        unflown = full_path[index:]
        points = self._planned_points_locked(lost_id, full_path)[index:]
        receivers = self._receivers()
        covers = [
            frozenset(r for r in receivers if self._inside_fence(r, points[k]) and self._inside_fence(r, points[k + 1]))
            for k in range(len(unflown))
        ]

        def chunk(run: list[int]) -> _Chunk:
            return _Chunk(
                lost_id=lost_id,
                first_index=index + run[0],
                lanes=[dict(unflown[k]) for k in run],
                points=points[run[0]:run[-1] + 2],
            )

        chunks = []
        for _, piece in _split_contiguous(list(range(len(unflown))), max(1, len(receivers))):
            run: list[int] = []
            common: frozenset = frozenset()
            for k in piece:
                if run:
                    if common and common & covers[k]:
                        run.append(k)
                        common = common & covers[k]
                        continue
                    if not common and not covers[k]:
                        run.append(k)
                        continue
                    chunks.append(chunk(run))
                run, common = [k], covers[k]
            if run:
                chunks.append(chunk(run))
        return chunks

    def _dispatch_locked(self) -> list[tuple[str, dict]]:
        """Offer every queued chunk to an eligible drone; return the messages to send.

        A chunk whose eligible drones are all answering another offer stays
        queued. A chunk with no eligible drone at all fails. Several chunks
        offered to one drone in the same round go out in one
        ``assign_lanes``. Re-flight chunks are offered after a lost drone's
        lanes, by ``_offer_reflight_locked``.

        A drone that cannot fly appended lanes because of its obstacle
        sensing, or that senses obstacles and landed before it finished its
        own lanes (``_unfit_receiver_locked``), is never offered any, and one
        that senses obstacles is offered them only once it has finished its
        own lanes (``_own_lanes_left_locked``): until then a chunk it would
        take waits, with the reason (no fresh telemetry, not flying yet, or
        lanes of its own left) in ``held``. The transit to a chunk
        starts where the receiver will actually be after its lanes
        (``_actual_path_locked``), and the fence and the separation are
        checked on the path it will actually fly.
        """
        planned: dict[str, list[dict]] = {}
        batches: dict[str, list[_Chunk]] = {}
        for retask in self._retasks:
            for chunk in retask.chunks:
                if chunk.status != "queued":
                    continue
                if self._landed_all:
                    self._fail_locked(chunk, "LAND ALL issued")
                    continue
                if chunk.confirm:
                    continue  # only the operator's confirm_retask lets it be offered
                fits = []
                waiting = False
                unfit: dict[str, str] = {}
                busy: list[str] = []
                for rid in self._receivers():
                    if rid in chunk.refusals:
                        continue
                    why = self._unfit_receiver_locked(rid)
                    if why is not None:
                        unfit[rid] = why
                        continue
                    lanes = planned.get(rid, self._drones[rid].lanes)
                    end = self._actual_path_locked(rid, lanes)[-1]
                    added = _transit(end, chunk.start) + [dict(lane) for lane in chunk.lanes]
                    if self._path_inside_fence(rid, lanes + added):
                        crowded = self._too_close(rid, lanes + added, planned)
                        if crowded is not None:
                            chunk.too_close[rid] = crowded
                            continue
                        chunk.too_close.pop(rid, None)
                        if rid in chunk.deferred:
                            waiting = True  # refused for now: wait, do not fail
                        elif (left := self._own_lanes_left_locked(rid)) is not None:
                            waiting = True  # it takes lanes once its own are done
                            busy.append(left)
                        elif (hover := self._near_lost_locked(rid, lanes + added)) is not None:
                            # A lost drone may still hover on the way: wait
                            # until it has landed by itself (LOST_OBSTACLE_S).
                            waiting = True
                            busy.append(f"{rid} would {hover}")
                        else:
                            fits.append((rid, end, added))
                self._hold_locked(chunk, None if fits else "; ".join(busy) or None)
                if not fits and waiting:
                    continue
                if not fits:
                    self._fail_locked(chunk, self._failure_reason(chunk, unfit=unfit))
                    continue
                free = [fit for fit in fits if self._drones[fit[0]].offer is None]
                if not free:
                    continue
                fresh = [fit for fit in free if fit[0] not in batches]
                rid, _, added = min(fresh or free, key=lambda fit: math.dist(fit[1], chunk.start))
                planned[rid] = planned.get(rid, self._drones[rid].lanes) + added
                batches.setdefault(rid, []).append(chunk)
                chunk.status = "offered"
                chunk.receiver = rid
                chunk.appended = [dict(lane) for lane in added]
        for chunk in self._reflights:
            if chunk.status == "queued":
                self._offer_reflight_locked(chunk, planned, batches)

        messages = []
        for rid, chunks in batches.items():
            state = self._drones[rid]
            state.offer = _Offer(
                receiver=rid,
                chunks=chunks,
                base_lanes=state.lanes,
                lanes_total=len(planned[rid]),
                acked=state.acked,
                ack_battery=state.ack_battery,
                ack_lanes_total=state.ack_lanes_total,
            )
            state.lanes = [dict(lane) for lane in planned[rid]]
            self._clear_ack(state)
            messages.append((rid, self._assign_message(rid, state)))
            logger.info("Offering %d re-tasked lanes to %s", sum(len(c.lanes) for c in chunks), rid)
        return messages

    def _failure_reason(
        self, chunk: _Chunk, receivers: list[str] | None = None, unfit: dict[str, str] | None = None,
    ) -> str:
        """Why no drone can take *chunk*: refusals, obstacle sensing, crowding and geofences, by drone.

        *receivers* are the drones that were considered; by default every
        drone ``_receivers()`` returns. *unfit* maps a drone left out for its
        obstacle sensing (``_unfit_receiver_locked``) to why.
        """
        unfit = unfit or {}
        receivers = self._receivers() if receivers is None else receivers
        if not receivers:
            held = [rid for rid, other in self._drones.items()
                    if not other.lost and other.zone is not None and self._excluded(rid)]
            if held:
                return f"no drone left to take them ({', '.join(held)} held for manual control)"
            return "no drone left to take them"
        parts = []
        refused = [rid for rid in receivers if rid in chunk.refusals]
        if refused:
            parts.append("refused by " + ", ".join(f"{rid} ({chunk.refusals[rid]})" for rid in refused))
        sensing = [rid for rid in receivers if rid not in chunk.refusals and rid in unfit]
        if sensing:
            parts.append("; ".join(f"{rid} {unfit[rid]}" for rid in sensing))
        crowded = [rid for rid in receivers if rid not in chunk.refusals and rid not in unfit
                   and rid in chunk.too_close]
        if crowded:
            parts.append("; ".join(f"{rid} would {chunk.too_close[rid]}" for rid in crowded))
        outside = [rid for rid in receivers if rid not in chunk.refusals and rid not in unfit
                   and rid not in chunk.too_close]
        if outside:
            parts.append("outside the geofence of " + ", ".join(outside))
        return "; ".join(parts)

    def _too_close(
        self, rid: str, lanes: list[dict], planned: dict[str, list[dict]],
        flown: tuple[str, int] | None = None,
    ) -> str | None:
        """Why giving *rid* the path *lanes* would break the zone gap, or None.

        The whole path *rid* would have, flown from its takeoff point, is
        compared with the whole planned path of every other drone that is not
        lost (also a drone held for manual control, which may still fly). The
        comparison ignores timing, like the plan's own separation check, so a
        re-task never brings two drones closer than the gap at any point.
        *flown* is ``(drone_id, count)`` for a re-flight's cutting drone
        (design section 3.4): its first *count* lanes, which it has finished,
        are not compared, and only lanes it was given after them are.

        Every path is the one the drone actually flies
        (``_actual_path_locked``): with its cuts, and with lanes a re-task
        appended flown from where the drone actually is. A drone without
        cuts flies its lanes as planned. A lost drone's path is not
        compared, because its lanes were taken from it; its position is
        (``_near_lost_locked``), until ``LOST_OBSTACLE_S`` after the loss or
        after its last telemetry that showed it flying, whichever is later,
        and only ever makes the offer wait.
        """
        gap = self._plan.gap_cm if self._plan is not None else self._zone_gap_cm
        if gap <= 0 or not self._retask_separation:
            return None
        mine = self._actual_path_locked(rid, lanes)
        for other_id, other in self._drones.items():
            if other_id == rid or other.lost or other.zone is None:
                continue
            theirs = self._actual_path_locked(other_id, planned.get(other_id, other.lanes))
            if flown is not None and other_id == flown[0]:
                theirs = theirs[flown[1]:]
                if len(theirs) < 2:
                    continue
            distance, _, _ = closest_approach(mine, theirs)
            # A drone that may sidestep may move toward this path inside its own zone.
            margin = self._sidestep_margin_locked(other_id)
            if distance < gap + margin:
                extra = f" and the {margin} cm sidestep margin" if margin else ""
                return f"come within {distance:.0f} cm of {other_id}, below the {gap} cm gap{extra}"
        return None

    def _sidestep_margin_locked(self, drone_id: str) -> int:
        """The sidestep margin to keep from *drone_id*'s path: ``sidestep_margin_cm`` if it may sidestep, else 0.

        A drone may sidestep when its latest telemetry's ``obstacle_avoid``
        reports a ``sidestep_cm`` above 0 (``lane_avoid``). Caller holds the lock.
        """
        state = self._drones.get(drone_id)
        avoid = (state.telemetry or {}).get(TELEMETRY_AVOID) if state is not None else None
        sidestep = _number(avoid.get(AVOID_SIDESTEP_CM_KEY)) if isinstance(avoid, dict) else None
        return self._sidestep_margin_cm if sidestep is not None and sidestep > 0 else 0

    def sidestep_margin_of(self, drone_id: str) -> int:
        """The sidestep margin a path must keep from *drone_id*'s path (``_sidestep_margin_locked``)."""
        with self._lock:
            return self._sidestep_margin_locked(drone_id)

    def _fail_locked(self, chunk: _Chunk, reason: str) -> None:
        chunk.status = "failed"
        chunk.receiver = None
        chunk.reason = reason
        chunk.held = None
        logger.error("Re-tasked lanes %s of %s are uncovered: %s",
                     ", ".join(str(entry["index"]) for entry in chunk.lane_entries()), chunk.lost_id, reason)

    @staticmethod
    def _hold_locked(chunk: _Chunk, held: str | None) -> None:
        """Record why *chunk* waits for a receiver (``held``), and log it when that changes."""
        if held == chunk.held:
            return
        chunk.held = held
        if held:
            lanes = ", ".join(str(entry["index"]) for entry in chunk.lane_entries())
            logger.info("Re-tasked lanes %s of %s wait: %s", lanes, chunk.lost_id, held)

    def _release_deferred_locked(self, drone_id: str, telemetry: dict) -> bool:
        """Clear the transient refusals of *drone_id* that its telemetry shows are over.

        A ``landing`` refusal is over once the drone reports ``idle`` (or,
        without a state, not flying); a ``busy`` one once it is neither
        taking off nor landing. Returns True if any queued chunk (a lost
        drone's lanes or a re-flight) may now be offered to the drone again.
        Caller holds ``_lock``.
        """
        released = False
        state = telemetry.get("state")
        flying = telemetry.get("is_flying")
        chunks = [chunk for retask in self._retasks for chunk in retask.chunks] + self._reflights
        for chunk in chunks:
            reason = chunk.deferred.get(drone_id)
            if reason is None:
                continue
            if reason == "landing":
                over = state == "idle" if isinstance(state, str) else flying is False
            else:
                over = state not in ("taking_off", "landing")
            if over:
                del chunk.deferred[drone_id]
                released = released or chunk.status == "queued"
        return released

    def _withdraw_offer_locked(self, drone_id: str, reason: str | None, *, transient: bool = False) -> None:
        """Take back a drone's pending offer and queue its chunks again.

        ``reason`` records a refusal, so the chunks are not offered to this
        drone again; ``None`` (the drone was lost) records nothing. With
        ``transient`` the refusal is only for now (``deferred``): the chunks
        are offered to the drone again once its telemetry shows it is over. The
        drone's lanes and ack state return to what they were before the
        offer, because the edge keeps its previous plan.
        """
        state = self._drones[drone_id]
        offer, state.offer = state.offer, None
        if offer is None:
            return
        state.lanes = offer.base_lanes
        state.acked = offer.acked
        state.ack_battery = offer.ack_battery
        state.ack_lanes_total = offer.ack_lanes_total
        for chunk in offer.chunks:
            chunk.status = "queued"
            chunk.receiver = None
            chunk.appended = []
            chunk.appended_start = None
            if reason is not None:
                (chunk.deferred if transient else chunk.refusals)[drone_id] = reason

    def _send_offers(self, messages: list[tuple[str, dict]]) -> None:
        """Send offers without the lock. A failed send counts as a refusal. Lock not held."""
        queue = list(messages)
        while queue:
            rid, msg = queue.pop(0)
            if self._send_logged(rid, msg):
                continue
            with self._lock:
                state = self._drones[rid]
                if state.offer is None or state.offer.lanes_total != len(msg["lanes"]):
                    continue
                self._withdraw_offer_locked(rid, "not reachable")
                queue.extend(self._dispatch_locked())
                events = self._settle_and_collect_locked()
            self._emit_all(events)

    def _settle_and_collect_locked(self) -> list[tuple[str, dict]]:
        """Wake waiters of settled re-tasks and build events for already reported ones.

        A re-task is settled when every chunk is accepted or failed. Chunks
        of a re-task whose summary was already emitted are reported as soon
        as they are accepted or fail. Each re-flight chunk whose status or
        receiver changed since it was last announced gets its
        ``reflight_*`` event.
        """
        events = []
        for retask in self._retasks:
            if all(chunk.status in ("acked", "failed") for chunk in retask.chunks):
                retask.settled.set()
            if not retask.reported:
                continue
            acked = [c for c in retask.chunks if c.status == "acked" and c.reported != "acked"]
            failed = [c for c in retask.chunks if c.status == "failed" and c.reported != "failed"]
            if acked:
                events.append(("fleet_event", self._retask_payload(retask.lost_id, acked, [])))
            if failed:
                events.append(("fleet_event", self._failed_payload(retask.lost_id, failed)))
            for chunk in acked + failed:
                chunk.reported = chunk.status
        for chunk in self._reflights:
            events.extend(self._reflight_events_locked(chunk))
        return events

    def _summarise_locked(self, retask: _Retask) -> tuple[list[tuple[str, dict]], dict[str, list[dict]]]:
        """Build the ``retask`` and ``retask_failed`` events that close ``on_drone_lost``."""
        if retask.reported or not any(r is retask for r in self._retasks):
            return [], {}
        retask.reported = True
        acked = [c for c in retask.chunks if c.status == "acked"]
        failed = [c for c in retask.chunks if c.status == "failed"]
        unconfirmed = [c for c in retask.chunks if c.status == "queued" and c.confirm]
        pending = [c for c in retask.chunks if c.status in ("queued", "offered") and not c.confirm]
        events = []
        if acked or pending:
            events.append(("fleet_event", self._retask_payload(retask.lost_id, acked, pending)))
        if unconfirmed:
            events.append(("fleet_event", self._confirm_payload(retask, unconfirmed)))
        if failed:
            events.append(("fleet_event", self._failed_payload(retask.lost_id, failed)))
        for chunk in acked + failed:
            chunk.reported = chunk.status
        appended: dict[str, list[dict]] = {}
        for chunk in acked:
            appended.setdefault(chunk.receiver, []).extend(dict(lane) for lane in chunk.appended)
        return events, appended

    @staticmethod
    def _retask_payload(lost_id: str, acked: list[_Chunk], pending: list[_Chunk]) -> dict:
        """A ``retask`` event. ``to`` and ``lanes`` cover accepted lanes only.

        ``to`` counts the lanes appended to each accepting drone (transit
        included) and ``lanes`` lists the lost drone's lanes it accepted.
        Lanes still waiting for an answer are listed under ``awaiting``; a
        lane that waits for a drone to finish its own lanes carries that as
        its ``detail``, and the event's ``detail`` says it too.
        """
        to: dict[str, int] = {}
        lanes = []
        for chunk in acked:
            to[chunk.receiver] = to.get(chunk.receiver, 0) + len(chunk.appended)
            lanes.extend(dict(entry, drone_id=chunk.receiver) for entry in chunk.lane_entries())
        parts = [", ".join(f"{n} lanes to {rid}" for rid, n in to.items()) or "no lanes accepted yet"]
        payload = {"event": "retask", "drone_id": lost_id, "to": to, "lanes": lanes}
        if pending:
            waiting: dict[str, int] = {}
            awaiting = []
            for chunk in pending:
                who = chunk.receiver or "a free drone"
                waiting[who] = waiting.get(who, 0) + len(chunk.lanes)
                extra = {"detail": chunk.held} if chunk.held and chunk.receiver is None else {}
                awaiting.extend(dict(entry, drone_id=chunk.receiver, **extra) for entry in chunk.lane_entries())
            text = ", ".join(f"{n} lanes awaiting {who}" for who, n in waiting.items())
            held = list(dict.fromkeys(c.held for c in pending if c.held and c.receiver is None))
            parts.append(text + (f" ({'; '.join(held)})" if held else ""))
            payload["awaiting"] = awaiting
        payload["detail"] = "; ".join(parts)
        return payload

    @staticmethod
    def _confirm_payload(retask: _Retask, chunks: list[_Chunk]) -> dict:
        """A ``retask_needs_confirmation`` event: the lanes that wait for the operator's ``confirm_retask``."""
        lanes = [dict(entry, reason=retask.confirm) for chunk in chunks for entry in chunk.lane_entries()]
        return {
            "event": "retask_needs_confirmation",
            "drone_id": retask.lost_id,
            "detail": (f"{retask.lost_id}: {retask.confirm}; its {len(lanes)} unflown lanes are offered to another "
                       "drone only once you confirm it"),
            "lanes": lanes,
        }

    @staticmethod
    def _failed_payload(lost_id: str, failed: list[_Chunk]) -> dict:
        """A ``retask_failed`` event listing every uncovered lane and why."""
        lanes = [dict(entry, reason=chunk.reason) for chunk in failed for entry in chunk.lane_entries()]
        reasons = list(dict.fromkeys(chunk.reason for chunk in failed))
        return {
            "event": "retask_failed",
            "drone_id": lost_id,
            "detail": f"{len(lanes)} lanes of {lost_id} uncovered: " + "; ".join(reasons),
            "lanes": lanes,
        }

    # ------------------------------------------------------------------
    # Re-flight of cut segments, offered once (design section 3.4).
    # Callers hold the lock.
    # ------------------------------------------------------------------

    def _refresh_reflight_locked(self, chunk: _Chunk) -> None:
        """Derive a re-flight chunk's segments and lanes from its cutting drone's current replay.

        The chunk re-flies the cut's own segments and its ``skipped``
        shadows, except any on lanes the cutting drone itself flies to re-fly
        another drone's cut. Nothing changes once the cutting drone is lost
        (its lanes from the loss on are re-tasked whole) or when its
        telemetry no longer lists the cut (its edge restarted or left the
        plan): the chunk keeps what it had.
        """
        state = self._drones[chunk.lost_id]
        if state.lost or chunk.cut_seq not in state.obstacle_cuts:
            return
        numbers, segments = self._obstacle_replay_locked(state)
        index = numbers.index(chunk.cut_seq)
        reason = state.obstacle_cuts[chunk.cut_seq].reason
        origin = self._origin(state)
        mine = [
            _on_grid(seg, origin) for seg in segments
            if seg.cut == index and seg.reason in (reason, SEGMENT_SKIPPED)
            and not self._in_reflight_lanes_locked(chunk.lost_id, seg.lane_index)
        ]
        plan = plan_reflight(mine)
        chunk.lanes = [dict(lane) for lane in plan.lanes]
        chunk.points = list(plan.points)
        chunk.legs = list(plan.legs)
        chunk.segments = list(plan.segments)
        chunk.too_short = list(plan.too_short)

    def _in_reflight_lanes_locked(self, drone_id: str, lane_index: int) -> bool:
        """True if lane *lane_index* of *drone_id* was appended to it to re-fly another drone's cut."""
        for chunk in self._reflights:
            span = chunk.appended_range
            if span is None:
                continue
            mine = chunk.flown_by == drone_id or (chunk.status == "offered" and chunk.receiver == drone_id)
            if mine and span[0] <= lane_index < span[1]:
                return True
        return False

    def _reflight_rest_locked(self, chunk: _Chunk) -> list[dict]:
        """What the receiver of a re-flight left unsearched of its segments, as ``current_plan()["uncovered"]`` entries.

        Each lane that re-flies one of the cutting drone's segments (the
        transits are not ground to search) is judged against the path the
        receiver actually flies (``_actual_path_locked``, its replay): a
        segment counts as searched only where that path runs along it
        (``lane_reflight.leg_rest``), so a leg flown short, displaced or not
        at all leaves its rest. That holds for the legs up to and including
        the lane of the receiver's first cut on the lanes the offer appended.
        Every segment the re-flight would have flown after that lane is
        reported whole, because the receiver reached it off the planned path,
        if at all; a cut on the transit to the segments therefore leaves
        every segment whole. A receiver that landed before it finished the
        appended lanes (``landed_at``) did not fly the lane it was on nor any
        after it, so every segment of those lanes is reported whole too.

        Each entry is listed under the cutting drone with the cutting
        drone's lane index, the chunk's reason (``blocked again by
        <receiver>``, or why it failed), the chunk's status and ``receiver``.
        """
        rid = chunk.flown_by
        span = chunk.appended_range
        if rid is None or span is None:
            return []
        state = self._drones[rid]
        first = span[1] - len(chunk.lanes)  # the receiver's lane index of the chunk's first lane
        hits = [cut.lane_index for cut in state.obstacle_cuts.values() if span[0] <= cut.lane_index < span[1]]
        blocked_at = min(hits) - first if hits else len(chunk.lanes)
        path = self._actual_path_locked(rid)
        out = []
        for k, leg in enumerate(chunk.legs):
            if leg is None:
                continue
            seg = chunk.segments[leg]
            lane = first + k
            landed = chunk.landed_at is not None and lane >= chunk.landed_at
            if k > blocked_at or landed or lane + 1 >= len(path):
                pieces = [seg]
            else:
                pieces = leg_rest(seg, path[lane], path[lane + 1])
            for piece in pieces:
                entry = _uncovered_entry(piece, chunk.lost_id, chunk.reason, chunk.cut_seq)
                entry.update(status=chunk.status, receiver=rid)
                out.append(entry)
        return out

    def _reflight_candidates_locked(self, cutter_id: str) -> tuple[list[str], dict[str, str], list[str]]:
        """The drones that may re-fly *cutter_id*'s segments, why each other receiver may not, and which may soon.

        The third list names the drones left out only for now: without
        fresh telemetry, or not flying yet because they are taking off
        (review N7). A chunk waits for them instead of failing.
        """
        now = self._clock()
        eligible: list[str] = []
        why: dict[str, str] = {}
        soon: list[str] = []
        for rid in self._receivers():
            if rid == cutter_id:
                continue
            state = self._drones[rid]
            telemetry = state.telemetry or {}
            battery = _number(telemetry.get("battery"))
            sensing = telemetry.get(TELEMETRY_SENSING)
            if state.telemetry_at is None or now - state.telemetry_at > TELEMETRY_FRESH_S:
                why[rid] = "has no fresh telemetry"
                soon.append(rid)
            elif telemetry.get("is_flying") is not True and telemetry.get("state") == "taking_off":
                why[rid] = "is taking off"
                soon.append(rid)
            elif telemetry.get("is_flying") is not True:
                why[rid] = "is not flying"
            elif battery is None:
                why[rid] = "reports no battery"
            elif battery < self._min_battery:
                why[rid] = f"has battery {battery:g}%, below {self._min_battery:g}%"
            elif sensing != SENSING_STEPPED:
                why[rid] = (f"senses obstacles {sensing:.40}" if isinstance(sensing, str)
                            else "reports no obstacle sensing")
            elif telemetry.get(TELEMETRY_SENSOR_OK) is not True:
                why[rid] = "reports its obstacle sensor is not ok"
            elif telemetry.get("drone_link_ok") is False:
                why[rid] = "cannot reach its drone"
            elif (unfit := self._unfit_receiver_locked(rid)) is not None:
                why[rid] = unfit
            else:
                eligible.append(rid)
        return eligible, why, soon

    def _reflight_receivers(self, cutter_id: str) -> list[str]:
        """The drones that may receive *cutter_id*'s segments to re-fly (design section 3.4).

        ``_receivers()`` (not lost, part of the plan, not held for manual
        control) without the cutting drone, keeping only drones whose
        telemetry from the last ``TELEMETRY_FRESH_S`` seconds shows
        ``is_flying: true``, a battery of at least the start minimum (``min_battery``)
        percent, ``obstacle_sensing`` ``stepped``, ``obstacle_sensor_ok:
        true`` and not ``drone_link_ok: false``, and whose lanes no
        ``sensor_down`` cut has ended. A drone that flies unsensed (``off``
        or ``forward_only``) never receives a segment.
        """
        return self._reflight_candidates_locked(cutter_id)[0]

    def _cutter_done_locked(self, cutter_id: str) -> bool:
        """True once the cutting drone has nothing left to fly.

        It is lost, or its telemetry from the last ``TELEMETRY_FRESH_S``
        seconds says it is not flying, or that its ``lane_index`` has reached
        ``lanes_total`` (it hovers at the end of its lanes) while it has no
        re-task offer pending and the ground has no lane for it beyond that
        index either.
        """
        state = self._drones[cutter_id]
        if state.lost:
            return True
        if state.telemetry_at is None or self._clock() - state.telemetry_at > TELEMETRY_FRESH_S:
            return False
        telemetry = state.telemetry or {}
        if telemetry.get("is_flying") is False:
            return True
        lane_index, lanes_total = telemetry.get("lane_index"), telemetry.get("lanes_total")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (lane_index, lanes_total)):
            return False
        return state.offer is None and lane_index >= lanes_total and lane_index >= len(state.lanes)

    def _lost_obstacle_locked(self, drone_id: str) -> tuple[float, float] | None:
        """Where lost *drone_id* may still hover, on the shared grid, or None. Caller holds the lock.

        A drone declared lost may still be in the air (``LOST_OBSTACLE_S``):
        its last reported position counts until ``LOST_OBSTACLE_S`` after it
        was declared lost, or after its last telemetry that showed it flying
        if that came later. So a lost drone whose link came back while it is
        in the air (it is sent ``land`` again, ``_reland_lost_locked``)
        counts where it reports itself for as long as it keeps reporting
        that it flies, and for ``LOST_OBSTACLE_S`` after its link drops
        again. None for a drone that is not lost, once that time is over,
        once its telemetry says it is not flying, and for one that never
        reported a position (nothing to place; its lanes are re-tasked as
        before).
        """
        state = self._drones[drone_id]
        if not state.lost or state.lost_at is None:
            return None
        telemetry = state.telemetry or {}
        if telemetry.get("is_flying") is False:
            return None
        since = state.lost_at
        if telemetry.get("is_flying") is True and state.telemetry_at is not None:
            since = max(since, state.telemetry_at)
        if self._clock() - since >= LOST_OBSTACLE_S:
            return None
        rel_x, rel_y = _number(telemetry.get("rel_x_cm")), _number(telemetry.get("rel_y_cm"))
        if rel_x is None or rel_y is None:
            return None
        origin = self._origin(state)
        return origin[0] + rel_x, origin[1] + rel_y

    def _point_too_close_locked(
        self, rid: str, lanes: list[dict], other_id: str, point: tuple[float, float], gap: int, lost: bool,
    ) -> str | None:
        """Why *rid* flying *lanes* would come within *gap* of *other_id* at *point*, or None. Caller holds the lock."""
        mine = self._actual_path_locked(rid, lanes)
        distance, _, _ = closest_approach(mine, [point, point])
        if distance >= gap:
            return None
        who = f"{other_id} (lost; it may still hover there)" if lost else other_id
        return (f"come within {distance:.0f} cm of {who} at ({point[0]:g}, {point[1]:g}) cm, "
                f"below the {gap} cm gap")

    def _near_cutter_locked(self, rid: str, lanes: list[dict], cutter_id: str) -> str | None:
        """Why *rid* flying *lanes* would come too close to the cutting drone where it is now, or None.

        While the cutting drone is in the air, its position from telemetry
        (relative to its takeoff point, placed on the shared grid as
        ``status()`` does) is a point obstacle at the zone gap; a cutting
        drone whose telemetry says it is not flying is ignored. A flying
        drone whose position is unknown counts as too close. A lost cutting
        drone counts at its last reported position only for
        ``LOST_OBSTACLE_S`` after the loss (``_lost_obstacle_locked``): it
        may still hover there until it lands by itself, but its position is
        frozen, so the chunk must not wait for ever (review N9). *rid*'s
        path is the one it actually flies (``_actual_path_locked``).
        """
        gap = self._plan.gap_cm if self._plan is not None else self._zone_gap_cm
        if gap <= 0 or not self._retask_separation:
            return None
        cutter = self._drones[cutter_id]
        if cutter.lost:
            point = self._lost_obstacle_locked(cutter_id)
            if point is None:
                return None
            return self._point_too_close_locked(rid, lanes, cutter_id, point, gap, lost=True)
        telemetry = cutter.telemetry or {}
        if telemetry.get("is_flying") is False:
            return None
        rel_x, rel_y = _number(telemetry.get("rel_x_cm")), _number(telemetry.get("rel_y_cm"))
        if rel_x is None or rel_y is None:
            return f"pass {cutter_id}, whose position is not known"
        origin = self._origin(cutter)
        point = (origin[0] + rel_x, origin[1] + rel_y)
        return self._point_too_close_locked(rid, lanes, cutter_id, point, gap, lost=False)

    def _near_lost_locked(self, rid: str, lanes: list[dict], skip: str | None = None) -> str | None:
        """Why *rid* flying *lanes* would come too close to a lost drone that may still hover, or None.

        Every lost drone other than *rid* and *skip* is checked at its last
        reported position while ``_lost_obstacle_locked`` places it there:
        until ``LOST_OBSTACLE_S`` after the loss, or after its last telemetry
        that showed it flying if that came later, and never once its
        telemetry says it is not flying. Caller holds the lock. A chunk this
        stops waits (it is never failed for it) and is checked again on the
        next telemetry of any drone.
        """
        gap = self._plan.gap_cm if self._plan is not None else self._zone_gap_cm
        if gap <= 0 or not self._retask_separation:
            return None
        for other_id, other in self._drones.items():
            if other_id in (rid, skip) or not other.lost:
                continue
            point = self._lost_obstacle_locked(other_id)
            if point is None:
                continue
            why = self._point_too_close_locked(rid, lanes, other_id, point, gap, lost=True)
            if why is not None:
                return why
        return None

    def _offer_reflight_locked(
        self, chunk: _Chunk, planned: dict[str, list[dict]], batches: dict[str, list[_Chunk]],
    ) -> None:
        """Offer one queued re-flight chunk, keep it waiting, or fail it. Part of ``_dispatch_locked``.

        A chunk still marked ``confirm`` (a person cut the operator has not
        confirmed) is left queued and never offered. Otherwise the chunk
        waits until its cutting drone is done (``_cutter_done_locked``).
        When the cutting drone was lost, its segments on lanes re-tasked whole
        are superseded and dropped (``_drop_superseded_locked``); the chunk
        fails with ``superseded`` only when nothing else is left. It fails
        with ``too_short`` when no segment is long enough to fly. Otherwise
        it goes to the nearest ``_reflight_receivers`` drone, preferring
        drones without a pending offer, whose own geofence contains its whole
        new path and whose new path keeps the zone gap from every other
        drone's path, from any lane the cutting drone was given after it
        finished (its finished path is not checked: the segments lie on it),
        and from the cutting drone's position while it is in the air. Every
        path is the one the drone actually flies (``_actual_path_locked``),
        and the transit starts where the receiver actually ends its lanes. It
        keeps waiting while a drone that refused it for now (``landing``,
        ``busy``) may take it later, while a drone has no fresh telemetry or
        is taking off (``held``, review N7), while a drone that would take it
        has not finished its own lanes (``_own_lanes_left_locked``), or while
        only the cutting drone's position is in the way; it is re-evaluated on each telemetry message
        of the cutting drone, and of every drone while it is held. With no
        drone left it fails with the reason and is never offered again.
        """
        cutter_id = chunk.lost_id
        cutter = self._drones[cutter_id]
        if self._landed_all:
            self._fail_reflight_locked(chunk, "LAND ALL issued")
            return
        if chunk.confirm:
            return  # a person cut: only the operator's confirm_reflight lets it be offered
        if not chunk.gate_open:
            if not self._cutter_done_locked(cutter_id):
                return
            chunk.gate_open = True
            chunk.cutter_lanes = len(cutter.lanes)
        self._refresh_reflight_locked(chunk)
        if cutter.lost and not self._drop_superseded_locked(chunk):
            return
        if not chunk.lanes:
            self._fail_reflight_locked(chunk, SEGMENT_TOO_SHORT)
            return
        receivers, why, soon = self._reflight_candidates_locked(cutter_id)
        chunk.too_close = {}
        fits = []
        waiting = False
        crowded = None
        busy: list[str] = []
        for rid in receivers:
            if rid in chunk.refusals:
                continue
            lanes = planned.get(rid, self._drones[rid].lanes)
            end = self._actual_path_locked(rid, lanes)[-1]
            added = _transit(end, chunk.start) + [dict(lane) for lane in chunk.lanes]
            if not self._path_inside_fence(rid, lanes + added):
                continue
            too_close = self._too_close(rid, lanes + added, planned, flown=(cutter_id, chunk.cutter_lanes))
            if too_close is None:
                too_close = (self._near_cutter_locked(rid, lanes + added, cutter_id)
                             or self._near_lost_locked(rid, lanes + added, skip=cutter_id))
                if too_close is not None and crowded is None:
                    crowded = f"{rid} would {too_close}"
            if too_close is not None:
                chunk.too_close[rid] = too_close
                continue
            if rid in chunk.deferred:
                waiting = True  # refused for now: wait, do not fail
            elif (left := self._own_lanes_left_locked(rid)) is not None:
                waiting = True  # it takes the re-flight once its own lanes are done
                busy.append(left)
            else:
                fits.append((rid, end, added))
        chunk.crowded = None if fits else crowded
        held = [f"{rid} {why[rid]}" for rid in soon if rid not in chunk.refusals] + busy
        self._hold_locked(chunk, None if fits or not held else "; ".join(held))
        if not fits and (waiting or crowded or held):
            return
        if not fits:
            reason = self._failure_reason(chunk, receivers) if receivers else self._no_reflight_receiver(cutter_id, why)
            self._fail_reflight_locked(chunk, reason)
            return
        free = [fit for fit in fits if self._drones[fit[0]].offer is None]
        if not free:
            return
        fresh = [fit for fit in free if fit[0] not in batches]
        rid, _, added = min(fresh or free, key=lambda fit: math.dist(fit[1], chunk.start))
        base = planned.get(rid, self._drones[rid].lanes)
        planned[rid] = base + added
        batches.setdefault(rid, []).append(chunk)
        chunk.status = "offered"
        chunk.receiver = rid
        chunk.appended = [dict(lane) for lane in added]
        chunk.appended_start = len(base)
        if rid not in chunk.offered_to:
            chunk.offered_to.append(rid)

    def _drop_superseded_locked(self, chunk: _Chunk) -> bool:
        """Drop the segments of a lost cutting drone's chunk that lie on lanes re-tasked whole (review N8).

        When the cutting drone is lost its lanes from the one it was on are
        re-tasked whole, so a shadow on them is flown with them: it is
        superseded and leaves the chunk. Everything else, the cut's own
        segment included, is still offered. Returns False when nothing is
        left, after failing the chunk with ``superseded``.
        """
        whole_from = len(self._drones[chunk.lost_id].lanes)
        kept = [seg for seg in chunk.segments if seg.lane_index < whole_from]
        short = [seg for seg in chunk.too_short if seg.lane_index < whole_from]
        if len(kept) == len(chunk.segments) and len(short) == len(chunk.too_short):
            return True
        logger.info("%s: lanes %s are re-tasked whole, so their segments are superseded",
                    _chunk_what(chunk),
                    ", ".join(str(seg.lane_index) for seg in chunk.segments + chunk.too_short
                              if seg.lane_index >= whole_from))
        if not kept and not short:
            self._fail_reflight_locked(chunk, "superseded")
            return False
        plan = plan_reflight(kept)
        chunk.lanes = [dict(lane) for lane in plan.lanes]
        chunk.points = list(plan.points)
        chunk.legs = list(plan.legs)
        chunk.segments = list(plan.segments)
        chunk.too_short = short + list(plan.too_short)
        return True

    def _no_reflight_receiver(self, cutter_id: str, why: dict[str, str]) -> str:
        """Why no drone at all may re-fly *cutter_id*'s segments."""
        parts = [f"{rid} {text}" for rid, text in why.items()]
        parts += [
            f"{rid} is held for manual control" for rid, other in self._drones.items()
            if rid != cutter_id and not other.lost and other.zone is not None and self._excluded(rid)
        ]
        if not parts:
            return "no drone left to take them"
        return "no drone can re-fly it: " + "; ".join(parts)

    def _fail_reflight_locked(self, chunk: _Chunk, reason: str) -> None:
        """Close a re-flight chunk for good with *reason*; its segments stay uncovered with that reason."""
        chunk.status = "failed"
        chunk.receiver = None
        chunk.reason = reason
        chunk.crowded = None
        chunk.held = None
        logger.error("Re-flight of %s (lanes %s) failed: %s", _chunk_what(chunk),
                     ", ".join(str(seg.lane_index) for seg in chunk.segments + chunk.too_short), reason)

    def _track_reflights_locked(self, drone_id: str, state: _DroneState) -> bool:
        """Follow the re-flights after one telemetry message of *drone_id*.

        For each re-flight *drone_id* accepted and has not finished: its
        first cut on the lanes the offer appended (the transit included)
        makes it ``blocked_again``, for good, or, when that cut is a
        ``sensor_down``, fails it because the sensor went down on it; a
        ``sensor_down`` cut before them, which ends its lanes, fails it.
        Either way what the receiver did search counts (``flown_rest``). Else
        a ``lane_index`` past the appended lanes ends it: ``reflown`` when the
        receiver's replayed path searched every segment
        (``_reflight_rest_locked`` leaves nothing), and otherwise ``failed``
        with what it flew off the planned path, for example after a cut on
        its own lanes moved where it flew them from. Telemetry that shows the
        receiver not flying before that (it landed, for example on its
        battery gate, without a cut) fails it with ``<receiver> landed before
        it finished the re-flight``, and only what the receiver left
        unsearched stays listed: the legs of every appended lane from its
        ``lane_index`` on are listed whole (``landed_at``). A ``lane_index``
        at or past their start makes it ``flying``. Returns True when a re-flight
        changed, when *drone_id* has a waiting re-flight of its own cuts that
        this telemetry may release or let through, or when a re-flight is
        held for a receiver that may now take it.
        """
        telemetry = state.telemetry or {}
        lane_index = telemetry.get("lane_index")
        if not isinstance(lane_index, int) or isinstance(lane_index, bool):
            lane_index = None
        cuts = [state.obstacle_cuts[n] for n in sorted(state.obstacle_cuts)]
        wanted = False
        for chunk in self._reflights:
            if chunk.status == "queued" and (chunk.lost_id == drone_id or chunk.held or chunk.crowded):
                wanted = True
            span = chunk.appended_range
            if chunk.flown_by != drone_id or chunk.status not in ("acked", "flying") or span is None:
                continue
            first, end = span
            before = chunk.status
            hit = next((cut for cut in cuts if first <= cut.lane_index < end), None)
            if hit is not None and hit.reason == CUT_SENSOR_DOWN:
                chunk.flown_rest = True
                self._fail_reflight_locked(chunk, f"{drone_id} ended its lanes on it (obstacle sensor down)")
            elif hit is not None:
                chunk.status = "blocked_again"
                chunk.reason = f"blocked again by {drone_id}"
                chunk.flown_rest = True
                logger.warning("%s was blocked again re-flying %s; it is not offered again",
                               drone_id, _chunk_what(chunk))
            elif any(cut.reason == CUT_SENSOR_DOWN and cut.lane_index < first for cut in cuts):
                self._fail_reflight_locked(chunk, f"{drone_id} ended its lanes before it (obstacle sensor down)")
            elif lane_index is not None and lane_index >= end:
                rest = self._reflight_rest_locked(chunk)
                if rest:
                    length = sum(entry["distance_cm"] for entry in rest)
                    chunk.flown_rest = True
                    self._fail_reflight_locked(
                        chunk, f"{drone_id} flew it off the planned path, {length} cm not searched")
                else:
                    chunk.status = "reflown"
                    logger.info("%s re-flew %s", drone_id, _chunk_what(chunk))
            elif telemetry.get("is_flying") is False:
                # On the ground with appended lanes left and no cut on them:
                # it will not fly them, so the chunk would stay flying (or
                # acked) for ever. Without a lane index nothing counts as flown.
                chunk.landed_at = lane_index if lane_index is not None else first
                chunk.flown_rest = True
                self._fail_reflight_locked(chunk, f"{drone_id} landed before it finished the re-flight")
            elif lane_index is not None and lane_index >= first:
                chunk.status = "flying"
            wanted = wanted or chunk.status != before
        return wanted

    def _fail_lost_receiver_locked(self, drone_id: str) -> None:
        """Fail every re-flight the lost *drone_id* accepted and has not finished, with ``receiver lost``."""
        for chunk in self._reflights:
            if chunk.flown_by == drone_id and chunk.status in ("acked", "flying"):
                self._fail_reflight_locked(chunk, "receiver lost")

    def _lost_chunks_locked(self, drone_id: str, full_path: list[dict], index: int) -> list[_Chunk]:
        """The chunks that re-task a lost drone's lanes ``full_path[index:]``.

        Lanes that were appended to the drone to re-fly another drone's cut
        (the transit included) are left out, because a re-flight is offered
        once and no third drone may be handed it; the lanes around them are
        chunked as ``_make_chunks`` does.
        """
        origin = self._origin(self._drones[drone_id])
        spans = sorted(
            chunk.appended_range for chunk in self._reflights
            if chunk.flown_by == drone_id and chunk.appended_range is not None
        )
        if not spans:
            return self._make_chunks(drone_id, origin, full_path, index)
        chunks: list[_Chunk] = []
        start = index
        for first, end in spans:
            if end <= start:
                continue
            if first > start:
                chunks.extend(self._make_chunks(drone_id, origin, full_path[:first], start))
            start = end
        if start < len(full_path):
            chunks.extend(self._make_chunks(drone_id, origin, full_path, start))
        return chunks

    def _reflight_events_locked(self, chunk: _Chunk) -> list[tuple[str, dict]]:
        """The ``reflight_*`` event for *chunk*'s status, once per change of status or receiver.

        Each event carries ``drone_id`` (the cutting drone), ``receiver``,
        ``cut_seq``, ``lanes`` in the ``lane_entries()`` shape (the segments
        concerned on the shared grid; for ``reflight_blocked``, and for a
        ``reflight_failed`` after the receiver flew some of it, what was left
        unsearched) and a ``detail`` sentence. Waiting and flying are not
        announced.
        """
        mark = (chunk.status, chunk.receiver if chunk.status == "offered" else None)
        if chunk.announced == mark:
            return []
        chunk.announced = mark
        event = _REFLIGHT_EVENTS.get(chunk.status)
        if event is None:
            return []
        receiver = chunk.receiver if chunk.status == "offered" else chunk.flown_by
        if chunk.flown_rest:
            lanes = [{key: entry[key] for key in _LANE_ENTRY_KEYS} for entry in self._reflight_rest_locked(chunk)]
        elif chunk.status == "failed" and not chunk.segments:
            lanes = [_segment_entry(seg) for seg in chunk.too_short]
        else:
            lanes = chunk.lane_entries()
        length = sum(entry["distance_cm"] for entry in lanes)
        what = _chunk_what(chunk)
        detail = {
            "offered": f"{what} offered to {receiver} to re-fly: {length} cm",
            "acked": f"{receiver} will re-fly {what} after its lanes",
            "reflown": f"{receiver} re-flew {what}",
            "blocked_again": f"{what} was blocked again by {receiver}: {length} cm not searched, not offered again",
            "failed": f"{what} not re-flown: {chunk.reason}",
        }[chunk.status]
        payload = {
            "event": event,
            "drone_id": chunk.lost_id,
            "receiver": receiver,
            "cut_seq": chunk.cut_seq,
            "lanes": lanes,
            "detail": detail,
        }
        if chunk.offset is not None:
            payload["reason"] = SEGMENT_OFFSET
        return [("fleet_event", payload)]

    # ------------------------------------------------------------------
    # Emergency landing
    # ------------------------------------------------------------------

    def land_all(self) -> dict[str, BaseException | None]:
        """Send ``emergency_land_all`` to every link, whatever fails on the way.

        Every link is attempted even if an earlier one raises. A
        ``KeyboardInterrupt`` or ``SystemExit`` raised by a link is re-raised
        only after every other link has been sent the message. Lost drones
        are included. The call takes no lock, so it never waits behind a
        blocking ``sync()`` or ``start()``.

        Returns a mapping of drone id to ``None`` (sent) or the exception.
        """
        self._landed_all = True
        results: dict[str, BaseException | None] = {}
        interrupt: BaseException | None = None
        for drone_id, link in self._links.items():
            try:
                link.send({"type": "emergency_land_all"})
                results[drone_id] = None
            except Exception as exc:
                logger.error("emergency_land_all to %s failed: %s", drone_id, exc)
                results[drone_id] = exc
            except BaseException as exc:
                logger.error("emergency_land_all to %s interrupted: %r", drone_id, exc)
                results[drone_id] = exc
                if interrupt is None:
                    interrupt = exc
        failed = [drone_id for drone_id, exc in results.items() if exc is not None]
        self._emit("fleet_event", {
            "event": "land_all",
            "detail": f"failed: {', '.join(failed)}" if failed else "sent to all",
        })
        if interrupt is not None:
            raise interrupt
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_logged(self, drone_id: str, msg: dict) -> bool:
        try:
            self._links[drone_id].send(msg)
            return True
        except Exception:
            logger.error("Sending %s to %s failed", msg.get("type"), drone_id, exc_info=True)
            return False

    def _emit_all(self, events: list[tuple[str, dict]]) -> None:
        for name, payload in events:
            self._emit(name, payload)

    def _emit(self, name: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(name, payload)
        except Exception:
            logger.exception("on_event callback raised for %s", name)
