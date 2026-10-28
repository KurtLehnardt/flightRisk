"""Pose reset at takeoff, and geofence refusals that say why.

Field report (two-laptop fake fleet, edge-1 geofence 450 cm): after several
Plan, Start, LAND ALL cycles, a drone-lost re-task of drone-2's lanes was
refused by drone-1 with ``geofence`` at x = 475 cm, although every lane fits
inside 450 cm of drone-1's takeoff point. The edge's dead-reckoned pose was
never reset between flights, so each run's net 75 cm sideways displacement
accumulated (75, 150, 225, ...) and the re-task path was checked from a pose
hundreds of centimetres away from where the drone really took off.

These tests run the real ``LaneExecutor`` and ``SafetyEnvelope`` on fake
Tellos behind a real ``FleetCoordinator`` and check that:

* every takeoff from the ground makes the drone's spot the origin again, so
  repeated runs do not accumulate and the re-task is accepted;
* a plan checked on the ground is checked from the next takeoff point, with
  the geofence unchanged (a path that leaves it is still refused);
* a geofence nack carries where the path leaves the fence and where it
  starts, and the coordinator shows that to the operator.
"""
from __future__ import annotations

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.coordinator import FleetCoordinator
from flightrisk.drone.lane_executor import CONTROL_TYPES, LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from tests.lane_executor_support import (
    QuietLogger,
    SteppedTello,
    assign,
    make_v2_executor,
    of_type,
    validate_sent,
    wait_for,
)

# The field test: a 400 x 400 cm area split into two 150 cm zones with a
# 100 cm gap, 75 cm strips, so each zone is forward 400, right 75, back 400.
AREA = (400, 400)
STRIP = 75


class Edge:
    """In-process link backed by a real ``LaneExecutor`` on a ``SteppedTello``."""

    def __init__(self, drone_id: str, geofence_cm: float):
        self.drone_id = drone_id
        self.on_message = None
        self.replies: list[dict] = []
        self.drone = SteppedTello(auto=True, name=drone_id, battery=90)
        self.drone.connect()
        self.log = QuietLogger()
        self.envelope = SafetyEnvelope(geofence_cm=geofence_cm)
        self.executor = LaneExecutor(self.drone, self.envelope, drone_id, send=self._reply, logger=self.log)

    def _reply(self, msg: dict) -> None:
        fp.validate_message(msg)
        self.replies.append(msg)
        if self.on_message is not None:
            self.on_message(msg)

    def send(self, msg: dict) -> None:
        fp.validate_message(msg)
        if msg["type"] in CONTROL_TYPES:
            self.executor.handle_message(msg)

    def pose(self) -> tuple[float, float]:
        pose = self.envelope.pose_snapshot()
        return round(pose.x_cm, 1), round(pose.y_cm, 1)

    def nacks(self) -> list[dict]:
        return [m for m in self.replies if m["type"] == "nack"]


@pytest.fixture
def make_fleet():
    """Factory for two edges behind a coordinator that plans without a fence (the field ground had none).

    By default drone-1's fence is 450 cm (FLIGHTRISK_GEOFENCE_CM=450 on
    edge-1) and drone-2's is the 500 cm default.
    """
    made = []

    def _make(area=AREA, fences=(450, 500)):
        edges = {"drone-1": Edge("drone-1", fences[0]), "drone-2": Edge("drone-2", fences[1])}
        events: list[tuple[str, dict]] = []
        coord = FleetCoordinator(edges, area_cm=area, strip_cm=STRIP, geofence_cm=None, retask_wait_s=1.0,
                                 on_event=lambda name, payload: events.append((name, payload)))
        made.append((coord, edges))
        return coord, edges, events

    yield _make
    for coord, edges in made:
        coord.land_all()
        for edge in edges.values():
            edge.executor.shutdown(timeout=2.0)


@pytest.fixture
def field_fleet(make_fleet):
    return make_fleet()


