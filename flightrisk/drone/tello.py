"""Tello drone controller wrapper around DJITelloPy.

Handles connection, video streaming, flight commands, and keepalive.
Standard Tello (TLW004) — SDK 1.3, WiFi AP mode only.

``FakeTelloController`` is an in-process stand-in with the same interface. It
never touches the network, so the edge device can fly a simulated drone
(``python -m flightrisk.edge_device --source fake``) and tests can exercise
flight logic without hardware.
"""

import threading
import time
import weakref
from collections import deque
from itertools import pairwise
from typing import Callable

import djitellopy.tello as _djitello
import numpy as np
from djitellopy import Tello

from flightrisk.config import get_config
from flightrisk.drone.controller import DroneCapabilities, DroneController, DroneState

# Backward-compatible re-export so `from flightrisk.drone.tello import DroneState` still works
__all__ = [
    "TelloController",
    "FakeTelloController",
    "DroneState",
    "DroneCapabilities",
    "DroneController",
]

# How long a land waits for the command channel before it gives up on the
# confirming land. A flight command waits at most _TAKEOFF_REPLY_TIMEOUT_S
# for its single reply, and another land holds the channel for one confirming
# exchange at a time (_LAND_REPLY_TIMEOUT_S plus the command gap), so 25 s
# covers either.
_LAND_CONFIRM_WAIT_S = 25.0
# How long a flight command waits for the Tello's reply. These match
# djitellopy's RESPONSE_TIMEOUT and TAKEOFF_TIMEOUT.
_COMMAND_REPLY_TIMEOUT_S = 7.0
_TAKEOFF_REPLY_TIMEOUT_S = 20.0
# djitellopy leaves at least this long between a reply and the next command,
# because the Tello ignores commands sent too close together. A land also
# leaves at least this long after each command it sent without reading the
# reply (the interrupting ``stop`` and ``land``).
_TIME_BETWEEN_COMMANDS_S = 0.1
_REPLY_POLL_S = 0.01
# A land is sent up to this many times. Repeating a land is always safe.
_LAND_ATTEMPTS = 3
# How long each confirming land waits for a fresh reply before it is sent
# again. Without state, only an ``ok`` that arrives after the land was sent
# confirms it.
_LAND_REPLY_TIMEOUT_S = _COMMAND_REPLY_TIMEOUT_S
# The Tello sends a state packet (UDP 8890) about ten times a second. A
# packet counts as fresh for this long after it arrives.
_STATE_FRESH_S = 0.5
# Once a state height has arrived within this long, a gap in the state
# stream is waited out: the land's reply alone confirms nothing. Only when
# no state height has arrived for this long does a fresh ``ok`` to the
# confirming land confirm it on its own.
_STATE_SEEN_S = 3.0
# A state height at or below this, in cm, counts as on the ground.
_GROUND_HEIGHT_CM = 10
# This many state packets in a row, each fresh when the next arrived, the
# newest still fresh, and all received after the confirming land was sent,
# must show ground height before the state stream confirms a land.
_GROUND_READINGS = 3
# After ``takeoff`` is sent, a Tello spins its motors up on the ground for a
# moment before it lifts. State packets at ground height that arrive within
# this long of the takeoff being sent therefore confirm no land, unless state
# has shown the drone above _GROUND_HEIGHT_CM since that takeoff.
_TAKEOFF_GROUND_IGNORE_S = 3.0
# When a confirming land is answered (``ok`` or not) while fresh state still
# shows the drone in the air, the land waits, holding no lock, for fresh
# state to show it on the ground before sending land again. It waits up to
# _LAND_GROUND_WAIT_S in all, and only while the height is falling: it sends
# land again once _LAND_STALL_S has passed since the land was sent with the
# height no lower than when the land was sent, once the height has reached
# no new low for _LAND_DESCENT_STALL_S, or once no state height has arrived
# for _STATE_SEEN_S.
_LAND_GROUND_WAIT_S = 6.0
_LAND_STALL_S = 1.0
_LAND_DESCENT_STALL_S = 2.0
# When fresh state shows the drone above the ground although its land was
# confirmed, or while a land is unconfirmed and the drone is not marked
# flying, the controller marks it flying again and lands it again: up to
# this many ``land()`` calls, this long apart.
_LAND_AGAIN_CALLS = 3
_LAND_AGAIN_RETRY_S = 2.0
# The state poller reads battery, height and temperature this often.
_STATE_POLL_INTERVAL_S = 2.0
# The poller's zero-height path acts after this many polls in a row at height 0.
_ZERO_HEIGHT_POLLS = 3
# Connect-time commands (``command``, ``streamon``) do not move the drone, so
# they are tried up to this many times.
_CONNECT_ATTEMPTS = 3
# The keepalive runs every _KEEPALIVE_INTERVAL_S and waits up to
# _KEEPALIVE_TIMEOUT_S for its reply while holding the command lock. A move
# or rotate that finds the keepalive on the channel waits up to
# _KEEPALIVE_WAIT_S for it instead of being dropped.
_KEEPALIVE_INTERVAL_S = 10.0
_KEEPALIVE_TIMEOUT_S = 5
_KEEPALIVE_WAIT_S = _KEEPALIVE_TIMEOUT_S + 1.0
# Video-stream recovery sends ``streamoff``, pauses, then sends ``streamon``.
# Each one holds the command lock only while it waits out the command gap
# (at most _TIME_BETWEEN_COMMANDS_S), is sent and has its reply read, and
# waits for its reply no longer than the keepalive does, so a move that waits
# behind it (up to _KEEPALIVE_WAIT_S) is never dropped. Each waits up to
# _STREAM_CHANNEL_WAIT_S for the channel to be free of flight commands and
# landings, checking every _STREAM_CHANNEL_POLL_S, before recovery gives up
# until the stream is next found stale.
_STREAM_RESTART_PAUSE_S = 1.0
_STREAM_REPLY_TIMEOUT_S = _KEEPALIVE_TIMEOUT_S
_STREAM_CHANNEL_WAIT_S = 30.0
_STREAM_CHANNEL_POLL_S = 0.05
# Directions accepted by the Tello's move commands.
_MOVE_DIRECTIONS = ("up", "down", "left", "right", "forward", "back")


class _StateStampedUdpObject(dict):
    """djitellopy's per-drone receive buffers, reporting each state packet as it arrives.

    djitellopy's state receiver thread stores every parsed state packet with
    ``drones[host]["state"] = ...`` and records no arrival time, so a height
    read later may be seconds old. ``TelloController`` puts this dict in
    place of that entry: every store of ``"state"`` also passes the packet
    to the controller, which records when it arrived. The ``"responses"``
    list is the same object as before, so command replies are unaffected. If
    a future djitellopy stores state some other way, no arrival is recorded
    and landing falls back to reply-based confirmation.
    """

    def __init__(self, base: dict, on_state: Callable[[object], None], name: str = "drone"):
        super().__init__(base)
        # A weak reference, so this module-level entry does not keep the
        # controller alive.
        self._on_state = weakref.WeakMethod(on_state)
        self._name = name
        self._failure_logged = False

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "state":
            on_state = self._on_state()
            if on_state is not None:
                try:
                    on_state(value)
                except Exception as e:
                    # An exception must never reach djitellopy's state
                    # receiver thread, which stops for good on the first
                    # one. The first failure is logged; later ones are not,
                    # so a lasting fault does not flood the log ten times a
                    # second.
                    if not self._failure_logged:
                        self._failure_logged = True
                        print(
                            f"[{self._name}] Recording a state packet failed ({e!r}); "
                            "further failures are not logged."
                        )


