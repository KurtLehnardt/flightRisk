"""Tests for re-task refusals (R-106).

A lane assignment an edge refuses must never be reported as covered. The
edge answers a refusal with a ``nack``, the coordinator plans re-tasked
lanes only onto drones whose geofence contains them, re-offers refused
lanes to another eligible drone, reports lanes no drone can take in a
``retask_failed`` event, and lists only accepted lanes in ``retask``.

Some links here run a real ``LaneExecutor`` and ``SafetyEnvelope`` over an
in-process fake Tello and answer synchronously from inside ``send``, so the
coordinator's geofence model is checked against the edge's own. No network,
drone or file is involved.
"""

import re
from pathlib import Path

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.coordinator import FleetCoordinator
from flightrisk.drone.lane_executor import CONTROL_TYPES, LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.observability import StructuredLogger

DIRS = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}
DASHBOARD = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
INDEX_HTML = DASHBOARD / "templates" / "index.html"
UI_JS = DASHBOARD / "static" / "ui" / "ui.js"

# The fleet t20 flew: four zones side by side across a 1200 x 400 cm area,
# 75 cm lanes, and the edges' default 500 cm geofence. Zones start at x = 0,
# 325, 650 and 975, so only drone-3 can reach drone-4's zone.
T20_AREA = (1200, 400)
T20_STRIP = 75
FENCE_CM = 500


def walk(origin, lanes):
    x, y = origin
    points = [(x, y)]
    for lane in lanes:
        dx, dy = DIRS[lane["direction"]]
        x += dx * lane["distance_cm"]
        y += dy * lane["distance_cm"]
        points.append((x, y))
    return points


class FakeEdge:
    """In-process link that acks every ``assign_lanes``, or nacks it with ``refuse``.

    ``silent`` drones answer nothing, like an edge whose answer was lost.
    """

    def __init__(self, drone_id, refuse=None, silent=False, battery=80):
        self.drone_id = drone_id
        self.refuse = refuse
        self.silent = silent
        self.battery = battery
        self.on_message = None
        self.sent = []

    def of_type(self, msg_type):
        return [m for m in self.sent if m["type"] == msg_type]

    def send(self, msg):
        fp.validate_message(msg)
        self.sent.append(msg)
        if msg["type"] == "ping":
            # Same clock on both sides: the sync measures a zero offset.
            self.reply({"type": "pong", "t1": msg["t1"], "t2": msg["t1"], "t3": msg["t1"]})
        elif msg["type"] == "assign_lanes" and not self.silent:
            if self.refuse:
                self.reply(fp.make_nack_assign_lanes(self.drone_id, self.refuse, len(msg["lanes"])))
            else:
                self.ack(len(msg["lanes"]))

    def ack(self, lanes_total):
        self.reply(fp.make_ack_assign_lanes(self.drone_id, self.battery, lanes_total))

    def reply(self, msg):
        self.on_message(msg)


class ExecutorEdge:
    """In-process link backed by a real ``LaneExecutor`` and ``SafetyEnvelope``.

    Messages pass the protocol validation the transports apply, and the
    executor's answers are delivered synchronously from inside ``send``.
    """

    def __init__(self, drone_id, geofence_cm=FENCE_CM):
        self.drone_id = drone_id
        self.on_message = None
        self.sent = []
        self.replies = []
        self.drone = FakeTelloController(name=drone_id, battery=90)
        self.drone.connect()
        self.executor = LaneExecutor(
            self.drone,
            SafetyEnvelope(geofence_cm=geofence_cm),
            drone_id,
            send=self._reply,
            logger=StructuredLogger(component="test-retask-refusal"),
        )

    def _reply(self, msg):
        fp.validate_message(msg)
        self.replies.append(msg)
        if self.on_message is not None:
            self.on_message(msg)

    def of_type(self, msg_type):
        return [m for m in self.sent if m["type"] == msg_type]

    def replies_of_type(self, msg_type):
        return [m for m in self.replies if m["type"] == msg_type]

    def send(self, msg):
        fp.validate_message(msg)
        self.sent.append(msg)
        if msg["type"] in CONTROL_TYPES:
            self.executor.handle_message(msg)


