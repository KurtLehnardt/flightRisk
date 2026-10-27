"""Edge safety and tagging fixes from the PR #87 review (R-110).

* B1: the lane executor's monitor never waits on the ground link, so a
  stalled uplink cannot delay the link-loss landing.
* B2: Tello flight moves are sent once and never retried, and no move reaches
  the wire after ``land()`` starts.
* N2: the Tello keepalive holds the command lock for its whole exchange.
* N7: ``takeoff()`` refuses while a land is in progress.
* B3: a flying edge tags every detection with its drone id and pose.
* B4: a flying edge is appearance-only by default: no ``FaceRecognizer`` and
  no ``face_embedding`` unless ``--with-face`` or
  ``FLIGHTRISK_APPEARANCE_ONLY=0``.

Nothing here touches a real drone or the network. Tello tests run the real
``djitellopy.Tello`` client over ``FakeTelloWire``, which replaces its UDP
command socket and answers like a Tello, so djitellopy's own send and retry
code is exercised. The B3 wiring test runs a real ``GroundTransportSync`` on
127.0.0.1. Timing assertions have generous margins because many test
processes may share the machine.
"""
from __future__ import annotations

import itertools
import secrets
import sys
import threading
import time
import types
from typing import Callable

import djitellopy.tello as djitello
import numpy as np
import pytest

from flightrisk.dashboard.app import FleetSupervisor
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController, TelloController
from flightrisk.edge import EdgeRunner
from flightrisk.edge_device import _build_parser, _build_runner, _make_sender, _run_flight_edge
from flightrisk.observability import StructuredLogger
from flightrisk.transport import GroundTransportSync

WAIT_S = 10.0

# Tello SDK words that make the drone fly.
FLIGHT_WORDS = {"takeoff", "up", "down", "left", "right", "forward", "back", "cw", "ccw", "rc"}

_hosts = (f"192.0.2.{n}" for n in itertools.count(10))


def wait_for(predicate: Callable[[], object], timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# ---------------------------------------------------------------------------
# A fake Tello command socket under the real djitellopy client
# ---------------------------------------------------------------------------


class FakeTelloWire:
    """Replaces djitellopy's UDP command socket and answers like a Tello.

    ``sendto`` records every command in ``sent``. A command listed in
    ``hold`` gets no reply until the test calls ``release(command)``, or,
    for a flight command, until a ``stop`` arrives: a Tello answers a move
    that ``stop`` interrupted with ``error``. Every other command is
    answered ``ok`` at once, except ``rc`` commands, which a Tello never
    answers. Replies go into djitellopy's per-drone response list, as its
    UDP receiver thread does.
    """

    def __init__(self, host: str, hold: set[str] | None = None):
        self.host = host
        self.hold = set(hold or ())
        self.sent: list[str] = []
        self.held: list[str] = []
        self.entered = threading.Event()
        self.on_send: Callable[[str], None] | None = None
        self._lock = threading.Lock()

    def _reply(self, text: str) -> None:
        djitello.drones[self.host]["responses"].append(text.encode("utf-8"))

    def sendto(self, data: bytes, address) -> int:
        command = data.decode("utf-8")
        assert address[0] == self.host
        with self._lock:
            self.sent.append(command)
        if self.on_send is not None:
            self.on_send(command)
        word = command.split()[0]
        if command in self.hold and command not in self.held:
            self.held.append(command)
            self.entered.set()
            return len(data)
        if word == "stop":
            interrupted = [c for c in self.held if c.split()[0] in FLIGHT_WORDS]
            for held in interrupted:
                self.held.remove(held)
                self._reply("error")
        if word != "rc":
            self._reply("ok")
        return len(data)

    def release(self, command: str, reply: str = "ok") -> None:
        self.held.remove(command)
        self._reply(reply)


@pytest.fixture
def tello_on_wire(monkeypatch):
    """Factory for a ``TelloController`` whose djitellopy client talks to a ``FakeTelloWire``."""
    made: list[TelloController] = []

    def _make(hold: set[str] | None = None) -> tuple[TelloController, FakeTelloWire]:
        host = next(_hosts)
        wire = FakeTelloWire(host, hold)
        # threads_initialized stops djitellopy from binding its real UDP
        # sockets; client_socket is the socket every command is sent on.
        monkeypatch.setattr(djitello, "threads_initialized", True)
        monkeypatch.setattr(djitello, "client_socket", wire, raising=False)
        ctrl = TelloController(name="drone-1", host=host)
        made.append(ctrl)
        return ctrl, wire

    yield _make
    for ctrl in made:
        ctrl._running = False
        # Keep djitellopy's end() (run when the client is garbage collected)
        # from sending anything after the fake socket is gone.
        ctrl.tello.is_flying = False
        ctrl.tello.stream_on = False


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