class TelloController:
    """Manages a single Tello drone connection and video stream.

    Flight commands (takeoff, move, rotate) are sent exactly once and never
    retried: djitellopy's ``send_control_command`` re-sends a command after
    any reply other than ``ok``, which would re-send a move that a landing
    has just interrupted. The djitellopy client is therefore built with
    ``retry_count=1``, and flight commands go through
    ``_send_flight_command``, which raises on any reply other than ``ok``.

    Once ``land()`` starts, no move, rotate or takeoff reaches the wire until
    a new ``takeoff()``: the landing flag is set, and every flight command
    checks it, under ``_wire_lock``, in the same step that sends it.

    A land is confirmed by the drone's own state stream whenever the drone
    sends state: fresh state packets that arrive after the confirming
    ``land`` was sent must show the drone on the ground, whatever the
    command replies were (see ``_confirm_land``). Ground readings taken while
    a takeoff may still be spinning up confirm nothing (see
    ``_ground_by_state``). Only when no state height has arrived for
    ``_STATE_SEEN_S`` does a fresh ``ok`` to the confirming land confirm it
    on its own. Until a land is confirmed ``state.is_flying`` stays true, so
    callers keep retrying, and neither the keepalive nor stream recovery
    sends anything, so the Tello's own 15 s auto-land still fires if every
    land is lost. If fresh state later shows the drone above the ground
    although its land was confirmed, the land is started again (see
    ``_note_state_packet``).
    """

    def __init__(self, name: str = "drone", host: str | None = None):
        if host is None:
            host = get_config().drone.tello_default_host
        self.name = name
        self.host = host
        # retry_count=1: djitellopy sends every command once. A retried move
        # could fly again after a landing interrupted it.
        self.tello = Tello(host=host, retry_count=1)
        self.state = DroneState()
        self.capabilities = DroneCapabilities(
            has_gps=False,
            has_rtsp=False,
            min_move_cm=20,
            max_move_cm=500,
            max_altitude_m=10,
            supports_missions=False,
        )
        self._frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        self._cmd_lock = threading.Lock()
        self._keepalive_thread: threading.Thread | None = None
        self._state_thread: threading.Thread | None = None
        self._running = False
        self._frame_callbacks: list[Callable[[np.ndarray], None]] = []
        self._last_frame_time: float = 0
        self._stream_recovering = False
        self._frozen_count: int = 0
        self._last_frame_hash: int = 0
        # Held only for the instant a flight command is checked and written
        # to the socket, and while land() marks a landing as started. It
        # orders the two: a flight command is either on the wire before the
        # landing starts (and the landing interrupts it) or never sent.
        self._wire_lock = threading.Lock()
        # Set by land() and cleared by takeoff(). While it is set, move() and
        # rotate() refuse to send anything, so a lane executor cannot push a
        # move in behind a landing.
        self._landing = False
        # Number of land() calls running now, and the number ever started.
        # takeoff() refuses while a land is running or when one started
        # after the takeoff was called (for example while it waited for the
        # command lock).
        self._lands_in_progress = 0
        self._land_seq = 0
        # Set by land() and cleared, under _wire_lock, only when a land is
        # confirmed (by fresh state showing the drone on the ground, or, with
        # no state for _STATE_SEEN_S, by a fresh "ok"), when the state poller
        # finds the drone on the ground, or when a takeoff succeeds with no
        # land since. Set again when fresh state shows the drone above the
        # ground after its land was confirmed (see _land_again_if_lifted).
        # While it is set the keepalive and stream recovery send nothing:
        # any command the Tello receives restarts its 15 s auto-land timer,
        # which is the last resort when every land packet is lost.
        self._land_unconfirmed = False
        # time.monotonic() when land() last sent a command whose reply it
        # did not read first. The next land waits _TIME_BETWEEN_COMMANDS_S
        # after it, because the Tello ignores commands sent too close
        # together.
        self._land_sent_at = 0.0
        # time.monotonic() when ``takeoff`` was last sent, or None if it
        # never was. Written under _wire_lock. See _TAKEOFF_GROUND_IGNORE_S.
        self._takeoff_sent_at: float | None = None
        # True from the moment a land is confirmed until the next takeoff is
        # sent. Fresh state showing the drone above the ground while it is
        # set means the drone lifted after all, and the land starts again.
        # Guarded by _wire_lock, as is _landing_again, which is true while
        # the thread that lands the drone again is running.
        self._believed_landed = False
        self._landing_again = False
        # True while the keepalive (or, for _stream_on_channel, a
        # stream-recovery command; for _land_on_channel, a land) holds
        # _cmd_lock. Guarded by _owner_lock, which is also held while any of
        # them takes or releases _cmd_lock.
        self._owner_lock = threading.Lock()
        self._keepalive_on_channel = False
        self._stream_on_channel = False
        self._land_on_channel = False
        # (time.monotonic() on arrival, height in cm or None) of the most
        # recent state packets, appended by djitellopy's state receiver
        # thread through _StateStampedUdpObject. Guarded by _state_lock, as
        # are _height_at and _airborne_at: when the newest packet carrying a
        # height arrived, and when the newest one showing a height above
        # _GROUND_HEIGHT_CM arrived (time.monotonic(), or None).
        self._state_lock = threading.Lock()
        self._state_readings: deque[tuple[float, int | None]] = deque(maxlen=_GROUND_READINGS)
        self._height_at: float | None = None
        self._airborne_at: float | None = None
        self._zero_height_count = 0
        self._stamp_state_packets(host)

    def _stamp_state_packets(self, host: str) -> None:
        """Have djitellopy report each state packet for ``host`` to ``_note_state_packet``."""
        try:
            udp = _djitello.drones.get(host)
            if type(udp) is dict:
                _djitello.drones[host] = _StateStampedUdpObject(udp, self._note_state_packet, self.name)
        except Exception as e:
            # No arrival times: landing falls back to reply-based confirmation.
            print(f"[{self.name}] Could not record state packet arrivals ({e!r}); lands are confirmed by replies only.")

    def _note_state_packet(self, state: object) -> None:
        """Record when a state packet arrived and the height it carries.

        Runs on djitellopy's state receiver thread, so it never waits for a
        command. A height above ``_GROUND_HEIGHT_CM`` while the drone is
        believed to be on the ground starts the land again (see
        ``_land_again_if_lifted``).
        """
        height = None
        if isinstance(state, dict):
            try:
                height = int(state["h"])
            except (KeyError, TypeError, ValueError):
                height = None
        now = time.monotonic()
        airborne = height is not None and height > _GROUND_HEIGHT_CM
        with self._state_lock:
            self._state_readings.append((now, height))
            if height is not None:
                self._height_at = now
                if airborne:
                    self._airborne_at = now
        if airborne and (self._believed_landed or self._land_wanted_while_not_flying()):
            self._land_again_if_lifted(height)

    def _land_wanted_while_not_flying(self) -> bool:
        """True while a land started since the last takeoff is unconfirmed and ``is_flying`` is false."""
        return self._landing and self._land_unconfirmed and not self.state.is_flying

    def _land_again_if_lifted(self, height: int) -> None:
        """Mark the drone flying and land it again: fresh state shows it above the ground.

        Acts when a land was confirmed and no takeoff has been sent since
        (the confirmation was wrong, or the drone lifted after it), and when
        a land started since the last takeoff is still unconfirmed while
        ``is_flying`` is false (for example the takeoff it interrupted
        raised, and the drone lifted anyway). Either way ``is_flying`` and
        the land flags are set again under ``_wire_lock``, so callers retry,
        the keepalive and stream recovery stay silent, and no move is sent;
        then a background thread sends the land (``_land_until_confirmed``),
        since this runs on the state receiver thread, which must not wait
        for commands.
        """
        with self._wire_lock:
            if self._believed_landed:
                self._believed_landed = False
                self._land_unconfirmed = True
                self._landing = True
                why = "after its land was confirmed"
            elif self._land_wanted_while_not_flying():
                why = "while its land is unconfirmed and it was not marked flying"
            else:
                return
            self.state.is_flying = True
            # djitellopy's end() lands only when its own flag says flying.
            self.tello.is_flying = True
            start = not self._landing_again
            self._landing_again = True
        print(f"[{self.name}] Fresh state shows the drone at {height} cm {why}; landing again.")
        if start:
            threading.Thread(
                target=self._land_until_confirmed, daemon=True, name=f"{self.name}-land-again"
            ).start()

    def _land_until_confirmed(self) -> None:
        """Call ``land()`` until a land is confirmed, at most ``_LAND_AGAIN_CALLS`` times."""
        calls = 0
        while True:
            with self._wire_lock:
                if not self._land_unconfirmed or calls >= _LAND_AGAIN_CALLS:
                    self._landing_again = False
                    return
            if calls:
                time.sleep(_LAND_AGAIN_RETRY_S)
            calls += 1
            try:
                self.land()
            except Exception as e:
                print(f"[{self.name}] Landing again failed: {e}")

    def _fresh_height(self) -> int | None:
        """The newest state height, or None if it is older than ``_STATE_FRESH_S`` or missing."""
        with self._state_lock:
            newest = self._state_readings[-1] if self._state_readings else None
        if newest is None or newest[1] is None or time.monotonic() - newest[0] > _STATE_FRESH_S:
            return None
        return newest[1]

    def _state_silent(self) -> bool:
        """True when no state height has arrived for ``_STATE_SEEN_S`` (or ever)."""
        with self._state_lock:
            height_at = self._height_at
        return height_at is None or time.monotonic() - height_at >= _STATE_SEEN_S

    def _ground_by_state(self, since: float | None = None) -> bool | None:
        """Whether fresh state shows the drone on the ground.

        None when no state height has arrived for ``_STATE_SEEN_S`` (or
        ever): the caller may fall back to the land's reply. True when the
        last ``_GROUND_READINGS`` packets all show a height at or below
        ``_GROUND_HEIGHT_CM``, each arrived within ``_STATE_FRESH_S`` of the
        one before, the newest is still fresh, all arrived after ``since``
        (a ``time.monotonic()`` value, when given), and none arrived within
        ``_TAKEOFF_GROUND_IGNORE_S`` of the last takeoff being sent unless
        state has shown the drone above the ground since that takeoff (a
        drone spinning up for takeoff still reads height 0). False
        otherwise: fresh state shows the drone in the air or not yet on the
        ground for long enough, or a short gap in the state stream, which
        is waited out rather than handed back to the reply.
        """
        with self._state_lock:
            readings = list(self._state_readings)
            height_at = self._height_at
            airborne_at = self._airborne_at
        now = time.monotonic()
        if not readings or height_at is None or now - height_at >= _STATE_SEEN_S:
            return None
        newest_at, newest_height = readings[-1]
        if newest_height is None or now - newest_at > _STATE_FRESH_S:
            return False
        if len(readings) < _GROUND_READINGS:
            return False
        if since is not None and readings[0][0] <= since:
            return False
        for (earlier, _), (later, _) in pairwise(readings):
            if later - earlier > _STATE_FRESH_S:
                return False
        if not all(h is not None and h <= _GROUND_HEIGHT_CM for _, h in readings):
            return False
        takeoff_at = self._takeoff_sent_at
        if (
            takeoff_at is not None
            and readings[0][0] < takeoff_at + _TAKEOFF_GROUND_IGNORE_S
            and (airborne_at is None or airborne_at < takeoff_at)
        ):
            # Maybe still spinning up: the drone may lift any moment.
            return False
        return True

    def _state_height_text(self) -> str:
        """The newest state height and its age, for log messages."""
        with self._state_lock:
            newest = self._state_readings[-1] if self._state_readings else None
        if newest is None or newest[1] is None:
            return "no state height"
        return f"height {newest[1]} cm ({time.monotonic() - newest[0]:.1f} s ago)"

    def connect(self) -> bool:
        """Connect to the Tello and start video stream."""
        try:
            self._retry_ground_command(self.tello.connect)
            self.state.battery = self.tello.get_battery()
            self.state.is_connected = True
            print(f"[{self.name}] Connected. Battery: {self.state.battery}%")

            self._retry_ground_command(self.tello.streamon)
            self._running = True
            self._start_keepalive()
            self._start_state_polling()
            return True
        except Exception as e:
            print(f"[{self.name}] Connection failed: {e}")
            return False

    def disconnect(self):
        """Land if flying, then stop streams and disconnect.

        The land goes through ``land()``, which interrupts any command in
        flight and retries the land; ``Tello.end()`` would land only once,
        because the djitellopy client no longer retries.
        """
        self._running = False
        if self.state.is_flying:
            try:
                self.land()
            except Exception as e:
                print(f"[{self.name}] Land before disconnect failed: {e}")
        with self._cmd_lock:
            try:
                self.tello.end()
            except Exception:
                pass
        self.state.is_connected = False
        self.state.is_flying = False
        print(f"[{self.name}] Disconnected.")

    def get_frame(self) -> np.ndarray | None:
        """Get the latest video frame as a BGR numpy array."""
        if not self.state.is_connected:
            return None
        try:
            frame_read = self.tello.get_frame_read()
            frame = frame_read.frame
            if frame is not None:
                # Lightweight frozen frame detection using center pixel hash
                h, w = frame.shape[:2]
                cx, cy = w // 2, h // 2
                sample = frame[max(0,cy-5):cy+5, max(0,cx-5):cx+5].tobytes()
                frame_hash = hash(sample)
                if frame_hash == getattr(self, '_last_frame_hash', None):
                    self._frozen_count = getattr(self, '_frozen_count', 0) + 1
                else:
                    self._frozen_count = 0
                self._last_frame_hash = frame_hash

                if self._frozen_count > 100:
                    if not self._stream_recovering:
                        print(f"[{self.name}] Frozen frame detected ({self._frozen_count} identical), recovering...")
                        self._recover_stream()
                    return None

                self._last_frame_time = time.time()
                with self._frame_lock:
                    self._frame = frame.copy()
                for cb in self._frame_callbacks:
                    cb(frame)
                return frame
            if (self._last_frame_time > 0
                    and time.time() - self._last_frame_time > 5
                    and not self._stream_recovering):
                self._recover_stream()
            return None
        except Exception:
            return None

    def _recover_stream(self):
        """Restart the video stream after it goes stale.

        Runs in a background thread. ``streamoff`` and ``streamon`` each go
        through ``_stream_command_when_free``, so each holds the command lock
        for its own exchange only (not for the pause between them), is never
        sent while a land is in progress, and waits for a flight command to
        finish rather than interleaving with it. A land that starts while one
        of them is on the wire does not wait for it: ``land()`` sends ``stop``
        at once and ``land`` right after it. While a land is unconfirmed
        recovery sends nothing. If the channel stays busy, recovery gives up
        and runs again the next time the stream is found stale.
        """
        self._stream_recovering = True

        def _do_recover():
            print(f"[{self.name}] Video stream stale, restarting...")
            try:
                try:
                    if not self._stream_command_when_free("streamoff"):
                        print(f"[{self.name}] Stream recovery skipped: the command channel stayed busy.")
                        return
                except Exception:
                    # A failed streamoff does not stop the streamon.
                    pass
                time.sleep(_STREAM_RESTART_PAUSE_S)
                try:
                    if self._stream_command_when_free("streamon"):
                        print(f"[{self.name}] Video stream restarted.")
                    else:
                        print(f"[{self.name}] Stream recovery failed: the command channel stayed busy.")
                except Exception as e:
                    print(f"[{self.name}] Stream recovery failed: {e}")
            finally:
                self._stream_recovering = False

        threading.Thread(target=_do_recover, daemon=True, name=f"{self.name}-stream-recover").start()

    def _stream_command_when_free(self, command: str) -> bool:
        """Send a stream command once the command channel is free. False if it never was.

        Polls without holding any lock, and does not try while a land is in
        progress or unconfirmed, so a land always takes the channel first.
        Gives up after ``_STREAM_CHANNEL_WAIT_S``, or as soon as the
        controller has stopped running. Raises like ``_stream_command_once``
        when the command was sent and not answered ``ok``.
        """
        deadline = time.monotonic() + _STREAM_CHANNEL_WAIT_S
        while True:
            if (
                not self._lands_in_progress
                and not self._land_unconfirmed
                and self._stream_command_once(command)
            ):
                return True
            if not self._running or time.monotonic() >= deadline:
                return False
            time.sleep(_STREAM_CHANNEL_POLL_S)

    def _stream_command_once(self, command: str) -> bool:
        """Send ``streamoff`` or ``streamon`` once if the channel is free.

        Returns False, sending nothing, when another command holds the
        command lock or a land is in progress or unconfirmed. Otherwise the
        command holds ``_cmd_lock`` for its whole exchange, like the
        keepalive, so its reply can never be read as the reply to a flight
        command or a land, and a move that finds it on the channel waits for
        it instead of being dropped. Holding the lock, it first waits out
        what is left of djitellopy's minimum gap since the last reply (at
        most ``_TIME_BETWEEN_COMMANDS_S``), so it can go between two lane
        moves; a land that starts during that wait ends it at once, and the
        command is not sent. The land check and the send happen under
        ``_wire_lock``, so a ``land()`` that starts at the same moment
        either stops the command from being sent or finds it on the wire
        and interrupts it at once. Returns True on an ``ok`` reply; raises
        ``RuntimeError`` on any other reply or none within
        ``_STREAM_REPLY_TIMEOUT_S``.
        """
        with self._owner_lock:
            if not self._cmd_lock.acquire(blocking=False):
                return False
            self._stream_on_channel = True
        try:
            tello = self.tello
            responses = tello.get_own_udp_object()["responses"]
            if not self._wait_command_gap_unless_landing():
                return False
            with self._wire_lock:
                if self._lands_in_progress or self._land_unconfirmed:
                    return False
                # Replies queued while the channel was free are stale.
                del responses[:]
                tello.send_command_without_return(command)
            reply = self._await_reply(responses, _STREAM_REPLY_TIMEOUT_S)
            if reply is None:
                raise RuntimeError(
                    f"[{self.name}] no reply to '{command}' within {_STREAM_REPLY_TIMEOUT_S:g} s"
                )
            if reply.lower() != "ok":
                raise RuntimeError(f"[{self.name}] '{command}' answered {reply!r}")
            # The bookkeeping that djitellopy's streamon() and streamoff() do
            # after an ok reply.
            if command == "streamon":
                tello.stream_on = True
            else:
                tello.stream_on = False
                if tello.background_frame_read is not None:
                    tello.background_frame_read.stop()
                    tello.background_frame_read = None
            return True
        finally:
            with self._owner_lock:
                self._stream_on_channel = False
                self._cmd_lock.release()

    def on_frame(self, callback: Callable[[np.ndarray], None]):
        """Register a callback that receives each new frame."""
        self._frame_callbacks.append(callback)

    # --- Flight commands ---
    # All flight commands are serialized through _cmd_lock to prevent
    # concurrent UDP commands from saturating the Tello's single channel,
    # and because a Tello reply does not say which command it answers.

    def takeoff(self, abort: threading.Event | None = None) -> bool:
        """Take off. Returns True once the Tello confirms the takeoff.

        Refused, with nothing sent and False returned, while a ``land()``
        is in progress, when a land started after this call began (a
        takeoff never runs straight after a landing it waited behind), or
        when ``abort`` is set. These are checked under the command lock in
        the same step that sends the takeoff, so a landing that starts at
        the same moment either stops the takeoff from being sent or finds
        it in flight and interrupts it. The takeoff is sent once; a reply
        other than ``ok``, or none, raises.
        """
        with self._wire_lock:
            land_seq = self._land_seq
            landing_now = self._lands_in_progress > 0
        if landing_now:
            print(f"[{self.name}] Takeoff refused: a landing is in progress.")
            return False
        with self._cmd_lock:
            if not self._send_flight_command(
                "takeoff", _TAKEOFF_REPLY_TIMEOUT_S, abort=abort, rearm_after_land=land_seq
            ):
                print(f"[{self.name}] Takeoff refused: a landing is in progress or was requested.")
                return False
            with self._wire_lock:
                # The drone is flying under command again, so an earlier land
                # that was never confirmed no longer holds off the keepalive.
                if self._land_seq == land_seq:
                    self._land_unconfirmed = False
            self.state.is_flying = True
            # djitellopy's end() lands only when its own flag says flying.
            self.tello.is_flying = True
            print(f"[{self.name}] Takeoff.")
            return True

    def land(self):
        """Land now, interrupting any flight command that is still in progress.

        From the moment this is called, no move, rotate or takeoff is sent
        until the next ``takeoff()``, including a retry of the command that
        was in flight (flight commands are never retried). If the command
        channel is free this is an ordinary ``land``. If a move, rotate,
        takeoff, keepalive or stream-recovery command is still waiting for
        its reply, the land does not queue behind it: ``stop`` (hover,
        accepted at any time by SDK 2.0 and later) is sent straight away and
        ``land`` ``_TIME_BETWEEN_COMMANDS_S`` later, without waiting for a
        reply, then a confirming ``land`` is sent once the channel is free.
        If another ``land()`` holds the channel, its land is already on the
        wire, so this one sends nothing extra (whose replies could be taken
        for that land's) and confirms after it.

        The confirming land is sent up to ``_LAND_ATTEMPTS`` times and is
        confirmed by fresh state showing the drone on the ground, or, with
        no state for ``_STATE_SEEN_S``, by a fresh ``ok`` (see
        ``_confirm_land``). ``is_flying`` stays true until then, and a land
        that is never confirmed raises, so callers keep retrying.
        """
        # The height when this land started, for telling a descent from a
        # hover (see _wait_for_ground).
        start_height = self._fresh_height()
        with self._wire_lock:
            self._landing = True
            self._land_unconfirmed = True
            self._lands_in_progress += 1
            self._land_seq += 1
        try:
            with self._owner_lock:
                took_channel = self._cmd_lock.acquire(blocking=False)
                if took_channel:
                    self._land_on_channel = True
                behind_land = not took_channel and self._land_on_channel
            if not took_channel:
                if behind_land:
                    print(f"[{self.name}] Landing: another land is confirming, waiting for it.")
                else:
                    print(f"[{self.name}] Landing now (interrupting an in-flight command).")
                    self._send_land_raw("stop")
                    self._pace_land()
                    self._send_land_raw("land")
            self._confirm_land(holding_channel=took_channel, start_height=start_height)
            print(f"[{self.name}] Landing." if took_channel else f"[{self.name}] Landing confirmed.")
        finally:
            with self._wire_lock:
                self._lands_in_progress -= 1

    def _send_land_raw(self, command: str) -> None:
        """Send ``stop`` or ``land`` for an interrupting land, without reading a reply."""
        try:
            self.tello.send_command_without_return(command)
        except Exception as e:
            print(f"[{self.name}] Immediate '{command}' failed: {e}")
        finally:
            self._land_sent_at = time.monotonic()

    def _pace_land(self) -> None:
        """Wait ``_TIME_BETWEEN_COMMANDS_S`` past both the last reply and the last command a land sent."""
        while True:
            since_sent = time.monotonic() - self._land_sent_at
            wait = max(self._command_gap_left(), _TIME_BETWEEN_COMMANDS_S - since_sent)
            if wait <= 0:
                return
            time.sleep(wait)

    def _leave_channel_after_land(self) -> None:
        """Release ``_cmd_lock`` taken by ``land()``."""
        with self._owner_lock:
            self._land_on_channel = False
            self._cmd_lock.release()

    def _take_channel_for_land(self) -> bool:
        """Take ``_cmd_lock`` for a confirming land. False if it stays busy or the land is confirmed meanwhile.

        Polls, so the flag that marks the channel as a land's is set in the
        same step that takes it. Waits up to ``_LAND_CONFIRM_WAIT_S``. State
        alone confirms nothing here: the command holding the channel may be
        a takeoff the Tello has not yet acted on, so the drone can still be
        on the ground and about to lift.
        """
        deadline = time.monotonic() + _LAND_CONFIRM_WAIT_S
        while True:
            with self._owner_lock:
                if self._cmd_lock.acquire(blocking=False):
                    self._land_on_channel = True
                    return True
            if not self._land_unconfirmed or time.monotonic() >= deadline:
                return False
            time.sleep(_REPLY_POLL_S)

    def _mark_land_confirmed(self, how: str | None = None) -> bool:
        """Record a confirmed land. False, changing nothing, if none was waiting.

        Clears ``_land_unconfirmed`` and ``is_flying`` under ``_wire_lock``,
        so the keepalive and stream recovery may run again, and marks the
        drone as believed landed until the next takeoff is sent, so fresh
        state showing it above the ground starts the land again. ``how``,
        when given, is logged.
        """
        with self._wire_lock:
            if not self._land_unconfirmed:
                return False
            self._land_unconfirmed = False
            self._believed_landed = True
            self.state.is_flying = False
            # djitellopy's end() lands only when its own flag says flying.
            self.tello.is_flying = False
        if how:
            print(f"[{self.name}] Land confirmed: {how}.")
        return True

    def _confirm_land(self, holding_channel: bool, start_height: int | None = None) -> None:
        """Send ``land`` until the drone is confirmed on the ground.

        ``holding_channel`` says whether the caller already holds
        ``_cmd_lock`` for this land; the lock is always released before this
        returns. ``start_height`` is the fresh state height when the land
        started, or None. Each attempt holds the channel only while it sends
        one ``land`` and waits for the reply (``_send_confirming_land``). Any
        further wait for the state stream happens with no lock held, so the
        state poller and other lands are never held up by it.

        A land is confirmed when either of these happens:

        * Fresh state packets that arrived after the confirming land was
          sent show the drone on the ground (see ``_ground_by_state``),
          whatever the replies were. This is checked while waiting for the
          reply too, so a drone already on the ground that never answers
          does not hold the channel for the reply timeout.
        * No state height has arrived for ``_STATE_SEEN_S``, and a fresh
          ``ok`` answers the confirming land (the reply-only confirmation
          used before the state stream was read). A shorter gap in the state
          stream is waited out instead.

        When the confirming land is answered (``ok``, ``error`` or anything
        else) while state does not show the drone on the ground, the attempt
        waits for fresh state to show it there (``_wait_for_ground``): up to
        ``_LAND_GROUND_WAIT_S`` while the height is falling, but only about
        ``_LAND_STALL_S`` while it is no lower than when the land was sent.
        A Tello answers ``error`` to a land that arrives while it is already
        landing (or while it is busy and will not land), and a late ``ok``
        may answer some earlier command, so neither reply alone says the
        drone is down. If the drone is not on the ground after that wait, or
        the land got no reply, ``land`` is sent again.

        Returns at once, sending nothing, if another land was confirmed
        since this one was requested. Raises ``RuntimeError`` after
        ``_LAND_ATTEMPTS`` unconfirmed attempts, leaving ``is_flying`` true.
        """
        outcome = "not sent"
        try:
            for attempt in range(1, _LAND_ATTEMPTS + 1):
                if not holding_channel:
                    if not self._take_channel_for_land():
                        if not self._land_unconfirmed:
                            return
                        raise RuntimeError(f"[{self.name}] land not confirmed: command channel still busy")
                    holding_channel = True
                try:
                    verdict, outcome, sent_at, sent_height = self._send_confirming_land()
                finally:
                    self._leave_channel_after_land()
                    holding_channel = False
                if verdict == "confirmed":
                    return
                if verdict == "watch":
                    # The height when the land was sent: the higher of the
                    # height when this land started and when this attempt
                    # was sent, so a descent that an earlier land began
                    # still counts as falling.
                    heights = [h for h in (start_height, sent_height) if h is not None]
                    ref_height = max(heights) if heights else None
                    confirmed, why = self._wait_for_ground(sent_at, _LAND_GROUND_WAIT_S, ref_height)
                    if confirmed:
                        return
                    outcome = f"{outcome}, but {why}"
                print(f"[{self.name}] Land attempt {attempt}/{_LAND_ATTEMPTS} not confirmed: {outcome}.")
            raise RuntimeError(
                f"[{self.name}] land not confirmed after {_LAND_ATTEMPTS} attempts (last: {outcome})"
            )
        finally:
            if holding_channel:
                self._leave_channel_after_land()

    def _send_confirming_land(self) -> tuple[str, str, float, int | None]:
        """Send one confirming ``land`` and wait for its reply. Caller holds ``_cmd_lock``.

        Waits out the command gap, then, under ``_wire_lock``, drops every
        queued reply (for example the ``ok`` that answered an interrupting
        ``stop``, which says nothing about the land) and sends ``land``. Then
        waits up to ``_LAND_REPLY_TIMEOUT_S`` for a reply, stopping early if
        fresh state shows the drone on the ground.

        Returns ``(verdict, outcome, sent_at, sent_height)``: ``verdict`` is
        ``"confirmed"`` (the land is confirmed), ``"watch"`` (the land was
        answered while state, seen within ``_STATE_SEEN_S``, does not show
        the drone on the ground; the caller waits for state with no lock
        held) or ``"failed"``; ``outcome`` describes the reply for log
        messages; ``sent_at`` is the ``time.monotonic()`` at which the land
        was sent and ``sent_height`` the fresh state height just before it
        was sent, or None.
        """
        tello = self.tello
        responses = tello.get_own_udp_object()["responses"]
        self._pace_land()
        sent_height = self._fresh_height()
        with self._wire_lock:
            if not self._land_unconfirmed:
                return "confirmed", "confirmed by another land", self._land_sent_at, sent_height
            del responses[:]
            try:
                tello.send_command_without_return("land")
                sent = True
                outcome = "no reply"
            except Exception as e:
                sent = False
                outcome = f"send failed: {e}"
            sent_at = self._land_sent_at = time.monotonic()
        reply = None
        if sent:
            reply = self._await_reply(
                responses,
                _LAND_REPLY_TIMEOUT_S,
                stop_waiting=lambda: self._ground_by_state(since=sent_at) is True,
            )
            if reply is not None:
                outcome = f"answered {reply!r}"
        on_ground = self._ground_by_state(since=sent_at)
        if on_ground:
            self._mark_land_confirmed(f"fresh state shows the drone on the ground ({outcome})")
            return "confirmed", outcome, sent_at, sent_height
        if on_ground is None:
            # No state for _STATE_SEEN_S: the land's own reply decides.
            if reply is not None and reply.lower() == "ok":
                self._mark_land_confirmed()
                return "confirmed", outcome, sent_at, sent_height
            if not self._land_unconfirmed:
                return "confirmed", outcome, sent_at, sent_height
            return "failed", outcome, sent_at, sent_height
        if not self._land_unconfirmed:
            return "confirmed", outcome, sent_at, sent_height
        return ("watch" if reply is not None else "failed"), outcome, sent_at, sent_height

    def _wait_for_ground(self, since: float, timeout_s: float, ref_height: int | None = None) -> tuple[bool, str]:
        """Wait, holding no lock, for fresh state to show the drone on the ground.

        Only state packets that arrived after ``since`` (when the land was
        sent) count. ``ref_height`` is the height when the land was sent, or
        None, in which case the first fresh height seen stands in for it.
        Returns ``(True, "")`` once the land is confirmed (by this wait,
        another land or the state poller). Returns ``(False, why)`` so the
        land is sent again, without waiting the whole ``timeout_s``, when
        the drone is not coming down:

        * ``_LAND_STALL_S`` after the land was sent, fresh state shows the
          height no lower than ``ref_height``: the land did not take, so the
          drone is hovering, still busy, or still climbing after a takeoff;
        * the height has reached no new low for ``_LAND_DESCENT_STALL_S``;
        * no state height has arrived for ``_STATE_SEEN_S``, so the next
          land's own reply decides;
        * ``timeout_s`` has passed.

        A short gap in the state stream is waited out.
        """
        deadline = time.monotonic() + timeout_s
        lowest = ref_height
        lowest_at = since
        while True:
            if not self._land_unconfirmed:
                return True, ""
            if self._ground_by_state(since=since):
                self._mark_land_confirmed("fresh state shows the drone on the ground")
                return True, ""
            now = time.monotonic()
            if now >= deadline:
                return False, f"fresh state still shows {self._state_height_text()} after {timeout_s:g} s"
            height = self._fresh_height()
            if height is None:
                if self._state_silent():
                    return False, f"no state height for {_STATE_SEEN_S:g} s"
            else:
                if ref_height is None:
                    ref_height = lowest = height
                    lowest_at = now
                elif height < lowest:
                    lowest, lowest_at = height, now
                if now - since >= _LAND_STALL_S and height >= ref_height:
                    return False, (
                        f"fresh state still shows {self._state_height_text()}, "
                        f"no lower than the {ref_height} cm when the land was sent"
                    )
                if now - lowest_at >= _LAND_DESCENT_STALL_S:
                    return False, (
                        f"fresh state still shows {self._state_height_text()}, "
                        f"no lower for {_LAND_DESCENT_STALL_S:g} s"
                    )
            time.sleep(_REPLY_POLL_S)

    def move(self, direction: str, distance_cm: int) -> bool:
        """Move in a direction. Drops command if another is in-flight.

        Returns True when the command was sent and acknowledged, and False
        when it was dropped (another flight command is in flight, a landing
        is in progress, or the direction is unknown). A move that finds only
        the keepalive on the channel waits for it rather than being dropped.
        The move is sent once and never retried; a reply other than ``ok``,
        or none, raises.
        """
        if self._landing or direction not in _MOVE_DIRECTIONS:
            return False
        if not self._acquire_for_flight():
            return False
        try:
            distance_cm = max(20, min(500, int(distance_cm)))
            return self._send_flight_command(f"{direction} {distance_cm}", _COMMAND_REPLY_TIMEOUT_S)
        finally:
            self._cmd_lock.release()

    def rotate(self, degrees: int) -> bool:
        """Rotate clockwise/counter-clockwise. Drops if another command is in-flight.

        Returns True when the command was sent and acknowledged, False when
        it was dropped. Like ``move()``, it is sent once and never retried.
        """
        if self._landing:
            return False
        if not self._acquire_for_flight():
            return False
        try:
            if degrees > 0:
                command = f"cw {min(360, int(degrees))}"
            else:
                command = f"ccw {min(360, abs(int(degrees)))}"
            return self._send_flight_command(command, _COMMAND_REPLY_TIMEOUT_S)
        finally:
            self._cmd_lock.release()

    def _acquire_for_flight(self) -> bool:
        """Take ``_cmd_lock`` for a move or rotate; False if a flight command holds it.

        The keepalive and a stream-recovery command each hold the channel
        for one short exchange, so a move that finds either there waits for
        it instead of being dropped (a dropped lane move makes the lane
        executor land).

        The minimum gap after the last reply is waited out before the lock
        is taken, not while holding it, so back-to-back lane moves leave the
        channel free for that long and a stream-recovery command can get in
        between them. ``_send_flight_command`` waits out the gap again under
        the lock, which takes no time unless another command got in.
        """
        gap = self._command_gap_left()
        if gap > 0:
            time.sleep(gap)
        with self._owner_lock:
            if self._cmd_lock.acquire(blocking=False):
                return True
            behind_short_exchange = self._keepalive_on_channel or self._stream_on_channel
        if not behind_short_exchange:
            return False
        return self._cmd_lock.acquire(timeout=_KEEPALIVE_WAIT_S)

    def _send_flight_command(
        self,
        command: str,
        timeout_s: float,
        *,
        abort: threading.Event | None = None,
        rearm_after_land: int | None = None,
    ) -> bool:
        """Send one flight command exactly once and wait for its reply.

        Caller holds ``_cmd_lock``. Returns False, sending nothing, when a
        landing has started (or ``abort`` is set). For a takeoff,
        ``rearm_after_land`` is the land count read when the takeoff was
        called: a finished landing is then cleared, but a landing still in
        progress, or one started since, refuses the command. The check and
        the send happen under ``_wire_lock``, so a ``land()`` that starts at
        the same moment is ordered either before the check or after the
        command is on the wire. Returns True on an ``ok`` reply; raises
        ``RuntimeError`` on any other reply or none within ``timeout_s``.
        Nothing is retried.
        """
        tello = self.tello
        self._pace_commands()
        responses = tello.get_own_udp_object()["responses"]
        with self._wire_lock:
            if abort is not None and abort.is_set():
                return False
            if rearm_after_land is not None:
                if self._lands_in_progress or self._land_seq != rearm_after_land:
                    return False
                self._landing = False
            elif self._landing:
                return False
            # Queued replies belong to earlier exchanges, such as the stop
            # and land of an interrupting landing. Drop them so this command
            # reads its own reply.
            del responses[:]
            if command == "takeoff":
                # Recorded before the send, so a takeoff that may have gone
                # out counts as sent: until state shows the drone above the
                # ground, ground readings may be a motor spin-up.
                self._takeoff_sent_at = time.monotonic()
                self._believed_landed = False
            tello.send_command_without_return(command)
        reply = self._await_reply(responses, timeout_s)
        if reply is None:
            raise RuntimeError(
                f"[{self.name}] no reply to '{command}' within {timeout_s:g} s (not retried)"
            )
        if reply.lower() != "ok":
            raise RuntimeError(f"[{self.name}] '{command}' answered {reply!r} (not retried)")
        return True

    def _await_reply(
        self,
        responses: list,
        timeout_s: float,
        stop_waiting: Callable[[], bool] | None = None,
    ) -> str | None:
        """Read the reply to the command just sent, or None if none arrives within ``timeout_s``.

        Caller holds ``_cmd_lock``. Records the reply time, as djitellopy
        does, so the next command keeps the minimum gap. Also returns None,
        without waiting further, as soon as ``stop_waiting()`` is true.
        """
        sent_at = time.monotonic()
        while not responses:
            if time.monotonic() - sent_at > timeout_s:
                return None
            if stop_waiting is not None and stop_waiting():
                return None
            time.sleep(_REPLY_POLL_S)
        self.tello.last_received_command_timestamp = time.time()
        raw = responses.pop(0)
        if isinstance(raw, (bytes, bytearray)):
            reply = bytes(raw).decode("utf-8", "replace")
        else:
            reply = str(raw)
        return reply.strip()

    def _command_gap_left(self) -> float:
        """Seconds left of djitellopy's minimum gap after the last reply, or 0 if it has passed."""
        try:
            gap = time.time() - float(self.tello.last_received_command_timestamp)
        except (TypeError, ValueError):
            return 0.0
        if 0 <= gap < _TIME_BETWEEN_COMMANDS_S:
            return _TIME_BETWEEN_COMMANDS_S - gap
        return 0.0

    def _pace_commands(self) -> None:
        """Wait until djitellopy's minimum gap after the last reply has passed."""
        wait = self._command_gap_left()
        if wait > 0:
            time.sleep(wait)

    def _wait_command_gap_unless_landing(self) -> bool:
        """Wait out the minimum gap after the last reply. False, at once, if a land starts.

        Caller holds ``_cmd_lock``. Waits at most ``_TIME_BETWEEN_COMMANDS_S``
        and checks for a land every ``_REPLY_POLL_S``, so a land that starts
        meanwhile gets the channel without waiting for the rest of the gap.
        """
        deadline = time.monotonic() + min(self._command_gap_left(), _TIME_BETWEEN_COMMANDS_S)
        while True:
            if self._lands_in_progress or self._land_unconfirmed:
                return False
            left = deadline - time.monotonic()
            if left <= 0:
                return True
            time.sleep(min(left, _REPLY_POLL_S))

    def _retry_ground_command(self, fn: Callable[[], object]) -> None:
        """Run a connect-time command (``command``, ``streamon``) with retries.

        These never move the drone, so retrying them is safe. djitellopy no
        longer retries anything itself (``retry_count=1``).
        """
        for attempt in range(1, _CONNECT_ATTEMPTS + 1):
            try:
                fn()
                return
            except Exception:
                if attempt == _CONNECT_ATTEMPTS:
                    raise

    def read_battery(self) -> int | None:
        """Battery percentage from the latest state packet, or None if unknown.

        Reads the state stream (no UDP command), so it is safe to call while
        a flight command is in progress.
        """
        try:
            battery = int(self.tello.get_battery())
        except Exception:
            return None
        self.state.battery = battery
        return battery

    def read_height_cm(self) -> int | None:
        """Height above takeoff in cm from the latest state packet, or None if unknown."""
        try:
            height = int(self.tello.get_height())
        except Exception:
            return None
        self.state.height = height
        return height

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int):
        """Send RC joystick control. Each value -100 to 100.

        Once a landing has started, only an all-zero (hover) command is
        sent until the next takeoff.
        """
        with self._wire_lock:
            if self._landing and any((lr, fb, ud, yaw)):
                return
            self.tello.send_rc_control(lr, fb, ud, yaw)

    def hover(self):
        """Stop all movement and hover in place."""
        self.tello.send_rc_control(0, 0, 0, 0)

    # NOTE: no goto_gps() — the standard Tello has no GPS. TelloController
    # intentionally only satisfies `DroneController`, not
    # `GpsDroneController` (see flightrisk/drone/controller.py). Callers must
    # check `isinstance(drone, GpsDroneController)` or
    # `drone.capabilities.has_gps` before invoking GPS navigation.

    # --- Internal threads ---

    def _start_keepalive(self):
        """Send keepalive every 10s to prevent auto-landing."""
        def _keepalive():
            while self._running:
                self._keepalive_once()
                time.sleep(_KEEPALIVE_INTERVAL_S)

        self._keepalive_thread = threading.Thread(
            target=_keepalive, daemon=True, name=f"{self.name}-keepalive"
        )
        self._keepalive_thread.start()

    def _keepalive_once(self) -> bool:
        """Send one keepalive if the command channel is free. Returns True if it was answered.

        The keepalive holds ``_cmd_lock`` for its whole exchange, so its
        ``ok`` can never be read as the reply to a land or a flight command.
        While a flight command holds the channel the keepalive is skipped;
        that command keeps the link alive. It is also skipped from the
        moment a land starts until a land is confirmed: a keepalive would
        restart the Tello's 15 s auto-land timer, which is the last resort
        when every land packet is lost.
        """
        with self._owner_lock:
            if not self._cmd_lock.acquire(blocking=False):
                return False
            self._keepalive_on_channel = True
        try:
            with self._wire_lock:
                if self._land_unconfirmed:
                    return False
            try:
                # Replies queued while the channel was free are stale.
                del self.tello.get_own_udp_object()["responses"][:]
            except Exception:
                pass
            self.tello.send_control_command("command", timeout=_KEEPALIVE_TIMEOUT_S)
            return True
        except Exception:
            return False
        finally:
            with self._owner_lock:
                self._keepalive_on_channel = False
                self._cmd_lock.release()

    def _start_state_polling(self):
        """Poll drone state every ``_STATE_POLL_INTERVAL_S``. Skips while a command holds the channel."""
        def _poll():
            poll_failures = 0
            while self._running:
                if self._cmd_lock.locked():
                    time.sleep(_STATE_POLL_INTERVAL_S)
                    continue
                try:
                    self.state.battery = self.tello.get_battery()
                    self.state.height = self.tello.get_height()
                    self.state.temperature = self.tello.get_temperature()
                    self.state.flight_time = self.tello.get_flight_time()
                    poll_failures = 0
                    self._check_zero_height()
                except Exception:
                    poll_failures += 1
                    if poll_failures >= 3 and self.state.is_connected:
                        self.state.is_connected = False
                        self._running = False
                        print(f"[{self.name}] Connection lost — {poll_failures} consecutive poll failures.")
                time.sleep(_STATE_POLL_INTERVAL_S)

        self._state_thread = threading.Thread(
            target=_poll, daemon=True, name=f"{self.name}-state"
        )
        self._state_thread.start()

    def _check_zero_height(self) -> None:
        """The state poller's zero-height path, run after each successful poll.

        Counts polls in a row that read height 0 while the drone is flying
        or a land is unconfirmed. After ``_ZERO_HEIGHT_POLLS`` of them: if a
        land is unconfirmed and fresh state shows the drone on the ground,
        the land is confirmed, which also clears ``is_flying`` and lets the
        keepalive and stream recovery run again. Otherwise a drone still
        marked flying is marked landed and reported as a crash. A stale
        height never clears an unconfirmed land.
        """
        if not (self.state.is_flying or self._land_unconfirmed) or self.state.height != 0:
            self._zero_height_count = 0
            return
        self._zero_height_count += 1
        if self._zero_height_count < _ZERO_HEIGHT_POLLS:
            return
        polls, self._zero_height_count = self._zero_height_count, 0
        if (
            self._land_unconfirmed
            and self._ground_by_state()
            and self._mark_land_confirmed(f"height 0 for {polls} polls")
        ):
            return
        if self.state.is_flying:
            self.state.is_flying = False
            print(f"[{self.name}] Crash detected — height 0 for {polls} polls.")


