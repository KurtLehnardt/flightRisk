"""Today's accepted ranges have a runtime home (``flightrisk/params/today.py``) that matches the golden file.

Runtime code never reads ``tests/golden``, so the edge passes ``TODAY_ACCEPTS`` to ``resolve_unit`` at
start. These tests keep it equal to what the golden file holds, check that the generator would write the
committed file again and refuses to overwrite it, and check that what the edge newly refuses with it is
exactly the pinned ``ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT`` set.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path

from flightrisk import edge_device
from flightrisk.edge_resolution import PARAMS_FILE_ENV_VAR, EdgeStartRefused, choose_model, resolve_edge
from flightrisk.params.resolver import TodayRange, today_accepts_from_golden
from flightrisk.params.today import TODAY_ACCEPTS
from tests import test_param_parity as parity

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "tests" / "golden" / "flight_params.json").read_text(encoding="utf-8"))
TODAY_FILE = ROOT / "flightrisk" / "params" / "today.py"


def _generator():
    spec = importlib.util.spec_from_file_location("make_param_today", ROOT / "scripts" / "make_param_today.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_today_accepts_equals_the_golden_file():
    expected = today_accepts_from_golden(GOLDEN)
    assert list(TODAY_ACCEPTS) == list(expected)
    assert {name: dict(per) for name, per in TODAY_ACCEPTS.items()} == expected


def test_every_range_is_a_today_range_with_a_known_policy():
    for per in TODAY_ACCEPTS.values():
        for accepts in per.values():
            assert accepts is None or (isinstance(accepts, TodayRange) and accepts.policy in {"refuse", "clamp"})


def test_the_generator_writes_the_committed_file_from_the_golden_file():
    assert _generator().render(GOLDEN) == TODAY_FILE.read_text(encoding="utf-8")


def test_the_generator_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "today.py"
    out.write_text("kept", encoding="utf-8")
    assert _generator().main(["make_param_today.py", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "kept"
    assert "refusing to overwrite" in capsys.readouterr().err


def test_the_edge_passes_today_accepts_by_default():
    assert inspect.signature(resolve_edge).parameters["today_accepts"].default is TODAY_ACCEPTS


#: The edge drone kind and ``--source`` that give a probe's model and ``simulated`` flag. Only the fake
#: adapter is simulated at the edge (CAP-6), so a simulated MAVLink probe has no edge start.
_EDGE_START = {
    (parity.T, False): ("tello", "tello"),
    (parity.T, True): ("fake", "fake"),
    (parity.F, False): ("mavlink", "camera"),
}


def test_the_edge_newly_refuses_exactly_the_pinned_set(tmp_path):
    parser = edge_device._build_parser()
    found = set()
    for probe, raw in parity.CASES:
        start = _EDGE_START.get((probe.model, probe.simulated))
        if start is None or parity._today(lambda: probe.read(raw)) == parity.REFUSED:
            continue
        drone, source = start
        environ = {PARAMS_FILE_ENV_VAR: str(tmp_path / "no-params.json")}
        argv: tuple[str, ...] = ()
        if probe.flag:
            argv = (probe.name, raw)
        else:
            environ.update({**probe.companions, probe.name: raw})
        choice = choose_model(drone=drone, drone_id="drone-1", environ=environ, load_inventory=lambda: None)
        try:
            resolve_edge(choice=choice, drone=drone, drone_id="drone-1", source=source, environ=environ,
                         argv=argv, parser=parser, load_inventory=lambda: None)
        except EdgeStartRefused:
            found.add((probe.model, probe.name, raw))
    assert found == parity.ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT
