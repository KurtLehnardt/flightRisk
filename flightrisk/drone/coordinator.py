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
events follow each offer to ``reflown`` or ``blocked_again``.

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
    CUT_SENSOR_DOWN,
    MAX_CUTS_LISTED,
    REFLIGHT_CUT_REASONS,
    SEGMENT_SKIPPED,
    SEGMENT_TOO_SHORT,
    SENSING_AUTOPILOT,
    SENSING_OFF,
    SENSING_STEPPED,
    SENSOR_UNAVAILABLE_DETAIL,
    TELEMETRY_CUT_REASONS,
    TELEMETRY_CUTS,
    TELEMETRY_CUTS_TOTAL,
    TELEMETRY_FIELDS,
    TELEMETRY_LATENCY_MS,
    TELEMETRY_SENSING,
    TELEMETRY_SENSOR_OK,
    TELEMETRY_SEQ,
    TELEMETRY_STATE,
)
from flightrisk.drone.plan_paths import closest_approach
from flightrisk.drone.search import split_parallel_track_zones

logger = logging.getLogger(__name__)

SYNC_ROUNDS = 8
"""Ping/pong rounds per link in one ``sync()``."""

DEFAULT_ZONE_GAP_CM = 100
"""Empty corridor between neighbouring zones."""

DEFAULT_ALT_SLOTS_CM = (100, 130)
"""Altitude slots given to zones in turn when no per-drone slot is set.

Both are at or below the default ceiling limit of 140 cm (200 cm ceiling
minus 60 cm margin, ``flightrisk.drone.safety``). The second slot was 160 cm
until a Tello at that slot hit a ceiling (docs/ceiling-and-altitude.md).
"""