@pytest.fixture
def executor_fleet():
    """Factory for a coordinator over ``ExecutorEdge`` links; shuts the executors down."""
    created = []

    def _make(n=4, area=T20_AREA, strip=T20_STRIP, geofence_cm=FENCE_CM, edge_fence_cm=FENCE_CM):
        edges = {f"drone-{i + 1}": ExecutorEdge(f"drone-{i + 1}", edge_fence_cm) for i in range(n)}
        created.extend(edges.values())
        events = []
        coord = FleetCoordinator(edges, area_cm=area, strip_cm=strip, geofence_cm=geofence_cm,
                                 on_event=lambda name, payload: events.append((name, payload)))
        return coord, edges, events

    yield _make
    for edge in created:
        edge.executor.shutdown(timeout=2.0)


def fleet_events(events, kind):
    return [payload for name, payload in events if name == "fleet_event" and payload["event"] == kind]


def make_fleet(edges, area=(1200, 400), strip=T20_STRIP, geofence_cm=None):
    events = []
    coord = FleetCoordinator(edges, area_cm=area, strip_cm=strip, geofence_cm=geofence_cm, retask_wait_s=0.05,
                             on_event=lambda name, payload: events.append((name, payload)))
    return coord, events


# ---------------------------------------------------------------------------
# Edge side
# ---------------------------------------------------------------------------


def test_edge_sends_nack_on_geofence_refusal():
    edge = ExecutorEdge("drone-1", geofence_cm=FENCE_CM)
    try:
        far = [{"direction": "forward", "distance_cm": 400}, {"direction": "right", "distance_cm": 300},
               {"direction": "right", "distance_cm": 300}]
        assert edge.executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": far}) is False

        nack = edge.replies[-1]
        # A geofence nack also says where the path leaves the fence and where
        # it starts, relative to the takeoff point (NACK_GEOFENCE_FIELDS).
        assert nack == {"type": "nack", "for": "assign_lanes", "drone_id": "drone-1",
                        "reason": "geofence", "lanes_total": 3, "geofence_cm": 500.0,
                        "x_cm": 600.0, "y_cm": 400.0, "rel_x_cm": 0.0, "rel_y_cm": 0.0}
        assert fp.validate_message(nack) == fp.NACK
        assert edge.replies_of_type("ack") == []
        # The refused plan was not stored.
        assert edge.executor.lanes_total == 0

        # Every other refusal is answered with a nack naming its reason.
        refusals = [
            ({"drone_id": "drone-2", "lanes": far}, "other_drone", 3),
            ({"drone_id": "drone-1", "lanes": [{"direction": "up", "distance_cm": 100}]}, "invalid", 1),
            ({"drone_id": "drone-1", "lanes": "forward"}, "invalid", 0),
            ({"drone_id": "drone-1", "lanes": far[:1], "alt_cm": 2000}, "altitude", 1),
        ]
        for fields, reason, lanes_total in refusals:
            assert edge.executor.handle_assign_lanes({"type": "assign_lanes", **fields}) is False
            assert edge.replies[-1] == {"type": "nack", "for": "assign_lanes", "drone_id": "drone-1",
                                        "reason": reason, "lanes_total": lanes_total}

        # An unreadable battery cannot be acked, so the plan is refused too.
        edge.drone.state.battery = None
        assert edge.executor.handle_assign_lanes(
            {"type": "assign_lanes", "drone_id": "drone-1", "lanes": far[:1]}) is False
        assert edge.replies[-1]["reason"] == "battery"
        edge.drone.state.battery = 90

        # A plan inside the fence is acked as before.
        assert edge.executor.handle_assign_lanes(
            {"type": "assign_lanes", "drone_id": "drone-1", "lanes": far[:2]}) is True
        assert edge.replies[-1] == {"type": "ack", "for": "assign_lanes", "drone_id": "drone-1",
                                    "battery": 90, "lanes_total": 2}
        for msg in edge.replies:
            fp.validate_message(msg)
    finally:
        edge.executor.shutdown(timeout=2.0)


