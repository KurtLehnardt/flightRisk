"""Tello drone controller wrapper around DJITelloPy.

Handles connection, video streaming, flight commands, and keepalive.
Standard Tello (TLW004) — SDK 1.3, WiFi AP mode only.

``FakeTelloController`` is an in-process stand-in with the same interface. It
never touches the network, so the edge device can fly a simulated drone
(``python -m flightrisk.edge_device --source fake``) and tests can exercise
flight logic without hardware.
"""

import math
import threading
import time
import weakref
from collections import deque
from typing import Callable, NamedTuple

import djitellopy.tello as _djitello
import numpy as np
from djitellopy import Tello

from flightrisk.config import get_config
from flightrisk.drone.controller import (
    BASIC_FLIGHT,
    CommandOutcome,
    CommandResult,
    DroneCapabilities,
    DroneController,
    DroneState,
)
from flightrisk.drone.snapshot import (
    CELSIUS,
    DEGREE,
    IS_FLYING_COMMANDED,
    METRE,
    METRE_PER_SECOND,
    PERCENT,
    SECOND,
    Reading,
    TelemetrySnapshot,
)
from flightrisk.drone.snapshot import reading as _reading

# Backward-compatible re-export so `from flightrisk.drone.tello import DroneState` still works
__all__ = [
    "TelloController",
    "FakeTelloController",
    "DroneState",
    "DroneCapabilities",
    "DroneController",
]

# The longest one land() call runs. The wait for the command channel, every
# confirming land's wait for its reply and every wait for the state stream
# all come out of this one budget, so a drone that never confirms a land
# holds up its caller (the lane executor's monitor, LAND ALL) for at most
# this long. A land that is not confirmed by then raises, and callers retry
# while ``is_flying`` stays true. A flight command waits at most
# _TAKEOFF_REPLY_TIMEOUT_S for its single reply, so 25 s still leaves time
# for a confirming land after a takeoff that holds the channel to the end.
_LAND_DEADLINE_S = 25.0
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
# again. Once the state stream is lost, only an ``ok`` that arrives after the
# land was sent confirms it.
_LAND_REPLY_TIMEOUT_S = _COMMAND_REPLY_TIMEOUT_S
# The Tello sends a state packet (UDP 8890) about ten times a second. A
# packet counts as fresh for this long after it arrives.
_STATE_FRESH_S = 0.5
# The state stream counts as lost once no state packet carrying a height has
# arrived for this long (or none ever has). A shorter silence is a gap: Wi-Fi
# often drops state packets for a moment while command replies still arrive,
# and a reply read during a gap may be the late answer to an earlier command
# (the interrupting ``stop``, or the move or takeoff it interrupted) while the
# drone still hovers. So during a gap a land waits for the stream to come
# back, and only once the stream is lost does a land's own ``ok`` confirm it.
_STATE_LOST_S = 3.0
# This many recent state packets are kept, enough to cover _MOTOR_OFF_S at
# up to 20 packets a second.
_STATE_READINGS_KEPT = 128
# A state height ``h`` whose absolute value is at or below this, in cm,
# counts as ground height. ``h`` is measured from the takeoff point, so a
# drone launched from a ledge, roof or car and hovering below that point
# reports a negative ``h``; the absolute value keeps it from counting as
# ground.
_GROUND_HEIGHT_CM = 10
# The downward time-of-flight distance (state field ``tof``) of a Tello
# resting on the ground reads about 10 cm. A reading above this, in cm,
# (including the out-of-range value 6553) means the drone is not on the
# ground.
_GROUND_TOF_CM = 30
# This many state packets in a row, each fresh when the next arrived, the
# newest still fresh, and all received after the confirming land was sent,
# must show ground height (and, when ``tof`` is present, ground distance)
# before the state stream confirms a land.
_GROUND_READINGS = 3
# The state field ``time`` counts, in whole seconds, how long the motors
# have run, so it goes up at least once a second while they spin. State
# confirms a land only when ``time`` has stayed the same across this long
# of consecutive fresh state packets: the motors are off. A drone that
# hovers with a ground-like ``h`` (a takeoff point above the ground, or a
# Tello still spinning up for a takeoff) has its motors running.
_MOTOR_OFF_S = 2.0
# Within a run of packets that shows the motors off (see ``_motors_off``),
# each packet may arrive up to this long after the one before. The motor time
# only goes up, and goes up at least once a second while the motors spin, so
# packets that all carry the same motor time across _MOTOR_OFF_S prove the
# motors off however they are spaced; a longer silence still ends the run,
# so a stream that was mostly silent never counts as watched. A Tello's state
# stream over Wi-Fi often pauses for half a second or so.
_MOTOR_RUN_GAP_S = 1.0
# A Tello answers ``takeoff`` only once it hovers, so a land can find a
# takeoff still spinning up on the ground. If the Tello ignores the stop and
# land, it lifts after them. A land that starts while a takeoff waits for its
# reply, or after a takeoff whose wait ended without an ``ok`` (the reply
# read may have been the late answer to some earlier command), starts two
# guards, both measured from when that takeoff was sent:
#
# * The lift guard, this long: nothing (no state packet and no reply)
#   confirms a land. A Tello that acts on a takeoff lifts within about 2 s
#   and reaches its hover height within about 5 s. A reply cannot stand in
#   for this wait: an ``ok`` read then may be the late answer to the
#   interrupting ``stop`` or to the takeoff itself.
# * The reply guard, _TAKEOFF_REPLY_TIMEOUT_S: the Tello's own ``ok`` to the
#   takeoff, sent once it hovers, may still arrive and be read as a land's
#   reply, so a reply alone (with the state stream lost) confirms nothing.
#   Fresh state may confirm a land once the lift guard is over.
#
# A takeoff answered ``ok`` before the land started starts neither guard:
# that ``ok`` has been read, and the Tello sends it only once it hovers.
_TAKEOFF_LIFT_GUARD_S = 10.0
# When a confirming land is answered (``ok`` or not) while fresh state still
# shows the drone in the air (or a takeoff it interrupted may still lift),
# the land waits up to this long, holding no lock, for fresh state to show
# it on the ground before sending land again.
_LAND_GROUND_WAIT_S = 6.0
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

