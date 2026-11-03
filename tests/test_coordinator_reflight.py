"""The offer-once re-flight of a cut segment on the ground (design section 3.4).

Two and three fake edges (the in-process links of ``tests/test_coordinator.py``,
answering synchronously from inside ``send``) run a started mission through
``FleetCoordinator`` with scripted telemetry and an injected clock. A cut
reported by one drone queues a ``reflight`` chunk, which is offered once to
another eligible drone after the cutting drone has finished its lanes, flown
reversed at the receiver's own altitude slot, and followed to ``reflown`` or
``blocked_again`` from the receiver's telemetry. No drone, network or real
time is involved.
"""
from __future__ import annotations

import logging

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone import coordinator as coordinator_module
from flightrisk.drone.coordinator import (
    MAX_REFLIGHTS_PER_DRONE,
    FleetCoordinator,
    ZoneAssignment,
)
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.obstacle_sensor import (
    CUT_AUTOPILOT,
    CUT_OBSTACLE,
    CUT_PERSON,
    CUT_SENSOR_DOWN,
    CUT_SENSOR_STALE,
    SEGMENT_SKIPPED,
    SEGMENT_TOO_SHORT,
    BlockedZone,
    FakeObstacleSensor,
)
from flightrisk.drone.safety import SafetyEnvelope
from tests.lane_executor_support import FakeClock as ExecutorClock
from tests.lane_executor_support import QuietLogger, SteppedTello, wait_for
from tests.test_coordinator import FakeClock, FakeEdge

# Each drone flies a two-pass zone from its takeoff point: forward 400, across
# 100, back 400, across 100. It ends at (200, 0) relative to its takeoff point.
LANES = (
    {"direction": "forward", "distance_cm": 400},
    {"direction": "right", "distance_cm": 100},
    {"direction": "back", "distance_cm": 400},
    {"direction": "right", "distance_cm": 100},
)
# The demo plan of design section 3.3, which ends at the far side of the zone.
DEMO = LANES + ({"direction": "forward", "distance_cm": 400},)

# drone-1 takes off at x = 0, drone-2 350 cm to its right, drone-3 350 cm to its left.
ORIGINS = {"drone-1": 0, "drone-2": 350, "drone-3": -350}
ALT = {"drone-1": 100, "drone-2": 130, "drone-3": 130}

# The re-flight of drone-1's cut at 250 cm on lane 0 (the worked example):
# drone-2 ends its lanes at (550, 0), transits forward 400 and left 550 to
# (0, 400), then flies back 150, right 100 and forward 150.
TRANSIT = [
    {"direction": "forward", "distance_cm": 400},
    {"direction": "left", "distance_cm": 275},
    {"direction": "left", "distance_cm": 275},
]
REFLIGHT = [
    {"direction": "back", "distance_cm": 150},
    {"direction": "right", "distance_cm": 100},
    {"direction": "forward", "distance_cm": 150},
]


class Fleet:
    """A started mission of fake edges, and helpers that script their telemetry."""

    def __init__(self, n=2, *, lanes=LANES, origins=None, geofence_cm=None, edge_kwargs=None, **kwargs):
        self.clock = FakeClock()
        ids = [f"drone-{i + 1}" for i in range(n)]
        origins = dict(ORIGINS, **(origins or {}))
        edge_kwargs = edge_kwargs or {}
        self.edges = {rid: FakeEdge(rid, self.clock, 0.0, **edge_kwargs.get(rid, {})) for rid in ids}
        self.events = []
        self.coord = FleetCoordinator(
            self.edges, area_cm=(1200, 400), strip_cm=100, clock=self.clock,
            on_event=lambda name, payload: self.events.append((name, payload)),
            geofence_cm=geofence_cm, retask_wait_s=0.0, **kwargs,
        )
        zones = [
            ZoneAssignment(drone_id=rid, x_cm=origins[rid], y_cm=0, w_cm=200, h_cm=400, alt_cm=ALT[rid],
                           lanes=tuple(dict(lane) for lane in lanes))
            for rid in ids
        ]
        xs = [origins[rid] for rid in ids]
        self.coord.plan_zones(zones, grid_cm=(min(xs), 0, max(xs) + 200 - min(xs), 400), gap_cm=100, strip_cm=100)
        for rid in ids:
            if not self.edges[rid].auto_ack:
                self.edges[rid].ack_lanes(len(lanes))
        self.coord.sync(rounds=1, timeout_s=0.1)
        result = self.coord.start()
        assert result.ok, result.refusals

    # -- telemetry ------------------------------------------------------

    def tel(self, rid, **fields):
        """One telemetry message from a sensed drone in flight, with *fields* on top."""
        msg = {
            "is_flying": True, "battery": 80, "obstacle_sensing": "stepped", "obstacle_sensor_ok": True,
            "obstacle_state": "clear", "lane_index": 0, "lanes_total": len(self.coord.lanes(rid)),
        }
        msg.update(fields)
        self.edges[rid].telemetry(**msg)

    def cut(self, rid, cuts, reasons, **fields):
        """Telemetry listing *cuts* (``[(lane, flown_cm), ...]``) as the edge does, numbered 1 to n."""
        flat = [v for pair in cuts for v in pair]
        self.tel(rid, obstacle_seq=len(cuts), obstacle_cuts=flat, obstacle_cuts_total=len(cuts),
                 obstacle_cut_reasons=reasons, **fields)

    def done(self, rid, **fields):
        """drone *rid* hovers at the end of its lanes (at (200, 0) from its takeoff unless told)."""
        total = len(self.coord.lanes(rid))
        msg = {"lane_index": total, "lanes_total": total, "rel_x_cm": 200, "rel_y_cm": 0}
        msg.update(fields)
        self.tel(rid, **msg)

    # -- what the ground did -------------------------------------------

    def assigns(self, rid):
        return self.edges[rid].of_type("assign_lanes")

    def reflight_events(self, kind=None):
        return [p for name, p in self.events
                if name == "fleet_event" and str(p.get("event", "")).startswith("reflight_")
                and (kind is None or p["event"] == kind)]

    def segments(self, drone_id="drone-1"):
        return [u for u in self.coord.current_plan()["uncovered"]
                if u.get("kind") == "segment" and u["drone_id"] == drone_id]


