"""Obstacle telemetry on the edge and its handling on the ground (design sections 3.3, 4, 6 and 7.3).

The ground replays a drone's cuts with ``lane_steps.uncovered_segments``,
the function the edge's executor uses, so both derive the same segments.
The ground side drives ``FleetCoordinator`` with the fake edges of
``tests/test_coordinator.py``; one test flies the demo plan into a wall on a
real ``LaneExecutor`` and feeds its telemetry to the coordinator. No drone,
network or real time is involved.
"""
from __future__ import annotations

import logging

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone import coordinator as coordinator_module
from flightrisk.drone.coordinator import FleetCoordinator
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.lane_steps import Cut, uncovered_segments
from flightrisk.drone.obstacle_sensor import (
    CUT_OBSTACLE,
    CUT_PERSON,
    CUT_SENSOR_DOWN,
    CUT_SENSOR_STALE,
    MAX_CUTS_LISTED,
    SEGMENT_SKIPPED,
    SENSOR_UNAVAILABLE_DETAIL,
    TELEMETRY_FIELDS,
    BlockedZone,
    FakeObstacleSensor,
)
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from tests.lane_executor_support import FakeClock as ExecutorClock
from tests.lane_executor_support import (
    QuietLogger,
    SteppedTello,
    assign,
    of_type,
    reply_for,
    send,
    start_at,
    wait_for,
)
from tests.test_coordinator import make_fleet

# The demo plan of design section 3.3.
DEMO_LANES = [
    {"direction": "forward", "distance_cm": 400},
    {"direction": "right", "distance_cm": 100},
    {"direction": "back", "distance_cm": 400},
    {"direction": "right", "distance_cm": 100},
    {"direction": "forward", "distance_cm": 400},
]


def lanes_of(*pairs):
    return [{"direction": d, "distance_cm": cm} for d, cm in pairs]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def cut_fleet(n=2, lanes=DEMO_LANES):
    """A planned fleet whose drones fly *lanes* from their zone origins."""
    coord, edges, clock, events = make_fleet(n=n)
    coord.plan()
    for state in coord._drones.values():
        state.lanes = [dict(lane) for lane in lanes]
    return coord, edges, clock, events


def cut_telemetry(edge, seq, cuts, reasons="", total=None, **extra):
    """Telemetry listing *cuts* as the edge does: the last eight, numbered up to *seq*."""
    flat = [v for pair in cuts for v in pair]
    edge.telemetry(is_flying=True, obstacle_seq=seq, obstacle_cuts=flat,
                   obstacle_cuts_total=len(cuts) if total is None else total,
                   obstacle_cut_reasons=reasons, **extra)


def lane_blocked(events):
    return [p for name, p in events if name == "fleet_event" and p.get("event") == "lane_blocked"]


def obstacle_entries(coord, drone_id="drone-1"):
    return [u for u in coord.current_plan()["uncovered"] if u.get("kind") == "segment" and u["drone_id"] == drone_id]


def seg_tuple(entry):
    return (entry["index"], entry["x_cm"], entry["y_cm"], entry["end_x_cm"], entry["end_y_cm"], entry["reason"])


def ground_segments(lanes, cuts, reasons=""):
    """Segments the coordinator derives for drone-1 (whose zone origin is 0, 0) from one telemetry message."""
    coord, edges, _, _ = cut_fleet(n=1, lanes=lanes)
    assert coord._drones["drone-1"].zone.origin == (0, 0)
    cut_telemetry(edges["drone-1"], len(cuts), cuts, reasons)
    return [seg_tuple(e) for e in obstacle_entries(coord)]


# ---------------------------------------------------------------------------
# The ground's replay is lane_steps'
# ---------------------------------------------------------------------------


def test_the_coordinator_has_no_private_copy_of_the_cut_rules():
    assert not hasattr(coordinator_module, "_apply_cut_deficit")
    assert not hasattr(coordinator_module, "_uncovered_segments")
    assert coordinator_module.uncovered_segments is uncovered_segments


@pytest.mark.parametrize("lanes, cuts, reasons", [
    (DEMO_LANES, [(0, 250)], "obstacle"),
    (DEMO_LANES, [(0, 250), (2, 100)], "obstacle,person"),
    (DEMO_LANES, [(0, 10)], "sensor_stale"),
    (DEMO_LANES, [(0, 250), (1, 50)], "obstacle,sensor_down"),
    (lanes_of(("forward", 500), ("forward", 400), ("right", 100), ("back", 900)), [(0, 300)], "person"),
    (lanes_of(("right", 300), ("forward", 100), ("left", 300)), [(0, 120)], "autopilot"),
])
def test_ground_segments_are_lane_steps_segments(lanes, cuts, reasons):
    expected = [
        (s.lane_index, s.x_cm, s.y_cm, s.end_x_cm, s.end_y_cm, s.reason)
        for s in uncovered_segments(lanes, [Cut(lane, flown, reason)
                                            for (lane, flown), reason in zip(cuts, reasons.split(","))])
    ]
    assert expected
    assert ground_segments(lanes, cuts, reasons) == expected