@pytest.mark.parametrize("msg", [
    {"type": "nack", "for": "assign_lanes", "drone_id": "d", "reason": "tired", "lanes_total": 1},
    {"type": "nack", "for": "assign_lanes", "drone_id": "d", "reason": ["geofence"], "lanes_total": 1},
    {"type": "nack", "for": "start_at", "drone_id": "d", "reason": "geofence", "lanes_total": 1},
    {"type": "nack", "for": "assign_lanes", "drone_id": "", "reason": "geofence", "lanes_total": 1},
    {"type": "nack", "for": "assign_lanes", "drone_id": "d", "reason": "geofence", "lanes_total": -1},
    {"type": "nack", "for": "assign_lanes", "drone_id": "d", "reason": "geofence"},
])
def test_malformed_nack_is_rejected(msg):
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(msg)


# ---------------------------------------------------------------------------
# Coordinator side
# ---------------------------------------------------------------------------


def test_coordinator_skips_drones_whose_geofence_excludes_lanes(executor_fleet):
    coord, edges, events = executor_fleet()
    plan = coord.plan()
    assert [z.x_cm for z in plan.zones] == [0, 325, 650, 975]
    for edge in edges.values():
        assert len(edge.replies_of_type("ack")) == 1

    lost = plan.zone_for("drone-4")
    appended = coord.on_drone_lost("drone-4")

    # drone-1 and drone-2 cannot reach drone-4's zone, so they are never asked.
    assert len(edges["drone-1"].of_type("assign_lanes")) == 1
    assert len(edges["drone-2"].of_type("assign_lanes")) == 1
    assert set(appended) == {"drone-3"}
    for edge in edges.values():
        assert edge.replies_of_type("nack") == []

    # drone-3's edge accepted all of drone-4's lanes, which it flies after
    # its own along drone-4's path.
    lanes = coord.lanes("drone-3")
    assert edges["drone-3"].replies_of_type("ack")[-1]["lanes_total"] == len(lanes)
    zone3 = plan.zone_for("drone-3")
    path = walk((zone3.x_cm, zone3.y_cm), lanes)
    assert path[-len(lost.lanes) - 1:] == walk((lost.x_cm, lost.y_cm), list(lost.lanes))
    for x, y in path:
        assert abs(x - zone3.x_cm) <= FENCE_CM and abs(y - zone3.y_cm) <= FENCE_CM

    (retask,) = fleet_events(events, "retask")
    assert retask["to"] == {"drone-3": len(appended["drone-3"])}
    assert [lane["index"] for lane in retask["lanes"]] == list(range(len(lost.lanes)))
    assert fleet_events(events, "retask_failed") == []


def test_nack_reoffers_lanes_to_other_drone(executor_fleet):
    # A ground station that does not know the edges' fence offers drone-4's
    # lanes to every survivor, as in the t20 run. drone-1 and drone-2 refuse
    # them, and the refused lanes go to drone-3 instead.
    coord, edges, events = executor_fleet(geofence_cm=None)
    plan = coord.plan()
    own = {d: list(plan.zone_for(d).lanes) for d in edges}
    lost = plan.zone_for("drone-4")

    appended = coord.on_drone_lost("drone-4")

    for drone_id in ("drone-1", "drone-2"):
        nacks = edges[drone_id].replies_of_type("nack")
        assert nacks and {n["reason"] for n in nacks} == {"geofence"}
        # A refusing drone keeps its own lanes, and its earlier ack still stands.
        assert coord.lanes(drone_id) == own[drone_id]
        assert coord.status()[drone_id]["acked"] is True
    assert set(appended) == {"drone-3"}

    (retask,) = fleet_events(events, "retask")
    assert set(retask["to"]) == {"drone-3"}
    assert sorted(lane["index"] for lane in retask["lanes"]) == list(range(len(lost.lanes)))
    assert {lane["drone_id"] for lane in retask["lanes"]} == {"drone-3"}
    assert "awaiting" not in retask
    assert fleet_events(events, "retask_failed") == []

    # Every lane of drone-4 is on drone-3's accepted path.
    lanes = coord.lanes("drone-3")
    assert edges["drone-3"].replies_of_type("ack")[-1]["lanes_total"] == len(lanes)
    zone3 = plan.zone_for("drone-3")
    path = walk((zone3.x_cm, zone3.y_cm), lanes)
    segments = set(zip(path, path[1:]))
    lost_path = walk((lost.x_cm, lost.y_cm), list(lost.lanes))
    for segment in zip(lost_path, lost_path[1:]):
        assert segment in segments


