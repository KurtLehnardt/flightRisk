"""Literal guard (issue #197, design section 22, requirements GRD-1 to GRD-6).

Ported from ``docs/design/hardware-profiles/guard_rule_prototype.py.txt`` and extended with rule G
(dict displays) and literal folding (design detail section 22). Name words come from the
registry ids in ``flightrisk.params.table``. A finding is keyed
``(file, enclosing scope, kind, callee or target, name, value)`` and every key must be in
``tests/param_literal_allowlist.py`` with its exact count. Scope: ``SCOPE_GLOBS``; ``params/``,
``hardware/`` and ``tests/`` are excluded by not being listed.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

UNIT_SUFFIX = r"(?:_|^)(cm|mm|m|km|s|ms|deg|dps|mps|pct|percent|c|hz|cm_s)$"
UNIT_TOKENS = {"cm", "mm", "m", "km", "s", "ms", "deg", "dps", "mps", "pct", "c", "hz", "per", "list"}
EXTRA_WORDS = {"timeout", "delay", "interval", "budget", "lag", "retries", "retry", "attempts",
               "readings", "samples", "votes", "tolerance", "threshold", "radius", "width", "depth",
               "speed", "distance", "gap", "offset", "height", "alt", "altitude", "fov", "pitch"}
SAFETY_WORDS = ("failsafe", "require", "sensing", "hover", "avoid", "refuse", "obstacle", "geofence",
                "ceiling", "land")
EXEMPT_NAME = re.compile(r"(_px|_bytes|_chars|_len)$|^(max_depth|frame_height|frame_width|match_threshold)$",
                         re.IGNORECASE)
EXEMPT_STR_NAME = re.compile(r"^(env_|event_)|(_env|_env_var|_detail|_message|_text|_note|_reason|_reasons|"
                             r"_label|_key|_keys|_field|_fields|_prefix|_suffix|_title|_help|_doc)$", re.IGNORECASE)
SCOPE_GLOBS = [
    "flightrisk/drone/*.py",
    "flightrisk/edge_device.py",
    "flightrisk/edge_profiles.py",
    "flightrisk/launcher/*.py",
    "flightrisk/dashboard/*.py",
    "flightrisk/fleet_protocol/constants.py",
]


def words_from_registry(ids=None) -> set[str]:
    """Name words: every dotted, underscore-separated id token of length >= 3 that is not a unit."""
    if ids is None:
        from flightrisk.params.table import TABLE

        ids = [row.id for row in TABLE]
    words = set()
    for pid in ids:
        for tok in re.split(r"[._]", pid):
            if len(tok) >= 3 and tok not in UNIT_TOKENS:
                words.add(tok)
    return words | EXTRA_WORDS


def build_unit_re(words: set[str]) -> re.Pattern:
    return re.compile(UNIT_SUFFIX + "|" + "|".join(sorted(map(re.escape, words), key=len, reverse=True)),
                      re.IGNORECASE)


_FOLD = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
         ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b}


def _num(node):
    """A numeric literal, folding unary signs and arithmetic of literals (``2 * 100`` is 200)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _num(node.operand)
        return None if v is None else (-v if isinstance(node.op, ast.USub) else v)
    if isinstance(node, ast.BinOp) and type(node.op) in _FOLD:
        left, right = _num(node.left), _num(node.right)
        if left is not None and right is not None:
            try:
                return _FOLD[type(node.op)](left, right)
            except (ArithmeticError, ValueError):
                return None
    return None


def _numstr(node):
    v = _num(node)
    if v is not None:
        return v
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", node.value):
        return node.value
    return None


def _nums(node):
    v = _num(node)
    if v is not None:
        return [v]
    if isinstance(node, ast.Tuple):
        return [x for x in (_num(e) for e in node.elts) if x is not None]
    return []


def _name(t):
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return t.attr
    return None


def _callee(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "?")