def fleet_events(events, kind):
    return [payload for name, payload in events if name == "fleet_event" and payload["event"] == kind]


def start_run(coord, edges):
    coord.plan()
    for edge in edges.values():
        assert of_type(edge.replies, "ack", "assign_lanes"), edge.nacks()
    coord.sync(rounds=1)
    result = coord.start(lead_s=0.05)
    assert result.ok, result.refusals


def finish_lanes(edges):
    for edge in edges.values():
        assert wait_for(lambda e=edge: e.executor.lane_index == 3 and not e.executor.is_busy), edge.drone_id


def land_all(coord, edges):
    coord.land_all()
    for edge in edges.values():
        assert wait_for(lambda e=edge: not e.drone.state.is_flying), edge.drone_id


def test_repeated_runs_do_not_accumulate_and_the_retask_is_accepted(field_fleet):
    """The field sequence: three Plan, Start, LAND ALL runs, then drone-2 is lost mid-run.

    On main the pose read (75, 0), (150, 0), (225, 0) after the runs and
    drone-1 refused the re-task with ``geofence`` at (475, 0).
    """
    coord, edges, events = field_fleet
    one = edges["drone-1"]
    poses = []
    for _ in range(3):
        start_run(coord, edges)
        finish_lanes(edges)
        poses.append(one.pose())
        land_all(coord, edges)
    # Every run ends 75 cm right of the spot it took off from, never further.
    assert poses == [(75.0, 0.0)] * 3

    # Fourth run: drone-1 is held on its first lane (as in the field log,
    # where the re-task arrived 4.4 s into the flight) when drone-2 is lost.
    one.drone.auto = False
    start_run(coord, edges)
    assert wait_for(lambda: one.drone.waiting)
    one.drone.release(1)  # the climb to the altitude slot
    assert wait_for(lambda: one.drone.waiting and one.drone.move_log[-1]["direction"] == "forward")
    # The takeoff made the drone's spot the origin again.
    assert one.pose() == (0.0, 0.0)
    assert ("edge_pose_reset", 75.0, 0.0) in [
        (event, fields.get("previous_x_cm"), fields.get("previous_y_cm")) for _, event, fields in one.log.events
    ]

    coord.handle_message("drone-1", {"type": "telemetry", "drone_id": "drone-1", "lane_index": 0, "is_flying": True})
    coord.handle_message("drone-2", {"type": "telemetry", "drone_id": "drone-2", "lane_index": 0, "is_flying": True})
    appended = coord.on_drone_lost("drone-2", wait_s=1.0)

    assert one.nacks() == []
    assert fleet_events(events, "retask_failed") == []
    assert set(appended) == {"drone-1"}
    (retask,) = fleet_events(events, "retask")
    assert [lane["index"] for lane in retask["lanes"]] == [0, 1, 2]
    assert coord.current_plan()["uncovered"] == []

    # drone-1 flies its own lanes, the transit and drone-2's lanes, all
    # inside its 450 cm fence around the spot it took off from.
    one.drone.auto = True
    one.drone.release(100)
    assert wait_for(lambda: one.executor.lane_index == one.executor.lanes_total and not one.executor.is_busy)
    assert one.executor.lanes_total == 7
    assert one.pose() == (325.0, 0.0)
    assert not [e for _, e, _ in one.log.events if e in ("edge_geofence_refused", "edge_assign_lanes_refused")]


