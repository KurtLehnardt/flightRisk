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

from flightrisk.drone.search import split_parallel_track_zones

logger = logging.getLogger(__name__)

SYNC_ROUNDS = 8
"""Ping/pong rounds per link in one ``sync()``."""

DEFAULT_ZONE_GAP_CM = 100
"""Empty corridor between neighbouring zones."""

MIN_START_BATTERY = 30
"""Lowest battery percentage that lets a drone take part in ``start()``."""

TELEMETRY_FRESH_S = 2.0
"""Telemetry older than this is ignored by the start battery gate."""

TELLO_MIN_MOVE_CM = 20
TELLO_MAX_MOVE_CM = 500

RETASK_WAIT_S = 2.0
"""How long ``on_drone_lost`` waits for the receiving drones to answer
before it reports the re-task. Later answers are reported as they arrive."""

# Floating-point slack in the geofence check, matching SafetyEnvelope.
_FENCE_TOLERANCE_CM = 1e-6

_DIRECTION_VECTORS = {
    "forward": (0, 1),
    "back": (0, -1),
    "right": (1, 0),
    "left": (-1, 0),
}

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

    def zone_dict(self) -> dict:
        return {"x_cm": self.x_cm, "y_cm": self.y_cm, "w_cm": self.w_cm, "h_cm": self.h_cm}

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

    def zone_for(self, drone_id: str) -> ZoneAssignment | None:
        for zone in self.zones:
            if zone.drone_id == drone_id:
                return zone
        return None

    def to_dict(self) -> dict:
        """Shape of the dashboard ``fleet_plan`` event."""
        return {
            "zones": [zone.to_dict() for zone in self.zones],
            "grid": {"x_cm": 0, "y_cm": 0, "w_cm": self.width_cm, "h_cm": self.depth_cm},
            "gap_cm": self.gap_cm,
            "strip_cm": self.strip_cm,
        }


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
    """A contiguous run of a lost drone's unflown lanes, re-tasked as one piece.

    ``points`` are the shared-grid positions along the run: its start, then
    the end of each lane. ``status`` is ``queued`` (waiting for a drone),
    ``offered`` (sent to ``receiver``, no answer yet), ``acked`` (accepted
    by ``receiver``) or ``failed`` (no drone can take it, see ``reason``).
    ``appended`` is what the offer added to the receiver's lanes for this
    run: the transit to its start, then its lanes. ``refusals`` maps each
    drone that refused the run to its reason, and ``reported`` is the status
    last sent to the dashboard.
    """

    lost_id: str
    first_index: int
    lanes: list[dict]
    points: list[tuple[int, int]]
    status: str = "queued"
    receiver: str | None = None
    appended: list[dict] = field(default_factory=list)
    refusals: dict[str, str] = field(default_factory=dict)
    reason: str | None = None
    reported: str | None = None

    @property
    def start(self) -> tuple[int, int]:
        return self.points[0]

    def lane_entries(self) -> list[dict]:
        """One entry per lane: its index in the lost drone's lanes, move and grid ends."""
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


