"""Regression tests for the retroactive review findings on the Tello land path (PR #106).

The tests drive a real ``TelloController`` over ``FakeTelloWire``. Each
behaviour change has a test that fails on the code before the fix.

* E: a land confirmed wrongly while the state stream was lost is noticed by
  the state poller from the motor time, and flagged as unconfirmed.
* F: a land that starts with no known height measures its stall from when
  the reference height was first seen.
* G: a failing state hook cannot kill djitellopy's state thread, even if
  stdout is dead.
* H: concurrent ``land()`` calls do not multiply the resends.
* D (debounce): one stale or noisy packet cannot trigger a resend.
* The resend notice is printed only once the channel is held.
* Descent rates, no known height, a frozen height, a resend blocked by
  another channel holder, a stale ``ok``, stream loss during a resend, and
  missing ``tof`` / ``time`` fields.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable

import pytest

from flightrisk.drone import tello as tello_module
from flightrisk.drone.tello import TelloController, _StateStampedUdpObject
from tests.test_review_fixes_edge import (
    WAIT_S,
    _flying_with_motors,
    _move_ignoring_errors,
    wait_for,
)


class Driver:
    """Feeds state packets whose height follows ``height_fn(seconds_since_start)``."""

    def __init__(self, wire, height_fn: Callable[[float], float], interval_s: float = 0.05, motors: bool = True):
        self.wire = wire
        self.height_fn = height_fn
        self.interval_s = interval_s
        self._stop = threading.Event()
        wire.stop_state()
        wire.set_motors(motors)
        self.t0 = time.monotonic()
        self._push()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _push(self) -> None:
        height = round(self.height_fn(time.monotonic() - self.t0))
        self.wire.height_cm, self.wire.tof_cm = height, height + 10
        self.wire.send_state()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._push()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2.0)


@pytest.fixture
def driver():
    made: list[Driver] = []

    def _make(wire, height_fn, **kwargs) -> Driver:
        d = Driver(wire, height_fn, **kwargs)
        made.append(d)
        return d

    yield _make
    for d in made:
        d.close()


def _watch(ctrl: TelloController, timeout_s: float, *, ref_height, land_sent_ago: float = 0.0):
    """Run ``_wait_for_ground`` as a land waiting after an "error" reply would; return (result, seconds)."""
    ctrl._land_unconfirmed = True
    since = time.monotonic() - land_sent_ago
    started = time.monotonic()
    result = ctrl._wait_for_ground(since, timeout_s, ref_height=ref_height, land_sent_at=since)
    return result, time.monotonic() - started


# --- E: a wrongly confirmed land is noticed from the motor time ----------------

def _wrongly_confirmed_land(tello_on_wire, monkeypatch):
    """The PR #106 review's scenario: hovering at 80 cm, state stream lost, a late move ``ok`` confirms a land."""
    monkeypatch.setattr(tello_module, "_COMMAND_REPLY_TIMEOUT_S", 2.0)
    ctrl, wire = _flying_with_motors(tello_on_wire)

    def script(command: str):
        word = command.split()[0]
        if word == "forward":
            return [("ok", 5.0)]  # the move's own, late, answer
        if word in ("stop", "land"):
            return []  # ignored, or the reply is lost
        return None

    wire.script = script
    mover = threading.Thread(target=lambda: _move_ignoring_errors(ctrl, "forward", 400), daemon=True)
    mover.start()
    assert wait_for(lambda: "forward 400" in wire.sent)
    wire.stop_state()
    time.sleep(3.4)
    ctrl.land()  # returns None: the late ok is read as the land's reply
    mover.join(WAIT_S)
    return ctrl, wire


def test_land_confirmed_by_a_stale_ok_is_flagged_when_the_motors_keep_running(tello_on_wire, monkeypatch):
    ctrl, wire = _wrongly_confirmed_land(tello_on_wire, monkeypatch)
    # The reviewer's observation: the land is confirmed with the drone in the air.
    assert ctrl._land_unconfirmed is False and ctrl.state.is_flying is False

    wire.start_state()  # the stream returns: h=80, tof 90, motors on
    assert wait_for(lambda: ctrl._state_shows_flight())
    time.sleep(1.2)  # the motor time advances
    sent_before = list(wire.sent)
    ctrl._poll_once()
    assert ctrl._land_unconfirmed is False  # one poll is not enough
    ctrl._poll_once()

    assert ctrl._land_unconfirmed is True
    assert ctrl.state.is_flying is True
    # The poller sends no land of its own: only the executor's retry does.
    assert [c for c in wire.sent[len(sent_before):] if c.split()[0] in ("land", "stop")] == []
    # The keepalive stays off while the land is unconfirmed.
    assert ctrl._keepalive_once() is False


