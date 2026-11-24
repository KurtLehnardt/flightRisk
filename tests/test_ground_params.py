"""The ground's one resolution of each unit (``flightrisk/ground_params.py``; GND-1, GND-2, GND-7, item 47).

The ground resolves a unit from its model with only the environment names and flags ground code reads
today (``GROUND_NAMES``). Each unit resolves to its model's golden defaults, an edge-only name never
reaches the ground, and the inputs today's ground readers accept that the ground now refuses are exactly
the pinned ``ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT`` combinations with a ground name.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from types import MappingProxyType

import pytest

from flightrisk import ground_params
from flightrisk.dashboard import manual_control
from flightrisk.drone import plan_edit, safety
from flightrisk.drone.safety import FIELD_LIMITS, ROOM_LIMITS
from flightrisk.ground_params import (
    GROUND_ENV_NAMES,
    GROUND_FLAGS,
    GROUND_NAMES,
    GroundUnit,
    GroundUnitRefused,
    resolve_ground_unit,
    units_for,
)
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.params.resolver import ParamRefused, resolve_unit
from flightrisk.params.spec import Source
from flightrisk.params.table import TABLE
from flightrisk.params.today import TODAY_ACCEPTS
from tests import test_param_parity as parity

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "tests" / "golden" / "flight_params.json").read_text(encoding="utf-8"))
T, F = parity.T, parity.F
BATTERY = "FLIGHTRISK_MIN_TAKEOFF_BATTERY"

# --- GROUND_NAMES ---------------------------------------------------------------------------------------------

#: Each name ground code reads today, with its reader (origin/main 9936b7b). Found with
#: ``git grep -n -o -E "FLIGHTRISK_[A-Z_]+|--fleet-(area|strip)-cm" origin/main -- flightrisk/dashboard
#: flightrisk/drone/coordinator.py flightrisk/config.py`` intersected with the registry's names, plus
#: FLIGHTRISK_MIN_TAKEOFF_BATTERY, which the ground reads through ``safety.min_takeoff_battery_from_env``.
GROUND_READERS = {
    "FLIGHTRISK_GEOFENCE_CM":
        "flightrisk/dashboard/app.py:584-602 fleet_geofence_cm (safety.GEOFENCE_CM_ENV)",
    "FLIGHTRISK_MIN_TAKEOFF_BATTERY":
        "flightrisk/drone/safety.py:428 min_takeoff_battery_from_env, called by "
        "flightrisk/drone/coordinator.py:900 and flightrisk/dashboard/single_search.py:196",
    "FLIGHTRISK_MANUAL_ORPHAN_LAND_S": "flightrisk/dashboard/manual_control.py:155 manual_orphan_land_s",
    "FLIGHTRISK_MANUAL_LOST_IDLE_S": "flightrisk/dashboard/manual_control.py:165 manual_lost_idle_s",
    "FLIGHTRISK_FLEET_AREA_CM": "flightrisk/dashboard/__main__.py:141, the default of --fleet-area-cm",
    "--fleet-area-cm": "flightrisk/dashboard/__main__.py:139-143, parsed by _parse_area",
    "FLIGHTRISK_FLEET_STRIP_CM": "flightrisk/dashboard/__main__.py:146, the default of --fleet-strip-cm",
    "--fleet-strip-cm": "flightrisk/dashboard/__main__.py:144-148, checked positive at :194",
    # A ground read: plan_edit.camera_footprint_cm is called only by the dashboard's map plan, in the
    # ground process, and no edge reads the variable.
    "FLIGHTRISK_CAMERA_FOOTPRINT_CM":
        "flightrisk/drone/plan_edit.py:95 camera_footprint_cm, called only by "
        "flightrisk/dashboard/app.py:1062, :1114, :3064 and :3137",
}

#: Registry names the grep finds that ground code does not read: they appear there only in comments and
#: docstrings. Each is read by an edge, so the ground leaves it out.
NOT_READ_BY_THE_GROUND = {
    "FLIGHTRISK_LINK_LOSS_LAND_S": "app.py:561, :2005 and manual_control.py:39 mention the edge's watchdog",
    "FLIGHTRISK_OBSTACLE_SIDESTEP_CM": "app.py:609 and coordinator.py:181 mention the edges' sidestep",
    "FLIGHTRISK_HOVER_TEST": "static/ui/ui.js:203 is a comment in the browser script",
}


def _registry_names() -> set[str]:
    names: set[str] = set()
    for spec in TABLE:
        names |= ({spec.env} if spec.env else set()) | {a.name for a in spec.env_aliases} | set(spec.cli)
    return names


def test_ground_names_are_the_names_ground_code_reads_today():
    assert GROUND_NAMES == set(GROUND_READERS)
    assert GROUND_ENV_NAMES == {name for name in GROUND_NAMES if name.startswith("FLIGHTRISK_")}
    assert GROUND_FLAGS == {name for name in GROUND_NAMES if name.startswith("--")}
    assert GROUND_NAMES <= _registry_names()
    assert set(NOT_READ_BY_THE_GROUND) <= _registry_names()
    assert not GROUND_NAMES & set(NOT_READ_BY_THE_GROUND)


def test_the_ground_readers_read_the_listed_names():
    assert safety.GEOFENCE_CM_ENV == "FLIGHTRISK_GEOFENCE_CM"
    assert safety.MIN_TAKEOFF_BATTERY_ENV == BATTERY
    assert manual_control.ORPHAN_LAND_ENV == "FLIGHTRISK_MANUAL_ORPHAN_LAND_S"
    assert manual_control.LOST_IDLE_ENV == "FLIGHTRISK_MANUAL_LOST_IDLE_S"
    assert plan_edit.CAMERA_FOOTPRINT_ENV == "FLIGHTRISK_CAMERA_FOOTPRINT_CM"


# --- Golden defaults, sources and edge-only names ------------------------------------------------------------


_TABLE_DEFAULTS = {spec.id: spec.defaults for spec in TABLE}


def _plain(value):
    return tuple(value) if isinstance(value, (list, tuple)) else value


@pytest.mark.parametrize("model_id", ["tello", "tello_edu", "generic_multirotor"])
def test_a_unit_resolves_to_the_golden_defaults_of_its_model(model_id):
    unit = resolve_ground_unit("drone-1", model_id, environ={})
    profile = BUILTIN_MODELS[model_id].profile
    checked = 0
    for row in GOLDEN["rows"]:
        entry = row["profiles"][profile]
        if entry is None or entry["default"] is None:
            assert row["id"] not in unit.values
            continue
        effective = unit.values[row["id"]]
        default = entry["default"]
        if isinstance(default, dict):
            # A marker such as {"unset": true} is that word; a value derived at run time stays a word too.
            (marker, detail), = default.items()
            expected = marker if detail is True else _TABLE_DEFAULTS[row["id"]][profile]
            assert isinstance(effective.value, str) and effective.value == expected, row["id"]
        else:
            assert _plain(effective.value) == _plain(default), row["id"]
        assert effective.source is Source.PROFILE
        checked += 1
    assert checked > 60


@pytest.mark.parametrize(("model_id", "limits"), [("tello", ROOM_LIMITS), ("tello_edu", ROOM_LIMITS),
                                                  ("generic_multirotor", FIELD_LIMITS)])
def test_a_unit_carries_its_model_profile_and_catalogue_values(model_id, limits):
    unit = resolve_ground_unit("drone-1", model_id, environ={})
    model = BUILTIN_MODELS[model_id]
    assert unit.unit_id == "drone-1" and unit.model_id == model_id
    assert unit.limits == limits
    assert unit.limits is limits
    assert (unit.min_move_cm, unit.max_move_cm) == (limits.min_move_cm, limits.max_move_cm)
    assert unit.environment == model.fact("environment").value
    assert unit.indoor_profile_measured is model.fact("indoor_profile_measured").value
    assert unit.max_range_m == model.envelope["max_range_m"].value
    camera = model.cameras[0]
    assert (unit.camera.id, unit.camera.hfov_deg) == (camera.id, camera.hfov_deg)
    assert (unit.camera.width_px, unit.camera.height_px) == (camera.photo_w_px or camera.stream_w_px,
                                                             camera.photo_h_px or camera.stream_h_px)
    golden_battery = next(r for r in GOLDEN["rows"] if r["id"] == "battery.min_takeoff_pct")
    assert unit.min_takeoff_battery_pct == golden_battery["profiles"][model.profile]["default"]
    assert isinstance(unit.min_takeoff_battery_pct, float)


def test_a_tello_unit_matches_todays_room_values():
    unit = resolve_ground_unit("drone-1", "tello", environ={})
    assert unit.limits is ROOM_LIMITS
    assert (unit.min_move_cm, unit.max_move_cm) == (safety.MIN_MOVE_CM, safety.MAX_MOVE_CM)
    assert unit.min_takeoff_battery_pct == safety.min_takeoff_battery_from_env({})
    assert unit.values["envelope.geofence_cm"].value == safety.DEFAULT_GEOFENCE_CM
    assert unit.values["plan.alt_slots_cm"].value == tuple(safety.DEFAULT_ALT_SLOTS_CM)


@pytest.mark.parametrize("model_id", ["tello", "generic_multirotor"])
def test_min_and_max_move_are_whole_centimetres_as_int(model_id):
    unit = resolve_ground_unit("drone-1", model_id, environ={})
    assert type(unit.min_move_cm) is int and type(unit.max_move_cm) is int
    assert unit.max_move_cm >= 2 * unit.min_move_cm


def test_a_ground_name_from_the_environment_gives_source_env():
    unit = resolve_ground_unit("drone-1", "tello", environ={"FLIGHTRISK_GEOFENCE_CM": "800", "PATH": "/bin"})
    geofence = unit.values["envelope.geofence_cm"]
    assert (geofence.value, geofence.source, geofence.source_detail) == (800, Source.ENV, "FLIGHTRISK_GEOFENCE_CM")
    # The limits stay the profile's; the unit's own geofence is in its values.
    assert unit.limits is ROOM_LIMITS
    battery = resolve_ground_unit("drone-1", "tello", environ={BATTERY: "40"})
    assert battery.min_takeoff_battery_pct == 40.0
    assert battery.values["battery.min_takeoff_pct"].source is Source.ENV


def test_a_ground_flag_gives_source_cli():
    unit = resolve_ground_unit("drone-1", "tello", environ={}, flags={"--fleet-strip-cm": "60"})
    strip = unit.values["plan.strip_cm"]
    assert (strip.value, strip.source, strip.source_detail) == (60, Source.CLI, "--fleet-strip-cm")


@pytest.mark.parametrize(("environ", "flags", "row"), [
    # Each is refused at an edge's start (K-6, K-8, a test-adapter pair), but the ground never reads it.
    ({"FLIGHTRISK_LINK_LOSS_LAND_S": "1.4"}, None, "link.loss_detect_s"),
    ({"FLIGHTRISK_CEILING_CM": "150"}, None, "envelope.ceiling_cm"),
    ({"FLIGHTRISK_TELLO_TEMP_WARN_C": "120"}, None, "tello.temp_warn_c"),
    ({}, {"--alt-slot": "500"}, "envelope.alt_slot_cm"),
])
def test_an_edge_only_name_is_ignored_by_the_ground(environ, flags, row):
    layer = {Source.CLI: flags} if flags else {Source.ENV: environ}
    with pytest.raises(ParamRefused):
        resolve_unit("tello", "drone-1", layer, today_accepts=TODAY_ACCEPTS)
    unit = resolve_ground_unit("drone-1", "tello", environ=environ, flags=flags)
    assert unit.values[row].source is Source.PROFILE


def test_a_ground_name_in_the_wrong_layer_is_not_read():
    """An environment name given as a flag, or a flag given in the environment, is left out."""
    unit = resolve_ground_unit("drone-1", "tello", environ={"--fleet-strip-cm": "0"}, flags={BATTERY: "20"})
    assert unit.values["plan.strip_cm"].source is Source.PROFILE
    assert unit.values["battery.min_takeoff_pct"].source is Source.PROFILE


def test_the_resolution_is_never_simulated_and_reads_only_ground_names(monkeypatch):
    seen = []

    def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return resolve_unit(*args, **kwargs)

    monkeypatch.setattr(ground_params, "resolve_unit", spy)
    environ = {"FLIGHTRISK_GEOFENCE_CM": "600", "FLIGHTRISK_LINK_LOSS_LAND_S": "4", "HOME": "/x"}
    flags = {"--fleet-area-cm": "600x400", "--alt-slot": "100", "--fleet": "drone-1"}
    resolve_ground_unit("drone-3", "tello", environ=environ, flags=flags)
    (args, kwargs), = seen
    assert args[:2] == ("tello", "drone-3")
    assert args[2] == {Source.ENV: {"FLIGHTRISK_GEOFENCE_CM": "600"}, Source.CLI: {"--fleet-area-cm": "600x400"}}
    assert kwargs["simulated"] is False
    assert kwargs["today_accepts"] is TODAY_ACCEPTS


# --- Item 47: what the ground newly refuses -------------------------------------------------------------------


def test_the_ground_newly_refuses_exactly_the_pinned_set_for_its_names():
    found = set()
    probed = set()
    for probe, raw in parity.CASES:
        # A ground resolution is never simulated (CAP-6), so a simulated probe has no ground counterpart.
        if probe.name not in GROUND_NAMES or probe.simulated:
            continue
        if parity._today(lambda probe=probe, raw=raw: probe.read(raw)) == parity.REFUSED:
            continue
        probed.add(probe.name)
        if probe.flag:
            environ, flags = {}, {probe.name: raw}
        else:
            environ, flags = {**probe.companions, probe.name: raw}, None
        try:
            resolve_ground_unit("drone-1", probe.model, environ=environ, flags=flags)
        except GroundUnitRefused:
            found.add((probe.model, probe.name, raw))
    pinned = {case for case in parity.ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT if case[1] in GROUND_NAMES}
    assert found == pinned
    # Today that is K-3 alone: a Tello's minimum takeoff battery at or under its 25 % return floor.
    assert pinned == {(T, BATTERY, "15"), (T, BATTERY, "19"), (T, BATTERY, "20")}
    # Every ground name the parity harness probes was probed here (the strip flag and name are uncompared).
    assert probed == GROUND_NAMES - {"FLIGHTRISK_FLEET_STRIP_CM", "--fleet-strip-cm"}


def test_a_refusal_keeps_the_resolvers_text_and_names_the_unit_and_the_variable():
    with pytest.raises(ParamRefused) as direct:
        resolve_unit("tello", "drone-2", {Source.ENV: {BATTERY: "20"}}, today_accepts=TODAY_ACCEPTS)
    with pytest.raises(GroundUnitRefused) as refused:
        resolve_ground_unit("drone-2", "tello", environ={BATTERY: "20"})
    assert refused.value.unit_id == "drone-2"
    assert refused.value.text == direct.value.message
    assert "drone-2" in str(refused.value) and BATTERY in str(refused.value)


def test_an_unknown_model_is_refused_naming_the_unit():
    with pytest.raises(GroundUnitRefused, match="drone-4"):
        resolve_ground_unit("drone-4", "no_such_model", environ={})


# --- units_for -----------------------------------------------------------------------------------------------


def test_units_for_gives_each_unit_the_model_of_the_implicit_inventory():
    units, refused = units_for(["drone-1", "drone-2", "drone-3"],
                               {"drone-2": "generic_multirotor", "drone-3": "not_in_the_catalogue"}, environ={})
    assert refused == {}
    assert {uid: unit.model_id for uid, unit in units.items()} == {
        "drone-1": "tello", "drone-2": "generic_multirotor", "drone-3": "tello",  # INV-4
    }
    assert units["drone-1"].limits is ROOM_LIMITS and units["drone-2"].limits is FIELD_LIMITS
    assert units_for([], None, environ={}) == ({}, {})


def test_units_for_refuses_each_unit_on_its_own():
    units, refused = units_for(["drone-1", "drone-2", "bad id"], {"drone-2": "generic_multirotor"},
                               environ={BATTERY: "20"})
    assert set(units) == {"drone-2"}
    assert set(refused) == {"drone-1", "bad id"}
    assert refused["drone-1"].startswith("unit drone-1: ") and BATTERY in refused["drone-1"]
    assert "not a valid drone id" in refused["bad id"]


# --- The planner's move rule (follow-up item 12) --------------------------------------------------------------


def _with_moves(monkeypatch, low, high):
    """Make the resolution give these shortest and longest moves; no ground name can set them today."""
    real = ground_params.resolve_unit

    def resolve_with_moves(*args, **kwargs):
        values = real(*args, **kwargs)
        values["move.min_cm"] = dataclasses.replace(values["move.min_cm"], value=low)
        values["move.max_cm"] = dataclasses.replace(values["move.max_cm"], value=high)
        return values

    monkeypatch.setattr(ground_params, "resolve_unit", resolve_with_moves)


def test_a_longest_move_under_twice_the_shortest_is_refused(monkeypatch):
    _with_moves(monkeypatch, 20, 39)
    with pytest.raises(GroundUnitRefused) as refused:
        resolve_ground_unit("drone-7", "generic_multirotor", environ={})
    text = str(refused.value)
    assert text.startswith("unit drone-7: ")
    assert "longest single move (39 cm)" in text and "shortest single move (20 cm)" in text
    assert "twice" in text


def test_a_longest_move_of_exactly_twice_the_shortest_is_accepted(monkeypatch):
    _with_moves(monkeypatch, 20.0, 40.0)
    unit = resolve_ground_unit("drone-7", "generic_multirotor", environ={})
    assert (unit.min_move_cm, unit.max_move_cm) == (20, 40)
    assert type(unit.min_move_cm) is int and type(unit.max_move_cm) is int


def test_a_fractional_move_is_refused_rather_than_rounded(monkeypatch):
    _with_moves(monkeypatch, 20.5, 500)
    with pytest.raises(GroundUnitRefused, match="drone-7"):
        resolve_ground_unit("drone-7", "tello", environ={})


# --- Item 53 and immutability ---------------------------------------------------------------------------------


def test_the_ground_never_reads_an_altitude_maximum():
    source = (ROOT / "flightrisk" / "ground_params.py").read_text(encoding="utf-8")
    assert not re.search(r"max_altitude_(m|cm)", source)
    assert not [f.name for f in dataclasses.fields(GroundUnit) if "altitude" in f.name or "height" in f.name]


def test_a_unit_is_read_only():
    unit = resolve_ground_unit("drone-1", "tello", environ={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        unit.min_move_cm = 1  # type: ignore[misc]
    assert isinstance(unit.values, MappingProxyType)
    with pytest.raises(TypeError):
        unit.values["move.min_cm"] = None  # type: ignore[index]