def test_worked_example_of_the_design():
    coord, edges, _, _ = cut_fleet(n=1)
    cut_telemetry(edges["drone-1"], 1, [(0, 250)], CUT_OBSTACLE)
    entries = obstacle_entries(coord)
    assert [seg_tuple(e) for e in entries] == [
        (0, 0, 250, 0, 400, CUT_OBSTACLE),
        (2, 100, 250, 100, 400, SEGMENT_SKIPPED),
    ]
    assert [e["distance_cm"] for e in entries] == [150, 150]
    # Both segments belong to the cut numbered 1, which groups them for the re-flight.
    assert [e["cut_seq"] for e in entries] == [1, 1]


def test_no_cuts_no_segments():
    coord, edges, _, _ = cut_fleet(n=1)
    cut_telemetry(edges["drone-1"], 0, [])
    assert obstacle_entries(coord) == []


def test_cut_reason_is_carried_to_the_segment():
    assert ground_segments(DEMO_LANES, [(0, 250)], CUT_PERSON)[0][-1] == CUT_PERSON


def test_a_cut_skips_the_following_lanes_in_the_same_direction():
    # A 900 cm displacement is split into 500 + 400 forward, then the lane across.
    lanes = lanes_of(("forward", 500), ("forward", 400), ("right", 100), ("back", 900))
    assert ground_segments(lanes, [(0, 300)], CUT_OBSTACLE) == [
        (0, 0, 300, 0, 500, CUT_OBSTACLE),
        # The run after the cut takes the cut's reason (design 3.3, step 3), not ``skipped``.
        (1, 0, 500, 0, 900, CUT_OBSTACLE),
        # The 600 cm deficit shortens the back lane, which starts at (100, 900).
        (3, 100, 300, 100, 900, SEGMENT_SKIPPED),
    ]


def test_a_forward_lane_after_a_forward_cut_is_never_lengthened_or_shortened():
    lanes = lanes_of(("forward", 400), ("right", 100), ("forward", 400))
    assert ground_segments(lanes, [(0, 250)], CUT_OBSTACLE) == [(0, 0, 250, 0, 400, CUT_OBSTACLE)]


def test_a_cut_on_a_shortened_lane_reports_both_parts():
    # Lane 0 cut at 250; lane 2 flown as back 250 and cut after 100 cm.
    assert ground_segments(DEMO_LANES, [(0, 250), (2, 100)], "obstacle,obstacle") == [
        (0, 0, 250, 0, 400, CUT_OBSTACLE),
        (2, 100, 250, 100, 400, SEGMENT_SKIPPED),
        # It rejoined the lane at (100, 250), flew 100 cm to (100, 150) and stopped.
        (2, 100, 150, 100, 0, CUT_OBSTACLE),
        # The drone ended 150 cm ahead of the plan, so the last forward lane is flown as forward 250.
        (4, 200, 150, 200, 0, SEGMENT_SKIPPED),
    ]


def test_a_lane_skipped_below_the_minimum_move_keeps_the_true_deficit():
    # Cut 390 cm short: the back lane would fly 10 cm, so it is skipped, and the
    # drone is then 10 cm ahead of the plan (+10, not zero), so the last forward
    # lane is shortened by 10 cm instead of ending 10 cm past its planned end.
    assert ground_segments(DEMO_LANES, [(0, 10)], CUT_OBSTACLE) == [
        (0, 0, 10, 0, 400, CUT_OBSTACLE),
        (2, 100, 0, 100, 400, SEGMENT_SKIPPED),
        (4, 200, 10, 200, 0, SEGMENT_SKIPPED),
    ]


def test_a_sensor_down_cut_ends_the_plan():
    assert ground_segments(DEMO_LANES, [(2, 100)], CUT_SENSOR_DOWN) == [
        (2, 100, 300, 100, 0, CUT_SENSOR_DOWN),
        (3, 100, 0, 200, 0, CUT_SENSOR_DOWN),
        (4, 200, 0, 200, 400, CUT_SENSOR_DOWN),
    ]


