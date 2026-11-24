"""The edge builds its start envelope from the resolved values (Phase A3, step S11, EDGE-2).

``_startup_envelope`` and ``_field_envelope`` read ``args.edge_resolution`` once the edge has resolved
its unit. With today's inputs the envelope is exactly today's (the S2 fixture); a value from the
launcher layer or the params file, inside today's range, reaches the envelope; a name today's envelope
does not read, and a test-adapter fact override on a production MAVLink unit, change nothing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from flightrisk import edge_device
from flightrisk.edge_resolution import ENVELOPE_ROWS, LAUNCHER_PARAMS_ENV_VAR, PARAMS_FILE_ENV_VAR
from flightrisk.hardware.inventory import ENV_FILE
from flightrisk.params.spec import Source
from tests.test_param_parity import CASES, IGNORED_ON_PRODUCTION_UNIT, REFUSED, G, _today

HERE = Path(__file__).parent
FIXTURE = json.loads((HERE / "fixtures" / "edge_start_baseline.json").read_text())
_PARSER = edge_device._build_parser()
_MAVLINK = ["--drone", "mavlink", "--frame-heading-deg", "0", "--mavlink-address", "tcpout://127.0.0.1:1"]
_ROOM = ["--source", "tello", "--drone-id", "drone-1"]
_FAKE = ["--source", "fake", "--drone-id", "drone-1"]
_FIELD = _MAVLINK + ["--source", "camera", "--drone-id", "drone-1"]
_spec = importlib.util.spec_from_file_location("make_edge_baseline", HERE / "tools" / "make_edge_baseline.py")
maker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maker)


class ReachedTheDrone(Exception):
    """Raised by the patched ``_build_drone``: the start was not refused."""


class _Log:
    def __getattr__(self, level):
        return lambda event, **fields: None


@pytest.fixture
def edge_env(monkeypatch, tmp_path):
    """No FlightRisk variable, no inventory and no params file unless a test adds one."""
    for name in [n for n in os.environ if n.startswith(G)]:
        monkeypatch.delenv(name)
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no-inventory.json"))
    monkeypatch.setenv(PARAMS_FILE_ENV_VAR, str(tmp_path / "no-params.json"))
    return monkeypatch


def _args(argv):
    args = _PARSER.parse_args(argv)
    edge_device._resolve_drone_and_source(args, _PARSER)
    return args


def _envelopes(argv):
    """Today's start envelope (no resolution) and the one built from the resolution, for ``argv``."""
    args = _args(argv)
    today = edge_device._startup_envelope(args)
    edge_device._resolve_edge_start(args, _Log(), argv=argv, parser=_PARSER)
    assert args.edge_resolution is not None
    return today, edge_device._startup_envelope(args), args.edge_resolution


def _jsonable(envelope):
    return json.loads(json.dumps(maker.jsonable(envelope), sort_keys=True))


def _launcher(monkeypatch, values):
    monkeypatch.setenv(LAUNCHER_PARAMS_ENV_VAR, json.dumps({"unit_id": "drone-1", "values": values}))


# --- Today's inputs build today's envelope ----------------------------------------------------------------


@pytest.mark.parametrize("argv,fixture", [
    (_ROOM, "safety_envelope_from_env_empty"),
    (_FAKE, "safety_envelope_from_env_empty"),
    (_FIELD, "safety_envelope_for_field"),
])
def test_with_no_input_the_resolved_envelope_is_the_baseline(edge_env, argv, fixture):
    _, resolved, _ = _envelopes(argv)
    assert _jsonable(resolved) == FIXTURE["start_objects"][fixture]


@pytest.mark.parametrize("argv", [_ROOM, _FIELD])
def test_with_nothing_new_resolved_the_envelope_object_is_kept(edge_env, argv):
    args = _args(argv)
    edge_device._resolve_edge_start(args, _Log(), argv=argv, parser=_PARSER)
    envelope = edge_device.SafetyEnvelope.for_field() if argv is _FIELD else edge_device.SafetyEnvelope()
    assert edge_device._resolved_envelope(args, envelope) is envelope
    _launcher(edge_env, {"battery.min_takeoff_pct": 40})
    args = _args(argv)
    edge_device._resolve_edge_start(args, _Log(), argv=argv, parser=_PARSER)
    assert edge_device._resolved_envelope(args, envelope).min_takeoff_battery == 40