#: Extra state-packet fields read on each state poll for ``snapshot()``.
#: djitellopy serves these from the cached UDP state packet, so reading them
#: sends nothing to the drone. (field, djitellopy getter, scale to SI, unit,
#: source label) Units: vgx/vgy/vgz are decimetres per second, tof and baro
#: (as returned by djitellopy) centimetres, attitude degrees. The speed unit
#: follows the Tello SDK community documentation and has not been checked on
#: hardware. Wi-Fi SNR is deliberately not read: it needs a ``wifi?`` query
#: on the command channel, which would compete with flight commands.
_EXTENDED_FIELDS: tuple[tuple[str, str, float, str, str], ...] = (
    ("vx_mps", "get_speed_x", 0.1, METRE_PER_SECOND, "tello.vgx"),
    ("vy_mps", "get_speed_y", 0.1, METRE_PER_SECOND, "tello.vgy"),
    ("vz_mps", "get_speed_z", 0.1, METRE_PER_SECOND, "tello.vgz"),
    ("tof_m", "get_distance_tof", 0.01, METRE, "tello.tof"),
    ("baro_altitude_m", "get_barometer", 0.01, METRE, "tello.baro"),
    ("pitch_deg", "get_pitch", 1.0, DEGREE, "tello.pitch"),
    ("roll_deg", "get_roll", 1.0, DEGREE, "tello.roll"),
    ("yaw_deg", "get_yaw", 1.0, DEGREE, "tello.yaw"),
)


class _StateReading(NamedTuple):
    """One Tello state packet as landing reads it. A field the packet lacks, or that is not a number, is None."""

    at: float  # time.monotonic() on arrival
    height: int | None  # ``h``: cm above (or, negative, below) the takeoff point
    tof: int | None  # ``tof``: downward time-of-flight distance in cm
    motor_s: int | None  # ``time``: whole seconds the motors have run


class _LandAttempt(NamedTuple):
    """The result of one confirming land (see ``TelloController._send_confirming_land``)."""

    verdict: str  # "confirmed", "watch" or "failed"
    outcome: str  # the reply, for log messages
    sent_at: float  # time.monotonic() when the land was sent
    ok_seen: bool  # answered "ok" outside the lift guard: may back up ground state
    ok_alone: bool  # answered "ok" outside the reply guard: may confirm with the state stream lost


def _state_int(state: dict, key: str) -> int | None:
    """The state field ``key`` as an int, or None if it is missing or not a number."""
    try:
        return int(state[key])
    except (KeyError, TypeError, ValueError):
        return None


def _tof_valid(tof: int | None) -> bool:
    """Whether ``tof`` is a usable distance. A missing, zero or negative reading proves nothing."""
    return tof is not None and tof > 0


def _shows_ground(reading: _StateReading) -> bool:
    """Whether one state packet is consistent with the drone resting on the ground.

    ``|h|`` must be at most ``_GROUND_HEIGHT_CM`` (a drone hovering below
    its takeoff point reports a negative ``h``), and a usable ``tof`` must
    not be above ``_GROUND_TOF_CM``.
    """
    if reading.height is None or abs(reading.height) > _GROUND_HEIGHT_CM:
        return False
    return not (_tof_valid(reading.tof) and reading.tof > _GROUND_TOF_CM)


def _motors_off(readings: list[_StateReading]) -> bool | None:
    """Whether the newest state packets show the motors off.

    The motor time (state field ``time``) goes up at least once a second
    while the motors spin. True when it is the same in every packet of a run
    (each packet within ``_MOTOR_RUN_GAP_S`` of the one before) that ends
    with the newest packet and spans at least ``_MOTOR_OFF_S``. Any change
    of the motor time ends the run. False when it changed within that span
    or the run is shorter. None when the newest packet has no motor time.
    """
    newest = readings[-1]
    if newest.motor_s is None:
        return None
    earliest = newest
    for reading in reversed(readings[:-1]):
        if reading.motor_s != newest.motor_s or earliest.at - reading.at > _MOTOR_RUN_GAP_S:
            break
        earliest = reading
    return newest.at - earliest.at >= _MOTOR_OFF_S


