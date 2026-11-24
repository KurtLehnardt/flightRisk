"""The map plan's area cap follows the fleet's units (A4b-4): twice the smallest published range."""

from __future__ import annotations

import pytest

from flightrisk import ground_params
from flightrisk.dashboard import app as app_module
from flightrisk.drone import plan_edit
from flightrisk.drone.fleet_plan_check import MAX_AREA_SIDE_CM
from flightrisk.drone.plan_edit import PlanSpec
from tests.test_fleet_dashboard import _supervisor

AREA = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}


@pytest.fixture(autouse=True)
def _no_ground_names(monkeypatch):
    for name in ground_params.GROUND_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _units(models):
    units, refused = ground_params.units_for(list(models), models, environ={})
    assert not refused
    return units


def test_a_tello_fleet_is_capped_at_the_room_profiles_area_side():
    assert ground_params.area_cap_cm(_units({"a": "tello", "b": "tello_edu"}).values()) == MAX_AREA_SIDE_CM == 20000


def test_a_fleet_without_a_range_limited_unit_has_no_cap():
    assert ground_params.area_cap_cm(_units({"a": "generic_multirotor"}).values()) is None
    assert ground_params.area_cap_cm([]) is None


def test_a_mixed_fleet_takes_the_range_limited_units_cap():
    units = _units({"a": "tello", "b": "generic_multirotor"})
    assert ground_params.area_cap_cm(units.values()) == MAX_AREA_SIDE_CM


def test_the_map_plan_passes_the_units_cap_to_build_plan(monkeypatch):
    supervisor, _transport, _sio, _clock = _supervisor(("drone-1", "drone-2"))
    seen = []
    real = plan_edit.build_plan

    def spy(*args, **kwargs):
        seen.append(kwargs.get("max_area_side_cm", "default"))
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module.plan_edit, "build_plan", spy)
    supervisor.build_map_plan(PlanSpec.from_payload({"pattern": "parallel_track", "area": dict(AREA)}))
    assert seen == [ground_params.area_cap_cm(supervisor.units.values())] == [MAX_AREA_SIDE_CM]


def test_a_supervisor_without_units_keeps_the_default_cap(monkeypatch):
    supervisor, _transport, _sio, _clock = _supervisor(("drone-1",))
    supervisor.units = None
    assert supervisor._area_cap_kwargs() == {}