def test_sensor_down_after_a_cut_keeps_the_remaining_lanes_inside_the_zone():
    segments = ground_segments(DEMO_LANES, [(0, 250), (1, 50)], f"{CUT_OBSTACLE},{CUT_SENSOR_DOWN}")
    assert segments == [
        (0, 0, 250, 0, 400, CUT_OBSTACLE),
        (1, 50, 250, 100, 250, CUT_SENSOR_DOWN),
        (2, 100, 400, 100, 0, CUT_SENSOR_DOWN),
        (3, 100, 0, 200, 0, CUT_SENSOR_DOWN),
        (4, 200, 0, 200, 400, CUT_SENSOR_DOWN),
    ]
    for _, x0, y0, x1, y1, _ in segments:
        assert 0 <= min(y0, y1) and max(y0, y1) <= 400


def test_cut_outside_the_lane_list_is_ignored():
    coord, edges, _, events = cut_fleet(n=1)
    cut_telemetry(edges["drone-1"], 1, [(9, 10)])
    assert obstacle_entries(coord) == []
    assert lane_blocked(events) == []


# ---------------------------------------------------------------------------
# Cuts in telemetry: numbering, events and plans
# ---------------------------------------------------------------------------


def test_cuts_become_segments_in_current_plan_with_drone_id_and_reason():
    coord, edges, _, _ = cut_fleet()
    cut_telemetry(edges["drone-1"], 1, [(0, 250)], CUT_PERSON)
    uncovered = coord.current_plan()["uncovered"]
    assert [(u["drone_id"], u["index"], u["reason"], u["distance_cm"]) for u in uncovered] == [
        ("drone-1", 0, CUT_PERSON, 150),
        ("drone-1", 2, SEGMENT_SKIPPED, 150),
    ]
    first = uncovered[0]
    assert (first["x_cm"], first["y_cm"], first["end_x_cm"], first["end_y_cm"]) == (0, 250, 0, 400)
    assert first["kind"] == "segment" and first["direction"] == "forward"
    assert set(first) == {"kind", "index", "direction", "distance_cm", "x_cm", "y_cm", "end_x_cm", "end_y_cm",
                          "drone_id", "reason", "cut_seq"}


def test_segments_use_absolute_coordinates_from_the_zone_origin():
    coord, edges, _, events = cut_fleet()
    origin_x = coord._drones["drone-2"].zone.origin[0]
    assert origin_x == 250
    cut_telemetry(edges["drone-2"], 1, [(0, 250)])
    uncovered = coord.current_plan()["uncovered"]
    assert (uncovered[0]["x_cm"], uncovered[0]["y_cm"]) == (origin_x, 250)
    assert (uncovered[1]["x_cm"], uncovered[1]["end_x_cm"]) == (origin_x + 100, origin_x + 100)
    (event,) = lane_blocked(events)
    assert (event["x_cm"], event["y_cm"]) == (origin_x, 250)


def test_one_lane_blocked_event_per_seq_increase():
    coord, edges, _, events = cut_fleet()
    edge = edges["drone-1"]
    cut_telemetry(edge, 1, [(0, 250)], CUT_OBSTACLE)
    cut_telemetry(edge, 1, [(0, 250)], CUT_OBSTACLE)  # the same telemetry again: nothing new
    cut_telemetry(edge, 1, [(0, 250)], CUT_OBSTACLE)
    (event,) = lane_blocked(events)
    assert event == {
        "event": "lane_blocked",
        "drone_id": "drone-1",
        "lane_index": 0,
        "x_cm": 0,
        "y_cm": 250,
        "unflown_cm": 150,
        "reason": CUT_OBSTACLE,
        "detail": "drone-1: lane 0 stopped at (0, 250) cm, 150 cm not searched (obstacle)",
    }
    assert [name for name, _ in events if name == "fleet_event"] == ["fleet_event"]

    cut_telemetry(edge, 2, [(0, 250), (4, 100)], f"{CUT_OBSTACLE},{CUT_SENSOR_STALE}")
    new = lane_blocked(events)[1:]
    # The first cut leaves a deficit of -150 that a forward lane never absorbs, so lane 4 is flown at its
    # planned length from y 0 and a cut after 100 cm leaves 300 cm of it.
    assert [(e["lane_index"], e["reason"], e["y_cm"], e["unflown_cm"]) for e in new] == [
        (4, CUT_SENSOR_STALE, 100, 300)
    ]


def test_two_cuts_between_telemetry_messages_give_two_events():
    coord, edges, _, events = cut_fleet()
    cut_telemetry(edges["drone-1"], 2, [(0, 250), (2, 100)], f"{CUT_OBSTACLE},{CUT_PERSON}")
    assert [(e["lane_index"], e["reason"]) for e in lane_blocked(events)] == [(0, CUT_OBSTACLE), (2, CUT_PERSON)]