@pytest.mark.parametrize("argv,environ", [
    (_ROOM + ["--alt-slot", "120"], {}),
    (_ROOM, {G + "GEOFENCE_CM": "400", G + "CEILING_CM": "250", G + "CEILING_MARGIN_CM": "70",
             G + "MIN_TAKEOFF_BATTERY": "40", G + "LINK_LOSS_LAND_S": "4"}),
    (_FIELD + ["--alt-slot-m", "12", "--link-loss-hold-s", "4"], {}),
    (_FIELD, {G + "FIELD_GEOFENCE_M": "25", G + "FIELD_CEILING_M": "40", G + "FIELD_CEILING_MARGIN_M": "6",
              G + "MIN_TAKEOFF_BATTERY": "40", G + "FIELD_LINK_LOSS_HOLD_S": "7"}),
])
def test_todays_environment_and_flags_build_todays_envelope(edge_env, argv, environ):
    for name, raw in environ.items():
        edge_env.setenv(name, raw)
    today, resolved, _ = _envelopes(argv)
    assert _jsonable(resolved) == _jsonable(today)


@pytest.mark.parametrize("argv,name,raw,attr,value", [
    # Read by no envelope today: an indoor slot comes from --alt-slot, and the landing level is fixed.
    (_ROOM, G + "FIELD_ALT_SLOT_M", "1.2", "alt_slot_cm", 100),
    (_ROOM, G + "LAND_BATTERY", "12", "land_battery", 15),
    # The field edge reads its hold from the older name only.
    (_FIELD, G + "LINK_LOSS_WAIT_S", "5", "link_loss_hold_s", 10),
])
def test_a_name_todays_envelope_does_not_read_changes_nothing(edge_env, argv, name, raw, attr, value):
    edge_env.setenv(name, raw)
    today, resolved, _ = _envelopes(argv)
    assert getattr(today, attr) == value
    assert _jsonable(resolved) == _jsonable(today)


# --- New layers reach the envelope ------------------------------------------------------------------------


def test_a_launcher_value_reaches_the_indoor_envelope(edge_env):
    _launcher(edge_env, {"envelope.geofence_cm": 400, "envelope.ceiling_margin_cm": 70,
                         "battery.min_takeoff_pct": 40})
    today, resolved, resolution = _envelopes(_ROOM)
    assert resolution.values["envelope.geofence_cm"].source is Source.LAUNCHER
    assert (today.geofence_cm, today.ceiling_margin_cm, today.min_takeoff_battery) == (500, 60, 30)
    assert (resolved.geofence_cm, resolved.ceiling_margin_cm, resolved.min_takeoff_battery) == (400, 70, 40)
    assert resolved.limits is today.limits
    assert resolved.link_loss_hold_s is None  # The indoor edge still lands at once.


def test_a_launcher_value_reaches_the_field_envelope(edge_env):
    _launcher(edge_env, {"envelope.geofence_cm": 2500, "battery.min_takeoff_pct": 40, "link.loss_wait_s": 5})
    today, resolved, _ = _envelopes(_FIELD)
    assert (today.geofence_cm, today.min_takeoff_battery, today.link_loss_hold_s) == (3000, 30, 10)
    assert (resolved.geofence_cm, resolved.min_takeoff_battery, resolved.link_loss_hold_s) == (2500, 40, 5)
    assert resolved.limits is today.limits


def test_a_params_file_value_reaches_the_field_envelope(edge_env, tmp_path):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"version": 1, "units": {"drone-1": {"envelope.geofence_cm": 2000}}}),
                    encoding="utf-8")
    edge_env.setenv(PARAMS_FILE_ENV_VAR, str(path))
    _, resolved, resolution = _envelopes(_FIELD)
    assert resolution.values["envelope.geofence_cm"].source is Source.FILE
    assert resolved.geofence_cm == 2000


def test_the_environment_wins_over_the_launcher_as_in_the_resolution(edge_env):
    _launcher(edge_env, {"battery.min_takeoff_pct": 40})
    edge_env.setenv(G + "MIN_TAKEOFF_BATTERY", "35")
    _, resolved, resolution = _envelopes(_ROOM)
    assert resolution.values["battery.min_takeoff_pct"].value == 35
    assert resolved.min_takeoff_battery == 35


