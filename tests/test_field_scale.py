"""Field scale: the outdoor envelope profile, field fleet plans, and one end-to-end path.

The end-to-end test runs the real ``FleetCoordinator`` with a field-scale
plan over in-process links to real ``LaneExecutor``s, each flying a
``MavlinkFleetDrone`` on its own simulated autopilot (``tests.support.fake_mavsdk``):
plan, clock sync, a common start, every lane flown at its own altitude slot,
then LAND ALL. No SITL, no hardware.
"""
from __future__ import annotations

import math
import time

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.autopilot import ARDUPILOT, PX4
from flightrisk.drone.coordinator import (
    DEFAULT_ALT_SLOTS_CM,
    DEFAULT_ZONE_GAP_CM,
    FleetCoordinator,
)
from flightrisk.drone.coverage import OPEN_QUAD_4K, nadir_swath_m
from flightrisk.drone.fleet_scale import PI_CAMERA_MODULE_3, ROOM_SCALE, field_scale
from flightrisk.drone.lane_executor import CONTROL_TYPES, LaneExecutor
from flightrisk.drone.lane_plan import parse_lanes, split_lane_moves
from flightrisk.drone.mavlink_fleet import MavlinkFleetDrone, MavlinkFleetOptions
from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink
from flightrisk.drone.safety import (
    DEFAULT_GEOFENCE_CM,
    FIELD_LIMITS,
    ROOM_LIMITS,
    CeilingError,
    SafetyEnvelope,
    limits_for,
    validate_ceiling,
)
from tests.lane_executor_support import QuietLogger
from tests.support.fake_mavsdk import SimVehicle, install