def ends(entry):
    return (entry["x_cm"], entry["y_cm"], entry["end_x_cm"], entry["end_y_cm"])


def released(n=2, **kwargs):
    """drone-1 cut lane 0 at 250 cm, drone-2 hovers done, drone-1 finished: the offer is out and acked."""
    fleet = Fleet(n, **kwargs)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    for rid in fleet.edges:
        if rid != "drone-1":
            fleet.done(rid)
    fleet.done("drone-1")
    return fleet


# ---------------------------------------------------------------------------
# A cut waits for the cutting drone to finish
# ---------------------------------------------------------------------------


def test_a_cut_queues_a_reflight_and_nothing_is_sent_while_the_cutter_has_lanes():
    fleet = Fleet()
    fleet.tel("drone-2", lane_index=1)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    assert len(fleet.assigns("drone-2")) == 1 and len(fleet.assigns("drone-1")) == 1
    segments = fleet.segments()
    assert [(s["index"], s["reason"], ends(s)) for s in segments] == [
        (0, CUT_OBSTACLE, (0, 250, 0, 400)),
        (2, SEGMENT_SKIPPED, (100, 250, 100, 400)),
    ]
    assert all(s["status"] == "waiting" and s["waiting_for"] == "drone-1" for s in segments)
    assert all(s["cut_seq"] == 1 for s in segments)
    assert fleet.reflight_events() == []
    # Every later telemetry message while it still has lanes changes nothing.
    fleet.tel("drone-1", lane_index=3)
    fleet.tel("drone-2", lane_index=4)
    assert len(fleet.assigns("drone-2")) == 1 and fleet.reflight_events() == []


def test_the_cutter_finishing_releases_the_offer_after_the_receivers_remaining_lanes():
    fleet = Fleet()
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.tel("drone-2", lane_index=1)  # still flying its own zone
    fleet.done("drone-1")
    offer = fleet.assigns("drone-2")[-1]
    fp.validate_message(offer)
    # Its own lanes unchanged, then the transit, then the reversed segment and the shadow.
    assert offer["lanes"] == list(LANES) + TRANSIT + REFLIGHT
    assert fleet.coord.lanes("drone-2") == offer["lanes"]
    # The cutting drone is never sent the segment back.
    assert len(fleet.assigns("drone-1")) == 1


def test_the_receiver_keeps_its_own_altitude_slot():
    fleet = released()
    offer = fleet.assigns("drone-2")[-1]
    assert offer["alt_cm"] == ALT["drone-2"] == 130
    assert offer["zone"]["x_cm"] == ORIGINS["drone-2"]


def test_a_finished_hovering_receiver_gets_the_offer_from_where_it_hovers():
    fleet = released()
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + REFLIGHT
    assert [e["event"] for e in fleet.reflight_events()] == ["reflight_offered", "reflight_accepted"]


def test_the_offered_and_accepted_events_carry_the_segments():
    fleet = released()
    offered, accepted = fleet.reflight_events()
    for event in (offered, accepted):
        assert event["drone_id"] == "drone-1" and event["receiver"] == "drone-2" and event["cut_seq"] == 1
        assert event["lanes"] == [
            {"index": 0, "direction": "forward", "distance_cm": 150,
             "x_cm": 0, "y_cm": 250, "end_x_cm": 0, "end_y_cm": 400},
            {"index": 2, "direction": "back", "distance_cm": 150,
             "x_cm": 100, "y_cm": 250, "end_x_cm": 100, "end_y_cm": 400},
        ]
        assert isinstance(event["detail"], str) and "drone-1" in event["detail"]
    assert "drone-2" in offered["detail"] and "300 cm" in offered["detail"]
    assert "will re-fly" in accepted["detail"]


def test_an_accepted_offer_shows_acked_then_flying():
    fleet = released()
    assert {(s["status"], s["receiver"]) for s in fleet.segments()} == {("acked", "drone-2")}
    fleet.tel("drone-2", lane_index=5)  # on the transit
    assert {(s["status"], s["receiver"]) for s in fleet.segments()} == {("flying", "drone-2")}
    assert [e["event"] for e in fleet.reflight_events()] == ["reflight_offered", "reflight_accepted"]


def test_a_cutter_that_lands_releases_the_offer():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.tel("drone-1", lane_index=2, is_flying=False)
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + REFLIGHT


def test_a_cut_on_the_last_lane_is_released_by_the_same_message():
    fleet = Fleet()
    fleet.done("drone-2")
    # Its last lane done, hovering clear of drone-2's new path (which runs along y = 0).
    fleet.cut("drone-1", [(2, 100)], CUT_OBSTACLE, lane_index=4, rel_x_cm=0, rel_y_cm=400)
    offer = fleet.assigns("drone-2")[-1]
    # Lane 2 (back 400) stopped after 100 cm at (100, 300): re-fly from (100, 0) forward 300.
    assert offer["lanes"][-1] == {"direction": "forward", "distance_cm": 300}


# ---------------------------------------------------------------------------
# Separation from the cutting drone
# ---------------------------------------------------------------------------


