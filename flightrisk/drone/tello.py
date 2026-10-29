"""Tello drone controller wrapper around DJITelloPy.

Handles connection, video streaming, flight commands, and keepalive.
Standard Tello (TLW004) — SDK 1.3, WiFi AP mode only.

``FakeTelloController`` is an in-process stand-in with the same interface. It
never touches the network, so the edge device can fly a simulated drone
(``python -m flightrisk.edge_device --source fake``) and tests can exercise
flight logic without hardware.
"""

import contextlib
import logging
import math
import threading
import time
import weakref
from collections import deque
from itertools import pairwise
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
    DroneLinkStatus,
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
from flightrisk.obs_log import Throttle, log_event, notice

_log = logging.getLogger(__name__)

#: Repeated Tello failures (frame read, keepalive, state poll) are logged at
#: most once per this many seconds per drone and kind.
_FAILURE_LOG_INTERVAL_S = 10.0
_failure_throttle = Throttle(_FAILURE_LOG_INTERVAL_S)


def _log_failure(drone_id: str, event: str, exc: BaseException, level: int = logging.WARNING, **fields) -> None:
    """Log a Tello failure that used to be silent: type and message, throttled."""
    allowed, suppressed = _failure_throttle.allow((drone_id, event))
    if not allowed:
        return
    if suppressed:
        fields["suppressed"] = suppressed
    log_event(_log, level, event, f"[{drone_id}] {event}: {type(exc).__name__}", category="error", component="tello",
              drone_id=drone_id, error_type=type(exc).__name__, error=str(exc), **fields)

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
# A Tello answers ``takeoff`` only once it hovers, but it also sends late and
# duplicate replies to earlier commands (a non-EDU Tello answers ``sdk?`` and
# ``sn?`` with "unknown command" or "error"). On a real run a takeoff was
# answered "error" 74 ms after it was sent and the drone never lifted, so it
# is not known whether that reply was a stray or a real refusal. A takeoff
# therefore reads its reply as follows (see ``_await_takeoff_reply``); the
# takeoff is never sent a second time:
#
# * "ok" is the takeoff's own answer: it took off.
# * A reply that can never answer a takeoff ("unknown command", or one that
#   is empty or not printable text) is skipped, and the wait goes on.
# * A bare "error" does not decide on its own. For up to _TAKEOFF_CONFIRM_S
#   after it is read (and never past the takeoff's reply timeout) further
#   replies are read and the state stream is watched. A later "ok", or
#   state that shows the drone in the air, means it took off. A later
#   refusal with detail, state that shows it still on the ground with its
#   motors still (checked only from _TAKEOFF_GROUND_AFTER_S after the send,
#   because a Tello that acts on a takeoff lifts within about 2 s), a land
#   that starts, or a window that ends without proof fails the takeoff with
#   the "error" it read, as before.
# * Any other reply, such as "error Motor stop", is a refusal and fails the
#   takeoff at once.
_TAKEOFF_CONFIRM_S = 6.0
_TAKEOFF_GROUND_AFTER_S = 3.0
# A takeoff reply is cut to this many characters in its log event.
_TAKEOFF_REPLY_LOG_LEN = 64
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
# The drone link counts as down for a takeoff (``TelloController.link_status``)
# once no state packet has arrived for this long. It is the same threshold
# landing uses for a lost state stream: about 30 missed packets at 10 a
# second, well past the brief Wi-Fi gaps described above, and state packets
# keep arriving while a command holds the channel, so a drone in the middle
# of a command is never counted as unreachable. A refused takeoff costs the
# operator one retry; a takeoff sent into a dead link cost a land loop for a
# drone that never left the ground (incident of 2026-10-28).
_LINK_STATE_MAX_AGE_S = _STATE_LOST_S
# The link detail text (sent to the ground in nacks and telemetry) is cut to
# this many characters; the protocol allows 200.
_LINK_DETAIL_MAX_LEN = 160
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
# While it waits, fresh state may show that the land did not take: the drone
# is clearly in the air (see ``_clearly_airborne``) and not coming down. A
# Tello busy with a move, or still climbing after a takeoff, answers a land
# ``error`` and ignores it, and a late ``ok`` may answer an earlier command.
# The land is then sent again at once, within the same wait, instead of at
# the end of it, but only once one of these holds:
#
# * _LAND_STALL_S has passed since the land was sent and the height ``h`` is
#   not at least _LAND_DESCENT_CM below the reference height (the higher of
#   ``h`` when ``land()`` started and when this land was sent);
# * ``h`` has reached no new low, by at least _LAND_DESCENT_CM, for
#   _LAND_DESCENT_STALL_S.
#
# Sending ``land`` again never makes a Tello fly, and a resend confirms
# nothing by itself: the same state rules decide. Resends do not add to the
# attempts (_LAND_ATTEMPTS) and do not move the end of the wait, whatever
# they are answered (a resend's reply is waited for only until the wait
# ends), so a land that is never confirmed gives up after about as long as
# before; they only stop a hovering drone from waiting out the rest of
# _LAND_GROUND_WAIT_S before land is sent again. Ground-like or
# stale readings never cause a resend, so a drone already down that waits
# for its motors-off evidence or the lift guard is not sent anything.
_LAND_STALL_S = 1.0
# ... and only once this many state packets in a row (each a new one) show the
# drone no lower than the reference, so one stale or reordered packet cannot
# send land again.
_LAND_STALL_PACKETS = 2
_LAND_DESCENT_STALL_S = 2.0
_LAND_DESCENT_CM = 5
# The state poller reads battery, height and temperature this often.
_STATE_POLL_INTERVAL_S = 2.0
# The poller's zero-height path acts after this many polls in a row at height 0.
_ZERO_HEIGHT_POLLS = 3
# After a confirmed land, the poller treats the land as unconfirmed again once
# this many fresh polls in a row show the motors running with the drone off
# the ground (see ``_guard_flight_after_land``).
_POST_LAND_FLIGHT_POLLS = 2
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
    sent_height: int | None = None  # fresh ``h`` just before the land was sent, or None


