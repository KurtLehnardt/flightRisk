from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable

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
    from flightrisk.dashboard.state import AppState, app_state

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


@pytest.fixture(autouse=True)
def _isolate_ceiling_settings(tmp_path, monkeypatch):
    """Keep the dashboard's ceiling settings in a per-test file and the edge ceiling at its defaults.

    No test may read or write the operator's ``~/.flightrisk/ceiling.json``.
    """
    monkeypatch.setenv("FLIGHTRISK_CEILING_FILE", str(tmp_path / "ceiling.json"))
    monkeypatch.delenv("FLIGHTRISK_CEILING_CM", raising=False)
    monkeypatch.delenv("FLIGHTRISK_CEILING_MARGIN_CM", raising=False)


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

    ``script``, when set, is called with each command after it is recorded.
    If it returns a list of ``(reply, delay_s)`` pairs, the wire gives
    exactly those replies (each after its delay) instead of its own; if it
    returns None, the wire answers as above.

    ``start_state(height_cm)`` makes the wire send Tello state packets, about
    20 a second, each carrying the current ``height_cm`` as ``h``. They are
    stored in djitellopy's per-drone ``"state"`` entry the way its state
    receiver thread stores them. Change ``height_cm`` to move the drone, and
    call ``stop_state()`` to make the state stream go silent. Each packet
    also carries ``tof_cm`` as ``tof`` (10, a Tello on the ground, unless
    changed) and the motor time as ``time``: whole seconds, starting at 5,
    that go up in real time while ``motors_on`` is true (call
    ``set_motors(on)`` to switch). A field named in ``omit_state_fields`` is
    left out of every packet, and a field named in ``state_overrides`` is
    sent with that text instead of its usual value.
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
        self.script: Callable[[str], list[tuple[str, float]] | None] | None = None
        self.height_cm = 0
        self.tof_cm = 10
        self.omit_state_fields: set[str] = set()
        self.state_overrides: dict[str, str] = {}
        self._motors_on = False
        self._motor_time_s = 5.0
        self._motors_since = 0.0
        self._lock = threading.Lock()
        # Delayed replies not yet given, as (command, timer).
        self._pending: list[tuple[str, threading.Timer]] = []
        self._state_stop: threading.Event | None = None
        self._state_thread: threading.Thread | None = None

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
        if self.script is not None:
            scripted = self.script(command)
            if scripted is not None:
                for text, delay_s in scripted:
                    self._reply_after(text, delay_s)
                return len(data)
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

    def _reply_after(self, text: str, delay_s: float) -> None:
        """Give ``text`` as a reply after ``delay_s`` (at once if 0); cancelled by ``cancel_pending``."""
        if delay_s <= 0:
            self._reply(text)
            return

        def answer() -> None:
            with self._lock:
                if entry not in self._pending:
                    return
                self._pending.remove(entry)
            self._reply(text)

        timer = threading.Timer(delay_s, answer)
        timer.daemon = True
        entry = (f"script:{text}", timer)
        with self._lock:
            self._pending.append(entry)
        timer.start()

    @property
    def motors_on(self) -> bool:
        return self._motors_on

    def set_motors(self, on: bool) -> None:
        """Start or stop the motors: the packet field ``time`` goes up in real time while they run."""
        with self._lock:
            now = time.monotonic()
            if self._motors_on:
                self._motor_time_s += now - self._motors_since
            self._motors_on = on
            self._motors_since = now

    def motor_time(self) -> int:
        """The motor time a state packet sent now carries, in whole seconds."""
        with self._lock:
            elapsed = time.monotonic() - self._motors_since if self._motors_on else 0.0
            return int(self._motor_time_s + elapsed)

    def send_state(self) -> None:
        """Deliver one Tello state packet carrying ``height_cm``, as djitellopy's state receiver does."""
        import djitellopy.tello as djitello

        fields = [
            ("mid", "-1"), ("x", "0"), ("y", "0"), ("z", "0"), ("mpry", "0,0,0"),
            ("pitch", "0"), ("roll", "0"), ("yaw", "0"), ("vgx", "0"), ("vgy", "0"), ("vgz", "0"),
            ("templ", "40"), ("temph", "42"), ("tof", str(int(self.tof_cm))),
            ("h", str(int(self.height_cm))), ("bat", "90"), ("baro", "0.00"),
            ("time", str(self.motor_time())), ("agx", "0.00"), ("agy", "0.00"), ("agz", "-1000.00"),
        ]
        packet = "".join(
            f"{key}:{self.state_overrides.get(key, value)};"
            for key, value in fields
            if key not in self.omit_state_fields
        )
        udp = djitello.drones.get(self.host)
        if udp is not None:
            udp["state"] = djitello.Tello.parse_state(packet)

    def start_state(self, height_cm: int | None = None, interval_s: float = 0.05) -> None:
        """Send a state packet every ``interval_s`` until ``stop_state()``."""
        if height_cm is not None:
            self.height_cm = height_cm
        self.stop_state()
        stop = threading.Event()

        def stream() -> None:
            while True:
                self.send_state()
                if stop.wait(interval_s):
                    return

        self._state_stop = stop
        self._state_thread = threading.Thread(target=stream, daemon=True, name=f"fake-tello-state-{self.host}")
        self._state_thread.start()

    def stop_state(self) -> None:
        """Stop sending state packets, like a state stream lost on Wi-Fi."""
        if self._state_stop is not None:
            self._state_stop.set()
            if self._state_thread is not None:
                self._state_thread.join(2.0)
        self._state_stop = None
        self._state_thread = None


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
        wire.stop_state()
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


# ---------------------------------------------------------------------------
# Network simulator and two-node fleet harness (tests/support). The import
# only registers extra fixtures; every fixture above is unchanged.
# ---------------------------------------------------------------------------

from tests.support.fixtures import (  # noqa: F401
    error_watch,
    netsim,
    two_node_fleet,
)


@pytest.fixture(autouse=True)
def _launcher_never_touches_the_real_flightrisk_files(request, tmp_path, monkeypatch):
    """The launcher writes the ceiling file on Start: a launcher test must never reach the real ~/.flightrisk."""
    if request.module.__name__.rsplit(".", 1)[-1].startswith("test_launcher"):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        monkeypatch.setenv("FLIGHTRISK_CEILING_FILE", str(tmp_path / "home-ceiling.json"))
        monkeypatch.setenv("FLIGHTRISK_LAUNCHER_FILE", str(tmp_path / "home-launcher.json"))