MIN_START_BATTERY = 30
"""Lowest battery percentage that lets a drone take part in ``start()``."""

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
the other drones with re-flights."""

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
    # Re-flight only. Why the chunk waits for a receiver that may take it
    # once it reports fresh telemetry or has taken off (design section 3.4,
    # review N7), shown as the waiting segment's ``detail``.
    held: str | None = None
    # Re-flight only. Where ``appended`` starts in the receiver's lanes (set
    # with the offer), the drone that accepted it (kept when the chunk fails
    # afterwards), every drone it was offered to, and the (status, receiver)
    # last announced in a ``reflight_*`` event.
    appended_start: int | None = None
    flown_by: str | None = None
    offered_to: list[str] = field(default_factory=list)
    announced: tuple[str, str | None] | None = None
    # Re-flight only. True once the receiver flew the appended lanes, or
    # some of them, and the chunk ended without re-flying every segment
    # (``blocked_again``, or ``failed`` because the receiver's sensor went
    # down on them or it flew them off the planned path): what it left
    # unsearched is listed instead of the whole segments.
    flown_rest: bool = False

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
    """The re-tasking of one lost drone's unflown lanes."""

    lost_id: str
    chunks: list[_Chunk]
    settled: threading.Event = field(default_factory=threading.Event)
    reported: bool = False


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
    offer: _Offer | None = None
    nack_reason: str | None = None
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
        min_battery: Lowest battery percentage allowed at ``start()``.
        clock: Monotonic ground clock in seconds.
        on_event: Optional callback ``(event_name, payload)`` for the
            dashboard. It receives ``fleet_detection`` for each new merged
            detection and ``fleet_event`` for ``drone_lost``, ``retask``,
            ``retask_failed``, ``land_all``, ``lane_blocked`` and the
            re-flight events ``reflight_offered``, ``reflight_accepted``,
            ``reflight_done``, ``reflight_blocked`` and ``reflight_failed``.
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
        retask_separation: Offer re-tasked lanes only to a drone whose new
            path stays the zone gap away from every other drone's path (the
            default). False switches that check off; only tests of the offer
            and refusal mechanics use it.
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
        min_battery: float = MIN_START_BATTERY,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, dict], None] | None = None,
        geofence_cm: float | None = None,
        retask_wait_s: float = RETASK_WAIT_S,
        alt_slot_of: Callable[[str, int], int] | None = None,
        retask_excluded: Callable[[str], bool] | None = None,
        retask_separation: bool = True,
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
        self._min_battery = float(min_battery)
        self._clock = clock
        self._on_event = on_event
        self._geofence_cm = float(geofence_cm) if geofence_cm is not None else None
        self._retask_wait_s = float(retask_wait_s)
        self._retask_excluded = retask_excluded
        self._retask_separation = bool(retask_separation)

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
        it is offered to another drone.
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
                if state.obstacle_cuts or any(key in telemetry for key in TELEMETRY_FIELDS):
                    # Only for a drone that reports obstacle sensing, so the
                    # status of a fleet without it (and the mobile contract
                    # fixtures built from it) is unchanged.
                    entry.update({key: telemetry.get(key) for key in _STATUS_OBSTACLE_KEYS})
                    # What current_plan() lists for the drone: a re-flown
                    # segment no longer counts, and what a re-flight left of
                    # one counts for the drone that cut it.
                    if uncovered is None:
                        uncovered = self._obstacle_uncovered_locked()
                    entry["uncovered_cm"] = sum(u["distance_cm"] for u in uncovered if u["drone_id"] == drone_id)
                    entry["reflights"] = self._reflight_counts_locked(drone_id)
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
                state.offer = None
                state.nack_reason = None
                state.executed_at_ground_s = None
                state.retask_starts = []
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
            with self._lock:
                state = self._drones[drone_id]
                state.telemetry = dict(msg)
                state.telemetry_at = self._clock()
                events.extend(self._note_cuts_locked(drone_id, state, state.telemetry))
                # A re-flight this drone flies may have ended, or one of its
                # own cuts may now be offered (design section 3.4).
                reflight = self._track_reflights_locked(drone_id, state)
                if self._release_deferred_locked(drone_id, state.telemetry) or reflight:
                    messages = self._dispatch_locked()
                    events.extend(self._settle_and_collect_locked())
                else:
                    # A re-flight may have changed without a dispatch (an
                    # edge restart fails its cutter's waiting chunks).
                    for chunk in self._reflights:
                        events.extend(self._reflight_events_locked(chunk))
            self._emit_all(events)
            self._send_offers(messages)
        elif msg_type == "detections":
            self.on_detection(drone_id, msg)
        else:
            logger.debug("Ignoring %r message from %s", msg_type, drone_id)

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
        (``obstacle``, ``person`` and ``sensor_stale``; never ``sensor_down``,
        ``autopilot`` or a lone ``skipped`` shadow, which rides with its cut),
        when it is not on lanes this drone flies to re-fly another drone's cut
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
        taken = sum(1 for chunk in self._reflights if chunk.lost_id == drone_id)
        for number, cut in new_cuts:
            if cut.reason not in REFLIGHT_CUT_REASONS:
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
                kind="reflight", cut_seq=number,
            )
            self._refresh_reflight_locked(chunk)
            if not chunk.segments and not chunk.too_short:
                continue  # Nothing of it in the replay; _note_cuts_locked has said so.
            self._reflights.append(chunk)
            taken += 1
            logger.info("%s's cut %d on lane %d waits to be offered to another drone", drone_id, number, cut.lane_index)
        for chunk in self._reflights:
            if chunk.lost_id == drone_id and chunk.status == "queued":
                self._refresh_reflight_locked(chunk)

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
        its sensor down on it, or flown off the planned path) is replaced by
        what the receiver left unsearched of it (``_reflight_rest_locked``).
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
            if not state.obstacle_cuts:
                continue
            numbers, segments = self._obstacle_replay_locked(state)
            origin = self._origin(state)
            reflight_lanes = appended.get(drone_id, set())
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

    def on_drone_lost(self, drone_id: str, wait_s: float | None = None) -> dict[str, list[dict]]:
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
            logger.warning("Drone %s lost; re-tasking its unflown lanes", drone_id)
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
                        retask = _Retask(drone_id, chunks)
                        self._retasks.append(retask)
            messages = self._dispatch_locked()
            events = self._settle_and_collect_locked()

        self._send_logged(drone_id, {"type": "land"})
        self._emit("fleet_event", {"event": "drone_lost", "drone_id": drone_id})
        self._emit_all(events)
        self._send_offers(messages)
        if retask is None:
            return {}
        if wait_s > 0:
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
        after it either (``lane_steps``). Telemetry without the obstacle
        fields changes nothing.
        """
        state = self._drones[rid]
        telemetry = state.telemetry or {}
        if telemetry.get(TELEMETRY_SENSING) == SENSING_STEPPED and telemetry.get(TELEMETRY_SENSOR_OK) is False:
            return "reports its obstacle sensor is not ok"
        if any(cut.reason == CUT_SENSOR_DOWN for cut in state.obstacle_cuts.values()):
            return "ended its lanes (obstacle sensor down)"
        return None

    def _inside_fence(self, drone_id: str, point: tuple[float, float]) -> bool:
        if self._geofence_cm is None:
            return True
        origin = self._origin(self._drones[drone_id])
        limit = self._geofence_cm + _FENCE_TOLERANCE_CM
        return abs(point[0] - origin[0]) <= limit and abs(point[1] - origin[1]) <= limit

    def _path_inside_fence(self, drone_id: str, lanes: list[dict]) -> bool:
        """True if every point of ``lanes`` as the drone actually flies them is inside its geofence.

        The path is ``_actual_path_locked`` (from the drone's takeoff point,
        with its cuts). Lanes are straight moves and the geofence is a
        square, so checking the end of every move covers the whole path.
        """
        if self._geofence_cm is None:
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
        sensing (``_unfit_receiver_locked``) is never offered any. The
        transit to a chunk starts where the receiver will actually be after
        its lanes (``_actual_path_locked``), and the fence and the separation
        are checked on the path it will actually fly.
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
                fits = []
                waiting = False
                unfit: dict[str, str] = {}
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
                        else:
                            fits.append((rid, end, added))
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
        cuts flies its lanes as planned.
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
            if distance < gap:
                return f"come within {distance:.0f} cm of {other_id}, below the {gap} cm gap"
        return None

    def _fail_locked(self, chunk: _Chunk, reason: str) -> None:
        chunk.status = "failed"
        chunk.receiver = None
        chunk.reason = reason
        logger.error("Re-tasked lanes %s of %s are uncovered: %s",
                     ", ".join(str(entry["index"]) for entry in chunk.lane_entries()), chunk.lost_id, reason)

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
        pending = [c for c in retask.chunks if c.status in ("queued", "offered")]
        events = []
        if acked or pending:
            events.append(("fleet_event", self._retask_payload(retask.lost_id, acked, pending)))
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
        Lanes still waiting for an answer are listed under ``awaiting``.
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
                awaiting.extend(dict(entry, drone_id=chunk.receiver) for entry in chunk.lane_entries())
            parts.append(", ".join(f"{n} lanes awaiting {who}" for who, n in waiting.items()))
            payload["awaiting"] = awaiting
        payload["detail"] = "; ".join(parts)
        return payload

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
        every segment whole.

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
            if k > blocked_at or lane + 1 >= len(path):
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
            elif battery < MIN_START_BATTERY:
                why[rid] = f"has battery {battery:g}%, below {MIN_START_BATTERY}%"
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
        ``is_flying: true``, a battery of at least ``MIN_START_BATTERY``
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

    def _near_cutter_locked(self, rid: str, lanes: list[dict], cutter_id: str) -> str | None:
        """Why *rid* flying *lanes* would come too close to the cutting drone where it is now, or None.

        While the cutting drone is in the air, its position from telemetry
        (relative to its takeoff point, placed on the shared grid as
        ``status()`` does) is a point obstacle at the zone gap; a cutting
        drone whose telemetry says it is not flying is ignored, and so is a
        lost one, as ``_too_close`` ignores lost drones (its last position is
        frozen, so the chunk would wait for ever; review N9). A flying drone
        whose position is unknown counts as too close. *rid*'s path is the
        one it actually flies (``_actual_path_locked``).
        """
        gap = self._plan.gap_cm if self._plan is not None else self._zone_gap_cm
        if gap <= 0 or not self._retask_separation:
            return None
        cutter = self._drones[cutter_id]
        if cutter.lost:
            return None
        telemetry = cutter.telemetry or {}
        if telemetry.get("is_flying") is False:
            return None
        rel_x, rel_y = _number(telemetry.get("rel_x_cm")), _number(telemetry.get("rel_y_cm"))
        if rel_x is None or rel_y is None:
            return f"pass {cutter_id}, whose position is not known"
        origin = self._origin(cutter)
        point = (origin[0] + rel_x, origin[1] + rel_y)
        mine = self._actual_path_locked(rid, lanes)
        distance, _, _ = closest_approach(mine, [point, point])
        if distance < gap:
            return (f"come within {distance:.0f} cm of {cutter_id} at ({point[0]:g}, {point[1]:g}) cm, "
                    f"below the {gap} cm gap")
        return None

    def _offer_reflight_locked(
        self, chunk: _Chunk, planned: dict[str, list[dict]], batches: dict[str, list[_Chunk]],
    ) -> None:
        """Offer one queued re-flight chunk, keep it waiting, or fail it. Part of ``_dispatch_locked``.

        The chunk waits until its cutting drone is done (``_cutter_done_locked``).
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
        is taking off (``held``, review N7), or while only the cutting drone's
        position is in the way; it is re-evaluated on each telemetry message
        of the cutting drone, and of every drone while it is held. With no
        drone left it fails with the reason and is never offered again.
        """
        cutter_id = chunk.lost_id
        cutter = self._drones[cutter_id]
        if self._landed_all:
            self._fail_reflight_locked(chunk, "LAND ALL issued")
            return
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
                too_close = self._near_cutter_locked(rid, lanes + added, cutter_id)
                if too_close is not None and crowded is None:
                    crowded = f"{rid} would {too_close}"
            if too_close is not None:
                chunk.too_close[rid] = too_close
                continue
            if rid in chunk.deferred:
                waiting = True  # refused for now: wait, do not fail
            else:
                fits.append((rid, end, added))
        chunk.crowded = None if fits else crowded
        held = [rid for rid in soon if rid not in chunk.refusals]
        chunk.held = None if fits or not held else "; ".join(f"{rid} {why[rid]}" for rid in held)
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
        logger.info("%s's cut %s: lanes %s are re-tasked whole, so their segments are superseded",
                    chunk.lost_id, chunk.cut_seq,
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
        logger.error("Re-flight of %s's cut %s (lanes %s) failed: %s", chunk.lost_id, chunk.cut_seq,
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
        its own lanes moved where it flew them from. A ``lane_index`` at or
        past their start makes it ``flying``. Returns True when a re-flight
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
            if chunk.status == "queued" and (chunk.lost_id == drone_id or chunk.held):
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
                logger.warning("%s was blocked again re-flying %s's cut %s; it is not offered again",
                               drone_id, chunk.lost_id, chunk.cut_seq)
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
                    logger.info("%s re-flew %s's cut %s", drone_id, chunk.lost_id, chunk.cut_seq)
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
        what = f"{chunk.lost_id}'s blocked segment (cut {chunk.cut_seq})"
        detail = {
            "offered": f"{what} offered to {receiver} to re-fly: {length} cm",
            "acked": f"{receiver} will re-fly {what} after its lanes",
            "reflown": f"{receiver} re-flew {what}",
            "blocked_again": f"{what} was blocked again by {receiver}: {length} cm not searched, not offered again",
            "failed": f"{what} not re-flown: {chunk.reason}",
        }[chunk.status]
        return [("fleet_event", {
            "event": event,
            "drone_id": chunk.lost_id,
            "receiver": receiver,
            "cut_seq": chunk.cut_seq,
            "lanes": lanes,
            "detail": detail,
        })]

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
