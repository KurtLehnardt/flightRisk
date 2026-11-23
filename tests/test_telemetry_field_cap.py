"""Every telemetry producer stays within the fleet protocol's field cap (``MAX_FIELDS``).

A MAVLink edge with a GPS fix, the drone link, the obstacle fields and the
person cue sends more than 32 fields, which the old cap dropped at the
sender's validation. These tests build the telemetry the way the edge does
(the executor's message, then ``_with_profile_status``) and validate it. The
worst case includes the camera sensor's depth threshold
(``obstacle_min_safe_depth``) and, for a Tello, the temperature check before
its last takeoff (``temp_preflight``); a MAVLink drone answers no ``temp?``,
so its telemetry never has that field.
"""
from __future__ import annotations

import types

import pytest

from flightrisk import edge_device
from flightrisk import fleet_protocol as fp
from flightrisk.drone.obstacle_sensor import TELEMETRY_MIN_SAFE_DEPTH, TELEMETRY_PERSON_CUE
from flightrisk.drone.temp_preflight import TELEMETRY_TEMP, TempLimits, judge_temperature
from tests.test_obstacle_h6_fixes import LinkedFakeTello, _CueSensor, _executor

_CUE = {"person_cue_down": True, "person_detection_age_s": 3.2, "min_safe_depth": 0.77}


class _GpsTello(LinkedFakeTello):
    """A fake drone with the drone link and a GPS fix, like the MAVLink edge's drone."""

    def geo_position(self):
        return (47.0, 8.0)


class _NoGpsTello(LinkedFakeTello):
    def geo_position(self):
        return None


def _runner(profile_fields=True):
    """A runner with every field ``_with_profile_status`` can add."""
    switcher = types.SimpleNamespace(status_fields=lambda: {
        "detection_profile": "fast", "detection_profile_fallback": True, "detection_profile_switchable": True,
        "detection_profile_fallback_reason": "model missing", "detection_profile_requested": "accurate",
    })
    return types.SimpleNamespace(detector_ok=True, camera_ok=True, profile_switcher=switcher if profile_fields else None)


def _wire(drone_cls, *, cue=True, v2=True, profile_fields=True, temp=None):
    """The telemetry message as the edge sends it.

    *temp* (default: only for the Tello, ``LinkedFakeTello``) adds the takeoff temperature check.
    """
    ex = _executor(_CueSensor(_CUE if cue else {"sensor": "fake"}), drone=drone_cls(name="drone-1"))
    ex._v2_telemetry = v2
    ex._obstacle_state, ex._obstacle_latency_ms = "clear", 210.0
    if temp if temp is not None else drone_cls is LinkedFakeTello:
        ex._temp_check = judge_temperature("91~93C", TempLimits())
    sent = []
    edge_device._with_profile_status(sent.append, _runner(profile_fields))(ex.telemetry())
    return sent[0]


def test_mavlink_telemetry_with_gps_and_the_person_cue_is_a_valid_fleet_message():
    # Executor fields alone were 34 with GPS and the cue (33 without the cue), over the old cap of 32.
    ex = _executor(_CueSensor(_CUE), drone=_GpsTello(name="drone-1"))
    ex._v2_telemetry = True
    ex._obstacle_state, ex._obstacle_latency_ms = "clear", 210.0
    msg = ex.telemetry()
    assert {"lat", "lon", TELEMETRY_PERSON_CUE, "drone_link_ok"} <= set(msg)
    assert len(msg) > 32
    assert fp.validate_message(msg, proto=2) == "telemetry"


def test_the_full_wire_message_of_a_mavlink_edge_is_valid():
    msg = _wire(_GpsTello)
    assert {"lat", "lon", TELEMETRY_PERSON_CUE, TELEMETRY_MIN_SAFE_DEPTH, "detector_ok", "camera_ok",
            "detection_profile_requested"} <= set(msg)
    assert fp.validate_message(fp.shape_for_peer(msg, 2), proto=2) == "telemetry"


def test_the_full_wire_message_of_a_tello_edge_after_a_takeoff_check_is_valid():
    msg = _wire(LinkedFakeTello)
    assert {TELEMETRY_TEMP, TELEMETRY_MIN_SAFE_DEPTH, TELEMETRY_PERSON_CUE} <= set(msg)
    assert msg[TELEMETRY_TEMP]["level"] == "warn"
    assert fp.validate_message(fp.shape_for_peer(msg, 2), proto=2) == "telemetry"


@pytest.mark.parametrize("drone_cls", [_GpsTello, _NoGpsTello, LinkedFakeTello])
@pytest.mark.parametrize("cue", [True, False])
@pytest.mark.parametrize("v2", [True, False])
@pytest.mark.parametrize("profile_fields", [True, False])
@pytest.mark.parametrize("temp", [True, False])
def test_every_telemetry_producer_stays_within_the_field_cap(drone_cls, cue, v2, profile_fields, temp):
    msg = _wire(drone_cls, cue=cue, v2=v2, profile_fields=profile_fields, temp=temp)
    assert len(msg) <= fp.MAX_FIELDS
    assert fp.validate_message(msg, proto=2 if v2 else 1) == "telemetry"


def test_the_worst_case_telemetry_leaves_headroom_under_the_cap():
    worst = max(len(_wire(cls)) for cls in (_GpsTello, _NoGpsTello, LinkedFakeTello))
    assert worst <= fp.MAX_FIELDS - 5, f"worst-case telemetry has {worst} of {fp.MAX_FIELDS} fields; add no field without grouping"


def test_the_cap_still_rejects_a_message_over_it():
    msg = _wire(_GpsTello)
    msg.update({f"x{i}": i for i in range(fp.MAX_FIELDS - len(msg) + 1)})
    assert len(msg) == fp.MAX_FIELDS + 1
    with pytest.raises(fp.ProtocolError, match="fields"):
        fp.validate_message(msg, proto=2)
