"""A Tello takeoff is not decided by a stray or duplicate reply.

On a real run (drone-2, 16:41:31 UTC) a takeoff was answered "error" only
74 ms after it was sent. A Tello answers a takeoff only once it hovers, so
that reply was most likely a late or duplicate reply to an earlier command
(this Tello is known to send them, and a non-EDU Tello answers ``sdk?`` and
``sn?`` with "unknown command" or "error"). It may also have been a real
refusal (a hot, low or unbalanced battery): the drone never lifted. These
tests pin how ``TelloController.takeoff()`` now handles both honestly:

* A reply that can never answer a takeoff ("unknown command", or one that is
  not text) is skipped, and the takeoff keeps waiting for its own reply.
* A bare "error" does not end the takeoff on its own. The takeoff keeps
  reading replies and watching the state stream for a short window: a
  later "ok", or state that shows the drone in the air, is a takeoff; state
  that shows it still on the ground, a refusal with detail, a land that
  starts, or a window that ends without proof fails the takeoff as before.
* A takeoff is never sent twice.
* Every reply read during a takeoff is logged with its text and its age.

Nothing here touches a real drone or the network: the real djitellopy
client runs over ``FakeTelloWire`` (``tests/conftest.py``).
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from flightrisk.drone import tello as tello_module

WAIT_S = 10.0
# The state stream sends a packet every 50 ms; let the motor time settle so
# a drone resting on the ground shows its motors off.
SETTLE_S = 0.3


def _on_the_ground(tello_on_wire):
    ctrl, wire = tello_on_wire()
    wire.start_state(0)
    time.sleep(SETTLE_S)
    return ctrl, wire


def _short_windows(monkeypatch, confirm_s: float = 2.0, ground_after_s: float = 0.5) -> None:
    """Shrink the confirmation windows so the tests run quickly."""
    monkeypatch.setattr(tello_module, "_TAKEOFF_CONFIRM_S", confirm_s, raising=False)
    monkeypatch.setattr(tello_module, "_TAKEOFF_GROUND_AFTER_S", ground_after_s, raising=False)


def _takeoff_replies(caplog) -> list[dict]:
    return [
        dict(record.fr_fields)
        for record in caplog.records
        if getattr(record, "fr_event", None) == "tello_takeoff_reply"
    ]


def test_stray_error_then_real_ok_takes_off(tello_on_wire, monkeypatch):
    """The drone-2 case: a bare "error" 20 ms after the send, then the Tello's own "ok" once it hovers."""
    _short_windows(monkeypatch)
    ctrl, wire = tello_on_wire()

    def stray_then_ok(command: str):
        if command == "takeoff":
            return [("error", 0.02), ("ok", 0.6)]
        return None

    wire.script = stray_then_ok
    assert ctrl.takeoff() is True
    assert wire.sent.count("takeoff") == 1
    assert ctrl.state.is_flying is True
    # The takeoff's own "ok" was read, so a later land needs no lift guard.
    assert ctrl._takeoff_answered_ok is True


def test_unknown_command_stray_is_skipped(tello_on_wire, monkeypatch):
    """A takeoff can never be answered "unknown command" (a non-EDU Tello's answer to sdk? or sn?)."""
    _short_windows(monkeypatch)
    ctrl, wire = tello_on_wire()

    def stray_then_ok(command: str):
        if command == "takeoff":
            return [("unknown command", 0.02), ("\x00\xff", 0.05), ("ok", 0.4)]
        return None

    wire.script = stray_then_ok
    assert ctrl.takeoff() is True
    assert wire.sent.count("takeoff") == 1


def test_bare_error_with_the_drone_still_on_the_ground_aborts(tello_on_wire, monkeypatch):
    """A real refusal answered with a bare "error": state shows the drone down with its motors off, so it fails."""
    _short_windows(monkeypatch, confirm_s=4.0, ground_after_s=0.5)
    ctrl, wire = _on_the_ground(tello_on_wire)

    def refuse(command: str):
        if command == "takeoff":
            return [("error", 0.02)]
        return None

    wire.script = refuse
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"'takeoff' answered 'error' \(not retried\)"):
        ctrl.takeoff()
    # Decided by the ground state, not by waiting out the whole window.
    assert time.monotonic() - started < 3.0
    assert wire.sent.count("takeoff") == 1
    assert ctrl.state.is_flying is False
    assert ctrl._takeoff_answered_ok is False
    assert ctrl._takeoff_in_flight is False


def test_refusal_with_detail_aborts_at_once(tello_on_wire, monkeypatch):
    """A refusal that says why ("error Motor stop") is a real answer: it fails at once, without a state wait."""
    _short_windows(monkeypatch, confirm_s=4.0)
    ctrl, wire = tello_on_wire()

    def refuse(command: str):
        if command == "takeoff":
            return [("error Motor stop", 0.02)]
        return None

    wire.script = refuse
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"'takeoff' answered 'error Motor stop' \(not retried\)"):
        ctrl.takeoff()
    assert time.monotonic() - started < 1.0
    assert wire.sent.count("takeoff") == 1


