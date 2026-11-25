"""CMP-1: Phase A2 defines new wire shapes, and no runtime code sends any of them.

A2 adds ``describe_unit``, ``return_home``, ``set_no_entry``, ``set_speed``,
``escort_setpoint``, ``unit_rev`` in the edge ``hello``, ``rth`` (with
``ap_link_loss``) in telemetry, ``on_complete: "return_home"``, the
``assign_lanes`` return keys and the ``straight_line_home`` nack reason. They
live in ``flightrisk/fleet_protocol/`` (and the start-time report in
``flightrisk/params/`` and ``flightrisk/hardware/``), and nothing else uses
them yet: A3 and C1 are the first senders.

The guard is an AST scan of every module under ``flightrisk/`` except
``fleet_protocol/`` (``params/`` and ``hardware/`` included) for:

* a reference to a new builder, gate helper or type constant
  (``NEW_NAMES``), by name, attribute or import;
* a string literal, or a constant string built with ``+``, ``"sep".join``
  or an f-string, equal to a new type, key, reason or builder name
  (``NEW_STRINGS``), which covers ``on_complete="return_home"``,
  ``msg["rth"]`` and ``getattr(fp, "make_set_speed")``;
* a keyword argument that adds a new key (``NEW_KEYWORDS`` anywhere);
* the new ``assign_lanes`` keys as a dict key, a subscript key or a keyword
  argument on any call, which covers an aliased builder,
  ``functools.partial`` and ``**{...}``.

``NOT_SENDERS`` lists, with reasons, the uses that send nothing. A static
scan cannot see a name or key built from non-constant data (a variable,
``chr``, a config file, ``dict(zip(...))``, ``setattr``, ``eval``); code
review covers those.

``ALLOWED`` is the explicit allowlist, empty in A2. A later phase that
starts sending one of these adds ``"path/relative/to/flightrisk.py":
{"token", ...}`` here, in the same change that adds the sender. A runtime
check adds that the live edge still refuses every new command at decode and
that the telemetry the edge builds carries no ``rth``.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from flightrisk import fleet_protocol as fp

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "flightrisk"
#: Packages that define the A2 shapes; everything else is runtime code.
DEFINING_PACKAGES = ("fleet_protocol",)

#: Builders, gate helpers and constants that only a sender of an A2 shape needs.
#: ``QUERY_TYPES`` is left out on purpose: ``obs_comm.py`` uses it to classify
#: received queries for the communication log (PROTO-7), which sends nothing.
NEW_NAMES = frozenset({
    "make_return_home", "make_set_no_entry", "make_set_speed", "make_escort_setpoint",
    "make_describe_unit", "make_describe_unit_ack", "make_describe_unit_nack",
    "make_return_ack", "make_return_nack", "make_rth_object",
    "RETURN_COMMAND_TYPES", "RETURN_HOME", "SET_NO_ENTRY", "SET_SPEED", "ESCORT_SETPOINT",
    "DESCRIBE_UNIT", "ACCEPT_NAMES", "NEW_NACK_REASONS", "ON_COMPLETE_ACTIONS_V2",
    "nack_reason_for_peer", "may_describe", "required_accepts",
})
#: Names that are new only when imported from the protocol package
#: (``may_send`` is also an older method name of the ground's link registry).
NEW_IMPORT_NAMES = frozenset({"may_send"})
#: New message types, keys and nack reasons, as they appear in a literal.
NEW_STRINGS = frozenset({
    "describe_unit", "return_home", "set_no_entry", "set_speed", "escort_setpoint",
    "straight_line_home", "unit_rev", "rth", "ap_link_loss", "corridor", "no_entry", "accepts",
}) | frozenset(fp.ACCEPT_NAMES)
#: Keyword arguments that add a new key to any builder.
NEW_KEYWORDS = frozenset({"unit_rev", "rth", "ap_link_loss", "accepts"})
#: The new ``assign_lanes`` keys. They are flagged wherever they appear as a
#: dict key, a subscript key or a keyword argument, whatever the call, so an
#: aliased builder, ``functools.partial`` or ``**{...}`` is caught too.
NEW_ASSIGN_LANES_KEYS = frozenset({"corridor", "landing", "no_entry", "speed_cm_s"})
#: Calls whose own ``speed_cm_s`` keyword predates A2 and builds no message:
#: the simulated drone's speed (``edge_device.py``).
UNRELATED_CALLEES = frozenset({"FakeTelloController"})
#: The builder names, also flagged as string literals so ``getattr(fp, "make_set_speed")`` is caught.
NEW_STRINGS = NEW_STRINGS | frozenset(name for name in NEW_NAMES if name.startswith("make_"))

#: The allowlist: {path relative to flightrisk/: {token, ...}}. Empty in A2;
#: A3 and C1 add their senders here.
ALLOWED: dict[str, frozenset[tuple[str, str]]] = {
    # The edge's describe_unit reply (PROTO-4): the ack built from the
    # start-time report, and the nack for a refused or too fast query.
    "edge_query.py": frozenset({
        ("make_describe_unit_ack", "_build"),
        ("make_describe_unit_nack", "_build"),
        ("make_describe_unit_nack", "_refuse"),
        ("make_describe_unit_nack", "answer"),
        # Reads the report's unit_rev for the edge hello (PROTO-2); sends nothing itself.
        ("unit_rev", "unit_rev"),
    }),
    # The edge hello carries the unit_rev of the start-time resolution (PROTO-2).
    "fleet_authority.py": frozenset({("unit_rev=", "_edge_hello")}),
}

#: The only tokens Phase A3 may add to ``ALLOWED``: the edge's ``describe_unit``
#: reply (PROTO-4) and the ``unit_rev`` it sends in its v2 ``hello`` (PROTO-3).
#: The ground's query builder ``make_describe_unit`` is not among them.
A3_TOKENS = frozenset({"make_describe_unit_ack", "make_describe_unit_nack", "unit_rev", "unit_rev="})

#: Uses that send nothing, each with its reason. Kept apart from ``ALLOWED``,
#: which lists senders and stays empty in A2.
#: Each exemption is ``(token, scope)``, where scope is the innermost enclosing
#: function name (``<module>`` outside any function), so the same token used
#: elsewhere in the file is still caught.
NOT_SENDERS: dict[str, frozenset[tuple[str, str]]] = {
    # Maps an edge refusal to the older nack reason "landing" (a value, not
    # the assign_lanes key), from before A2.
    "drone/lane_executor.py": frozenset({("landing:", "<module>")}),
    # A flight-safety log event's own ``landing`` field (the orphan landing
    # that was cleared), from before A2; it builds no protocol message.
    "dashboard/manual_control.py": frozenset({("landing=", "observe_telemetry")}),
    # The start-time report builds the describe_unit reply body and its
    # unit_rev (data, not a send); the edge hello and the reply are built
    # only by fleet_protocol builders, which the scan of senders covers.
    # The gate on the unshaped send path refuses what needs an accepted name,
    # and the authority downgrades a new nack reason to "invalid": both only
    # hold messages back or lower them to today's wire.
    "fleet_ground_link.py": frozenset({("required_accepts", "_payload_for")}),
    "fleet_authority.py": frozenset({("nack_reason_for_peer", "_nack"), ("nack_reason_for_peer", "_drop_at_dispatch")}),
    "params/report.py": frozenset({("unit_rev", "wire"), ("unit_rev=", "build_report")}),
}


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _folded(node: ast.expr) -> str | None:
    """The string a constant expression builds (``+``, ``"sep".join(...)``, an f-string of constants), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_folded(value) for value in node.values]
        return None if None in parts else "".join(parts)
    if (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join"
        and len(node.args) == 1 and isinstance(node.args[0], (ast.Tuple, ast.List))
    ):
        sep = _folded(node.func.value)
        parts = [_folded(item) for item in node.args[0].elts]
        return None if sep is None or None in parts else sep.join(parts)
    return None