@pytest.mark.parametrize("argv,provider", [(_ROOM, "_envelope_pose_provider"), (_FIELD, "_autopilot_pose_provider")])
def test_the_flight_edge_flies_the_resolved_envelope(edge_env, argv, provider):
    _launcher(edge_env, {"battery.min_takeoff_pct": 40})
    seen = []
    real = getattr(edge_device, provider)
    edge_env.setattr(edge_device, provider, lambda envelope, *rest: seen.append(envelope) or real(envelope, *rest))
    edge_env.setattr(edge_device, "_build_runner", lambda *a, **k: SimpleNamespace())

    def build_drone(args, log):
        raise ReachedTheDrone()

    edge_env.setattr(edge_device, "_build_drone", build_drone)
    with pytest.raises(ReachedTheDrone):
        edge_device._run_flight_edge(_args(argv), _Log(), stop_event=threading.Event(), argv=argv, parser=_PARSER)
    assert [envelope.min_takeoff_battery for envelope in seen] == [40]


def test_an_envelope_the_caller_passes_is_kept(edge_env):
    _launcher(edge_env, {"battery.min_takeoff_pct": 40})
    passed = edge_device.SafetyEnvelope()
    seen = []
    edge_env.setattr(edge_device, "_envelope_pose_provider", lambda envelope: seen.append(envelope) or None)
    edge_env.setattr(edge_device, "_build_runner", lambda *a, **k: SimpleNamespace())
    edge_env.setattr(edge_device, "_build_drone", lambda args, log: (_ for _ in ()).throw(ReachedTheDrone()))
    with pytest.raises(ReachedTheDrone):
        edge_device._run_flight_edge(_args(_ROOM), _Log(), stop_event=threading.Event(), envelope=passed,
                                     argv=_ROOM, parser=_PARSER)
    assert seen == [passed]


# --- Fact overrides on a production unit ------------------------------------------------------------------


def _accepted_raw(name):
    for probe, raw in CASES:
        if probe.name == name and not probe.flag and raw.strip() and _today(lambda: probe.read(raw)) != REFUSED:
            return probe, raw
    raise AssertionError(f"no accepted probe input for {name}")


@pytest.mark.parametrize("name", sorted(name for _, name in IGNORED_ON_PRODUCTION_UNIT)
                         + ["FLIGHTRISK_TELLO_TAKEOFF_LATE_S"])
def test_a_fact_override_on_a_production_mavlink_unit_does_not_reach_the_envelope(edge_env, name):
    probe = None
    if name in {n for _, n in IGNORED_ON_PRODUCTION_UNIT}:
        probe, raw = _accepted_raw(name)
        for companion, value in {**probe.companions, name: raw}.items():
            edge_env.setenv(companion, value)
    else:
        edge_env.setenv(name, "5")
    _, resolved, resolution = _envelopes(_FIELD)
    assert resolution.simulated is False
    if probe is not None:
        assert resolution.values[probe.param_id].source is Source.PROFILE  # The override was ignored.
    assert all(resolution.values[row].source is Source.PROFILE for row in ENVELOPE_ROWS.values())
    assert _jsonable(resolved) == FIXTURE["start_objects"]["safety_envelope_for_field"]


# --- Where no resolution exists, the envelope is today's --------------------------------------------------


def test_without_a_resolution_the_envelope_builders_are_todays(edge_env):
    _launcher(edge_env, {"battery.min_takeoff_pct": 40})
    room = _args(_ROOM)
    assert getattr(room, "edge_resolution", None) is None
    assert _jsonable(edge_device._startup_envelope(room, {})) == FIXTURE["start_objects"]["safety_envelope_from_env_empty"]
    field = _args(_FIELD)
    assert _jsonable(edge_device._field_envelope(field, {})) == FIXTURE["start_objects"]["safety_envelope_for_field"]


def test_an_invalid_resolved_envelope_refuses_the_start(edge_env, monkeypatch):
    args = _args(_ROOM)
    edge_device._resolve_edge_start(args, _Log(), argv=_ROOM, parser=_PARSER)
    from flightrisk import edge_resolution

    monkeypatch.setattr(edge_resolution, "envelope_arguments",
                        lambda resolution, today: {"land_battery": 50, "min_takeoff_battery": 40})
    with pytest.raises(SystemExit, match=r"^edge_device: the resolved envelope is invalid: .*Refusing to start\.$"):
        edge_device._startup_envelope(args)
