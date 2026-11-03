"""The ground and replay fixes of the review of PR 177: B3, B4, N6 to N10, N12 and the sensor-down nit.

* B4: a huge ``obstacle_seq`` never stalls the coordinator; a suspect jump
  uses only the listed cuts and repeats none.
* B3: the shared replay reports what a lane never reaches (probe 3); at a
  continuing re-task the edge ends its deficit and the ground plans the
  transit, separation and fence from where the receiver actually is (probes
  1 and 2, on fake edges and on a real ``LaneExecutor``); a re-flight is
  ``reflown`` only where the receiver's replay shows its legs flown.
* N6, N7, N8, N9, N10, N12 and the sensor-down nit, each on the coordinator.

Fake edges with an injected clock, and a real ``LaneExecutor`` on a fake
Tello with a scripted sensor; no drone, network or real time is involved
except the one bound on how long a telemetry message may take (B4).
"""
from __future__ import annotations

import logging
import threading
import time

from flightrisk.drone import coordinator as coordinator_module
from flightrisk.drone.coordinator import MAX_OBSTACLE_SEQ, FleetCoordinator, ZoneAssignment
from flightrisk.drone.lane_steps import RetaskLane, replay_lanes
from flightrisk.drone.obstacle_sensor import (
    CUT_OBSTACLE,
    CUT_SENSOR_DOWN,
    SEGMENT_SKIPPED,
    SENSOR_UNAVAILABLE_DETAIL,
    BlockedZone,
    FakeObstacleSensor,
)
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from tests.lane_executor_support import FakeClock as ExecutorClock
from tests.lane_executor_support import QuietLogger, SteppedTello, assign, reply_for, send, start_at, wait_for
from tests.test_coordinator import FakeEdge
from tests.test_coordinator_obstacle import (
    cut_fleet,
    cut_telemetry,
    lane_blocked,
    lanes_of,
    obstacle_entries,
    ready_fleet,
    seg_tuple,
)
from tests.test_coordinator_reflight import (
    ALT,
    DEMO,
    LANES,
    ORIGINS,
    REFLIGHT,
    TRANSIT,
    ExecutorLink,
    Fleet,
    SharedClock,
    ends,
    released,
)

LOGGER = coordinator_module.logger.name

#: Probe 3 of the review: one plan whose last lane is shorter than its first.
PROBE_3 = lanes_of(("forward", 400), ("right", 100), ("back", 300), ("right", 100), ("forward", 300))


def statuses(fleet, drone_id="drone-1"):
    return {(s["index"], s.get("status"), s["reason"]) for s in fleet.segments(drone_id)}


# ---------------------------------------------------------------------------
# B4: a huge obstacle_seq
# ---------------------------------------------------------------------------


def test_b4_an_obstacle_seq_of_1e12_returns_within_100_ms_and_leaves_the_lock_free(caplog):
    coord, edges, _, events = cut_fleet(n=1)
    edge = edges["drone-1"]
    cut_telemetry(edge, 1, [(0, 250)])
    elapsed: list[float] = []

    def deliver():
        start = time.perf_counter()
        edge.telemetry(is_flying=True, obstacle_seq=1e12, obstacle_cuts=[0, 250, 2, 100],
                       obstacle_cuts_total=2, obstacle_cut_reasons="obstacle,obstacle")
        elapsed.append(time.perf_counter() - start)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        thread = threading.Thread(target=deliver, daemon=True)
        thread.start()
        thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert elapsed and elapsed[0] < 0.1
    assert coord._lock.acquire(timeout=0.1)
    coord._lock.release()
    # Ignored outright: nothing new is recorded or announced.
    assert any("above" in r.getMessage() and str(MAX_OBSTACLE_SEQ) in r.getMessage() for r in caplog.records)
    assert len(lane_blocked(events)) == 1
    assert coord._drones["drone-1"].obstacle_seq == 1
    # The next sane count carries on as before.
    cut_telemetry(edge, 2, [(0, 250), (2, 100)])
    assert [e["lane_index"] for e in lane_blocked(events)] == [0, 2]


def test_b4_the_cap_is_accepted_and_one_above_it_is_ignored():
    coord, edges, _, events = cut_fleet(n=1)
    edge = edges["drone-1"]
    edge.telemetry(is_flying=True, obstacle_seq=MAX_OBSTACLE_SEQ + 1, obstacle_cuts=[0, 250],
                   obstacle_cuts_total=1, obstacle_cut_reasons="obstacle")
    assert lane_blocked(events) == [] and coord._drones["drone-1"].obstacle_seq == 0
    edge.telemetry(is_flying=True, obstacle_seq=MAX_OBSTACLE_SEQ, obstacle_cuts=[0, 250],
                   obstacle_cuts_total=1, obstacle_cut_reasons="obstacle")
    assert [e["lane_index"] for e in lane_blocked(events)] == [0]