def _dropped_busy(command: str) -> CommandResult:
    """The result of a move or rotate dropped because another flight command holds the channel."""
    return CommandResult(command, CommandOutcome.DROPPED_BUSY, "another flight command holds the command channel")


def _note_dropped(drone_id: str, result: CommandResult) -> CommandResult:
    """Log (throttled) a move or rotate dropped because the channel was busy; return ``result``."""
    allowed, suppressed = _failure_throttle.allow((drone_id, "tello_command_dropped", result.command))
    if allowed:
        log_event(_log, logging.WARNING, "tello_command_dropped", f"[{drone_id}] {result.command} dropped: channel busy",
                  category="drone_comm", component="tello", drone_id=drone_id, command=result.command,
                  outcome=result.outcome.value, suppressed=suppressed or None)
    return result


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


def _clearly_airborne(reading: _StateReading) -> bool:
    """Whether one state packet clearly shows the drone off the ground.

    A usable ``tof`` (the distance straight down) decides: above
    ``_GROUND_TOF_CM`` is in the air. Without one, ``|h|`` above
    ``_GROUND_HEIGHT_CM`` is. A packet that is ground-like by either, such
    as a drone resting on a ledge above its takeoff point, is not clearly
    airborne. Only used to decide whether to send a land again sooner,
    never to confirm one.
    """
    if reading.height is None:
        return False
    if _tof_valid(reading.tof):
        return reading.tof > _GROUND_TOF_CM
    return abs(reading.height) > _GROUND_HEIGHT_CM


def _lifted(reading: _StateReading) -> bool:
    """Whether one state packet shows a drone that has climbed off its takeoff point.

    Stricter than ``_clearly_airborne``, because it is used to count a
    takeoff as done: ``h`` must be above ``_GROUND_HEIGHT_CM`` (a takeoff
    climbs, so ``h`` goes up), and a usable ``tof`` must be above
    ``_GROUND_TOF_CM`` as well.
    """
    if reading.height is None or reading.height <= _GROUND_HEIGHT_CM:
        return False
    return not (_tof_valid(reading.tof) and reading.tof <= _GROUND_TOF_CM)


def _stray_reply(reply: str) -> bool:
    """Whether a reply can never be the answer to a takeoff: "unknown command", empty, or not printable text."""
    if not reply or reply.lower() == "unknown command":
        return True
    return "�" in reply or not reply.isprintable()


def _log_text(reply: str | None) -> str | None:
    """A reply as it may appear in a log event: printable characters only, cut to ``_TAKEOFF_REPLY_LOG_LEN``."""
    if reply is None:
        return None
    text = "".join(ch if ch.isprintable() and ch != "�" else "?" for ch in reply)
    return text[:_TAKEOFF_REPLY_LOG_LEN]


