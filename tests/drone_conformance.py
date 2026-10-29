"""Reusable drone-backend conformance checks: the rules every controller follows.

Any backend, registered or not, can be checked by describing it as a
``Subject`` (how to build one, how to make it fly, and two optional hooks
for what cannot be observed from outside) and calling ``run_all``, or one
check at a time. ``tests/test_drone_conformance.py`` runs every check against
the built-in subjects; copy one of its entries for a new backend.

The rules (``docs/drone-interface.md``):

* ``connect()`` returns True; ``disconnect()`` can be called twice; after it
  the drone reports not connected and not flying.
* Telemetry (``read_telemetry``) is a ``TelemetrySnapshot`` whose readings
  are ``None`` or a ``Reading`` in the documented SI unit, never a boolean.
* Capabilities are honest: every declared capability has a callable method,
  every method of an undeclared optional capability answers with a non-OK
  ``CommandResult`` (never ``NotImplementedError``), and the backend's source
  never raises ``NotImplementedError``. ``flight_scale`` is ``room`` or
  ``field``.
* Every command's return value normalizes with ``command_result`` to its
  own command name; declared flight commands succeed while flying.
* ``land()`` is idempotent and never dropped: on the ground it succeeds,
  while a move is in progress it is not refused for being busy, and it
  never returns ``DROPPED_BUSY``.
* ``rc_control`` never passes a stick value outside -100..100 to the drone:
  it is either refused or clamped.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from flightrisk.drone.controller import (
    Capability,
    CommandOutcome,
    CommandResult,
    capabilities_of,
    command_result,
)
from flightrisk.drone.snapshot import (
    READING_UNITS,
    Reading,
    TelemetrySnapshot,
    read_telemetry,
)

__all__ = ["CHECKS", "Subject", "run_all"]

#: Optional capability -> the method that provides it.
_OPTIONAL_METHODS = {
    Capability.RETURN_TO_HOME: "return_to_home",
    Capability.GOTO_GPS: "goto_gps",
}
#: Basic capability -> the methods that must exist when it is declared.
_BASIC_METHODS = {
    Capability.TAKEOFF_LAND: ("takeoff", "land"),
    Capability.HOVER: ("hover",),
    Capability.RELATIVE_MOVE: ("move",),
    Capability.ROTATE: ("rotate",),
    Capability.RC_CONTROL: ("rc_control",),
    Capability.VIDEO: ("get_frame", "on_frame"),
}


@dataclass
class Subject:
    """How to exercise one backend.

    * ``make()`` returns a new, unconnected controller.
    * ``ready(ctrl)`` runs after ``connect()`` until the drone may take off
      (for example, waiting for an autopilot's preflight); default: nothing.
    * ``rc_sent(ctrl)`` returns the last stick values the backend passed to
      the drone, scaled to -100..100, or None when nothing was passed; leave
      it None when that cannot be observed (the check then requires
      ``rc_control`` to be refused).
    * ``teardown(ctrl)`` runs at the end of every check.
    * ``move_cm`` is a move every subject can fly, ``slow_move_cm`` one that
      takes long enough to land in the middle of (0 skips that check).
    """

    name: str
    make: Callable[[], Any]
    ready: Callable[[Any], None] = field(default=lambda ctrl: None)
    rc_sent: Callable[[Any], tuple | None] | None = None
    teardown: Callable[[Any], None] = field(default=lambda ctrl: None)
    move_cm: int = 50
    slow_move_cm: int = 0


def _connected(subject: Subject) -> Any:
    ctrl = subject.make()
    assert ctrl.connect() is True, f"{subject.name}: connect() must return True"
    subject.ready(ctrl)
    return ctrl


def _flying(subject: Subject) -> Any:
    ctrl = _connected(subject)
    result = command_result(ctrl.takeoff(), "takeoff")
    assert result.ok, f"{subject.name}: takeoff failed: {result}"
    assert read_telemetry(ctrl).is_flying, f"{subject.name}: not flying after takeoff"
    return ctrl


def check_connect_disconnect(subject: Subject) -> None:
    ctrl = _connected(subject)
    try:
        assert read_telemetry(ctrl).connected is True, f"{subject.name}: not connected after connect()"
        ctrl.disconnect()
        snap = read_telemetry(ctrl)
        assert snap.connected is False and snap.is_flying is False, f"{subject.name}: {snap}"
        ctrl.disconnect()  # idempotent
    finally:
        subject.teardown(ctrl)


def check_telemetry_shape(subject: Subject) -> None:
    ctrl = _connected(subject)
    try:
        snap = read_telemetry(ctrl)
        assert isinstance(snap, TelemetrySnapshot)
        assert snap.drone_id == ctrl.name
        assert snap.is_flying_source in ("commanded", "sensed")
        for name, value in snap.readings().items():
            if value is None:
                continue
            assert isinstance(value, Reading), (subject.name, name)
            assert isinstance(value.value, (int, float)) and not isinstance(value.value, bool), (subject.name, name)
            assert value.unit == READING_UNITS[name], (subject.name, name, value.unit)
            assert value.age_s() >= 0.0, (subject.name, name)
    finally:
        subject.teardown(ctrl)


def check_capabilities_honest(subject: Subject) -> None:
    ctrl = subject.make()
    try:
        caps = capabilities_of(ctrl)
        assert caps.flight_scale in ("room", "field"), (subject.name, caps.flight_scale)
        for capability, methods in _BASIC_METHODS.items():
            if caps.supports(capability):
                for method in methods:
                    assert callable(getattr(ctrl, method, None)), f"{subject.name}: declares {capability} without {method}"
        for capability, method in _OPTIONAL_METHODS.items():
            if caps.supports(capability):
                assert callable(getattr(ctrl, method, None)), f"{subject.name}: declares {capability} without {method}"
        source = inspect.getsource(inspect.getmodule(type(ctrl)))
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Raise) and node.exc is not None:
                target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                assert getattr(target, "id", None) != "NotImplementedError", subject.name
    finally:
        subject.teardown(ctrl)


def check_undeclared_commands_are_refused(subject: Subject) -> None:
    ctrl = _flying(subject)
    try:
        caps = capabilities_of(ctrl)
        if not caps.supports(Capability.RC_CONTROL):
            result = command_result(ctrl.rc_control(0, 10, 0, 0), "rc_control")
            assert result.outcome is CommandOutcome.REJECTED, (subject.name, result)
        if not caps.supports(Capability.VIDEO):
            assert ctrl.get_frame() is None, subject.name
        if not caps.supports(Capability.RETURN_TO_HOME) and callable(getattr(ctrl, "return_to_home", None)):
            assert not command_result(ctrl.return_to_home(), "return_to_home").ok, subject.name
    finally:
        _land_quietly(ctrl)
        subject.teardown(ctrl)


def check_command_results(subject: Subject) -> None:
    ctrl = _flying(subject)
    try:
        caps = capabilities_of(ctrl)
        calls: list[tuple[str, Callable[[], Any]]] = []
        if caps.supports(Capability.RELATIVE_MOVE):
            calls.append(("move", lambda: ctrl.move("forward", subject.move_cm)))
        if caps.supports(Capability.ROTATE):
            calls.append(("rotate", lambda: ctrl.rotate(10)))
        if caps.supports(Capability.HOVER):
            calls.append(("hover", lambda: ctrl.hover()))
        if caps.supports(Capability.RC_CONTROL):
            calls.append(("rc_control", lambda: ctrl.rc_control(0, 0, 0, 0)))
        calls.append(("bad_move", lambda: ctrl.move("sideways", subject.move_cm)))
        calls.append(("land", lambda: ctrl.land()))
        for name, call in calls:
            if name == "bad_move":
                bad = command_result(call(), "move")
                assert not bad.ok, (subject.name, bad)
                continue
            value = call()
            assert value is None or isinstance(value, (bool, CommandResult)), (subject.name, name, value)
            result = command_result(value, name)
            assert result.command == name, (subject.name, name, result)
            assert result.ok, (subject.name, name, result)
    finally:
        _land_quietly(ctrl)
        subject.teardown(ctrl)


def check_land_idempotent(subject: Subject) -> None:
    ctrl = _connected(subject)
    try:
        assert command_result(ctrl.land(), "land").ok, f"{subject.name}: land on the ground must succeed"
        assert command_result(ctrl.takeoff(), "takeoff").ok
        assert command_result(ctrl.land(), "land").ok
        assert _wait(lambda: not read_telemetry(ctrl).is_flying), f"{subject.name}: still flying after land"
        assert command_result(ctrl.land(), "land").ok
    finally:
        subject.teardown(ctrl)


def check_land_during_move(subject: Subject) -> None:
    if not subject.slow_move_cm:
        return
    ctrl = _flying(subject)
    try:
        out: dict[str, Any] = {}
        worker = threading.Thread(target=lambda: out.setdefault("move", ctrl.move("forward", subject.slow_move_cm)),
                                  daemon=True)
        worker.start()
        time.sleep(0.2)
        started = time.monotonic()
        result = command_result(ctrl.land(), "land")
        assert result.outcome is not CommandOutcome.DROPPED_BUSY, subject.name
        assert result.ok, (subject.name, result)
        assert time.monotonic() - started < 5.0, f"{subject.name}: land waited for the move"
        worker.join(10.0)
        assert not worker.is_alive(), f"{subject.name}: the move never ended after the land"
        assert _wait(lambda: not read_telemetry(ctrl).is_flying, 20.0), subject.name
    finally:
        subject.teardown(ctrl)


def check_rc_bounds(subject: Subject) -> None:
    ctrl = _flying(subject)
    try:
        if not capabilities_of(ctrl).supports(Capability.RC_CONTROL):
            return  # covered by check_undeclared_commands_are_refused
        result = command_result(ctrl.rc_control(150, -150, 101, -999), "rc_control")
        if result.outcome is CommandOutcome.REJECTED:
            return
        assert result.ok, (subject.name, result)
        assert subject.rc_sent is not None, f"{subject.name}: rc_control accepted values it cannot show were clamped"
        sent = subject.rc_sent(ctrl)
        assert sent is not None, f"{subject.name}: nothing recorded"
        assert all(-100 <= v <= 100 for v in sent), (subject.name, sent)
        assert sent[0] == 100 and sent[1] == -100, (subject.name, sent)
    finally:
        _land_quietly(ctrl)
        subject.teardown(ctrl)


def _land_quietly(ctrl: Any) -> None:
    with contextlib.suppress(Exception):  # cleanup only
        ctrl.land()


def _wait(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


CHECKS: dict[str, Callable[[Subject], None]] = {
    "connect_disconnect": check_connect_disconnect,
    "telemetry_shape": check_telemetry_shape,
    "capabilities_honest": check_capabilities_honest,
    "undeclared_commands_are_refused": check_undeclared_commands_are_refused,
    "command_results": check_command_results,
    "land_idempotent": check_land_idempotent,
    "land_during_move": check_land_during_move,
    "rc_bounds": check_rc_bounds,
}


def run_all(subject: Subject) -> None:
    """Run every check against *subject*; the first failure raises ``AssertionError``."""
    for check in CHECKS.values():
        check(subject)