def test_a_cutter_hovering_near_the_path_keeps_the_chunk_waiting_until_it_lands():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    # Done, but hovering 50 cm beside the segment it left.
    fleet.done("drone-1", rel_x_cm=50, rel_y_cm=300)
    assert len(fleet.assigns("drone-2")) == 1
    segments = fleet.segments()
    assert {(s["status"], s["waiting_for"]) for s in segments} == {("waiting", "drone-1")}
    assert all("drone-2 would come within" in s["detail"] and "drone-1" in s["detail"] for s in segments)
    assert fleet.reflight_events() == []
    # Re-evaluated on each of its telemetry messages: still too close, then moved away.
    fleet.done("drone-1", rel_x_cm=60, rel_y_cm=300)
    assert len(fleet.assigns("drone-2")) == 1
    fleet.done("drone-1", rel_x_cm=60, rel_y_cm=300, is_flying=False)
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + REFLIGHT


def test_the_cutting_drones_planned_path_is_not_checked_but_its_position_is():
    # The segment lies on drone-1's planned path, so the whole-path check could
    # never pass; the offer goes out because drone-1's path is left out of it.
    fleet = released()
    lanes = fleet.coord.lanes("drone-2")
    assert lanes[-3:] == REFLIGHT
    with fleet.coord._lock:
        assert "of drone-1" in fleet.coord._too_close("drone-2", lanes, {})
        assert fleet.coord._too_close("drone-2", lanes, {}, flown=("drone-1", len(LANES))) is None
        assert fleet.coord._near_cutter_locked("drone-2", lanes, "drone-1") is None


def test_lanes_the_cutting_drone_is_given_after_it_finished_are_still_checked():
    fleet = released()
    lanes = fleet.coord.lanes("drone-2")
    # Say drone-1 were also given a lost drone's lanes that run over the shadow at x = 100.
    more = list(LANES) + [{"direction": "left", "distance_cm": 100}, {"direction": "forward", "distance_cm": 300}]
    with fleet.coord._lock:
        assert fleet.coord._too_close("drone-2", lanes, {"drone-1": more}, flown=("drone-1", len(LANES))) == (
            "come within 0 cm of drone-1, below the 100 cm gap")
        assert fleet.coord._too_close("drone-2", lanes, {}, flown=("drone-1", len(LANES))) is None


def test_a_cutter_at_the_far_end_of_its_demo_zone_blocks_the_transit_until_it_lands():
    # Design section 3.4, "Risks": the demo plan ends at (200, 400), on drone-2's transit.
    fleet = Fleet(lanes=DEMO)
    fleet.done("drone-2", rel_x_cm=200, rel_y_cm=400)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1", rel_x_cm=200, rel_y_cm=400)
    assert len(fleet.assigns("drone-2")) == 1
    assert {s["status"] for s in fleet.segments()} == {"waiting"}
    fleet.done("drone-1", rel_x_cm=200, rel_y_cm=400, is_flying=False)
    assert len(fleet.assigns("drone-2")) == 2


def test_other_drones_paths_are_still_checked():
    # drone-3 (at x = 700) would cross drone-2's zone on its transit; drone-2 is the only one that fits.
    fleet = Fleet(3, origins={"drone-3": 700}, edge_kwargs={"drone-2": {"auto_ack": False}})
    fleet.done("drone-2")
    fleet.done("drone-3")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    assert len(fleet.assigns("drone-2")) == 2 and len(fleet.assigns("drone-3")) == 1
    # drone-2 refuses: drone-3 is still too close to drone-2's path, so it fails with both reasons.
    fleet.edges["drone-2"].reply({"type": "nack", "for": "assign_lanes", "drone_id": "drone-2",
                                  "reason": "geofence", "lanes_total": len(fleet.assigns("drone-2")[-1]["lanes"])})
    (failed,) = fleet.reflight_events("reflight_failed")
    assert "refused by drone-2 (geofence)" in failed["detail"]
    assert "drone-3 would come within" in failed["detail"] and "of drone-2" in failed["detail"]
    assert len(fleet.assigns("drone-3")) == 1


# ---------------------------------------------------------------------------
# Geofence
# ---------------------------------------------------------------------------


def test_a_segment_inside_the_receivers_fence_is_offered():
    # The demo placement: the cut at (0, 250) is 350 cm from drone-2's takeoff, inside a 450 cm fence.
    fleet = released(geofence_cm=450)
    assert fleet.assigns("drone-2")[-1]["lanes"][-3:] == REFLIGHT


def test_a_segment_outside_the_receivers_fence_fails_with_the_geofence_text():
    fleet = released(geofence_cm=450, origins={"drone-2": 500})
    assert len(fleet.assigns("drone-2")) == 1
    (failed,) = fleet.reflight_events()
    assert failed["event"] == "reflight_failed" and failed["receiver"] is None
    assert "outside the geofence of drone-2" in failed["detail"]
    segments = fleet.segments()
    assert {s["status"] for s in segments} == {"failed"}
    assert {s["reason"] for s in segments} == {"outside the geofence of drone-2"}


# ---------------------------------------------------------------------------
# Ack, nack, nobody
# ---------------------------------------------------------------------------


def nack(fleet, rid, reason):
    fleet.edges[rid].reply({"type": "nack", "for": "assign_lanes", "drone_id": rid, "reason": reason,
                            "lanes_total": len(fleet.assigns(rid)[-1]["lanes"])})


def test_a_nack_moves_the_offer_to_the_next_eligible_drone():
    # drone-3, on the left, is nearer to the segment and is asked first.
    fleet = Fleet(3, edge_kwargs={"drone-3": {"auto_ack": False}})
    fleet.done("drone-2")
    fleet.done("drone-3")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    assert len(fleet.assigns("drone-3")) == 2 and len(fleet.assigns("drone-2")) == 1
    nack(fleet, "drone-3", "geofence")
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(LANES) + TRANSIT + REFLIGHT
    # drone-3 keeps its own lanes.
    assert fleet.coord.lanes("drone-3") == list(LANES)
    assert [(e["event"], e["receiver"]) for e in fleet.reflight_events()] == [
        ("reflight_offered", "drone-3"), ("reflight_offered", "drone-2"), ("reflight_accepted", "drone-2"),
    ]
    assert fleet.coord.status()["drone-3"]["reflights"]["offered"] == 1


