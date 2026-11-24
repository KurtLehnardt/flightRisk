"""On the ground, only ``flightrisk/ground_params.py`` calls the resolver, and always with today's ranges (SEAM-10).

The rule covers every file in the ``"A4"`` and ``"X-legacy"`` sections of ``tests/param_adopters.py``, plus
``flightrisk/ground_params.py`` while the ledger does not name it yet. In those files:

* only ``flightrisk/ground_params.py`` calls ``resolve_unit`` or ``resolve`` (or any other resolver function
  that takes ``today_accepts``), under any import name: a plain or aliased import of the function, the
  resolver module under any name, the full dotted path, or a relative import;
* each such call passes the keyword ``today_accepts`` whose value is the bare name ``TODAY_ACCEPTS``;
* that name is bound exactly once in the module, by a module-level
  ``from flightrisk.params.today import TODAY_ACCEPTS`` with no alias, so it is never rebound;
* no call anywhere passes ``today_accepts=None``, and no ``**`` mapping is passed to a resolver call;
* the resolver functions are never used other than as the function of a direct call (no assignment,
  argument, ``getattr`` or star import), so no call escapes the rule under another name.

``None`` or a missing keyword would check environment and flag values against the target bounds, which
accept inputs today's readers refuse. Not covered statically: ``importlib.import_module``, ``__import__``,
``sys.modules``, ``exec`` and ``eval``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from flightrisk.params import resolver as resolver_module
from tests.param_adopters import PARAM_ADOPTERS

ROOT = Path(__file__).resolve().parent.parent
GROUND_PARAMS = "flightrisk/ground_params.py"
SECTIONS = ("A4", "X-legacy")
RESOLVER = "flightrisk.params.resolver"
TODAY_MODULE = "flightrisk.params.today"
TODAY_NAME = "TODAY_ACCEPTS"
KEYWORD = "today_accepts"

ONLY_GROUND = f"only {GROUND_PARAMS} may call the resolver"
NO_KEYWORD = f"does not pass the keyword {KEYWORD}"
IS_NONE = f"{KEYWORD}=None"
NOT_BARE = f"{KEYWORD} must be the bare name {TODAY_NAME}"
NOT_IMPORTED = f"{TODAY_NAME} is not imported at module level from {TODAY_MODULE}"
REBOUND = f"{TODAY_NAME} is rebound"
STARS = "passes a ** mapping to the resolver"
INDIRECT = "resolver function used other than as the function of a direct call"
GETATTR = "getattr on the resolver hides a call"
STAR_IMPORT = "star import of the resolver hides its calls"
AMBIGUOUS = "is bound to the resolver by an import and also bound otherwise"


def _target_functions() -> frozenset[str]:
    """``resolve``, ``resolve_unit`` and any other resolver function that takes ``today_accepts``."""
    names = {"resolve", "resolve_unit"}
    for name, fn in inspect.getmembers(resolver_module, inspect.isfunction):
        if fn.__module__ == RESOLVER and KEYWORD in inspect.signature(fn).parameters:
            names.add(name)
    return frozenset(names)


TARGET_FUNCTIONS = _target_functions()
TARGETS = frozenset(f"{module}.{name}" for name in TARGET_FUNCTIONS for module in (RESOLVER, "flightrisk.params"))
#: Modules through which an attribute chain can reach a resolver function.
REACHING = frozenset({"flightrisk", "flightrisk.params", RESOLVER})


def scanned_files() -> list[str]:
    """The ledger's A4 and X-legacy sections, plus ground_params.py."""
    paths = {GROUND_PARAMS}
    for section in SECTIONS:
        paths |= set(PARAM_ADOPTERS[section])
    return sorted(paths)


def _package(rel_path: str) -> str:
    parts = rel_path[:-3].split("/")
    return ".".join(parts if parts[-1] == "__init__" else parts[:-1])


def _absolute(node: ast.ImportFrom, package: str) -> str | None:
    if not node.level:
        return node.module
    parts = package.split(".") if package else []
    if node.level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else [])) or None