def test_the_event_counts_a_same_direction_run_and_the_lanes_a_sensor_down_ends():
    lanes = lanes_of(("forward", 500), ("forward", 400), ("right", 100), ("back", 900))
    coord, edges, _, events = cut_fleet(n=1, lanes=lanes)
    cut_telemetry(edges["drone-1"], 1, [(0, 300)])
    (event,) = lane_blocked(events)
    # 200 cm of lane 0 and all 400 cm of lane 1; the shadow on lane 3 is not part of the stop.
    assert (event["x_cm"], event["y_cm"], event["unflown_cm"]) == (0, 300, 600)

    coord, edges, _, events = cut_fleet(n=1)
    cut_telemetry(edges["drone-1"], 1, [(2, 100)], CUT_SENSOR_DOWN)
    (event,) = lane_blocked(events)
    assert (event["lane_index"], event["x_cm"], event["y_cm"]) == (2, 100, 300)
    assert event["unflown_cm"] == 300 + 100 + 400
    assert event["reason"] == CUT_SENSOR_DOWN


def test_the_ground_keeps_every_cut_although_telemetry_lists_the_last_eight():
    lanes = lanes_of(*[("forward", 400), ("right", 50)] * 10)
    coord, edges, _, events = cut_fleet(n=1, lanes=lanes)
    cuts = []
    for k in range(10):
        cuts.append((2 * k, 100))
        listed = cuts[-MAX_CUTS_LISTED:]
        cut_telemetry(edges["drone-1"], k + 1, listed, ",".join([CUT_OBSTACLE] * len(listed)), total=len(cuts))
    entries = obstacle_entries(coord)
    assert [e["index"] for e in entries] == [2 * k for k in range(10)]
    assert [e["cut_seq"] for e in entries] == list(range(1, 11))
    assert all(e["distance_cm"] == 300 for e in entries)
    assert [e["lane_index"] for e in lane_blocked(events)] == [2 * k for k in range(10)]
    assert coord.status()["drone-1"]["uncovered_cm"] == 3000


def test_cuts_no_message_listed_are_reported_missing(caplog):
    lanes = lanes_of(*[("forward", 400), ("right", 50)] * 10)
    coord, edges, _, events = cut_fleet(n=1, lanes=lanes)
    cuts = [(2 * k, 100) for k in range(10)]
    with caplog.at_level(logging.WARNING, logger=coordinator_module.logger.name):
        cut_telemetry(edges["drone-1"], 10, cuts[-MAX_CUTS_LISTED:], total=10)
    # Only the eight listed cuts (numbers 3 to 10) are known; the first two are lost.
    assert [e["cut_seq"] for e in obstacle_entries(coord)] == list(range(3, 11))
    assert len(lane_blocked(events)) == MAX_CUTS_LISTED
    assert any("2 cuts that no telemetry listed" in r.getMessage() for r in caplog.records)


def test_telemetry_without_obstacle_fields_changes_nothing():
    coord, edges, _, events = cut_fleet()
    edges["drone-1"].telemetry(is_flying=True)
    assert coord.current_plan()["uncovered"] == []
    assert lane_blocked(events) == []
    # Status of a drone that does not report obstacle sensing is exactly what it was.
    entry = coord.status()["drone-1"]
    assert not any(k.startswith("obstacle_") for k in entry)
    assert "uncovered_cm" not in entry and "reflights" not in entry


def test_sticky_cuts_survive_telemetry_without_the_fields():
    coord, edges, _, events = cut_fleet()
    cut_telemetry(edges["drone-1"], 1, [(0, 250)])
    edges["drone-1"].telemetry(is_flying=True)
    assert len(coord.current_plan()["uncovered"]) == 2
    assert len(lane_blocked(events)) == 1
    assert coord.status()["drone-1"]["uncovered_cm"] == 300


