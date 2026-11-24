"""CMP-1: Phase A2 defines new wire shapes, and no runtime code sends any of them.

A2 adds ``describe_unit``, ``return_home``, ``set_no_entry``, ``set_speed``,
``escort_setpoint``, ``unit_rev`` in the edge ``hello``, ``rth`` (with
``ap_link_loss``) in telemetry, ``on_complete: "return_home"``, the
``assign_lanes`` return keys and the ``straight_line_home`` nack reason. They
live in ``flightrisk/fleet_protocol/`` (and the start-time report in
``flightrisk/params/`` and ``flightrisk/hardware/``), and nothing else uses
them yet: A3 and C1 are the first senders.

The guard is an AST scan of every other module under ``flightrisk/`` for:

* a reference to a new builder, gate helper or type constant
  (``NEW_NAMES``), by name, attribute or import;
* a string literal equal to a new type, key or reason (``NEW_STRINGS``),
  which covers ``on_complete="return_home"`` and ``msg["rth"]``;
* a keyword argument that adds a new key (``NEW_KEYWORDS`` anywhere, and the
  ``assign_lanes`` return keys on ``make_assign_lanes``).

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
DEFINING_PACKAGES = ("fleet_protocol", "params", "hardware")

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
#: Keyword arguments that add a new key to ``make_assign_lanes``.
NEW_ASSIGN_LANES_KEYWORDS = frozenset({"corridor", "landing", "no_entry", "speed_cm_s"})

#: The allowlist: {path relative to flightrisk/: {token, ...}}. Empty in A2;
#: A3 and C1 add their senders here.
ALLOWED: dict[str, frozenset[str]] = {}


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def find_senders(source: str, filename: str = "<source>") -> Iterator[tuple[int, str]]:
    """Every ``(line, token)`` in *source* that uses an A2 wire shape."""
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
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
        elif isinstance(node, ast.Call):
            assign_lanes = _call_name(node.func) == "make_assign_lanes"
            for keyword in node.keywords:
                if keyword.arg in NEW_KEYWORDS or (assign_lanes and keyword.arg in NEW_ASSIGN_LANES_KEYWORDS):
                    yield node.lineno, f"{keyword.arg}="


def _runtime_modules() -> Iterator[Path]:
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if rel.parts[0] in DEFINING_PACKAGES or "__pycache__" in rel.parts:
            continue
        yield path


def _scan() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in _runtime_modules():
        rel = path.relative_to(PACKAGE).as_posix()
        for _, token in find_senders(path.read_text(encoding="utf-8"), str(path)):
            found.setdefault(rel, set()).add(token)
    return found


def test_no_runtime_module_uses_an_a2_wire_shape():
    found = _scan()
    violations = {
        rel: sorted(tokens - ALLOWED.get(rel, frozenset()))
        for rel, tokens in found.items()
        if tokens - ALLOWED.get(rel, frozenset())
    }
    assert violations == {}, (
        "runtime code uses an A2 wire shape (CMP-1); add the sender to ALLOWED only in the phase that ships it"
    )


def test_every_allowlist_entry_is_still_used():
    found = _scan()
    stale = {rel: sorted(tokens - found.get(rel, set())) for rel, tokens in ALLOWED.items()
             if tokens - found.get(rel, set())}
    assert stale == {}


def test_the_allowlist_is_empty_in_phase_a2():
    assert ALLOWED == {}


def test_the_scan_covers_the_runtime_modules():
    rels = {path.relative_to(PACKAGE).as_posix() for path in _runtime_modules()}
    assert {"edge_device.py", "transport.py", "fleet_authority.py", "fleet_ground_link.py"} <= rels
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
])
def test_the_scanner_finds_each_kind_of_sender(source, token):
    assert token in {found for _, found in find_senders(source)}


@pytest.mark.parametrize("source", [
    "self._links.may_send(ws, msg)",
    "FakeTelloController(name=n, speed_cm_s=10)",
    "def f():\n    '''Return home when the battery is low.'''",
    "fp.make_assign_lanes(d, 1, l, z, on_complete='hover')",
])
def test_the_scanner_ignores_older_unrelated_uses(source):
    assert list(find_senders(source)) == []


# --- runtime ------------------------------------------------------------------


_GATED_COMMANDS = [
    fp.make_describe_unit("drone-1", cmd_id="c-du"),
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
