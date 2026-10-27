"""Tests for flightrisk.drone.coordinator.FleetCoordinator.

Every edge is an in-process fake link driven by a fake ground clock. Each
fake edge runs its own clock thousands of seconds away from the ground clock,
so a sign error in the clock offset shows up as a gap of hours, not
milliseconds. No network, drone or real time is involved.
"""

import threading
import time

import pytest

from flightrisk.drone.coordinator import (
    TELLO_MAX_MOVE_CM,
    TELLO_MIN_MOVE_CM,
    FleetCoordinator,
    _transit,
)
from flightrisk.drone.search import generate_parallel_track, split_parallel_track_zones

DIRS = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}
TELEMETRY_STALE_S = 2.5


class FakeClock:
    """Manually advanced monotonic ground clock."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeEdge:
    """In-process EdgeLink whose edge clock is ``ground + true_offset_s``.

    Replies are delivered synchronously from inside ``send``, which is how a
    loopback link behaves and the hardest case for lock handling.
    """

    def __init__(self, drone_id, clock, true_offset_s, battery=80, delays=None,
                 auto_ack=True, start_late_s=0.0, answer_pings=True):
        self.drone_id = drone_id
        self.clock = clock
        self.true_offset_s = true_offset_s
        self.battery = battery
        # One (uplink_s, downlink_s) pair per ping round; the last repeats.
        self.delays = list(delays or [(0.002, 0.002)])
        self.auto_ack = auto_ack
        self.start_late_s = start_late_s
        self.answer_pings = answer_pings
        self.clock_offset_s = None
        self.on_message = None
        self.sent = []
        self._ping_round = 0

    def edge_now(self):
        return self.clock.now + self.true_offset_s

    def of_type(self, msg_type):
        return [m for m in self.sent if m["type"] == msg_type]

    def reply(self, msg):
        self.on_message(msg)

    def send(self, msg):
        self.sent.append(msg)
        kind = msg["type"]
        if kind == "ping" and self.answer_pings:
            up, down = self.delays[min(self._ping_round, len(self.delays) - 1)]
            self._ping_round += 1
            self.clock.advance(up)
            t2 = self.edge_now()
            self.clock.advance(0.0005)
            t3 = self.edge_now()
            self.clock.advance(down)
            self.reply({"type": "pong", "t1": msg["t1"], "t2": t2, "t3": t3})
        elif kind == "clock":
            self.clock_offset_s = msg["offset_s"]
        elif kind == "assign_lanes" and self.auto_ack:
            self.ack_lanes(len(msg["lanes"]))
        elif kind == "start_at":
            # The edge fires when its own clock reaches t_ground_s + offset_s,
            # then reports the execution time converted back to ground time.
            t_edge_exec = msg["t_ground_s"] + self.clock_offset_s + self.start_late_s
            self.reply({"type": "ack", "for": "start_at", "drone_id": self.drone_id,
                        "executed_at_ground_s": t_edge_exec - self.clock_offset_s})

    def ack_lanes(self, lanes_total, battery=None):
        self.reply({"type": "ack", "for": "assign_lanes", "drone_id": self.drone_id,
                    "battery": self.battery if battery is None else battery,
                    "lanes_total": lanes_total})

    def telemetry(self, **fields):
        msg = {"type": "telemetry", "drone_id": self.drone_id, "battery": self.battery,
               "height_cm": 100, "is_flying": False, "rel_x_cm": 0, "rel_y_cm": 0,
               "heading_deg": 0, "lane_index": 0, "lanes_total": 0, "link_age_s": 0.1}
        msg.update(fields)
        self.reply(msg)


class RaisingLink:
    """A link whose every send raises ``exc``."""

    def __init__(self, exc):
        self.exc = exc
        self.on_message = None
        self.attempts = 0

    def send(self, msg):
        self.attempts += 1
        raise self.exc


def make_fleet(n=2, offsets=None, area=(400, 400), strip=50, **edge_kwargs):
    clock = FakeClock()
    offsets = offsets or [4000.0, -4000.0, 1234.5, -0.25][:n]
    edges = {
        f"drone-{i + 1}": FakeEdge(f"drone-{i + 1}", clock, offsets[i], **edge_kwargs)
        for i in range(n)
    }
    events = []
    coord = FleetCoordinator(edges, area_cm=area, strip_cm=strip, clock=clock,
                             on_event=lambda name, payload: events.append((name, payload)))
    return coord, edges, clock, events


def walk(origin, lanes):
    """Every point visited when flying ``lanes`` from ``origin``."""
    x, y = origin
    points = [(x, y)]
    for lane in lanes:
        dx, dy = DIRS[lane["direction"]]
        x += dx * lane["distance_cm"]
        y += dy * lane["distance_cm"]
        points.append((x, y))
    return points


def as_lanes(waypoints):
    return [{"direction": w.direction, "distance_cm": w.distance_cm} for w in waypoints]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_plan_two_zones_two_altitudes():
    coord, edges, _, _ = make_fleet(n=2, area=(400, 400), strip=50)
    plan = coord.plan()

    one, two = plan.zones
    assert (one.drone_id, one.x_cm, one.y_cm, one.w_cm, one.h_cm, one.alt_cm) == ("drone-1", 0, 0, 150, 400, 100)
    assert (two.drone_id, two.x_cm, two.y_cm, two.w_cm, two.h_cm, two.alt_cm) == ("drone-2", 250, 0, 150, 400, 160)
    assert two.x_cm - (one.x_cm + one.w_cm) == 100  # the gap between zones

    # Lanes are split_parallel_track_zones output without the shared-launch
    # sideways offset, because each drone takes off at its own zone origin.
    zones = split_parallel_track_zones(2, width_cm=300, depth_cm=400, strip_width_cm=50)
    assert list(one.lanes) == as_lanes(zones[0])
    assert list(two.lanes) == as_lanes(zones[1][-len(zones[0]):])
    assert list(one.lanes) == as_lanes(generate_parallel_track(150, 400, 50))

    for zone in plan.zones:
        for x, y in walk((zone.x_cm, zone.y_cm), zone.lanes):
            assert zone.x_cm <= x <= zone.x_cm + zone.w_cm
            assert zone.y_cm <= y <= zone.y_cm + zone.h_cm

    for drone_id, edge in edges.items():
        (msg,) = edge.of_type("assign_lanes")
        zone = plan.zone_for(drone_id)
        assert set(msg) == {"type", "drone_id", "alt_cm", "lanes", "zone"}
        assert msg["drone_id"] == drone_id
        assert msg["alt_cm"] == zone.alt_cm
        assert msg["lanes"] == list(zone.lanes)
        assert msg["zone"] == {"x_cm": zone.x_cm, "y_cm": 0, "w_cm": 150, "h_cm": 400}

    payload = plan.to_dict()
    assert payload["grid"] == {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
    assert [z["alt_cm"] for z in payload["zones"]] == [100, 160]


def test_plan_refused_while_a_drone_is_flying():
    coord, edges, clock, _ = make_fleet(n=2)
    coord.plan()
    edges["drone-1"].telemetry(is_flying=True)
    with pytest.raises(RuntimeError, match="drone-1"):
        coord.plan()
    clock.advance(5.0)  # The flying report is now stale.
    coord.plan()


# ---------------------------------------------------------------------------
# Clock sync
# ---------------------------------------------------------------------------


def test_sync_median_offset():
    # Six symmetric rounds and two badly asymmetric ones; the median ignores
    # the outliers.
    delays = [(0.002, 0.002)] * 3 + [(0.050, 0.001)] + [(0.002, 0.002)] * 3 + [(0.001, 0.080)]
    coord, edges, _, _ = make_fleet(n=2, offsets=[4000.0, -4000.0], delays=delays)
    results = coord.sync()

    for drone_id, true_offset in (("drone-1", 4000.0), ("drone-2", -4000.0)):
        edge = edges[drone_id]
        result = results[drone_id]
        assert len(edge.of_type("ping")) == 8
        assert result.samples == 8
        assert result.synced
        assert result.offset_s == pytest.approx(true_offset, abs=1e-6)
        assert result.rtt_s == pytest.approx(0.004, abs=1e-6)
        # The offset is sent unchanged: edge clock minus ground clock.
        (clock_msg,) = edge.of_type("clock")
        assert clock_msg == {"type": "clock", "offset_s": result.offset_s}
        assert coord.clock_sync(drone_id).offset_s == result.offset_s

    status = coord.status()
    assert status["drone-1"]["offset_ms"] == pytest.approx(4_000_000.0, abs=1e-3)
    assert status["drone-2"]["rtt_ms"] == pytest.approx(4.0, abs=1e-3)


def test_sync_without_pongs_leaves_link_unsynced():
    coord, edges, _, _ = make_fleet(n=2)
    edges["drone-2"].answer_pings = False
    results = coord.sync(timeout_s=0.01)
    assert results["drone-1"].synced
    assert not results["drone-2"].synced
    assert results["drone-2"].samples == 0
    assert edges["drone-2"].of_type("clock") == []


def test_sync_ignores_pong_for_an_old_round():
    coord, edges, _, _ = make_fleet(n=1)
    edge = edges["drone-1"]
    coord.sync()
    before = coord.clock_sync("drone-1")
    edge.reply({"type": "pong", "t1": 1.0, "t2": 2.0, "t3": 3.0})
    assert coord.clock_sync("drone-1") == before


class ThreadedEdge:
    """EdgeLink that answers pings from another thread, as a socket would."""

    def __init__(self, true_offset_s, answer=True):
        self.true_offset_s = true_offset_s
        self.answer = answer
        self.on_message = None
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        if msg["type"] == "ping" and self.answer:
            def reply():
                t2 = time.monotonic() + self.true_offset_s
                self.on_message({"type": "pong", "t1": msg["t1"], "t2": t2, "t3": t2})
            threading.Timer(0.001, reply).start()


def test_sync_with_replies_from_another_thread():
    edge = ThreadedEdge(true_offset_s=-4000.0)
    coord = FleetCoordinator({"drone-1": edge}, area_cm=400, strip_cm=50)
    result = coord.sync(timeout_s=5.0)["drone-1"]
    assert result.samples == 8
    # Real scheduling jitter applies, so the margin is generous.
    assert result.offset_s == pytest.approx(-4000.0, abs=0.5)
    assert edge.sent[-1] == {"type": "clock", "offset_s": result.offset_s}


def test_land_all_does_not_wait_for_a_blocked_sync():
    silent = ThreadedEdge(0.0, answer=False)
    other = ThreadedEdge(0.0, answer=False)
    coord = FleetCoordinator({"drone-1": silent, "drone-2": other}, area_cm=400, strip_cm=50)
    syncing = threading.Thread(target=coord.sync, kwargs={"timeout_s": 0.25}, daemon=True)
    syncing.start()
    coord.land_all()
    # sync() needs at least 4 s (2 links x 8 rounds x 0.25 s); land_all has
    # already returned, so it did not wait behind it.
    assert syncing.is_alive()
    assert {"type": "emergency_land_all"} in silent.sent
    assert {"type": "emergency_land_all"} in other.sent
    syncing.join(timeout=30)


# ---------------------------------------------------------------------------
# Scheduled start
# ---------------------------------------------------------------------------


def test_start_refused_without_acks_or_low_battery():
    coord, edges, clock, _ = make_fleet(n=2)

    result = coord.start()
    assert not result.ok and "fleet" in result.refusals  # no plan yet

    edges["drone-2"].auto_ack = False
    coord.sync()
    coord.plan()
    result = coord.start()
    assert not result.ok
    assert result.refusals == {"drone-2": "no assign_lanes ack"}

    # A stale ack (wrong lane count) does not count.
    lanes_total = len(coord.lanes("drone-2"))
    edges["drone-2"].ack_lanes(lanes_total + 3)
    assert coord.start().refusals == {"drone-2": "no assign_lanes ack"}

    # An ack claiming another drone's id is ignored.
    edges["drone-2"].reply({"type": "ack", "for": "assign_lanes", "drone_id": "drone-1",
                            "battery": 90, "lanes_total": lanes_total})
    assert coord.start().refusals == {"drone-2": "no assign_lanes ack"}

    edges["drone-2"].ack_lanes(lanes_total, battery=25)
    result = coord.start()
    assert not result.ok
    assert "drone-2" in result.refusals and "battery" in result.refusals["drone-2"]

    # Fresh telemetry below the minimum also refuses, even after a good ack.
    edges["drone-2"].ack_lanes(lanes_total, battery=90)
    edges["drone-1"].telemetry(battery=12)
    result = coord.start()
    assert set(result.refusals) == {"drone-1"}

    # Unsynced links are refused too.
    edges["drone-1"].answer_pings = False
    coord.sync(timeout_s=0.01)
    clock.advance(TELEMETRY_STALE_S)
    assert coord.start().refusals == {"drone-1": "clock not synced"}

    for edge in edges.values():
        assert edge.of_type("start_at") == []


def test_fresh_telemetry_battery_can_satisfy_the_gate():
    coord, edges, clock, _ = make_fleet(n=1)
    coord.sync()
    coord.plan()
    edge = edges["drone-1"]
    edge.ack_lanes(len(coord.lanes("drone-1")), battery=20)
    assert "battery" in coord.start().refusals["drone-1"]

    edge.telemetry(battery=85)
    clock.advance(TELEMETRY_STALE_S)
    assert "battery" in coord.start().refusals["drone-1"]  # stale telemetry is ignored

    edge.telemetry(battery=85)
    clock.advance(1.5)
    assert coord.start().ok


def test_start_sends_single_start_at():
    coord, edges, clock, _ = make_fleet(n=2, offsets=[4000.0, -4000.0])
    edges["drone-1"].start_late_s = 0.010
    edges["drone-2"].start_late_s = 0.040
    coord.sync()
    coord.plan()

    now = clock.now
    result = coord.start(lead_s=2.0)
    assert result.ok and result.refusals == {}
    assert result.t_ground_s == pytest.approx(now + 2.0)

    for edge in edges.values():
        (msg,) = edge.of_type("start_at")
        assert msg == {"type": "start_at", "t_ground_s": result.t_ground_s}
        # The edge fires when its clock reads t_ground_s + offset_s. On the
        # true ground clock that instant must be the scheduled time; a sign
        # error would put it 8000 s away.
        fire_on_ground = msg["t_ground_s"] + edge.clock_offset_s - edge.true_offset_s
        assert fire_on_ground == pytest.approx(result.t_ground_s, abs=0.005)

    second = coord.start()
    assert not second.ok and second.refusals == {"fleet": "already started"}
    for edge in edges.values():
        assert len(edge.of_type("start_at")) == 1

    # Skew comes straight from executed_at_ground_s in the acks.
    assert coord.skew_ms() == pytest.approx(30.0, abs=1e-3)
    status = coord.status()
    assert status["drone-1"]["skew_ms"] == pytest.approx(10.0, abs=1e-3)
    assert status["drone-2"]["skew_ms"] == pytest.approx(40.0, abs=1e-3)


def test_start_aborts_with_land_all_when_a_start_at_send_fails():
    coord, edges, _, _ = make_fleet(n=2)
    coord.sync()
    coord.plan()
    broken = edges["drone-1"]
    original_send = broken.send

    def send(msg):
        if msg["type"] == "start_at":
            raise ConnectionError("link down")
        original_send(msg)

    broken.send = send
    result = coord.start()
    assert not result.ok and set(result.refusals) == {"drone-1"}
    # No other drone is given a takeoff time once one send has failed.
    assert edges["drone-2"].of_type("start_at") == []
    for edge in edges.values():
        assert len(edge.of_type("emergency_land_all")) == 1


# ---------------------------------------------------------------------------
# Detections
# ---------------------------------------------------------------------------


def test_dedup_merges_nearby_detections():
    coord, edges, _, events = make_fleet(n=2)
    coord.plan()
    events.clear()

    # drone-1 takes off at (0, 0); drone-2 at (250, 0) on the shared grid.
    first, is_new = coord.on_detection("drone-1", {"rel_x_cm": 300, "rel_y_cm": 200, "thumbnail": "abc"})
    assert is_new and (first.x_cm, first.y_cm) == (300, 200)

    # drone-2 sees the same person: (250 + 60, 210) is 14 cm away.
    merged, is_new = coord.on_detection("drone-2", {"rel_x_cm": 60, "rel_y_cm": 210})
    assert not is_new
    assert merged.id == first.id
    assert merged.drone_ids == ("drone-1", "drone-2")
    assert merged.count == 2
    assert (merged.x_cm, merged.y_cm) == (300, 200)

    # A detection 100 cm away (beyond the 80 cm radius) is a new pin.
    far, is_new = coord.on_detection("drone-2", {"rel_x_cm": 50, "rel_y_cm": 300})
    assert is_new and far.id != first.id

    # Detections also arrive through the link callback.
    edges["drone-1"].reply({"type": "detections", "drone_id": "drone-1", "rel_x_cm": 310, "rel_y_cm": 190})
    assert len(coord.detections()) == 2
    assert coord.detections()[0].count == 3

    assert coord.on_detection("drone-1", {"rel_x_cm": None}) is None

    pins = [payload for name, payload in events if name == "fleet_detection"]
    assert [p["id"] for p in pins] == [first.id, far.id]
    assert pins[0]["rel_x_cm"] == 300 and pins[0]["thumbnail"] == "abc"


# ---------------------------------------------------------------------------
# Loss recovery
# ---------------------------------------------------------------------------


def test_retask_on_loss_redistributes_lanes():
    coord, edges, _, events = make_fleet(n=2, area=(400, 400), strip=50)
    coord.sync()
    plan = coord.plan()
    assert coord.start().ok
    one, two = plan.zones
    original_one = list(one.lanes)
    original_two = list(two.lanes)
    assert len(original_two) == 5

    edges["drone-1"].telemetry(lane_index=1, is_flying=True)
    edges["drone-2"].telemetry(lane_index=2, is_flying=True)
    appended = coord.on_drone_lost("drone-2")
    assert set(appended) == {"drone-1"}

    # drone-1 receives its own lanes unchanged, then a transit, then
    # drone-2's lanes from index 2 on.
    msg = edges["drone-1"].of_type("assign_lanes")[-1]
    lanes = msg["lanes"]
    assert lanes[:len(original_one)] == original_one
    unflown = original_two[2:]
    assert lanes[-len(unflown):] == unflown
    assert msg["alt_cm"] == 100 and msg["zone"] == one.zone_dict()

    # The transit ends exactly where drone-2's lane 2 started, so the
    # re-tasked lanes retrace drone-2's planned path on the shared grid.
    path_one = walk((one.x_cm, one.y_cm), lanes)
    path_two = walk((two.x_cm, two.y_cm), original_two)
    assert path_one[-len(unflown) - 1:] == path_two[2:]
    for lane in lanes:
        assert TELLO_MIN_MOVE_CM <= lane["distance_cm"] <= TELLO_MAX_MOVE_CM

    assert edges["drone-2"].of_type("land") == [{"type": "land"}]
    assert coord.lanes("drone-2") == original_two[:2]
    assert coord.status()["drone-2"]["lost"] is True
    kinds = [p["event"] for name, p in events if name == "fleet_event"]
    assert kinds[-2:] == ["drone_lost", "retask"]

    # A second loss report for the same drone changes nothing.
    sent_before = len(edges["drone-1"].sent)
    assert coord.on_drone_lost("drone-2") == {}
    assert len(edges["drone-1"].sent) == sent_before


@pytest.mark.parametrize("start,end", [((0, 0), (10, -7)), ((50, 400), (250, 0)), ((0, 0), (1210, 0)), ((5, 5), (5, 5))])
def test_transit_moves_are_tello_valid_and_exact(start, end):
    moves = _transit(start, end)
    assert walk(start, moves)[-1] == end
    for lane in moves:
        assert TELLO_MIN_MOVE_CM <= lane["distance_cm"] <= TELLO_MAX_MOVE_CM
    if start[0] != end[0] and start[1] != end[1]:
        # Forward or back first, so the drone leaves its own zone sideways last.
        assert moves[0]["direction"] in ("forward", "back")


# ---------------------------------------------------------------------------
# LAND ALL
# ---------------------------------------------------------------------------


def test_land_all_reaches_every_link():
    clock = FakeClock()
    first = FakeEdge("drone-1", clock, 4000.0)
    broken = RaisingLink(ConnectionError("socket closed"))
    last = FakeEdge("drone-3", clock, -4000.0)
    events = []
    coord = FleetCoordinator({"drone-1": first, "drone-2": broken, "drone-3": last},
                             area_cm=(700, 400), strip_cm=50, clock=clock,
                             on_event=lambda name, payload: events.append((name, payload)))
    coord.sync()
    coord.plan()

    results = coord.land_all()
    assert first.of_type("emergency_land_all") == [{"type": "emergency_land_all"}]
    assert last.of_type("emergency_land_all") == [{"type": "emergency_land_all"}]
    assert results["drone-1"] is None and results["drone-3"] is None
    assert isinstance(results["drone-2"], ConnectionError)
    assert broken.attempts >= 1
    assert ("fleet_event", {"event": "land_all", "detail": "failed: drone-2"}) in events

    # No start after LAND ALL until the fleet is planned again.
    assert coord.start().refusals == {"fleet": "land_all issued; plan again before starting"}


def test_land_all_finishes_before_reraising_interrupt():
    clock = FakeClock()
    interrupted = RaisingLink(KeyboardInterrupt())
    others = [FakeEdge(f"drone-{i}", clock, 0.0) for i in (2, 3)]
    coord = FleetCoordinator({"drone-1": interrupted, "drone-2": others[0], "drone-3": others[1]},
                             area_cm=(700, 400), strip_cm=50, clock=clock)
    with pytest.raises(KeyboardInterrupt):
        coord.land_all()
    for edge in others:
        assert edge.of_type("emergency_land_all") == [{"type": "emergency_land_all"}]


# ---------------------------------------------------------------------------
# N drones
# ---------------------------------------------------------------------------


def test_generic_for_n_equals_4():
    offsets = [4000.0, -4000.0, 1234.5, -0.25]
    coord, edges, _, _ = make_fleet(n=4, offsets=offsets, area=(1000, 400), strip=50)

    results = coord.sync()
    for i, drone_id in enumerate(edges):
        assert results[drone_id].offset_s == pytest.approx(offsets[i], abs=1e-6)

    plan = coord.plan()
    assert [z.x_cm for z in plan.zones] == [0, 275, 550, 825]
    assert all(z.w_cm == 175 for z in plan.zones)
    assert [z.alt_cm for z in plan.zones] == [100, 160, 100, 160]
    for left, right in zip(plan.zones, plan.zones[1:]):
        assert right.x_cm - (left.x_cm + left.w_cm) == 100
        assert left.alt_cm != right.alt_cm

    result = coord.start()
    assert result.ok
    for edge in edges.values():
        assert len(edge.of_type("start_at")) == 1

    lost = plan.zone_for("drone-3")
    edges["drone-3"].telemetry(lane_index=1)
    appended = coord.on_drone_lost("drone-3")
    assert set(appended) == {"drone-1", "drone-2", "drone-4"}

    # drone-3 has 5 lanes and had flown 1. Its 4 unflown lanes are split into
    # contiguous chunks of 2, 1 and 1, one per remaining drone. Each chunk is
    # appended after that drone's own lanes and a transit that ends exactly
    # where the chunk started on drone-3's path.
    unflown = list(lost.lanes)[1:]
    assert len(unflown) == 4
    lost_path = walk((lost.x_cm, lost.y_cm), list(lost.lanes))
    chunks = {}
    for drone_id, added in appended.items():
        zone = plan.zone_for(drone_id)
        lanes = coord.lanes(drone_id)
        assert lanes[:len(zone.lanes)] == list(zone.lanes)
        assert lanes[len(zone.lanes):] == added
        path = walk((zone.x_cm, zone.y_cm), lanes)
        matches = [
            (first, size) for first, size in ((0, 2), (2, 1), (3, 1))
            if added[-size:] == unflown[first:first + size]
            and path[-size - 1:] == lost_path[1 + first:1 + first + size + 1]
        ]
        assert len(matches) == 1, (drone_id, added)
        chunks[matches[0]] = drone_id
        assert edges[drone_id].of_type("assign_lanes")[-1]["lanes"] == lanes
    assert set(chunks) == {(0, 2), (2, 1), (3, 1)}

    land = coord.land_all()
    assert set(land) == set(edges) and all(v is None for v in land.values())
    for edge in edges.values():
        assert len(edge.of_type("emergency_land_all")) == 1