def wait_for(predicate, timeout=30.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Envelope profiles
# ---------------------------------------------------------------------------


def test_room_profile_is_the_tello_envelope_unchanged():
    envelope = SafetyEnvelope()
    assert envelope.limits is ROOM_LIMITS
    assert envelope.geofence_cm == DEFAULT_GEOFENCE_CM
    assert limits_for("room") is ROOM_LIMITS and limits_for(None) is ROOM_LIMITS and limits_for("moon") is ROOM_LIMITS
    assert limits_for("field") is FIELD_LIMITS
    assert envelope.check_move("forward", 500) and not envelope.check_move("forward", 501)
    with pytest.raises(ValueError, match="altitude slot must be within 20-500 cm"):
        SafetyEnvelope(alt_slot_cm=600)
    with pytest.raises(ValueError, match="ceiling height must be within 150-1000 cm"):
        validate_ceiling(1200, 60)
    assert split_lane_moves(1200) == [400, 400, 400]


def test_field_profile_allows_metre_scale_and_stays_conservative():
    envelope = SafetyEnvelope.for_field()
    assert envelope.limits is FIELD_LIMITS
    assert (envelope.alt_slot_cm, envelope.ceiling_cm, envelope.ceiling_margin_cm, envelope.geofence_cm) == (
        1500, 3000, 500, 3000)
    assert envelope.max_alt_slot_cm == 2500
    assert envelope.check_move("forward", 2500)
    assert not envelope.check_move("forward", 3100)  # leaves the 30 m fence
    assert not envelope.check_move("up", 100)  # lanes never change altitude
    assert envelope.slot_move(500) == ("up", 1000)
    with pytest.raises(CeilingError):
        envelope.set_alt_slot(2600)
    with pytest.raises(ValueError, match="ceiling height must be within 1000-12000 cm"):
        SafetyEnvelope.for_field(ceiling_cm=12500)
    with pytest.raises(ValueError, match="geofence_cm must be at most 50000 cm"):
        SafetyEnvelope.for_field(geofence_cm=60000)
    with pytest.raises(ValueError, match="ceiling margin must be at least 300 cm"):
        SafetyEnvelope.for_field(ceiling_margin_cm=100)
    assert split_lane_moves(12000, FIELD_LIMITS.min_move_cm, FIELD_LIMITS.max_move_cm) == [4000, 4000, 4000]
    assert parse_lanes([{"direction": "forward", "distance_cm": 4000}]) == [("forward", 4000)]


# ---------------------------------------------------------------------------
# Field plans
# ---------------------------------------------------------------------------


def test_room_scale_matches_the_coordinator_defaults():
    assert ROOM_SCALE.alt_slots_cm == tuple(DEFAULT_ALT_SLOTS_CM)
    assert ROOM_SCALE.zone_gap_cm == DEFAULT_ZONE_GAP_CM
    assert ROOM_SCALE.geofence_cm == DEFAULT_GEOFENCE_CM
    assert ROOM_SCALE.coordinator_kwargs()["area_cm"] == (400, 400)


def test_field_lanes_cover_the_whole_area():
    """N1: lanes inset by half a swath, the count rounded up, so the zone is seen edge to edge."""
    scale = field_scale(2, area_m=(70, 30), alt_slots_m=(15, 20), overlap=0.2)
    swath = nadir_swath_m(PI_CAMERA_MODULE_3, 15.0)
    assert swath == pytest.approx(2 * 15 * math.tan(math.radians(33)))
    assert scale.swath_cm == pytest.approx(swath * 100)
    assert scale.strip_cm <= swath * 0.8 * 100  # at least the requested overlap
    assert scale.alt_slots_cm == (1500, 2000)
    assert scale.coverage_fraction == pytest.approx(1.0, abs=0.001)  # centimetre rounding only
    assert scale.coverage_note == "the lanes cover the whole area at the lowest slot"
    settings = scale.edge_settings(2)
    # Each drone takes off half a swath inside its 35 m zone.
    assert settings[0]["zone_x_m"] == pytest.approx(swath / 2)
    assert settings[1]["zone_x_m"] == pytest.approx(35 + swath / 2)
    # The coordinator lays exactly the lanes that cover the zone, with the zones 35 m apart.
    zone_c = scale.zone_width_cm(2)
    assert zone_c // scale.strip_cm == 2
    assert zone_c + scale.zone_gap_cm == 3500
    # Lane span 35 - swath across, 30 m deep: the fence reaches the far corner plus 5 m.
    assert scale.geofence_cm == pytest.approx(3500)
    # A wider lens at the same height spaces lanes further apart.
    assert field_scale(1, camera=OPEN_QUAD_4K, area_m=(200, 60)).strip_cm > field_scale(1, area_m=(200, 60)).strip_cm


def test_the_sitl_plan_that_left_holes_is_now_covered():
    """The 24 m SITL zone used to be flown at x = 0 and 10.4 m with a 13 m swath: about 30% unseen."""
    scale = field_scale(1, area_m=(24, 20), alt_slots_m=(10,))
    assert scale.coverage_fraction == pytest.approx(1.0, abs=0.001)  # centimetre rounding only
    assert scale.zone_width_cm(1) // scale.strip_cm == 3


def test_gaps_between_zones_are_reported_not_refused():
    scale = field_scale(2, area_m=(70, 30), alt_slots_m=(15, 20), zone_gap_m=10)
    assert scale.coverage_fraction == pytest.approx(60 / 70, abs=0.01)
    assert "not seen: 30.0-40.0 m" in scale.coverage_note


def test_every_drone_needs_its_own_slot():
    with pytest.raises(ValueError, match="3 drones need 3 altitude slots"):
        field_scale(3, area_m=(150, 60), alt_slots_m=(15, 20))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"alt_slots_m": (15, 18)}, "at least 5 m apart"),
        ({"alt_slots_m": (8, 15)}, "outside 10-120 m"),
        ({"alt_slots_m": (130, 15)}, "outside 10-120 m"),
        ({"area_m": (600, 100)}, "at most 500 m"),
        ({"geofence_m": 10.0}, "smaller than a drone's lanes"),
        ({"overlap": 1.0}, "overlap"),
        ({"alt_slots_m": ()}, "at least one slot"),
    ],
)
def test_field_plans_refuse_unsafe_or_impossible_settings(kwargs, message):
    with pytest.raises(ValueError, match=message):
        field_scale(2, **kwargs)


# ---------------------------------------------------------------------------
# End to end: field plan, two MAVLink drones, the real coordinator and executors
# ---------------------------------------------------------------------------


