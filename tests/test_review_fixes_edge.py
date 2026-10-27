"""Edge safety and tagging fixes from the PR #87 review (R-110).

* B1: the lane executor's monitor never waits on the ground link, so a
  stalled uplink cannot delay the link-loss landing.
* B2: Tello flight moves are sent once and never retried, and no move reaches
  the wire after ``land()`` starts.
* N2: the Tello keepalive holds the command lock for its whole exchange.
* R-113: Tello video-stream recovery (``streamoff`` and ``streamon``) holds the
  command lock for each exchange, and a land always takes the channel first.
* N7: ``takeoff()`` refuses while a land is in progress.
* R-116: a Tello land is confirmed by fresh state packets showing the drone
  on the ground, whatever the command replies were; with no fresh state the
  R-114 reply-based confirmation applies.
* B3: a flying edge tags every detection with its drone id and pose.
* B4: a flying edge is appearance-only by default: no ``FaceRecognizer`` and
  no ``face_embedding`` unless ``--with-face`` or
  ``FLIGHTRISK_APPEARANCE_ONLY=0``.

Nothing here touches a real drone or the network. Tello tests run the real
``djitellopy.Tello`` client over ``FakeTelloWire`` (in ``tests/conftest.py``),
which replaces its UDP command socket and answers like a Tello, so
djitellopy's own send and retry code is exercised. The B3 wiring test runs a real ``GroundTransportSync`` on
127.0.0.1. Timing assertions have generous margins because many test
processes may share the machine.
"""
from __future__ import annotations

import secrets
import sys
import threading
import time
import types
from typing import Callable

import numpy as np
import pytest

from flightrisk.dashboard.app import FleetSupervisor
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone import tello as tello_module
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController, TelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import _build_parser, _build_runner, _make_sender, _run_flight_edge
from flightrisk.observability import StructuredLogger
from flightrisk.transport import GroundTransportSync
from tests.conftest import TELLO_FLIGHT_WORDS

WAIT_S = 10.0

FLIGHT_WORDS = TELLO_FLIGHT_WORDS