def test_nack_for_another_reason_is_reoffered():
    edges = {f"drone-{i}": FakeEdge(f"drone-{i}") for i in (1, 2, 3)}
    coord, events = make_fleet(edges, area=(900, 400))
    coord.plan()
    edges["drone-2"].refuse = "battery"
    own_two = coord.lanes("drone-2")

    appended = coord.on_drone_lost("drone-3")

    assert len(edges["drone-2"].of_type("assign_lanes")) == 2  # the plan, then one refused offer
    assert coord.lanes("drone-2") == own_two
    assert set(appended) == {"drone-1"}
    (retask,) = fleet_events(events, "retask")
    assert set(retask["to"]) == {"drone-1"}
    assert fleet_events(events, "retask_failed") == []


def test_unassignable_lanes_emit_retask_failed(executor_fleet):
    # With a 400 cm fence drone-3 reaches only the first lanes of drone-4's
    # zone, and nobody reaches the rest.
    coord, edges, events = executor_fleet(geofence_cm=400, edge_fence_cm=400)
    plan = coord.plan()
    lost = plan.zone_for("drone-4")
    zone3 = plan.zone_for("drone-3")
    lost_path = walk((lost.x_cm, lost.y_cm), list(lost.lanes))
    reachable = [
        i for i in range(len(lost.lanes))
        if all(abs(x - zone3.x_cm) <= 400 for x, _ in lost_path[i:i + 2])
    ]
    unreachable = [i for i in range(len(lost.lanes)) if i not in reachable]
    assert reachable and unreachable

    coord.on_drone_lost("drone-4")

    (failed,) = fleet_events(events, "retask_failed")
    assert failed["drone_id"] == "drone-4"
    assert [lane["index"] for lane in failed["lanes"]] == unreachable
    for lane in failed["lanes"]:
        assert lane["direction"] == lost.lanes[lane["index"]]["direction"]
        assert (lane["x_cm"], lane["y_cm"]) == lost_path[lane["index"]]
        assert (lane["end_x_cm"], lane["end_y_cm"]) == lost_path[lane["index"] + 1]
        assert "outside the geofence" in lane["reason"]
    assert "uncovered" in failed["detail"]

    (retask,) = fleet_events(events, "retask")
    assert sorted(lane["index"] for lane in retask["lanes"]) == reachable
    assert set(retask["to"]) == {"drone-3"}
    for edge in edges.values():
        assert edge.replies_of_type("nack") == []

    # The uncovered lanes stay visible in the plan a reconnecting browser gets.
    uncovered = coord.current_plan()["uncovered"]
    assert [(u["drone_id"], u["index"]) for u in uncovered] == [("drone-4", i) for i in unreachable]

    # The dashboard shows them above the search map.
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = UI_JS.read_text(encoding="utf-8")
    assert 'id="fleet-uncovered"' in html
    assert re.search(r"kind === 'retask_failed'", js)
    assert "plan.uncovered" in js


