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
from typing import Callable

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

# How long an interrupting land waits for an in-flight command to release the
# command channel before it gives up on the confirming land. A flight command
# waits at most _TAKEOFF_REPLY_TIMEOUT_S for its single reply, and another
# land confirming on the channel holds it for at most _LAND_ATTEMPTS times
# (_LAND_REPLY_TIMEOUT_S plus the command gap), so 25 s covers either.
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
# again. Only an ``ok`` that arrives after the land was sent confirms it.
_LAND_REPLY_TIMEOUT_S = _COMMAND_REPLY_TIMEOUT_S
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

    A land counts as done only when a fresh ``ok`` answers it. Until then
    ``state.is_flying`` stays true, so callers keep retrying, and neither the
    keepalive nor stream recovery sends anything, so the Tello's own 15 s
    auto-land still fires if every land is lost.
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
        # confirmed by a fresh "ok" or a takeoff succeeds with no land since.
        # While it is set the keepalive and stream recovery send nothing:
        # any command the Tello receives restarts its 15 s auto-land timer,
        # which is the last resort when every land packet is lost.
        self._land_unconfirmed = False
        # time.monotonic() when land() last sent a command whose reply it
        # did not read first. The next land waits _TIME_BETWEEN_COMMANDS_S
        # after it, because the Tello ignores commands sent too close
        # together.
        self._land_sent_at = 0.0
        # True while the keepalive (or, for _stream_on_channel, a
        # stream-recovery command; for _land_on_channel, a land) holds
        # _cmd_lock. Guarded by _owner_lock, which is also held while any of
        # them takes or releases _cmd_lock.
        self._owner_lock = threading.Lock()
        self._keepalive_on_channel = False
        self._stream_on_channel = False
        self._land_on_channel = False

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

        The confirming land is sent up to ``_LAND_ATTEMPTS`` times, and only
        a fresh ``ok`` confirms it (see ``_confirm_land``). ``is_flying``
        stays true until then, and a land that is never confirmed raises, so
        callers keep retrying.
        """
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
            if took_channel:
                try:
                    self._confirm_land()
                    print(f"[{self.name}] Landing.")
                finally:
                    self._leave_channel_after_land()
                return

            if behind_land:
                print(f"[{self.name}] Landing: another land is confirming, waiting for it.")
            else:
                print(f"[{self.name}] Landing now (interrupting an in-flight command).")
                self._send_land_raw("stop")
                self._pace_land()
                self._send_land_raw("land")
            if not self._cmd_lock.acquire(timeout=_LAND_CONFIRM_WAIT_S):
                raise RuntimeError(f"[{self.name}] land not confirmed: command channel still busy")
            with self._owner_lock:
                self._land_on_channel = True
            try:
                self._confirm_land()
                print(f"[{self.name}] Landing confirmed.")
            finally:
                self._leave_channel_after_land()
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

    def _confirm_land(self) -> None:
        """Send ``land`` until a fresh ``ok`` confirms it. Caller holds ``_cmd_lock``.

        Each attempt waits out the command gap, then, under ``_wire_lock``,
        drops every queued reply (for example the ``ok`` that answered an
        interrupting ``stop``, which says nothing about the land) and sends
        ``land``. It then waits up to ``_LAND_REPLY_TIMEOUT_S`` for a reply;
        only ``ok`` confirms the land. On confirmation ``is_flying`` is
        cleared and the keepalive may run again. Returns at once, sending
        nothing, if another ``land()`` was confirmed since this one was
        requested. Raises ``RuntimeError`` after ``_LAND_ATTEMPTS``
        unconfirmed attempts, leaving ``is_flying`` true.
        """
        tello = self.tello
        responses = tello.get_own_udp_object()["responses"]
        outcome = "not sent"
        for attempt in range(1, _LAND_ATTEMPTS + 1):
            self._pace_land()
            with self._wire_lock:
                if not self._land_unconfirmed:
                    return
                del responses[:]
                try:
                    tello.send_command_without_return("land")
                    sent = True
                except Exception as e:
                    outcome = f"send failed: {e}"
                    sent = False
                self._land_sent_at = time.monotonic()
            if sent:
                reply = self._await_reply(responses, _LAND_REPLY_TIMEOUT_S)
                if reply is not None and reply.lower() == "ok":
                    with self._wire_lock:
                        self._land_unconfirmed = False
                        self.state.is_flying = False
                        # djitellopy's end() lands only when its own flag says flying.
                        tello.is_flying = False
                    return
                outcome = "no reply" if reply is None else f"answered {reply!r}"
            print(f"[{self.name}] Land attempt {attempt}/{_LAND_ATTEMPTS} not confirmed: {outcome}.")
        raise RuntimeError(
            f"[{self.name}] land not confirmed after {_LAND_ATTEMPTS} attempts (last: {outcome})"
        )

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
            tello.send_command_without_return(command)
        reply = self._await_reply(responses, timeout_s)
        if reply is None:
            raise RuntimeError(
                f"[{self.name}] no reply to '{command}' within {timeout_s:g} s (not retried)"
            )
        if reply.lower() != "ok":
            raise RuntimeError(f"[{self.name}] '{command}' answered {reply!r} (not retried)")
        return True

    def _await_reply(self, responses: list, timeout_s: float) -> str | None:
        """Read the reply to the command just sent, or None if none arrives within ``timeout_s``.

        Caller holds ``_cmd_lock``. Records the reply time, as djitellopy
        does, so the next command keeps the minimum gap.
        """
        sent_at = time.monotonic()
        while not responses:
            if time.monotonic() - sent_at > timeout_s:
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
        """Poll drone state every 2 seconds. Skips when a flight command is active."""
        def _poll():
            poll_failures = 0
            while self._running:
                if self._cmd_lock.locked():
                    time.sleep(2)
                    continue
                try:
                    self.state.battery = self.tello.get_battery()
                    self.state.height = self.tello.get_height()
                    self.state.temperature = self.tello.get_temperature()
                    self.state.flight_time = self.tello.get_flight_time()
                    poll_failures = 0
                    if self.state.is_flying and self.state.height == 0:
                        zero_height_count = getattr(self, '_zero_height_count', 0) + 1
                        self._zero_height_count = zero_height_count
                        if zero_height_count >= 3:
                            self.state.is_flying = False
                            self._zero_height_count = 0
                            print(f"[{self.name}] Crash detected — height 0 for {zero_height_count} polls.")
                    else:
                        self._zero_height_count = 0
                except Exception:
                    poll_failures += 1
                    if poll_failures >= 3 and self.state.is_connected:
                        self.state.is_connected = False
                        self._running = False
                        print(f"[{self.name}] Connection lost — {poll_failures} consecutive poll failures.")
                time.sleep(2)

        self._state_thread = threading.Thread(
            target=_poll, daemon=True, name=f"{self.name}-state"
        )
        self._state_thread.start()


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