def _key(node: ast.expr | None) -> str | None:
    return None if node is None else _folded(node)


def find_senders(source: str, filename: str = "<source>") -> Iterator[tuple[int, str]]:
    """Every ``(line, token)`` in *source* that uses an A2 wire shape."""
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)):
            built = _folded(node)
            if built is not None and built in NEW_STRINGS | NEW_ASSIGN_LANES_KEYS:
                yield node.lineno, built
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if _key(key) in NEW_ASSIGN_LANES_KEYS:
                    yield node.lineno, f"{_key(key)}:"
        elif isinstance(node, ast.Subscript) and _key(node.slice) in NEW_ASSIGN_LANES_KEYS:
            yield node.lineno, f"[{_key(node.slice)}]"
        if isinstance(node, ast.Name) and node.id in NEW_NAMES:
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute) and node.attr in NEW_NAMES:
            yield node.lineno, node.attr
        elif isinstance(node, ast.ImportFrom):
            from_protocol = (node.module or "").startswith("flightrisk.fleet_protocol")
            for alias in node.names:
                if alias.name in NEW_NAMES or (from_protocol and alias.name in NEW_IMPORT_NAMES):
                    yield node.lineno, alias.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in NEW_STRINGS:
            yield node.lineno, node.value
        if isinstance(node, ast.Call):
            unrelated = _call_name(node.func) in UNRELATED_CALLEES
            for keyword in node.keywords:
                if keyword.arg in NEW_KEYWORDS or (not unrelated and keyword.arg in NEW_ASSIGN_LANES_KEYS):
                    yield node.lineno, f"{keyword.arg}="