def test_lanes_with_no_drone_left_emit_retask_failed():
    edges = {"drone-1": FakeEdge("drone-1"), "drone-2": FakeEdge("drone-2", silent=True)}
    coord, events = make_fleet(edges, area=(400, 400), geofence_cm=FENCE_CM)
    coord.plan()

    coord.on_drone_lost("drone-1")  # offered to drone-2, which never answers
    assert fleet_events(events, "retask_failed") == []
    coord.on_drone_lost("drone-2")

    failed = fleet_events(events, "retask_failed")
    by_drone = {f["drone_id"]: f for f in failed}
    # drone-1's lanes, still waiting on drone-2, and drone-2's own lanes are
    # all uncovered now.
    assert set(by_drone) == {"drone-1", "drone-2"}
    assert len(by_drone["drone-1"]["lanes"]) == len(coord.plan_result.zone_for("drone-1").lanes)
    assert all(lane["reason"] == "no drone left to take them" for f in failed for lane in f["lanes"])


def test_retask_event_lists_only_acked_lanes():
    edges = {
        "drone-1": FakeEdge("drone-1", silent=True),
        "drone-2": FakeEdge("drone-2"),
        "drone-3": FakeEdge("drone-3"),
    }
    coord, events = make_fleet(edges, area=(900, 400))
    plan = coord.plan()
    edges["drone-1"].ack(len(coord.lanes("drone-1")))
    lost = plan.zone_for("drone-3")

    appended = coord.on_drone_lost("drone-3")

    # drone-1 was offered a chunk but has not answered; drone-2 accepted.
    assert len(edges["drone-1"].of_type("assign_lanes")) == 2
    assert set(appended) == {"drone-2"}
    (retask,) = fleet_events(events, "retask")
    assert retask["to"] == {"drone-2": len(appended["drone-2"])}
    accepted = {lane["index"] for lane in retask["lanes"]}
    awaiting = {lane["index"] for lane in retask["awaiting"]}
    assert {lane["drone_id"] for lane in retask["lanes"]} == {"drone-2"}
    assert {lane["drone_id"] for lane in retask["awaiting"]} == {"drone-1"}
    assert accepted and awaiting and not accepted & awaiting
    assert accepted | awaiting == set(range(len(lost.lanes)))
    assert "awaiting drone-1" in retask["detail"]
    assert "to drone-1" not in retask["detail"]

    # drone-1's late ack is reported on its own, listing just its lanes.
    edges["drone-1"].ack(len(coord.lanes("drone-1")))
    late = fleet_events(events, "retask")[-1]
    assert late is not retask
    assert set(late["to"]) == {"drone-1"}
    assert {lane["index"] for lane in late["lanes"]} == awaiting
    assert "awaiting" not in late

    # A repeated ack reports nothing new.
    edges["drone-1"].ack(len(coord.lanes("drone-1")))
    assert len(fleet_events(events, "retask")) == 2


def test_late_nack_with_no_alternative_reports_retask_failed():
    edges = {"drone-1": FakeEdge("drone-1", silent=True), "drone-2": FakeEdge("drone-2")}
    coord, events = make_fleet(edges, area=(400, 400))
    coord.plan()
    edges["drone-1"].ack(len(coord.lanes("drone-1")))
    own_one = coord.lanes("drone-1")

    coord.on_drone_lost("drone-2")
    (retask,) = fleet_events(events, "retask")
    assert retask["to"] == {} and retask["lanes"] == []
    assert "no lanes accepted yet" in retask["detail"]

    offered = len(coord.lanes("drone-1"))
    edges["drone-1"].reply(fp.make_nack_assign_lanes("drone-1", "battery", offered))

    (failed,) = fleet_events(events, "retask_failed")
    assert failed["drone_id"] == "drone-2" and failed["lanes"]
    assert all("refused by drone-1 (battery)" in lane["reason"] for lane in failed["lanes"])
    assert coord.lanes("drone-1") == own_one
    assert len(fleet_events(events, "retask")) == 1


