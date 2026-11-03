"""Obstacle telemetry on the edge and its handling on the ground (design sections 3.3, 6 and 7.3).

The executor side is covered by a stub accessor on a real ``LaneExecutor``;
the ground side drives ``FleetCoordinator`` with the fake edges of
``tests/test_coordinator.py``. No drone, network or real time is involved.
"""
from __future__ import annotations

import logging

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone import coordinator as coordinator_module
from flightrisk.drone.coordinator import (
    FleetCoordinator,
    _apply_cut_deficit,
    _uncovered_segments,
)
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.obstacle_sensor import (
    CUT_OBSTACLE,
    CUT_PERSON,
    CUT_SENSOR_DOWN,
    CUT_SENSOR_STALE,
    MAX_CUTS_LISTED,
    SEGMENT_SKIPPED,
    SENSOR_UNAVAILABLE_DETAIL,
    TELEMETRY_FIELDS,
)
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from tests.lane_executor_support import FakeClock as ExecutorClock
from tests.lane_executor_support import QuietLogger, of_type
from tests.test_coordinator import make_fleet

# The demo plan of design section 3.3.
DEMO_LANES = [
    {"direction": "forward", "distance_cm": 400},
    {"direction": "right", "distance_cm": 100},
    {"direction": "back", "distance_cm": 400},
    {"direction": "right", "distance_cm": 100},
    {"direction": "forward", "distance_cm": 400},
]


def seg_tuple(seg):
    return (seg["lane_index"], seg["x_cm"], seg["y_cm"], seg["end_x_cm"], seg["end_y_cm"], seg["reason"])


# ---------------------------------------------------------------------------
# apply_cut_deficit and uncovered_segments (the local copy of the lane_steps pair)
# ---------------------------------------------------------------------------


def test_deficit_is_absorbed_by_the_next_lane_the_other_way():
    # A forward cut of 150 leaves the drone 150 cm short; a back lane rejoins the grid.
    assert _apply_cut_deficit("back", 400, -150) == (250, 0)
    # Another forward lane would widen the gap: it is flown as planned and the deficit stays.
    assert _apply_cut_deficit("forward", 400, -150) == (400, -150)
    # No deficit, no change; a lane is never lengthened.
    assert _apply_cut_deficit("back", 400, 0) == (400, 0)
    assert _apply_cut_deficit("forward", 400, 150) == (250, 0)


def test_deficit_larger_than_the_lane_leaves_a_remainder():
    assert _apply_cut_deficit("back", 100, -150) == (0, -50)


def test_a_lane_shortened_below_the_minimum_move_is_skipped():
    flown, deficit = _apply_cut_deficit("back", 400, -390)
    assert flown == 0
    assert deficit == 10  # the drone did not move, so the plan is 10 cm ahead of it


def test_sideways_lanes_do_not_touch_the_forward_deficit():
    assert _apply_cut_deficit("right", 100, 0) == (100, 0)
    assert _apply_cut_deficit("left", 100, 40) == (100, 40)  # a deficit of the other sign never shortens
    assert _apply_cut_deficit("left", 100, -40) == (60, 0)


def test_worked_example_of_the_design():
    segments = _uncovered_segments(DEMO_LANES, [(0, 250)])
    assert [seg_tuple(s) for s in segments] == [
        (0, 0, 250, 0, 400, CUT_OBSTACLE),
        (2, 100, 250, 100, 400, SEGMENT_SKIPPED),
    ]
    assert [s["unflown_cm"] for s in segments] == [150, 150]
    assert [s["cut"] for s in segments] == [True, False]


def test_no_cuts_no_segments():
    assert _uncovered_segments(DEMO_LANES, []) == []


def test_cut_reason_is_carried_to_the_segment():
    (segment, _) = _uncovered_segments(DEMO_LANES, [(0, 250)], [CUT_PERSON])
    assert segment["reason"] == CUT_PERSON


def test_a_cut_skips_the_following_lanes_in_the_same_direction():
    # A 900 cm displacement is split into 500 + 400 forward, then the lane across.
    lanes = [
        {"direction": "forward", "distance_cm": 500},
        {"direction": "forward", "distance_cm": 400},
        {"direction": "right", "distance_cm": 100},
        {"direction": "back", "distance_cm": 900},
    ]
    segments = _uncovered_segments(lanes, [(0, 300)])
    assert [seg_tuple(s) for s in segments] == [
        (0, 0, 300, 0, 500, CUT_OBSTACLE),
        (1, 0, 500, 0, 900, CUT_OBSTACLE),
        # The 600 cm deficit shortens the back lane, which starts at (100, 900).
        (3, 100, 300, 100, 900, SEGMENT_SKIPPED),
    ]


def test_a_forward_lane_after_a_forward_cut_is_never_lengthened_or_shortened():
    lanes = [
        {"direction": "forward", "distance_cm": 400},
        {"direction": "right", "distance_cm": 100},
        {"direction": "forward", "distance_cm": 400},
    ]
    segments = _uncovered_segments(lanes, [(0, 250)])
    assert [seg_tuple(s) for s in segments] == [(0, 0, 250, 0, 400, CUT_OBSTACLE)]