@pytest.mark.parametrize("reason", ["busy", "landing"])
def test_a_transient_nack_defers_the_offer_instead_of_failing(reason):
    fleet = Fleet(edge_kwargs={"drone-2": {"auto_ack": False}})
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    assert len(fleet.assigns("drone-2")) == 2
    nack(fleet, "drone-2", reason)
    assert fleet.reflight_events("reflight_failed") == []
    assert {s["status"] for s in fleet.segments()} == {"waiting"}
    assert fleet.coord.lanes("drone-2") == list(LANES)
    # Still taking off or landing: the refusal is not over.
    fleet.done("drone-2", state="landing" if reason == "busy" else "taking_off")
    assert len(fleet.assigns("drone-2")) == 2
    if reason == "busy":
        fleet.done("drone-2", state="flying_lanes")
        offer = fleet.assigns("drone-2")[-1]
        assert len(fleet.assigns("drone-2")) == 3 and offer["lanes"][-3:] == REFLIGHT
    else:
        # Over once it is idle, but a drone on the ground cannot re-fly anything.
        fleet.done("drone-2", state="idle", is_flying=False)
        (failed,) = fleet.reflight_events("reflight_failed")
        assert "drone-2 is not flying" in failed["detail"]


def test_no_drone_left_fails_with_the_failure_reason_and_is_never_offered_again():
    fleet = Fleet(edge_kwargs={"drone-2": {"auto_ack": False}})
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    nack(fleet, "drone-2", "geofence")
    (failed,) = fleet.reflight_events("reflight_failed")
    assert "refused by drone-2 (geofence)" in failed["detail"]
    assert {s["reason"] for s in fleet.segments()} == {"refused by drone-2 (geofence)"}
    for _ in range(3):
        fleet.done("drone-1")
        fleet.done("drone-2")
    assert len(fleet.assigns("drone-2")) == 2
    assert len(fleet.reflight_events()) == 2  # offered, failed


def test_a_failed_send_counts_as_a_refusal():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)

    def broken(msg):
        raise ConnectionError("link down")

    fleet.edges["drone-2"].send = broken
    fleet.done("drone-1")
    (failed,) = fleet.reflight_events("reflight_failed")
    assert "refused by drone-2 (not reachable)" in failed["detail"]


def test_a_single_drone_mission_has_nobody_to_offer_to():
    fleet = Fleet(1)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    (failed,) = fleet.reflight_events()
    assert failed["event"] == "reflight_failed" and "no drone left to take them" in failed["detail"]


# ---------------------------------------------------------------------------
# Who is eligible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fields, why", [
    ({"obstacle_sensing": "off"}, "drone-2 senses obstacles off"),
    ({"obstacle_sensing": "forward_only"}, "drone-2 senses obstacles forward_only"),
    ({"obstacle_sensing": None}, "drone-2 reports no obstacle sensing"),
    ({"obstacle_sensor_ok": False}, "drone-2 reports its obstacle sensor is not ok"),
    ({"battery": 29}, "drone-2 has battery 29%, below 30%"),
    ({"drone_link_ok": False}, "drone-2 cannot reach its drone"),
    ({"is_flying": False}, "drone-2 is not flying"),
])
def test_an_ineligible_drone_never_receives_a_segment(fields, why):
    fleet = Fleet()
    fleet.done("drone-2", **fields)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    assert len(fleet.assigns("drone-2")) == 1
    (failed,) = fleet.reflight_events()
    assert failed["event"] == "reflight_failed"
    assert failed["detail"].endswith(f"not re-flown: no drone can re-fly it: {why}")


def test_stale_receiver_telemetry_excludes_it():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.clock.advance(coordinator_module.TELEMETRY_FRESH_S + 0.5)
    fleet.done("drone-1")
    assert len(fleet.assigns("drone-2")) == 1
    (failed,) = fleet.reflight_events()
    assert "drone-2 has no fresh telemetry" in failed["detail"]


def test_a_drone_held_for_manual_control_is_not_a_receiver():
    fleet = Fleet(retask_excluded=lambda rid: rid == "drone-2")
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    (failed,) = fleet.reflight_events()
    assert "drone-2 is held for manual control" in failed["detail"]
    assert len(fleet.assigns("drone-2")) == 1


def test_reflight_receivers_lists_the_eligible_drones():
    fleet = Fleet(3)
    fleet.done("drone-2")
    fleet.done("drone-3", obstacle_sensing="off")
    with fleet.coord._lock:
        assert fleet.coord._reflight_receivers("drone-1") == ["drone-2"]
        assert fleet.coord._reflight_receivers("drone-2") == []


# ---------------------------------------------------------------------------
# Which cuts are offered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason, offered", [
    (CUT_OBSTACLE, True),
    (CUT_PERSON, True),
    (CUT_SENSOR_STALE, True),
    (CUT_SENSOR_DOWN, False),
    (CUT_AUTOPILOT, False),
])
def test_only_obstacle_person_and_sensor_stale_cuts_are_offered(reason, offered):
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], reason, lane_index=2)
    statuses = {s.get("status") for s in fleet.segments()}
    assert statuses == ({"waiting"} if offered else {None})
    fleet.done("drone-1", is_flying=False)
    assert (len(fleet.assigns("drone-2")) == 2) is offered


def test_shadows_ride_with_their_cut_and_are_never_offered_alone():
    fleet = released()
    with fleet.coord._lock:
        (chunk,) = fleet.coord._reflights
    assert chunk.kind == "reflight" and chunk.lost_id == "drone-1"
    assert chunk.first_index == 0 and chunk.cut_seq == 1
    assert [(s.lane_index, s.reason) for s in chunk.segments] == [(0, CUT_OBSTACLE), (2, SEGMENT_SKIPPED)]