class Scanner(ast.NodeVisitor):
    def __init__(self, rel, unit_re):
        self.rel, self.unit_re, self.scope, self.out = rel, unit_re, ["<module>"], []
        self.kinds = ["module"]

    def unit(self, name):
        return bool(name) and bool(self.unit_re.search(name)) and not EXEMPT_NAME.search(name)

    def safety(self, name):
        low = (name or "").lower()
        return any(w in low for w in SAFETY_WORDS) and not EXEMPT_STR_NAME.search(low)

    def add(self, kind, target, name, value, line):
        if value == 0 or value == "0":
            return
        self.out.append(((self.rel, ".".join(self.scope), kind, target, name, value), line))

    def _scoped(self, node, kind):
        self.scope.append(node.name)
        self.kinds.append(kind)
        self.generic_visit(node)
        self.kinds.pop()
        self.scope.pop()

    def visit_ClassDef(self, node):
        is_enum = any((_name(b) or "").endswith("Enum") for b in node.bases)
        self._scoped(node, "enum" if is_enum else "class")

    def visit_FunctionDef(self, node):
        a = node.args
        pos = a.posonlyargs + a.args
        pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults))
        pairs += [(k, d) for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
        for arg, d in pairs:
            if self.unit(arg.arg):
                for v in _nums(d):
                    self.add("default", node.name, arg.arg, v, d.lineno)
            if isinstance(d, ast.Constant) and isinstance(d.value, (bool, str)) and self.safety(arg.arg):
                self.add("safety_default", node.name, arg.arg, d.value, d.lineno)
        self._scoped(node, "function")

    visit_AsyncFunctionDef = visit_FunctionDef

    def _binding(self, target_name, value, line, where="function"):
        if self.unit(target_name):
            for v in _nums(value):
                self.add("assign", target_name, target_name, v, line)
            if isinstance(value, ast.Call) and _callee(value) == "field":
                for kw in value.keywords:
                    if kw.arg == "default":
                        for v in _nums(kw.value):
                            self.add("field", target_name, target_name, v, line)
        definition = where == "class" or (where == "module" and target_name.upper().startswith("DEFAULT_"))
        if (definition and isinstance(value, ast.Constant) and isinstance(value.value, (bool, str))
                and self.safety(target_name)):
            self.add("safety_literal", target_name, target_name, value.value, line)

    def visit_Assign(self, node):
        for t in node.targets:
            n = _name(t)
            if n:
                self._binding(n, node.value, node.lineno, self.kinds[-1])
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        n = _name(node.target)
        if n and node.value is not None:
            self._binding(n, node.value, node.lineno, self.kinds[-1])
        self.generic_visit(node)

    def visit_Call(self, node):
        callee = _callee(node)
        for kw in node.keywords:
            if kw.arg and kw.arg != "default" and self.unit(kw.arg):
                for v in _nums(kw.value):
                    self.add("kwarg", callee, kw.arg, v, kw.value.lineno)
        if callee == "add_argument" and node.args:
            opt = next((a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)), "")
            for kw in node.keywords:
                if kw.arg == "default" and self.unit(opt.lstrip("-").replace("-", "_")):
                    v = _numstr(kw.value)
                    if v is not None:
                        self.add("argparse_default", "add_argument", opt, v, kw.value.lineno)
        if callee in ("get", "getenv") and len(node.args) >= 2:
            key = node.args[0]
            keyname = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else _name(key)
            env_like = keyname and ("FLIGHTRISK_" in keyname or keyname.endswith(("_ENV", "_ENV_VAR")) or keyname.startswith("ENV_"))
            stem = (keyname or "").replace("FLIGHTRISK_", "").replace("_ENV_VAR", "").replace("_ENV", "").replace("ENV_", "")
            if env_like and self.unit(stem):
                v = _numstr(node.args[1])
                if v is not None:
                    self.add("env_default", callee, keyname, v, node.lineno)
        self.generic_visit(node)

    def visit_Dict(self, node):
        """(G) a numeric value under a unit-named string key: ``{"geofence_cm": 450}``."""
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str) and self.unit(k.value):
                for n in _nums(v):
                    self.add("dict", k.value, k.value, n, v.lineno)
        self.generic_visit(node)

    def visit_Compare(self, node):
        ops = [node.left, *node.comparators]
        if all(isinstance(o, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for o in node.ops):
            names = [_name(o) for o in ops if isinstance(o, (ast.Name, ast.Attribute))]
            hit = next((n for n in names if self.unit(n)), None)
            if hit:
                for o in ops:
                    v = _num(o)
                    if v is not None:
                        self.add("compare", "<compare>", hit, v, node.lineno)
        self.generic_visit(node)


REF_TARGET = re.compile(r"^(?:ast:(?P<path>[^:]+)|(?P<mod>[\w.]+)):(?P<attr>.+)$")


def _ref_names(ref: str):
    """Yield ``(file, name)`` for each code location a golden ``*_ref`` string points at."""
    for part in ref.split(" & "):
        m = REF_TARGET.match(part.strip())
        if not m:
            continue
        path = m["path"] or m["mod"].replace(".", "/") + ".py"
        attr = re.split(r"[\[*~]", m["attr"])[0]
        inner = re.search(r"\((\w+)\)", attr)
        yield path, inner.group(1) if inner else attr.split("(")[0].split(".")[-1]


def _abbreviates(short: str, word: str) -> bool:
    if not short or short[0] != word[:1]:
        return False
    rest = iter(word)
    return all(c in rest for c in short)


def _name_value_match(row, name, value) -> bool:
    """Rule 4: ``value`` is a default of ``row`` and ``name`` names the row.

    Names the row by its env or alias name, or by tokens: the last token of the name equals the last
    token of the row's leaf, and every other token of the name equals, or abbreviates (its letters in order, first letter equal),
    a token of the row id. Bool values and None never match; they are too common.
    """
    if value is None or isinstance(value, bool) or not isinstance(name, str):
        return False
    if not any(type(v) is type(value) and v == value for v in row.defaults.values()):
        return False
    lowered = name.lower().strip("_")
    if lowered in {n.lower() for n in (row.env, *(a.name for a in row.env_aliases)) if n}:
        return True
    tokens = lowered.split("_")
    row_tokens = re.split(r"[._]", row.id)
    if len(tokens) < 2 or tokens[-1] != row_tokens[-1]:
        return False
    return all(any(_abbreviates(t, rt) for rt in row_tokens) for t in tokens[:-1])


def registry_matches(allowlist) -> list[tuple[tuple, frozenset, str]]:
    """Allowlist keys that duplicate a registry row, with the categories each may carry.

    A key matches when (1) it defines a name that the golden file's ``today_ref`` (row value) or
    ``lo_ref``/``hi_ref`` (today's accepted range) points at, (2) it is the default of an
    environment name or flag that a row reads, or (3) its name is a row id's last segment and its
    value is that row's default, or (4) its name and value match a row's env or alias name or id tokens
    and a default (``_name_value_match``). A value match must be ``deferred``; a range match may be ``bound``.
    """
    import json

    from flightrisk.params.table import TABLE

    golden = json.loads((ROOT / "tests" / "golden" / "flight_params.json").read_text(encoding="utf-8"))
    refs: dict[tuple, set] = {}

    def walk(obj, rid):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("today_ref", "lo_ref", "hi_ref") and isinstance(v, str):
                    for loc in _ref_names(v):
                        refs.setdefault(loc, set()).add((rid, "value" if k == "today_ref" else "range"))
                walk(v, rid)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, rid)

    for row in golden["rows"]:
        walk(row, row["id"])
    env_names, by_leaf = {}, {}
    for row in TABLE:
        for n in (row.env, *(a.name for a in row.env_aliases), *row.cli):
            if n:
                env_names.setdefault(n, set()).add(row.id)
        by_leaf.setdefault(row.id.split(".")[-1], []).append(row)
    out = []
    for file, entries in allowlist.items():
        for key in entries:
            _, _, kind, target, name, value = key
            why, allowed = set(), {"deferred"}
            if kind != "compare":
                for (rid, what) in refs.get((file, name), set()) | refs.get((file, target), set()):
                    why.add(f"{rid} {what}")
            if kind in ("env_default", "argparse_default") and name in env_names:
                why |= {f"{rid} env" for rid in env_names[name]}
            for row in by_leaf.get(str(name).lower(), ()):
                if value in row.defaults.values():
                    why.add(f"{row.id} default")
            for row in TABLE:
                if _name_value_match(row, name, value):
                    why.add(f"{row.id} name and default")
            if why:
                if all(w.endswith(" range") for w in why):
                    allowed.add("bound")
                out.append((key, frozenset(allowed), "; ".join(sorted(why))))
    return out


