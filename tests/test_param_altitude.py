"""Design section 27 item 53, part 1: the regulatory altitude cap rows and an unknown airframe maximum."""

from __future__ import annotations

import ast
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.params.altitude import AIRFRAME_MAX_ID, CAP_ID, CAP_ON_ID, airframe_max_cm, altitude_limit_cm
from flightrisk.params.resolver import LAUNCHER_CONFIRMED, ParamRefused, resolve, resolve_unit
from flightrisk.params.spec import Bound, Confirm, Edit, Source
from flightrisk.params.table import LAUNCHER_CONFIRMED_ONLY, TABLE, UNSET

ROOT = Path(__file__).resolve().parent.parent
G = "generic_multirotor"
BY_ID = {row.id: row for row in TABLE}


ACK = {"value": False, "ack": {"id": "outside_faa_jurisdiction", "text_version": "1",
                               "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "by": "operator"}}


def _values(cap_on=True, cap=12000, airframe=UNSET):
    return {CAP_ON_ID: cap_on, CAP_ID: cap, AIRFRAME_MAX_ID: airframe}


# The helper, every branch.

def test_cap_on_with_unknown_airframe_is_the_cap():
    assert altitude_limit_cm(_values(airframe=UNSET)) == 12000
    assert altitude_limit_cm(_values(airframe=None)) == 12000


def test_cap_on_with_lower_airframe_is_the_airframe():
    assert altitude_limit_cm(_values(airframe=1000)) == 1000


def test_cap_on_with_higher_airframe_is_the_cap():
    assert altitude_limit_cm(_values(airframe=50000)) == 12000


def test_cap_off_with_known_airframe_is_the_airframe():
    assert altitude_limit_cm(_values(cap_on=False, airframe=50000)) == 50000
    assert altitude_limit_cm(_values(cap_on=False, airframe=1000)) == 1000


@pytest.mark.parametrize("airframe", [UNSET, None])
def test_cap_off_with_unknown_airframe_raises(airframe):
    with pytest.raises(ValueError, match="no known airframe maximum"):
        altitude_limit_cm(_values(cap_on=False, airframe=airframe))


@pytest.mark.parametrize("missing", [CAP_ID, CAP_ON_ID, AIRFRAME_MAX_ID])
def test_a_missing_row_raises_and_is_never_defaulted(missing):
    values = _values(airframe=5000)
    del values[missing]
    with pytest.raises(ValueError, match="missing"):
        altitude_limit_cm(values)


@pytest.mark.parametrize("values", [_values(cap_on="no"), _values(cap="high"), _values(airframe="tall"),
                                    _values(cap=True), _values(airframe=False)])
def test_a_wrong_type_raises(values):
    with pytest.raises(ValueError):
        altitude_limit_cm(values)


def test_airframe_max_cm_reads_unknown_as_none():
    assert airframe_max_cm(_values(airframe=UNSET)) is None
    assert airframe_max_cm(_values(airframe=3000)) == 3000


def test_helper_takes_resolved_values():
    assert altitude_limit_cm(resolve("tello", "t1", {})) == 1000
    assert altitude_limit_cm(resolve(G, "g1", {})) == 12000
    stated = resolve(G, "g1", {Source.INVENTORY: {AIRFRAME_MAX_ID: 50000}})
    assert altitude_limit_cm(stated) == 12000
    off = resolve_unit(G, "g1", {Source.INVENTORY: {AIRFRAME_MAX_ID: 50000},
                                 Source.LAUNCHER: {CAP_ON_ID: False, LAUNCHER_CONFIRMED: {CAP_ON_ID: ACK}}})
    assert off[CAP_ON_ID].value is False
    assert altitude_limit_cm(off) == 50000


def test_params_altitude_imports_nothing_from_drone():
    tree = ast.parse((ROOT / "flightrisk" / "params" / "altitude.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("flightrisk.drone"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("flightrisk.drone") for a in node.names)


# The rows.

def test_row_8_generic_is_unknown_with_the_inventory_bound():
    row = BY_ID[AIRFRAME_MAX_ID]
    assert row.defaults[G] == UNSET
    assert row.bounds[G] == Bound(1000, 60000)
    assert row.edit[G] is Edit.FILE_ENV
    assert row.defaults["tello"] == 1000
    assert row.bounds["tello"] == Bound(1000, 1000)
    assert row.edit["tello"] is Edit.READ_ONLY


@pytest.mark.parametrize("value", [999, 60001])
def test_row_8_inventory_value_outside_the_bound_is_refused(value):
    with pytest.raises(ParamRefused):
        resolve(G, "g1", {Source.INVENTORY: {AIRFRAME_MAX_ID: value}})


def test_regulatory_cap_row():
    row = BY_ID[CAP_ID]
    for profile in ("tello", G):
        assert row.defaults[profile] == 12000
        assert row.bounds[profile] == Bound(12000, 12000)
        assert row.edit[profile] is Edit.READ_ONLY


def test_cap_on_row_is_a_safety_switch_on_by_default():
    row = BY_ID[CAP_ON_ID]
    assert row.safety_switch is True and row.off_value is False
    assert row.confirm is Confirm.LESS_SAFE
    assert row.defaults == {"tello": True, G: True}
    assert row.env is None and row.cli == () and row.env_aliases == ()
    assert CAP_ON_ID in LAUNCHER_CONFIRMED_ONLY


@pytest.mark.parametrize("source", [Source.FILE, Source.INVENTORY])
def test_cap_off_from_a_file_or_the_inventory_is_refused(source):
    with pytest.raises(ParamRefused):
        resolve(G, "g1", {source: {CAP_ON_ID: False}})


def test_cap_off_by_confirm_flag_is_refused():
    with pytest.raises(ParamRefused):
        resolve_unit(G, "g1", {Source.FILE: {CAP_ON_ID: False}}, confirmations={CAP_ON_ID: False})


def test_cap_off_from_the_launcher_without_confirmation_is_refused():
    with pytest.raises(ParamRefused):
        resolve_unit(G, "g1", {Source.LAUNCHER: {CAP_ON_ID: False}})
    with pytest.raises(ParamRefused):
        resolve_unit(G, "g1", {Source.LAUNCHER: {CAP_ON_ID: False, LAUNCHER_CONFIRMED: {CAP_ON_ID: True}}})


def test_cap_off_from_the_launcher_with_confirmation_is_accepted():
    result = resolve_unit(G, "g1", {Source.INVENTORY: {AIRFRAME_MAX_ID: 50000},
                                    Source.LAUNCHER: {CAP_ON_ID: False, LAUNCHER_CONFIRMED: {CAP_ON_ID: ACK}}})
    assert result[CAP_ON_ID].value is False and result[CAP_ON_ID].source is Source.LAUNCHER


def test_cap_has_no_environment_name_or_flag():
    result = resolve(G, "g1", {Source.ENV: {"FLIGHTRISK_ALTITUDE_CAP_ON": "0"}, Source.CLI: {}})
    assert result[CAP_ON_ID].value is True


# The catalogue and the declared maximum.

def test_generic_catalogue_max_height_is_unknown():
    assert BUILTIN_MODELS[G].envelope["max_height_m"].value is None
    assert BUILTIN_MODELS["tello"].envelope["max_height_m"].value == 30.0


def test_coverage_camera_handles_an_unknown_max_height_without_a_default():
    camera = BUILTIN_MODELS[G].coverage_camera("main", -90.0)
    assert camera.max_alt_m == math.inf


def test_mavlink_fleet_drone_declares_an_unknown_maximum():
    from flightrisk.drone.mavlink_fleet import MavlinkFleetDrone, MavlinkFleetOptions
    from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink

    drone = MavlinkFleetDrone("drone-1", MavsdkAutopilotLink("tcpout://127.0.0.1:1"), MavlinkFleetOptions())
    assert drone.capabilities.max_altitude_m is None


# --- The acknowledgement time is strict and fresh (F4) ---------------------------------------------------------

_NOW = datetime(2026, 11, 24, 18, 0, 0, tzinfo=timezone.utc)


def _at_problem(at):
    from flightrisk.params.altitude import acknowledgement_problem

    return acknowledgement_problem({**ACK, "ack": {**ACK["ack"], "at": at}}, now=_NOW)


@pytest.mark.parametrize("at", [
    "2026-11-24T18:00:00Z", "2026-11-24T18:00:00+00:00", "2026-11-24T17:59:59.5Z",
    "2026-11-24T17:59:59.123456+00:00", "2026-11-24T18:04:59Z", "2026-11-23T18:00:01Z",
])
def test_a_fresh_utc_time_in_the_strict_form_is_accepted(at):
    assert _at_problem(at) is None


@pytest.mark.parametrize("at", [
    "2026-11-24 18:00:00Z", "20261124T180000Z", "2026-W48-2T18:00:00Z", "2026-11-24T18:00:00-00:00",
    "2026-11-24T19:00:00+01:00", "2026-11-24T18:00:00", "2026-11-24T18:00Z", "2026-11-24T18:00:00z",
    "2026-11-24T18:00:00.1234567Z", "2026-11-24T18:00:00Z ", "2026-13-24T18:00:00Z", 1764007200, None,
])
def test_any_other_form_of_the_time_is_refused(at):
    assert "is not an ISO 8601 time in UTC" in _at_problem(at)


@pytest.mark.parametrize("at", ["2026-11-24T18:05:01Z", "9999-12-31T23:59:59Z"])
def test_a_time_more_than_5_minutes_ahead_is_refused(at):
    assert "more than 5 minutes in the future" in _at_problem(at)


@pytest.mark.parametrize("at", ["2026-11-23T17:59:59Z", "1970-01-01T00:00:00Z"])
def test_a_time_older_than_24_hours_is_refused(at):
    assert "more than 24 hours old" in _at_problem(at)


def test_the_freshness_limits_are_named_constants():
    from datetime import timedelta

    from flightrisk.params import altitude

    assert altitude.ACK_MAX_FUTURE == timedelta(minutes=5) and altitude.ACK_MAX_AGE == timedelta(hours=24)