def test_the_flag_is_logged_once_and_the_retry_land_lands_the_drone(tello_on_wire, monkeypatch, capsys):
    ctrl, wire = _wrongly_confirmed_land(tello_on_wire, monkeypatch)
    wire.start_state()
    assert wait_for(lambda: ctrl._state_shows_flight())
    time.sleep(1.2)
    capsys.readouterr()
    for _ in range(4):
        ctrl._poll_once()
    assert capsys.readouterr().out.count("after a land was confirmed") == 1

    def script(command: str):
        if command == "land":
            def touch_down() -> None:
                wire.height_cm, wire.tof_cm = 0, 10
                wire.set_motors(False)

            timer = threading.Timer(0.2, touch_down)
            timer.daemon = True
            timer.start()
            return [("ok", 0.2)]
        return None

    wire.script = script
    ctrl.land()  # what the lane executor's monitor does
    assert ctrl._land_unconfirmed is False and ctrl.state.is_flying is False


def test_a_hand_carried_drone_with_motors_off_is_not_flagged(tello_on_wire):
    ctrl, wire = _flying_with_motors(tello_on_wire)
    wire.height_cm, wire.tof_cm = 0, 10
    wire.set_motors(False)
    ctrl.land()  # confirmed by state: on the ground, motors off
    assert ctrl._land_unconfirmed is False
    wire.height_cm, wire.tof_cm = 120, 130  # carried up by hand, motors off
    time.sleep(2.5)
    for _ in range(4):
        ctrl._poll_once()
        time.sleep(0.2)
    assert ctrl._land_unconfirmed is False and ctrl.state.is_flying is False


def test_a_normal_landing_is_not_flagged(tello_on_wire):
    ctrl, wire = _flying_with_motors(tello_on_wire)

    def script(command: str):
        if command == "land":
            def touch_down() -> None:
                wire.height_cm, wire.tof_cm = 0, 10
                wire.set_motors(False)

            timer = threading.Timer(0.3, touch_down)
            timer.daemon = True
            timer.start()
            return [("ok", 0.3)]
        return None

    wire.script = script
    ctrl.land()
    for _ in range(4):
        ctrl._poll_once()
        time.sleep(0.2)
    assert ctrl._land_unconfirmed is False and ctrl.state.is_flying is False


def test_a_new_takeoff_after_a_confirmed_land_is_not_flagged(tello_on_wire):
    ctrl, wire = _flying_with_motors(tello_on_wire)

    def landing(command: str):
        if command == "land":
            def touch_down() -> None:
                wire.height_cm, wire.tof_cm = 0, 10
                wire.set_motors(False)

            timer = threading.Timer(0.2, touch_down)
            timer.daemon = True
            timer.start()
            return [("ok", 0.2)]
        return None

    wire.script = landing
    ctrl.land()
    assert ctrl._post_land_watch is True
    wire.script = None
    assert ctrl.takeoff() is True
    wire.height_cm, wire.tof_cm = 80, 90
    wire.set_motors(True)
    assert wait_for(lambda: ctrl._state_shows_flight())
    time.sleep(1.2)
    for _ in range(4):
        ctrl._poll_once()
    assert ctrl._post_land_watch is False
    assert ctrl._land_unconfirmed is False and ctrl.state.is_flying is True


# --- F and the debounce: _wait_for_ground ---------------------------------------

@pytest.mark.parametrize("rate_cm_s", [15, 40, 80])
def test_a_steady_descent_is_never_sent_land_again(tello_on_wire, driver, rate_cm_s):
    ctrl, wire = tello_on_wire()
    driver(wire, lambda t: 500 - rate_cm_s * t)
    time.sleep(0.2)
    result, _ = _watch(ctrl, 2.5, ref_height=500)
    assert result == (False, "")


def test_a_frozen_height_with_a_live_state_stream_is_sent_land_again_after_the_stall(tello_on_wire, driver):
    ctrl, wire = tello_on_wire()
    driver(wire, lambda t: 80)
    time.sleep(0.2)
    (confirmed, why), elapsed = _watch(ctrl, 4.0, ref_height=80)
    assert confirmed is False and "no lower than" in why
    assert tello_module._LAND_STALL_S <= elapsed < 2.0