def test_the_count_carries_across_plans_and_old_cuts_are_ignored():
    coord, edges, clock, events = cut_fleet()
    edge = edges["drone-1"]
    cut_telemetry(edge, 1, [(0, 250)])
    cut_telemetry(edge, 2, [(0, 250), (2, 100)])
    assert len(obstacle_entries(coord)) == 4
    clock.advance(5.0)  # the flying report is stale, so planning is allowed
    coord.plan()
    for state in coord._drones.values():
        state.lanes = [dict(lane) for lane in DEMO_LANES]
    assert obstacle_entries(coord) == []
    # The old plan's telemetry arrives once more before the edge accepts the new plan.
    cut_telemetry(edge, 2, [(0, 250), (2, 100)])
    assert obstacle_entries(coord) == [] and len(lane_blocked(events)) == 2
    # The edge accepted the plan: its count stays at 2 and the plan has no cuts.
    cut_telemetry(edge, 2, [])
    assert obstacle_entries(coord) == []
    # The new plan's first cut is cut number 3.
    cut_telemetry(edge, 3, [(4, 100)], CUT_PERSON)
    assert [(e["index"], e["reason"], e["cut_seq"]) for e in obstacle_entries(coord)] == [(4, CUT_PERSON, 3)]
    assert [e["lane_index"] for e in lane_blocked(events)] == [0, 2, 4]


def test_a_plan_the_edge_left_without_the_ground_drops_its_cuts():
    # obstacle_cuts_total counts only the edge's current plan, so older cuts are dropped.
    coord, edges, _, _ = cut_fleet(n=1)
    cut_telemetry(edges["drone-1"], 1, [(0, 250)])
    assert obstacle_entries(coord)
    cut_telemetry(edges["drone-1"], 1, [], total=0)
    assert obstacle_entries(coord) == []


def test_a_lower_count_means_the_edge_restarted(caplog):
    coord, edges, _, events = cut_fleet(n=1)
    edge = edges["drone-1"]
    cut_telemetry(edge, 3, [(0, 250)], total=1)
    with caplog.at_level(logging.WARNING, logger=coordinator_module.logger.name):
        cut_telemetry(edge, 0, [])
    assert obstacle_entries(coord) == []
    assert any("restarted" in r.getMessage() for r in caplog.records)
    cut_telemetry(edge, 1, [(2, 100)])
    assert [e["lane_index"] for e in lane_blocked(events)] == [0, 2]


def test_a_continuing_re_task_keeps_the_cuts_and_replays_appended_lanes():
    coord, edges, _, events = cut_fleet(n=1)
    edge = edges["drone-1"]
    cut_telemetry(edge, 1, [(0, 250)])
    # A re-task appends lanes (the coordinator keeps the previous lanes as the prefix).
    state = coord._drones["drone-1"]
    state.lanes = state.lanes + lanes_of(("right", 100), ("back", 400))
    cut_telemetry(edge, 1, [(0, 250)])
    assert [e["index"] for e in obstacle_entries(coord)] == [0, 2]
    # A cut on an appended lane indexes the whole lane list.
    cut_telemetry(edge, 2, [(0, 250), (6, 100)], "obstacle,person")
    assert [(e["index"], e["reason"]) for e in obstacle_entries(coord)] == [
        (0, CUT_OBSTACLE), (2, SEGMENT_SKIPPED), (6, CUT_PERSON),
    ]
    assert [e["lane_index"] for e in lane_blocked(events)] == [0, 6]


def test_malformed_cut_fields_are_dropped_without_raising():
    coord, edges, _, events = cut_fleet()
    edges["drone-1"].telemetry(is_flying=True, obstacle_seq=1, obstacle_cuts=["x", "y", 7],
                               obstacle_cut_reasons=17)
    assert coord.current_plan()["uncovered"] == []
    assert lane_blocked(events) == []
    edges["drone-1"].telemetry(is_flying=True, obstacle_seq="many", obstacle_cuts="no")
    edges["drone-1"].telemetry(is_flying=True, obstacle_seq=1.5, obstacle_cuts=[0, 250])
    edges["drone-1"].telemetry(is_flying=True, obstacle_seq=-1, obstacle_cuts=[0, 250])
    assert coord.current_plan()["uncovered"] == []
    assert lane_blocked(events) == []


def test_a_malformed_pair_does_not_shift_the_numbers_of_the_others():
    coord, edges, _, events = cut_fleet(n=1)
    edges["drone-1"].telemetry(is_flying=True, obstacle_seq=3, obstacle_cuts=[0, 250, "x", 5, 2, 100],
                               obstacle_cuts_total=3, obstacle_cut_reasons="obstacle,obstacle,person")
    assert [(e["index"], e["reason"], e["cut_seq"]) for e in obstacle_entries(coord) if e["reason"] != "skipped"] \
        == [(0, CUT_OBSTACLE, 1), (2, CUT_PERSON, 3)]
    assert [e["lane_index"] for e in lane_blocked(events)] == [0, 2]