def test_a_cut_reported_before_start_is_only_reported():
    clock = FakeClock()
    edges = {rid: FakeEdge(rid, clock, 0.0) for rid in ("drone-1", "drone-2")}
    coord = FleetCoordinator(edges, area_cm=(600, 400), strip_cm=100, clock=clock)
    coord.plan()
    edges["drone-1"].telemetry(is_flying=True, obstacle_seq=1, obstacle_cuts=[0, 100],
                               obstacle_cuts_total=1, obstacle_cut_reasons=CUT_OBSTACLE)
    assert coord._reflights == []
    assert all("status" not in u for u in coord.current_plan()["uncovered"])


def test_at_most_four_cuts_per_drone_per_plan_are_offered(caplog):
    zigzag = tuple(lane for _ in range(5) for lane in (
        {"direction": "forward", "distance_cm": 400}, {"direction": "right", "distance_cm": 20}))
    fleet = Fleet(lanes=zigzag)
    cuts = [(2 * k, 100) for k in range(5)]
    with caplog.at_level(logging.WARNING, logger=coordinator_module.logger.name):
        fleet.cut("drone-1", cuts, ",".join([CUT_OBSTACLE] * 5), lane_index=9)
    assert MAX_REFLIGHTS_PER_DRONE == 4
    by_cut = {}
    for s in fleet.segments():
        by_cut.setdefault(s["cut_seq"], set()).add(s.get("status"))
    assert by_cut == {1: {"waiting"}, 2: {"waiting"}, 3: {"waiting"}, 4: {"waiting"}, 5: {None}}
    assert any("only reported" in r.getMessage() for r in caplog.records)
    # A later message listing the same cuts takes up nothing more.
    fleet.cut("drone-1", cuts, ",".join([CUT_OBSTACLE] * 5), lane_index=9)
    assert len(fleet.coord._reflights) == 4


def test_the_cap_counts_per_cutting_drone():
    fleet = Fleet(3)
    reasons = ",".join([CUT_OBSTACLE] * 2)
    fleet.cut("drone-1", [(0, 100), (2, 50)], reasons, lane_index=3)
    fleet.cut("drone-2", [(0, 100), (2, 50)], reasons, lane_index=3)
    assert sorted(c.lost_id for c in fleet.coord._reflights) == ["drone-1", "drone-1", "drone-2", "drone-2"]


def test_a_later_cut_reshapes_a_waiting_cuts_shadows():
    lanes = (
        {"direction": "forward", "distance_cm": 400},
        {"direction": "right", "distance_cm": 50},
        {"direction": "forward", "distance_cm": 400},
        {"direction": "right", "distance_cm": 50},
        {"direction": "back", "distance_cm": 400},
    )
    fleet = Fleet(lanes=lanes)
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    with fleet.coord._lock:
        (first,) = fleet.coord._reflights
    # 150 cm short on the forward axis: the back lane is predicted shortened by 150 cm.
    assert [(s.lane_index, s.reason, s.length_cm) for s in first.segments] == [
        (0, CUT_OBSTACLE, 150), (4, SEGMENT_SKIPPED, 150)]
    # Lane 2 is then cut 100 cm in: 450 cm short, the back lane is skipped whole, for the second cut.
    fleet.cut("drone-1", [(0, 250), (2, 100)], "obstacle,obstacle", lane_index=4)
    with fleet.coord._lock:
        first, second = fleet.coord._reflights
    assert [(s.lane_index, s.reason, s.length_cm) for s in first.segments] == [(0, CUT_OBSTACLE, 150)]
    assert [(s.lane_index, s.reason, s.length_cm) for s in second.segments] == [
        (2, CUT_OBSTACLE, 300), (4, SEGMENT_SKIPPED, 400)]


# ---------------------------------------------------------------------------
# Too short
# ---------------------------------------------------------------------------


def test_a_segment_under_twenty_cm_is_reported_too_short_and_the_rest_is_offered():
    fleet = Fleet(lanes=DEMO)
    fleet.done("drone-2")
    # Cut 10 cm into lane 0: the last forward lane is shortened by 10 cm.
    fleet.cut("drone-1", [(0, 10)], CUT_OBSTACLE, lane_index=4)
    fleet.done("drone-1", is_flying=False)
    offer = fleet.assigns("drone-2")[-1]
    # Lane 0 back 390 from (0, 400) to (0, 10); the transit to (100, 0) is 10 cm
    # back (an overshoot and return, as every transit under 20 cm is flown) and
    # 100 cm right; then lane 2, skipped whole, forward 400.
    assert offer["lanes"][-5:] == [
        {"direction": "back", "distance_cm": 390},
        {"direction": "back", "distance_cm": 30},
        {"direction": "forward", "distance_cm": 20},
        {"direction": "right", "distance_cm": 100},
        {"direction": "forward", "distance_cm": 400},
    ]
    short = [s for s in fleet.segments() if s["index"] == 4]
    assert [(s["status"], s["reason"], s["distance_cm"]) for s in short] == [
        (SEGMENT_TOO_SHORT, SEGMENT_TOO_SHORT, 10)]
    offered = [s for s in fleet.segments() if s["index"] != 4]
    assert {s["status"] for s in offered} == {"acked"}
    (accepted,) = fleet.reflight_events("reflight_accepted")
    assert [lane["index"] for lane in accepted["lanes"]] == [0, 2]