def test_a_noisy_slow_descent_is_not_mistaken_for_a_hover(tello_on_wire, driver):
    """6 cm/s with +-3 cm noise: single packets above the reference never send land again."""
    ctrl, wire = tello_on_wire()
    count = [0]

    def noisy(t: float) -> float:
        count[0] += 1
        return 300 - 6 * t + (3 if count[0] % 2 else -3)

    driver(wire, noisy)
    time.sleep(0.2)
    result, _ = _watch(ctrl, 3.0, ref_height=300)
    assert result == (False, "")


def test_one_stale_packet_amid_a_descent_does_not_send_land_again(tello_on_wire, driver):
    """A reordered packet carrying the old height, once, 1.5 s into a descent."""
    ctrl, wire = tello_on_wire()
    spiked = [False]

    def height(t: float) -> float:
        if t > 1.5 and not spiked[0]:
            spiked[0] = True
            return 400
        return 400 - 40 * t

    driver(wire, height)
    result, _ = _watch(ctrl, 3.0, ref_height=400)
    assert spiked[0]
    assert result == (False, "")


def test_no_known_height_at_land_time_measures_the_stall_from_the_first_packet(tello_on_wire, driver):
    """A drone descending 15 cm/s with the land sent long ago and no reference: not a stall."""
    ctrl, wire = tello_on_wire()
    driver(wire, lambda t: 300 - 15 * t)
    time.sleep(0.2)
    result, _ = _watch(ctrl, 2.5, ref_height=None, land_sent_ago=10.0)
    assert result == (False, "")


def test_no_known_height_at_land_time_still_catches_a_hover_after_the_stall(tello_on_wire, driver):
    ctrl, wire = tello_on_wire()
    driver(wire, lambda t: 80)
    time.sleep(0.2)
    (confirmed, why), elapsed = _watch(ctrl, 4.0, ref_height=None, land_sent_ago=10.0)
    assert confirmed is False and "no lower than" in why
    assert tello_module._LAND_STALL_S <= elapsed < 2.5


@pytest.mark.parametrize("omitted", ["tof", "time"])
def test_a_missing_tof_or_motor_time_still_detects_a_hover(tello_on_wire, driver, omitted):
    ctrl, wire = tello_on_wire()
    wire.omit_state_fields.add(omitted)
    driver(wire, lambda t: 80)
    time.sleep(0.2)
    (confirmed, why), _ = _watch(ctrl, 4.0, ref_height=80)
    assert confirmed is False and "no lower than" in why


# --- G: a failing state hook cannot kill the state thread --------------------------

def test_a_failing_state_hook_with_a_dead_stdout_does_not_raise(monkeypatch):
    def broken_hook(state: object) -> None:
        raise ValueError("bad packet")

    class Owner:
        def hook(self, state: object) -> None:
            broken_hook(state)

    owner = Owner()
    udp = _StateStampedUdpObject({"responses": []}, owner.hook, "drone-x")

    def dead_notice(*args, **kwargs):
        raise BrokenPipeError("stdout is gone")

    monkeypatch.setattr(tello_module, "notice", dead_notice)
    udp["state"] = {"h": "1"}  # must not raise into djitellopy's state thread
    udp["state"] = {"h": "2"}
    assert udp["state"] == {"h": "2"}


# --- the resend notice, channel holders and concurrent lands --------------------------

def _refusing_hover(tello_on_wire, monkeypatch, *, ground_wait_s=2.0, deadline_s=5.0, answer="error"):
    monkeypatch.setattr(tello_module, "_LAND_GROUND_WAIT_S", ground_wait_s)
    monkeypatch.setattr(tello_module, "_LAND_DEADLINE_S", deadline_s)
    ctrl, wire = _flying_with_motors(tello_on_wire)
    lands: list[float] = []

    def script(command: str):
        if command == "land":
            lands.append(time.monotonic())
            return [(answer, 0.02)]
        return None

    wire.script = script
    return ctrl, wire, lands


def test_resend_notice_is_not_printed_when_another_holder_keeps_the_channel(tello_on_wire, monkeypatch, capsys):
    ctrl, _, lands = _refusing_hover(tello_on_wire, monkeypatch)
    errors: list[BaseException] = []

    def land() -> None:
        try:
            ctrl.land()
        except RuntimeError as exc:
            errors.append(exc)

    lander = threading.Thread(target=land, daemon=True)
    lander.start()
    assert wait_for(lambda: len(lands) == 1)
    # A non-land holder (a keepalive, say) takes the channel before the resend.
    held = threading.Event()
    release = threading.Event()

    def hold_channel() -> None:
        while not ctrl._cmd_lock.acquire(blocking=False):
            time.sleep(0.001)
        held.set()
        release.wait(WAIT_S)
        ctrl._cmd_lock.release()

    holder = threading.Thread(target=hold_channel, daemon=True)
    holder.start()
    assert held.wait(WAIT_S)
    lander.join(WAIT_S)
    release.set()
    holder.join(WAIT_S)
    out = capsys.readouterr().out
    assert errors and not lander.is_alive()
    assert len(lands) == 1
    assert "sending land again" not in out