def _bindings(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """Every binding of a name in the file, in any scope, with the node that binds it."""
    found: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found += [(a.asname or a.name.split(".")[0], node) for a in node.names if a.name != "*"]
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append((node.id, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, node))
        elif isinstance(node, ast.arg):
            found.append((node.arg, node))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found += [(name, node) for name in node.names]
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            found.append((node.name, node))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            found.append((node.rest, node))
    return found


def _imports(tree: ast.AST, package: str) -> tuple[dict[str, set[str]], list[tuple[str | None, ast.AST]]]:
    """``{name: dotted targets an import binds it to}`` and the star imports, in any scope."""
    names: dict[str, set[str]] = {}
    stars: list[tuple[str | None, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                # ``import a.b.c`` binds ``a``; ``import a.b.c as x`` binds ``x`` to ``a.b.c``.
                target = a.name if a.asname else a.name.split(".")[0]
                names.setdefault(a.asname or target, set()).add(target)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            for a in node.names:
                if a.name == "*":
                    stars.append((module, node))
                elif module:
                    names.setdefault(a.asname or a.name, set()).add(f"{module}.{a.name}")
    return names, stars


def _relevant(dotted: str) -> bool:
    return dotted in TARGETS or dotted in REACHING


def problems_in(text: str, rel_path: str) -> list[str]:
    """Every breach of the rule in one source file; each names the file, the line and the reason."""
    tree = ast.parse(text.lstrip("\ufeff"), filename=rel_path)
    imported, stars = _imports(tree, _package(rel_path))
    bindings = _bindings(tree)
    problems: list[str] = []

    def problem(node: ast.AST, reason: str) -> None:
        problems.append(f"{rel_path}:{getattr(node, 'lineno', 0)}: {reason}")

    def dotted(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return imported.get(node.id, set())
        if isinstance(node, ast.Attribute):
            return {f"{base}.{node.attr}" for base in dotted(node.value)}
        return set()

    for module, node in stars:
        if module and any(t.startswith(module + ".") for t in TARGETS):
            problem(node, STAR_IMPORT)
    for name, targets in imported.items():
        other = [node for bound, node in bindings
                 if bound == name and not isinstance(node, (ast.Import, ast.ImportFrom))]
        if other and any(_relevant(t) for t in targets):
            problem(other[0], f"{name} {AMBIGUOUS}")

    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    funcs = {id(call.func) for call in calls}
    passes_today_name = []
    for call in calls:
        is_target = bool(dotted(call.func) & TARGETS)
        keyword = next((kw for kw in call.keywords if kw.arg == KEYWORD), None)
        if keyword is not None and isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
            problem(call, IS_NONE)
        if isinstance(call.func, ast.Name) and call.func.id == "getattr" and len(call.args) > 1:
            name = call.args[1]
            if any(t in REACHING for t in dotted(call.args[0])) and not (
                    isinstance(name, ast.Constant) and name.value not in TARGET_FUNCTIONS):
                problem(call, GETATTR)
        if not is_target:
            continue
        if rel_path != GROUND_PARAMS:
            problem(call, ONLY_GROUND)
        if any(kw.arg is None for kw in call.keywords):
            problem(call, STARS)
        if keyword is None:
            problem(call, NO_KEYWORD)
        elif isinstance(keyword.value, ast.Name) and keyword.value.id == TODAY_NAME:
            passes_today_name.append(call)
        elif not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None):
            problem(call, NOT_BARE)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load) \
                and id(node) not in funcs and dotted(node) & TARGETS:
            problem(node, INDIRECT)

    if passes_today_name:
        today_bindings = [node for bound, node in bindings if bound == TODAY_NAME]
        module_import = [
            node for node in tree.body
            if isinstance(node, ast.ImportFrom) and _absolute(node, _package(rel_path)) == TODAY_MODULE
            and any(a.name == TODAY_NAME and a.asname is None for a in node.names)
        ]
        if not module_import:
            problem(passes_today_name[0], NOT_IMPORTED)
        for node in today_bindings:
            if node not in module_import[:1]:
                problem(node, REBOUND)
    return problems


def resolver_calls(text: str, rel_path: str) -> int:
    """How many direct resolver calls a file makes, under any import name."""
    tree = ast.parse(text, filename=rel_path)
    imported, _ = _imports(tree, _package(rel_path))

    def dotted(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return imported.get(node.id, set())
        if isinstance(node, ast.Attribute):
            return {f"{base}.{node.attr}" for base in dotted(node.value)}
        return set()

    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call) and dotted(node.func) & TARGETS)


# --- The repository -----------------------------------------------------------------------------------------


def test_the_rule_covers_resolve_and_resolve_unit():
    assert {"resolve", "resolve_unit"} <= TARGET_FUNCTIONS
    assert f"{RESOLVER}.resolve_unit" in TARGETS


def test_the_scanned_files_are_the_a4_and_x_legacy_sections_plus_ground_params():
    files = scanned_files()
    assert GROUND_PARAMS in files
    assert set(PARAM_ADOPTERS["X-legacy"]) | set(PARAM_ADOPTERS["A4"]) <= set(files)
    assert all((ROOT / rel).is_file() for rel in files)


def test_only_ground_params_calls_the_resolver_and_always_with_today_accepts():
    sources = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in scanned_files()}
    problems = [line for rel, text in sources.items() for line in problems_in(text, rel)]
    assert problems == []
    assert resolver_calls(sources[GROUND_PARAMS], GROUND_PARAMS) >= 1