def test_stale_nack_is_ignored_and_plan_nack_blocks_start():
    edges = {"drone-1": FakeEdge("drone-1"), "drone-2": FakeEdge("drone-2")}
    coord, events = make_fleet(edges, area=(400, 400))
    coord.plan()
    assert all(result.synced for result in coord.sync(rounds=1).values())

    # A nack for a plan the drone was not sent changes nothing.
    edges["drone-2"].reply(fp.make_nack_assign_lanes("drone-2", "geofence", 99))
    assert coord.status()["drone-2"]["acked"] is True

    # A refused plan blocks the start with the edge's reason.
    edges["drone-2"].reply(fp.make_nack_assign_lanes("drone-2", "geofence", len(coord.lanes("drone-2"))))
    assert coord.status()["drone-2"]["acked"] is False
    result = coord.start()
    assert not result.ok
    assert result.refusals == {"drone-2": "assign_lanes refused (geofence)"}
    assert all(edge.of_type("start_at") == [] for edge in edges.values())


# ---------------------------------------------------------------------------
# Transient refusals (landing, busy) are retried, not permanent
# ---------------------------------------------------------------------------


class TransientEdge(FakeEdge):
    """Nacks every ``assign_lanes`` with *reason* until ``refuse`` is cleared."""

    def send(self, msg):
        if msg["type"] == "assign_lanes" and self.refuse:
            self.sent.append(msg)
            self.reply({"type": "nack", "for": "assign_lanes", "drone_id": self.drone_id,
                        "reason": self.refuse, "lanes_total": len(msg["lanes"])})
            return
        super().send(msg)


def _telemetry(drone_id, state, is_flying):
    return {"type": "telemetry", "drone_id": drone_id, "state": state, "is_flying": is_flying}


@pytest.mark.parametrize("reason, still, over", [
    ("landing", ("landing", True), ("idle", False)),
    ("busy", ("taking_off", True), ("hovering", True)),
])
def test_a_transient_nack_is_retried_once_the_drone_is_ready(reason, still, over):
    edges = {f"drone-{i}": TransientEdge(f"drone-{i}") for i in (1, 2, 3)}
    coord, events = make_fleet(edges, area=(900, 400))
    coord.plan()
    for drone_id in ("drone-1", "drone-2"):
        edges[drone_id].refuse = reason
    own_one = coord.lanes("drone-1")

    appended = coord.on_drone_lost("drone-3")

    # Nobody could take the lanes yet, but they are not reported as uncovered.
    assert appended == {}
    assert fleet_events(events, "retask_failed") == []
    offers = {d: len(e.of_type("assign_lanes")) for d, e in edges.items()}

    # Telemetry that still shows the refusal reason re-offers nothing.
    coord.handle_message("drone-1", _telemetry("drone-1", *still))
    assert {d: len(e.of_type("assign_lanes")) for d, e in edges.items()} == offers

    # Once the drone is ready the lanes are offered again and accepted.
    for drone_id in ("drone-1", "drone-2"):
        edges[drone_id].refuse = None
    coord.handle_message("drone-1", _telemetry("drone-1", *over))
    assert len(edges["drone-1"].of_type("assign_lanes")) == offers["drone-1"] + 1
    assert len(coord.lanes("drone-1")) > len(own_one)
    assert fleet_events(events, "retask_failed") == []
    assert any(set(p["to"]) == {"drone-1"} and "awaiting" not in p for p in fleet_events(events, "retask"))


def test_a_permanent_nack_is_still_never_reoffered_to_the_same_drone():
    edges = {f"drone-{i}": TransientEdge(f"drone-{i}") for i in (1, 2, 3)}
    coord, events = make_fleet(edges, area=(900, 400))
    coord.plan()
    edges["drone-1"].refuse = "battery"
    edges["drone-2"].refuse = "battery"
    coord.on_drone_lost("drone-3")
    assert fleet_events(events, "retask_failed")
    count = len(edges["drone-1"].of_type("assign_lanes"))
    edges["drone-1"].refuse = None
    coord.handle_message("drone-1", _telemetry("drone-1", "idle", False))
    assert len(edges["drone-1"].of_type("assign_lanes")) == count