def test_resend_is_printed_when_it_is_sent(tello_on_wire, monkeypatch, capsys):
    ctrl, _, lands = _refusing_hover(tello_on_wire, monkeypatch, ground_wait_s=2.0, deadline_s=3.0)
    with pytest.raises(RuntimeError):
        ctrl.land()
    assert len(lands) >= 2
    assert "sending land again" in capsys.readouterr().out


def test_a_stale_ok_to_a_resend_confirms_nothing_while_state_shows_a_hover(tello_on_wire, monkeypatch):
    ctrl, wire = _flying_with_motors(tello_on_wire)
    monkeypatch.setattr(tello_module, "_LAND_GROUND_WAIT_S", 2.0)
    monkeypatch.setattr(tello_module, "_LAND_DEADLINE_S", 4.0)
    lands: list[float] = []

    def script(command: str):
        if command == "land":
            lands.append(time.monotonic())
            return [("error" if len(lands) == 1 else "ok", 0.02)]
        return None

    wire.script = script
    with pytest.raises(RuntimeError):
        ctrl.land()
    assert len(lands) >= 2
    assert ctrl._land_unconfirmed is True and ctrl.state.is_flying is True


def test_stream_loss_during_a_resend_does_not_confirm_a_drone_last_seen_hovering(tello_on_wire, monkeypatch):
    ctrl, wire = _flying_with_motors(tello_on_wire)
    monkeypatch.setattr(tello_module, "_LAND_GROUND_WAIT_S", 2.0)
    # The stream counts as lost 3 s after its last packet, and a later attempt's
    # fresh ok may then confirm (by design); this call ends before that.
    monkeypatch.setattr(tello_module, "_LAND_DEADLINE_S", 3.5)
    lands: list[float] = []

    def script(command: str):
        if command == "land":
            lands.append(time.monotonic())
            if len(lands) == 1:
                return [("error", 0.02)]
            wire.stop_state()  # the stream dies as the resend goes out
            return [("ok", 0.02)]
        return None

    wire.script = script
    with pytest.raises(RuntimeError):
        ctrl.land()
    assert len(lands) >= 2
    assert ctrl._land_unconfirmed is True and ctrl.state.is_flying is True


def test_concurrent_lands_on_a_refusing_drone_do_not_multiply_resends(tello_on_wire, monkeypatch):
    ctrl, _, lands = _refusing_hover(tello_on_wire, monkeypatch, ground_wait_s=3.0, deadline_s=3.5)
    errors: list[BaseException] = []

    def land() -> None:
        try:
            ctrl.land()
        except RuntimeError as exc:
            errors.append(exc)

    started = time.monotonic()
    threads = [threading.Thread(target=land, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT_S)
    assert len(errors) == 3
    # Three callers each send a confirming land (paced 0.1 s apart) at the
    # start. In the rest of the first ground wait only resends go out, and
    # they are one per _LAND_STALL_S for the drone, not one per caller.
    offsets = [t - started for t in lands]
    resends = [t for t in offsets if 0.6 <= t < 3.0]
    assert 1 <= len(resends) <= 3, offsets
    assert all(b - a >= 0.9 for a, b in itertools.pairwise(resends)), offsets


# --- the real controller checks takeoff's abort flag under the wire lock ----------------

def test_a_land_arriving_during_takeoffs_abort_check_waits_for_the_wire_lock(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_module, "_LAND_DEADLINE_S", 2.0)
    ctrl, wire = tello_on_wire()
    seen: list[bool] = []
    lander: list[threading.Thread] = []

    def land() -> None:
        try:
            ctrl.land()
        except RuntimeError:
            pass  # no state stream here, so the land is not confirmed

    class LandDuringCheck(threading.Event):
        def is_set(self) -> bool:
            if not lander:
                thread = threading.Thread(target=land, daemon=True)
                lander.append(thread)
                thread.start()
                thread.join(0.3)
                # The land could not even mark itself as started yet.
                seen.append(ctrl._landing)
            return False

    assert ctrl.takeoff(LandDuringCheck()) is True
    lander[0].join(WAIT_S)
    assert not lander[0].is_alive()
    assert seen == [False]
    assert wire.sent.index("takeoff") < wire.sent.index("land")