def test_ground_plan_is_checked_from_the_next_takeoff_point_and_the_fence_still_holds():
    """A drone that landed away from its takeoff point is checked from where it will take off.

    The fence is not loosened: a plan leaving 450 cm of the new takeoff point
    is refused on the ground, and checked again move by move in flight.
    """
    drone = SteppedTello(auto=True, name="drone-1", battery=90)
    drone.connect()
    sent: list[dict] = []
    envelope = SafetyEnvelope(geofence_cm=450)
    executor = LaneExecutor(drone, envelope, "drone-1", send=sent.append, logger=QuietLogger())
    try:
        executor.handle_clock({"type": "clock", "offset_s": 0.0})
        right_400 = [{"direction": "right", "distance_cm": 400}]
        assert executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": right_400})
        assert executor.handle_start_at({"type": "start_at", "t_ground_s": executor.now() + 0.05})
        assert wait_for(lambda: executor.lane_index == 1 and not executor.is_busy)
        executor.land_now("ground_land")
        assert wait_for(lambda: not drone.state.is_flying)
        # Telemetry still shows where the drone landed, relative to its takeoff.
        assert (envelope.pose_snapshot().x_cm, envelope.pose_snapshot().y_cm) == (400.0, 0.0)

        # On main this was refused (400 + 400 = 800 > 450): the stale pose
        # was used although the next flight starts from where the drone is.
        assert executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": right_400})
        assert sent[-1]["type"] == "ack"

        # A path that leaves the fence around the next takeoff point is still refused.
        too_far = [{"direction": "right", "distance_cm": 300}, {"direction": "right", "distance_cm": 200}]
        assert not executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "lanes": too_far})
        assert sent[-1] == {"type": "nack", "for": "assign_lanes", "drone_id": "drone-1", "reason": "geofence",
                            "lanes_total": 2, "geofence_cm": 450.0, "x_cm": 500.0, "y_cm": 0.0,
                            "rel_x_cm": 0.0, "rel_y_cm": 0.0}

        # The accepted plan flies from the new takeoff point.
        assert executor.handle_assign_lanes(
            {"type": "assign_lanes", "drone_id": "drone-1", "lanes": right_400 + [{"direction": "left", "distance_cm": 400}]}
        )
        assert executor.handle_start_at({"type": "start_at", "t_ground_s": executor.now() + 0.05})
        assert wait_for(lambda: executor.lane_index == 2 and not executor.is_busy)
        assert (envelope.pose_snapshot().x_cm, envelope.pose_snapshot().y_cm) == (0.0, 0.0)
        assert ("land",) not in drone.calls[drone.takeoff_marks[-1]:]
        for msg in sent:
            fp.validate_edge_to_ground(msg, proto=1)
    finally:
        executor.shutdown(timeout=2.0)


def test_a_geofence_refusal_tells_the_operator_where_and_why(make_fleet):
    """A real geofence refusal of a re-task reaches the dashboard with the edge's numbers.

    The area is 200 cm deep and drone-1's fence 200 cm, so drone-1's own
    zone fits but drone-2's zone (x 250 to 325) is genuinely out of its
    reach. The ground plans without a fence, as in the field test, so only
    the edge can refuse.
    """
    coord, edges, events = make_fleet(area=(400, 200), fences=(200, 500))
    one = edges["drone-1"]
    start_run(coord, edges)
    finish_lanes(edges)
    coord.handle_message("drone-1", {"type": "telemetry", "drone_id": "drone-1", "lane_index": 3, "is_flying": True})
    coord.on_drone_lost("drone-2", wait_s=1.0)

    (nack,) = one.nacks()
    assert nack["reason"] == "geofence"
    assert (nack["geofence_cm"], nack["x_cm"], nack["y_cm"], nack["rel_x_cm"], nack["rel_y_cm"]) == (
        200.0, 250.0, 0.0, 75.0, 0.0)
    (failed,) = fleet_events(events, "retask_failed")
    reason = ("refused by drone-1 (geofence: the path would reach (250, 0) cm from drone-1's takeoff point, "
              "outside its 200 cm geofence; drone-1 is at (75, 0) cm from its takeoff point)")
    assert [lane["reason"] for lane in failed["lanes"]] == [reason] * 3
    assert reason in failed["detail"]
    assert [u["reason"] for u in coord.current_plan()["uncovered"]] == [reason] * 3


