"""A seeded schedule of random link faults, and the monitor that checks a fleet under it.

``plan_episodes`` is a pure function of its seed: the same seed gives the same
list of ``Episode`` objects. ``ChaosRun`` plays a plan against a
``TwoNodeHarness`` in real time while a sampler thread watches the safety
invariants that hold for the fleet code on main:

* no drone stays airborne once its link-loss watchdog has been expired for
  longer than ``slack_s`` (the drone lands when the heartbeats stop, and a
  landing is never held up by a busy command channel or a stalled link);
* an ``emergency_land_all`` reaches every edge whose connection was healthy
  from before the call until the deadline, and the call itself reaches every
  socket the ground holds;
* the ground can order a takeoff again after every outage (the run keeps the
  fleet flying so that every outage is a test of the watchdog).

Errors in read loops and leaked resources are checked by the caller with
``ErrorWatch`` and ``snapshot``.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from tests.support.fleet_harness import TwoNodeHarness
from tests.support.netsim import BOTH, DOWN, UP

KINDS = ("latency", "loss", "reorder", "partition", "partition_all", "sever", "flap", "busy", "emergency", "quiet")
_WEIGHTS = (14, 10, 6, 18, 8, 7, 10, 6, 8, 5)


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


def plan_episodes(rng: random.Random, duration_s: float, drone_ids: list[str]) -> list[Episode]:
    """A reproducible timeline of fault episodes covering *duration_s* seconds."""
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
            params, length = (), (1.6 if kind == "emergency" else rng.uniform(1.0, 4.0))
        episodes.append(Episode(round(t, 3), kind, target, direction, round(length, 3), params))
        t += rng.uniform(0.2, 4.0)
    return episodes


@dataclass
class ChaosReport:
    violations: list[str] = field(default_factory=list)
    watchdog_landings: int = 0
    emergencies_checked: int = 0
    emergencies_skipped: int = 0
    takeoff_orders: int = 0
    episodes_by_kind: dict[str, int] = field(default_factory=dict)
    max_age_while_flying_s: float = 0.0
    max_landing_overrun_s: float = 0.0
    samples: int = 0


class ChaosRun:
    """Plays a plan against a started harness; every edge is attached to ground node ``ground``."""

    def __init__(self, harness: TwoNodeHarness, plan: list[Episode], duration_s: float, *,
                 ground: str = "a", slack_s: float = 1.0, sample_s: float = 0.05):
        self.h = harness
        self.plan = plan
        self.duration_s = duration_s
        self.ground = ground
        self.slack_s = slack_s
        self.sample_s = sample_s
        self.report = ChaosReport()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._reverts: list[tuple[float, Any]] = []

    # --- running ---

    def run(self) -> ChaosReport:
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
        return self.report

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
                drone.set_busy(True)
                self._revert(end, lambda: drone.set_busy(False))
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
                was_flying[drone_id] = flying
            self._stop.wait(self.sample_s)

    def _commander(self) -> None:
        """Keep sending healthy, idle drones up so each outage meets a flying drone."""
        node = self.h.grounds[self.ground]
        while not self._stop.is_set():
            for drone_id, edge in self.h.edges.items():
                if not edge.is_flying and edge.link_age_s < edge.link_loss_land_s / 2:
                    try:
                        if node.takeoff(drone_id):
                            self.report.takeoff_orders += 1
                    except Exception as exc:  # noqa: BLE001 - any failure to order a takeoff is a finding
                        self._violation(f"ordering a takeoff of {drone_id} raised {exc!r}")
            self._stop.wait(0.4)

    def _check_emergency(self, wait_s: float) -> None:
        node = self.h.grounds[self.ground]
        links = {d: self.h.link(d, self.ground) for d in self.h.edges}
        try:
            reachable = [
                d for d, link in links.items()
                if d in node.drone_ids and link.open_connections >= 1 and link.is_clean(DOWN)
            ]
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
            stable = [d for d in reachable if marks[d] == (links[d].fault_version(DOWN), links[d].accepted)]
            self.report.emergencies_skipped += len(links) - len(stable)
            for drone_id in stable:
                self.report.emergencies_checked += 1
                if len(self.h.edges[drone_id].emergencies) <= before[drone_id]:
                    self._violation(f"emergency_land_all never reached {drone_id} over a healthy link")
        except Exception as exc:  # noqa: BLE001 - reported as a finding
            self._violation(f"emergency check raised {exc!r}")
