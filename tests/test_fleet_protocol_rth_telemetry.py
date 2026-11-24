"""The ``rth`` telemetry object, ``ap_link_loss`` inside it, and the field-cap headroom (A2 step S7).

``rth`` is one grouped top-level field, like ``detection_profile`` (#211), so
the worst-case telemetry gains one field. The headroom test builds the worst
case with the producer matrix of ``tests/test_telemetry_field_cap.py``,
imported rather than copied, so the count follows the real producers.
"""

from __future__ import annotations

import json

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.fleet_protocol.builders import TelemetryV2
from tests.test_telemetry_field_cap import (
    LinkedFakeTello,
    _GpsTello,
    _NoGpsTello,
    _wire,
)

_FULL_RTH = {
    "state": "rth_returning",
    "reason": "battery_cannot_reach_home",
    "home": {"x_cm": 120.0, "y_cm": -40},
    "distance_cm": 3400.5,
    "uncertainty_cm": 250,
    "reserve_pct": 12.0,
    "threshold_pct": 38.5,
    "countdown_s": 0,
    "cannot_recall": False,
    "landing_area_id": "pad-1",
    "landing_place": 0,
    "ap_link_loss": "land",
}


def _telemetry_args():
    return ("drone-1", 80.0, 120.0, True, 10.0, 20.0, 90.0, 0, 4, 0.1, 1000.0)


def _v2():
    return TelemetryV2(epoch=3, plan_id="plan-1", state="flying_lanes", plan_valid=True)


def _telemetry_with(rth):
    msg = fp.make_telemetry(*_telemetry_args(), v2=_v2())
    msg["rth"] = rth
    return msg


# --- The object -----------------------------------------------------------


def test_the_full_rth_object_is_accepted():
    fp.validate_rth_object(_FULL_RTH)
    assert fp.validate_message(_telemetry_with(_FULL_RTH), proto=2) == "telemetry"


def test_only_the_state_is_required():
    fp.validate_rth_object({"state": "flying"})


def test_every_state_and_reason_is_accepted():
    for state in fp.RTH_STATES:
        fp.validate_rth_object({"state": state})
    for reason in fp.RTH_REASONS:
        fp.validate_rth_object({"state": "rth_landing", "reason": reason})


@pytest.mark.parametrize("action", sorted(fp.AP_LINK_LOSS_ACTIONS))
def test_every_link_loss_action_is_accepted_including_unknown(action):
    fp.validate_rth_object({"state": "flying", "ap_link_loss": action})
    assert fp.validate_message(_telemetry_with({"state": "flying", "ap_link_loss": action}), proto=2) == "telemetry"


def test_ap_link_loss_unknown_is_accepted():
    fp.validate_rth_object({"state": "cannot_be_recalled", "ap_link_loss": "unknown", "cannot_recall": True})


def test_nullable_keys_accept_null():
    fp.validate_rth_object({
        "state": "holding", "reason": None, "home": None, "distance_cm": None, "uncertainty_cm": None,
        "reserve_pct": None, "threshold_pct": None, "countdown_s": None, "landing_area_id": None,
        "landing_place": None,
    })


def test_a_key_this_version_does_not_define_is_ignored():
    fp.validate_rth_object({"state": "flying", "later_key": 1})


@pytest.mark.parametrize("rth", [
    None,
    [],
    "rth_returning",
    {},
    {"state": "returning"},
    {"state": "RTH_RETURNING"},
    {"state": ["flying"]},
    {"state": "flying", "reason": "low_battery"},
    {"state": "flying", "home": [1, 2]},
    {"state": "flying", "home": {"x_cm": 1}},
    {"state": "flying", "home": {"x_cm": 1, "y_cm": 2, "z_cm": 3}},
    {"state": "flying", "home": {"x_cm": 1, "y_cm": fp.MAX_LANE_DISTANCE_CM + 1}},
    {"state": "flying", "distance_cm": -1},
    {"state": "flying", "distance_cm": "12"},
    {"state": "flying", "distance_cm": True},
    {"state": "flying", "uncertainty_cm": float("nan")},
    {"state": "flying", "uncertainty_cm": float("inf")},
    {"state": "flying", "reserve_pct": 101},
    {"state": "flying", "threshold_pct": -0.5},
    {"state": "flying", "countdown_s": -2},
    {"state": "flying", "cannot_recall": 1},
    {"state": "flying", "cannot_recall": None},
    {"state": "flying", "landing_area_id": ""},
    {"state": "flying", "landing_area_id": "pad 1"},
    {"state": "flying", "landing_place": fp.MAX_ROSTER},
    {"state": "flying", "landing_place": 1.0},
    {"state": "flying", "ap_link_loss": "rtl"},
    {"state": "flying", "ap_link_loss": None},
])
def test_malformed_rth_objects_are_refused(rth):
    with pytest.raises(fp.ProtocolError):
        fp.validate_rth_object(rth)
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(_telemetry_with(rth), proto=2)


