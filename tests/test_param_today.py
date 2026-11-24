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
import inspect
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from flightrisk.params import resolver as resolver_module
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


# Every runtime call of a resolver entry point must pass today's accepted ranges (SEAM-10).
# Passing None, or leaving the keyword out, would check environment and flag values against
# the target bounds, which would change behaviour at adoption.

RESOLVER = "flightrisk.params.resolver"
TODAY_ACCEPTS = "flightrisk.params.today.today_accepts"


def _target_names() -> set[str]:
    """Names of the resolver functions that take ``today_accepts``, plus a possible package re-export."""
    names: set[str] = set()
    for fn_name, fn in inspect.getmembers(resolver_module, inspect.isfunction):
        if fn.__module__ == RESOLVER and "today_accepts" in inspect.signature(fn).parameters:
            names.add(f"{RESOLVER}.{fn_name}")
            names.add(f"flightrisk.params.{fn_name}")
    return names


def _package_of(rel_path: str) -> str:
    parts = rel_path.replace("\\", "/").split("/")
    return ".".join(parts[:-1])


def _absolute_module(node: ast.ImportFrom, package: str | None) -> str | None:
    """The absolute module an ``ImportFrom`` names, resolving relative imports against ``package``."""
    if not node.level:
        return node.module
    if not package:
        return None
    parts = package.split(".")
    if node.level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    if node.module:
        base.append(node.module)
    return ".".join(base) or None


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(body: list[ast.AST]):
    """Every node of ``body`` that belongs to this scope, naming but not entering nested scopes."""
    stack = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPES):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _import_map(tree: ast.AST, package: str) -> tuple[dict[str, str], list[ast.ImportFrom]]:
    """Map every name bound by an import, anywhere in the file, to its dotted target."""
    names: dict[str, str] = {}
    stars: list[ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    first = alias.name.split(".")[0]
                    names[first] = first
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, package)
            for alias in node.names:
                if alias.name == "*":
                    stars.append(node)
                elif module:
                    names[alias.asname or alias.name] = f"{module}.{alias.name}"
    return names, stars