# --- Self-tests on synthetic sources ------------------------------------------------------------------------

_RESOLVER_IMPORT = "from flightrisk.params.resolver import resolve_unit\n"
_TODAY_IMPORT = "from flightrisk.params.today import TODAY_ACCEPTS\n"
_IMPORTS = _RESOLVER_IMPORT + _TODAY_IMPORT
_CALL = "resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n"
_GOOD = _IMPORTS + _CALL
OTHER = "flightrisk/drone/x.py"

GOOD_SOURCES = [
    (GROUND_PARAMS, _GOOD),
    (GROUND_PARAMS, "from flightrisk.params import resolver as r\n" + _TODAY_IMPORT
     + "r.resolve('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n"),
    (GROUND_PARAMS, "import flightrisk.params.resolver\n" + _TODAY_IMPORT
     + "flightrisk.params.resolver.resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n"),
    (GROUND_PARAMS, ("from .params.resolver import resolve_unit as ru\nfrom .params.today import TODAY_ACCEPTS\n"
                     "ru('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n")),
    (GROUND_PARAMS, _IMPORTS + "def f(unit):\n    return resolve_unit('m', unit, {}, today_accepts=TODAY_ACCEPTS)\n"),
    (OTHER, "from flightrisk.params.resolver import ParamRefused, resolve_unit\nx = ParamRefused\n"),
    (OTHER, "def resolve(x):\n    return x\nresolve(1)\n"),
    (OTHER, "from flightrisk.params import resolver\nf = getattr(resolver, 'ParamRefused')\n"),
]


@pytest.mark.parametrize(("rel_path", "source"), GOOD_SOURCES)
def test_the_rule_accepts_good_forms(rel_path, source):
    assert problems_in(source, rel_path) == []


_REBINDINGS = [
    "TODAY_ACCEPTS = None\n",
    "TODAY_ACCEPTS |= {}\n",
    "del TODAY_ACCEPTS\n",
    "def f():\n    TODAY_ACCEPTS = {}\n",
    "def f():\n    global TODAY_ACCEPTS\n",
    "def f():\n    x = 1\n    def g():\n        nonlocal x\n    TODAY_ACCEPTS = x\n",
    "for TODAY_ACCEPTS in ():\n    pass\n",
    "def f(TODAY_ACCEPTS=None):\n    return TODAY_ACCEPTS\n",
    "f = lambda TODAY_ACCEPTS: 0\n",
    "x = [0 for TODAY_ACCEPTS in ()]\n",
    "with open('x') as TODAY_ACCEPTS:\n    pass\n",
    "try:\n    pass\nexcept ValueError as TODAY_ACCEPTS:\n    pass\n",
    "class C:\n    TODAY_ACCEPTS = None\n",
    "class TODAY_ACCEPTS:\n    pass\n",
    "def TODAY_ACCEPTS():\n    return {}\n",
    "if (TODAY_ACCEPTS := None):\n    pass\n",
    "match 1:\n    case TODAY_ACCEPTS:\n        pass\n",
    "match {}:\n    case {**TODAY_ACCEPTS}:\n        pass\n",
    "import os as TODAY_ACCEPTS\n",
    _TODAY_IMPORT,
]