class FakeTelloController:
    """In-process simulated Tello with the ``TelloController`` interface.

    Nothing here touches the network. Flight commands update a simulated
    state and are appended to ``calls`` as tuples (``("takeoff",)``,
    ``("land",)``, ``("move", direction, distance_cm)``,
    ``("rotate", degrees)``), so tests can assert on exactly what a real
    drone would have been told. Like the real drone, it refuses to move while
    on the ground.

    ``speed_cm_s`` makes moves take simulated time (distance divided by
    speed), which lets a fake edge show lane progress in the dashboard. A
    landing interrupts a move that is in progress.
    """

    TAKEOFF_HEIGHT_CM = 80

    def __init__(
        self,
        name: str = "fake-tello",
        battery: int = 100,
        takeoff_height_cm: int = TAKEOFF_HEIGHT_CM,
        speed_cm_s: float | None = None,
        frame_shape: tuple[int, int, int] = (720, 960, 3),
    ):
        self.name = name
        self.host = "fake"
        self.state = DroneState(battery=battery)
        self.capabilities = DroneCapabilities(
            has_gps=False,
            has_rtsp=False,
            min_move_cm=20,
            max_move_cm=500,
            max_altitude_m=10,
            supports_missions=False,
        )
        self.calls: list[tuple] = []
        self.takeoff_height_cm = takeoff_height_cm
        self.speed_cm_s = speed_cm_s
        self._frame_shape = frame_shape
        self._frame_callbacks: list[Callable[[np.ndarray], None]] = []
        self._lock = threading.Lock()
        self._interrupt = threading.Event()

    def _record(self, *call) -> None:
        with self._lock:
            self.calls.append(call)

    def connect(self) -> bool:
        self.state.is_connected = True
        print(f"[{self.name}] Fake drone connected. Battery: {self.state.battery}%")
        return True

    def disconnect(self) -> None:
        if self.state.is_flying:
            self.land()
        self.state.is_connected = False

    def get_frame(self) -> np.ndarray | None:
        if not self.state.is_connected:
            return None
        frame = np.zeros(self._frame_shape, dtype=np.uint8)
        for cb in self._frame_callbacks:
            cb(frame)
        return frame

    def on_frame(self, callback: Callable[[np.ndarray], None]) -> None:
        self._frame_callbacks.append(callback)

    def takeoff(self, abort: threading.Event | None = None) -> bool:
        """Take off unless ``abort`` is set, like ``TelloController.takeoff``."""
        with self._lock:
            if abort is not None and abort.is_set():
                return False
            self.calls.append(("takeoff",))
            self._interrupt.clear()
            self.state.is_flying = True
            self.state.height = self.takeoff_height_cm
        return True

    def land(self) -> None:
        self._interrupt.set()
        self._record("land")
        self.state.is_flying = False
        self.state.height = 0

    def _fly_for(self, distance_cm: float) -> None:
        if self.speed_cm_s:
            self._interrupt.wait(distance_cm / self.speed_cm_s)

    def move(self, direction: str, distance_cm: int) -> bool:
        if not self.state.is_flying:
            raise RuntimeError(f"[{self.name}] cannot move while landed")
        if direction not in ("forward", "back", "left", "right", "up", "down"):
            return False
        self._record("move", direction, distance_cm)
        self._fly_for(distance_cm)
        if direction == "up":
            self.state.height += distance_cm
        elif direction == "down":
            self.state.height = max(0, self.state.height - distance_cm)
        return True

    def rotate(self, degrees: int) -> bool:
        if not self.state.is_flying:
            raise RuntimeError(f"[{self.name}] cannot rotate while landed")
        self._record("rotate", degrees)
        return True

    def hover(self) -> None:
        self._record("hover")

    def rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> None:
        self._record("rc", lr, fb, ud, yaw)

    def read_battery(self) -> int | None:
        return self.state.battery

    def read_height_cm(self) -> int | None:
        return self.state.height
