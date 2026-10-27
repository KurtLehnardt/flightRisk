"""Edge safety and tagging fixes from the PR #87 review (R-110).

* B1: the lane executor's monitor never waits on the ground link, so a
  stalled uplink cannot delay the link-loss landing.
* B2: Tello flight moves are sent once and never retried, and no move reaches
  the wire after ``land()`` starts.
* N2: the Tello keepalive holds the command lock for its whole exchange.
* R-113: Tello video-stream recovery (``streamoff`` and ``streamon``) holds the
  command lock for each exchange, and a land always takes the channel first.
* N7: ``takeoff()`` refuses while a land is in progress.
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

    # An explicit FLIGHTRISK_APPEARANCE_ONLY=1 also covers webcam and file.
    monkeypatch.setenv("FLIGHTRISK_APPEARANCE_ONLY", "1")
    for argv in (["--source", "webcam"], ["--source", "file", "--video", "clip.mp4"]):
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

    # Webcam keeps face recognition by default, as before.
    monkeypatch.delenv("FLIGHTRISK_APPEARANCE_ONLY")
    _build_runner(parser.parse_args(["--source", "webcam"]), log)
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