def test_status_carries_the_obstacle_fields_per_drone():
    coord, edges, _, _ = cut_fleet()
    cut_telemetry(edges["drone-1"], 1, [(0, 250)], CUT_OBSTACLE, obstacle_sensing="stepped",
                  obstacle_sensor_ok=True, obstacle_state="blocked", obstacle_latency_ms=210)
    edges["drone-2"].telemetry(obstacle_sensing="off", obstacle_sensor_ok=False, obstacle_state="unavailable")
    status = coord.status()
    one, two = status["drone-1"], status["drone-2"]
    assert (one["obstacle_sensing"], one["obstacle_sensor_ok"], one["obstacle_state"]) == ("stepped", True, "blocked")
    assert one["obstacle_latency_ms"] == 210
    assert one["uncovered_cm"] == 300
    assert one["reflights"] == {"offered": 0, "reflown": 0, "blocked_again": 0}
    assert (two["obstacle_sensing"], two["obstacle_sensor_ok"], two["uncovered_cm"]) == ("off", False, 0)
    assert two["obstacle_latency_ms"] is None


def test_the_re_flight_seam_gets_the_new_cuts_once():
    seen = []

    class Seamed(FleetCoordinator):
        def _on_new_cuts_locked(self, drone_id, state, new_cuts):
            numbers, segments = self._obstacle_replay_locked(state)
            seen.append((drone_id, list(new_cuts), numbers, [(s.lane_index, s.cut) for s in segments]))

    coord, edges, clock, _ = make_fleet(n=1)
    coord = Seamed(dict(coord._links), area_cm=(400, 400), strip_cm=50, clock=clock)
    coord.plan()
    coord._drones["drone-1"].lanes = [dict(lane) for lane in DEMO_LANES]
    edge = edges["drone-1"]
    cut_telemetry(edge, 1, [(0, 250)], CUT_OBSTACLE)
    cut_telemetry(edge, 1, [(0, 250)], CUT_OBSTACLE)
    cut_telemetry(edge, 2, [(0, 250), (2, 100)], "obstacle,person")
    assert seen == [
        ("drone-1", [(1, Cut(0, 250, CUT_OBSTACLE))], [1], [(0, 0), (2, 0)]),
        ("drone-1", [(2, Cut(2, 100, CUT_PERSON))], [1, 2], [(0, 0), (2, 0), (2, 1), (4, 1)]),
    ]


# ---------------------------------------------------------------------------
# A real edge's telemetry on the ground
# ---------------------------------------------------------------------------


def test_a_real_executor_cut_reaches_the_ground_as_the_same_segments():
    """Fly the demo plan into a wall at y 250 on a real executor and hand its telemetry to the coordinator."""
    clock = ExecutorClock()
    drone = SteppedTello(auto=True, name="drone-1", battery=90)
    drone.connect()
    envelope = SafetyEnvelope(alt_slot_cm=100, geofence_cm=500, clock=clock)
    sensor = FakeObstacleSensor(envelope.pose_snapshot, [BlockedZone(heading_deg=0.0, y_min=250, x_max=50)])
    sent: list[dict] = []
    executor = LaneExecutor(drone, envelope, "drone-1", send=sent.append, clock=clock, logger=QuietLogger(),
                            fleet_v2=True, obstacle_sensor=sensor, obstacle_retry_pause_s=0.0)
    try:
        executor.handle_clock({"type": "clock", "offset_s": 0.0})
        send(executor, assign(lanes=DEMO_LANES, plan_id="p-1", plan_seq=1, cmd_id="a-1"))
        assert reply_for(sent, "a-1")["type"] == "ack"
        t_ground = clock() + 0.5
        send(executor, start_at(t_ground, plan_id="p-1", cmd_id="s-1"))
        clock.set(t_ground)
        assert wait_for(lambda: not executor.is_busy and executor.lane_index >= executor.lanes_total)
        telemetry = executor.telemetry()
        edge_segments = executor.obstacle_segments()
    finally:
        executor.shutdown(timeout=2.0)
    assert telemetry["obstacle_seq"] == 1 and telemetry["obstacle_cuts"] == [0, 250]

    coord, edges, _, events = cut_fleet(n=1)
    edges["drone-1"].reply(telemetry)
    entries = obstacle_entries(coord)
    assert [seg_tuple(e) for e in entries] == [
        (s.lane_index, s.x_cm, s.y_cm, s.end_x_cm, s.end_y_cm, s.reason) for s in edge_segments
    ]
    assert [e["distance_cm"] for e in entries] == [s.length_cm for s in edge_segments]
    (event,) = lane_blocked(events)
    assert (event["lane_index"], event["x_cm"], event["y_cm"], event["unflown_cm"]) == (0, 0, 250, 150)


# ---------------------------------------------------------------------------
# The start gate
# ---------------------------------------------------------------------------


