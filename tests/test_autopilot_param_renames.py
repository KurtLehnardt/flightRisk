"""ArduCopter 4.7 parameter renames in the failsafe and return-altitude checks.

ArduCopter 4.7 renamed ``RTL_ALT`` (centimetres) to ``RTL_ALT_M`` (metres) and ``SYSID_THISMAV`` to
``MAV_SYSID`` (tag Copter-4.7.3: ArduCopter/mode_rtl.cpp lines 8-15 and 65, ArduCopter/Parameters.cpp
line 40). The checks read the new name first, fall back to the old one, and refuse when neither can be read.
"""

from __future__ import annotations

import math

import pytest

from flightrisk.drone.autopilot import ARDUPILOT, FAILSAFE_PARAMS, PX4, failsafe_problems, rtl_altitude_cm
from tests.support.fake_mavsdk import ARDUPILOT_PARAMS, PX4_PARAMS

#: Copter 4.6 names, as the fake vehicle reports them.
AP46 = dict(ARDUPILOT_PARAMS)
#: The same settings with Copter 4.7 names, as a Copter 4.7.3 SITL reports them.
AP47 = {
    **{k: v for k, v in ARDUPILOT_PARAMS.items() if k not in ("RTL_ALT", "SYSID_THISMAV")},
    "RTL_ALT_M": 15.0,
    "MAV_SYSID": 1,
}
CHECK = {"geofence_cm": 3000, "ceiling_cm": 3000, "slot_cm": 1500}


def reader(params):
    """A fake parameter reader: the value, or None when the parameter does not exist."""
    return params.get


def problems(autopilot, params, **overrides):
    return failsafe_problems(autopilot, reader(params), **{**CHECK, **overrides})


def test_copter_46_names_only_pass_as_before():
    assert "RTL_ALT_M" not in AP46 and "MAV_SYSID" not in AP46
    assert problems(ARDUPILOT, AP46) == []
    assert rtl_altitude_cm(ARDUPILOT, reader(AP46)) == 1500
    too_high = problems(ARDUPILOT, {**AP46, "RTL_ALT": 3000})
    assert any("RTL_ALT return altitude 30 m is above" in p for p in too_high), too_high


def test_copter_47_names_only_now_pass():
    assert "RTL_ALT" not in AP47 and "SYSID_THISMAV" not in AP47
    assert problems(ARDUPILOT, AP47) == []
    assert rtl_altitude_cm(ARDUPILOT, reader(AP47)) == 1500  # 15 m read as 1500 cm


def test_copter_47_return_altitude_above_the_slot_is_refused_in_metres():
    found = problems(ARDUPILOT, {**AP47, "RTL_ALT_M": 30.0})
    assert any("RTL_ALT_M return altitude 30 m is above" in p for p in found), found
    assert problems(ARDUPILOT, {**AP47, "RTL_ALT_M": 15.0}) == []  # exactly the slot passes


def test_copter_47_system_id_is_checked():
    found = problems(ARDUPILOT, {**AP47, "MAV_SYSID": 3})
    assert any("system id is 3" in p for p in found), found


@pytest.mark.parametrize(
    ("new_m", "old_cm", "expected_cm"),
    [
        (10.0, 3000, 1000),  # the new name wins, so the old, higher value is not refused
        (30.0, 1000, 3000),  # the new name wins, so its higher value is refused
    ],
)
def test_both_names_present_the_new_name_wins(new_m, old_cm, expected_cm):
    params = {**AP46, "RTL_ALT_M": new_m, "RTL_ALT": old_cm, "MAV_SYSID": 1}
    assert rtl_altitude_cm(ARDUPILOT, reader(params)) == expected_cm
    found = problems(ARDUPILOT, params)
    refused = [p for p in found if "return altitude" in p]
    if expected_cm > CHECK["slot_cm"]:
        assert refused and refused[0].startswith("RTL_ALT_M return altitude"), found
    else:
        assert found == []


def test_both_system_ids_present_the_new_name_wins():
    found = problems(ARDUPILOT, {**AP46, "MAV_SYSID": 3, "SYSID_THISMAV": 1})
    assert any("system id is 3" in p for p in found), found


@pytest.mark.parametrize("unreadable", [{}, {"RTL_ALT_M": math.nan}, {"RTL_ALT_M": None, "RTL_ALT": math.inf}])
def test_neither_return_altitude_name_readable_is_refused_naming_both(unreadable):
    params = {k: v for k, v in AP46.items() if k != "RTL_ALT"} | unreadable
    found = problems(ARDUPILOT, params)
    assert "cannot read RTL_ALT_M / RTL_ALT (return altitude)" in found, found
    assert rtl_altitude_cm(ARDUPILOT, reader(params)) is None
    # Without a slot the value is still required: unreadable never passes.
    assert "cannot read RTL_ALT_M / RTL_ALT (return altitude)" in problems(ARDUPILOT, params, slot_cm=None)


def test_neither_system_id_name_readable_is_refused_naming_both():
    params = {k: v for k, v in AP47.items() if k != "MAV_SYSID"}
    assert "cannot read MAV_SYSID / SYSID_THISMAV (system id)" in problems(ARDUPILOT, params)


def test_both_names_of_each_rename_are_read():
    names = FAILSAFE_PARAMS[ARDUPILOT]
    for name in ("RTL_ALT_M", "RTL_ALT", "MAV_SYSID", "SYSID_THISMAV"):
        assert name in names
    assert len(names) == len(set(names))


def test_px4_is_unchanged():
    assert FAILSAFE_PARAMS[PX4] == (
        "GF_ACTION", "GF_MAX_HOR_DIST", "GF_MAX_VER_DIST", "COM_LOW_BAT_ACT", "NAV_RCL_ACT", "NAV_DLL_ACT",
        "RTL_RETURN_ALT", "MAV_SYS_ID",
    )
    assert problems(PX4, PX4_PARAMS) == []
    assert rtl_altitude_cm(PX4, reader(PX4_PARAMS)) == 1500
    # ArduPilot names mean nothing on PX4.
    no_rtl = {k: v for k, v in PX4_PARAMS.items() if k != "RTL_RETURN_ALT"} | {"RTL_ALT_M": 10.0, "RTL_ALT": 1000}
    assert "cannot read RTL_RETURN_ALT (return altitude)" in problems(PX4, no_rtl)
    assert rtl_altitude_cm(PX4, reader(no_rtl)) is None
    no_sysid = {k: v for k, v in PX4_PARAMS.items() if k != "MAV_SYS_ID"} | {"MAV_SYSID": 1}
    assert "cannot read MAV_SYS_ID (system id)" in problems(PX4, no_sysid)
    too_high = problems(PX4, {**PX4_PARAMS, "RTL_RETURN_ALT": 30.0})
    assert any("RTL_RETURN_ALT return altitude 30 m is above" in p for p in too_high), too_high


def test_unknown_autopilot_has_no_return_altitude():
    assert rtl_altitude_cm(None, reader(AP47)) is None
    assert rtl_altitude_cm("other", reader(AP47)) is None
