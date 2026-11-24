"""CAP-4: changing a fake drone's declared capabilities changes its return method and separation.

The rules that read the declarations (the recovery flow and the safety envelope profile) are not edited here.
Only what the fake declares changes.
"""

from dataclasses import replace

from flightrisk.dashboard.recovery import run_confirm_recovery
from flightrisk.drone.controller import Capability
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig
from flightrisk.drone.safety import FIELD_LIMITS, ROOM_LIMITS, limits_for


def _recover(drone: FakeDrone) -> tuple[str, list[str]]:
    drone.connect()
    drone.takeoff()
    outcome = run_confirm_recovery(drone)
    return outcome, [call[0] for call in drone.calls]


def test_default_fake_declares_todays_capabilities():
    caps = FakeDrone().capabilities
    assert caps.flight_scale == "room"
    assert caps.supports(Capability.RETURN_TO_HOME)
    assert (caps.min_move_cm, caps.max_move_cm) == (20, 500)


def test_declaring_no_return_to_home_changes_the_return_method():
    outcome, calls = _recover(FakeDrone())
    assert outcome == "returned_and_landed"
    assert "return_to_home" in calls
    outcome, calls = _recover(FakeDrone(config=FakeDroneConfig(supports_return_to_home=False)))
    assert outcome == "landed_in_place"
    assert "return_to_home" not in calls


def test_declaring_a_field_scale_changes_the_separation():
    room, field = FakeDrone(), FakeDrone()
    field.capabilities = replace(field.capabilities, flight_scale="field")
    room_limits, field_limits = limits_for(room.capabilities.flight_scale), limits_for(field.capabilities.flight_scale)
    assert room_limits is ROOM_LIMITS
    assert field_limits is FIELD_LIMITS
    assert field_limits.default_alt_slot_cm != room_limits.default_alt_slot_cm
    assert field_limits.min_alt_slot_cm != room_limits.min_alt_slot_cm