def test_b4_a_suspect_jump_uses_only_the_listed_cuts_and_neither_repeats_nor_renames_one(caplog):
    fleet = Fleet()
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    start = time.perf_counter()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        fleet.tel("drone-1", lane_index=3, obstacle_seq=10**9, obstacle_cuts=[0, 250, 2, 100],
                  obstacle_cuts_total=2, obstacle_cut_reasons="obstacle,obstacle")
    assert time.perf_counter() - start < 0.1
    assert any("jumped from 1 to 1000000000" in r.getMessage() for r in caplog.records)
    assert not any("no telemetry listed" in r.getMessage() for r in caplog.records)
    # Lane 0's cut keeps its number 1 and is not announced again; lane 2's is cut 2.
    blocked = [e for name, e in fleet.events if name == "fleet_event" and e.get("event") == "lane_blocked"]
    assert [e["lane_index"] for e in blocked] == [0, 2]
    with fleet.coord._lock:
        assert [(c.cut_seq, c.first_index) for c in fleet.coord._reflights] == [(1, 0), (2, 2)]
    # The counts that follow number on from there.
    fleet.tel("drone-1", lane_index=4, obstacle_seq=10**9 + 1, obstacle_cuts=[0, 250, 2, 100, 3, 50],
              obstacle_cuts_total=3, obstacle_cut_reasons="obstacle,obstacle,obstacle")
    with fleet.coord._lock:
        assert sorted(fleet.coord._drones["drone-1"].obstacle_cuts) == [1, 2, 3]


# ---------------------------------------------------------------------------
# B3 a: honest coverage within one plan (probe 3)
# ---------------------------------------------------------------------------


def fly_real(lanes, blocked):
    """Fly *lanes* on a real executor into *blocked*; return its telemetry, its segments and its pose."""
    clock = ExecutorClock()
    drone = SteppedTello(auto=True, name="drone-1", battery=90)
    drone.connect()
    envelope = SafetyEnvelope(alt_slot_cm=100, geofence_cm=500, clock=clock)
    sensor = FakeObstacleSensor(envelope.pose_snapshot, blocked)
    sent: list[dict] = []
    executor = LaneExecutor(drone, envelope, "drone-1", send=sent.append, clock=clock, logger=QuietLogger(),
                            fleet_v2=True, obstacle_sensor=sensor, obstacle_retry_pause_s=0.0)
    try:
        executor.handle_clock({"type": "clock", "offset_s": 0.0})
        send(executor, assign(lanes=lanes, plan_id="p-1", plan_seq=1, cmd_id="a-1"))
        assert reply_for(sent, "a-1")["type"] == "ack"
        t_ground = clock() + 0.5
        send(executor, start_at(t_ground, plan_id="p-1", cmd_id="s-1"))
        clock.set(t_ground)
        assert wait_for(lambda: not executor.is_busy and executor.lane_index >= executor.lanes_total)
        return executor.telemetry(), executor.obstacle_segments(), envelope.pose_snapshot()
    finally:
        executor.shutdown(timeout=2.0)


def test_b3_probe_3_the_last_lane_ending_short_is_reported_on_edge_and_ground_alike():
    telemetry, edge_segments, pose = fly_real(PROBE_3, [BlockedZone(heading_deg=0.0, y_min=50, x_max=50)])
    assert telemetry["obstacle_cuts"] == [0, 50]
    # The drone ends 50 cm short of the plan's end, (200, 400).
    assert (round(pose.x_cm), round(pose.y_cm)) == (200, 350)
    coord, edges, _, _ = cut_fleet(n=1, lanes=PROBE_3)
    edges["drone-1"].reply(telemetry)
    entries = obstacle_entries(coord)
    assert [seg_tuple(e) for e in entries] == [
        (s.lane_index, s.x_cm, s.y_cm, s.end_x_cm, s.end_y_cm, s.reason) for s in edge_segments
    ]
    assert [seg_tuple(e) for e in entries] == [
        (0, 0, 50, 0, 400, CUT_OBSTACLE),
        (2, 100, 100, 100, 400, SEGMENT_SKIPPED),
        (4, 200, 350, 200, 400, SEGMENT_SKIPPED),
    ]
    assert coord.status()["drone-1"]["uncovered_cm"] == 350 + 300 + 50


# ---------------------------------------------------------------------------
# B3 b and c: continuing re-tasks plan from where the receiver is
# ---------------------------------------------------------------------------

#: drone-2's transit after its demo plan was cut on its last lane at 100 cm:
#: from (550, 100) on the shared grid to drone-1's segment end, (0, 400).
PROBE_1_TRANSIT = [
    {"direction": "forward", "distance_cm": 300},
    {"direction": "left", "distance_cm": 275},
    {"direction": "left", "distance_cm": 275},
]