def ready_fleet():
    coord, edges, clock, events = make_fleet(n=2)
    coord.plan()
    coord.sync()
    return coord, edges, clock


def test_start_refuses_a_stepped_drone_whose_sensor_is_down():
    coord, edges, _ = ready_fleet()
    edges["drone-1"].telemetry(obstacle_sensing="stepped", obstacle_sensor_ok=False, obstacle_state="unavailable")
    edges["drone-2"].telemetry(obstacle_sensing="stepped", obstacle_sensor_ok=True)
    result = coord.start()
    assert not result.ok
    assert set(refusals(result)) == {"drone-1"}
    assert not edges["drone-1"].of_type("start_at") and not edges["drone-2"].of_type("start_at")


def refusals(result):
    return result.refusals


def test_start_refusal_names_the_sensor_problem():
    coord, edges, _ = ready_fleet()
    edges["drone-1"].telemetry(obstacle_sensing="stepped", obstacle_sensor_ok=False, obstacle_state="unavailable")
    result = coord.start()
    assert not result.ok
    text = refusals(result)["drone-1"]
    assert text.startswith(SENSOR_UNAVAILABLE_DETAIL)
    assert "drone-2" not in refusals(result)


def test_start_only_warns_for_a_drone_flying_unsensed(caplog):
    coord, edges, _ = ready_fleet()
    edges["drone-1"].telemetry(obstacle_sensing="off", obstacle_sensor_ok=False)
    with caplog.at_level(logging.WARNING, logger=coordinator_module.logger.name):
        result = coord.start()
    assert result.ok
    assert any("drone-1" in r.getMessage() and "unsensed" in r.getMessage() for r in caplog.records)
    assert len(edges["drone-1"].of_type("start_at")) == 1


def test_start_ignores_a_dead_sensor_report_that_is_stale_or_missing():
    coord, edges, clock = ready_fleet()
    edges["drone-1"].telemetry(obstacle_sensing="stepped", obstacle_sensor_ok=False)
    clock.advance(5.0)  # older than TELEMETRY_FRESH_S
    assert coord.start().ok


def test_start_allows_a_stepped_drone_with_a_working_sensor_and_older_edges():
    coord, edges, _ = ready_fleet()
    edges["drone-1"].telemetry(obstacle_sensing="stepped", obstacle_sensor_ok=True)
    edges["drone-2"].telemetry()  # an edge that knows nothing of obstacle sensing
    assert coord.start().ok


# ---------------------------------------------------------------------------
# The privacy screen
# ---------------------------------------------------------------------------


def edge_telemetry(**extra):
    msg = {"type": "telemetry", "drone_id": "drone-1", "battery": 80, "height_cm": 100, "is_flying": True,
           "rel_x_cm": 0, "rel_y_cm": 0, "heading_deg": 0, "lane_index": 3, "lanes_total": 5, "link_age_s": 0.1,
           "takeoff_at_local_s": 1.0}
    msg.update(extra)
    return msg


def full_obstacle_fields(pairs=MAX_CUTS_LISTED):
    cuts = [v for i in range(pairs) for v in (i, 150 + i)]
    return {
        "obstacle_sensing": "stepped", "obstacle_sensor_ok": True, "obstacle_state": "blocked",
        "obstacle_latency_ms": 212.5, "obstacle_seq": 11, "obstacle_cuts": cuts,
        "obstacle_cuts_total": 11, "obstacle_cut_reasons": ",".join([CUT_SENSOR_STALE] * pairs),
    }


def test_privacy_screen_accepts_the_flat_sixteen_number_cut_list():
    fields = full_obstacle_fields()
    assert len(fields["obstacle_cuts"]) == 16 == fp.MAX_NUMBERS_PER_LIST
    assert set(fields) == set(TELEMETRY_FIELDS)
    # Every number in the message counts towards the per-message limit, which has room to spare.
    assert len(fields["obstacle_cuts"]) <= fp.MAX_LISTED_NUMBERS
    fp.validate_edge_to_ground(edge_telemetry(**fields), proto=1)
    fp.validate_edge_to_ground(
        edge_telemetry(**fields, epoch=0, plan_id="p-1", state="flying_lanes", plan_valid=True), proto=2
    )


def test_privacy_screen_would_refuse_a_longer_list_so_the_cap_matters():
    fields = full_obstacle_fields(pairs=MAX_CUTS_LISTED + 1)
    with pytest.raises(fp.ProtocolError, match="numbers"):
        fp.validate_edge_to_ground(edge_telemetry(**fields), proto=1)