def test_bare_error_without_any_proof_still_aborts(tello_on_wire, monkeypatch):
    """No state stream and no further reply: the window ends and the takeoff fails as before."""
    _short_windows(monkeypatch, confirm_s=0.8)
    ctrl, wire = tello_on_wire()

    def refuse(command: str):
        if command == "takeoff":
            return [("error", 0.02)]
        return None

    wire.script = refuse
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"'takeoff' answered 'error' \(not retried\)"):
        ctrl.takeoff()
    assert 0.7 <= time.monotonic() - started < 5.0
    assert wire.sent.count("takeoff") == 1
    assert ctrl.state.is_flying is False


def test_bare_error_then_a_detailed_refusal_aborts(tello_on_wire, monkeypatch):
    """A stray "error" followed by the Tello's real refusal fails with the real refusal."""
    _short_windows(monkeypatch, confirm_s=4.0)
    ctrl, wire = tello_on_wire()

    def refuse(command: str):
        if command == "takeoff":
            return [("error", 0.02), ("error Not joystick", 0.3)]
        return None

    wire.script = refuse
    with pytest.raises(RuntimeError, match="error Not joystick"):
        ctrl.takeoff()
    assert wire.sent.count("takeoff") == 1


@pytest.mark.parametrize("error_after_s", [0.02, 1.0])
def test_airborne_state_with_an_error_reply_counts_as_takeoff(tello_on_wire, monkeypatch, error_after_s):
    """The Tello lifts to 80 cm; an "error" read early (stray) or late (while it climbs) does not undo that."""
    _short_windows(monkeypatch, confirm_s=3.0, ground_after_s=2.5)
    ctrl, wire = _on_the_ground(tello_on_wire)

    def lift() -> None:
        wire.height_cm, wire.tof_cm = 80, 90

    def climbs(command: str):
        if command == "takeoff":
            wire.set_motors(True)
            timer = threading.Timer(0.4, lift)
            timer.daemon = True
            timer.start()
            return [("error", error_after_s)]
        return None

    wire.script = climbs
    assert ctrl.takeoff() is True
    assert wire.sent.count("takeoff") == 1
    assert ctrl.state.is_flying is True
    # No "ok" was read: the Tello's own "ok" may still arrive, so a land
    # right after this takeoff keeps the lift and reply guards.
    assert ctrl._takeoff_answered_ok is False


def test_land_during_the_confirmation_fails_the_takeoff_and_sends_no_second_takeoff(tello_on_wire, monkeypatch):
    """A land that starts while a bare "error" is being confirmed ends the takeoff; the stop's "ok" is not taken."""
    _short_windows(monkeypatch, confirm_s=4.0)
    monkeypatch.setattr(tello_module, "_TAKEOFF_LIFT_GUARD_S", 0.5)
    monkeypatch.setattr(tello_module, "_TAKEOFF_REPLY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(tello_module, "_LAND_REPLY_TIMEOUT_S", 0.5)
    ctrl, wire = _on_the_ground(tello_on_wire)

    def stray(command: str):
        if command == "takeoff":
            return [("error", 0.02)]
        return None

    wire.script = stray
    outcome: list = []

    def take_off() -> None:
        try:
            outcome.append(ctrl.takeoff())
        except RuntimeError as exc:
            outcome.append(exc)

    taker = threading.Thread(target=take_off, daemon=True)
    taker.start()
    deadline = time.monotonic() + WAIT_S
    while "takeoff" not in wire.sent and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.15)
    ctrl.land()
    taker.join(WAIT_S)
    assert not taker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], RuntimeError)
    assert "answered 'error'" in str(outcome[0])
    assert wire.sent.count("takeoff") == 1
    assert "land" in wire.sent
    assert ctrl.state.is_flying is False


def test_every_takeoff_reply_is_logged_with_its_age(tello_on_wire, monkeypatch, caplog):
    """Each reply read during a takeoff is a structured event with its text and its age in ms."""
    _short_windows(monkeypatch)
    ctrl, wire = tello_on_wire()

    def stray_then_ok(command: str):
        if command == "takeoff":
            return [("error", 0.02), ("ok", 0.5)]
        return None

    wire.script = stray_then_ok
    with caplog.at_level(logging.INFO, logger="flightrisk.drone.tello"):
        assert ctrl.takeoff() is True
    replies = _takeoff_replies(caplog)
    assert [r["reply"] for r in replies] == ["error", "ok"]
    first, second = replies
    assert isinstance(first["reply_age_ms"], int) and 0 <= first["reply_age_ms"] < 400
    assert isinstance(second["reply_age_ms"], int) and 400 <= second["reply_age_ms"] < 3000
    assert first["drone_id"] == ctrl.name
    assert first["verdict"] == "unconfirmed_error"
    assert second["verdict"] == "accepted"


def test_plain_takeoff_ok_is_unchanged(tello_on_wire, caplog):
    """The normal path: one "ok", accepted at once, logged once."""
    ctrl, wire = tello_on_wire()
    with caplog.at_level(logging.INFO, logger="flightrisk.drone.tello"):
        assert ctrl.takeoff() is True
    assert wire.sent.count("takeoff") == 1
    assert ctrl._takeoff_answered_ok is True
    replies = _takeoff_replies(caplog)
    assert len(replies) == 1 and replies[0]["reply"] == "ok" and replies[0]["verdict"] == "accepted"