def test_b3_probe_1_the_ground_plans_the_re_flight_from_where_drone_2_stopped():
    fleet = Fleet(lanes=DEMO)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1", rel_x_cm=200, rel_y_cm=400, is_flying=False)
    # drone-2 cut its last lane 100 cm in and hovers there, done.
    fleet.cut("drone-2", [(4, 100)], CUT_OBSTACLE, lane_index=5, rel_x_cm=200, rel_y_cm=100)
    offer = fleet.assigns("drone-2")[-1]
    assert offer["lanes"] == list(DEMO) + PROBE_1_TRANSIT + REFLIGHT
    with fleet.coord._lock:
        assert fleet.coord._drones["drone-2"].retask_starts == [len(DEMO)]
        path = fleet.coord._actual_path_locked("drone-2")
    # The path checked for the fence and the separation is the one flown:
    # up to the cut at (550, 100), forward to (550, 400), across at y 400.
    assert path[len(DEMO):] == [(550, 100), (550, 400), (275, 400), (0, 400), (0, 250), (100, 250), (100, 400)]
    # Nothing of the appended lanes is listed under drone-2.
    assert [(s["index"], ends(s)) for s in fleet.segments("drone-2")] == [(4, (550, 100, 550, 400))]
    end = len(offer["lanes"])
    fleet.done("drone-2", lane_index=end, lanes_total=end, rel_x_cm=-250, rel_y_cm=400)
    (done,) = fleet.reflight_events("reflight_done")
    assert done["receiver"] == "drone-2" and done["drone_id"] == "drone-1"
    assert fleet.segments("drone-1") == []


def test_b3_probe_1_a_real_drone_2_flies_the_transit_and_legs_where_the_ground_planned_them():
    clock = SharedClock()
    edge1 = FakeEdge("drone-1", clock, 0.0)
    wall = BlockedZone(heading_deg=0.0, x_min=150, y_min=100)
    link2 = ExecutorLink("drone-2", clock, [wall])
    events: list[tuple[str, dict]] = []
    coord = FleetCoordinator({"drone-1": edge1, "drone-2": link2}, area_cm=(600, 400), strip_cm=100, clock=clock,
                             on_event=lambda name, payload: events.append((name, payload)))
    try:
        zones = [ZoneAssignment(drone_id=rid, x_cm=ORIGINS[rid], y_cm=0, w_cm=200, h_cm=400, alt_cm=ALT[rid],
                                lanes=DEMO) for rid in ("drone-1", "drone-2")]
        coord.plan_zones(zones, grid_cm=(0, 0, 550, 400), gap_cm=100, strip_cm=100)
        coord.sync(rounds=1, timeout_s=1.0)
        result = coord.start(lead_s=0.5)
        assert result.ok, result.refusals
        clock.set(result.t_ground_s)
        assert link2.finished()
        assert link2.executor.obstacle_cuts[0].lane_index == 4 and link2.executor.obstacle_cuts[0].flown_cm == 100
        # What stopped drone-2 has moved away (a person walked off).
        link2.sensor.blocked.clear()
        gates_before = len(link2.sensor.calls)
        link2.report()
        # drone-1 cut lane 0 at 250 cm and has landed.
        edge1.telemetry(is_flying=False, obstacle_sensing="stepped", obstacle_sensor_ok=True, lane_index=5,
                        lanes_total=5, rel_x_cm=200, rel_y_cm=400, obstacle_seq=1, obstacle_cuts=[0, 250],
                        obstacle_cuts_total=1, obstacle_cut_reasons=CUT_OBSTACLE)
        offer = link2.sent[-1]
        assert offer["type"] == "assign_lanes" and offer["lanes"] == list(DEMO) + PROBE_1_TRANSIT + REFLIGHT
        assert link2.finished() and link2.executor.lanes_total == len(offer["lanes"])
        # The edge keeps the first appended lane as a re-task start.
        assert isinstance(link2.executor._lanes[len(DEMO)], RetaskLane)
        pose = link2.executor.envelope.pose_snapshot()
        link2.report()
        with coord._lock:
            path = coord._actual_path_locked("drone-2")
            numbers, ground_segments = coord._obstacle_replay_locked(coord._drones["drone-2"])
        edge_segments = link2.executor.obstacle_segments()
    finally:
        link2.executor.shutdown(timeout=2.0)
    # Edge and ground agree on where drone-2 ended and on what it left.
    assert (round(pose.x_cm) + ORIGINS["drone-2"], round(pose.y_cm)) == path[-1] == (100, 400)
    assert edge_segments == ground_segments and numbers == [1]
    # The transit went forward from the cut point at x 200 (550 on the grid),
    # then the reversed leg was flown facing back along x -350 (0 on the grid).
    later = link2.sensor.calls[gates_before:]
    assert any(call.heading_deg == 0.0 and call.x_cm == 200 and call.y_cm == 100 for call in later)
    assert any(abs(call.heading_deg - 180.0) < 1.0 and call.x_cm == -350 and call.y_cm == 400 for call in later)
    kinds = [p["event"] for name, p in events if name == "fleet_event" and p["event"].startswith("reflight_")]
    assert kinds[-3:] == ["reflight_offered", "reflight_accepted", "reflight_done"]