def test_an_unknown_state_is_refused_by_name():
    with pytest.raises(fp.ProtocolError, match="'state'"):
        fp.validate_message(_telemetry_with({"state": "orbiting"}), proto=2)


def test_the_object_is_within_the_depth_cap():
    msg = _telemetry_with(_FULL_RTH)
    fp.validate_message(msg, proto=2)

    def depth(value):
        if isinstance(value, dict):
            return 1 + max((depth(v) for v in value.values()), default=0)
        if isinstance(value, list):
            return 1 + max((depth(v) for v in value), default=0)
        return 0

    assert depth(msg) <= fp.MAX_DEPTH


def test_no_rth_number_is_in_a_list():
    assert not any(isinstance(value, list) for value in _FULL_RTH.values())
    assert not any(isinstance(value, list) for value in _FULL_RTH["home"].values())


# --- The builders ---------------------------------------------------------


def test_make_rth_object_omits_unset_keys_and_takes_a_home_pair():
    rth = fp.make_rth_object("rth_waiting", home=(5, -6), countdown_s=12.0, ap_link_loss="unknown")
    assert rth == {"state": "rth_waiting", "home": {"x_cm": 5, "y_cm": -6}, "countdown_s": 12.0,
                   "ap_link_loss": "unknown"}
    assert list(rth) == [key for key in fp.RTH_FIELDS if key in rth]


def test_make_rth_object_writes_every_key():
    rth = fp.make_rth_object(**_FULL_RTH)
    assert rth == _FULL_RTH
    assert tuple(rth) == fp.RTH_FIELDS


def test_make_rth_object_refuses_a_bad_value():
    with pytest.raises(fp.ProtocolError):
        fp.make_rth_object("airborne")
    with pytest.raises(fp.ProtocolError):
        fp.make_rth_object("flying", ap_link_loss="hover")


def test_make_telemetry_without_rth_is_byte_identical():
    before_v1 = json.dumps(fp.make_telemetry(*_telemetry_args()), separators=(",", ":"))
    before_v2 = json.dumps(fp.make_telemetry(*_telemetry_args(), v2=_v2()), separators=(",", ":"))
    assert json.dumps(fp.make_telemetry(*_telemetry_args(), rth=None), separators=(",", ":")) == before_v1
    assert json.dumps(fp.make_telemetry(*_telemetry_args(), v2=_v2(), rth=None), separators=(",", ":")) == before_v2
    assert "rth" not in json.loads(before_v1) and "rth" not in json.loads(before_v2)


def test_make_telemetry_with_rth_adds_one_field():
    plain = fp.make_telemetry(*_telemetry_args(), v2=_v2())
    msg = fp.make_telemetry(*_telemetry_args(), v2=_v2(), rth=fp.make_rth_object(**_FULL_RTH))
    assert set(msg) - set(plain) == {"rth"}
    assert msg["rth"] == _FULL_RTH
    assert fp.validate_message(msg, proto=2) == "telemetry"


def test_make_telemetry_refuses_rth_without_v2():
    with pytest.raises(fp.ProtocolError, match="v2"):
        fp.make_telemetry(*_telemetry_args(), rth={"state": "flying"})


def test_make_telemetry_refuses_a_malformed_rth():
    with pytest.raises(fp.ProtocolError):
        fp.make_telemetry(*_telemetry_args(), v2=_v2(), rth={"state": "lost"})


# --- Older peers ----------------------------------------------------------


def test_rth_and_ap_link_loss_are_stripped_for_a_v1_peer():
    msg = fp.make_telemetry(*_telemetry_args(), v2=_v2(), rth=fp.make_rth_object(**_FULL_RTH))
    shaped = fp.shape_for_peer(msg, 1)
    assert "rth" not in shaped
    assert "ap_link_loss" not in json.dumps(shaped)
    assert fp.validate_message(shaped, proto=1) == "telemetry"
    assert fp.shape_for_peer(msg, 2)["rth"] == _FULL_RTH


def test_rth_is_a_v2_field():
    assert "rth" in fp.V2_FIELDS


# --- Field-cap headroom ---------------------------------------------------


def test_the_worst_case_telemetry_with_rth_leaves_headroom_under_the_cap():
    worst_without = max(len(_wire(cls)) for cls in (_GpsTello, _NoGpsTello, LinkedFakeTello))
    worst = 0
    for cls in (_GpsTello, _NoGpsTello, LinkedFakeTello):
        msg = _wire(cls)
        assert "rth" not in msg, "a producer sets rth in A2; update this test to count it once"
        msg["rth"] = fp.make_rth_object(**_FULL_RTH)
        assert fp.validate_message(msg, proto=2) == "telemetry"
        worst = max(worst, len(msg))
    assert worst == worst_without + 1
    assert worst <= fp.MAX_FIELDS - 5, (
        f"worst-case telemetry with rth has {worst} of {fp.MAX_FIELDS} fields; add no field without grouping"
    )