def test_a_cut_on_a_shortened_lane_reports_both_parts():
    # Lane 0 cut at 250; lane 2 flown as back 250 and cut after 100 cm.
    segments = _uncovered_segments(DEMO_LANES, [(0, 250), (2, 100)])
    assert [seg_tuple(s) for s in segments] == [
        (0, 0, 250, 0, 400, CUT_OBSTACLE),
        (2, 100, 250, 100, 400, SEGMENT_SKIPPED),
        # It rejoined the lane at (100, 250), flew 100 cm to (100, 150) and stopped.
        (2, 100, 150, 100, 0, CUT_OBSTACLE),
        # The drone ended 150 cm ahead of the plan, so the last forward lane is flown as forward 250.
        (4, 200, 150, 200, 0, SEGMENT_SKIPPED),
    ]


def test_a_sensor_down_cut_ends_the_plan():
    segments = _uncovered_segments(DEMO_LANES, [(2, 100)], [CUT_SENSOR_DOWN])
    assert [seg_tuple(s) for s in segments] == [
        (2, 100, 300, 100, 0, CUT_SENSOR_DOWN),
        (3, 100, 0, 200, 0, CUT_SENSOR_DOWN),
        (4, 200, 0, 200, 400, CUT_SENSOR_DOWN),
    ]


def test_cut_outside_the_lane_list_is_ignored():
    assert _uncovered_segments(DEMO_LANES, [(9, 10)]) == []


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def cut_fleet(n=2):
    """A planned and acked fleet whose drones fly the demo lanes."""
    coord, edges, clock, events = make_fleet(n=n)
    coord.plan()
    for state in coord._drones.values():
        state.lanes = [dict(lane) for lane in DEMO_LANES]
    return coord, edges, clock, events


def cut_telemetry(edge, seq, cuts, reasons="", **extra):
    flat = [v for pair in cuts for v in pair]
    edge.telemetry(is_flying=True, obstacle_seq=seq, obstacle_cuts=flat, obstacle_cuts_total=len(cuts),
                   obstacle_cut_reasons=reasons, **extra)


def lane_blocked(events):
    return [p for name, p in events if name == "fleet_event" and p.get("event") == "lane_blocked"]


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
    # planned length and a cut after 100 cm leaves 300 cm of it.
    assert [(e["lane_index"], e["reason"], e["y_cm"], e["unflown_cm"]) for e in new] == [
        (4, CUT_SENSOR_STALE, 100, 300)
    ]


def test_two_cuts_between_telemetry_messages_give_two_events():
    coord, edges, _, events = cut_fleet()
    cut_telemetry(edges["drone-1"], 2, [(0, 250), (2, 100)], f"{CUT_OBSTACLE},{CUT_PERSON}")
    assert [(e["lane_index"], e["reason"]) for e in lane_blocked(events)] == [(0, CUT_OBSTACLE), (2, CUT_PERSON)]


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


def test_a_lower_seq_means_a_new_plan_and_starts_again():
    coord, edges, _, events = cut_fleet()
    edge = edges["drone-1"]
    cut_telemetry(edge, 1, [(0, 250)])
    cut_telemetry(edge, 0, [])  # the edge accepted a new plan
    assert coord.current_plan()["uncovered"] == []
    cut_telemetry(edge, 1, [(2, 100)])
    assert [e["lane_index"] for e in lane_blocked(events)] == [0, 2]


def test_planning_again_clears_the_segments_and_ignores_stale_telemetry():
    coord, edges, clock, events = cut_fleet()
    cut_telemetry(edges["drone-1"], 1, [(0, 250)])
    assert coord.current_plan()["uncovered"]
    clock.advance(5.0)  # the flying report is stale, so planning is allowed
    coord.plan()
    assert coord.current_plan()["uncovered"] == []
    for state in coord._drones.values():
        state.lanes = [dict(lane) for lane in DEMO_LANES]
    # The old plan's telemetry arrives once more before the edge accepts the new plan.
    cut_telemetry(edges["drone-1"], 1, [(0, 250)])
    assert len(lane_blocked(events)) == 1


def test_malformed_cut_fields_are_dropped_without_raising():
    coord, edges, _, events = cut_fleet()
    edges["drone-1"].telemetry(is_flying=True, obstacle_seq=1, obstacle_cuts=[0, "x", -1, 5, 7],
                               obstacle_cut_reasons=17)
    assert coord.current_plan()["uncovered"] == []
    assert lane_blocked(events) == []
    edges["drone-1"].telemetry(is_flying=True, obstacle_seq="many", obstacle_cuts="no")
    assert coord.current_plan()["uncovered"] == []


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
            seen.append((drone_id, list(new_cuts), len(state.obstacle_segments)))

    coord, edges, clock, _ = make_fleet(n=1)
    coord = Seamed(dict(coord._links), area_cm=(400, 400), strip_cm=50, clock=clock)
    coord.plan()
    coord._drones["drone-1"].lanes = [dict(lane) for lane in DEMO_LANES]
    edge = edges["drone-1"]
    cut_telemetry(edge, 1, [(0, 250)], CUT_OBSTACLE)
    cut_telemetry(edge, 1, [(0, 250)], CUT_OBSTACLE)
    assert seen == [("drone-1", [(0, 250.0, CUT_OBSTACLE)], 2)]


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
    """A real executor with the optional obstacle accessor, as the stepped executor will have."""

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


def test_telemetry_is_unchanged_without_an_accessor():
    executor, _, _ = make_executor(LaneExecutor)
    try:
        msg = telemetry_of(executor)
        assert not any(key in msg for key in TELEMETRY_FIELDS)
        assert not hasattr(executor, "obstacle_status")
    finally:
        executor.shutdown(timeout=2.0)


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