def test_b3_probe_2_both_drones_cut_lanes_0_and_4_and_the_re_flight_starts_where_drone_2_is():
    # The Oct 28 room: both drones cut lanes 0 and 4 at 250 cm.
    fleet = Fleet(lanes=DEMO)
    both = [(0, 250), (4, 250)]
    fleet.cut("drone-1", both, "obstacle,obstacle", lane_index=5, rel_x_cm=200, rel_y_cm=250, is_flying=False)
    fleet.cut("drone-2", both, "obstacle,obstacle", lane_index=5, rel_x_cm=200, rel_y_cm=250)
    offer = fleet.assigns("drone-2")[-1]
    # From (550, 250): forward 150 and across to (0, 400), drone-1's lane 0
    # and its shadow on lane 2; then across to (200, 400) and back down lane 4.
    assert offer["lanes"][len(DEMO):] == [
        {"direction": "forward", "distance_cm": 150},
        {"direction": "left", "distance_cm": 275},
        {"direction": "left", "distance_cm": 275},
        *REFLIGHT,
        {"direction": "right", "distance_cm": 100},
        {"direction": "back", "distance_cm": 150},
    ]
    end = len(offer["lanes"])
    fleet.done("drone-2", lane_index=end, lanes_total=end)
    assert [e["cut_seq"] for e in fleet.reflight_events("reflight_done")] == [1, 2]
    assert fleet.segments("drone-1") == []
    # Under the old rule the reversed leg of lane 0 would have been flown as nothing.
    flown = replay_lanes(offer["lanes"], both)
    assert flown.targets[len(DEMO) + 3] == 0


def test_b3_a_lost_drones_lanes_go_to_a_receiver_from_where_it_stopped():
    fleet = Fleet(lanes=DEMO)
    fleet.cut("drone-2", [(4, 100)], "autopilot", lane_index=5, rel_x_cm=200, rel_y_cm=100)
    fleet.tel("drone-1", lane_index=2)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    offer = fleet.assigns("drone-2")[-1]
    # drone-1's lanes 2 to 4 start at (100, 400); drone-2 is at (550, 100).
    assert offer["lanes"][len(DEMO):] == [
        {"direction": "forward", "distance_cm": 300},
        {"direction": "left", "distance_cm": 450},
        *DEMO[2:],
    ]


def test_b3_c_a_receiver_moved_by_a_cut_the_ground_learns_late_has_not_re_flown_the_segment():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    offer = fleet.assigns("drone-2")[-1]
    assert offer["lanes"] == list(LANES) + TRANSIT + REFLIGHT
    # Only then does drone-2's telemetry list a cut 50 cm into its last lane:
    # it flies the appended lanes from (500, 0), 50 cm left of where the
    # ground planned them.
    fleet.cut("drone-2", [(3, 50)], CUT_OBSTACLE, lane_index=4)
    end = len(offer["lanes"])
    fleet.done("drone-2", lane_index=end, lanes_total=end)
    assert fleet.reflight_events("reflight_done") == []
    (failed,) = fleet.reflight_events("reflight_failed")
    assert failed["receiver"] == "drone-2"
    assert failed["detail"].endswith("drone-2 flew it off the planned path, 300 cm not searched")
    assert [ends(lane) for lane in failed["lanes"]] == [(0, 250, 0, 400), (100, 250, 100, 400)]
    assert {(s["index"], s["status"], s["receiver"]) for s in fleet.segments()} == {
        (0, "failed", "drone-2"), (2, "failed", "drone-2")}
    assert fleet.coord.status()["drone-2"]["reflights"] == {"offered": 1, "reflown": 0, "blocked_again": 0}


# ---------------------------------------------------------------------------
# N6: a receiver whose obstacle sensing cannot fly appended lanes
# ---------------------------------------------------------------------------


def test_n6_a_lost_drones_lanes_skip_a_stepped_receiver_whose_sensor_is_not_ok():
    fleet = Fleet(3, retask_separation=False, origins={"drone-3": 700})
    fleet.done("drone-2", obstacle_sensor_ok=False)
    fleet.done("drone-3")
    fleet.tel("drone-1", lane_index=1)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert len(fleet.assigns("drone-2")) == 1
    assert len(fleet.assigns("drone-3")) == 2