# Reviewed coincidental matches of ``registry_matches``: key -> why it is not a registry duplicate.
REGISTRY_MATCH_EXCEPTIONS = {
    ('flightrisk/dashboard/pipeline.py', '<module>._frame_loop', 'compare', '<compare>', 'count', 1):
        "A generic 'count' compared with 1, not the escort drone count of row escort.count.",
    ('flightrisk/drone/plan_paths.py', '<module>.split_area', 'compare', '<compare>', 'count', 1):
        "A lane-split count compared with 1, not the escort drone count of row escort.count.",
    ('flightrisk/dashboard/detection_profiles.py', '<module>', 'assign', 'REPLY_TIMEOUT_S', 'REPLY_TIMEOUT_S', 20.0):
        "The wait for an edge's reply to a detection-profile change, not the takeoff reply timeout of "
        "row takeoff.reply_timeout_s; the equal value is a coincidence.",
    ('flightrisk/drone/fake.py', '<module>.FakeDrone.__init__', 'kwarg', 'DroneCapabilities', 'max_move_cm', 500):
        "The capability a test double declares for itself, not a read of row move.max_cm; the double is not flight code.",
    ('flightrisk/drone/fake.py', '<module>.FakeDrone.__init__', 'kwarg', 'DroneCapabilities', 'min_move_cm', 20):
        "The capability a test double declares for itself, not a read of row move.min_cm; the double is not flight code.",
    ('flightrisk/drone/search.py', '<module>', 'default', 'generate_spiral', 'radius_cm', 50):
        "A spiral-search generator radius default, not the stationary radius of row move.stationary_radius_cm; the equal value is a coincidence.",
    ('flightrisk/drone/tello.py', '<module>.FakeTelloController.__init__', 'kwarg', 'DroneCapabilities', 'max_move_cm', 500):
        "The capability a test double declares for itself, not a read of row move.max_cm; the double is not flight code.",
    ('flightrisk/drone/tello.py', '<module>.FakeTelloController.__init__', 'kwarg', 'DroneCapabilities', 'min_move_cm', 20):
        "The capability a test double declares for itself, not a read of row move.min_cm; the double is not flight code.",
    ('flightrisk/drone/template_backend.py', '<module>.TemplateDrone.__init__', 'kwarg', 'DroneCapabilities', 'max_move_cm', 500):
        "The capability a test double declares for itself, not a read of row move.max_cm; the double is not flight code.",
    ('flightrisk/drone/template_backend.py', '<module>.TemplateDrone.__init__', 'kwarg', 'DroneCapabilities', 'min_move_cm', 20):
        "The capability a test double declares for itself, not a read of row move.min_cm; the double is not flight code.",
}