def test_a_cut_whose_segments_are_all_too_short_fails_too_short():
    fleet = Fleet()
    fleet.done("drone-2")
    # Cut 10 cm before the end of lane 0: 10 cm left, and a 10 cm shadow on lane 2.
    fleet.cut("drone-1", [(0, 390)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1")
    assert len(fleet.assigns("drone-2")) == 1
    (failed,) = fleet.reflight_events()
    assert failed["event"] == "reflight_failed" and failed["detail"].endswith(SEGMENT_TOO_SHORT)
    assert [lane["distance_cm"] for lane in failed["lanes"]] == [10, 10]
    assert {(s["status"], s["reason"]) for s in fleet.segments()} == {(SEGMENT_TOO_SHORT, SEGMENT_TOO_SHORT)}


# ---------------------------------------------------------------------------
# Outcome from the receiver's telemetry
# ---------------------------------------------------------------------------

# In drone-2's lanes: its own 0-3, the transit 4-6, the re-flight 7-9.
APPENDED_END = len(LANES) + len(TRANSIT) + len(REFLIGHT)


def test_the_receiver_passing_the_appended_lanes_marks_it_reflown_once():
    fleet = released()
    fleet.tel("drone-2", lane_index=8)
    assert {s["status"] for s in fleet.segments()} == {"flying"}
    fleet.done("drone-2", lane_index=APPENDED_END, lanes_total=APPENDED_END)
    fleet.done("drone-2", lane_index=APPENDED_END, lanes_total=APPENDED_END)
    (done,) = fleet.reflight_events("reflight_done")
    assert done["receiver"] == "drone-2" and done["cut_seq"] == 1 and len(done["lanes"]) == 2
    # Re-flown segments leave the uncovered list.
    assert fleet.segments() == []
    status = fleet.coord.status()
    assert status["drone-2"]["reflights"] == {"offered": 1, "reflown": 1, "blocked_again": 0}
    assert status["drone-1"]["uncovered_cm"] == 0
    assert [e["event"] for e in fleet.reflight_events()] == [
        "reflight_offered", "reflight_accepted", "reflight_done"]


def test_a_receiver_cut_on_the_segment_marks_it_blocked_again_once():
    fleet = released()
    # drone-2 stops 50 cm into "back 150" (its lane 7), at (0, 350).
    fleet.cut("drone-2", [(7, 50)], CUT_OBSTACLE, lane_index=8)
    fleet.cut("drone-2", [(7, 50)], CUT_OBSTACLE, lane_index=9)
    (blocked,) = fleet.reflight_events("reflight_blocked")
    assert blocked["drone_id"] == "drone-1" and blocked["receiver"] == "drone-2" and blocked["cut_seq"] == 1
    # The part flown, (0, 400) to (0, 350), is covered; the rest of that lane is
    # not, and the shadow it would have flown after it is reported whole.
    rest = fleet.segments()
    assert [(s["index"], ends(s), s["reason"], s["status"]) for s in rest] == [
        (0, (0, 350, 0, 250), "blocked again by drone-2", "blocked_again"),
        (2, (100, 250, 100, 400), "blocked again by drone-2", "blocked_again"),
    ]
    assert [lane["distance_cm"] for lane in blocked["lanes"]] == [100, 150]
    assert "250 cm not searched" in blocked["detail"]
    # Nothing of it is listed again under drone-2, and it is never offered again.
    assert fleet.segments("drone-2") == []
    assert len(fleet.coord._reflights) == 1
    status = fleet.coord.status()
    assert status["drone-2"]["reflights"] == {"offered": 1, "reflown": 0, "blocked_again": 1}
    assert status["drone-1"]["uncovered_cm"] == 250
    fleet.done("drone-2", lane_index=APPENDED_END, lanes_total=APPENDED_END)
    assert fleet.reflight_events("reflight_done") == []
    assert [e["event"] for e in fleet.reflight_events()] == [
        "reflight_offered", "reflight_accepted", "reflight_blocked"]


def test_a_receiver_cut_on_the_transit_counts_as_blocked_again():
    fleet = released()
    # Stopped 100 cm into the transit's forward 400 (its lane 4).
    fleet.cut("drone-2", [(4, 100)], CUT_PERSON, lane_index=5)
    (blocked,) = fleet.reflight_events("reflight_blocked")
    # Nothing of the segments was flown: both are reported whole.
    assert [(s["index"], ends(s)) for s in fleet.segments()] == [
        (0, (0, 250, 0, 400)), (2, (100, 250, 100, 400))]
    assert [lane["distance_cm"] for lane in blocked["lanes"]] == [150, 150]
    assert len(fleet.coord._reflights) == 1


def test_a_receiver_cut_on_its_own_lanes_is_offered_as_its_own_cut():
    fleet = Fleet()
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.cut("drone-2", [(2, 100)], CUT_OBSTACLE, lane_index=3)
    assert sorted(c.lost_id for c in fleet.coord._reflights) == ["drone-1", "drone-2"]


def test_a_receiver_whose_sensor_ends_its_lanes_before_the_segment_fails_it():
    fleet = released()
    fleet.cut("drone-2", [(2, 100)], CUT_SENSOR_DOWN, lane_index=APPENDED_END)
    assert fleet.reflight_events("reflight_done") == []
    (failed,) = fleet.reflight_events("reflight_failed")
    assert failed["receiver"] == "drone-2" and "obstacle sensor down" in failed["detail"]


# ---------------------------------------------------------------------------
# Lost drones
# ---------------------------------------------------------------------------


def test_the_receiver_lost_before_the_appended_lanes_fails_the_chunk_and_its_lanes_are_not_re_tasked():
    fleet = released(3, retask_separation=False, origins={"drone-3": 700})
    assert fleet.coord.lanes("drone-2")[-3:] == REFLIGHT
    fleet.tel("drone-2", lane_index=2)
    fleet.coord.on_drone_lost("drone-2", wait_s=0)
    (failed,) = fleet.reflight_events("reflight_failed")
    assert failed["receiver"] == "drone-2" and failed["detail"].endswith("receiver lost")
    assert {(s["status"], s["reason"]) for s in fleet.segments()} == {("failed", "receiver lost")}
    # Only drone-2's own unflown lanes 2 and 3 are re-tasked, never the transit or the segment.
    (retask,) = fleet.coord._retasks
    assert [entry["index"] for chunk in retask.chunks for entry in chunk.lane_entries()] == [2, 3]
    for rid in ("drone-1", "drone-3"):
        for offer in fleet.assigns(rid)[1:]:
            assert offer["lanes"][-3:] != REFLIGHT


def test_the_receiver_lost_while_re_flying_leaves_nothing_to_re_task():
    fleet = released()
    fleet.tel("drone-2", lane_index=8)
    assert fleet.coord.on_drone_lost("drone-2", wait_s=0) == {}
    assert fleet.coord._retasks == []
    assert fleet.reflight_events("reflight_failed")[0]["detail"].endswith("receiver lost")
    assert len(fleet.assigns("drone-1")) == 1


def test_lanes_after_a_re_flight_are_still_re_tasked_when_the_receiver_is_lost():
    fleet = released(3, retask_separation=False, origins={"drone-3": 700})
    # drone-3 is lost first and some of its lanes go to drone-2, after the re-flight.
    fleet.tel("drone-3", lane_index=0)
    fleet.coord.on_drone_lost("drone-3", wait_s=0)
    lanes = fleet.coord.lanes("drone-2")
    assert lanes[:APPENDED_END] == list(LANES) + TRANSIT + REFLIGHT and len(lanes) > APPENDED_END
    fleet.tel("drone-2", lane_index=2)
    fleet.coord.on_drone_lost("drone-2", wait_s=0)
    retask = fleet.coord._retasks[-1]
    indexes = [entry["index"] for chunk in retask.chunks for entry in chunk.lane_entries()]
    assert indexes == [2, 3] + list(range(APPENDED_END, len(lanes)))


def test_the_cutting_drone_lost_later_supersedes_a_shadow_on_a_lane_re_tasked_whole():
    fleet = Fleet()
    fleet.done("drone-2")
    # drone-1 cut lane 0 and is lost on lane 1: lanes 1 to 3, with the shadow on lane 2, are re-tasked whole.
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=1, rel_x_cm=50, rel_y_cm=250)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    (failed,) = fleet.reflight_events("reflight_failed")
    assert failed["detail"].endswith("superseded") and failed["receiver"] is None
    offers = fleet.assigns("drone-2")
    # Only the lost drone's whole lanes went out, never a re-flight.
    assert all(offer["lanes"][-3:] != REFLIGHT for offer in offers)
    assert any(name == "fleet_event" and p.get("event") == "retask" for name, p in fleet.events)
    assert {(s["index"], s["status"], s["reason"]) for s in fleet.segments()} == {(0, "failed", "superseded")}


def test_the_cutting_drone_lost_after_its_segments_releases_the_offer():
    # Separation off: drone-1's last report puts it on drone-2's new path, and this
    # test is about the loss opening the wait, not about the cutting drone's position.
    fleet = Fleet(retask_separation=False)
    fleet.done("drone-2")
    # Lost on its last lane, past lane 2: nothing of the chunk is re-tasked whole.
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=3, rel_x_cm=150, rel_y_cm=0)
    assert len(fleet.assigns("drone-2")) == 1
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert fleet.reflight_events("reflight_failed") == []
    # Lane 3 of drone-1, re-tasked whole, then the re-flight, in one assign_lanes.
    lanes = fleet.assigns("drone-2")[-1]["lanes"]
    assert lanes[-3:] == REFLIGHT and {"direction": "right", "distance_cm": 100} in lanes[len(LANES):-3]
    assert [e["event"] for e in fleet.reflight_events()] == ["reflight_offered", "reflight_accepted"]


