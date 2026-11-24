"""Today's accepted ranges have a runtime home (``flightrisk/params/today.py``) that matches the golden file.

Runtime code never reads ``tests/golden``, so the edge passes ``TODAY_ACCEPTS`` to ``resolve_unit`` at
start. These tests keep it equal to what the golden file holds, check that the generator would write the
committed file again and refuses to overwrite it, and check that what the edge newly refuses with it is
exactly the pinned ``ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT`` set.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from flightrisk import edge_device
from flightrisk.edge_resolution import PARAMS_FILE_ENV_VAR, EdgeStartRefused, choose_model, resolve_edge
from flightrisk.params import resolver as resolver_module
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


# --- TODAY_ACCEPTS is read-only, so no caller can loosen it (SEAM-10, XCb review finding 1) ---------------


def _tello_start(environ: dict[str, str], tmp_path, **kw):
    environ = {PARAMS_FILE_ENV_VAR: str(tmp_path / "no-params.json"), **environ}
    choice = choose_model(drone="tello", drone_id="drone-1", environ=environ, load_inventory=lambda: None)
    return resolve_edge(choice=choice, drone="tello", drone_id="drone-1", source="tello", environ=environ,
                        parser=edge_device._build_parser(), load_inventory=lambda: None, **kw)


def test_today_accepts_is_read_only_at_every_level():
    name = next(n for n, per in TODAY_ACCEPTS.items() if per)
    with pytest.raises(AttributeError):
        TODAY_ACCEPTS.clear()  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        TODAY_ACCEPTS["FLIGHTRISK_CEILING_CM"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        del TODAY_ACCEPTS[name]  # type: ignore[attr-defined]
    for per in TODAY_ACCEPTS.values():
        assert isinstance(per, MappingProxyType)
    with pytest.raises(TypeError):
        TODAY_ACCEPTS[name][next(iter(TODAY_ACCEPTS[name]))] = None  # type: ignore[index]


@pytest.mark.parametrize(("name", "raw"), [("FLIGHTRISK_CEILING_CM", "5000"), ("FLIGHTRISK_GEOFENCE_CM", "-5")])
def test_a_cleared_copy_cannot_loosen_a_tello_refusal(tmp_path, name, raw):
    copy = dict(TODAY_ACCEPTS)
    copy.clear()
    assert len(TODAY_ACCEPTS) == len(today_accepts_from_golden(GOLDEN))
    with pytest.raises(EdgeStartRefused):
        _tello_start({name: raw}, tmp_path)
    for loose in (copy, {}, None):
        with pytest.raises(ValueError, match="today_accepts"):
            _tello_start({name: raw}, tmp_path, today_accepts=loose)


# --- Every runtime resolver call passes TODAY_ACCEPTS (SEAM-10; folded in from XCb, review findings 1 and 2) ---
#
# The rule is flow-insensitive and conservative. A name passed as today_accepts must be bound, everywhere in
# its file and in every scope, only to flightrisk.params.today.TODAY_ACCEPTS (by import or plain assignment)
# or be a keyword-only parameter named today_accepts that defaults to it. Any other binding of that name in
# the file (an assignment of anything else, a global or nonlocal declaration, a class-body or match-capture
# binding, a loop or with target, a def, an except name) fails. A name that some module binds to a resolver
# function, the resolver module or TODAY_ACCEPTS becomes an alias, so a call through a re-export shim in
# another module is checked as a resolver call.
#
# Not covered statically: importlib.import_module, __import__, sys.modules lookups, exec and eval, getattr
# with a computed name, and ** arguments passed to a wrapper defined in another module. resolve_edge, the
# only wrapper today, also refuses None and an empty map at run time.

RESOLVER = "flightrisk.params.resolver"
TODAY_NAME = "flightrisk.params.today.TODAY_ACCEPTS"
REACHING = {"flightrisk", "flightrisk.params", RESOLVER, "flightrisk.params.today"}
EXEMPT = {"flightrisk/params/resolver.py"}


def _targets() -> set[str]:
    names: set[str] = set()
    for fn_name, fn in inspect.getmembers(resolver_module, inspect.isfunction):
        if fn.__module__ == RESOLVER and "today_accepts" in inspect.signature(fn).parameters:
            names |= {f"{RESOLVER}.{fn_name}", f"flightrisk.params.{fn_name}"}
    return names


TARGETS = _targets()


def _module_of(rel_path: str) -> tuple[str, str]:
    parts = rel_path[:-3].split("/")
    package = ".".join(parts[:-1])
    return (package if parts[-1] == "__init__" else ".".join(parts)), package


def _absolute(node: ast.ImportFrom, package: str) -> str | None:
    if not node.level:
        return node.module
    parts = package.split(".") if package else []
    if node.level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else [])) or None


def _bindings(tree: ast.AST, package: str) -> dict[str, list[tuple]]:
    """Every binding of every name in the file, in any scope: (kind, detail, node)."""
    found: dict[str, list[tuple]] = {}
    handled: set[int] = set()

    def add(name, kind, detail, node):
        found.setdefault(name, []).append((kind, detail, node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.asname or a.name.split(".")[0], "import", a.name if a.asname else a.name.split(".")[0], node)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            for a in node.names:
                if a.name == "*":
                    add("*", "star", module, node)
                else:
                    add(a.asname or a.name, "import", f"{module}.{a.name}" if module else None, node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and node.value is not None:
                    add(t.id, "value", node.value, node)
                    handled.add(id(t))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
            for a, d in zip(positional, defaults):
                add(a.arg, "param", (False, d), node)
            for a, d in zip(args.kwonlyargs, args.kw_defaults):
                add(a.arg, "param", (True, d), node)
            for a in (args.vararg, args.kwarg):
                if a is not None:
                    add(a.arg, "param", (False, None), node)
            if not isinstance(node, ast.Lambda):
                add(node.name, "other", "def", node)
        elif isinstance(node, ast.ClassDef):
            add(node.name, "other", "class", node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                add(name, "other", type(node).__name__.lower(), node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            add(node.name, "other", "except", node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            add(node.name, "other", "match capture", node)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            add(node.rest, "other", "match capture", node)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load) and id(node) not in handled:
            add(node.id, "other", "store", node)
    return found


def _canon(dotted: str | None, aliases: dict[str, str]) -> str | None:
    for _ in range(10):
        if dotted is None:
            return None
        hit = next((a for a in sorted(aliases, key=len, reverse=True)
                    if dotted == a or dotted.startswith(a + ".")), None)
        if hit is None:
            return dotted
        dotted = aliases[hit] + dotted[len(hit):]
    return dotted


def _relevant(target: str | None) -> bool:
    return bool(target) and (target in TARGETS or target in REACHING or target == TODAY_NAME)


def _file_aliases(text: str, rel_path: str, aliases: dict[str, str]) -> dict[str, str]:
    """Names this module binds by import to a resolver function, a reaching module or TODAY_ACCEPTS."""
    module, package = _module_of(rel_path)
    found = {}
    for name, binds in _bindings(ast.parse(text), package).items():
        for kind, detail, _ in binds:
            target = _canon(detail, aliases) if kind == "import" else None
            if _relevant(target) and f"{module}.{name}" != target:
                found[f"{module}.{name}"] = target
    return found


def today_accepts_problems(text: str, rel_path: str, aliases: dict[str, str] | None = None) -> list[str]:
    """Problems with how one source file calls the resolver; each names the file, the line and the reason."""
    aliases = aliases or {}
    _, package = _module_of(rel_path)
    tree = ast.parse(text.lstrip("\ufeff"), filename=rel_path)
    binds = _bindings(tree, package)
    problems: list[str] = []

    def problem(node, reason):
        problems.append(f"{rel_path}:{getattr(node, 'lineno', 0)}: {reason}")

    resolved: dict[str, str] = {}
    for name, entries in binds.items():
        if name == "*":
            for _, module, node in entries:
                if _relevant(_canon(module, aliases)):
                    problem(node, "star import of the resolver hides calls from the rule")
            continue
        imports = {_canon(d, aliases) for k, d, _ in entries if k == "import"}
        clean = len(imports) == 1 and all(k == "import" for k, _, _ in entries)
        if clean:
            resolved[name] = next(iter(imports))
        elif any(_relevant(t) for t in imports):
            problem(entries[0][2], f"ambiguous binding of {name}")

    def dotted(node):
        if isinstance(node, ast.Name):
            return resolved.get(node.id)
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return _canon(f"{base}.{node.attr}", aliases) if base else None
        return None

    def good(value, seen=frozenset()):
        if dotted(value) == TODAY_NAME:
            return True
        if not isinstance(value, ast.Name) or value.id in seen or value.id not in binds:
            return False
        for kind, detail, _ in binds[value.id]:
            if kind == "value" and good(detail, seen | {value.id}):
                continue
            if kind == "import" and _canon(detail, aliases) == TODAY_NAME:
                continue
            if kind == "param" and detail[0] and value.id == "today_accepts" and detail[1] is not None \
                    and good(detail[1], seen | {value.id}):
                continue
            return False
        return True

    func_ids = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            is_target = dotted(node.func) in TARGETS
            stars = [kw for kw in node.keywords if kw.arg is None]
            keyword = next((kw for kw in node.keywords if kw.arg == "today_accepts"), None)
            if is_target and stars:
                problem(node, "call passes ** arguments, so today_accepts cannot be checked")
            for kw in stars:
                if isinstance(kw.value, ast.Dict) and any(
                        isinstance(k, ast.Constant) and k.value == "today_accepts" for k in kw.value.keys):
                    problem(node, "today_accepts passed through a dict display with **")
            if keyword is not None and isinstance(node.func, ast.Name) and node.func.id == "dict":
                problem(node, "today_accepts passed through dict(), which hides it from the rule")
            elif keyword is not None and not good(keyword.value):
                value = keyword.value
                if isinstance(value, ast.Constant) and value.value is None:
                    problem(node, "today_accepts is None, which checks against the target bounds")
                elif isinstance(value, ast.Dict) and not value.keys:
                    problem(node, "today_accepts is an empty map, which checks against the target bounds")
                else:
                    problem(node, f"today_accepts must be {TODAY_NAME}, got {type(value).__name__}")
            elif keyword is None and is_target and not stars:
                problem(node, "call does not name the today_accepts keyword (default None)")
            if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) > 1:
                name = node.args[1]
                if not isinstance(name, ast.Constant) and _relevant(dotted(node.args[0])):
                    problem(node, "getattr with a computed name on the resolver hides the call from the rule")
                elif isinstance(name, ast.Constant) and any(t.rsplit(".", 1)[1] == name.value for t in TARGETS):
                    problem(node, f"getattr of {name.value} hides the call from the rule")
        elif isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in func_ids:
            if isinstance(node.ctx, ast.Load) and dotted(node) in TARGETS:
                problem(node, "resolver function used other than as the function of a direct call")
    return problems


def repo_problems(sources: dict[str, str]) -> list[str]:
    """Run the rule over ``{rel_path: text}``, following names re-exported from one module to another."""
    aliases: dict[str, str] = {}
    for _ in range(10):
        grown = dict(aliases)
        for rel, text in sources.items():
            grown.update(_file_aliases(text, rel, grown))
        if grown == aliases:
            break
        aliases = grown
    problems: list[str] = []
    for rel, text in sorted(sources.items()):
        problems += today_accepts_problems(text, rel, aliases)
    return problems


def test_the_resolver_functions_that_take_today_accepts_include_resolve_unit():
    assert {f"{RESOLVER}.resolve_unit", f"{RESOLVER}.resolve"} <= TARGETS


def test_every_runtime_call_of_the_resolver_passes_today_accepts():
    files = sorted((ROOT / "flightrisk").rglob("*.py"))
    sources = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8").lstrip("\ufeff") for p in files}
    sources = {rel: text for rel, text in sources.items() if rel not in EXEMPT}
    assert len(sources) > 50 and any(rel.startswith("flightrisk/params/") for rel in sources)
    assert "flightrisk/edge_resolution.py" in sources
    assert repo_problems(sources) == []


def test_scanning_params_flags_only_resolver_py_when_it_is_not_exempt():
    text = (ROOT / "flightrisk" / "params" / "resolver.py").read_text(encoding="utf-8")
    assert today_accepts_problems(text, "flightrisk/params/resolver.py")


_IMPORTS = ("from flightrisk.params.resolver import resolve_unit\n"
            "from flightrisk.params.today import TODAY_ACCEPTS\n")

GOOD_CALLS = [
    _IMPORTS + "resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n",
    "from flightrisk.params import resolver as r, today as t\n"
    "r.resolve_unit('m', 'u', {}, today_accepts=t.TODAY_ACCEPTS)\n",
    "import flightrisk.params.resolver\nimport flightrisk.params.today\n"
    "flightrisk.params.resolver.resolve('m', 'u', {}, today_accepts=flightrisk.params.today.TODAY_ACCEPTS)\n",
    _IMPORTS + "R = TODAY_ACCEPTS\ndef f():\n    return resolve_unit('m', 'u', {}, today_accepts=R)\n",
    _IMPORTS + "def wrap(*, today_accepts=TODAY_ACCEPTS):\n"
    "    return resolve_unit('m', 'u', {}, today_accepts=today_accepts)\nwrap()\n",
    "def f():\n    from flightrisk.params.resolver import resolve_unit\n"
    "    from flightrisk.params.today import TODAY_ACCEPTS\n"
    "    return resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n",
    "def other(resolve_unit):\n    return 1\n",
]

BAD_CALLS = [
    ("from flightrisk.params.resolver import resolve_unit\nresolve_unit('m', 'u', {})\n", "does not name"),
    (_IMPORTS + "resolve_unit('m', 'u', {}, today_accepts=None)\n", "is None"),
    (_IMPORTS + "resolve_unit('m', 'u', {}, today_accepts={})\n", "empty map"),
    (_IMPORTS + "def f(**kw):\n    return resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS, **kw)\n",
     "** arguments"),
    (_IMPORTS + "resolve_unit('m', 'u', {}, **{'today_accepts': TODAY_ACCEPTS})\n", "dict display"),
    (_IMPORTS + "def wrap(ranges=TODAY_ACCEPTS):\n    return resolve_unit('m', 'u', {}, today_accepts=ranges)\n",
     "must be"),
    (_IMPORTS + "def wrap(*, today_accepts=TODAY_ACCEPTS):\n"
     "    return resolve_unit('m', 'u', {}, today_accepts=today_accepts)\nwrap(today_accepts=None)\n", "is None"),
    (_IMPORTS + "f = resolve_unit\n", "other than as the function"),
    ("from flightrisk.params import resolver\nf = getattr(resolver, 'resolve_unit')\n", "getattr"),
    ("from flightrisk.params import resolver\nn = 'resolve_unit'\nf = getattr(resolver, n)\n", "computed name"),
    ("from flightrisk.params.resolver import *\n", "star import"),
    # The bypasses of the XCb review, finding 1.
    (_IMPORTS + "R = TODAY_ACCEPTS\ndef poison():\n    global R\n    R = None\n"
     "resolve_unit('m', 'u', {}, today_accepts=R)\n", "must be"),
    (_IMPORTS + "def outer():\n    r = TODAY_ACCEPTS\n    def poison():\n        nonlocal r\n        r = {}\n"
     "    poison()\n    return resolve_unit('m', 'u', {}, today_accepts=r)\n", "must be"),
    (_IMPORTS + "R = TODAY_ACCEPTS\nclass C:\n    R = None\n    x = resolve_unit('m', 'u', {}, today_accepts=R)\n",
     "must be"),
    (_IMPORTS + "class C:\n    TODAY_ACCEPTS = None\n"
     "    x = resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n", "ambiguous binding of TODAY_ACCEPTS"),
    (_IMPORTS + "R = TODAY_ACCEPTS\nmatch 1:\n    case R:\n        pass\n"
     "resolve_unit('m', 'u', {}, today_accepts=R)\n", "must be"),
    (_IMPORTS + "match {}:\n    case TODAY_ACCEPTS:\n        pass\n"
     "resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n", "ambiguous binding of TODAY_ACCEPTS"),
]


@pytest.mark.parametrize("source", GOOD_CALLS)
def test_the_rule_accepts_good_forms(source):
    assert today_accepts_problems(source, "flightrisk/drone/x.py") == []


@pytest.mark.parametrize(("source", "reason"), BAD_CALLS)
def test_the_rule_rejects_bad_forms(source, reason):
    found = today_accepts_problems(source, "flightrisk/drone/x.py")
    assert any(reason in line for line in found), found


@pytest.mark.parametrize("use", [
    "from flightrisk.drone.shim import ru\nru('m', 'u', {})\n",
    "from flightrisk.drone import shim\nshim.ru('m', 'u', {}, today_accepts=None)\n",
    "import flightrisk.drone.shim as s\ns.ru('m', 'u', {})\n",
    "from flightrisk.drone.shim import r2\nr2('m', 'u', {})\n",
])
def test_the_rule_follows_a_re_export_shim_into_the_calling_module(use):
    sources = {
        "flightrisk/drone/shim.py": "from flightrisk.params.resolver import resolve_unit as ru\n",
        "flightrisk/drone/shim2.py": "",
        "flightrisk/drone/user.py": use,
    }
    if "r2" in use:
        sources["flightrisk/drone/shim.py"] += "from flightrisk.drone.shim2 import ru as r2\n"
        sources["flightrisk/drone/shim2.py"] = "from flightrisk.params.resolver import resolve_unit as ru\n"
    found = repo_problems(sources)
    assert any(line.startswith("flightrisk/drone/user.py:") for line in found), found
    assert repo_problems({**sources, "flightrisk/drone/user.py": ""}) == []