def find_scoped_senders(source: str, filename: str = "<source>") -> Iterator[tuple[int, str, str]]:
    """Every ``(line, token, scope)`` of ``find_senders``, with the innermost enclosing function name as scope."""
    tree = ast.parse(source, filename=filename)
    spans = sorted(
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    for line, token in find_senders(source, filename):
        inside = [span for span in spans if span[0] <= line <= span[1]]
        yield line, token, (max(inside)[2] if inside else "<module>")


def _runtime_modules() -> Iterator[Path]:
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if rel.parts[0] in DEFINING_PACKAGES or "__pycache__" in rel.parts:
            continue
        yield path


def _scan() -> dict[str, set[tuple[str, str]]]:
    found: dict[str, set[tuple[str, str]]] = {}
    for path in _runtime_modules():
        rel = path.relative_to(PACKAGE).as_posix()
        for _, token, scope in find_scoped_senders(path.read_text(encoding="utf-8"), str(path)):
            found.setdefault(rel, set()).add((token, scope))
    return found


def _permitted(rel: str) -> frozenset[tuple[str, str]]:
    return ALLOWED.get(rel, frozenset()) | NOT_SENDERS.get(rel, frozenset())


def test_no_runtime_module_uses_an_a2_wire_shape():
    found = _scan()
    violations = {rel: sorted(tokens - _permitted(rel)) for rel, tokens in found.items() if tokens - _permitted(rel)}
    assert violations == {}, (
        "runtime code uses an A2 wire shape (CMP-1); add the sender to ALLOWED only in the phase that ships it"
    )


def test_every_allowlist_entry_is_still_used():
    found = _scan()
    stale = {rel: sorted(tokens - found.get(rel, set())) for rel, tokens in {**ALLOWED, **NOT_SENDERS}.items()
             if tokens - found.get(rel, set())}
    assert stale == {}


def test_an_exemption_covers_only_its_own_function():
    exempt = {("landing=", "observe_telemetry")}
    source = "def observe_telemetry():\n    log(landing=1)\n\ndef other():\n    send(landing=spot)\n"
    found = {(token, scope) for _, token, scope in find_scoped_senders(source)}
    assert found - exempt == {("landing=", "other")}


def test_the_allowlist_names_only_a3_tokens():
    tokens = {token for entries in ALLOWED.values() for token, _ in entries}
    assert tokens <= A3_TOKENS
    assert all(ALLOWED.values()), "an ALLOWED entry must name at least one token"


def test_the_scan_covers_the_runtime_modules():
    rels = {path.relative_to(PACKAGE).as_posix() for path in _runtime_modules()}
    assert {"edge_device.py", "transport.py", "fleet_authority.py", "fleet_ground_link.py",
            "params/report.py", "hardware/catalogue.py"} <= rels
    assert not any(rel.split("/")[0] in DEFINING_PACKAGES for rel in rels)


@pytest.mark.parametrize("source, token", [
    ("fp.make_return_home('d', fence=f)", "make_return_home"),
    ("from flightrisk.fleet_protocol import make_describe_unit", "make_describe_unit"),
    ("from flightrisk.fleet_protocol.unit import may_send", "may_send"),
    ("x = fp.RETURN_COMMAND_TYPES", "RETURN_COMMAND_TYPES"),
    ("fp.make_assign_lanes(d, 1, l, z, on_complete='return_home')", "return_home"),
    ("fp.make_assign_lanes(d, 1, l, z, speed_cm_s=150)", "speed_cm_s="),
    ("fp.make_assign_lanes(d, 1, l, z, corridor=c)", "corridor="),
    ("fp.make_telemetry(*a, v2=v, rth=r)", "rth="),
    ("fp.make_edge_hello(d, n, 0, unit_rev=rev)", "unit_rev="),
    ("msg['rth'] = r", "rth"),
    ("send({'type': 'escort_setpoint'})", "escort_setpoint"),
    ("reason = 'straight_line_home'", "straight_line_home"),
    # The forms a first scan missed (review of PR #228).
    ("getattr(fp, 'make_' + 'return' + '_home')", "make_return_home"),
    ("getattr(fp, 'make_set_speed')", "make_set_speed"),
    ("getattr(fp, f'make_set_speed')", "make_set_speed"),
    ("t = '_'.join(('return', 'home'))", "return_home"),
    ("mal = fp.make_assign_lanes\nmal(d, 1, l, z, speed_cm_s=150)", "speed_cm_s="),
    ("fp.make_assign_lanes(d, 1, l, z, **{'speed_cm_s': 150, 'landing': {}})", "speed_cm_s:"),
    ("fp.make_assign_lanes(d, 1, l, z, **{'speed_cm_s': 150, 'landing': {}})", "landing:"),
    ("functools.partial(fp.make_assign_lanes, no_entry=[])", "no_entry="),
    ("msg['speed_cm_s'] = 150", "[speed_cm_s]"),
    ("msg['landing'] = spot", "[landing]"),
    ("msg.update(landing=spot)", "landing="),
    ("msg['speed_' + 'cm_s'] = 150", "[speed_cm_s]"),
    ("plan = dict(corridor=c)", "corridor="),
])
def test_the_scanner_finds_each_kind_of_sender(source, token):
    assert token in {found for _, found in find_senders(source)}


@pytest.mark.parametrize("source", [
    "self._links.may_send(ws, msg)",
    "FakeTelloController(name=n, speed_cm_s=10)",
    "reason = 'landing'",
    "if refusal == 'landing': pass",
    "def f():\n    '''Return home when the battery is low.'''",
    "fp.make_assign_lanes(d, 1, l, z, on_complete='hover')",
])
def test_the_scanner_ignores_older_unrelated_uses(source):
    assert list(find_senders(source)) == []


# --- runtime ------------------------------------------------------------------


_GATED_COMMANDS = [
    fp.make_return_home("drone-1", cmd_id="c-rh"),
    fp.make_set_no_entry("drone-1", [], fence=(7, "ground-a"), cmd_id="c-ne"),
    fp.make_set_speed("drone-1", 150, fence=(7, "ground-a"), cmd_id="c-sp"),
    fp.make_escort_setpoint("drone-1", 0, 0, 1500, 90, 2, fence=(7, "ground-a"), cmd_id="c-es"),
]


@pytest.mark.parametrize("msg", _GATED_COMMANDS, ids=lambda m: m["type"])
@pytest.mark.parametrize("require_fence", [False, True])
def test_the_live_edge_still_refuses_every_new_command_at_decode(msg, require_fence):
    """Nothing reaches an edge's handlers: today's edge refuses the new commands, as before A2."""
    with pytest.raises(fp.ProtocolError):
        fp.decode(json.dumps(msg), direction=fp.DIRECTION_GROUND_TO_EDGE, proto=2, require_fence=require_fence)
    with pytest.raises(fp.ProtocolError):
        fp.encode(msg, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=2)


def test_the_telemetry_the_edge_builds_has_no_rth():
    from tests.test_telemetry_field_cap import _GpsTello, _NoGpsTello, _wire

    for drone_cls in (_GpsTello, _NoGpsTello):
        msg = _wire(drone_cls)
        assert "rth" not in msg and "ap_link_loss" not in msg
        assert "unit_rev" not in msg


def test_the_protocol_version_is_unchanged():
    assert (fp.PROTOCOL_VERSION, fp.MIN_PROTOCOL_VERSION) == (2, 1)