def test_n6_with_no_fit_receiver_the_lost_lanes_fail_with_the_sensor_reason():
    fleet = Fleet()
    fleet.done("drone-2", obstacle_sensor_ok=False)
    fleet.tel("drone-1", lane_index=1)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert len(fleet.assigns("drone-2")) == 1
    (failed,) = [p for name, p in fleet.events if name == "fleet_event" and p.get("event") == "retask_failed"]
    assert "drone-2 reports its obstacle sensor is not ok" in failed["detail"]


def test_n6_a_receiver_whose_lanes_a_sensor_down_ended_gets_no_lanes_of_either_kind():
    fleet = Fleet(retask_separation=False)
    fleet.cut("drone-2", [(2, 100)], CUT_SENSOR_DOWN, lane_index=4)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    (failed,) = fleet.reflight_events("reflight_failed")
    assert failed["detail"].endswith("drone-2 ended its lanes (obstacle sensor down)")
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert len(fleet.assigns("drone-2")) == 1


def test_n6_a_receiver_without_obstacle_fields_still_gets_a_lost_drones_lanes():
    fleet = Fleet()
    fleet.edges["drone-2"].telemetry(is_flying=True, lane_index=4, lanes_total=4)
    fleet.tel("drone-1", lane_index=1)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert len(fleet.assigns("drone-2")) == 2


# ---------------------------------------------------------------------------
# N7: a receiver taking off makes the re-flight wait
# ---------------------------------------------------------------------------


def test_n7_a_receiver_taking_off_makes_the_chunk_wait_then_takes_it():
    fleet = Fleet()
    fleet.done("drone-2", is_flying=False, state="taking_off")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    assert fleet.reflight_events() == []
    assert {(s["status"], s["detail"]) for s in fleet.segments()} == {("waiting", "drone-2 is taking off")}
    fleet.done("drone-2", state="flying_lanes")
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + REFLIGHT


def test_n7_a_receiver_on_the_ground_still_fails_the_chunk():
    fleet = Fleet()
    fleet.done("drone-2", is_flying=False, state="idle")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    (failed,) = fleet.reflight_events()
    assert failed["detail"].endswith("drone-2 is not flying")


# ---------------------------------------------------------------------------
# N8: superseded only drops shadows re-tasked whole
# ---------------------------------------------------------------------------


def test_n8_the_cuts_own_segment_is_still_offered_when_the_cutter_is_lost():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=1, rel_x_cm=50, rel_y_cm=250)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    with fleet.coord._lock:
        (chunk,) = fleet.coord._reflights
    assert [(s.lane_index, s.reason) for s in chunk.segments] == [(0, CUT_OBSTACLE)]
    assert chunk.status == "acked" and chunk.lanes == [REFLIGHT[0]]


# ---------------------------------------------------------------------------
# N9: a lost cutter is no longer a point obstacle
# ---------------------------------------------------------------------------


def test_n9_a_lost_cutters_frozen_position_no_longer_holds_its_chunk():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    # Done, but hovering 50 cm beside the segment: the chunk waits for it.
    fleet.done("drone-1", rel_x_cm=50, rel_y_cm=300)
    assert {s["status"] for s in fleet.segments()} == {"waiting"}
    with fleet.coord._lock:
        lanes = fleet.coord.lanes("drone-2") + TRANSIT + REFLIGHT
        assert fleet.coord._near_cutter_locked("drone-2", lanes, "drone-1") is not None
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    with fleet.coord._lock:
        assert fleet.coord._near_cutter_locked("drone-2", lanes, "drone-1") is None
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + REFLIGHT


# ---------------------------------------------------------------------------
# N10: the start gate
# ---------------------------------------------------------------------------


def test_n10_start_refuses_any_dead_sensor_unless_the_mode_is_off_or_autopilot():
    for sensing, refused in (("forward_only", True), ("stepped", True), ("lidar", True), (None, True),
                             ("off", False), ("autopilot", False)):
        coord, edges, _ = ready_fleet()
        edges["drone-1"].telemetry(obstacle_sensing=sensing, obstacle_sensor_ok=False)
        result = coord.start()
        assert result.ok is not refused, sensing
        if refused:
            assert set(result.refusals) == {"drone-1"}
            assert result.refusals["drone-1"].startswith(SENSOR_UNAVAILABLE_DETAIL)


# ---------------------------------------------------------------------------
# N12: an edge restart never lets a reused cut number be offered twice
# ---------------------------------------------------------------------------


