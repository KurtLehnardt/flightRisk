"""A seeded schedule of random link faults, and the monitor that checks a fleet under it.

``plan_episodes`` is a pure function of its seed: the same seed gives the same
list of ``Episode`` objects. ``ChaosRun`` plays a plan against a
``TwoNodeHarness`` in real time while a sampler thread watches the safety
invariants that hold for the fleet code on main:

* no drone stays airborne once its link-loss watchdog has been expired for
  longer than ``slack_s`` (the drone lands when the heartbeats stop, and a
  landing is never held up by a busy command channel or a stalled link);
* an ``emergency_land_all`` reaches every edge whose connection was healthy
  from before the call until the deadline, the drone is then told to land
  (a ``land`` call after the message), and the call itself reaches every
  socket the ground holds;
* the ground can order a takeoff again after every outage (the run keeps the
  fleet flying so that every outage is a test of the watchdog);
* one edge flies real lanes through the real ``LaneExecutor`` (``assign_lanes``
  and ``start_at`` over the faulty link, extended while it flies), so the
  ``busy`` fault, which only affects ``move``, has something to hit;
* in each drone's command log nothing moves after a landing until the next
  takeoff, and takeoffs and landings alternate (never two flights at once).

What the plan guarantees. Random episodes alone could leave a short run
without the faults that give these checks teeth (with some seeds no
emergency, or no partition long enough to outlast the watchdog). So every plan
contains, at fixed fractions of the run, one ``busy`` episode on the lane
drone, one down-direction (or two-way) partition lasting
``limit_s + slack_s + 2`` seconds, one ``sever`` and one ``emergency``, and
every random episode that would overlap one of them is removed, so they run
alone and cannot be undone by a concurrent heal or leave the fleet
disconnected. A run shorter than ``MIN_SOAK_S`` cannot hold them and is
refused. Errors in read loops and leaked resources are checked by the caller
with ``ErrorWatch`` and ``snapshot``.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from flightrisk import fleet_protocol as fp
from tests.support.fleet_harness import TwoNodeHarness
from tests.support.netsim import BOTH, DOWN, UP

KINDS = ("latency", "loss", "reorder", "partition", "partition_all", "sever", "flap", "busy", "emergency", "quiet")
_WEIGHTS = (14, 10, 6, 18, 8, 7, 10, 6, 8, 5)

#: The shortest run that fits the guaranteed episodes with room for random ones around them.
MIN_SOAK_S = 45.0
#: Where the guaranteed episodes start, as fractions of the run.
BUSY_AT, DOWN_AT, SEVER_AT, EMERGENCY_AT = 0.15, 0.30, 0.50, 0.65
BUSY_S = 4.0
EMERGENCY_WAIT_S = 1.6
#: Clear space kept around a guaranteed episode: (seconds before its start, seconds after its end).
_CLEARANCE = {"busy": (3.0, 1.0), "partition": (3.0, 1.0), "sever": (3.0, 3.0), "emergency": (3.0, 3.0)}

#: A square flown by the lane drone; repeating it keeps the drone inside its geofence.
LANE_SQUARE = (
    {"direction": "forward", "distance_cm": 150}, {"direction": "right", "distance_cm": 100},
    {"direction": "back", "distance_cm": 150}, {"direction": "left", "distance_cm": 100},
)
LANE_ZONE = {"x_cm": 0, "y_cm": 0, "w_cm": 300, "h_cm": 300}
MOTION = frozenset({"move", "rotate", "rc_control"})


@dataclass(frozen=True)
class Episode:
    start_s: float
    kind: str
    target: str  # a drone id, or "all"
    direction: str
    duration_s: float
    params: tuple[tuple[str, Any], ...] = ()

    def get(self, name: str, default: Any = None) -> Any:
        return dict(self.params).get(name, default)

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


def _random_episodes(rng: random.Random, duration_s: float, drone_ids: list[str]) -> list[Episode]:
    episodes: list[Episode] = []
    t = rng.uniform(1.0, 3.0)
    while t < duration_s - 4.0:
        kind = rng.choices(KINDS, weights=_WEIGHTS)[0]
        target = "all" if kind == "partition_all" else rng.choice(drone_ids)
        direction = rng.choice((BOTH, UP, DOWN))
        hold = rng.random() < 0.5
        if kind == "latency":
            params = (("latency_s", rng.uniform(0.02, 0.4)), ("jitter_s", rng.uniform(0.0, 0.2)))
            length = rng.uniform(1.0, 6.0)
        elif kind == "loss":
            params, length = (("loss", rng.uniform(0.05, 0.5)),), rng.uniform(1.0, 5.0)
        elif kind == "reorder":
            params, length = (("reorder", rng.uniform(0.1, 0.5)), ("reorder_delay_s", 0.3)), rng.uniform(1.0, 5.0)
        elif kind in ("partition", "partition_all"):
            params, length = (("hold", hold),), rng.choice((rng.uniform(0.2, 1.5), rng.uniform(2.0, 6.0)))
        elif kind == "sever":
            params, length = (), 0.0
        elif kind == "flap":
            params = (("down_s", rng.uniform(0.2, 1.5)), ("up_s", rng.uniform(0.2, 1.5)), ("hold", hold),
                      ("sever", rng.random() < 0.3))
            length = rng.uniform(3.0, 9.0)
        elif kind == "busy":
            params, length = (), rng.uniform(1.0, 4.0)
        else:  # emergency, quiet
            params, length = (), (EMERGENCY_WAIT_S if kind == "emergency" else rng.uniform(1.0, 4.0))
        episodes.append(Episode(round(t, 3), kind, target, direction, round(length, 3), params))
        t += rng.uniform(0.2, 4.0)
    return episodes


def guaranteed_episodes(rng: random.Random, duration_s: float, drone_ids: list[str], *, limit_s: float,
                        slack_s: float, lane_drone: str) -> list[Episode]:
    """The four episodes every plan contains, at fixed fractions of *duration_s*."""
    down_s = round(limit_s + slack_s + 2.0, 3)  # outlasts the watchdog and the slack, with a second to spare
    return [
        Episode(round(BUSY_AT * duration_s, 3), "busy", lane_drone, BOTH, BUSY_S),
        Episode(round(DOWN_AT * duration_s, 3), "partition", rng.choice(drone_ids), rng.choice((DOWN, BOTH)), down_s,
                (("hold", rng.random() < 0.5),)),
        Episode(round(SEVER_AT * duration_s, 3), "sever", rng.choice(drone_ids), BOTH, 0.0),
        Episode(round(EMERGENCY_AT * duration_s, 3), "emergency", "all", BOTH, EMERGENCY_WAIT_S),
    ]


def plan_episodes(rng: random.Random, duration_s: float, drone_ids: list[str], *, limit_s: float = 3.0,
                  slack_s: float = 1.0, lane_drone: str | None = None) -> list[Episode]:
    """A reproducible timeline of fault episodes covering *duration_s* seconds.

    Always contains one emergency, one sever, one ``busy`` episode on
    *lane_drone* (default: the last drone) and one down or two-way partition
    of ``limit_s + slack_s + 2`` seconds, placed at fixed fractions of the run
    with the random episodes around them cleared away (see the module notes).
    """
    if duration_s < MIN_SOAK_S:
        raise ValueError(f"a chaos run needs at least {MIN_SOAK_S:.0f} s to hold its guaranteed episodes, got {duration_s}")
    lane_drone = lane_drone or drone_ids[-1]
    fixed = guaranteed_episodes(rng, duration_s, drone_ids, limit_s=limit_s, slack_s=slack_s, lane_drone=lane_drone)
    windows = []
    for ep in fixed:
        before, after = _CLEARANCE[ep.kind]
        windows.append((ep.start_s - before, ep.end_s + after))
    random_part = [
        ep for ep in _random_episodes(rng, duration_s, drone_ids)
        if not any(ep.start_s < w_end and ep.end_s > w_start for w_start, w_end in windows)
    ]
    return sorted(fixed + random_part, key=lambda ep: ep.start_s)


def check_flight_log(calls: list[tuple]) -> list[str]:
    """Violations in one drone's command log: motion after a landing, or two flights at once.

    *calls* is ``FakeDrone.calls``. After a ``land``, no ``move``, ``rotate`` or
    ``rc_control`` may follow until a ``takeoff``; and a ``takeoff`` may not
    follow another ``takeoff`` without a ``land`` between them.
    """
    problems: list[str] = []
    airborne = False
    landed_at: int | None = None
    for index, call in enumerate(calls):
        name = call[0]
        if name == "takeoff":
            if airborne:
                problems.append(f"call {index}: takeoff while a flight was already in progress")
            airborne, landed_at = True, None
        elif name == "land":
            airborne, landed_at = False, index
        elif name in MOTION and landed_at is not None:
            problems.append(f"call {index}: {call} after the landing at call {landed_at}")
    return problems


@dataclass
class ChaosReport:
    violations: list[str] = field(default_factory=list)
    watchdog_landings: int = 0
    emergencies_checked: int = 0
    emergencies_skipped: int = 0
    skipped_because: list[str] = field(default_factory=list)
    emergency_landings: int = 0
    takeoff_orders: int = 0
    #: Drones that a watchdog landed and that were flying again before the run ended.
    reflights: int = 0
    lane_plans_sent: int = 0
    lane_starts_sent: int = 0
    lane_moves: int = 0
    moves_during_busy: int = 0
    episodes_by_kind: dict[str, int] = field(default_factory=dict)
    max_age_while_flying_s: float = 0.0
    max_landing_overrun_s: float = 0.0
    samples: int = 0


class ChaosRun:
    """Plays a plan against a started harness; every edge is attached to ground node ``ground``."""

    def __init__(self, harness: TwoNodeHarness, plan: list[Episode], duration_s: float, *,
                 ground: str = "a", slack_s: float = 1.0, sample_s: float = 0.05, lane_drone: str | None = None):
        self.h = harness
        self.plan = plan
        self.duration_s = duration_s
        self.ground = ground
        self.slack_s = slack_s
        self.sample_s = sample_s
        #: The edge that flies lanes (the others just take off and hover).
        self.lane_drone = lane_drone or list(harness.edges)[-1]
        self._lanes: list[dict] = []
        self.report = ChaosReport()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._reverts: list[tuple[float, Any]] = []

    # --- running ---

    def run(self) -> ChaosReport:
        self.h.grounds[self.ground].sync_clock(self.lane_drone)  # start_at needs the edge to know the clock offset
        self.h.takeoff_all()
        threads = [threading.Thread(target=self._sample, name="chaos-sampler", daemon=True),
                   threading.Thread(target=self._commander, name="chaos-commander", daemon=True)]
        for t in threads:
            t.start()
        started = time.monotonic()
        try:
            self._play(started)
        finally:
            self._stop.set()
            for t in threads + self._workers:
                t.join(timeout=10)
            self.h.sim.clear_all()
        self._check_logs()
        return self.report

    def _check_logs(self) -> None:
        for drone_id, edge in self.h.edges.items():
            for problem in check_flight_log(list(edge.drone.calls)):
                self._violation(f"{drone_id}: {problem}")
        self.report.lane_moves = sum(1 for call in list(self.h.edges[self.lane_drone].drone.calls) if call[0] == "move")

    def _violation(self, text: str) -> None:
        with self._lock:
            self.report.violations.append(text)

    def _play(self, started: float) -> None:
        queue = sorted(self.plan, key=lambda ep: ep.start_s)
        while (queue or self._reverts) and time.monotonic() - started < self.duration_s:
            now = time.monotonic() - started
            due = [r for r in self._reverts if r[0] <= now]
            for r in due:
                self._reverts.remove(r)
                self._safely(r[1])
            while queue and queue[0].start_s <= now:
                self._apply(queue.pop(0), now)
            time.sleep(0.02)
        while time.monotonic() - started < self.duration_s:
            time.sleep(0.05)
        for _, undo in self._reverts:
            self._safely(undo)

    def _safely(self, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a fault injector failing is a harness bug, not a fleet result
            self._violation(f"chaos injector raised {exc!r}")

    # --- applying one episode ---

    def _links(self, ep: Episode):
        names = list(self.h.edges) if ep.target == "all" else [ep.target]
        return [self.h.link(name, self.ground) for name in names]

    def _apply(self, ep: Episode, now: float) -> None:
        by_kind = self.report.episodes_by_kind
        by_kind[ep.kind] = by_kind.get(ep.kind, 0) + 1
        end = now + ep.duration_s
        links = self._links(ep) if ep.kind not in ("busy", "emergency", "quiet") else []
        if ep.kind == "latency":
            for link in links:
                link.set_faults(ep.direction, latency_s=ep.get("latency_s"), jitter_s=ep.get("jitter_s"))
            self._revert(end, lambda: [l.set_faults(ep.direction, latency_s=0.0, jitter_s=0.0) for l in links])
        elif ep.kind == "loss":
            for link in links:
                link.set_faults(ep.direction, loss=ep.get("loss"))
            self._revert(end, lambda: [l.set_faults(ep.direction, loss=0.0) for l in links])
        elif ep.kind == "reorder":
            for link in links:
                link.set_faults(ep.direction, reorder=ep.get("reorder"), reorder_delay_s=ep.get("reorder_delay_s"))
            self._revert(end, lambda: [l.set_faults(ep.direction, reorder=0.0) for l in links])
        elif ep.kind in ("partition", "partition_all"):
            for link in links:
                link.partition(ep.direction, hold=ep.get("hold"))
            self._revert(end, lambda: [l.heal(ep.direction) for l in links])
        elif ep.kind == "sever":
            for link in links:
                link.sever()
        elif ep.kind == "flap":
            for link in links:
                link.stop_flap()  # a link flaps once at a time
                link.flap(ep.get("down_s"), ep.get("up_s"), direction=ep.direction, hold=ep.get("hold"),
                          sever=ep.get("sever"))
            self._revert(end, lambda: [l.stop_flap() for l in links])
        elif ep.kind == "busy":
            drone = self.h.edges[ep.target].drone if ep.target in self.h.edges else None
            if drone is not None:
                first = len(drone.calls)
                drone.set_busy(True)

                def clear_busy() -> None:
                    drone.set_busy(False)
                    # Moves the executor sent into the busy channel: the only command the fault touches.
                    self.report.moves_during_busy += sum(1 for c in list(drone.calls)[first:] if c[0] == "move")
                self._revert(end, clear_busy)
        elif ep.kind == "emergency":
            worker = threading.Thread(target=self._check_emergency, args=(ep.duration_s,), name="chaos-emergency",
                                      daemon=True)
            self._workers.append(worker)
            worker.start()

    def _revert(self, at: float, undo) -> None:
        self._reverts.append((at, undo))

    # --- invariants ---

    def _sample(self) -> None:
        """Flying with an expired watchdog for longer than ``slack_s`` is a failure."""
        was_flying = {d: False for d in self.h.edges}
        landed_by_watchdog = {d: False for d in self.h.edges}
        expired_since: dict[str, float | None] = {d: None for d in self.h.edges}
        while not self._stop.is_set():
            now = time.monotonic()
            self.report.samples += 1
            for drone_id, edge in self.h.edges.items():
                flying = edge.is_flying
                age = edge.link_age_s
                limit = edge.link_loss_land_s
                if flying:
                    self.report.max_age_while_flying_s = max(self.report.max_age_while_flying_s, age)
                    if age > limit:
                        expired_since[drone_id] = expired_since[drone_id] or now
                        overrun = now - expired_since[drone_id]
                        self.report.max_landing_overrun_s = max(self.report.max_landing_overrun_s, overrun)
                        if overrun > self.slack_s:
                            self._violation(
                                f"{drone_id} still flying {overrun:.2f}s after its watchdog expired "
                                f"(link age {age:.2f}s, limit {limit:.2f}s)")
                            expired_since[drone_id] = None  # report each stuck episode once per slack
                    else:
                        expired_since[drone_id] = None
                else:
                    expired_since[drone_id] = None
                    if was_flying[drone_id] and age >= limit:
                        self.report.watchdog_landings += 1
                        landed_by_watchdog[drone_id] = True
                if flying and landed_by_watchdog[drone_id]:
                    landed_by_watchdog[drone_id] = False
                    self.report.reflights += 1
                was_flying[drone_id] = flying
            self._stop.wait(self.sample_s)

    def _commander(self) -> None:
        """Keep sending healthy, idle drones up so each outage meets a flying drone."""
        node = self.h.grounds[self.ground]
        while not self._stop.is_set():
            for drone_id, edge in self.h.edges.items():
                if drone_id == self.lane_drone:
                    self._lane_tick(node, drone_id, edge)
                    continue
                if not edge.is_flying and edge.link_age_s < edge.link_loss_land_s / 2:
                    try:
                        if node.takeoff(drone_id):
                            self.report.takeoff_orders += 1
                    except Exception as exc:  # noqa: BLE001 - any failure to order a takeoff is a finding
                        self._violation(f"ordering a takeoff of {drone_id} raised {exc!r}")
            self._stop.wait(0.4)

    def _lane_tick(self, node, drone_id: str, edge) -> None:
        """Fly lanes: a fresh plan after any landing, the same plan extended while the drone is idle in the air.

        Extending a plan the drone has fully flown carries its progress over,
        so it continues with the new lanes instead of starting again.
        """
        if edge.link_age_s >= edge.link_loss_land_s / 2:
            return
        try:
            if not edge.is_flying:
                self._lanes = list(LANE_SQUARE)
            elif edge.flight.is_busy:
                return
            else:
                self._lanes = self._lanes + list(LANE_SQUARE)
            plan = fp.make_assign_lanes(drone_id, edge.envelope.alt_slot_cm, self._lanes, LANE_ZONE)
            if node.send(drone_id, plan):
                self.report.lane_plans_sent += 1
            if node.send(drone_id, fp.make_start_at(time.monotonic() + 1.0)):
                self.report.lane_starts_sent += 1
        except Exception as exc:  # noqa: BLE001 - any failure to order a flight is a finding
            self._violation(f"ordering lanes for {drone_id} raised {exc!r}")

    def _check_emergency(self, wait_s: float, attempts: int = 4) -> None:
        """Send ``emergency_land_all`` and check every edge it could reach; repeat until one edge was checked.

        An edge is only judged when its link stayed healthy from before the call
        until the deadline. A connection can end for reasons of its own, for
        instance when the library's 20 s keepalive times out after a partition
        dropped a ping, so one pass might leave no edge to judge. Repeating
        the emergency is what an operator would do, and it keeps the check from
        depending on that timing.
        """
        try:
            for _ in range(attempts):
                if self._stop.is_set() or self._emergency_pass(wait_s):
                    return
        except Exception as exc:  # noqa: BLE001 - reported as a finding
            self._violation(f"emergency check raised {exc!r}")

    def _emergency_pass(self, wait_s: float) -> int:
        """One emergency; returns how many edges were judged."""
        node = self.h.grounds[self.ground]
        links = {d: self.h.link(d, self.ground) for d in self.h.edges}
        reachable = []
        for d, link in links.items():
            if d not in node.drone_ids:
                why = "not registered on the ground"
            elif link.open_connections < 1:
                why = "no open connection"
            elif not link.is_clean(DOWN):
                why = "downlink not clean"
            else:
                reachable.append(d)
                continue
            self.report.skipped_because.append(f"{d}: {why}")
        marks = {d: (links[d].fault_version(DOWN), links[d].accepted) for d in reachable}
        before = {d: len(self.h.edges[d].emergencies) for d in reachable}
        clients_before = node.client_count()
        sent = node.emergency_land_all()
        clients_after = node.client_count()
        if clients_before == clients_after and sent != clients_before:
            self._violation(f"emergency_land_all reached {sent} of {clients_before} sockets")
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.05)
        now = {d: (links[d].fault_version(DOWN), links[d].accepted) for d in reachable}
        stable = [d for d in reachable if marks[d] == now[d]]
        self.report.emergencies_skipped += len(links) - len(stable)
        self.report.skipped_because += [
            f"{d}: link (fault version, accepted) went {marks[d]} -> {now[d]} during the wait"
            for d in reachable if d not in stable
        ]
        for drone_id in stable:
            edge = self.h.edges[drone_id]
            self.report.emergencies_checked += 1
            if len(edge.emergencies) <= before[drone_id]:
                self._violation(f"emergency_land_all never reached {drone_id} over a healthy link")
            elif not edge.landed_after_emergency(before[drone_id]):
                self._violation(f"{drone_id} received emergency_land_all but was never told to land")
            else:
                self.report.emergency_landings += 1
        return len(stable)