def _number(value: Any) -> float | None:
    """Return ``value`` as a float if it is a real number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return float(value)


def _lane(direction: str, distance_cm: int) -> dict:
    return {"direction": direction, "distance_cm": int(distance_cm)}


def _path_point(origin: tuple[int, int], lanes: list[dict], upto: int | None = None) -> tuple[int, int]:
    """Position on the shared grid after flying ``lanes[:upto]`` from ``origin``."""
    x, y = origin
    for lane in lanes[:upto]:
        dx, dy = _DIRECTION_VECTORS.get(lane["direction"], (0, 0))
        x += dx * lane["distance_cm"]
        y += dy * lane["distance_cm"]
    return x, y


def _axis_moves(delta_cm: int, positive: str, negative: str) -> list[dict]:
    """Tello-valid moves (20 to 500 cm each) whose net displacement is ``delta_cm``.

    A displacement shorter than the Tello minimum is flown as an overshoot
    followed by a minimum-length return, so the net result stays exact.
    """
    if delta_cm == 0:
        return []
    direction, opposite = (positive, negative) if delta_cm > 0 else (negative, positive)
    distance = abs(delta_cm)
    if distance < TELLO_MIN_MOVE_CM:
        return [_lane(direction, distance + TELLO_MIN_MOVE_CM), _lane(opposite, TELLO_MIN_MOVE_CM)]
    segments = math.ceil(distance / TELLO_MAX_MOVE_CM)
    base, remainder = divmod(distance, segments)
    return [_lane(direction, base + (1 if i < remainder else 0)) for i in range(segments)]


def _transit(start: tuple[int, int], end: tuple[int, int]) -> list[dict]:
    """Moves from ``start`` to ``end``: forward or back first, then sideways.

    Matching the forward position first keeps the drone inside its own zone
    until it crosses the gap sideways at the target's forward position.
    """
    return _axis_moves(end[1] - start[1], "forward", "back") + _axis_moves(end[0] - start[0], "right", "left")


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
        dedup_radius_cm: Detections closer than this on the shared grid are
            merged, whichever drones reported them.
        zone_gap_cm: Empty corridor between neighbouring zones.
        min_battery: Lowest battery percentage allowed at ``start()``.
        clock: Monotonic ground clock in seconds.
        on_event: Optional callback ``(event_name, payload)`` for the
            dashboard. It receives ``fleet_detection`` for each new merged
            detection and ``fleet_event`` for ``drone_lost``, ``retask``,
            ``retask_failed`` and ``land_all``. Exceptions it raises are
            logged and ignored.
        geofence_cm: Half-width of the square geofence each edge keeps
            around its takeoff point (``FLIGHTRISK_GEOFENCE_CM`` on the
            edge). Re-tasked lanes are offered only to drones whose geofence
            contains their whole new path. ``None`` plans without a fence;
            the edges still enforce their own and answer with a ``nack``.
        retask_wait_s: How long ``on_drone_lost`` waits for the receiving
            drones to answer before reporting the re-task.
    """

    def __init__(
        self,
        links: Mapping[str, EdgeLink],
        area_cm: int | tuple[int, int],
        strip_cm: int,
        alt_slots_cm: tuple[int, ...] = (100, 160),
        dedup_radius_cm: float = 80,
        *,
        zone_gap_cm: int = DEFAULT_ZONE_GAP_CM,
        min_battery: float = MIN_START_BATTERY,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, dict], None] | None = None,
        geofence_cm: float | None = None,
        retask_wait_s: float = RETASK_WAIT_S,
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
        self._dedup_radius_cm = float(dedup_radius_cm)
        self._zone_gap_cm = int(zone_gap_cm)
        self._min_battery = float(min_battery)
        self._clock = clock
        self._on_event = on_event
        self._geofence_cm = float(geofence_cm) if geofence_cm is not None else None
        self._retask_wait_s = float(retask_wait_s)

        self._lock = threading.RLock()
        self._drones: dict[str, _DroneState] = {drone_id: _DroneState() for drone_id in self._links}
        self._plan: FleetPlan | None = None
        self._started_at: float | None = None
        self._landed_all = False
        self._detections: list[FleetDetection] = []
        self._next_detection_id = 1
        self._retasks: list[_Retask] = []

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
        drone's id.
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
            ]
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
                }
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
                alt_cm=self._alt_slots_cm[i % len(self._alt_slots_cm)],
                lanes=lanes,
            ))
        plan = FleetPlan(
            zones=tuple(zones),
            width_cm=self._width_cm,
            depth_cm=self._depth_cm,
            gap_cm=self._zone_gap_cm,
            strip_cm=self._strip_cm,
        )

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
            for zone in zones:
                state = self._drones[zone.drone_id]
                state.zone = zone
                state.lanes = [dict(lane) for lane in zone.lanes]
                state.lost = False
                state.offer = None
                state.nack_reason = None
                state.executed_at_ground_s = None
                self._clear_ack(state)
                messages[zone.drone_id] = self._assign_message(zone.drone_id, state)
        for drone_id, msg in messages.items():
            self._send_logged(drone_id, msg)
        return plan

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
        return state.zone.x_cm, state.zone.y_cm

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
        some, and otherwise from the ``assign_lanes`` ack.

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
                fresh = _number((state.telemetry or {}).get("battery"))
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
            with self._lock:
                state = self._drones[drone_id]
                state.telemetry = dict(msg)
                state.telemetry_at = self._clock()
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
                    for chunk in offer.chunks:
                        chunk.status = "acked"
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
        if msg.get("for") != "assign_lanes":
            logger.debug("Ignoring nack for %r from %s", msg.get("for"), drone_id)
            return
        reason = msg.get("reason")
        reason = reason if isinstance(reason, str) and reason else "refused"
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
            logger.warning("%s refused its assign_lanes (%s)", drone_id, reason)
            if state.offer is not None:
                self._withdraw_offer_locked(drone_id, reason)
                messages = self._dispatch_locked()
                events = self._settle_and_collect_locked()
            else:
                self._clear_ack(state)
                state.nack_reason = reason
        self._emit_all(events)
        self._send_offers(messages)

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
                    retask = _Retask(drone_id, self._make_chunks(drone_id, self._origin(state), full_path, index))
                    self._retasks.append(retask)
            messages = self._dispatch_locked()
            events = self._settle_and_collect_locked()

        logger.warning("Drone %s lost; re-tasking its unflown lanes", drone_id)
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
        """Drones that can still be given lanes: not lost and part of the plan."""
        return [rid for rid, other in self._drones.items() if not other.lost and other.zone is not None]

    def _inside_fence(self, drone_id: str, point: tuple[float, float]) -> bool:
        if self._geofence_cm is None:
            return True
        origin = self._origin(self._drones[drone_id])
        limit = self._geofence_cm + _FENCE_TOLERANCE_CM
        return abs(point[0] - origin[0]) <= limit and abs(point[1] - origin[1]) <= limit

    def _path_inside_fence(self, drone_id: str, lanes: list[dict]) -> bool:
        """True if every point of ``lanes`` flown from the drone's takeoff is inside its geofence.

        Lanes are straight moves and the geofence is a square, so checking
        the end of every move covers the whole path.
        """
        if self._geofence_cm is None:
            return True
        x, y = self._origin(self._drones[drone_id])
        for lane in lanes:
            dx, dy = _DIRECTION_VECTORS.get(lane["direction"], (0, 0))
            x += dx * lane["distance_cm"]
            y += dy * lane["distance_cm"]
            if not self._inside_fence(drone_id, (x, y)):
                return False
        return True

    def _make_chunks(
        self, lost_id: str, origin: tuple[int, int], full_path: list[dict], index: int,
    ) -> list[_Chunk]:
        """Split ``full_path[index:]`` into chunks to offer.

        The lanes are first split into one contiguous piece per remaining
        drone. A piece is split again wherever no single drone's geofence
        contains all of it, and lanes outside every geofence are kept
        together so they are reported as one uncovered chunk.
        """
        unflown = full_path[index:]
        points = [_path_point(origin, full_path, index)]
        for lane in unflown:
            points.append(_path_point(points[-1], [lane]))
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
        ``assign_lanes``.
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
                for rid in self._receivers():
                    if rid in chunk.refusals:
                        continue
                    lanes = planned.get(rid, self._drones[rid].lanes)
                    end = _path_point(self._origin(self._drones[rid]), lanes)
                    added = _transit(end, chunk.start) + [dict(lane) for lane in chunk.lanes]
                    if self._path_inside_fence(rid, lanes + added):
                        fits.append((rid, end, added))
                if not fits:
                    self._fail_locked(chunk, self._failure_reason(chunk))
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

    def _failure_reason(self, chunk: _Chunk) -> str:
        receivers = self._receivers()
        if not receivers:
            return "no drone left to take them"
        parts = []
        refused = [rid for rid in receivers if rid in chunk.refusals]
        if refused:
            parts.append("refused by " + ", ".join(f"{rid} ({chunk.refusals[rid]})" for rid in refused))
        outside = [rid for rid in receivers if rid not in chunk.refusals]
        if outside:
            parts.append("outside the geofence of " + ", ".join(outside))
        return "; ".join(parts)

    def _fail_locked(self, chunk: _Chunk, reason: str) -> None:
        chunk.status = "failed"
        chunk.receiver = None
        chunk.reason = reason
        logger.error("Re-tasked lanes %d-%d of %s are uncovered: %s",
                     chunk.first_index, chunk.first_index + len(chunk.lanes) - 1, chunk.lost_id, reason)

    def _withdraw_offer_locked(self, drone_id: str, reason: str | None) -> None:
        """Take back a drone's pending offer and queue its chunks again.

        ``reason`` records a refusal, so the chunks are not offered to this
        drone again; ``None`` (the drone was lost) records nothing. The
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
            if reason is not None:
                chunk.refusals[drone_id] = reason

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
        as they are accepted or fail.
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