# ---------------------------------------------------------------------------
# Plans, events and the lost-drone path
# ---------------------------------------------------------------------------


def test_a_new_plan_drops_the_reflights():
    fleet = released()
    fleet.done("drone-1", is_flying=False)
    fleet.done("drone-2", is_flying=False, lane_index=0)
    fleet.coord.plan()
    assert fleet.coord._reflights == []
    assert fleet.segments() == []


def test_land_all_fails_a_waiting_reflight():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.coord.land_all()
    fleet.done("drone-1")
    (failed,) = fleet.reflight_events("reflight_failed")
    assert failed["detail"].endswith("LAND ALL issued")


def test_one_fleet_event_per_transition():
    fleet = released()
    for _ in range(3):
        fleet.done("drone-1")
        fleet.tel("drone-2", lane_index=5)
    fleet.done("drone-2", lane_index=APPENDED_END, lanes_total=APPENDED_END)
    fleet.done("drone-2", lane_index=APPENDED_END, lanes_total=APPENDED_END)
    assert [e["event"] for e in fleet.reflight_events()] == [
        "reflight_offered", "reflight_accepted", "reflight_done"]
    for event in fleet.reflight_events():
        assert set(event) == {"event", "drone_id", "receiver", "cut_seq", "lanes", "detail"}


def test_an_acked_reflight_is_never_queued_again():
    fleet = released()
    with fleet.coord._lock:
        (chunk,) = fleet.coord._reflights
    assert chunk.status == "acked" and chunk.flown_by == "drone-2"
    fleet.done("drone-1", is_flying=False)
    fleet.tel("drone-2", lane_index=2)
    nack(fleet, "drone-2", "busy")  # an answer to nothing pending changes nothing
    assert chunk.status == "acked" and chunk.flown_by == "drone-2"
    assert len(fleet.assigns("drone-2")) == 2


def test_the_lost_drone_path_keeps_kind_lost():
    fleet = Fleet()
    fleet.tel("drone-1", lane_index=1)
    fleet.done("drone-2")
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    (retask,) = fleet.coord._retasks
    assert {chunk.kind for chunk in retask.chunks} == {"lost"}
    assert fleet.coord._reflights == []
    assert fleet.reflight_events() == []