def test_n12_an_edge_restart_fails_its_waiting_chunks_and_renumbers_its_cuts(caplog):
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        fleet.tel("drone-1", lane_index=2, obstacle_seq=0, obstacle_cuts=[], obstacle_cuts_total=0)
    assert any("restarted" in r.getMessage() for r in caplog.records)
    (failed,) = fleet.reflight_events("reflight_failed")
    assert failed["cut_seq"] == 1 and failed["detail"].endswith("drone-1's edge restarted, so its cuts are no longer known")
    # The restarted edge counts from 1 again; its first cut is cut 2 on the ground.
    fleet.cut("drone-1", [(2, 100)], CUT_OBSTACLE, lane_index=3)
    with fleet.coord._lock:
        assert [(c.cut_seq, c.first_index, c.status) for c in fleet.coord._reflights] == [
            (1, 0, "failed"), (2, 2, "queued")]
    assert {(s["cut_seq"], s["index"]) for s in fleet.segments()} == {(2, 2)}
    # Done, hovering clear of drone-2's way to the segment at (100, 0).
    fleet.done("drone-1", rel_x_cm=0, rel_y_cm=400)
    assert len(fleet.assigns("drone-2")) == 2
    offered = fleet.reflight_events("reflight_offered")
    assert [e["cut_seq"] for e in offered] == [2]


# ---------------------------------------------------------------------------
# Nit: a sensor_down on the appended lanes is reported as such
# ---------------------------------------------------------------------------


def test_nit_a_sensor_down_on_the_re_flight_is_reported_as_sensor_down_not_blocked_again():
    fleet = released()
    # drone-2's sensor goes down 50 cm into the reversed leg (its lane 7).
    fleet.cut("drone-2", [(7, 50)], CUT_SENSOR_DOWN, lane_index=7)
    assert fleet.reflight_events("reflight_blocked") == []
    (failed,) = fleet.reflight_events("reflight_failed")
    assert failed["receiver"] == "drone-2"
    assert failed["detail"].endswith("drone-2 ended its lanes on it (obstacle sensor down)")
    # The 50 cm it flew count; the rest of the leg and the shadow do not.
    assert [ends(lane) for lane in failed["lanes"]] == [(0, 350, 0, 250), (100, 250, 100, 400)]
    reason = "drone-2 ended its lanes on it (obstacle sensor down)"
    assert statuses(fleet) == {(0, "failed", reason), (2, "failed", reason)}
    assert fleet.segments("drone-2") == []
    assert fleet.coord.status()["drone-2"]["reflights"]["blocked_again"] == 0


# ---------------------------------------------------------------------------
# A sensing receiver takes appended lanes only once its own lanes are done
# ---------------------------------------------------------------------------

WAIT_DRONE_2 = "waiting for drone-2 to finish its own lanes"


def retask_events(fleet):
    return [p for name, p in fleet.events if name == "fleet_event" and p.get("event") == "retask"]


def test_rule_a_re_flight_waits_for_a_receiver_with_lanes_left_and_goes_out_once_it_finishes(caplog):
    fleet = Fleet()
    fleet.tel("drone-2", lane_index=2)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        fleet.done("drone-1")
        fleet.tel("drone-2", lane_index=3)
    assert len(fleet.assigns("drone-2")) == 1 and fleet.reflight_events() == []
    assert {(s["status"], s["waiting_for"], s["detail"]) for s in fleet.segments()} == {("waiting", None, WAIT_DRONE_2)}
    assert sum(WAIT_DRONE_2 in r.getMessage() for r in caplog.records) == 1  # logged once, not per message
    fleet.done("drone-2")
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + REFLIGHT
    assert [e["event"] for e in fleet.reflight_events()] == ["reflight_offered", "reflight_accepted"]


def test_rule_a_lost_drones_lanes_wait_for_a_stepped_receiver_mid_zone(caplog):
    fleet = Fleet()
    fleet.tel("drone-2", lane_index=1)
    fleet.tel("drone-1", lane_index=1)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert len(fleet.assigns("drone-2")) == 1
    (waiting,) = retask_events(fleet)
    assert waiting["to"] == {} and waiting["detail"].endswith(f"3 lanes awaiting a free drone ({WAIT_DRONE_2})")
    assert [(lane["index"], lane["detail"]) for lane in waiting["awaiting"]] == [(k, WAIT_DRONE_2) for k in (1, 2, 3)]
    assert any(r.getMessage() == f"Re-tasked lanes 1, 2, 3 of drone-1 wait: {WAIT_DRONE_2}" for r in caplog.records)
    # Its own lanes done, drone-2 takes them.
    fleet.done("drone-2")
    offer = fleet.assigns("drone-2")[-1]
    assert offer["lanes"][:len(LANES)] == list(LANES) and offer["lanes"][-3:] == list(LANES[1:])
    accepted = retask_events(fleet)[-1]
    assert accepted["to"] == {"drone-2": len(offer["lanes"]) - len(LANES)}


def test_rule_a_fleet_without_obstacle_fields_re_tasks_a_mid_zone_drone_at_once():
    fleet = Fleet()
    for rid in ("drone-1", "drone-2"):
        fleet.edges[rid].telemetry(is_flying=True, lane_index=1, lanes_total=len(LANES))
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    offer = fleet.assigns("drone-2")[-1]
    assert len(fleet.assigns("drone-2")) == 2
    # Its own lanes, then a transit from (550, 0) to drone-1's lane 1 start at (0, 400), then lanes 1 to 3.
    assert offer["lanes"] == list(LANES) + TRANSIT + list(LANES[1:])
    (event,) = retask_events(fleet)
    assert event["to"] == {"drone-2": len(TRANSIT) + 3} and "awaiting" not in event