def wait_for(predicate: Callable[[], object], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def flight_commands(commands: list[str]) -> list[str]:
    return [c for c in commands if c.split()[0] in FLIGHT_WORDS]


# ---------------------------------------------------------------------------
# B2: no move retries, no move after land() starts
# ---------------------------------------------------------------------------


def test_djitellopy_client_never_retries(tello_on_wire):
    ctrl, _ = tello_on_wire()
    assert ctrl.tello.retry_count == 1


def test_no_move_sent_after_land_starts(tello_on_wire):
    ctrl, wire = tello_on_wire(hold={"forward 200"})
    assert ctrl.takeoff() is True

    errors: list[Exception] = []

    def fly() -> None:
        try:
            ctrl.move("forward", 200)
        except Exception as exc:
            errors.append(exc)

    mover = threading.Thread(target=fly, daemon=True)
    mover.start()
    assert wire.entered.wait(WAIT_S)

    # LAND while the move is in flight. The Tello answers the interrupted
    # move with "error"; djitellopy's retry would have sent it again.
    ctrl.land()
    mover.join(WAIT_S)
    assert not mover.is_alive()

    assert wire.sent == ["takeoff", "forward 200", "stop", "land", "land"]
    land_started = wire.sent.index("stop")
    assert flight_commands(wire.sent[land_started:]) == []
    assert len(errors) == 1 and "not retried" in str(errors[0])
    assert ctrl.state.is_flying is False

    # Nothing that flies is sent behind the landing.
    assert ctrl.move("back", 100) is False
    assert ctrl.rotate(90) is False
    ctrl.rc_control(0, 50, 0, 0)
    assert flight_commands(wire.sent[land_started:]) == []


def test_move_refused_when_land_starts_just_before_send(tello_on_wire):
    """A land that starts after move() checked the flag still stops the move."""
    ctrl, wire = tello_on_wire()
    assert ctrl.takeoff() is True

    real_pace = ctrl._pace_commands
    lander = threading.Thread(target=ctrl.land, daemon=True)

    def pace_then_land() -> None:
        # Runs inside move(), after its first landing check and before the
        # command is written. Start a landing right here.
        real_pace()
        if not lander.is_alive() and not ctrl._landing:
            lander.start()
            assert wait_for(lambda: "stop" in wire.sent)

    ctrl._pace_commands = pace_then_land
    assert ctrl.move("forward", 200) is False
    lander.join(WAIT_S)
    assert not lander.is_alive()
    assert "forward 200" not in wire.sent
    assert wire.sent[:3] == ["takeoff", "stop", "land"]
    assert ctrl.state.is_flying is False


def test_move_error_reply_raises_without_retry(tello_on_wire):
    ctrl, wire = tello_on_wire(hold={"right 100"})
    assert ctrl.takeoff() is True
    result: list[object] = []

    def fly() -> None:
        try:
            result.append(ctrl.move("right", 100))
        except RuntimeError as exc:
            result.append(exc)

    mover = threading.Thread(target=fly, daemon=True)
    mover.start()
    assert wire.entered.wait(WAIT_S)
    wire.release("right 100", reply="error No valid imu")
    mover.join(WAIT_S)
    assert isinstance(result[0], RuntimeError)
    assert wire.sent.count("right 100") == 1


def test_disconnect_lands_a_flying_drone_first(tello_on_wire):
    # djitellopy's end() would land only once now that it never retries,
    # so disconnect() lands through land() first.
    ctrl, wire = tello_on_wire()
    assert ctrl.takeoff() is True
    ctrl.disconnect()
    assert wire.sent == ["takeoff", "land"]
    assert ctrl.state.is_flying is False


# ---------------------------------------------------------------------------
# N2: the keepalive takes the command lock
# ---------------------------------------------------------------------------


def test_keepalive_takes_command_lock(tello_on_wire):
    ctrl, wire = tello_on_wire(hold={"command"})
    # (command, command lock held, keepalive on the channel) at each send.
    seen: list[tuple[str, bool, bool]] = []
    wire.on_send = lambda command: seen.append(
        (command, ctrl._cmd_lock.locked(), ctrl._keepalive_on_channel)
    )

    # While a flight command holds the channel the keepalive is skipped.
    assert ctrl._cmd_lock.acquire(timeout=WAIT_S)
    try:
        assert ctrl._keepalive_once() is False
    finally:
        ctrl._cmd_lock.release()
    assert "command" not in wire.sent

    # Otherwise it holds the lock until its own reply has arrived.
    keepalive = threading.Thread(target=ctrl._keepalive_once, daemon=True)
    keepalive.start()
    assert wire.entered.wait(WAIT_S)
    assert seen == [("command", True, True)]
    assert ctrl._cmd_lock.locked()

    # A land meanwhile does not wait: stop and land go out at once. The
    # confirming land is sent only once the keepalive has read a reply and
    # left the channel, so the two exchanges never overlap.
    ctrl.state.is_flying = True
    lander = threading.Thread(target=ctrl.land, daemon=True)
    lander.start()
    keepalive.join(WAIT_S)
    lander.join(WAIT_S)
    assert not keepalive.is_alive() and not lander.is_alive()
    assert wire.sent == ["command", "stop", "land", "land"]
    assert seen[1] == ("stop", True, True)
    assert seen[3] == ("land", True, False)
    assert ctrl.state.is_flying is False


def test_move_waits_behind_keepalive_instead_of_dropping(tello_on_wire):
    ctrl, wire = tello_on_wire(hold={"command"})
    assert ctrl.takeoff() is True
    keepalive = threading.Thread(target=ctrl._keepalive_once, daemon=True)
    keepalive.start()
    assert wire.entered.wait(WAIT_S)

    moved: list[bool] = []
    mover = threading.Thread(target=lambda: moved.append(ctrl.move("forward", 50)), daemon=True)
    mover.start()
    time.sleep(0.2)
    assert "forward 50" not in wire.sent  # waiting, not interleaved
    wire.release("command")
    mover.join(WAIT_S)
    keepalive.join(WAIT_S)
    assert moved == [True]
    assert wire.sent == ["takeoff", "command", "forward 50"]


# ---------------------------------------------------------------------------
# R-113: video-stream recovery takes the command lock, and a land wins
# ---------------------------------------------------------------------------


def test_stream_recovery_takes_command_lock(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_module, "_STREAM_RESTART_PAUSE_S", 0.05)
    ctrl, wire = tello_on_wire()
    # Recovery waits for a busy channel only while the controller runs.
    ctrl._running = True
    # (command, command lock held, stream recovery on the channel, lands in
    # progress) at each send.
    seen: list[tuple[str, bool, bool, int]] = []
    wire.on_send = lambda command: seen.append(
        (command, ctrl._cmd_lock.locked(), ctrl._stream_on_channel, ctrl._lands_in_progress)
    )
    assert ctrl.takeoff() is True

    # A land in progress holds the channel. Recovery sends nothing until the
    # land has finished, so it never delays the land.
    wire.hold = {"land"}
    lander = threading.Thread(target=ctrl.land, daemon=True)
    lander.start()
    assert wire.entered.wait(WAIT_S)
    ctrl._recover_stream()
    time.sleep(0.3)
    assert wire.sent == ["takeoff", "land"]
    wire.release("land")
    lander.join(WAIT_S)
    assert not lander.is_alive()
    assert wait_for(lambda: not ctrl._stream_recovering)
    assert wire.sent == ["takeoff", "land", "streamoff", "streamon"]
    # Each stream command is sent holding the command lock, after the land.
    assert seen[2:] == [("streamoff", True, True, 0), ("streamon", True, True, 0)]
    assert not ctrl._cmd_lock.locked() and ctrl._stream_on_channel is False
    assert ctrl.tello.stream_on is True

    # A land that starts while a stream command is on the wire does not wait
    # for it: stop goes out at once, and land follows without waiting for
    # the channel. The confirming land is sent only once the stream command
    # has read a reply and left the channel, and streamon is sent only once
    # the land has finished.
    assert ctrl.takeoff() is True
    start = len(wire.sent)
    wire.hold = {"streamoff"}
    wire.entered.clear()
    ctrl._recover_stream()
    assert wire.entered.wait(WAIT_S)
    assert ctrl._cmd_lock.locked() and ctrl._stream_on_channel
    ctrl.land()
    assert wait_for(lambda: not ctrl._stream_recovering)
    assert wire.sent[start - 1:] == ["takeoff", "streamoff", "stop", "land", "land", "streamon"]
    assert seen[start + 1] == ("stop", True, True, 1)
    # The immediate land is paced _TIME_BETWEEN_COMMANDS_S after the stop
    # (R-114). The stream command reads the stop's "ok" in the meantime, so
    # whether it still holds the channel then depends on scheduling.
    assert seen[start + 2][0] == "land" and seen[start + 2][3] == 1
    assert seen[start + 3:] == [
        ("land", True, False, 1),
        ("streamon", True, True, 0),
    ]
    assert ctrl.state.is_flying is False

    # The land check is made in the same step as the send: with a land in
    # progress (here, one waiting for the channel) nothing is sent and the
    # channel is left free.
    time.sleep(0.2)  # past djitellopy's minimum gap between commands
    sent_before = len(wire.sent)
    with ctrl._wire_lock:
        ctrl._lands_in_progress += 1
    try:
        assert ctrl._stream_command_once("streamon") is False
    finally:
        with ctrl._wire_lock:
            ctrl._lands_in_progress -= 1
    assert len(wire.sent) == sent_before
    assert not ctrl._cmd_lock.locked()
    assert ctrl._stream_command_once("streamon") is True
    assert wire.sent[sent_before:] == ["streamon"]


def test_move_waits_behind_stream_recovery_instead_of_dropping(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_module, "_STREAM_RESTART_PAUSE_S", 0.05)
    ctrl, wire = tello_on_wire(hold={"streamoff"})
    ctrl._running = True
    assert ctrl.takeoff() is True
    ctrl._recover_stream()
    assert wire.entered.wait(WAIT_S)

    moved: list[bool] = []
    mover = threading.Thread(target=lambda: moved.append(ctrl.move("forward", 50)), daemon=True)
    mover.start()
    time.sleep(0.2)
    assert "forward 50" not in wire.sent  # waiting, not interleaved
    wire.release("streamoff")
    mover.join(WAIT_S)
    assert moved == [True]
    assert wait_for(lambda: not ctrl._stream_recovering)
    assert wire.sent[:2] == ["takeoff", "streamoff"]
    assert sorted(wire.sent[2:]) == ["forward 50", "streamon"]


# ---------------------------------------------------------------------------
# N7: no takeoff during a land
# ---------------------------------------------------------------------------


def test_takeoff_refused_while_landing(tello_on_wire):
    ctrl, wire = tello_on_wire(hold={"land"})
    assert ctrl.takeoff() is True

    # A land whose reply has not arrived yet holds the channel.
    lander = threading.Thread(target=ctrl.land, daemon=True)
    lander.start()
    assert wire.entered.wait(WAIT_S)

    # A takeoff called now waits for the channel, then refuses: it must
    # never run straight after the landing it waited behind.
    outcome: list[bool] = []
    taker = threading.Thread(target=lambda: outcome.append(ctrl.takeoff()), daemon=True)
    taker.start()
    time.sleep(0.2)
    wire.release("land")
    lander.join(WAIT_S)
    taker.join(WAIT_S)
    assert outcome == [False]
    assert wire.sent == ["takeoff", "land"]
    assert ctrl.state.is_flying is False

    # With an abort flag set, takeoff sends nothing.
    aborted = threading.Event()
    aborted.set()
    assert ctrl.takeoff(abort=aborted) is False
    assert wire.sent == ["takeoff", "land"]

    # A takeoff called after the landing finished re-arms the drone.
    assert ctrl.takeoff() is True
    assert ctrl.move("forward", 50) is True
    assert wire.sent == ["takeoff", "land", "takeoff", "forward 50"]


def test_executor_takeoff_refused_when_land_lands_first():
    """A land that completes between the executor's abort check and takeoff wins."""

    class LandRacesTakeoff(FakeTelloController):
        executor: LaneExecutor | None = None

        def takeoff(self, abort=None):
            # The ground's LAND arrives just as the flight thread reaches
            # drone.takeoff(), after _do_takeoff checked the abort flag.
            self.executor.land_now("ground_land")
            return super().takeoff(abort=abort)

    drone = LandRacesTakeoff(name="drone-1", battery=90)
    drone.connect()
    executor = LaneExecutor(
        drone,
        SafetyEnvelope(alt_slot_cm=100),
        "drone-1",
        send=lambda msg: None,
        logger=StructuredLogger(component="test-review-fixes-edge"),
    )
    drone.executor = executor
    try:
        executor.note_ground_message()
        assert executor.handle_takeoff()
        assert executor.wait_idle(WAIT_S)
        assert ("takeoff",) not in drone.calls
        assert drone.state.is_flying is False
    finally:
        executor.shutdown(timeout=2.0)


# ---------------------------------------------------------------------------
# R-114 (PR #87 re-review R1): a land counts only on a fresh "ok"
# ---------------------------------------------------------------------------


def test_land_unconfirmed_raises_after_attempts(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_module, "_LAND_REPLY_TIMEOUT_S", 0.2)
    attempts = tello_module._LAND_ATTEMPTS
    # Every land packet is lost on Wi-Fi.
    ctrl, wire = tello_on_wire(drop={"land"})
    assert ctrl.takeoff() is True

    with pytest.raises(RuntimeError, match="land not confirmed"):
        ctrl.land()
    assert wire.sent == ["takeoff"] + ["land"] * attempts
    # Still flying as far as anyone can tell, so callers keep retrying.
    assert ctrl.state.is_flying is True
    assert not ctrl._cmd_lock.locked() and ctrl._lands_in_progress == 0

    # Until a land is confirmed nothing else goes out: a keepalive or a
    # stream command would restart the Tello's own 15 s auto-land timer.
    assert ctrl._keepalive_once() is False
    assert ctrl._stream_command_once("streamon") is False
    assert ctrl.move("forward", 50) is False
    assert wire.sent == ["takeoff"] + ["land"] * attempts

    # The lane executor's monitor keeps retrying while the drone reports
    # flying...
    now = [1000.0]

    def clock() -> float:
        return now[0]

    executor = LaneExecutor(
        ctrl,
        SafetyEnvelope(alt_slot_cm=100, clock=clock),
        "drone-1",
        send=lambda msg: None,
        clock=clock,
        logger=StructuredLogger(component="test-review-fixes-edge"),
    )
    try:
        executor.note_ground_message()
        executor.land_now("emergency_land_all")
        assert ctrl.state.is_flying is True
        assert wire.sent.count("land") == 2 * attempts

        # ...and once Wi-Fi recovers, its next retry is confirmed by the
        # land's own reply.
        wire.drop.clear()
        now[0] += executor.land_retry_s
        executor.note_ground_message()
        executor.tick()
        assert ctrl.state.is_flying is False
        assert wire.sent.count("land") == 2 * attempts + 1
    finally:
        executor.shutdown(timeout=2.0)

    # Once a land is confirmed the keepalive runs again.
    assert ctrl._keepalive_once() is True
    assert wire.sent[-1] == "command"


def test_stale_stop_ok_not_taken_as_land_confirmation(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_module, "_LAND_REPLY_TIMEOUT_S", 0.2)
    attempts = tello_module._LAND_ATTEMPTS
    ctrl, wire = tello_on_wire(hold={"forward 200"}, drop={"land"})
    assert ctrl.takeoff() is True

    errors: list[Exception] = []

    def fly() -> None:
        try:
            ctrl.move("forward", 200)
        except Exception as exc:
            errors.append(exc)

    mover = threading.Thread(target=fly, daemon=True)
    mover.start()
    assert wire.entered.wait(WAIT_S)

    # LAND interrupts the move. The Tello answers the move "error" and the
    # stop "ok", and every land packet is lost. The stop's "ok" is still
    # queued when the land confirms; it says nothing about the land.
    with pytest.raises(RuntimeError, match="land not confirmed"):
        ctrl.land()
    mover.join(WAIT_S)
    assert not mover.is_alive() and len(errors) == 1
    assert wire.sent == ["takeoff", "forward 200", "stop", "land"] + ["land"] * attempts
    assert ctrl.state.is_flying is True
    assert ctrl._keepalive_once() is False

    # A land that gets through is confirmed by its own reply.
    wire.drop.clear()
    ctrl.land()
    assert ctrl.state.is_flying is False
    assert wire.sent == ["takeoff", "forward 200", "stop", "land"] + ["land"] * (attempts + 1)


def test_stop_and_land_paced(tello_on_wire):
    ctrl, wire = tello_on_wire(hold={"forward 200"})
    sent_at: list[tuple[str, float]] = []
    wire.on_send = lambda command: sent_at.append((command, time.monotonic()))
    assert ctrl.takeoff() is True

    mover = threading.Thread(target=lambda: _move_ignoring_errors(ctrl, "forward", 200), daemon=True)
    mover.start()
    assert wire.entered.wait(WAIT_S)
    land_called = time.monotonic()
    ctrl.land()
    mover.join(WAIT_S)
    assert not mover.is_alive()

    assert [command for command, _ in sent_at] == ["takeoff", "forward 200", "stop", "land", "land"]
    t_stop, t_land, t_confirm = (t for _, t in sent_at[2:])
    gap = tello_module._TIME_BETWEEN_COMMANDS_S
    # The Tello ignores a command sent too soon after another, so the
    # immediate land follows the stop by at least the gap, and so does the
    # confirming land after it.
    assert t_land - t_stop >= gap
    assert t_confirm - t_land >= gap
    # The stop still goes out at once.
    assert t_stop - land_called < 1.0
    assert ctrl.state.is_flying is False


def _move_ignoring_errors(ctrl: TelloController, direction: str, distance_cm: int) -> None:
    try:
        ctrl.move(direction, distance_cm)
    except RuntimeError:
        pass


def test_second_land_does_not_disturb_a_confirming_land(tello_on_wire):
    """A second LAND sends no stop or land whose replies the first could read as its own."""
    ctrl, wire = tello_on_wire(hold={"land"})
    assert ctrl.takeoff() is True
    first = threading.Thread(target=ctrl.land, daemon=True)
    first.start()
    assert wire.entered.wait(WAIT_S)  # the first land waits for its reply

    second = threading.Thread(target=ctrl.land, daemon=True)
    second.start()
    assert wait_for(lambda: ctrl._lands_in_progress == 2)
    time.sleep(0.3)
    assert wire.sent == ["takeoff", "land"]
    wire.release("land")
    first.join(WAIT_S)
    second.join(WAIT_S)
    assert not first.is_alive() and not second.is_alive()
    # The first land was confirmed after the second was asked for, so the
    # second has nothing left to send.
    assert wire.sent == ["takeoff", "land"]
    assert ctrl.state.is_flying is False
    assert ctrl._land_unconfirmed is False


# ---------------------------------------------------------------------------
# R-114 (PR #87 re-review R2): stream recovery gets in between lane moves
# ---------------------------------------------------------------------------


def test_stream_recovery_gets_in_between_lane_moves(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_module, "_STREAM_RESTART_PAUSE_S", 0.05)
    # Each lane move takes 0.25 s to answer; everything else is answered at once.
    ctrl, wire = tello_on_wire(delays={"forward": 0.25, "back": 0.25})
    ctrl._running = True
    # (command, command lock held, stream recovery on the channel) at each send.
    seen: list[tuple[str, bool, bool]] = []
    wire.on_send = lambda command: seen.append(
        (command, ctrl._cmd_lock.locked(), ctrl._stream_on_channel)
    )
    assert ctrl.takeoff() is True

    moves = 10
    moved: list[bool] = []

    def fly_lanes() -> None:
        # Back to back, as the lane executor flies them.
        for i in range(moves):
            moved.append(ctrl.move("forward" if i % 2 == 0 else "back", 100))

    flier = threading.Thread(target=fly_lanes, daemon=True)
    flier.start()
    assert wait_for(lambda: "forward 100" in wire.sent)
    ctrl._recover_stream()
    flier.join(WAIT_S * 3)
    assert not flier.is_alive()
    assert wait_for(lambda: not ctrl._stream_recovering)

    # No lane move was dropped.
    assert moved == [True] * moves
    commands = [command for command, _, _ in seen]
    move_at = [i for i, command in enumerate(commands) if command.split()[0] in ("forward", "back")]
    off, on = commands.index("streamoff"), commands.index("streamon")
    # The stream was restarted during the lanes, not after them...
    assert move_at[0] < off < on < move_at[-1]
    # ...each stream command holding the channel, so none overlapped a move.
    assert seen[off] == ("streamoff", True, True)
    assert seen[on] == ("streamon", True, True)
    assert all(seen[i][2] is False for i in move_at)
    assert ctrl.tello.stream_on is True


def test_stream_gap_wait_never_delays_a_land(tello_on_wire, monkeypatch):
    """A land that starts while stream recovery waits out the command gap goes first."""
    monkeypatch.setattr(tello_module, "_STREAM_RESTART_PAUSE_S", 0.05)
    ctrl, wire = tello_on_wire()
    ctrl._running = True
    # (command, command lock held, stream recovery on the channel, lands in
    # progress) at each send.
    seen: list[tuple[str, bool, bool, int]] = []
    wire.on_send = lambda command: seen.append(
        (command, ctrl._cmd_lock.locked(), ctrl._stream_on_channel, ctrl._lands_in_progress)
    )
    assert ctrl.takeoff() is True

    lander = threading.Thread(target=ctrl.land, daemon=True)
    real_gap = ctrl._command_gap_left

    def gap_then_land() -> float:
        # Stream recovery asks how much of the gap is left while it holds
        # the channel. Start a landing right here.
        if threading.current_thread().name.endswith("stream-recover") and not ctrl._landing:
            lander.start()
            assert wait_for(lambda: "stop" in wire.sent)
            return tello_module._TIME_BETWEEN_COMMANDS_S
        return real_gap()

    ctrl._command_gap_left = gap_then_land
    ctrl._recover_stream()
    assert wait_for(lambda: ctrl._landing)
    lander.join(WAIT_S)
    assert not lander.is_alive()
    assert wait_for(lambda: not ctrl._stream_recovering)

    # The stop went out while the stream command held the channel, and the
    # stream command was not sent until the land had been confirmed.
    assert wire.sent == ["takeoff", "stop", "land", "land", "streamoff", "streamon"]
    assert seen[1] == ("stop", True, True, 1)
    assert seen[4:] == [("streamoff", True, True, 0), ("streamon", True, True, 0)]
    assert ctrl.state.is_flying is False


# ---------------------------------------------------------------------------
# R-116 (PR #87 re-review 2, N1-N3, N7): the state stream confirms a land
# ---------------------------------------------------------------------------


class LandingTello:
    """Scripted Tello behaviour for ``FakeTelloWire.script``, modelled on the reviewer's probes.

    A move is answered only when a ``stop`` interrupts it, with ``error``;
    the ``stop`` itself is answered ``ok``. The first ``land`` that reaches
    a flying drone starts a descent of ``descent_s`` and is answered ``ok``
    at touchdown, when the state height drops to 0. A ``land`` that arrives
    while the drone is already landing, or on the ground, is answered with
    ``late_land_reply`` (None: never answered). Every other command is left
    to the wire's default ``ok``.
    """

    def __init__(self, wire, descent_s: float = 0.8, late_land_reply: str | None = "error"):
        self.wire = wire
        self.descent_s = descent_s
        self.late_land_reply = late_land_reply
        self.move_in_flight = False
        self.landing = False
        self.touched_down_at: float | None = None

    def _touch_down(self) -> None:
        self.wire.height_cm = 0
        self.landing = False
        self.touched_down_at = time.monotonic()

    def __call__(self, command: str):
        word = command.split()[0]
        if word in ("forward", "back", "left", "right", "up", "down", "cw", "ccw"):
            self.move_in_flight = True
            return []
        if word == "stop":
            replies = [("error", 0.0)] if self.move_in_flight else []
            self.move_in_flight = False
            return replies + [("ok", 0.01)]
        if word == "land":
            if self.landing or self.wire.height_cm == 0:
                return [] if self.late_land_reply is None else [(self.late_land_reply, 0.02)]
            self.landing = True
            self.wire.height_cm = 40
            timer = threading.Timer(self.descent_s, self._touch_down)
            timer.daemon = True
            timer.start()
            return [("ok", self.descent_s)]
        return None


def _flying_on_state(tello_on_wire, **wire_kwargs):
    """A ``TelloController`` that has taken off and whose wire streams state at 80 cm."""
    ctrl, wire = tello_on_wire(**wire_kwargs)
    wire.start_state(0)
    assert ctrl.takeoff() is True
    wire.height_cm = 80
    # The controller has seen the drone in the air (so ground readings
    # count again after the takeoff) and fresh state does not show it down.
    assert wait_for(lambda: ctrl._fresh_height() == 80)
    assert ctrl._ground_by_state() is False
    return ctrl, wire


def test_land_confirmed_by_state_height_despite_error_reply(tello_on_wire, capsys):
    """N1: a Tello that answers 'error' to a land sent while it is already landing is confirmed once state shows it down."""
    ctrl, wire = _flying_on_state(tello_on_wire)
    tello = LandingTello(wire)
    wire.script = tello

    mover = threading.Thread(target=lambda: _move_ignoring_errors(ctrl, "forward", 200), daemon=True)
    mover.start()
    assert wait_for(lambda: "forward 200" in wire.sent)

    # LAND interrupts the move: stop, then the immediate land, which starts
    # the descent. The confirming land arrives while the drone is landing,
    # and the Tello answers it "error".
    ctrl.land()
    returned_at = time.monotonic()
    mover.join(WAIT_S)
    assert not mover.is_alive()

    # The land was confirmed by the state stream, only once the drone was down.
    assert tello.touched_down_at is not None and returned_at >= tello.touched_down_at
    assert ctrl.state.is_flying is False
    assert ctrl._land_unconfirmed is False
    # No land was repeated: the "error" did not count as a failed attempt.
    assert wire.sent == ["takeoff", "forward 200", "stop", "land", "land"]
    out = capsys.readouterr().out
    assert "Landing confirmed." in out
    assert "not confirmed" not in out

    # The keepalive and stream recovery run again.
    assert ctrl._keepalive_once() is True
    assert ctrl._stream_command_once("streamon") is True
    assert wire.sent[-2:] == ["command", "streamon"]


def test_stale_ok_does_not_confirm_while_state_airborne(tello_on_wire, monkeypatch, capsys):
    """N2: a late 'ok' read as the land's reply confirms nothing while fresh state shows the drone in the air."""
    # Long enough that the scripted late "ok" is read even on a loaded machine.
    monkeypatch.setattr(tello_module, "_LAND_REPLY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(tello_module, "_LAND_GROUND_WAIT_S", 0.5)
    attempts = tello_module._LAND_ATTEMPTS
    ctrl, wire = _flying_on_state(tello_on_wire)
    lands_sent = [0]
    move_in_flight = [False]

    def lossy(command: str):
        word = command.split()[0]
        if word == "forward":
            move_in_flight[0] = True
            return []
        if word == "stop":
            # The interrupted move is answered at once; the stop's own "ok"
            # is held up on Wi-Fi.
            replies = [("error", 0.0)] if move_in_flight[0] else []
            move_in_flight[0] = False
            return replies
        if word == "land":
            # Every land packet is lost and the drone hovers at 80 cm. The
            # stop's late "ok" turns up just after the first confirming land
            # (the second land on the wire), so it is read as that land's
            # reply.
            lands_sent[0] += 1
            return [("ok", 0.05)] if lands_sent[0] == 2 else []
        return None

    wire.script = lossy
    # (lands on the wire, command lock held) while the land runs.
    samples: list[tuple[int, bool]] = []
    sampling = threading.Event()

    def sample() -> None:
        while not sampling.is_set():
            samples.append((wire.sent.count("land"), ctrl._cmd_lock.locked()))
            time.sleep(0.005)

    mover = threading.Thread(target=lambda: _move_ignoring_errors(ctrl, "forward", 200), daemon=True)
    mover.start()
    assert wait_for(lambda: "forward 200" in wire.sent)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        with pytest.raises(RuntimeError, match="land not confirmed"):
            ctrl.land()
    finally:
        sampling.set()
        sampler.join(WAIT_S)
    mover.join(WAIT_S)
    assert not mover.is_alive()

    assert wire.sent == ["takeoff", "forward 200", "stop", "land"] + ["land"] * attempts
    out = capsys.readouterr().out
    assert "Land attempt 1/3 not confirmed: answered 'ok', but fresh state still shows height 80 cm" in out
    # Still flying as far as anyone can tell, so callers keep retrying, and
    # nothing restarts the Tello's own auto-land timer.
    assert ctrl.state.is_flying is True
    assert ctrl._land_unconfirmed is True
    assert ctrl._keepalive_once() is False
    assert ctrl._stream_command_once("streamon") is False
    # While waiting for the state stream after that "ok", the land held no lock.
    assert any(not locked for lands, locked in samples if lands == 2)

    # A land that gets through is confirmed once state shows the drone down.
    tello = LandingTello(wire, descent_s=0.3)
    wire.script = tello
    ctrl.land()
    assert tello.touched_down_at is not None
    assert ctrl.state.is_flying is False
    assert ctrl._land_unconfirmed is False
    assert ctrl._keepalive_once() is True


def test_land_on_ground_with_error_reply_clears_flags(tello_on_wire):
    """N3: LAND on a drone already on the ground, answered 'error' or not at all, leaves nothing stuck."""
    ctrl, wire = _flying_on_state(tello_on_wire)
    tello = LandingTello(wire, descent_s=0.3)
    wire.script = tello
    ctrl.land()
    assert ctrl.state.is_flying is False
    assert ctrl._keepalive_once() is True

    # LAND ALL again with the drone on the ground: the Tello answers "error".
    assert wait_for(lambda: ctrl._ground_by_state() is True)
    ctrl.land()
    assert ctrl.state.is_flying is False
    assert ctrl._land_unconfirmed is False
    assert wire.sent.count("land") == 2
    assert ctrl._keepalive_once() is True
    assert ctrl._stream_command_once("streamon") is True

    # A grounded Tello that never answers the land: confirmed from state
    # straight away, so the land does not hold the command channel for its
    # reply timeouts.
    tello.late_land_reply = None
    started = time.monotonic()
    ctrl.land()
    assert time.monotonic() - started < 3.0
    assert wire.sent.count("land") == 3
    assert ctrl._land_unconfirmed is False
    assert not ctrl._cmd_lock.locked()
    assert ctrl._keepalive_once() is True


def test_poller_zero_height_clears_land_unconfirmed(tello_on_wire, monkeypatch, capsys):
    monkeypatch.setattr(tello_module, "_STATE_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(tello_module, "_LAND_REPLY_TIMEOUT_S", 0.2)
    ctrl, wire = _flying_on_state(tello_on_wire, drop={"land"})

    # Every land packet is lost, so the land gives up with the drone in the air.
    with pytest.raises(RuntimeError, match="land not confirmed"):
        ctrl.land()
    assert ctrl._land_unconfirmed is True

    polls: list[int] = []
    real_check = ctrl._check_zero_height

    def counted_check() -> None:
        polls.append(ctrl.state.height)
        real_check()

    ctrl._check_zero_height = counted_check

    # The Tello's own auto-land brings it down, but its state stream has gone
    # silent: the last packet says 0 and grows stale. A stale height does not
    # clear the unconfirmed land.
    wire.stop_state()
    wire.height_cm = 0
    for _ in range(3):
        wire.send_state()
    time.sleep(tello_module._STATE_FRESH_S + 0.2)
    ctrl._running = True
    ctrl._start_state_polling()
    assert wait_for(lambda: len(polls) >= 2 * tello_module._ZERO_HEIGHT_POLLS)
    assert ctrl._land_unconfirmed is True
    assert ctrl._keepalive_once() is False

    # Once fresh state shows height 0, the poller's zero-height path
    # confirms the land.
    wire.start_state()
    assert wait_for(lambda: ctrl._land_unconfirmed is False)
    assert ctrl.state.is_flying is False
    assert "Land confirmed: height 0 for 3 polls." in capsys.readouterr().out
    assert ctrl._keepalive_once() is True
    assert ctrl._stream_command_once("streamon") is True


def test_no_fresh_state_falls_back_to_reply_confirmation(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_module, "_LAND_REPLY_TIMEOUT_S", 0.2)
    # Replies decide only once no state height has arrived for this long
    # (shortened here to keep the test quick).
    monkeypatch.setattr(tello_module, "_STATE_SEEN_S", 1.0)
    attempts = tello_module._LAND_ATTEMPTS
    ctrl, wire = tello_on_wire()

    # No state packet has ever arrived: the land's own "ok" confirms it.
    assert ctrl.takeoff() is True
    assert ctrl._ground_by_state() is None
    ctrl.land()
    assert ctrl.state.is_flying is False
    assert wire.sent == ["takeoff", "land"]

    # The state stream showed the drone in the air, then went silent for
    # _STATE_SEEN_S: the land's own "ok" confirms it.
    assert ctrl.takeoff() is True
    wire.start_state(80)
    assert wait_for(lambda: ctrl._ground_by_state() is False)
    wire.stop_state()
    time.sleep(tello_module._STATE_SEEN_S + 0.2)
    assert ctrl._ground_by_state() is None
    ctrl.land()
    assert ctrl.state.is_flying is False
    assert wire.sent == ["takeoff", "land", "takeoff", "land"]

    # A stale height of 0 confirms nothing. With every land lost, the land
    # raises after its attempts, as before the state stream was read.
    assert ctrl.takeoff() is True
    wire.height_cm = 0
    for _ in range(3):
        wire.send_state()
    time.sleep(tello_module._STATE_SEEN_S + 0.2)
    assert ctrl._ground_by_state() is None
    wire.drop = {"land"}
    with pytest.raises(RuntimeError, match="land not confirmed"):
        ctrl.land()
    assert wire.sent == ["takeoff", "land", "takeoff", "land", "takeoff"] + ["land"] * attempts
    assert ctrl.state.is_flying is True
    assert ctrl._land_unconfirmed is True
    assert ctrl._keepalive_once() is False


def test_raw_land_not_blocked_by_channel_holder(tello_on_wire):
    """N7: the immediate land goes out while another command holds the channel and the stop is lost."""
    ctrl, wire = tello_on_wire(drop={"stop"})
    # (command, time sent, command lock held) at each send.
    sent_at: list[tuple[str, float, bool]] = []
    wire.on_send = lambda command: sent_at.append((command, time.monotonic(), ctrl._cmd_lock.locked()))
    assert ctrl.takeoff() is True

    # A command that never lets go of the channel, such as a takeoff still
    # waiting for its reply.
    assert ctrl._cmd_lock.acquire(timeout=WAIT_S)
    lander = threading.Thread(target=ctrl.land, daemon=True)
    try:
        land_called = time.monotonic()
        lander.start()
        # The stop is lost, and the channel stays held. The land goes out
        # anyway, one command gap after the stop.
        assert wait_for(lambda: wire.sent.count("land") == 1)
        assert lander.is_alive() and ctrl._cmd_lock.locked()
        assert [command for command, _, _ in sent_at] == ["takeoff", "stop", "land"]
        (_, t_stop, stop_locked), (_, t_land, land_locked) = sent_at[1], sent_at[2]
        assert stop_locked and land_locked
        assert t_land - t_stop >= tello_module._TIME_BETWEEN_COMMANDS_S
        assert t_land - land_called < 1.0
        time.sleep(0.3)
        assert wire.sent == ["takeoff", "stop", "land"]
    finally:
        ctrl._cmd_lock.release()

    # Once the channel is free, the confirming land goes out and is confirmed.
    lander.join(WAIT_S)
    assert not lander.is_alive()
    assert wire.sent == ["takeoff", "stop", "land", "land"]
    assert ctrl.state.is_flying is False


# ---------------------------------------------------------------------------
# PR #93 review: takeoff spin-up, hovering after an error reply, state gaps
# ---------------------------------------------------------------------------


def _takeoff_ignoring_errors(ctrl: TelloController) -> None:
    try:
        ctrl.takeoff()
    except RuntimeError:
        pass


def _recording_confirmations(ctrl: TelloController, observe: Callable[[], object]) -> list:
    """Wrap ``ctrl._mark_land_confirmed`` so each confirmation it makes records ``observe()``."""
    confirmations: list = []
    real_mark = ctrl._mark_land_confirmed

    def recording_mark(how: str | None = None) -> bool:
        confirmed = real_mark(how)
        if confirmed:
            confirmations.append(observe())
        return confirmed

    ctrl._mark_land_confirmed = recording_mark
    return confirmations


class SpinUpTello:
    """Scripted Tello for ``FakeTelloWire.script`` whose takeoff spins up on the ground before it lifts.

    ``takeoff`` leaves the state height at 0 for ``spin_s`` (the motors
    spinning up), then lifts the drone to 80 cm, and is answered ``ok``
    ``reply_s`` after it was sent. With ``spin_s`` None the drone lifts
    only when the test calls ``lift()``. ``stop`` is answered ``ok`` at
    once. A ``land`` that arrives while the drone is on the ground or still
    taking off is answered ``error`` and ignored, as a Tello does; one that
    reaches a hovering drone starts a descent of ``descent_s`` and is
    answered ``ok`` at touchdown.
    """

    def __init__(self, wire, spin_s: float | None = 1.0, reply_s: float = 2.5, descent_s: float = 0.5):
        self.wire = wire
        self.spin_s = spin_s
        self.reply_s = reply_s
        self.descent_s = descent_s
        self.phase = "ground"
        self.lifted_at: float | None = None
        self.touched_down_at: float | None = None
        self._lock = threading.Lock()

    def lift(self) -> None:
        with self._lock:
            if self.phase == "takeoff":
                self.phase = "hover"
                self.wire.height_cm = 80
                self.lifted_at = time.monotonic()

    def _touch_down(self) -> None:
        with self._lock:
            self.phase = "ground"
            self.wire.height_cm = 0
            self.touched_down_at = time.monotonic()

    def __call__(self, command: str):
        word = command.split()[0]
        with self._lock:
            if word == "takeoff":
                self.phase = "takeoff"
                if self.spin_s is not None:
                    timer = threading.Timer(self.spin_s, self.lift)
                    timer.daemon = True
                    timer.start()
                return [("ok", self.reply_s)]
            if word == "stop":
                return [("ok", 0.01)]
            if word == "land":
                if self.phase != "hover":
                    return [("error", 0.02)]
                self.phase = "landing"
                self.wire.height_cm = 40
                timer = threading.Timer(self.descent_s, self._touch_down)
                timer.daemon = True
                timer.start()
                return [("ok", self.descent_s)]
        return None


def test_land_during_takeoff_spin_up_waits_for_touchdown(tello_on_wire):
    """R1: LAND while a takeoff spins up is not confirmed by height-0 readings; the drone lifts and is landed."""
    ctrl, wire = tello_on_wire()
    wire.start_state(0)
    tello = SpinUpTello(wire, spin_s=1.0, reply_s=2.5, descent_s=0.5)
    wire.script = tello
    # (drone lifted, drone touched down) at each land confirmation.
    confirmations = _recording_confirmations(
        ctrl, lambda: (tello.lifted_at is not None, tello.touched_down_at is not None)
    )
    # Whether the drone had lifted, each time the land flag was seen
    # cleared after the land started but before touchdown.
    early_clears: list[bool] = []
    sampling = threading.Event()

    def sample() -> None:
        armed = False
        while not sampling.is_set():
            lifted = tello.lifted_at is not None
            if ctrl._land_unconfirmed:
                armed = True
            elif armed and tello.touched_down_at is None:
                early_clears.append(lifted)
            time.sleep(0.005)

    taker = threading.Thread(target=lambda: _takeoff_ignoring_errors(ctrl), daemon=True)
    taker.start()
    assert wait_for(lambda: "takeoff" in wire.sent)
    time.sleep(0.1)
    # LAND 0.1 s into the takeoff. The stop's "ok" ends the takeoff's wait
    # for its reply, and every land is answered "error" until the drone has
    # lifted.
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        ctrl.land()
    finally:
        sampling.set()
        sampler.join(WAIT_S)
    taker.join(WAIT_S)

    # The drone lifted while the land was running, and the land was
    # confirmed only once it was back on the ground.
    assert tello.lifted_at is not None
    assert tello.touched_down_at is not None
    assert confirmations and all(lifted and down for lifted, down in confirmations)
    assert early_clears == []
    assert ctrl._land_unconfirmed is False
    assert ctrl.state.is_flying is False
    assert flight_commands(wire.sent) == ["takeoff"]
    assert wire.sent.count("land") >= 3


def test_land_starts_again_when_state_shows_liftoff_after_confirmation(tello_on_wire, monkeypatch, capsys):
    """R1: a takeoff that spins up longer than the ground-ignore window, then lifts after its land was confirmed."""
    monkeypatch.setattr(tello_module, "_TAKEOFF_GROUND_IGNORE_S", 0.3)
    ctrl, wire = tello_on_wire()
    wire.start_state(0)
    tello = SpinUpTello(wire, spin_s=None, reply_s=30.0, descent_s=0.5)
    wire.script = tello
    taker = threading.Thread(target=lambda: _takeoff_ignoring_errors(ctrl), daemon=True)
    taker.start()
    assert wait_for(lambda: "takeoff" in wire.sent)
    time.sleep(0.1)

    # Past the shortened window, three readings at height 0 confirm the land.
    ctrl.land()
    taker.join(WAIT_S)
    assert ctrl._land_unconfirmed is False
    assert ctrl.state.is_flying is False
    assert ctrl._keepalive_once() is True
    lands_before = wire.sent.count("land")

    # The drone lifts anyway. Fresh state above the ground marks it flying
    # again, holds off the keepalive, and lands it.
    rearmed: list[tuple[bool, bool, bool]] = []
    real_land = ctrl.land

    def observed_land() -> None:
        rearmed.append((ctrl.state.is_flying, ctrl._land_unconfirmed, ctrl._landing))
        real_land()

    ctrl.land = observed_land
    tello.lift()
    assert wait_for(lambda: tello.touched_down_at is not None)
    assert wait_for(lambda: ctrl._land_unconfirmed is False and not ctrl._landing_again)
    assert rearmed and rearmed[0] == (True, True, True)
    assert wire.sent.count("land") > lands_before
    assert ctrl.state.is_flying is False
    assert flight_commands(wire.sent) == ["takeoff"]
    assert "landing again" in capsys.readouterr().out
    assert ctrl._keepalive_once() is True


def test_takeoff_after_an_unconfirmed_land_is_not_landed_again(tello_on_wire, monkeypatch):
    """A takeoff sent after a land that was never confirmed is not taken for a drone lifting under that land."""
    monkeypatch.setattr(tello_module, "_LAND_REPLY_TIMEOUT_S", 0.2)
    attempts = tello_module._LAND_ATTEMPTS
    # LAND on a drone on the ground that sends no state yet, with every land
    # lost: the land stays unconfirmed and the drone is not marked flying.
    ctrl, wire = tello_on_wire(drop={"land"}, delays={"takeoff": 1.0})
    with pytest.raises(RuntimeError, match="land not confirmed"):
        ctrl.land()
    assert ctrl._land_unconfirmed is True
    assert ctrl.state.is_flying is False

    # The operator takes off; the drone climbs before the Tello answers.
    wire.drop.clear()
    wire.start_state(0)
    wire.on_send = lambda command: setattr(wire, "height_cm", 80) if command == "takeoff" else None
    assert ctrl.takeoff() is True
    time.sleep(0.3)
    assert wire.sent == ["land"] * attempts + ["takeoff"]
    assert ctrl.state.is_flying is True
    assert ctrl._land_unconfirmed is False
    assert ctrl._landing_again is False


class BusyTello:
    """Scripted Tello for ``FakeTelloWire.script``: busy with a move, it answers ``land`` with ``error``.

    Every ``stop`` is lost. A move takes ``move_s`` and is answered ``ok``
    when done; a ``land`` that arrives meanwhile is answered ``error`` and
    ignored. A ``land`` that reaches a hovering drone starts a descent of
    ``descent_s`` and is answered ``ok`` at touchdown; one that arrives
    while it is landing or on the ground is answered ``error``.
    """

    def __init__(self, wire, move_s: float = 1.0, descent_s: float = 1.0):
        self.wire = wire
        self.move_s = move_s
        self.descent_s = descent_s
        self.phase = "hover"
        self.touched_down_at: float | None = None
        self._lock = threading.Lock()

    def _move_done(self) -> None:
        with self._lock:
            if self.phase != "moving":
                return
            self.phase = "hover"
        self.wire._reply("ok")

    def _touch_down(self) -> None:
        with self._lock:
            self.phase = "ground"
            self.wire.height_cm = 0
            self.touched_down_at = time.monotonic()

    def __call__(self, command: str):
        word = command.split()[0]
        with self._lock:
            if word in ("forward", "back", "left", "right"):
                self.phase = "moving"
                timer = threading.Timer(self.move_s, self._move_done)
                timer.daemon = True
                timer.start()
                return []
            if word == "stop":
                return []
            if word == "land":
                if self.phase != "hover":
                    return [("error", 0.02)]
                self.phase = "landing"
                self.wire.height_cm = 40
                timer = threading.Timer(self.descent_s, self._touch_down)
                timer.daemon = True
                timer.start()
                return [("ok", self.descent_s)]
        return None


def test_land_rejected_mid_move_is_sent_again_once_state_shows_no_descent(tello_on_wire):
    """R2: after an 'error' reply, a land is sent again within about 1 s while fresh state shows the drone hovering."""
    ctrl, wire = _flying_on_state(tello_on_wire)
    tello = BusyTello(wire, move_s=1.0, descent_s=1.0)
    wire.script = tello
    sent_at: list[tuple[str, float]] = []
    wire.on_send = lambda command: sent_at.append((command, time.monotonic()))

    mover = threading.Thread(target=lambda: _move_ignoring_errors(ctrl, "forward", 300), daemon=True)
    mover.start()
    assert wait_for(lambda: "forward 300" in wire.sent)
    time.sleep(0.2)
    land_called = time.monotonic()
    # The stop is lost; the immediate land and the first confirming land
    # arrive mid-move and are answered "error". The move ends 0.8 s after
    # LAND and the drone hovers at 80 cm.
    ctrl.land()
    mover.join(WAIT_S)
    assert not mover.is_alive()

    lands = [t for command, t in sent_at if command == "land"]
    assert len(lands) == 3
    # The land is sent again about _LAND_STALL_S after the first confirming
    # land, not after _LAND_GROUND_WAIT_S.
    assert lands[2] - lands[1] <= 1.5
    # No slower than the 3.01 s that LAND took here before state confirmed lands.
    assert tello.touched_down_at is not None
    assert tello.touched_down_at - land_called <= 3.01
    assert ctrl._land_unconfirmed is False
    assert ctrl.state.is_flying is False


def test_land_sent_again_when_descent_stops_above_ground(tello_on_wire):
    """After an 'error' reply, a drone that sank a little and then hovers is not waited on for the whole wait."""
    ctrl, wire = _flying_on_state(tello_on_wire)
    lander = LandingTello(wire, descent_s=0.3)
    lands_at: list[float] = []

    def sinking(command: str):
        if command != "land":
            return None
        lands_at.append(time.monotonic())
        if len(lands_at) == 1:
            # The land is refused, and the drone settles 30 cm lower, at the
            # end of some descent of its own, then hovers there.
            wire.height_cm = 50
            return [("error", 0.02)]
        return lander(command)

    wire.script = sinking
    ctrl.land()
    assert len(lands_at) == 2
    gap = lands_at[1] - lands_at[0]
    # Sent again once the height stopped falling for _LAND_DESCENT_STALL_S,
    # well before _LAND_GROUND_WAIT_S.
    assert tello_module._LAND_DESCENT_STALL_S - 0.1 <= gap <= tello_module._LAND_DESCENT_STALL_S + 1.5
    assert gap < tello_module._LAND_GROUND_WAIT_S
    assert lander.touched_down_at is not None
    assert ctrl._land_unconfirmed is False


class SteppedLandingTello(LandingTello):
    """Like ``LandingTello``, but the descent lowers the state height 10 cm every ``step_s``."""

    def __init__(self, wire, step_s: float = 0.25, late_land_reply: str | None = "error"):
        super().__init__(wire, descent_s=0.0, late_land_reply=late_land_reply)
        self.step_s = step_s

    def _descend(self) -> None:
        while self.wire.height_cm > 0:
            time.sleep(self.step_s)
            self.wire.height_cm = max(0, self.wire.height_cm - 10)
        self._touch_down()

    def __call__(self, command: str):
        if command.split()[0] != "land" or self.landing or self.wire.height_cm == 0:
            return super().__call__(command)
        self.landing = True
        threading.Thread(target=self._descend, daemon=True).start()
        return []


def test_steady_descent_after_error_reply_is_waited_out(tello_on_wire):
    """A land answered 'error' while state shows a steady descent is not sent again."""
    ctrl, wire = _flying_on_state(tello_on_wire)
    tello = SteppedLandingTello(wire, step_s=0.25)
    wire.script = tello

    mover = threading.Thread(target=lambda: _move_ignoring_errors(ctrl, "forward", 200), daemon=True)
    mover.start()
    assert wait_for(lambda: "forward 200" in wire.sent)
    # The immediate land starts a 2 s descent; the confirming land is
    # answered "error" because the drone is already landing.
    ctrl.land()
    # Confirmed once the drone reached ground height, and not before.
    assert wire.height_cm <= tello_module._GROUND_HEIGHT_CM
    mover.join(WAIT_S)
    assert wire.sent == ["takeoff", "forward 200", "stop", "land", "land"]
    assert ctrl._land_unconfirmed is False
    assert wait_for(lambda: tello.touched_down_at is not None)


def test_descent_begun_by_the_immediate_land_counts_as_falling(tello_on_wire):
    """A confirming land sent mid-descent compares the height with the one when LAND was pressed."""
    ctrl, wire = _flying_on_state(tello_on_wire)
    # The immediate land drops the drone to 40 cm, where it stays for 1.5 s
    # until touchdown; the confirming land, sent at 40 cm, is answered
    # "error" because the drone is already landing.
    tello = LandingTello(wire, descent_s=1.5)
    wire.script = tello

    mover = threading.Thread(target=lambda: _move_ignoring_errors(ctrl, "forward", 200), daemon=True)
    mover.start()
    assert wait_for(lambda: "forward 200" in wire.sent)
    ctrl.land()
    mover.join(WAIT_S)
    # 40 cm is lower than the 80 cm when the land started, so the land kept
    # waiting instead of being sent again.
    assert tello.touched_down_at is not None
    assert wire.sent == ["takeoff", "forward 200", "stop", "land", "land"]
    assert ctrl._land_unconfirmed is False


def test_state_stream_lost_during_a_land_hands_over_to_the_reply(tello_on_wire, monkeypatch, capsys):
    """Once state has been silent for _STATE_SEEN_S, the wait ends and the next land's own reply decides."""
    monkeypatch.setattr(tello_module, "_STATE_SEEN_S", 1.0)
    ctrl, wire = _flying_on_state(tello_on_wire)

    def state_lost(command: str):
        if command == "land":
            # The state stream dies as the land goes out (stopped from
            # another thread, since the land is sent under _wire_lock); the
            # Tello answers every land "ok".
            threading.Thread(target=wire.stop_state, daemon=True).start()
            return [("ok", 0.02)]
        return None

    wire.script = state_lost
    started = time.monotonic()
    ctrl.land()
    elapsed = time.monotonic() - started

    assert wire.sent == ["takeoff", "land", "land"]
    assert "no state height for 1 s" in capsys.readouterr().out
    # Well before _LAND_GROUND_WAIT_S.
    assert elapsed < tello_module._LAND_GROUND_WAIT_S - 2.0
    assert ctrl._land_unconfirmed is False
    assert ctrl.state.is_flying is False


def test_short_state_gap_does_not_hand_confirmation_to_a_stale_ok(tello_on_wire, monkeypatch, capsys):
    """N2: with state seen within _STATE_SEEN_S, a gap in the stream is waited out, not settled by a late 'ok'."""
    # Long enough that the stop's late "ok" is read as the land's reply even
    # on a loaded machine.
    monkeypatch.setattr(tello_module, "_LAND_REPLY_TIMEOUT_S", 1.5)
    ctrl, wire = _flying_on_state(tello_on_wire)
    move_in_flight = [False]

    def spike(command: str):
        word = command.split()[0]
        if word == "forward":
            move_in_flight[0] = True
            return []
        if word == "stop":
            # A Wi-Fi spike: the state stream goes silent for 1.2 s, and the
            # stop's own "ok" arrives 0.7 s late.
            wire.stop_state()
            restart = threading.Timer(1.2, wire.start_state)
            restart.daemon = True
            restart.start()
            replies = [("error", 0.0)] if move_in_flight[0] else []
            move_in_flight[0] = False
            return replies + [("ok", 0.7)]
        if word == "land":
            # Every land is lost; the drone hovers at 80 cm.
            return []
        return None

    wire.script = spike
    mover = threading.Thread(target=lambda: _move_ignoring_errors(ctrl, "forward", 200), daemon=True)
    mover.start()
    assert wait_for(lambda: "forward 200" in wire.sent)
    with pytest.raises(RuntimeError, match="land not confirmed"):
        ctrl.land()
    mover.join(WAIT_S)

    out = capsys.readouterr().out
    # The late "ok" was read as the first confirming land's reply while the
    # state stream was silent, and confirmed nothing.
    assert "Land attempt 1/3 not confirmed: answered 'ok'" in out
    assert ctrl.state.is_flying is True
    assert ctrl._land_unconfirmed is True
    assert ctrl._keepalive_once() is False


def test_ground_readings_from_before_the_land_confirm_nothing(tello_on_wire):
    ctrl, wire = tello_on_wire()
    for _ in range(3):
        wire.send_state()
    assert ctrl._ground_by_state() is True
    land_sent_at = time.monotonic()
    time.sleep(0.01)
    # Readings that arrived before the land was sent say nothing about it.
    assert ctrl._ground_by_state(since=land_sent_at) is False
    wire.send_state()
    wire.send_state()
    assert ctrl._ground_by_state(since=land_sent_at) is False
    wire.send_state()
    assert ctrl._ground_by_state(since=land_sent_at) is True


def test_ground_readings_with_a_gap_between_them_confirm_no_land(tello_on_wire):
    ctrl, wire = tello_on_wire()
    wire.send_state()
    time.sleep(tello_module._STATE_FRESH_S + 0.2)
    wire.send_state()
    wire.send_state()
    # Three ground readings, the newest fresh, but not one unbroken run.
    assert ctrl._ground_by_state() is False
    wire.send_state()
    assert ctrl._ground_by_state() is True


def test_one_or_two_ground_readings_confirm_nothing(tello_on_wire):
    ctrl, wire = tello_on_wire()
    wire.height_cm = 80
    for _ in range(3):
        wire.send_state()
    assert ctrl._ground_by_state() is False
    wire.height_cm = 0
    for _ in range(2):
        # A glitch of one or two readings at height 0 while hovering.
        wire.send_state()
        assert ctrl._ground_by_state() is False
    # The third reading in a row at ground height confirms.
    wire.send_state()
    assert ctrl._ground_by_state() is True


def test_state_confirms_no_land_while_a_takeoff_holds_the_channel(tello_on_wire, monkeypatch):
    """A takeoff still waiting for its reply may lift any moment, whatever state shows."""
    # A spin-up that outlasts the window, so only the channel guard holds.
    monkeypatch.setattr(tello_module, "_TAKEOFF_GROUND_IGNORE_S", 0.2)
    ctrl, wire = tello_on_wire(hold={"takeoff"}, drop={"stop", "land"})
    wire.start_state(0)
    taker = threading.Thread(target=lambda: _takeoff_ignoring_errors(ctrl), daemon=True)
    taker.start()
    assert wire.entered.wait(WAIT_S)
    assert wait_for(lambda: ctrl._ground_by_state() is True)

    lander = threading.Thread(target=ctrl.land, daemon=True)
    lander.start()
    try:
        assert wait_for(lambda: wire.sent.count("land") == 1)
        time.sleep(0.5)
        # Fresh state shows the drone on the ground, but the takeoff still
        # holds the channel: the land is not confirmed.
        assert ctrl._land_unconfirmed is True
        assert lander.is_alive() and ctrl._cmd_lock.locked()
        assert wire.sent == ["takeoff", "stop", "land"]
    finally:
        wire.release("takeoff", "error")
    lander.join(WAIT_S)
    taker.join(WAIT_S)
    assert not lander.is_alive()
    # Once the channel is free, the confirming land goes out and fresh
    # state after it confirms the land.
    assert wire.sent == ["takeoff", "stop", "land", "land"]
    assert ctrl._land_unconfirmed is False


def test_state_stamping_failure_is_logged_once(tello_on_wire, capsys):
    ctrl, wire = tello_on_wire()
    # Every state packet now fails to be recorded.
    ctrl._state_readings = None
    for _ in range(5):
        wire.send_state()
    out = capsys.readouterr().out
    assert out.count("Recording a state packet failed") == 1
    # djitellopy still stores the packet.
    assert ctrl.tello.get_own_udp_object()["state"]["h"] == 0


# ---------------------------------------------------------------------------
# B1: a stalled uplink cannot delay the link-loss landing
# ---------------------------------------------------------------------------


class StalledUplink:
    """Edge transport whose ``send`` blocks, like ``EdgeTransportSync.send``
    (30 s timeout) when the link drops without a TCP reset."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sent: list[dict] = []

    def send(self, message: dict, timeout: float = 30.0) -> bool:
        self.entered.set()
        self.release.wait(timeout)
        self.sent.append(message)
        return False


def test_watchdog_lands_within_3_5s_when_uplink_blocks():
    log = StructuredLogger(component="test-review-fixes-edge")
    drone = FakeTelloController(name="drone-1", battery=90)
    drone.connect()
    land_times: list[float] = []
    real_land = drone.land

    def timed_land() -> None:
        land_times.append(time.monotonic())
        real_land()

    drone.land = timed_land
    uplink = StalledUplink()
    executor = LaneExecutor(
        drone,
        SafetyEnvelope(alt_slot_cm=100, link_loss_land_s=3.0),
        "drone-1",
        send=_make_sender(uplink, log),
        logger=log,
    )
    try:
        drone.takeoff()
        executor.start()
        executor.note_ground_message()
        last_ground_message = time.monotonic()

        # The first telemetry message is stuck in the uplink...
        assert uplink.entered.wait(WAIT_S)
        # ...but the monitor keeps checking and lands on link loss in time.
        assert wait_for(lambda: land_times, timeout=WAIT_S)
        landed_after = land_times[0] - last_ground_message
        assert 2.9 <= landed_after <= 3.5, landed_after
        assert drone.state.is_flying is False

        # The queue is bounded and drops the oldest telemetry, so the newest
        # (showing the landing) is what goes out once the uplink recovers.
        assert len(executor._telemetry_queue) <= executor._telemetry_queue.maxlen
        assert wait_for(lambda: executor._telemetry_dropped > 0)
        assert wait_for(
            lambda: executor._telemetry_queue and executor._telemetry_queue[-1]["is_flying"] is False
        )
        uplink.release.set()
        assert wait_for(
            lambda: any(m["type"] == "telemetry" and m["is_flying"] is False for m in uplink.sent)
        )
    finally:
        uplink.release.set()
        executor.shutdown(timeout=2.0)


def test_tick_without_sender_thread_sends_inline():
    """Before start() there is no sender thread, so tick() sends telemetry itself."""
    sent: list[dict] = []
    drone = FakeTelloController(name="drone-1", battery=90)
    executor = LaneExecutor(
        drone, SafetyEnvelope(alt_slot_cm=100), "drone-1", send=sent.append,
        logger=StructuredLogger(component="test-review-fixes-edge"),
    )
    executor.tick()
    assert [m["type"] for m in sent] == ["telemetry"]


def test_no_inline_telemetry_send_after_stop():
    """R-114 (PR #87 re-review R6): once the executor is stopping, telemetry is dropped, never sent inline."""
    caller = threading.current_thread()
    delivered: list[dict] = []
    inline: list[dict] = []
    release = threading.Event()

    def send(msg: dict) -> None:
        if threading.current_thread() is caller:
            # The uplink has stalled: an inline send would block the caller.
            inline.append(msg)
            release.wait(WAIT_S)
        else:
            delivered.append(msg)

    drone = FakeTelloController(name="drone-1", battery=90)
    drone.connect()
    executor = LaneExecutor(
        drone, SafetyEnvelope(alt_slot_cm=100), "drone-1", send=send,
        logger=StructuredLogger(component="test-review-fixes-edge"),
    )
    try:
        executor.start()
        executor.note_ground_message()
        assert wait_for(lambda: delivered)  # the sender thread delivers telemetry
        executor.shutdown(timeout=2.0)

        # A monitor tick that runs once the executor is stopping, after the
        # sender thread has gone, drops its telemetry.
        executor._next_telemetry_s = None
        started = time.monotonic()
        executor.tick()
        assert time.monotonic() - started < 1.0
        assert inline == []
    finally:
        release.set()


# ---------------------------------------------------------------------------
# B4: appearance-only by default on a flying edge
# ---------------------------------------------------------------------------


class FakeDetector:
    """Stands in for YOLO: one person in every frame."""

    def __init__(self, *args, **kwargs):
        pass

    def detect(self, frame):
        return [{"bbox": [100, 100, 160, 260], "confidence": 0.9}]


class FakeReID:
    def __init__(self, *args, **kwargs):
        pass

    def extract_embedding(self, crop):
        return np.ones(4, np.float32)


@pytest.fixture
def fake_vision(monkeypatch):
    """Replace the vision models with fakes; return the list of FaceRecognizers built."""
    built: list[object] = []

    class RecordingFaceRecognizer:
        def __init__(self, *args, **kwargs):
            built.append(self)

        def extract_embedding(self, crop):
            return np.full(4, 0.5, np.float32)

    for name, attr, cls in (
        ("flightrisk.vision.detector", "PersonDetector", FakeDetector),
        ("flightrisk.vision.reid", "PersonReID", FakeReID),
        ("flightrisk.vision.face", "FaceRecognizer", RecordingFaceRecognizer),
    ):
        module = types.ModuleType(name)
        setattr(module, attr, cls)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("FLIGHTRISK_APPEARANCE_ONLY", raising=False)
    return built


def _wire_detections(runner: EdgeRunner) -> list[dict]:
    message = runner.process_frame(np.zeros((360, 480, 3), np.uint8))
    return runner.to_dict(message)["detections"]


def test_flight_edge_appearance_only_by_default_never_builds_face(fake_vision, monkeypatch):
    parser = _build_parser()
    log = StructuredLogger(component="test-review-fixes-edge")
    for source in ("tello", "fake"):
        args = parser.parse_args(["--source", source, "--drone-id", "drone-2"])
        runner = _build_runner(args, log)
        assert fake_vision == []
        assert runner.appearance_only is True and runner._face is None
        detections = _wire_detections(runner)
        assert len(detections) == 1
        assert "face_embedding" not in detections[0]
        # Person crops and appearance (ReID) embeddings still go to the ground.
        assert detections[0]["crop"]
        assert detections[0]["reid_embedding"] == [1.0, 1.0, 1.0, 1.0]

    # A face model handed to an appearance-only runner is ignored.
    runner = EdgeRunner(detector=FakeDetector(), face=object(), appearance_only=True)
    assert runner._face is None

    # An explicit FLIGHTRISK_APPEARANCE_ONLY=1 also covers camera and file.
    monkeypatch.setenv("FLIGHTRISK_APPEARANCE_ONLY", "1")
    for argv in (["--source", "camera"], ["--source", "file", "--video", "clip.mp4"]):
        runner = _build_runner(parser.parse_args(argv), log)
        assert fake_vision == []
        assert "face_embedding" not in _wire_detections(runner)[0]


def test_with_face_flag_enables_face(fake_vision, monkeypatch):
    parser = _build_parser()
    log = StructuredLogger(component="test-review-fixes-edge")

    runner = _build_runner(parser.parse_args(["--source", "tello", "--with-face"]), log)
    assert len(fake_vision) == 1
    assert runner.appearance_only is False
    assert _wire_detections(runner)[0]["face_embedding"] == [0.5, 0.5, 0.5, 0.5]

    # The flag wins over the environment.
    monkeypatch.setenv("FLIGHTRISK_APPEARANCE_ONLY", "1")
    _build_runner(parser.parse_args(["--source", "fake", "--with-face"]), log)
    assert len(fake_vision) == 2

    # FLIGHTRISK_APPEARANCE_ONLY=0 enables it without the flag.
    monkeypatch.setenv("FLIGHTRISK_APPEARANCE_ONLY", "0")
    runner = _build_runner(parser.parse_args(["--source", "fake"]), log)
    assert len(fake_vision) == 3
    assert "face_embedding" in _wire_detections(runner)[0]

    # The camera source keeps face recognition by default, as before.
    monkeypatch.delenv("FLIGHTRISK_APPEARANCE_ONLY")
    _build_runner(parser.parse_args(["--source", "camera"]), log)
    assert len(fake_vision) == 4

    # --appearance-only and --with-face cannot be combined, and a value of
    # FLIGHTRISK_APPEARANCE_ONLY that is not a boolean is refused.
    with pytest.raises(SystemExit):
        parser.parse_args(["--source", "fake", "--with-face", "--appearance-only"])
    monkeypatch.setenv("FLIGHTRISK_APPEARANCE_ONLY", "maybe")
    with pytest.raises(ValueError):
        _build_runner(parser.parse_args(["--source", "fake"]), log)


# ---------------------------------------------------------------------------
# B3: a flying edge tags detections with drone id and pose
# ---------------------------------------------------------------------------


class FakeSocketIO:
    def __init__(self):
        self._lock = threading.Lock()
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload=None, *args, **kwargs):
        with self._lock:
            self.events.append((event, payload))

    def named(self, name: str) -> list[dict]:
        with self._lock:
            return [payload for event, payload in self.events if event == name]


def test_flight_edge_tags_detections_with_drone_and_pose(fake_vision, monkeypatch):
    token = secrets.token_hex(16)
    monkeypatch.setenv("FLIGHTRISK_EDGE_TOKEN", token)

    # Hold on to the envelope the edge builds, and give it a pose that is
    # not the origin, so the test proves the pose comes from it.
    envelope = SafetyEnvelope(alt_slot_cm=100)
    envelope.record_move("forward", 120)
    envelope.record_move("right", 40)
    with envelope._lock:
        envelope.pose.apply_rotation(90)
    monkeypatch.setattr(
        "flightrisk.edge_device.SafetyEnvelope.from_env", lambda alt_slot_cm, **kwargs: envelope
    )

    received: list = []
    ground = GroundTransportSync(host="127.0.0.1", port=0, token=token, on_message=received.append)
    ground.start()
    stop = threading.Event()
    edge: threading.Thread | None = None
    try:
        args = _build_parser().parse_args([
            "--source", "fake", "--drone-id", "drone-1", "--ws-url", f"ws://127.0.0.1:{ground.port}",
        ])
        edge = threading.Thread(
            target=_run_flight_edge,
            args=(args, StructuredLogger(component="test-review-fixes-edge"), stop),
            daemon=True,
        )
        edge.start()
        assert wait_for(lambda: any(m.detections for m in list(received)), timeout=20.0)
        message = next(m for m in list(received) if m.detections)

        assert message.drone_id == "drone-1"
        assert (message.rel_x_cm, message.rel_y_cm, message.heading_deg) == (40.0, 120.0, 90.0)
        assert message.detections[0].crop_jpeg
        assert message.detections[0].face_embedding is None
        assert fake_vision == []  # appearance-only by default

        # The dashboard's fleet path accepts it and places it on the map.
        socketio = FakeSocketIO()
        supervisor = FleetSupervisor(ground, ["drone-1"], socketio)
        supervisor.handle_detections(message)
        placed = socketio.named("fleet_detection")
        assert len(placed) == 1
        assert placed[0]["drone_id"] == "drone-1"
    finally:
        stop.set()
        if edge is not None:
            edge.join(20.0)
        ground.stop()
    assert edge is not None and not edge.is_alive()