def scan_source(text: str, rel: str, unit_re: re.Pattern) -> list[tuple[tuple, int]]:
    scanner = Scanner(rel, unit_re)
    scanner.visit(ast.parse(text))
    return scanner.out


def scope_files(root: Path = ROOT) -> list[Path]:
    return sorted({p for g in SCOPE_GLOBS for p in root.glob(g)})


def scan_repo(root: Path = ROOT, ids=None) -> Counter:
    unit_re = build_unit_re(words_from_registry(ids))
    keys: Counter = Counter()
    for p in scope_files(root):
        found = scan_source(p.read_text(encoding="utf-8"), str(p.relative_to(root)), unit_re)
        keys.update(k for k, _ in found)
    return keys


def _keys(text: str, ids=None, rel: str = "flightrisk/drone/x.py") -> list[tuple]:
    unit_re = build_unit_re(words_from_registry(ids))
    return [k for k, _ in scan_source(text, rel, unit_re)]


# --- self-tests -----------------------------------------------------------------------------------

PLANTED = {
    "A": ("def f():\n    hover_s = 5\n", "assign"),
    "B": ("def f(x):\n    if x.timeout_s > 9:\n        pass\n", "compare"),
    "C": ("p.add_argument('--timeout-s', default='75')\n", "argparse_default"),
    "D": ("import os\nx = os.getenv('FLIGHTRISK_TIMEOUT_S', '30')\n", "env_default"),
    "E": ("from dataclasses import field\nclass C:\n    timeout_s = field(default=7)\n", "field"),
    "F": ("def f(require_failsafes=True):\n    pass\n", "safety_default"),
    "G": ("ROOM = {'geofence_cm': 450}\n", "dict"),
    "default": ("def f(timeout_s=3):\n    pass\n", "default"),
    "kwonly_default": ("def f(*, timeout_s=3):\n    pass\n", "default"),
}


@pytest.mark.parametrize("text,value", [
    ("GEOFENCE_CM = 2 * 100\n", 200),
    ("GEOFENCE_CM = 4.5 * 100\n", 450.0),
    ("GEOFENCE_CM = 2.0e2\n", 200.0),
    ("GEOFENCE_CM = +450\n", 450),
    ("GEOFENCE_CM = 1.0 / 4\n", 0.25),
    ("ROOM = {'geofence_cm': 3 * 150}\n", 450),
    ("import os\nx = os.getenv('FLIGHTRISK_GEOFENCE_CM', '4.5e2')\n", "4.5e2"),
])
def test_self_folded_literal_is_reported(text, value):
    assert value in [k[5] for k in _keys(text, ids=["envelope.geofence_cm"])]


