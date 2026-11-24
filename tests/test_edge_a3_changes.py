"""What A3 changes at edge start, sorted by today's start path (Phase A3, step S8).

``tests/test_param_parity.py`` pins ``ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT``: the inputs that today's
single-value readers accept but that ``resolve_unit`` refuses through a cross-value constraint. Today's
own cross-value checks already refuse some of them at edge start. This test runs each input through
today's start path (the parser, ``_resolve_drone_and_source``, ``_startup_envelope``, which calls
``_field_envelope`` on a MAVLink edge, ``_temp_limits`` and ``_flight_guards``) and sorts it into
"refused at start today" or "newly refused". The newly refused list is pinned below and recorded as
design section 27, A3 item 47. The test then shows that the current edge refuses every input of both
groups before it builds the drone.

The S2 fixture (``tests/fixtures/edge_start_baseline.json``) anchors the "today" side: its probe rows
show that today's single-value reader accepted each input, and its start objects show that the start
path called here still builds today's envelope, temperature levels and flight guards.
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
from flightrisk.edge_resolution import PARAMS_FILE_ENV_VAR
from flightrisk.hardware.inventory import ENV_FILE
from tests.test_param_parity import ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT, CASES, F, G, REFUSED, T

HERE = Path(__file__).parent
FIXTURE = json.loads((HERE / "fixtures" / "edge_start_baseline.json").read_text())
_PARSER = edge_device._build_parser()
_MAVLINK = ["--drone", "mavlink", "--frame-heading-deg", "0", "--mavlink-address", "tcpout://127.0.0.1:1"]
_PROBE = {(probe.model, probe.name, raw): probe for probe, raw in CASES}
_spec = importlib.util.spec_from_file_location("make_edge_baseline", HERE / "tools" / "make_edge_baseline.py")
maker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maker)

#: The inputs of ``ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT`` that a real edge start accepts today and the
#: A3 edge refuses. Recorded as design section 27, A3 item 47. Every other input of that set is refused
#: at start today by today's own cross-value checks, so refusing it is no behaviour change.
NEWLY_REFUSED_AT_START = frozenset({
    # K-3 on the Tello: a minimum takeoff battery at or under the 25 % return floor.
    (T, G + "MIN_TAKEOFF_BATTERY", "15"),
    (T, G + "MIN_TAKEOFF_BATTERY", "19"),
    (T, G + "MIN_TAKEOFF_BATTERY", "20"),
    # K-6: a link-loss detect time under 1.5 times the 1 s heartbeat interval.
    (T, G + "LINK_LOSS_LAND_S", "1.4"),
    (F, G + "LINK_LOSS_LAND_S", "1.4"),
    # K-8: a 900 cm margin under the probe's 1000 cm ceiling leaves 100 cm, below the Tello's 130 cm slot.
    (T, G + "CEILING_MARGIN_CM", "900"),
})
INPUTS = sorted(ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT)


class ReachedTheDrone(Exception):
    """Raised by the patched ``_build_drone``: the start was not refused."""


class _Log:
    def __getattr__(self, level):
        return lambda event, **fields: None


@pytest.fixture
def edge_env(monkeypatch, tmp_path):
    """No FlightRisk variable, no inventory and no params file; the drone and the runner are patched."""
    for name in [n for n in os.environ if n.startswith(G)]:
        monkeypatch.delenv(name)
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no-inventory.json"))
    monkeypatch.setenv(PARAMS_FILE_ENV_VAR, str(tmp_path / "no-params.json"))
    built: list[object] = []

    def build_drone(args, log):
        built.append(args)
        raise ReachedTheDrone()

    monkeypatch.setattr(edge_device, "_build_drone", build_drone)
    monkeypatch.setattr(edge_device, "_build_runner", lambda *a, **k: SimpleNamespace())
    return SimpleNamespace(monkeypatch=monkeypatch, built=built)


def _start_input(key, monkeypatch):
    """Set ``key``'s input (with its probe's companions) on a start of its model; returns argv and parsed args."""
    model, name, raw = key
    probe = _PROBE[key]
    argv = (["--source", "tello"] if model == T else _MAVLINK + ["--source", "camera"]) + ["--drone-id", "drone-1"]
    if probe.flag:
        argv += [name, raw]
    else:
        for env_name, value in {**probe.companions, name: raw}.items():
            monkeypatch.setenv(env_name, value)
    args = _PARSER.parse_args(argv)
    edge_device._resolve_drone_and_source(args, _PARSER)
    return argv, args


def _refused_by_todays_start_path(args) -> bool:
    try:
        edge_device._startup_envelope(args, os.environ)
        edge_device._temp_limits(None, None, os.environ)
        edge_device._flight_guards(os.environ)
    except SystemExit:
        return True
    return False


def _fixture_probe_value(key):
    model, name, raw = key
    kind = "flag" if _PROBE[key].flag else "env"
    rows = [value for row, value in FIXTURE["probes"].items() if row.split("|", 1)[1] == f"{model}|{name}|{kind}|{raw!r}"]
    assert len(rows) == 1, f"{key}: expected one S2 fixture probe row, found {len(rows)}"
    return rows[0]


def _jsonable(value):
    return json.loads(json.dumps(maker.jsonable(value), sort_keys=True))


def test_newly_refused_is_a_subset_of_the_pinned_parity_set():
    assert NEWLY_REFUSED_AT_START < ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT
    assert len(ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT - NEWLY_REFUSED_AT_START) == 17
    assert len(NEWLY_REFUSED_AT_START) == 6


@pytest.mark.parametrize("key", INPUTS, ids=lambda k: f"{k[0]}:{k[1]}={k[2]}")
def test_todays_single_value_reader_accepted_the_input(key):
    assert _fixture_probe_value(key) != REFUSED


@pytest.mark.parametrize("argv,envelope", [
    (["--source", "tello", "--drone-id", "drone-1"], "safety_envelope_from_env_empty"),
    (_MAVLINK + ["--source", "camera", "--drone-id", "drone-1"], "safety_envelope_for_field"),
])
def test_the_start_path_checked_here_builds_todays_start_objects(edge_env, argv, envelope):
    args = _PARSER.parse_args(argv)
    edge_device._resolve_drone_and_source(args, _PARSER)
    assert _jsonable(edge_device._startup_envelope(args, {})) == FIXTURE["start_objects"][envelope]
    assert _jsonable(edge_device._temp_limits(None, None, {})) == FIXTURE["start_objects"]["temp_limits_from_env_empty"]
    assert _jsonable(edge_device._flight_guards({})) == FIXTURE["start_objects"]["flight_guards_empty_env"]


def test_each_input_is_sorted_by_todays_start_path(edge_env):
    newly = set()
    for key in INPUTS:
        with pytest.MonkeyPatch.context() as mp:
            _, args = _start_input(key, mp)
            if not _refused_by_todays_start_path(args):
                newly.add(key)
    assert newly == NEWLY_REFUSED_AT_START


@pytest.mark.parametrize("key", INPUTS, ids=lambda k: f"{k[0]}:{k[1]}={k[2]}")
def test_the_a3_edge_refuses_the_input_before_it_builds_the_drone(edge_env, key):
    argv, args = _start_input(key, edge_env.monkeypatch)
    with pytest.raises(SystemExit) as refused:
        edge_device._run_flight_edge(args, _Log(), stop_event=threading.Event(), argv=argv, parser=_PARSER)
    assert str(refused.value.code).startswith("edge_device: ")
    assert edge_env.built == [], "the drone was built before the refusal"
    # The resolution on its own refuses it too, so the refusal does not rest on today's checks.
    argv, args = _start_input(key, edge_env.monkeypatch)
    with pytest.raises(SystemExit):
        edge_device._resolve_edge_start(args, _Log(), argv=argv, parser=_PARSER)