def test_rule_a_receiver_that_cuts_its_own_last_lane_before_finishing_is_planned_from_there():
    fleet = Fleet()
    fleet.tel("drone-2", lane_index=3)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1", is_flying=False)
    assert {s["detail"] for s in fleet.segments()} == {WAIT_DRONE_2}
    # drone-2 stops 50 cm into its last lane and is done: it is at (500, 0), not (550, 0).
    fleet.cut("drone-2", [(3, 50)], CUT_OBSTACLE, lane_index=4, rel_x_cm=150, rel_y_cm=0)
    offer = fleet.assigns("drone-2")[-1]
    assert offer["lanes"] == list(LANES) + [
        {"direction": "forward", "distance_cm": 400},
        {"direction": "left", "distance_cm": 500},
    ] + REFLIGHT
    end = len(offer["lanes"])
    fleet.done("drone-2", lane_index=end, lanes_total=end)
    (done,) = fleet.reflight_events("reflight_done")
    assert done["drone_id"] == "drone-1" and done["receiver"] == "drone-2"
    assert fleet.segments("drone-1") == []



# ---------------------------------------------------------------------------
# R2-N2: a lost drone's lanes never wait for a receiver that cannot finish
# ---------------------------------------------------------------------------

LANDED_DRONE_2 = "drone-2 landed before it finished its own lanes"


def lost_entries(fleet):
    """The re-tasked lanes in ``current_plan()["uncovered"]`` (no ``kind``)."""
    return [u for u in fleet.coord.current_plan()["uncovered"] if "kind" not in u]


def failed_events(fleet):
    return [p for name, p in fleet.events if name == "fleet_event" and p.get("event") == "retask_failed"]


def test_r2n2_waiting_lanes_are_listed_in_the_plan_with_their_reason_until_they_are_offered():
    fleet = Fleet()
    fleet.tel("drone-2", lane_index=1)
    fleet.tel("drone-1", lane_index=1)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert [(u["index"], u["drone_id"], u["status"], u["reason"]) for u in lost_entries(fleet)] == [
        (k, "drone-1", "waiting", WAIT_DRONE_2) for k in (1, 2, 3)]
    # The entry keeps the shape of a failed lane: the lane's ends on the shared grid.
    assert lost_entries(fleet)[0]["x_cm"] == 0 and lost_entries(fleet)[0]["end_x_cm"] == 100
    fleet.done("drone-2")
    assert len(fleet.assigns("drone-2")) == 2
    assert lost_entries(fleet) == [] and failed_events(fleet) == []


def test_r2n2_a_receiver_that_lands_mid_zone_fails_the_waiting_lanes_with_that_reason():
    """Probe: drone-2, the only receiver, lands (battery, operator) before it finishes its own lanes."""
    fleet = Fleet()
    fleet.tel("drone-2", lane_index=1)
    fleet.tel("drone-1", lane_index=1)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert {u["status"] for u in lost_entries(fleet)} == {"waiting"}
    fleet.tel("drone-2", lane_index=2, is_flying=False)
    assert len(fleet.assigns("drone-2")) == 1
    assert [(u["index"], u["reason"]) for u in lost_entries(fleet)] == [(k, LANDED_DRONE_2) for k in (1, 2, 3)]
    assert all("status" not in u for u in lost_entries(fleet))
    (failed,) = failed_events(fleet)
    assert failed["detail"] == f"3 lanes of drone-1 uncovered: {LANDED_DRONE_2}"
    assert [lane["index"] for lane in failed["lanes"]] == [1, 2, 3]
    with fleet.coord._lock:
        assert [(c.status, c.held) for r in fleet.coord._retasks for c in r.chunks] == [("failed", None)]
    # Later telemetry changes nothing: the lanes are never offered and not reported twice.
    for _ in range(3):
        fleet.tel("drone-2", lane_index=2, is_flying=False)
    assert len(fleet.assigns("drone-2")) == 1 and len(failed_events(fleet)) == 1


