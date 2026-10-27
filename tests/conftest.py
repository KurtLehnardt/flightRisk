from __future__ import annotations

import itertools
import threading
from typing import Callable

import numpy as np
import pytest


@pytest.fixture
def sample_frame():
    return np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)


@pytest.fixture
def sample_crop():
    return np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)


@pytest.fixture
def solid_blue_frame():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :, 0] = 255  # Blue channel
    return frame


@pytest.fixture
def tiny_crop():
    return np.random.randint(0, 255, (1, 1, 3), dtype=np.uint8)


@pytest.fixture
def large_crop():
    return np.random.randint(0, 255, (2000, 2000, 3), dtype=np.uint8)


@pytest.fixture(autouse=False)
def clean_app_state():
    """Reset flightrisk.dashboard.app module-level state for test isolation.

    Request this fixture explicitly in any test that reads or mutates
    ``app_state`` or ``_alerted_tracks`` -- it snapshots both before the
    test body runs and restores them afterward so tests can't leak
    drone/session/target state into each other.
    """
    import copy
    import dataclasses
    from flightrisk.dashboard import app as app_module
    from flightrisk.dashboard.state import app_state, AppState

    # Snapshot the current AppState fields (shallow copy is fine for
    # the scalar / None fields that tests typically touch).
    original_fields = {f.name: copy.copy(getattr(app_state, f.name)) for f in dataclasses.fields(AppState)}
    original_alerted = app_module._alerted_tracks.copy()
    yield
    # Restore each field on the singleton
    for name, value in original_fields.items():
        setattr(app_state, name, value)
    app_module._alerted_tracks.clear()
    app_module._alerted_tracks.update(original_alerted)


@pytest.fixture(autouse=True)
def _isolate_captures_dir(tmp_path, monkeypatch):
    """Point the alerts captures directory at a per-test temp dir.

    No test may write into the real ``captures/`` directory.
    """
    monkeypatch.setattr("flightrisk.dashboard.alerts.CAPTURES_DIR", tmp_path / "captures")


# ---------------------------------------------------------------------------
# A fake Tello command socket under the real djitellopy client
# ---------------------------------------------------------------------------

# Tello SDK words that make the drone fly.
TELLO_FLIGHT_WORDS = frozenset({"takeoff", "up", "down", "left", "right", "forward", "back", "cw", "ccw", "rc"})

_tello_hosts = (f"192.0.2.{n}" for n in itertools.count(10))


class FakeTelloWire:
    """Replaces djitellopy's UDP command socket and answers like a Tello.

    ``sendto`` records every command in ``sent``. A command listed in
    ``hold`` gets no reply until the test calls ``release(command)``, or,
    for a flight command, until a ``stop`` arrives: a Tello answers a move
    that ``stop`` interrupted with ``error``. A command whose first word is
    in ``drop`` is never answered, like a packet lost on Wi-Fi. A command
    whose first word is in ``delays`` is answered ``ok`` after that many
    seconds, unless a ``stop`` interrupts it first. Every other command is
    answered ``ok`` at once, except ``rc`` commands, which a Tello never
    answers. Replies go into djitellopy's per-drone response list, as its
    UDP receiver thread does.
    """

    def __init__(
        self,
        host: str,
        hold: set[str] | None = None,
        drop: set[str] | None = None,
        delays: dict[str, float] | None = None,
    ):
        self.host = host
        self.hold = set(hold or ())
        self.drop = set(drop or ())
        self.delays = dict(delays or {})
        self.sent: list[str] = []
        self.held: list[str] = []
        self.entered = threading.Event()
        self.on_send: Callable[[str], None] | None = None
        self._lock = threading.Lock()
        # Delayed replies not yet given, as (command, timer).
        self._pending: list[tuple[str, threading.Timer]] = []

    def _reply(self, text: str) -> None:
        import djitellopy.tello as djitello

        responses = djitello.drones.get(self.host)
        if responses is not None:
            responses["responses"].append(text.encode("utf-8"))

    def _reply_later(self, command: str, delay_s: float) -> None:
        def answer() -> None:
            with self._lock:
                if entry not in self._pending:
                    return
                self._pending.remove(entry)
            self._reply("ok")

        timer = threading.Timer(delay_s, answer)
        timer.daemon = True
        entry = (command, timer)
        with self._lock:
            self._pending.append(entry)
        timer.start()

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
        if word in self.drop:
            return len(data)
        if word in self.delays:
            self._reply_later(command, self.delays[word])
            return len(data)
        if word == "stop":
            interrupted = [c for c in self.held if c.split()[0] in TELLO_FLIGHT_WORDS]
            for held in interrupted:
                self.held.remove(held)
                self._reply("error")
            with self._lock:
                stopped = [p for p in self._pending if p[0].split()[0] in TELLO_FLIGHT_WORDS]
                for entry in stopped:
                    self._pending.remove(entry)
                    entry[1].cancel()
            for _ in stopped:
                self._reply("error")
        if word != "rc":
            self._reply("ok")
        return len(data)

    def release(self, command: str, reply: str = "ok") -> None:
        self.held.remove(command)
        self._reply(reply)

    def cancel_pending(self) -> None:
        """Drop every delayed reply not yet given."""
        with self._lock:
            pending, self._pending = self._pending, []
        for _, timer in pending:
            timer.cancel()


@pytest.fixture
def tello_on_wire(monkeypatch):
    """Factory for a ``TelloController`` whose djitellopy client talks to a ``FakeTelloWire``."""
    import djitellopy.tello as djitello

    from flightrisk.drone.tello import TelloController

    made: list[tuple[TelloController, FakeTelloWire]] = []

    def _make(
        hold: set[str] | None = None,
        drop: set[str] | None = None,
        delays: dict[str, float] | None = None,
    ) -> tuple[TelloController, FakeTelloWire]:
        host = next(_tello_hosts)
        wire = FakeTelloWire(host, hold, drop, delays)
        # threads_initialized stops djitellopy from binding its real UDP
        # sockets; client_socket is the socket every command is sent on.
        monkeypatch.setattr(djitello, "threads_initialized", True)
        monkeypatch.setattr(djitello, "client_socket", wire, raising=False)
        ctrl = TelloController(name="drone-1", host=host)
        made.append((ctrl, wire))
        return ctrl, wire

    yield _make
    for ctrl, wire in made:
        ctrl._running = False
        wire.cancel_pending()
        # Keep djitellopy's end() (run when the client is garbage collected)
        # from sending anything after the fake socket is gone.
        ctrl.tello.is_flying = False
        ctrl.tello.stream_on = False


@pytest.fixture(autouse=True)
def _reset_match_alert_latch():
    """Give every test a fresh single-alert latch and recovery runner.

    The latch is a process-wide singleton: a test that raises an alert leaves
    it ``AWAITING_RESPONSE``, which would (correctly) suppress alerts in the
    next test. Resetting before and after keeps tests independent.
    """
    from flightrisk.dashboard.recovery import reset_alert_flow

    reset_alert_flow()
    yield
    reset_alert_flow()