def test_a_plan_refused_for_the_geofence_blocks_start_with_the_numbers():
    edges = {"drone-1": Edge("drone-1", 100), "drone-2": Edge("drone-2", 500)}
    coord = FleetCoordinator(edges, area_cm=AREA, strip_cm=STRIP, geofence_cm=None)
    try:
        coord.plan()
        coord.sync(rounds=1)
        result = coord.start(lead_s=0.05)
        assert not result.ok
        assert result.refusals == {
            "drone-1": "assign_lanes refused (geofence: the path would reach (0, 400) cm from drone-1's takeoff "
                       "point, outside its 100 cm geofence; drone-1 is at (0, 0) cm from its takeoff point)"
        }
    finally:
        for edge in edges.values():
            edge.executor.shutdown(timeout=2.0)


def test_v2_geofence_nack_carries_the_fields():
    executor, _drone, sent, _clock, _log = make_v2_executor(geofence_cm=150)
    try:
        far = [{"direction": "forward", "distance_cm": 200}]
        executor.handle_message(assign(lanes=far, cmd_id="c-1"))
        (nack,) = of_type(sent, "nack")
        assert nack["reason"] == "geofence" and nack["cmd_id"] == "c-1"
        assert {k: nack[k] for k in fp.NACK_GEOFENCE_FIELDS} == {
            "geofence_cm": 150.0, "x_cm": 0.0, "y_cm": 200.0, "rel_x_cm": 0.0, "rel_y_cm": 0.0}
        validate_sent(sent)
    finally:
        executor.shutdown(timeout=2.0)


@pytest.mark.parametrize("proto", [1, 2])
def test_geofence_nack_fields_are_validated(proto):
    base = {"type": "nack", "for": "assign_lanes", "drone_id": "d", "reason": "geofence", "lanes_total": 1}
    if proto == 2:
        base["cmd_id"] = "c-1"
    ok = dict(base, geofence_cm=450, x_cm=475.0, y_cm=0, rel_x_cm=300.0, rel_y_cm=0.0)
    assert fp.validate_edge_to_ground(ok, proto=proto) == fp.NACK
    for key in fp.NACK_GEOFENCE_FIELDS:
        for bad in ("far", None, True, float("nan")):
            with pytest.raises(fp.ProtocolError):
                fp.validate_edge_to_ground(dict(base, **{key: bad}), proto=proto)


def test_geofence_nack_builders():
    fields = {"geofence_cm": 450.0, "x_cm": 475.0, "y_cm": 0.0, "rel_x_cm": 300.0, "rel_y_cm": 0.0}
    v1 = fp.make_nack_assign_lanes("drone-1", "geofence", 7, geofence=fields)
    assert v1 == {"type": "nack", "for": "assign_lanes", "drone_id": "drone-1", "reason": "geofence",
                  "lanes_total": 7, **fields}
    v2 = fp.make_nack("assign_lanes", "drone-1", "geofence", "c-9", lanes_total=7, geofence=fields)
    assert {k: v2[k] for k in fields} == fields
    # Without the fields the builders produce exactly what they did before.
    assert fp.make_nack_assign_lanes("drone-1", "geofence", 7) == {
        "type": "nack", "for": "assign_lanes", "drone_id": "drone-1", "reason": "geofence", "lanes_total": 7}
    with pytest.raises(fp.ProtocolError):
        fp.make_nack_assign_lanes("drone-1", "geofence", 7, geofence={"face": 1.0})


def test_reset_pose_returns_the_previous_pose():
    envelope = SafetyEnvelope(geofence_cm=450)
    envelope.record_move("forward", 400)
    envelope.record_move("right", 75)
    previous = envelope.reset_pose()
    assert (previous.x_cm, previous.y_cm) == (75.0, 400.0)
    pose = envelope.pose_snapshot()
    assert (pose.x_cm, pose.y_cm, pose.heading_deg) == (0.0, 0.0, 0.0)
    # The fence itself is unchanged: 450 cm around the (new) takeoff point.
    assert envelope.geofence_cm == 450.0
    assert envelope.check_move("right", 450)
    assert not envelope.check_move("right", 452)