def test_r2n2_a_silent_receiver_makes_the_lanes_wait_with_that_reason_and_its_loss_fails_them():
    """Probe: drone-2 is silent when drone-1 is lost; the loss check declares it lost later."""
    silent = "waiting for drone-2, which has no fresh telemetry"
    fleet = Fleet()
    fleet.tel("drone-2", lane_index=1)
    fleet.tel("drone-1", lane_index=1)
    fleet.clock.advance(30.0)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    (waiting,) = retask_events(fleet)
    assert waiting["detail"].endswith(f"3 lanes awaiting a free drone ({silent})")
    assert {(u["status"], u["reason"]) for u in lost_entries(fleet)} == {("waiting", silent)}
    fleet.coord.on_drone_lost("drone-2", wait_s=0)
    reasons = {(u["drone_id"], u["reason"]) for u in lost_entries(fleet)}
    assert ("drone-1", "no drone left to take them") in reasons
    assert all("status" not in u for u in lost_entries(fleet))
    assert any(e["drone_id"] == "drone-1" for e in failed_events(fleet))


def test_r2n2_a_silent_receiver_that_reports_again_is_waited_for_as_before():
    fleet = Fleet()
    fleet.tel("drone-2", lane_index=1)
    fleet.tel("drone-1", lane_index=1)
    fleet.clock.advance(30.0)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    fleet.tel("drone-2", lane_index=2)
    assert {(u["status"], u["reason"]) for u in lost_entries(fleet)} == {("waiting", WAIT_DRONE_2)}
    fleet.done("drone-2")
    assert len(fleet.assigns("drone-2")) == 2 and failed_events(fleet) == []


def test_r2n2_a_receiver_not_flying_yet_is_waited_for_with_that_reason():
    fleet = Fleet()
    fleet.tel("drone-2", is_flying=False, state="idle")
    fleet.tel("drone-1", lane_index=1)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert {u["reason"] for u in lost_entries(fleet)} == {"waiting for drone-2, which is not flying"}
    fleet.tel("drone-2", is_flying=False, state="taking_off")
    assert {u["reason"] for u in lost_entries(fleet)} == {"waiting for drone-2, which is taking off"}
    fleet.tel("drone-2", lane_index=1)
    assert {u["reason"] for u in lost_entries(fleet)} == {WAIT_DRONE_2}
    fleet.done("drone-2")
    assert len(fleet.assigns("drone-2")) == 2 and failed_events(fleet) == []


def test_r2n2_lanes_wait_for_another_receiver_when_one_has_landed():
    fleet = Fleet(3, retask_separation=False, origins={"drone-3": 700})
    fleet.tel("drone-2", lane_index=1)
    fleet.tel("drone-3", lane_index=1)
    fleet.tel("drone-2", lane_index=2, is_flying=False)
    fleet.tel("drone-1", lane_index=1)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert {(u["status"], u["reason"]) for u in lost_entries(fleet)} == {
        ("waiting", "waiting for drone-3 to finish its own lanes")}
    fleet.done("drone-3")
    assert len(fleet.assigns("drone-3")) == 2 and len(fleet.assigns("drone-2")) == 1
    assert lost_entries(fleet) == [] and failed_events(fleet) == []


def test_r2n2_a_receiver_that_landed_after_its_own_lanes_is_still_offered_them():
    """Unchanged: finished is finished, as before the fix."""
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.done("drone-2", is_flying=False)
    fleet.tel("drone-1", lane_index=1)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert len(fleet.assigns("drone-2")) == 2 and failed_events(fleet) == []


def test_r2n2_a_fleet_without_obstacle_fields_still_offers_a_landed_drone_the_lanes_at_once():
    fleet = Fleet()
    fleet.edges["drone-2"].telemetry(is_flying=True, lane_index=1, lanes_total=len(LANES))
    fleet.edges["drone-2"].telemetry(is_flying=False, lane_index=2, lanes_total=len(LANES))
    fleet.edges["drone-1"].telemetry(is_flying=True, lane_index=1, lanes_total=len(LANES))
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + list(LANES[1:])
    assert fleet.coord.current_plan()["uncovered"] == []
    (event,) = retask_events(fleet)
    assert event["to"] == {"drone-2": len(TRANSIT) + 3} and "awaiting" not in event


def test_r2n2_a_fleet_without_obstacle_fields_lists_no_waiting_lanes_while_an_offer_is_deferred():
    """Only a hold of a drone that senses obstacles adds ``waiting`` entries; a deferred refusal adds none."""
    fleet = Fleet(edge_kwargs={"drone-2": {"auto_ack": False}})
    fleet.edges["drone-2"].telemetry(is_flying=True, lane_index=1, lanes_total=len(LANES))
    fleet.edges["drone-1"].telemetry(is_flying=True, lane_index=1, lanes_total=len(LANES))
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    offer = fleet.assigns("drone-2")[-1]
    assert len(fleet.assigns("drone-2")) == 2
    fleet.edges["drone-2"].reply({"type": "nack", "for": "assign_lanes", "drone_id": "drone-2",
                                  "reason": "busy", "lanes_total": len(offer["lanes"])})
    with fleet.coord._lock:
        assert [(c.status, c.held) for r in fleet.coord._retasks for c in r.chunks] == [("queued", None)]
    assert fleet.coord.current_plan()["uncovered"] == []