class FieldEdge:
    """In-process ``EdgeLink``: a real LaneExecutor on a MavlinkFleetDrone on a simulated autopilot."""

    def __init__(self, monkeypatch, drone_id: str, autopilot: str, slot_cm: float, geofence_cm: float):
        self.drone_id = drone_id
        self.on_message = None
        self.replies: list[dict] = []
        self.vehicle = SimVehicle(autopilot, time_scale=20.0)
        install(monkeypatch, self.vehicle)
        self.envelope = SafetyEnvelope.for_field(alt_slot_cm=slot_cm, geofence_cm=geofence_cm)
        self.drone = MavlinkFleetDrone(drone_id, MavsdkAutopilotLink("tcpout://127.0.0.1:1"),
                                       MavlinkFleetOptions(frame_heading_deg=0.0))
        self.drone.bind_envelope(self.envelope)
        assert self.drone.connect()
        self.executor = LaneExecutor(self.drone, self.envelope, drone_id, send=self._reply, logger=QuietLogger())
        self.executor.start()

    def _reply(self, msg: dict) -> None:
        fp.validate_message(msg)
        self.replies.append(msg)
        if self.on_message is not None:
            self.on_message(msg)

    def send(self, msg: dict) -> None:
        fp.validate_message(msg)
        self.executor.note_ground_message()
        if msg["type"] in CONTROL_TYPES:
            self.executor.handle_message(msg, recv_t=self.executor.now())

    def close(self) -> None:
        self.executor.shutdown(timeout=5.0)
        self.drone.disconnect()


def test_two_mavlink_drones_fly_a_field_plan_end_to_end(monkeypatch):
    scale = field_scale(2, area_m=(70, 30), alt_slots_m=(15, 20))
    settings = scale.edge_settings(2)
    edges = {
        "drone-1": FieldEdge(monkeypatch, "drone-1", ARDUPILOT, settings[0]["alt_slot_m"] * 100,
                             settings[0]["geofence_m"] * 100),
        "drone-2": FieldEdge(monkeypatch, "drone-2", PX4, settings[1]["alt_slot_m"] * 100,
                             settings[1]["geofence_m"] * 100),
    }
    try:
        for edge in edges.values():
            assert wait_for(lambda e=edge: e.drone.link_status().ok), edge.drone.link_status()
        coord = FleetCoordinator(edges, retask_wait_s=1.0, **scale.coordinator_kwargs())
        plan = coord.plan()
        for edge in edges.values():
            acks = [m for m in edge.replies if m["type"] == "ack" and m.get("for") == "assign_lanes"]
            assert acks, edge.replies
        assert [zone.alt_cm for zone in plan.zones] == [1500, 2000]
        coord.sync(rounds=2)
        result = coord.start(lead_s=0.2)
        assert result.ok, result.refusals
        for edge in edges.values():
            lanes = edge.executor.lanes_total
            assert lanes > 0
            assert wait_for(lambda e=edge, n=lanes: e.executor.lane_index == n and not e.executor.is_busy, 60.0), (
                edge.drone_id, edge.executor.lane_index, lanes)
        # Each drone ends where its lanes end, relative to its own takeoff point.
        vectors = {"forward": (0, 1), "back": (0, -1), "right": (1, 0), "left": (-1, 0)}
        for zone, slot_m in zip(plan.zones, (15.0, 20.0), strict=True):
            edge = edges[zone.drone_id]
            end_x = sum(vectors[lane["direction"]][0] * lane["distance_cm"] for lane in zone.lanes) / 100
            end_y = sum(vectors[lane["direction"]][1] * lane["distance_cm"] for lane in zone.lanes) / 100
            assert edge.vehicle.alt == pytest.approx(slot_m, abs=0.7), edge.drone_id
            assert edge.vehicle.north == pytest.approx(end_y, abs=1.0), edge.drone_id
            assert edge.vehicle.east == pytest.approx(end_x, abs=1.0), edge.drone_id
        status = coord.status()
        assert set(status) == {"drone-1", "drone-2"}
        coord.land_all()
        for edge in edges.values():
            assert wait_for(lambda e=edge: not e.drone.state.is_flying, 30.0), edge.drone_id
            assert ("land",) in edge.vehicle.commands
    finally:
        for edge in edges.values():
            edge.close()