# ---------------------------------------------------------------------------
# A real receiver edge flies the re-flight
# ---------------------------------------------------------------------------


class SharedClock(ExecutorClock):
    """The executor's fake clock, also readable as ``now`` for ``FakeEdge``."""

    @property
    def now(self):
        return self()


class ExecutorLink:
    """An ``EdgeLink`` backed by a real ``LaneExecutor`` over a fake Tello with a scripted sensor.

    Control messages pass the protocol validation the transports apply and are
    handled synchronously inside ``send``; the executor's answers go straight
    to the coordinator. Telemetry is sent only when the test calls
    ``report()``, because the executor's own threads are not started.
    """

    def __init__(self, drone_id, clock, blocked=()):
        self.on_message = None
        self.sent = []
        self.drone = SteppedTello(auto=True, name=drone_id, battery=90)
        self.drone.connect()
        envelope = SafetyEnvelope(alt_slot_cm=ALT[drone_id], geofence_cm=500, clock=clock)
        self.sensor = FakeObstacleSensor(envelope.pose_snapshot, list(blocked))
        self.executor = LaneExecutor(self.drone, envelope, drone_id, send=self._reply, clock=clock,
                                     logger=QuietLogger(), obstacle_sensor=self.sensor, obstacle_retry_pause_s=0.0)

    def _reply(self, msg):
        if self.on_message is not None:
            self.on_message(msg)

    def send(self, msg):
        fp.validate_message(msg)
        self.sent.append(msg)
        if msg["type"] in LaneExecutor.CONTROL_TYPES:
            self.executor.handle_message(msg)

    def report(self):
        self._reply(self.executor.telemetry())

    def finished(self):
        return wait_for(lambda: not self.executor.is_busy and self.executor.lane_index >= self.executor.lanes_total)


@pytest.fixture
def real_receiver():
    """A started mission: drone-1 scripted, drone-2 a real edge; yields a factory taking drone-2's blocked zones."""
    made = []

    def make(blocked=()):
        clock = SharedClock()
        edge1 = FakeEdge("drone-1", clock, 0.0)
        link2 = ExecutorLink("drone-2", clock, blocked)
        made.append(link2)
        events = []
        coord = FleetCoordinator({"drone-1": edge1, "drone-2": link2}, area_cm=(600, 400), strip_cm=100,
                                 clock=clock, on_event=lambda name, payload: events.append((name, payload)))
        zones = [
            ZoneAssignment(drone_id=rid, x_cm=ORIGINS[rid], y_cm=0, w_cm=200, h_cm=400, alt_cm=ALT[rid],
                           lanes=LANES)
            for rid in ("drone-1", "drone-2")
        ]
        coord.plan_zones(zones, grid_cm=(0, 0, 550, 400), gap_cm=100, strip_cm=100)
        coord.sync(rounds=1, timeout_s=1.0)
        result = coord.start(lead_s=0.5)
        assert result.ok, result.refusals
        clock.set(result.t_ground_s)
        assert link2.finished()
        link2.report()
        # drone-1 cut lane 0 at 250 cm and hovers at the end of its lanes.
        edge1.telemetry(is_flying=True, obstacle_sensing="stepped", obstacle_sensor_ok=True, lane_index=4,
                        lanes_total=4, rel_x_cm=200, rel_y_cm=0, obstacle_seq=1, obstacle_cuts=[0, 250],
                        obstacle_cuts_total=1, obstacle_cut_reasons=CUT_OBSTACLE)
        return coord, link2, events

    yield make
    for link in made:
        link.executor.shutdown(timeout=2.0)


def real_segments(coord):
    return [u for u in coord.current_plan()["uncovered"] if u.get("kind") == "segment"]


def test_a_real_edge_accepts_the_re_flight_and_flies_it_to_reflown(real_receiver):
    coord, link2, events = real_receiver()
    offer = link2.sent[-1]
    assert offer["type"] == "assign_lanes" and offer["lanes"] == list(LANES) + TRANSIT + REFLIGHT
    assert offer["alt_cm"] == 130
    assert link2.finished()
    assert link2.executor.lanes_total == APPENDED_END
    # The edge flew the reversed segment nose-first: it faced back (180) for the back 150.
    assert any(abs(call.heading_deg - 180.0) < 1.0 and call.x_cm == -350 for call in link2.sensor.calls)
    link2.report()
    kinds = [p["event"] for name, p in events if name == "fleet_event" and p["event"].startswith("reflight_")]
    assert kinds == ["reflight_offered", "reflight_accepted", "reflight_done"]
    assert real_segments(coord) == []


def test_a_real_edge_blocked_again_on_the_reversed_segment(real_receiver):
    # Seen from drone-2's takeoff point, drone-1's cut segment runs along x = -350.
    # Facing back toward it, the edge finds it blocked once it is 50 cm in.
    wall = BlockedZone(heading_deg=180.0, x_max=-300, y_max=360)
    coord, link2, events = real_receiver(blocked=[wall])
    assert link2.finished()
    link2.report()
    telemetry = link2.executor.telemetry()
    assert telemetry["obstacle_cuts"] == [7, 50]
    kinds = [p["event"] for name, p in events if name == "fleet_event" and p["event"].startswith("reflight_")]
    assert kinds == ["reflight_offered", "reflight_accepted", "reflight_blocked"]
    assert [(s["drone_id"], s["index"], ends(s), s["status"]) for s in real_segments(coord)] == [
        ("drone-1", 0, (0, 350, 0, 250), "blocked_again"),
        ("drone-1", 2, (100, 250, 100, 400), "blocked_again"),
    ]
    # The edge's own cut is not offered again.
    assert len(coord._reflights) == 1