def test_self_dict_key_not_unit_named_is_ignored():
    assert _keys("X = {'name': 450, 1: 2}\n", ids=["envelope.geofence_cm"]) == []


@pytest.mark.parametrize("rule", sorted(PLANTED))
def test_self_planted_literal_is_reported(rule):
    text, kind = PLANTED[rule]
    kinds = {k[2] for k in _keys(text)}
    assert kind in kinds, (rule, kinds)


def test_self_planted_in_temporary_file(tmp_path):
    d = tmp_path / "flightrisk" / "drone"
    d.mkdir(parents=True)
    (d / "planted.py").write_text("def f():\n    hover_s = 5\n", encoding="utf-8")
    keys = scan_repo(tmp_path, ids=["a.timeout_s"])
    assert list(keys) == [("flightrisk/drone/planted.py", "<module>.f", "assign", "hover_s", "hover_s", 5)]


def test_self_zero_bool_and_non_name_not_reported():
    assert _keys("timeout_s = 0\nplain = 5\nflag_s = True\n") == []


def test_self_unary_minus_folded():
    assert [k[5] for k in _keys("offset_cm = -5\n")] == [-5]


def test_word_new_registry_word_makes_literal_a_finding():
    text = "def f():\n    zorblax = 5\n"
    assert _keys(text, ids=["a.timeout_s"]) == []
    assert [k[5] for k in _keys(text, ids=["a.timeout_s", "x.zorblax_cm"])] == [5]


def test_word_derived_from_registry_ids():
    words = words_from_registry(["stranded.countdown_s", "a.b_cm"])
    assert "stranded" in words and "countdown" in words
    assert "cm" not in words and "b" not in words


def test_word_real_registry_has_words():
    assert {"stranded", "countdown"} <= words_from_registry()


def test_key_changes_when_literal_moves_function():
    k1 = _keys("def f():\n    hover_s = 5\n")
    k2 = _keys("def g():\n    hover_s = 5\n")
    assert k1 != k2


def test_key_changes_when_value_changes():
    assert _keys("def f():\n    hover_s = 5\n") != _keys("def f():\n    hover_s = 6\n")


def test_key_has_six_fields_and_exact_counts():
    text = "def f():\n    hover_s = 5\n    hover_s = 5\n"
    unit_re = build_unit_re(words_from_registry(["a.timeout_s"]))
    counts = Counter(k for k, _ in scan_source(text, "flightrisk/drone/x.py", unit_re))
    (key, n), = counts.items()
    assert len(key) == 6 and n == 2


def test_self_allowlist_sections_are_per_file_in_scope():
    from tests.param_literal_allowlist import ALLOWLIST

    in_scope = {str(p.relative_to(ROOT)) for p in scope_files()}
    assert set(ALLOWLIST) <= in_scope


# --- full-repo run ---------------------------------------------------------------------------------


def test_every_finding_is_allowlisted_with_exact_count():
    from tests.param_literal_allowlist import ALLOWLIST, CATEGORIES

    expected = Counter()
    for file, entries in ALLOWLIST.items():
        for key, entry in entries.items():
            assert entry["category"] in CATEGORIES
            assert entry["reason"]
            if entry["category"] == "deferred":
                assert re.search(r"\bA[3-5]\b", entry["reason"]), key
            expected[key] = entry["count"]
    assert scan_repo() == expected


def test_registry_duplicates_are_deferred():
    """F2: a non-deferred entry may not be a registry row's value, env default or row default."""
    from tests.param_literal_allowlist import ALLOWLIST

    wrong = []
    for key, allowed, why in registry_matches(ALLOWLIST):
        category = ALLOWLIST[key[0]][key]["category"]
        if category not in allowed and key not in REGISTRY_MATCH_EXCEPTIONS:
            wrong.append((key, category, why))
    assert not wrong, wrong
    stale = [k for k in REGISTRY_MATCH_EXCEPTIONS if k not in {m[0] for m in registry_matches(ALLOWLIST)}]
    assert not stale, stale


def test_registry_match_catches_a_planted_duplicate():
    planted = {"flightrisk/drone/tello_bound.py": {
        ("flightrisk/drone/tello_bound.py", "<module>", "assign", "TELLO_CMD_PORT", "TELLO_CMD_PORT", 8889): {}}}
    (key, allowed, why), = registry_matches(planted)
    assert allowed == {"deferred"} and "tello_sdk.command_port" in why
