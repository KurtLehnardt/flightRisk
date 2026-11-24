"""Runtime code's copy of today's accepted ranges (SEAM-10).

``flightrisk.params.today.today_accepts`` must equal ``today_accepts_from_golden``
of the golden file exactly, so adopting ``resolve_unit`` keeps today's range
checks (A3 to A5). ``flightrisk/params/today_ranges.py`` is generated from the
golden file by ``scripts/make_param_golden.py --today-module`` and must be what
a fresh render gives, byte for byte.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from flightrisk.params import today, today_ranges
from flightrisk.params.resolver import ParamRefused, resolve_unit, today_accepts_from_golden
from flightrisk.params.spec import Source
from flightrisk.params.table import TABLE
from tests import test_param_parity as parity

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "flight_params.json"
SCRIPT = ROOT / "scripts" / "make_param_golden.py"
MODULE = ROOT / "flightrisk" / "params" / "today_ranges.py"


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _generator():
    spec = importlib.util.spec_from_file_location("make_param_golden", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _typed(accepts) -> dict[str, dict[str, Any]]:
    """``accepts`` with the type of every end, so an integer and an equal float differ."""
    return {
        name: {profile: None if r is None else (type(r.lo), r.lo, type(r.hi), r.hi, r.policy, r.lo_open, r.hi_open)
               for profile, r in per.items()}
        for name, per in accepts.items()
    }


def test_today_accepts_equals_the_golden_file_exactly():
    expected = today_accepts_from_golden(_golden())
    got = today.today_accepts()
    assert got == expected
    assert _typed(got) == _typed(expected)


def test_names_match_the_golden_file_and_the_registry():
    golden_names = {name for row in _golden()["rows"] for name in row["env_aliases"]}
    table_names = {
        name for spec in TABLE
        for name in ([spec.env] if spec.env else []) + [a.name for a in spec.env_aliases] + list(spec.cli)
    }
    assert set(today.today_accepts()) == golden_names == table_names


def test_the_module_has_one_entry_per_row_with_names_and_copies_no_name():
    rows = [row["id"] for row in _golden()["rows"] if row["env_aliases"]]
    assert list(today_ranges.TODAY_RANGES) == rows
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert [type(n) for n in tree.body] == [ast.Expr, ast.Assign]
    strings = {n.value for n in ast.walk(tree.body[1]) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not [s for s in strings if s.startswith(("FLIGHTRISK_", "--"))]
    fields = {key for per in today_ranges.TODAY_RANGES.values() for r in (per or {}).values() for key in (r or {})}
    assert fields <= {"lo", "hi", "policy", "lo_open", "hi_open"}


def test_regenerating_the_module_gives_the_committed_bytes():
    assert _generator().render_today_module(_golden()).encode("utf-8") == MODULE.read_bytes()


def test_the_generator_flag_writes_the_committed_bytes(tmp_path):
    target = tmp_path / "today_ranges.py"
    result = subprocess.run([sys.executable, str(SCRIPT), "--today-module", str(target)],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == MODULE.read_bytes()


def test_rendering_refuses_a_row_whose_names_carry_different_ranges():
    golden = _golden()
    row = next(r for r in golden["rows"] if len(r["env_aliases"]) > 1 and any(
        a["today_accepts"] for a in r["env_aliases"].values()))
    first = next(iter(row["env_aliases"].values()))
    first["today_accepts"] = None
    with pytest.raises(ValueError, match=re.escape(row["id"])):
        _generator().render_today_module(golden)


def test_every_call_returns_new_dictionaries():
    first = today.today_accepts()
    name = next(n for n, per in first.items() if per)
    first[name].clear()
    first.clear()
    assert today.today_accepts() == today_accepts_from_golden(_golden())


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return type(a) is type(b) and a == b


def _outcome(probe, raw: str) -> tuple[Any, Any]:
    """``_resolved`` and the ``resolve_unit`` outcome of one parity case, with the parity module's TODAY.

    The case maps to a flag or an environment layer exactly as ``test_resolver_matches_today`` maps it.
    """
    if probe.flag:
        source, layer = Source.CLI, {probe.name: raw}
        resolved = parity._resolved(probe.model, probe.param_id, cli=layer, simulated=probe.simulated)
    else:
        source, layer = Source.ENV, {**probe.companions, probe.name: raw}
        resolved = parity._resolved(probe.model, probe.param_id, env=layer, simulated=probe.simulated)
    try:
        unit = resolve_unit(probe.model, "drone-1", {source: layer}, simulated=probe.simulated,
                            today_accepts=parity.TODAY)
    except ParamRefused:
        unit_value = parity.REFUSED
    else:
        unit_value = unit[probe.param_id].value if probe.param_id in unit else parity.IGNORED
    return resolved, unit_value


def test_parity_probes_give_the_same_results_with_the_runtime_copy(monkeypatch):
    assert len(parity.CASES) > 500
    golden_derived = [_outcome(probe, raw) for probe, raw in parity.CASES]
    monkeypatch.setattr(parity, "TODAY", today.today_accepts())
    runtime = [_outcome(probe, raw) for probe, raw in parity.CASES]
    differ = [
        (probe.model, probe.name, raw, before, after)
        for (probe, raw), before, after in zip(parity.CASES, golden_derived, runtime)
        if not all(_same(x, y) for x, y in zip(before, after))
    ]
    assert differ == []