def _reference_height(start_height: int | None, sent_height: int | None) -> int | None:
    """The height a land is measured against: the higher of ``h`` when ``land()`` started and when the land was sent.

    The higher one, so a descent that an earlier land (such as the
    immediate land that interrupts a move) began still counts as coming
    down. None when neither is known.
    """
    heights = [h for h in (start_height, sent_height) if h is not None]
    return max(heights) if heights else None


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
                    # one. Without recorded packets a land waits for the
                    # stream to count as lost and then falls back to its
                    # reply, so the first failure is logged; later ones are
                    # not, so a lasting fault does not flood the log ten
                    # times a second.
                    if not self._failure_logged:
                        self._failure_logged = True
                        # A dead stdout must not stop the state thread either.
                        with contextlib.suppress(Exception):
                            notice(
                                _log, "tello_state_record_failed",
                                f"[{self._name}] Recording a state packet failed ({e!r}); "
                                "further failures are not logged.",
                                level=logging.WARNING, drone_id=self._name, error_type=type(e).__name__,
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

    def __init__(
        self,
        name: str = "drone",
        host: str | None = None,
        local_ip: str | None = None,
        ifindex: int | None = None,
        *,
        binding=None,
        socket_factory=None,
    ):
        """Build the controller. Nothing is sent to the drone until ``connect()``.

        Without ``local_ip`` (the default) the stock djitellopy client is
        used, exactly as before. With ``local_ip`` (``--tello-local-ip``)
        every Tello socket is bound to that adapter's IP through
        ``flightrisk.drone.tello_bound.BoundTello``, so two controllers on
        two adapters can each fly a Tello at the same address (see
        docs/two-tellos-one-pc.md); ``ifindex`` (Windows only) also pins
        outgoing commands to that interface. ``binding`` (a
        ``TelloBinding``, which then replaces ``local_ip`` and ``ifindex``)
        and ``socket_factory`` are seams for tests. Raises
        ``TelloBindingError`` when the binding cannot work.
        """
        if host is None:
            host = get_config().drone.tello_default_host
        self.name = name
        self.host = host
        # The adapter IP every Tello socket is bound to, or None for the
        # stock djitellopy client.
        self.local_ip: str | None = None
        if local_ip is None and binding is None:
            # retry_count=1: djitellopy sends every command once. A retried move
            # could fly again after a landing interrupted it.
            self.tello = Tello(host=host, retry_count=1)
        else:
            self.tello = self._bound_client(host, local_ip, ifindex, binding, socket_factory)
            self.local_ip = self.tello.binding.local_ip
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
        # Armed when a land is confirmed, cleared by the next takeoff: the
        # state poller then watches for motors that keep running with the
        # drone off the ground (see _check_flight_after_land).
        # _post_land_flight_polls counts the fresh polls in a row that did.
        self._post_land_watch = False
        self._post_land_flight_polls = 0
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
        # Drone-link bookkeeping for link_status(), guarded by _link_lock:
        # the error of the last command-channel exchange (keepalive, stream
        # command or flight command) that raised or got no reply, or None
        # once an exchange was answered since. Landing does not touch it.
        # _state_arrivals is True when state packet arrival times are
        # recorded (see _stamp_state_packets); without them the state age is
        # unknown and does not gate a takeoff.
        self._link_lock = threading.Lock()
        self._command_error: str | None = None
        self._state_arrivals = False
        self._stamp_state_packets(host)

    def _bound_client(self, host: str, local_ip: str | None, ifindex: int | None, binding, socket_factory):
        """The Tello client for ``local_ip`` mode: every socket bound to one adapter (see ``__init__``)."""
        from flightrisk.drone.tello_bound import BoundTello, TelloBinding, check_binding

        if binding is None:
            binding = TelloBinding(local_ip=local_ip, ifindex=ifindex)
        if socket_factory is None:
            check_binding(binding, host)
            return BoundTello(host, binding, retry_count=1, name=self.name)
        check_binding(binding, host, socket_factory=socket_factory)
        return BoundTello(host, binding, retry_count=1, name=self.name, socket_factory=socket_factory)

    def _stamp_state_packets(self, host: str) -> None:
        """Have djitellopy report each state packet for ``host`` to ``_note_state_packet``."""
        if self.local_ip is not None:
            self._stamp_bound_state_packets()
            return
        try:
            udp = _djitello.drones.get(host)
            if type(udp) is dict:
                _djitello.drones[host] = _StateStampedUdpObject(udp, self._note_state_packet, self.name)
                self._state_arrivals = True
        except Exception as e:
            # No arrival times: landing falls back to reply-based confirmation.
            notice(
                _log, "tello_state_arrivals_unavailable",
                f"[{self.name}] Could not record state packet arrivals ({e!r}); "
                "lands are confirmed by replies once the state stream counts as lost.",
                level=logging.WARNING, drone_id=self.name, error_type=type(e).__name__,
            )

    def _stamp_bound_state_packets(self) -> None:
        """``_stamp_state_packets`` for a bound client: wrap its own per-drone table, not djitellopy's."""
        try:
            udp = self.tello.get_own_udp_object()
            if type(udp) is dict:
                self.tello.replace_udp_object(_StateStampedUdpObject(udp, self._note_state_packet, self.name))
                self._state_arrivals = True
        except Exception as e:  # noqa: BLE001 - as _stamp_state_packets: never stop the controller over this
            notice(
                _log, "tello_state_arrivals_unavailable",
                f"[{self.name}] Could not record state packet arrivals ({e!r}); "
                "lands are confirmed by replies once the state stream counts as lost.",
                level=logging.WARNING, drone_id=self.name, error_type=type(e).__name__,
            )

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

    def _fresh_reading(self) -> _StateReading | None:
        """The newest state packet carrying a height, or None if there is none or it is older than ``_STATE_FRESH_S``."""
        readings = self._height_readings()
        if not readings or time.monotonic() - readings[-1].at > _STATE_FRESH_S:
            return None
        return readings[-1]

    def _fresh_height(self) -> int | None:
        """The ``h`` of ``_fresh_reading()``, or None."""
        reading = self._fresh_reading()
        return None if reading is None else reading.height

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

    # --- Drone link health (read-only) ---

    def link_status(self) -> DroneLinkStatus:
        """Whether this controller can talk to the Tello right now. Sends nothing and never blocks on a command.

        Unhealthy (``ok`` false), in this order, when:

        * the controller is not connected (``not_connected``);
        * no state packet has ever arrived (``no_state``), or none for more
          than ``_LINK_STATE_MAX_AGE_S`` (``state_stale``). The Tello sends
          state about ten times a second whatever the command channel is
          doing, so this does not depend on the state poller, which skips
          while a command holds the channel;
        * the last command-channel exchange (keepalive, stream command or
          flight command) raised, for example ``OSError`` 51 (network
          unreachable), or got no reply, and no exchange has been answered
          since (``command_failed``). The keepalive runs every
          ``_KEEPALIVE_INTERVAL_S``, so a recovered link clears this within
          that time.

        ``age_s`` is the age of the newest state packet (rounded to 0.1 s),
        or ``None`` when unknown. When state arrival times cannot be
        recorded (``_stamp_state_packets`` failed) the state age is unknown
        and only the other checks apply.

        With a local IP (``--tello-local-ip``) an unhealthy ``detail`` ends
        with ``via <local_ip>``, so the operator sees which adapter's drone
        is unreachable.
        """
        status = self._link_status()
        local_ip = getattr(self, "local_ip", None)
        if local_ip is None or status.ok:
            return status
        detail = f"{status.detail} via {local_ip}"
        if len(detail) > _LINK_DETAIL_MAX_LEN:
            suffix = f"... via {local_ip}"
            detail = status.detail[: _LINK_DETAIL_MAX_LEN - len(suffix)] + suffix
        return DroneLinkStatus(status.ok, status.age_s, status.reason, detail)

    def _link_status(self) -> DroneLinkStatus:
        """``link_status`` without the adapter suffix."""
        if not self.state.is_connected:
            return DroneLinkStatus(False, None, "not_connected", "Tello not connected")
        age: float | None = None
        if self._state_arrivals:
            with self._state_lock:
                newest = self._state_readings[-1].at if self._state_readings else None
            if newest is None:
                return DroneLinkStatus(False, None, "no_state", "Tello unreachable (no state received)")
            age = max(0.0, time.monotonic() - newest)
            if age > _LINK_STATE_MAX_AGE_S:
                return DroneLinkStatus(
                    False, round(age, 1), "state_stale", f"Tello unreachable (no state for {age:.0f} s)"
                )
            age = round(age, 1)
        with self._link_lock:
            error = self._command_error
        if error is not None:
            detail = f"Tello unreachable (last command failed: {error})"
            if len(detail) > _LINK_DETAIL_MAX_LEN:
                detail = detail[: _LINK_DETAIL_MAX_LEN - 4] + "...)"
            return DroneLinkStatus(False, age, "command_failed", detail)
        return DroneLinkStatus(True, age)

    def _note_command_outcome(self, error: BaseException | str | None) -> None:
        """Record how the last command-channel exchange ended: ``None`` when it was answered."""
        if isinstance(error, BaseException):
            error = f"{type(error).__name__}: {error}"
        if error is not None:
            # Printable text only: it travels to the ground in nacks and telemetry.
            error = "".join(ch if ch.isprintable() else " " for ch in str(error))
        with self._link_lock:
            self._command_error = error

    def connect(self) -> bool:
        """Connect to the Tello and start video stream."""
        try:
            self._retry_ground_command(self.tello.connect)
            self._set_measured("battery", self.tello.get_battery())
            self.state.is_connected = True
            notice(_log, "tello_connected", f"[{self.name}] Connected. Battery: {self.state.battery}%",
                   drone_id=self.name, battery_pct=self.state.battery)

            self._retry_ground_command(self.tello.streamon)
            self._running = True
            self._start_keepalive()
            self._start_state_polling()
            return True
        except Exception as e:
            notice(_log, "tello_connect_failed", f"[{self.name}] Connection failed: {e}", level=logging.ERROR,
                   drone_id=self.name, error_type=type(e).__name__)
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
                notice(_log, "tello_land_before_disconnect_failed", f"[{self.name}] Land before disconnect failed: {e}",
                       level=logging.ERROR, drone_id=self.name, error_type=type(e).__name__)
        with self._cmd_lock:
            try:
                self.tello.end()
            except Exception:
                pass
        self.state.is_connected = False
        self.state.is_flying = False
        notice(_log, "tello_disconnected", f"[{self.name}] Disconnected.", drone_id=self.name)

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
                        notice(_log, "tello_frozen_frame",
                               f"[{self.name}] Frozen frame detected ({self._frozen_count} identical), recovering...",
                               level=logging.WARNING, drone_id=self.name, identical_frames=self._frozen_count)
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
        except Exception as e:
            _log_failure(self.name, "tello_get_frame_failed", e)
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
            notice(_log, "tello_stream_stale", f"[{self.name}] Video stream stale, restarting...",
                   level=logging.WARNING, drone_id=self.name)
            try:
                try:
                    if not self._stream_command_when_free("streamoff"):
                        notice(_log, "tello_stream_recovery_skipped",
                               f"[{self.name}] Stream recovery skipped: the command channel stayed busy.",
                               level=logging.WARNING, drone_id=self.name, reason="channel_busy")
                        return
                except Exception:
                    # A failed streamoff does not stop the streamon.
                    pass
                time.sleep(_STREAM_RESTART_PAUSE_S)
                try:
                    if self._stream_command_when_free("streamon"):
                        notice(_log, "tello_stream_restarted", f"[{self.name}] Video stream restarted.", drone_id=self.name)
                    else:
                        notice(_log, "tello_stream_recovery_failed",
                               f"[{self.name}] Stream recovery failed: the command channel stayed busy.",
                               level=logging.WARNING, drone_id=self.name, reason="channel_busy")
                except Exception as e:
                    notice(_log, "tello_stream_recovery_failed", f"[{self.name}] Stream recovery failed: {e}",
                           level=logging.WARNING, drone_id=self.name, error_type=type(e).__name__)
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
                try:
                    tello.send_command_without_return(command)
                except Exception as e:
                    self._note_command_outcome(e)
                    raise
            reply = self._await_reply(responses, _STREAM_REPLY_TIMEOUT_S)
            if reply is None:
                self._note_command_outcome(f"no reply to '{command}' within {_STREAM_REPLY_TIMEOUT_S:g} s")
                raise RuntimeError(
                    f"[{self.name}] no reply to '{command}' within {_STREAM_REPLY_TIMEOUT_S:g} s"
                )
            # Any reply proves the link works.
            self._note_command_outcome(None)
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
        it in flight and interrupts it. The takeoff is sent once and never
        again. A reply that can never answer a takeoff is skipped, and a
        bare ``error`` is checked against further replies and the state
        stream before it fails the takeoff (see ``_TAKEOFF_CONFIRM_S``). Any
        other reply than ``ok``, a bare ``error`` that is not shown wrong,
        or no reply raises ``RuntimeError``.
        """
        with self._wire_lock:
            land_seq = self._land_seq
            landing_now = self._lands_in_progress > 0
        if landing_now:
            notice(_log, "tello_takeoff_refused", f"[{self.name}] Takeoff refused: a landing is in progress.",
                   level=logging.WARNING, drone_id=self.name, reason="landing")
            return False
        with self._cmd_lock:
            if not self._send_flight_command(
                "takeoff", _TAKEOFF_REPLY_TIMEOUT_S, abort=abort, rearm_after_land=land_seq
            ):
                notice(_log, "tello_takeoff_refused",
                       f"[{self.name}] Takeoff refused: a landing is in progress or was requested.",
                       level=logging.WARNING, drone_id=self.name, reason="landing_or_abort")
                return False
            with self._wire_lock:
                # The drone is flying under command again, so an earlier land
                # that was never confirmed no longer holds off the keepalive.
                if self._land_seq == land_seq:
                    self._land_unconfirmed = False
            self.state.is_flying = True
            # djitellopy's end() lands only when its own flag says flying.
            self.tello.is_flying = True
            notice(_log, "tello_takeoff", f"[{self.name}] Takeoff.", drone_id=self.name)
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
        confirmed. While the land waits for state, a land that fresh state
        shows did not take (the drone clearly in the air and not coming
        down) is sent again at once rather than at the end of the wait (see
        ``_LAND_STALL_S``); that confirms nothing by itself. The whole call
        takes at most ``_LAND_DEADLINE_S``.

        Contract (callers such as the lane executor and LAND ALL rely on
        it): returns None once the land is confirmed, and raises
        ``RuntimeError`` when it is not, so callers keep retrying. It never
        returns False.
        """
        deadline = time.monotonic() + _LAND_DEADLINE_S
        # The height when this land started, for telling a descent from a
        # hover (see _LAND_STALL_S).
        start_height = self._fresh_height()
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
            notice(
                _log, "tello_land_guarded",
                f"[{self.name}] Landing follows a takeoff that may still lift: nothing confirms it "
                f"for {self._lift_guard_left():.1f} s, and no reply alone for {self._reply_guard_left():.1f} s.",
                level=logging.WARNING, drone_id=self.name,
            )
        try:
            with self._owner_lock:
                took_channel = self._cmd_lock.acquire(blocking=False)
                if took_channel:
                    self._land_on_channel = True
                behind_land = not took_channel and self._land_on_channel
            if not took_channel:
                if behind_land:
                    notice(_log, "tello_land_waiting", f"[{self.name}] Landing: another land is confirming, waiting for it.",
                           drone_id=self.name)
                else:
                    notice(_log, "tello_land_interrupting", f"[{self.name}] Landing now (interrupting an in-flight command).",
                           level=logging.WARNING, drone_id=self.name)
                    self._send_land_raw("stop")
                    self._pace_land()
                    self._send_land_raw("land")
            self._confirm_land(holding_channel=took_channel, deadline=deadline, start_height=start_height)
            notice(_log, "tello_land_done", f"[{self.name}] Landing." if took_channel else f"[{self.name}] Landing confirmed.",
                   drone_id=self.name, took_channel=took_channel)
        finally:
            with self._wire_lock:
                self._lands_in_progress -= 1

    def _send_land_raw(self, command: str) -> None:
        """Send ``stop`` or ``land`` for an interrupting land, without reading a reply."""
        try:
            self.tello.send_command_without_return(command)
        except Exception as e:
            notice(_log, "tello_immediate_command_failed", f"[{self.name}] Immediate '{command}' failed: {e}",
                   level=logging.ERROR, drone_id=self.name, command=command, error_type=type(e).__name__)
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
            self._post_land_watch = True
            self._post_land_flight_polls = 0
        if how:
            notice(_log, "tello_land_confirmed", f"[{self.name}] Land confirmed: {how}.", drone_id=self.name, how=how)
        return True

    def _confirm_land(self, holding_channel: bool, deadline: float, start_height: int | None = None) -> None:
        """Send ``land`` until the drone is confirmed on the ground.

        ``holding_channel`` says whether the caller already holds
        ``_cmd_lock`` for this land; the lock is always released before this
        returns. ``deadline`` (a ``time.monotonic()`` value) ends the whole
        ``land()`` call: the waits for the channel, for each reply and for
        the state stream all stop there. ``start_height`` is the fresh state
        height when ``land()`` started, or None. Each attempt holds the channel only
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
        again. During that wait, once fresh state shows the land did not take
        (the drone clearly in the air and not coming down, see
        ``_LAND_STALL_S``), ``land`` is sent again at once, as part of the
        same attempt and within the same wait. That land is checked like any
        confirming land while it waits for its reply; after that, the rest
        of the wait counts state packets from the attempt's first land on,
        as before, and an ``ok`` to any land of the attempt counts as that
        attempt's ``ok``.

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
                    watch_until = time.monotonic() + wait_s
                    # State packets count from the attempt's first land on
                    # (``since``), as they did before lands were sent again;
                    # a land sent again only adds its "ok", if any, and
                    # restarts the stall timing.
                    since = land_attempt.sent_at
                    while True:
                        confirmed, not_taking = self._wait_for_ground(
                            since,
                            watch_until - time.monotonic(),
                            land_attempt.ok_seen,
                            land_attempt.ok_alone,
                            ref_height=_reference_height(start_height, land_attempt.sent_height),
                            land_sent_at=land_attempt.sent_at,
                        )
                        if confirmed:
                            return
                        if not not_taking:
                            break
                        if not self._take_channel_for_land(watch_until):
                            if not self._land_unconfirmed:
                                return
                            break
                        holding_channel = True
                        try:
                            # Another land() may have sent a land since: they
                            # share the one drone, so one land per
                            # _LAND_STALL_S is enough. Wait for that land
                            # instead, and measure the stall from it.
                            with self._wire_lock:
                                last_sent_at = self._land_sent_at
                            if time.monotonic() - last_sent_at < _LAND_STALL_S:
                                skipped = True
                                land_attempt = land_attempt._replace(sent_at=max(land_attempt.sent_at, last_sent_at))
                                resent = None
                            else:
                                skipped = False
                                notice(
                                    _log, "tello_land_not_taking",
                                    f"[{self.name}] Land attempt {attempt}/{_LAND_ATTEMPTS} ({outcome}) did not take: "
                                    f"{not_taking}; sending land again.",
                                    level=logging.WARNING, drone_id=self.name, attempt=attempt,
                                    attempts=_LAND_ATTEMPTS,
                                )
                                # Its reply is waited for only until the wait ends.
                                resent = self._send_confirming_land(min(deadline, watch_until))
                        finally:
                            self._leave_channel_after_land()
                            holding_channel = False
                        if skipped:
                            continue
                        if resent.verdict == "confirmed":
                            return
                        # Whatever its reply (or none), the attempt keeps
                        # waiting until watch_until, as it would have
                        # without the land sent again.
                        outcome = resent.outcome
                        land_attempt = land_attempt._replace(
                            outcome=resent.outcome,
                            sent_at=resent.sent_at,
                            sent_height=resent.sent_height,
                            ok_seen=land_attempt.ok_seen or resent.ok_seen,
                            ok_alone=land_attempt.ok_alone or resent.ok_alone,
                        )
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
                notice(_log, "tello_land_unconfirmed",
                       f"[{self.name}] Land attempt {attempt}/{_LAND_ATTEMPTS} not confirmed: {outcome}.",
                       level=logging.ERROR, drone_id=self.name, attempt=attempt, attempts=_LAND_ATTEMPTS)
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
        ``ok_alone`` whether outside the reply guard. ``sent_height`` is the
        fresh state height just before the land was sent, or None.
        """
        tello = self.tello
        responses = tello.get_own_udp_object()["responses"]
        self._pace_land()
        sent_height = self._fresh_height()
        with self._wire_lock:
            if not self._land_unconfirmed:
                return _LandAttempt(
                    "confirmed", "confirmed by another land", self._land_sent_at, False, False, sent_height
                )
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
            return _LandAttempt("confirmed", outcome, sent_at, ok_seen, ok_alone, sent_height)
        if not self._land_unconfirmed:
            return _LandAttempt("confirmed", outcome, sent_at, ok_seen, ok_alone, sent_height)
        if lift_guarded:
            return _LandAttempt("watch", outcome, sent_at, ok_seen, ok_alone, sent_height)
        if on_ground is None and self._state_lost():
            # The state stream is lost: the land's own reply decides, unless
            # the last packet since the land was sent showed the drone in the
            # air, or the takeoff it follows may still be answered.
            if ok_alone and not self._airborne_since(sent_at - _STATE_LOST_S):
                self._mark_land_confirmed(f"the state stream is lost and the land was {outcome}")
                return _LandAttempt("confirmed", outcome, sent_at, ok_seen, ok_alone, sent_height)
            if self._reply_guard_left() > 0:
                return _LandAttempt("watch", outcome, sent_at, ok_seen, ok_alone, sent_height)
            return _LandAttempt("failed", outcome, sent_at, ok_seen, ok_alone, sent_height)
        # Fresh state shows the drone not yet on the ground, or the stream
        # has paused but is not lost: a reply read now may answer an earlier
        # command while the drone still hovers, so the state stream decides.
        return _LandAttempt("watch" if reply is not None else "failed", outcome, sent_at, ok_seen, ok_alone, sent_height)

    def _wait_for_ground(
        self,
        since: float,
        timeout_s: float,
        ok_seen: bool = False,
        ok_alone: bool = False,
        ref_height: int | None = None,
        land_sent_at: float | None = None,
    ) -> tuple[bool, str]:
        """Wait, holding no lock, for fresh state to show the drone on the ground.

        Only state packets that arrived after ``since`` count; ``ok_seen``
        is passed to ``_ground_by_state``. Returns ``(True, "")`` once the
        land is confirmed (by this wait, another land or the state poller)
        and ``(False, "")`` if it is not within ``timeout_s``. A pause in the
        state stream shorter than ``_STATE_LOST_S`` is waited out. Once the
        stream is lost and both takeoff guards are over, the land's reply
        decides at once: ``ok_alone`` (the land was answered ``ok`` outside
        the reply guard) confirms it unless the last packet since ``since``
        showed the drone in the air; otherwise this returns ``(False, "")``,
        so the next land's own reply can decide.

        Returns ``(False, why)`` early, confirming nothing, when fresh state
        shows the land did not take (see ``_LAND_STALL_S``): the newest fresh
        packet is clearly airborne and ``h`` is not coming down, measured
        from ``land_sent_at`` (when the latest land was sent; ``since`` if
        None) against ``ref_height`` (the first clearly airborne fresh
        height stands in when None). The caller then sends land again.
        """
        deadline = time.monotonic() + timeout_s
        stall_from = since if land_sent_at is None else land_sent_at
        lowest = ref_height
        lowest_at = stall_from
        # When the reference height was first known, and how many packets in a
        # row (each a new one) were no lower than it. With no reference at the
        # start, the first airborne packet becomes the reference, and the stall
        # is measured from then, not from the land.
        ref_set_at = stall_from
        flat_packets = 0
        last_packet_at = None
        while True:
            if not self._land_unconfirmed:
                return True, ""
            on_ground = self._ground_by_state(since=since, ok_seen=ok_seen)
            if on_ground:
                self._mark_land_confirmed("fresh state shows the drone on the ground")
                return True, ""
            if (
                on_ground is None
                and self._lift_guard_left() <= 0
                and self._reply_guard_left() <= 0
                and self._state_lost()
            ):
                if ok_alone and not self._airborne_since(since - _STATE_LOST_S):
                    self._mark_land_confirmed("the state stream is lost and the land was answered 'ok'")
                    return True, ""
                return False, ""
            now = time.monotonic()
            if now >= deadline:
                return False, ""
            reading = self._fresh_reading()
            if reading is None or not _clearly_airborne(reading):
                flat_packets = 0
                last_packet_at = None
            else:
                height = reading.height
                if ref_height is None:
                    ref_height = lowest = height
                    lowest_at = ref_set_at = now
                elif height <= lowest - _LAND_DESCENT_CM:
                    lowest, lowest_at = height, now
                if reading.at != last_packet_at:
                    last_packet_at = reading.at
                    flat_packets = flat_packets + 1 if height > ref_height - _LAND_DESCENT_CM else 0
                if (
                    now - max(stall_from, ref_set_at) >= _LAND_STALL_S
                    and flat_packets >= _LAND_STALL_PACKETS
                ):
                    return False, (
                        f"fresh state shows {self._state_height_text()}, no lower than the "
                        f"{ref_height} cm when the land was sent, {now - stall_from:.1f} s after it"
                    )
                if now - lowest_at >= _LAND_DESCENT_STALL_S:
                    return False, (
                        f"fresh state shows {self._state_height_text()}, "
                        f"no lower for {now - lowest_at:.1f} s"
                    )
            time.sleep(_REPLY_POLL_S)

    def move(self, direction: str, distance_cm: int) -> bool | CommandResult:
        """Move in a direction. Drops command if another is in-flight.

        Returns True when the command was sent and acknowledged, and False
        when a landing is in progress (or starts before the move is sent).
        When another flight command holds the channel the move is dropped
        and a ``DROPPED_BUSY`` :class:`CommandResult` is returned; an
        unknown direction sends nothing and returns a ``REJECTED`` one. Both
        are falsy, like False. Callers normalize the return value with
        ``command_result()`` and act on ``.ok`` (see
        ``docs/drone-interface.md``). A move that finds only the keepalive on
        the channel waits for it rather than being dropped. The move is sent
        once and never retried; a reply other than ``ok``, or none, raises.
        """
        if direction not in _MOVE_DIRECTIONS:
            return CommandResult("move", CommandOutcome.REJECTED, f"unknown direction {direction!r}")
        if self._landing:
            return False
        if not self._acquire_for_flight():
            return _note_dropped(self.name, _dropped_busy("move"))
        try:
            distance_cm = max(20, min(500, int(distance_cm)))
            return self._send_flight_command(f"{direction} {distance_cm}", _COMMAND_REPLY_TIMEOUT_S)
        finally:
            self._cmd_lock.release()

    def rotate(self, degrees: int) -> bool | CommandResult:
        """Rotate clockwise/counter-clockwise. Drops if another command is in-flight.

        Returns True when the command was sent and acknowledged, False when
        a landing is in progress, and a ``DROPPED_BUSY``
        :class:`CommandResult` when another flight command holds the
        channel. Like ``move()``, it is sent once and never retried.
        """
        if self._landing:
            return False
        if not self._acquire_for_flight():
            return _note_dropped(self.name, _dropped_busy("rotate"))
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
        A takeoff's reply is read by ``_await_takeoff_reply``, which skips
        stray replies and checks a bare ``error`` against the state stream.
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
                    self._post_land_watch = False
                    self._post_land_flight_polls = 0
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
                try:
                    tello.send_command_without_return(command)
                except Exception as e:
                    # For example OSError 51 (network unreachable): the
                    # drone link is down (see link_status).
                    self._note_command_outcome(e)
                    raise
            if is_takeoff:
                reply, accepted = self._await_takeoff_reply(responses, timeout_s, rearm_after_land)
            else:
                reply = self._await_reply(responses, timeout_s)
                accepted = reply is not None and reply.lower() == "ok"
        finally:
            if is_takeoff:
                with self._wire_lock:
                    if self._takeoff_in_flight:
                        # Only the Tello's own "ok" counts here. A takeoff
                        # that state showed in the air without one keeps the
                        # land guards, because that "ok" may still arrive.
                        self._takeoff_answered_ok = reply is not None and reply.lower() == "ok"
                    self._takeoff_in_flight = False
        # Any reply proves the link works; none means it may not.
        self._note_command_outcome(None if reply is not None else f"no reply to '{command}' within {timeout_s:g} s")
        if reply is None:
            raise RuntimeError(
                f"[{self.name}] no reply to '{command}' within {timeout_s:g} s (not retried)"
            )
        if not accepted:
            raise RuntimeError(f"[{self.name}] '{command}' answered {reply!r} (not retried)")
        return True

    def _await_takeoff_reply(
        self, responses: list, timeout_s: float, land_seq: int
    ) -> tuple[str | None, bool]:
        """Read the reply to the takeoff just sent and decide whether it took off (see ``_TAKEOFF_CONFIRM_S``).

        Caller holds ``_cmd_lock``; ``land_seq`` is the land count when the
        takeoff was sent. Returns the reply that decides (None when no reply
        that could answer a takeoff arrived within ``timeout_s`` of the
        send) and whether the takeoff counts as done. Sends nothing, so the
        takeoff is never sent twice. Every reply read is logged as a
        ``tello_takeoff_reply`` event with its text and its age in ms.
        """
        sent_at = self._takeoff_sent_at
        if sent_at is None:
            sent_at = time.monotonic()
        deadline = sent_at + timeout_s
        unconfirmed: str | None = None  # the bare "error" being confirmed
        confirm_until = deadline

        def landing() -> bool:
            with self._wire_lock:
                return self._lands_in_progress > 0 or self._land_seq != land_seq

        def decided() -> bool:
            return landing() or self._takeoff_state_verdict(sent_at) is not None

        while True:
            end = min(deadline, confirm_until)
            reply = self._await_reply(
                responses,
                max(0.0, end - time.monotonic()),
                stop_waiting=decided if unconfirmed is not None else None,
            )
            if reply is None:
                if unconfirmed is None:
                    return None, False
                return unconfirmed, self._confirm_takeoff_error(unconfirmed, sent_at, landing())
            age_ms = round((time.monotonic() - sent_at) * 1000)
            if unconfirmed is not None and landing():
                # Replies read once a land has started may be the land's
                # own (the interrupting stop's "ok"), so none is taken.
                self._log_takeoff_reply(reply, age_ms, "ignored_landing")
                return unconfirmed, self._confirm_takeoff_error(unconfirmed, sent_at, True)
            if reply.lower() == "ok":
                self._log_takeoff_reply(reply, age_ms, "accepted")
                return reply, True
            if _stray_reply(reply):
                self._log_takeoff_reply(reply, age_ms, "skipped_stray")
                continue
            if reply.lower() == "error":
                self._log_takeoff_reply(reply, age_ms, "unconfirmed_error")
                if unconfirmed is None:
                    unconfirmed = reply
                    confirm_until = time.monotonic() + _TAKEOFF_CONFIRM_S
                continue
            self._log_takeoff_reply(reply, age_ms, "refused")
            return reply, False

    def _confirm_takeoff_error(self, reply: str, sent_at: float, landing: bool) -> bool:
        """Decide a takeoff answered only with a bare "error": True when fresh state shows the drone in the air.

        Logs the outcome. A land that has started, state that shows the
        drone on the ground, or no proof either way all fail the takeoff.
        """
        verdict = None if landing else self._takeoff_state_verdict(sent_at)
        took_off = verdict == "airborne"
        if landing:
            outcome = "landing"
        else:
            outcome = verdict or "no_proof"
        age_s = time.monotonic() - sent_at
        if took_off:
            text = (
                f"[{self.name}] Takeoff answered {_log_text(reply)!r}, but state shows the drone in the air "
                f"({self._state_height_text()}): counted as a takeoff."
            )
        else:
            text = (
                f"[{self.name}] Takeoff answered {_log_text(reply)!r} and not confirmed after {age_s:.1f} s "
                f"({outcome}; {self._state_height_text()})."
            )
        notice(
            _log, "tello_takeoff_error_checked", text, level=logging.WARNING, drone_id=self.name,
            reply=_log_text(reply), outcome=outcome, took_off=took_off, since_send_ms=round(age_s * 1000),
        )
        return took_off

    def _takeoff_state_verdict(self, sent_at: float) -> str | None:
        """What fresh state received since the takeoff was sent shows: ``"airborne"``, ``"ground"`` or None.

        Looks at the last ``_GROUND_READINGS`` state packets carrying a
        height that arrived after ``sent_at``, each within
        ``_STATE_FRESH_S`` of the one before, the newest still fresh.
        ``"airborne"`` when every one shows the drone climbed off its
        takeoff point (see ``_lifted``). ``"ground"`` only from
        ``_TAKEOFF_GROUND_AFTER_S`` after the send, when every one shows
        ground (see ``_shows_ground``) and the motor time has not changed
        since just before the send. None otherwise, including when state is
        missing, stale or mixed.
        """
        with self._state_lock:
            readings = [r for r in self._state_readings if r.height is not None]
        after = [r for r in readings if r.at > sent_at]
        now = time.monotonic()
        if len(after) < _GROUND_READINGS or now - after[-1].at > _STATE_FRESH_S:
            return None
        recent = after[-_GROUND_READINGS:]
        if any(later.at - earlier.at > _STATE_FRESH_S for earlier, later in pairwise(recent)):
            return None
        if all(_lifted(r) for r in recent):
            return "airborne"
        if now - sent_at < _TAKEOFF_GROUND_AFTER_S or not all(_shows_ground(r) for r in recent):
            return None
        before = [r for r in readings if r.at <= sent_at][-1:]
        motor_times = {r.motor_s for r in before + after if r.motor_s is not None}
        if len(motor_times) > 1:
            # The motors ran after the takeoff was sent: it may still lift.
            return None
        return "ground"

    def _log_takeoff_reply(self, reply: str, age_ms: int, verdict: str) -> None:
        """Log one reply read during a takeoff with its text, its age since the send and what was made of it."""
        log_event(
            _log, logging.INFO, "tello_takeoff_reply",
            f"[{self.name}] takeoff reply {_log_text(reply)!r} after {age_ms} ms: {verdict}",
            category="drone_comm", component="tello", drone_id=self.name,
            reply=_log_text(reply), reply_age_ms=age_ms, verdict=verdict,
        )

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
            except Exception:  # noqa: BLE001, S112 -- optional field; absence is reported as None
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
            self._note_command_outcome(None)
            return True
        except Exception as e:
            # Unanswered, answered with an error, or the send itself raised
            # (OSError 51 when this laptop is not on the Tello's Wi-Fi): the
            # drone link counts as down until an exchange is answered.
            self._note_command_outcome(e)
            _log_failure(self.name, "tello_keepalive_failed", e)
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
            self._guard_flight_after_land()
        except Exception as e:
            self._poll_failures += 1
            _log_failure(self.name, "tello_poll_failed", e, consecutive=self._poll_failures)
            if self._poll_failures >= 3 and self.state.is_connected:
                self.state.is_connected = False
                self._running = False
                notice(_log, "tello_link_lost",
                       f"[{self.name}] Connection lost — {self._poll_failures} consecutive poll failures.",
                       level=logging.ERROR, drone_id=self.name, poll_failures=self._poll_failures)

    def _guard_flight_after_land(self) -> None:
        """Flag a drone that is flying after a land was confirmed, so the land is retried.

        A land can be confirmed wrongly: with the state stream lost, a stale
        ``ok`` (the answer to a move, say) is read as the land's reply. When
        the stream returns, the drone may still hover with its motors
        running while ``is_flying`` is false, and the keepalive would hold
        off the Tello's 15 s auto-land. Once a land was confirmed and no
        takeoff has been sent since, this counts fresh polls in a row whose
        state shows the motor time advancing (see ``_motors_running``) with
        the drone off the ground. After ``_POST_LAND_FLIGHT_POLLS`` of them
        it sets ``_land_unconfirmed`` and ``is_flying`` again and logs once,
        and the lane executor's monitor retries the land. It never sends a
        command itself. The motor time, not the height, decides, so a drone
        carried by hand with its motors off is never flagged.
        """
        with self._state_lock:
            readings = list(self._state_readings)
        with self._wire_lock:
            if not self._post_land_watch or self._land_unconfirmed:
                self._post_land_flight_polls = 0
                return
            fresh = bool(readings) and time.monotonic() - readings[-1].at <= _STATE_FRESH_S
            if not (fresh and _motors_running(readings) and not _shows_ground(readings[-1])):
                self._post_land_flight_polls = 0
                return
            self._post_land_flight_polls += 1
            if self._post_land_flight_polls < _POST_LAND_FLIGHT_POLLS:
                return
            polls = self._post_land_flight_polls
            self._post_land_watch = False
            self._post_land_flight_polls = 0
            self._land_unconfirmed = True
            self.state.is_flying = True
            self.tello.is_flying = True
        notice(
            _log, "tello_flying_after_land",
            f"[{self.name}] State shows the drone flying (motors running, {self._state_height_text()}) "
            f"after a land was confirmed, in {polls} polls in a row: the land is treated as unconfirmed "
            "and will be retried.",
            level=logging.ERROR, drone_id=self.name, polls=polls,
        )

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
            notice(_log, "tello_crash_suspected", f"[{self.name}] Crash detected — height 0 for {polls} polls.",
                   level=logging.ERROR, drone_id=self.name, polls=polls)


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

    This is the edge device's ``--source fake``. The dashboard's simulated
    drone is a different class, ``FakeDrone`` in
    :mod:`flightrisk.drone.fake`, registered as the ``sim`` backend. This one
    satisfies the ``DroneController`` protocol and has no ``snapshot()``, so
    ``read_telemetry`` adapts its ``state`` cache.
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
        notice(_log, "tello_connected", f"[{self.name}] Fake drone connected. Battery: {self.state.battery}%",
               drone_id=self.name, battery_pct=self.state.battery, fake=True)
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
        # Clamped to -100..100 like djitellopy's send_rc_control, so the fake
        # records what a real Tello would be sent.
        self._record("rc", *(max(-100, min(100, int(v))) for v in (lr, fb, ud, yaw)))

    def read_battery(self) -> int | None:
        return self.state.battery

    def read_height_cm(self) -> int | None:
        return self.state.height