def test_the_longest_reason_string_is_accepted():
    from flightrisk.drone.obstacle_sensor import MAX_CUT_REASONS_CHARS

    fields = full_obstacle_fields()
    # Eight of the longest reason, comma separated, is the longest real value.
    fields["obstacle_cut_reasons"] = ",".join([CUT_SENSOR_STALE] * MAX_CUTS_LISTED)
    assert len(fields["obstacle_cut_reasons"]) <= MAX_CUT_REASONS_CHARS
    fp.validate_edge_to_ground(edge_telemetry(**fields), proto=1)


# ---------------------------------------------------------------------------
# lane_monitor.telemetry()
# ---------------------------------------------------------------------------


class StubExecutor(LaneExecutor):
    """A real executor whose obstacle accessor returns what the test sets."""

    status = None
    error: Exception | None = None

    def obstacle_status(self):
        if self.error is not None:
            raise self.error
        return self.status


def make_executor(cls):
    clock = ExecutorClock()
    drone = FakeTelloController(name="drone-1", battery=90)
    drone.connect()
    sent: list[dict] = []
    log = QuietLogger()
    executor = cls(drone, SafetyEnvelope(alt_slot_cm=100, clock=clock), "drone-1", send=sent.append,
                   clock=clock, logger=log)
    return executor, sent, log


def telemetry_of(executor):
    return executor.telemetry()


def test_telemetry_is_unchanged_for_an_executor_without_a_sensor():
    executor, _, _ = make_executor(LaneExecutor)
    try:
        # The executor's own accessor (t01) is what the monitor reads.
        assert executor.obstacle_status() == {}
        msg = telemetry_of(executor)
        assert not any(key in msg for key in TELEMETRY_FIELDS)
    finally:
        executor.shutdown(timeout=2.0)


class NoAccessorExecutor(LaneExecutor):
    """An executor whose class provides no obstacle accessor."""

    obstacle_status = None


def test_telemetry_is_unchanged_without_an_accessor():
    plain, _, _ = make_executor(LaneExecutor)
    bare, _, _ = make_executor(NoAccessorExecutor)
    try:
        assert set(telemetry_of(bare)) == set(telemetry_of(plain))
    finally:
        plain.shutdown(timeout=2.0)
        bare.shutdown(timeout=2.0)


@pytest.mark.parametrize("status", [None, {}])
def test_telemetry_adds_nothing_when_the_accessor_returns_nothing(status):
    plain, _, _ = make_executor(LaneExecutor)
    stub, _, _ = make_executor(StubExecutor)
    try:
        stub.status = status
        a, b = telemetry_of(plain), telemetry_of(stub)
        assert set(a) == set(b)
    finally:
        plain.shutdown(timeout=2.0)
        stub.shutdown(timeout=2.0)


def test_telemetry_carries_the_accessors_fields():
    stub, _, _ = make_executor(StubExecutor)
    try:
        fields = full_obstacle_fields()
        stub.status = dict(fields, not_a_contract_field=1)
        msg = telemetry_of(stub)
        assert {k: msg[k] for k in TELEMETRY_FIELDS} == fields
        assert "not_a_contract_field" not in msg
        fp.validate_edge_to_ground(msg, proto=1)
    finally:
        stub.shutdown(timeout=2.0)


def test_a_partial_status_adds_only_its_fields():
    stub, _, _ = make_executor(StubExecutor)
    try:
        stub.status = {"obstacle_sensing": "off"}
        msg = telemetry_of(stub)
        assert msg["obstacle_sensing"] == "off"
        assert not any(key in msg for key in TELEMETRY_FIELDS if key != "obstacle_sensing")
    finally:
        stub.shutdown(timeout=2.0)


def test_a_failing_accessor_does_not_break_telemetry():
    stub, _, log = make_executor(StubExecutor)
    try:
        stub.error = RuntimeError("sensor thread died")
        msg = telemetry_of(stub)
        assert msg["type"] == "telemetry" and not any(key in msg for key in TELEMETRY_FIELDS)
        assert any(event == "edge_obstacle_status_failed" for _, event, _ in log.events)
    finally:
        stub.shutdown(timeout=2.0)


def test_tick_sends_the_obstacle_fields():
    stub, sent, _ = make_executor(StubExecutor)
    try:
        stub.status = {"obstacle_sensing": "stepped", "obstacle_sensor_ok": True}
        stub.tick()
        telemetry = of_type(sent, "telemetry")[-1]
        assert telemetry["obstacle_sensing"] == "stepped" and telemetry["obstacle_sensor_ok"] is True
    finally:
        stub.shutdown(timeout=2.0)