def today_accepts_problems(text: str, rel_path: str) -> list[str]:
    """Problems with how one source file calls the resolver; each names the file, the line and the reason."""
    targets = _target_names()
    tree = ast.parse(text.lstrip("\ufeff"), filename=rel_path)
    names, stars = _import_map(tree, _package_of(rel_path))
    problems: list[str] = []

    def problem(node: ast.AST, reason: str) -> None:
        problems.append(f"{rel_path}:{getattr(node, 'lineno', 0)}: {reason}")

    def dotted(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def is_today_call(node: ast.AST | None) -> bool:
        return isinstance(node, ast.Call) and dotted(node.func) == TODAY_ACCEPTS

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing_functions(node: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        found = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(node)
        return found

    def param_default(fn: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> tuple[bool, ast.AST | None]:
        """Whether ``name`` is a parameter of ``fn``, and its default expression, if any."""
        args = fn.args
        positional = [*args.posonlyargs, *args.args]
        defaults: list[ast.AST | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        for arg, default in zip(positional, defaults):
            if arg.arg == name:
                return True, default
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if arg.arg == name:
                return True, default
        for arg in (args.vararg, args.kwarg):
            if arg is not None and arg.arg == name:
                return True, None
        return False, None

    def scope_bindings(body: list[ast.AST], name: str) -> list[ast.AST | None]:
        """Values bound to ``name`` in a scope; ``None`` stands for a binding that is not a plain assignment."""
        values: list[ast.AST | None] = []
        plain: set[int] = set()
        for node in _own_nodes(body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        values.append(node.value)
                        plain.add(id(target))
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    values.append(node.value)
                    plain.add(id(node.target))
            elif isinstance(node, ast.Name):
                if node.id == name and isinstance(node.ctx, ast.Store) and id(node) not in plain:
                    values.append(None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                values.append(None)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if any((alias.asname or alias.name.split(".")[0]) == name for alias in node.names):
                    values.append(None)
        return values

    def check_value(call: ast.Call, value: ast.AST) -> None:
        if is_today_call(value):
            return
        if isinstance(value, ast.Constant) and value.value is None:
            problem(call, "today_accepts is None, which checks against the target bounds")
            return
        if not isinstance(value, ast.Name):
            problem(call, f"today_accepts must be flightrisk.params.today.today_accepts(), got {type(value).__name__}")
            return
        for fn in enclosing_functions(call):
            is_param, default = param_default(fn, value.id)
            own = scope_bindings(fn.body, value.id)
            if is_param or own:
                if is_param and not is_today_call(default):
                    problem(call, f"wrapper {fn.name} forwards its parameter {value.id}, "
                                  "whose default is not flightrisk.params.today.today_accepts()")
                    return
                if any(not is_today_call(v) for v in own):
                    problem(call, f"{value.id} in {fn.name} is also bound to something other than today_accepts()")
                return
        module_values = scope_bindings(tree.body, value.id)
        if not module_values:
            problem(call, f"today_accepts name {value.id} is not bound to today_accepts() in this file")
        elif any(not is_today_call(v) for v in module_values):
            problem(call, f"{value.id} is bound to something other than today_accepts()")

    for star in stars:
        if _absolute_module(star, _package_of(rel_path)) in (RESOLVER, "flightrisk.params"):
            problem(star, "star import of the resolver hides calls from the rule")

    func_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_nodes.add(id(node.func))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if dotted(node.func) in targets:
                if any(kw.arg is None for kw in node.keywords):
                    problem(node, "call passes ** arguments, so today_accepts cannot be checked")
                    continue
                keyword = next((kw for kw in node.keywords if kw.arg == "today_accepts"), None)
                if keyword is None:
                    problem(node, "call does not name the today_accepts keyword (default None)")
                else:
                    check_value(node, keyword.value)
            elif isinstance(node.func, ast.Name) and node.func.id == "getattr":
                for arg in node.args[1:]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and any(
                            t.rsplit(".", 1)[1] == arg.value for t in targets):
                        problem(node, f"getattr of {arg.value} hides the call from the rule")
        elif isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in func_nodes:
            if dotted(node) in targets and isinstance(getattr(node, "ctx", None), ast.Load):
                problem(node, "resolver function used other than as the function of a direct call")
    return problems


def test_the_resolver_functions_that_take_today_accepts_include_resolve_unit():
    targets = _target_names()
    assert f"{RESOLVER}.resolve_unit" in targets
    assert f"{RESOLVER}.resolve" in targets


def test_every_runtime_call_of_the_resolver_passes_today_accepts():
    files = sorted(
        path for path in (ROOT / "flightrisk").rglob("*.py")
        if "params" not in path.relative_to(ROOT / "flightrisk").parts[:1]
    )
    assert len(files) > 50
    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
        problems += today_accepts_problems(text, path.relative_to(ROOT).as_posix())
    assert problems == []


GOOD_CALLS = [
    "from flightrisk.params.resolver import resolve_unit\nfrom flightrisk.params.today import today_accepts\n"
    "resolve_unit('m', 'u', {}, today_accepts=today_accepts())\n",
    "from flightrisk.params import resolver as r, today as m\n"
    "r.resolve_unit('m', 'u', {}, today_accepts=m.today_accepts())\n",
    "import flightrisk.params.resolver\nimport flightrisk.params.today\n"
    "flightrisk.params.resolver.resolve_unit('m', 'u', {}, "
    "today_accepts=flightrisk.params.today.today_accepts())\n",
    "import flightrisk.params.resolver as r\nimport flightrisk.params.today as t\n"
    "r.resolve('m', 'u', {}, today_accepts=t.today_accepts())\n",
    "from flightrisk.params import resolve_unit\nfrom flightrisk.params.today import today_accepts as t\n"
    "resolve_unit('m', 'u', {}, today_accepts=t())\n",
    "from flightrisk.params.resolver import resolve_unit as go\n"
    "from flightrisk.params.today import today_accepts\n"
    "def f():\n    ranges = today_accepts()\n    return go('m', 'u', {}, today_accepts=ranges)\n",
    "from flightrisk.params.resolver import resolve_unit\nfrom flightrisk.params.today import today_accepts\n"
    "RANGES = today_accepts()\ndef f():\n    return resolve_unit('m', 'u', {}, today_accepts=RANGES)\n",
    "from flightrisk.params.resolver import resolve_unit\nfrom flightrisk.params.today import today_accepts\n"
    "def wrap(ranges=today_accepts()):\n    return resolve_unit('m', 'u', {}, today_accepts=ranges)\n",
    "def f():\n    from flightrisk.params.resolver import resolve_unit\n"
    "    from flightrisk.params.today import today_accepts\n"
    "    return resolve_unit('m', 'u', {}, today_accepts=today_accepts())\n",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from flightrisk.params.resolver import resolve_unit\n",
    "def other(resolve_unit):\n    return 1\n",
]

# Each bad source with a fragment of the reason the rule must give.
BAD_CALLS = [
    ("from flightrisk.params.resolver import resolve_unit\nresolve_unit('m', 'u', {})\n", "does not name"),
    ("from flightrisk.params.resolver import resolve\nresolve('m', 'u', {})\n", "does not name"),
    ("from flightrisk.params.resolver import resolve_unit\n"
     "resolve_unit('m', 'u', {}, today_accepts=None)\n", "is None"),
    ("from flightrisk.params.resolver import resolve_unit\nfrom flightrisk.params.today import today_accepts\n"
     "def f(**kw):\n    return resolve_unit('m', 'u', {}, today_accepts=today_accepts(), **kw)\n", "** arguments"),
    ("from flightrisk.params.resolver import resolve_unit as go\ngo('m', 'u', {})\n", "does not name"),
    ("from flightrisk.params import resolver as r\nr.resolve_unit('m', 'u', {}, today_accepts=None)\n", "is None"),
    ("import flightrisk.params.resolver\nflightrisk.params.resolver.resolve_unit('m', 'u', {})\n", "does not name"),
    ("from ..params.resolver import resolve_unit\nresolve_unit('m', 'u', {})\n", "does not name"),
    ("from ..params import resolver\nresolver.resolve_unit('m', 'u', {})\n", "does not name"),
    ("from flightrisk.params import resolve_unit\nresolve_unit('m', 'u', {})\n", "does not name"),
    ("from flightrisk.params.resolver import resolve_unit\n"
     "def wrap(ranges=None):\n    return resolve_unit('m', 'u', {}, today_accepts=ranges)\n", "wrapper wrap"),
    ("from flightrisk.params.resolver import resolve_unit\n"
     "def wrap(ranges):\n    return resolve_unit('m', 'u', {}, today_accepts=ranges)\n", "wrapper wrap"),
    ("from flightrisk.params.resolver import resolve_unit\nfrom flightrisk.params.today import today_accepts\n"
     "def f():\n    r = today_accepts()\n    r = None\n    return resolve_unit('m', 'u', {}, today_accepts=r)\n",
     "also bound"),
    ("from flightrisk.params.resolver import resolve_unit\nR = None\n"
     "resolve_unit('m', 'u', {}, today_accepts=R)\n", "bound to something other"),
    ("from flightrisk.params.resolver import resolve_unit\n"
     "class C:\n    def f(self):\n        return resolve_unit('m', 'u', {}, today_accepts=self.x)\n",
     "got Attribute"),
    ("from flightrisk.params.resolver import resolve_unit\nfrom flightrisk.params.today import today_accepts\n"
     "resolve_unit('m', 'u', {}, today_accepts=today_accepts() if 1 else None)\n", "got IfExp"),
    ("from flightrisk.params.resolver import resolve_unit, today_accepts_from_golden\n"
     "resolve_unit('m', 'u', {}, today_accepts=today_accepts_from_golden({}))\n", "must be"),
    ("from flightrisk.params.resolver import resolve_unit\nf = resolve_unit\n", "other than as the function"),
    ("import functools\nfrom flightrisk.params.resolver import resolve_unit\n"
     "g = functools.partial(resolve_unit, 'm', 'u', {})\n", "other than as the function"),
    ("from flightrisk.params import resolver\nf = getattr(resolver, 'resolve_unit')\n", "getattr"),
    ("from flightrisk.params.resolver import *\n", "star import"),
]


@pytest.mark.parametrize("source", GOOD_CALLS)
def test_the_rule_accepts_good_forms(source):
    assert today_accepts_problems(source, "flightrisk/drone/x.py") == []


@pytest.mark.parametrize(("source", "reason"), BAD_CALLS)
def test_the_rule_rejects_bad_forms(source, reason):
    found = today_accepts_problems(source, "flightrisk/drone/x.py")
    assert found, source
    assert all(line.startswith("flightrisk/drone/x.py:") for line in found)
    assert any(reason in line for line in found), found


def test_the_rule_follows_a_wrapper_that_forwards_a_none_default_through_an_alias():
    source = (
        "from flightrisk.params import resolver as r\n"
        "def wrap(model, ranges=None):\n"
        "    return r.resolve_unit(model, 'u', {}, today_accepts=ranges)\n"
    )
    found = today_accepts_problems(source, "flightrisk/x.py")
    assert len(found) == 1 and "wrapper wrap" in found[0] and ":3:" in found[0]


def test_the_rule_ignores_the_parameters_default_only_when_it_is_today_accepts():
    source = (
        "from flightrisk.params.resolver import resolve_unit\n"
        "from flightrisk.params.today import today_accepts\n"
        "def wrap(a=today_accepts(), b=None):\n"
        "    resolve_unit('m', 'u', {}, today_accepts=a)\n"
        "    resolve_unit('m', 'u', {}, today_accepts=b)\n"
    )
    found = today_accepts_problems(source, "flightrisk/x.py")
    assert len(found) == 1 and ":5:" in found[0]