BAD_SOURCES = [
    # Calls from any other file, under any import name.
    (OTHER, _GOOD, ONLY_GROUND),
    (OTHER, "from flightrisk.params.resolver import resolve as r\n" + _TODAY_IMPORT
     + "r('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n", ONLY_GROUND),
    (OTHER, "import flightrisk.params.resolver as res\n" + _TODAY_IMPORT
     + "res.resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n", ONLY_GROUND),
    (OTHER, "import flightrisk.params.resolver\n" + _TODAY_IMPORT
     + "flightrisk.params.resolver.resolve('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n", ONLY_GROUND),
    (OTHER, "import flightrisk\nflightrisk.params.resolver.resolve('m', 'u', {})\n", ONLY_GROUND),
    (OTHER, "from ..params import resolver\nresolver.resolve_unit('m', 'u', {})\n", ONLY_GROUND),
    (OTHER, "def f():\n    from flightrisk.params.resolver import resolve_unit\n"
     + "    return resolve_unit('m', 'u', {})\n", ONLY_GROUND),
    # The keyword and its value.
    (GROUND_PARAMS, _IMPORTS + "resolve_unit('m', 'u', {})\n", NO_KEYWORD),
    (GROUND_PARAMS, _IMPORTS + "resolve_unit('m', 'u', {}, today_accepts=None)\n", IS_NONE),
    (OTHER, "def wrap(*, today_accepts=None):\n    return today_accepts\nwrap(today_accepts=None)\n", IS_NONE),
    (GROUND_PARAMS, _IMPORTS + "resolve_unit('m', 'u', {}, today_accepts={})\n", NOT_BARE),
    (GROUND_PARAMS, _IMPORTS + "R = TODAY_ACCEPTS\nresolve_unit('m', 'u', {}, today_accepts=R)\n", NOT_BARE),
    (GROUND_PARAMS, _RESOLVER_IMPORT + "from flightrisk.params import today\n"
     "resolve_unit('m', 'u', {}, today_accepts=today.TODAY_ACCEPTS)\n", NOT_BARE),
    (GROUND_PARAMS, _RESOLVER_IMPORT + "from flightrisk.params.today import TODAY_ACCEPTS as T\n"
     "resolve_unit('m', 'u', {}, today_accepts=T)\n", NOT_BARE),
    (GROUND_PARAMS, _RESOLVER_IMPORT + "from flightrisk.params import today\n"
     "resolve_unit('m', 'u', {}, today_accepts=dict(today.TODAY_ACCEPTS))\n", NOT_BARE),
    # Where TODAY_ACCEPTS comes from.
    (GROUND_PARAMS, _RESOLVER_IMPORT + _CALL, NOT_IMPORTED),
    (GROUND_PARAMS, _RESOLVER_IMPORT + "from flightrisk.params.resolver import TodayRange as TODAY_ACCEPTS\n" + _CALL,
     NOT_IMPORTED),
    (GROUND_PARAMS, _RESOLVER_IMPORT + "from flightrisk.params import today\nTODAY_ACCEPTS = today.TODAY_ACCEPTS\n"
     + _CALL, NOT_IMPORTED),
    (GROUND_PARAMS, _RESOLVER_IMPORT + "def f():\n    from flightrisk.params.today import TODAY_ACCEPTS\n"
     "    return resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n", NOT_IMPORTED),
    (GROUND_PARAMS, _RESOLVER_IMPORT + "try:\n    from flightrisk.params.today import TODAY_ACCEPTS\n"
     "except ImportError:\n    pass\n" + _CALL, NOT_IMPORTED),
    # ** mappings.
    (GROUND_PARAMS, _IMPORTS + "def f(**kw):\n"
     "    return resolve_unit('m', 'u', {}, today_accepts=TODAY_ACCEPTS, **kw)\n", STARS),
    (GROUND_PARAMS, _IMPORTS + "resolve_unit('m', 'u', {}, **{'today_accepts': TODAY_ACCEPTS})\n", STARS),
    # Uses that would hide a call from the rule.
    (GROUND_PARAMS, _IMPORTS + "f = resolve_unit\n", INDIRECT),
    (OTHER, "from flightrisk.params import resolver\ncallbacks = [resolver.resolve]\n", INDIRECT),
    (OTHER, "from flightrisk.params import resolver\nf = getattr(resolver, 'resolve_unit')\n", GETATTR),
    (OTHER, "from flightrisk.params import resolver\nname = 'resolve'\nf = getattr(resolver, name)\n", GETATTR),
    (OTHER, "from flightrisk.params.resolver import *\n", STAR_IMPORT),
    (OTHER, "from flightrisk.params import resolver\nresolver = object()\n", AMBIGUOUS),
] + [(GROUND_PARAMS, _GOOD + rebinding, REBOUND) for rebinding in _REBINDINGS]


@pytest.mark.parametrize(("rel_path", "source", "reason"), BAD_SOURCES)
def test_the_rule_rejects_bad_forms(rel_path, source, reason):
    found = problems_in(source, rel_path)
    assert any(reason in line for line in found), found


def test_a_rebinding_is_reported_at_its_own_line():
    found = problems_in(_GOOD + "\n\nTODAY_ACCEPTS = None\n", GROUND_PARAMS)
    assert found == [f"{GROUND_PARAMS}:6: {REBOUND}"]


def test_resolver_calls_counts_calls_under_any_name():
    source = ("from flightrisk.params import resolver as r\nimport flightrisk.params.resolver\n" + _IMPORTS
              + _CALL + "r.resolve('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n"
              + "flightrisk.params.resolver.resolve('m', 'u', {}, today_accepts=TODAY_ACCEPTS)\n")
    assert resolver_calls(source, GROUND_PARAMS) == 3
    assert resolver_calls("from flightrisk.params.resolver import ParamRefused\n", GROUND_PARAMS) == 0