def _motors_running(readings: list[_StateReading]) -> bool:
    """Whether the motor time changed within the last ``_MOTOR_OFF_S`` of state packets: the motors spin."""
    newest = readings[-1]
    if newest.motor_s is None:
        return False
    return any(
        reading.motor_s is not None and reading.motor_s != newest.motor_s
        for reading in readings
        if newest.at - reading.at <= _MOTOR_OFF_S
    )


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

    def __init__(self, base: dict, on_state: Callable[[object], None]):
        super().__init__(base)
        # A weak reference, so this module-level entry does not keep the
        # controller alive.
        self._on_state = weakref.WeakMethod(on_state)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "state":
            on_state = self._on_state()
            if on_state is not None:
                try:
                    on_state(value)
                except Exception:
                    pass


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

    A land is confirmed by the drone's own state stream whenever it is
    fresh: fresh state packets that arrive after the confirming ``land`` was
    sent must show the drone on the ground (``|h|`` and ``tof`` near zero)
    with its motors off (the motor time has stopped going up), whatever the
    command replies were (see ``_confirm_land`` and ``_ground_by_state``).
    A brief gap in the stream is waited out. Only once the stream is lost
    (no packet for ``_STATE_LOST_S``) does a fresh ``ok`` to the confirming
    land confirm it on its own. A land that finds a takeoff that may still
    lift (one still waiting for its reply, or one whose wait ended without
    an ``ok``) is confirmed by nothing until ``_TAKEOFF_LIFT_GUARD_S`` after
    that takeoff, and by no reply alone until ``_TAKEOFF_REPLY_TIMEOUT_S``
    after it, because the Tello may still be spinning up and lift after the
    land and answer the takeoff once it hovers. Until a land is confirmed
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
            features=BASIC_FLIGHT,
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
        # confirmed (by fresh state showing the drone on the ground with its
        # motors off, or, with the state stream lost, by a fresh "ok"), when the
        # state poller finds the drone on the ground the same way, or when a
        # takeoff succeeds with no land since.
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
        # The most recent state packets, appended by djitellopy's state
        # receiver thread through _StateStampedUdpObject. Guarded by
        # _state_lock.
        self._state_lock = threading.Lock()
        self._state_readings: deque[_StateReading] = deque(maxlen=_STATE_READINGS_KEPT)
        # Takeoff bookkeeping for the lift and reply guards (see
        # _TAKEOFF_LIFT_GUARD_S), all guarded by _wire_lock.
        # _takeoff_in_flight is true from the moment a takeoff is sent until
        # its reply wait ends (whatever reply it read, or none);
        # _takeoff_sent_at is time.monotonic() when the last takeoff was sent
        # (None before the first); _takeoff_answered_ok is whether that
        # takeoff's wait ended on an "ok". A land that starts while a takeoff
        # is in flight, or after one that was not answered "ok", sets
        # _lift_guard_until, the time.monotonic() before which nothing
        # confirms a land, and _reply_guard_until, before which no reply
        # alone does. The next takeoff clears both.
        self._takeoff_in_flight = False
        self._takeoff_sent_at: float | None = None
        self._takeoff_answered_ok = False
        self._lift_guard_until = 0.0
        self._reply_guard_until = 0.0
        self._zero_height_count = 0
        # Telemetry bookkeeping for snapshot(). _measured_at maps each
        # DroneState field ("battery", "height", "temperature",
        # "flight_time") to the time.time() it was last read from the drone,
        # by connect(), the state poller or read_battery()/read_height_cm().
        # A field never read has no entry and shows up as None. _extended
        # holds the extra state-packet readings of the last successful poll.
        self._measured_at: dict[str, float] = {}
        self._extended: dict[str, Reading] = {}
        self._poll_failures = 0
        self._stamp_state_packets(host)

    def _stamp_state_packets(self, host: str) -> None:
        """Have djitellopy report each state packet for ``host`` to ``_note_state_packet``."""
        try:
            udp = _djitello.drones.get(host)
            if type(udp) is dict:
                _djitello.drones[host] = _StateStampedUdpObject(udp, self._note_state_packet)
        except Exception:
            # No arrival times: landing falls back to reply-based confirmation.
            pass

    def _note_state_packet(self, state: object) -> None:
        """Record when a state packet arrived and the fields landing reads from it."""
        if isinstance(state, dict):
            reading = _StateReading(
                time.monotonic(), _state_int(state, "h"), _state_int(state, "tof"), _state_int(state, "time")
            )
        else:
            reading = _StateReading(time.monotonic(), None, None, None)
        with self._state_lock:
            self._state_readings.append(reading)

    def _lift_guard_left(self) -> float:
        """Seconds until a land that found a takeoff that may still lift may be confirmed, or 0 (see ``_TAKEOFF_LIFT_GUARD_S``)."""
        return max(0.0, self._lift_guard_until - time.monotonic())

    def _reply_guard_left(self) -> float:
        """Seconds until a reply alone may confirm such a land, or 0 (see ``_TAKEOFF_LIFT_GUARD_S``)."""
        return max(0.0, self._reply_guard_until - time.monotonic())

    def _height_readings(self) -> list[_StateReading]:
        """The kept state packets that carry a height, oldest first."""
        with self._state_lock:
            return [reading for reading in self._state_readings if reading.height is not None]

    def _state_lost(self) -> bool:
        """Whether no state packet carrying a height has arrived for ``_STATE_LOST_S`` (or none ever has)."""
        readings = self._height_readings()
        return not readings or time.monotonic() - readings[-1].at > _STATE_LOST_S

    def _airborne_since(self, since: float) -> bool:
        """Whether the newest state packet carrying a height that arrived after ``since`` shows the drone off the ground.

        False when no such packet arrived. Once the state stream is lost,
        this is the last word the drone gave about where it is, so an ``ok``
        read since then does not confirm a land on its own.
        """
        readings = self._height_readings()
        return bool(readings) and readings[-1].at > since and not _shows_ground(readings[-1])

    def _state_shows_flight(self) -> bool:
        """Whether fresh state shows the drone flying: off the ground by ``|h|`` or ``tof``, or its motors running."""
        with self._state_lock:
            readings = list(self._state_readings)
        if not readings or time.monotonic() - readings[-1].at > _STATE_FRESH_S:
            return False
        newest = readings[-1]
        if newest.height is not None and not _shows_ground(newest):
            return True
        return _motors_running(readings)

    def _ground_by_state(self, since: float | None = None, ok_seen: bool = False) -> bool | None:
        """Whether fresh state shows the drone on the ground with its motors off.

        None when there is no fresh height: no state packet has arrived, the
        newest is older than ``_STATE_FRESH_S``, or it carries no height.
        None does not mean the stream is lost: see ``_state_lost``.
        Otherwise True only when all of these hold:

        * The lift guard is over: no takeoff that a land found may still
          lift (see ``_TAKEOFF_LIFT_GUARD_S``).
        * The last ``_GROUND_READINGS`` packets, each arriving within
          ``_STATE_FRESH_S`` of the one before and all after ``since`` (a
          ``time.monotonic()`` value, when given), show ground: ``|h|`` at
          most ``_GROUND_HEIGHT_CM``, and ``tof`` not above
          ``_GROUND_TOF_CM``.
        * The motor time has not changed for ``_MOTOR_OFF_S`` (see
          ``_motors_off``).

        A packet without a usable ``tof`` or motor time cannot prove the
        drone is down, so when either is missing (or ``tof`` is not a
        positive number) the result is ``ok_seen``: the confirming land
        must also have been answered with a fresh ``ok``, the stricter
        reply-based confirmation. False otherwise: fresh state shows the
        drone in the air or its motors running, or not yet on the ground
        for long enough.
        """
        with self._state_lock:
            readings = list(self._state_readings)
        if not readings:
            return None
        newest = readings[-1]
        if newest.height is None or time.monotonic() - newest.at > _STATE_FRESH_S:
            return None
        if self._lift_guard_left() > 0:
            return False
        ground: list[_StateReading] = []
        height_off = False
        for reading in reversed(readings):
            if len(ground) == _GROUND_READINGS:
                break
            if since is not None and reading.at <= since:
                break
            if ground and ground[-1].at - reading.at > _STATE_FRESH_S:
                break
            if not _shows_ground(reading):
                if not (_tof_valid(reading.tof) and reading.tof <= _GROUND_TOF_CM):
                    break
                height_off = True
            ground.append(reading)
        if len(ground) < _GROUND_READINGS:
            return False
        motors_off = _motors_off(readings)
        if motors_off is False:
            return False
        if motors_off is None or height_off or not all(_tof_valid(r.tof) for r in ground):
            return ok_seen
        return True

    def _state_height_text(self) -> str:
        """The newest state height, distance, motor time and age, for log messages."""
        with self._state_lock:
            newest = self._state_readings[-1] if self._state_readings else None
        if newest is None or newest.height is None:
            return "no state height"
        return (
            f"height {newest.height} cm, tof {newest.tof} cm, motor time {newest.motor_s} s "
            f"({time.monotonic() - newest.at:.1f} s ago)"
        )

    def connect(self) -> bool:
        """Connect to the Tello and start video stream."""
        try:
            self._retry_ground_command(self.tello.connect)
            self._set_measured("battery", self.tello.get_battery())
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
        confirmed by fresh state showing the drone on the ground with its
        motors off, or, once the state stream is lost, by a fresh ``ok``
        (see ``_confirm_land``). If this land finds a takeoff that has not
        been answered, or one whose wait ended without an ``ok``, the drone
        may still be spinning up and lift after the land, so ``is_flying``
        is set, nothing confirms the land until ``_TAKEOFF_LIFT_GUARD_S``
        after that takeoff was sent, and no reply alone confirms it until
        ``_TAKEOFF_REPLY_TIMEOUT_S`` after it (this call may then raise, and
        a retry confirms). ``is_flying`` stays true until the land is
        confirmed. The whole call takes at most ``_LAND_DEADLINE_S``.

        Contract (callers such as the lane executor and LAND ALL rely on
        it): returns None once the land is confirmed, and raises
        ``RuntimeError`` when it is not, so callers keep retrying. It never
        returns False.
        """
        deadline = time.monotonic() + _LAND_DEADLINE_S
        with self._wire_lock:
            self._landing = True
            self._land_unconfirmed = True
            self._lands_in_progress += 1
            self._land_seq += 1
            # A takeoff still waiting for its reply, or one whose wait ended
            # on a reply other than "ok" (or none), may still lift and may
            # still be answered "ok" (see _TAKEOFF_LIFT_GUARD_S).
            takeoff_sent_at = self._takeoff_sent_at
            takeoff_may_lift = takeoff_sent_at is not None and (
                self._takeoff_in_flight or not self._takeoff_answered_ok
            )
            if takeoff_may_lift:
                self._lift_guard_until = max(self._lift_guard_until, takeoff_sent_at + _TAKEOFF_LIFT_GUARD_S)
                self._reply_guard_until = max(
                    self._reply_guard_until, takeoff_sent_at + _TAKEOFF_REPLY_TIMEOUT_S
                )
            guarded = self._reply_guard_left() > 0
            if guarded:
                # The drone may be about to lift: until this land is
                # confirmed, callers must treat it as flying and retry.
                self.state.is_flying = True
                self.tello.is_flying = True
        if guarded:
            print(
                f"[{self.name}] Landing follows a takeoff that may still lift: nothing confirms it "
                f"for {self._lift_guard_left():.1f} s, and no reply alone for {self._reply_guard_left():.1f} s."
            )
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
            self._confirm_land(holding_channel=took_channel, deadline=deadline)
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

    def _take_channel_for_land(self, deadline: float) -> bool:
        """Take ``_cmd_lock`` for a confirming land. False if it stays busy or the land is confirmed meanwhile.

        Polls, so the flag that marks the channel as a land's is set in the
        same step that takes it. Waits until ``deadline`` (a
        ``time.monotonic()`` value: the end of the whole ``land()`` call,
        not of one attempt). State alone confirms nothing here: the command
        holding the channel may be a takeoff the Tello has not yet acted on,
        so the drone can still be on the ground and about to lift.
        """
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
        so the keepalive and stream recovery may run again. ``how``, when
        given, is logged.
        """
        with self._wire_lock:
            if not self._land_unconfirmed:
                return False
            self._land_unconfirmed = False
            self.state.is_flying = False
            # djitellopy's end() lands only when its own flag says flying.
            self.tello.is_flying = False
        if how:
            print(f"[{self.name}] Land confirmed: {how}.")
        return True

    def _confirm_land(self, holding_channel: bool, deadline: float) -> None:
        """Send ``land`` until the drone is confirmed on the ground.

        ``holding_channel`` says whether the caller already holds
        ``_cmd_lock`` for this land; the lock is always released before this
        returns. ``deadline`` (a ``time.monotonic()`` value) ends the whole
        ``land()`` call: the waits for the channel, for each reply and for
        the state stream all stop there. Each attempt holds the channel only
        while it sends one ``land`` and waits for the reply
        (``_send_confirming_land``). Any further wait for the state stream
        happens with no lock held, so the state poller and other lands are
        never held up by it.

        A land is confirmed when either of these happens:

        * Fresh state packets show the drone on the ground with its motors
          off (see ``_ground_by_state``), whatever the replies were. This is
          checked while waiting for the reply too, so a drone already on the
          ground that never answers does not hold the channel for the reply
          timeout.
        * The state stream is lost (no packet for ``_STATE_LOST_S``, see
          ``_state_lost``), a fresh ``ok`` answers the confirming land, and
          no packet that arrived after that land was sent showed the drone
          in the air last (the reply-only confirmation used before the state
          stream was read). A gap shorter than ``_STATE_LOST_S`` is waited
          out: a reply read during it may answer an earlier command while
          the drone still hovers.

        Neither applies while the lift guard runs, and the second not while
        the reply guard runs (see ``_TAKEOFF_LIFT_GUARD_S``): the attempt
        waits for them to end instead.

        When the confirming land is answered (``ok``, ``error`` or anything
        else) while fresh state still shows the drone in the air, or while
        the state stream has paused but is not lost, the attempt waits up to
        ``_LAND_GROUND_WAIT_S`` for fresh state to show it on the ground. A
        Tello answers ``error`` to a land that arrives while it is already
        landing, and a late ``ok`` may answer some earlier command, so
        neither reply alone says the drone is down. If the drone is still in
        the air after that wait, or the land got no reply, ``land`` is sent
        again.

        Returns at once, sending nothing, if another land was confirmed
        since this one was requested. Raises ``RuntimeError`` after
        ``_LAND_ATTEMPTS`` unconfirmed attempts or at ``deadline``, leaving
        ``is_flying`` true.
        """
        outcome = "not sent"
        attempt = 0
        try:
            while attempt < _LAND_ATTEMPTS:
                attempt += 1
                if not holding_channel:
                    if not self._take_channel_for_land(deadline):
                        if not self._land_unconfirmed:
                            return
                        raise RuntimeError(f"[{self.name}] land not confirmed: command channel still busy")
                    holding_channel = True
                try:
                    land_attempt = self._send_confirming_land(deadline)
                finally:
                    self._leave_channel_after_land()
                    holding_channel = False
                outcome = land_attempt.outcome
                if land_attempt.verdict == "confirmed":
                    return
                if land_attempt.verdict == "watch":
                    wait_s = min(_LAND_GROUND_WAIT_S, deadline - time.monotonic())
                    if self._wait_for_ground(
                        land_attempt.sent_at, wait_s, land_attempt.ok_seen, land_attempt.ok_alone
                    ):
                        return
                    if self._lift_guard_left() > 0:
                        outcome = (
                            f"{outcome}, but it follows a takeoff that may still lift, so nothing "
                            f"confirms it for another {self._lift_guard_left():.1f} s"
                        )
                    elif self._ground_by_state() is not None:
                        outcome = (
                            f"{outcome}, but fresh state still shows {self._state_height_text()} "
                            f"after {max(wait_s, 0.0):.1f} s"
                        )
                    elif not self._state_lost():
                        outcome = (
                            f"{outcome}, but the state stream paused ({self._state_height_text()}) "
                            f"and did not show the drone on the ground within {max(wait_s, 0.0):.1f} s"
                        )
                    elif self._reply_guard_left() > 0:
                        outcome = (
                            f"{outcome}, but the state stream is lost and the takeoff it follows may "
                            f"still be answered, so no reply confirms it for another "
                            f"{self._reply_guard_left():.1f} s"
                        )
                    else:
                        outcome = (
                            f"{outcome}, but the state stream is lost and last showed "
                            f"{self._state_height_text()}"
                        )
                print(f"[{self.name}] Land attempt {attempt}/{_LAND_ATTEMPTS} not confirmed: {outcome}.")
                if time.monotonic() >= deadline:
                    break
            raise RuntimeError(
                f"[{self.name}] land not confirmed after {attempt} attempt(s) "
                f"within {_LAND_DEADLINE_S:g} s (last: {outcome})"
            )
        finally:
            if holding_channel:
                self._leave_channel_after_land()

    def _send_confirming_land(self, deadline: float) -> _LandAttempt:
        """Send one confirming ``land`` and wait for its reply. Caller holds ``_cmd_lock``.

        Waits out the command gap, then, under ``_wire_lock``, drops every
        queued reply (for example the ``ok`` that answered an interrupting
        ``stop``, which says nothing about the land) and sends ``land``. Then
        waits up to ``_LAND_REPLY_TIMEOUT_S`` (never past ``deadline``) for a
        reply, stopping early if fresh state shows the drone on the ground.

        Returns a ``_LandAttempt``. Its ``verdict`` is ``"confirmed"`` (the
        land is confirmed), ``"watch"`` (the land was answered while fresh
        state shows the drone not yet on the ground or the state stream has
        paused without being lost, or a takeoff guard runs; the caller waits
        for state with no lock held) or ``"failed"``. ``ok_seen`` is whether
        the land was answered ``ok`` outside the lift guard, and
        ``ok_alone`` whether outside the reply guard.
        """
        tello = self.tello
        responses = tello.get_own_udp_object()["responses"]
        self._pace_land()
        with self._wire_lock:
            if not self._land_unconfirmed:
                return _LandAttempt("confirmed", "confirmed by another land", self._land_sent_at, False, False)
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
                max(0.0, min(_LAND_REPLY_TIMEOUT_S, deadline - time.monotonic())),
                stop_waiting=lambda: self._ground_by_state(since=sent_at) is True,
            )
            if reply is not None:
                outcome = f"answered {reply!r}"
        is_ok = reply is not None and reply.lower() == "ok"
        # During the lift guard an "ok" may be the late answer to the
        # interrupting stop or to the takeoff itself, so it counts for
        # nothing. During the reply guard it may still be the takeoff's own
        # answer, so it never confirms a land on its own.
        lift_guarded = self._lift_guard_left() > 0
        ok_seen = is_ok and not lift_guarded
        ok_alone = is_ok and self._reply_guard_left() <= 0
        on_ground = self._ground_by_state(since=sent_at, ok_seen=ok_seen)
        if on_ground:
            self._mark_land_confirmed(f"fresh state shows the drone on the ground ({outcome})")
            return _LandAttempt("confirmed", outcome, sent_at, ok_seen, ok_alone)
        if not self._land_unconfirmed:
            return _LandAttempt("confirmed", outcome, sent_at, ok_seen, ok_alone)
        if lift_guarded:
            return _LandAttempt("watch", outcome, sent_at, ok_seen, ok_alone)
        if on_ground is None and self._state_lost():
            # The state stream is lost: the land's own reply decides, unless
            # the last packet since the land was sent showed the drone in the
            # air, or the takeoff it follows may still be answered.
            if ok_alone and not self._airborne_since(sent_at - _STATE_LOST_S):
                self._mark_land_confirmed(f"the state stream is lost and the land was {outcome}")
                return _LandAttempt("confirmed", outcome, sent_at, ok_seen, ok_alone)
            if self._reply_guard_left() > 0:
                return _LandAttempt("watch", outcome, sent_at, ok_seen, ok_alone)
            return _LandAttempt("failed", outcome, sent_at, ok_seen, ok_alone)
        # Fresh state shows the drone not yet on the ground, or the stream
        # has paused but is not lost: a reply read now may answer an earlier
        # command while the drone still hovers, so the state stream decides.
        return _LandAttempt("watch" if reply is not None else "failed", outcome, sent_at, ok_seen, ok_alone)

    def _wait_for_ground(self, since: float, timeout_s: float, ok_seen: bool = False, ok_alone: bool = False) -> bool:
        """Wait, holding no lock, for fresh state to show the drone on the ground.

        Only state packets that arrived after ``since`` count; ``ok_seen``
        is passed to ``_ground_by_state``. Returns True once the land is
        confirmed (by this wait, another land or the state poller) and False
        if it is not within ``timeout_s``. A pause in the state stream
        shorter than ``_STATE_LOST_S`` is waited out. Once the stream is lost
        and both takeoff guards are over, the land's reply decides at once:
        ``ok_alone`` (the land was answered ``ok`` outside the reply guard)
        confirms it unless the last packet since ``since`` showed the drone
        in the air; otherwise this returns False, so the next land's own
        reply can decide.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if not self._land_unconfirmed:
                return True
            on_ground = self._ground_by_state(since=since, ok_seen=ok_seen)
            if on_ground:
                self._mark_land_confirmed("fresh state shows the drone on the ground")
                return True
            if (
                on_ground is None
                and self._lift_guard_left() <= 0
                and self._reply_guard_left() <= 0
                and self._state_lost()
            ):
                if ok_alone and not self._airborne_since(since - _STATE_LOST_S):
                    self._mark_land_confirmed("the state stream is lost and the land was answered 'ok'")
                    return True
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(_REPLY_POLL_S)

    def move(self, direction: str, distance_cm: int) -> bool | CommandResult:
        """Move in a direction. Drops command if another is in-flight.

        Returns True when the command was sent and acknowledged, and False
        when it was dropped (another flight command is in flight or a
        landing is in progress). An unknown direction sends nothing and
        returns a ``REJECTED`` :class:`CommandResult`. Callers normalize the
        return value with ``command_result()`` and act on ``.ok`` (see
        ``docs/drone-interface.md``). A move that finds only the keepalive on
        the channel waits for it rather than being dropped. The move is sent
        once and never retried; a reply other than ``ok``, or none, raises.
        """
        if direction not in _MOVE_DIRECTIONS:
            return CommandResult("move", CommandOutcome.REJECTED, f"unknown direction {direction!r}")
        if self._landing:
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
        is_takeoff = rearm_after_land is not None
        reply = None
        try:
            with self._wire_lock:
                if abort is not None and abort.is_set():
                    return False
                if is_takeoff:
                    if self._lands_in_progress or self._land_seq != rearm_after_land:
                        return False
                    self._landing = False
                    # A land that starts while this takeoff waits for its
                    # reply, or after a wait that ends without an "ok",
                    # starts the takeoff guards (see _TAKEOFF_LIFT_GUARD_S).
                    self._takeoff_in_flight = True
                    self._takeoff_answered_ok = False
                    self._takeoff_sent_at = time.monotonic()
                    self._lift_guard_until = 0.0
                    self._reply_guard_until = 0.0
                elif self._landing:
                    return False
                # Queued replies belong to earlier exchanges, such as the stop
                # and land of an interrupting landing. Drop them so this command
                # reads its own reply.
                del responses[:]
                tello.send_command_without_return(command)
            reply = self._await_reply(responses, timeout_s)
        finally:
            if is_takeoff:
                with self._wire_lock:
                    if self._takeoff_in_flight:
                        self._takeoff_answered_ok = reply is not None and reply.lower() == "ok"
                    self._takeoff_in_flight = False
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
        self._set_measured("battery", battery)
        return battery

    def read_height_cm(self) -> int | None:
        """Height above takeoff in cm from the latest state packet, or None if unknown."""
        try:
            height = int(self.tello.get_height())
        except Exception:
            return None
        self._set_measured("height", height)
        return height

    def _set_measured(self, field: str, value) -> None:
        """Store a value read from the drone in ``state.<field>`` and stamp when it was read.

        Every write of drone-reported telemetry into ``state`` goes through
        here, so ``snapshot()`` can give each reading its true age.
        """
        setattr(self.state, field, value)
        self._measured_at[field] = time.time()

    # --- Telemetry (TelemetrySource role) ---

    def snapshot(self) -> TelemetrySnapshot:
        """Normalized telemetry from the state the controller has read.

        Each reading keeps the time it was read from the drone (by
        ``connect()``, the state poller or ``read_battery()`` /
        ``read_height_cm()``), so a poll skipped while a flight command holds
        the command lock shows up as a growing ``age_s`` rather than as fresh
        data. A field never read is ``None``. ``is_flying`` follows takeoff
        and confirmed lands, corrected by the poller's zero-height check.
        Reading this sends nothing to the drone.
        """
        s = self.state
        at = self._measured_at
        return TelemetrySnapshot(
            backend="tello",
            drone_id=self.name,
            captured_at=time.time(),
            connected=s.is_connected,
            is_flying=s.is_flying,
            is_flying_source=IS_FLYING_COMMANDED,
            battery_pct=_reading(s.battery, PERCENT, "tello.bat", at.get("battery")),
            height_m=_reading(s.height, METRE, "tello.h", at.get("height"), scale=0.01),
            temperature_c=_reading(s.temperature, CELSIUS, "tello.templ_temph_avg", at.get("temperature")),
            flight_time_s=_reading(s.flight_time, SECOND, "tello.time", at.get("flight_time")),
            **self._extended,
        )

    def _read_extended_state(self, now: float) -> dict[str, Reading]:
        """Read the extra cached state-packet fields for ``snapshot()``. Never raises.

        A field that is missing or not numeric is left out (``None`` in the
        snapshot); it never counts as a poll failure.
        """
        extended: dict[str, Reading] = {}
        for field_name, getter, scale, unit, source in _EXTENDED_FIELDS:
            try:
                raw = getattr(self.tello, getter)()
            except Exception:  # noqa: BLE001 -- optional field; absence is reported as None
                continue
            value = _reading(raw, unit, source, now, scale=scale)
            if value is not None:
                extended[field_name] = value
        vx, vy = extended.get("vx_mps"), extended.get("vy_mps")
        if vx is not None and vy is not None:
            extended["ground_speed_mps"] = Reading(
                value=math.hypot(vx.value, vy.value),
                unit=METRE_PER_SECOND,
                source="derived(tello.vgx,tello.vgy)",
                measured_at=now,
            )
        return extended

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
            self._poll_failures = 0
            while self._running:
                if self._cmd_lock.locked():
                    time.sleep(_STATE_POLL_INTERVAL_S)
                    continue
                self._poll_once()
                time.sleep(_STATE_POLL_INTERVAL_S)

        self._state_thread = threading.Thread(
            target=_poll, daemon=True, name=f"{self.name}-state"
        )
        self._state_thread.start()

    def _poll_once(self) -> None:
        """One state poll: the body of the polling loop.

        Each field is stamped as it is read, so a poll that fails part-way
        still reports the fields it did read with their true age.
        """
        try:
            self._set_measured("battery", self.tello.get_battery())
            self._set_measured("height", self.tello.get_height())
            self._set_measured("temperature", self.tello.get_temperature())
            self._set_measured("flight_time", self.tello.get_flight_time())
            self._poll_failures = 0
            self._extended = self._read_extended_state(time.time())
            self._check_zero_height()
        except Exception:
            self._poll_failures += 1
            if self._poll_failures >= 3 and self.state.is_connected:
                self.state.is_connected = False
                self._running = False
                print(f"[{self.name}] Connection lost — {self._poll_failures} consecutive poll failures.")

    def _check_zero_height(self) -> None:
        """The state poller's zero-height path, run after each successful poll.

        Counts polls in a row that read height 0 while the drone is flying
        or a land is unconfirmed. After ``_ZERO_HEIGHT_POLLS`` of them: if a
        land is unconfirmed and fresh state shows the drone on the ground
        with its motors off, the land is confirmed, which also clears
        ``is_flying`` and lets the keepalive and stream recovery run again.
        Otherwise a drone still marked flying is marked landed and reported
        as a crash, except in two cases, where it stays marked flying (and
        the lane executor keeps retrying an unconfirmed land): while the
        lift guard of an unconfirmed land runs, because the drone may be
        spinning up at height 0 and about to lift; and while fresh state
        shows the drone flying (its motors running, or ``tof`` or ``|h|``
        well above the ground), as for a drone hovering at the height of a
        takeoff point above the ground. A stale height never clears an
        unconfirmed land.
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
        if self._land_unconfirmed and self._lift_guard_left() > 0:
            return
        if self._state_shows_flight():
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
