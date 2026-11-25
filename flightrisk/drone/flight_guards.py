"""Guards around the Tello's altitude moves, its late takeoff replies and its transient refusals.

Incident of 2026-11-21 (drone-2, one standard Tello, takeoff from a desk about
122 cm above the floor, altitude slot 20 cm): the takeoff ``ok`` came 8.8 s
after the takeoff was sent, the climb read the height once right after it
(``h`` 100 cm, while the drone was really 30 to 40 cm above the desk) and sent
``down 80`` about 100 ms later. The Tello answered ``error No valid imu``, and
the drone drifted off the desk edge and came down to about 30 cm above the
floor. The same stale read after a late takeoff ``ok`` sent a Tello up 110 cm
on 2026-10-28. The lane executor now:

* waits for a settled height after the takeoff reply before any move to the
  altitude slot (``TelloController.settled_height``): ``settle_samples``
  fresh state packets, all received after the wait began, steady within
  ``settle_tolerance_cm``, within ``settle_timeout_s``. On a timeout nothing
  is sent (the Tello hovers on its own) and the drone lands;
* bounds every descent to the slot by the downward distance (``tof``): the
  drone must keep ``DESCENT_CLEARANCE_CM`` above whatever is below it. A
  descent that would go further, or one with no usable distance, lands
  instead, so the drone never flies its lanes at another height than its slot;
* lands instead of climbing when the takeoff ``ok`` came later than
  ``takeoff_late_s`` after the takeoff was sent;
* retries a move the Tello refused with one of ``TRANSIENT_REFUSALS`` at most
  ``refusal_retries`` times, after a hover of ``refusal_hold_s``: a move to
  the slot is computed again from a new settled height (never resent with
  its old distance, which the drone may have partly flown), and a sensed
  lane step goes back through its sense gate. Every other move lands as
  before. The refusal that takes a flight past ``refusals_per_flight``
  (with the default of 2, the third refusal of the flight) lands, whatever
  the step, and a refused lane step counts as possibly flown for the geofence
  (the envelope's fence shrinks by its length for the rest of the flight), so
  the dead-reckoning error cannot grow past the fence. Each refusal is
  logged and counted in telemetry;
* with ``hover_test``, takes off and hovers: no move to the slot, no lane,
  and no manual step, nudge or ``resume_search`` way back.

Only a controller with ``settled_height`` (the Tello) is settled, bounded by
``tof`` and held to ``takeoff_late_s``; the others keep their single height
read. Only a controller that raises ``CommandRefusedError`` (the Tello) can
have a refusal retried.
"""
from __future__ import annotations

from dataclasses import dataclass

from flightrisk.drone.safety import MIN_ALT_SLOT_CM
from flightrisk.fleet_protocol import MAX_TEXT_LEN
from flightrisk.params import table as _registry
from flightrisk.params.defaults import model_default

__all__ = [
    "DEFAULT_REFUSALS_PER_FLIGHT",
    "DEFAULT_REFUSAL_HOLD_S",
    "DEFAULT_REFUSAL_RETRIES",
    "DEFAULT_SETTLE_SAMPLES",
    "DEFAULT_SETTLE_TIMEOUT_S",
    "DEFAULT_SETTLE_TOLERANCE_CM",
    "DEFAULT_TAKEOFF_LATE_S",
    "DESCENT_CLEARANCE_CM",
    "ENV_HOVER_TEST",
    "ENV_REFUSALS_PER_FLIGHT",
    "ENV_REFUSAL_HOLD_S",
    "ENV_REFUSAL_RETRIES",
    "ENV_SETTLE_SAMPLES",
    "ENV_SETTLE_TIMEOUT_S",
    "ENV_SETTLE_TOLERANCE_CM",
    "ENV_TAKEOFF_LATE_S",
    "EVENT_GUARD_SETTING_NON_DEFAULT",
    "HOVER_TEST_DETAIL",
    "REFUSALS_PER_FLIGHT_RANGE",
    "REFUSAL_HOLD_RANGE_S",
    "REFUSAL_RETRIES_RANGE",
    "SETTLE_SAMPLES_RANGE",
    "SETTLE_TIMEOUT_RANGE_S",
    "SETTLE_TOLERANCE_RANGE_CM",
    "TAKEOFF_LATE_RANGE_S",
    "TRANSIENT_REFUSALS",
    "FlightGuards",
    "reply_text",
    "transient_refusal",
]

ENV_SETTLE_SAMPLES = "FLIGHTRISK_TELLO_SETTLE_SAMPLES"
ENV_SETTLE_TOLERANCE_CM = "FLIGHTRISK_TELLO_SETTLE_TOLERANCE_CM"
ENV_SETTLE_TIMEOUT_S = "FLIGHTRISK_TELLO_SETTLE_TIMEOUT_S"
ENV_TAKEOFF_LATE_S = "FLIGHTRISK_TELLO_TAKEOFF_LATE_S"
ENV_REFUSAL_RETRIES = "FLIGHTRISK_TELLO_REFUSAL_RETRIES"
ENV_REFUSAL_HOLD_S = "FLIGHTRISK_TELLO_REFUSAL_HOLD_S"
ENV_REFUSALS_PER_FLIGHT = "FLIGHTRISK_TELLO_REFUSALS_PER_FLIGHT"
ENV_HOVER_TEST = "FLIGHTRISK_HOVER_TEST"


# The Tello sends about ten state packets a second, so five is about half a
# second of steady readings.
DEFAULT_SETTLE_SAMPLES = model_default("takeoff.settle_samples", _registry.T)
SETTLE_SAMPLES_RANGE = (2, 50)
# Expect a few cm of error in the Tello's own height (docs/ceiling-and-altitude.md).
DEFAULT_SETTLE_TOLERANCE_CM = model_default("takeoff.settle_tolerance_cm", _registry.T)
SETTLE_TOLERANCE_RANGE_CM = (2.0, 50.0)
# Long enough for a hovering Tello to show it, short enough that a drone that
# never settles is landed soon. The Tello lands by itself after 15 s without
# a command; the edge's keepalive covers that, and this stays well below it.
DEFAULT_SETTLE_TIMEOUT_S = model_default("takeoff.settle_timeout_s", _registry.T)
SETTLE_TIMEOUT_RANGE_S = (1.0, 10.0)
# A Tello lifts within about 2 s of a takeoff and hovers within about 5 s
# (flightrisk/drone/tello.py). With two edges on one laptop the ``ok`` has
# taken 4.7 to 6.7 s, 8.8 s three times, and once 16 s. A settled height
# covers a late reply up to this; beyond it, so much of the takeoff went
# unobserved that the drone lands instead of climbing. Set it lower (for
# example 3) for a first test flight.
DEFAULT_TAKEOFF_LATE_S = model_default("takeoff.late_s", _registry.T)
TAKEOFF_LATE_RANGE_S = (2.0, 20.0)
# Each retry costs one hover of the hold time plus one reply wait in the air,
# with a drone that may not hold its position well, so they stay few.
DEFAULT_REFUSAL_RETRIES = model_default("takeoff.refusal_retries", _registry.T)
REFUSAL_RETRIES_RANGE = (0, 3)
DEFAULT_REFUSAL_HOLD_S = model_default("takeoff.refusal_hold_s", _registry.T)
REFUSAL_HOLD_RANGE_S = (0.5, 5.0)
# A flight retries at most this many refusals in all, across its steps: a
# drone that refuses every step (drone-2 on 2026-11-22) lands rather than
# retry each one. Reset at every takeoff.
DEFAULT_REFUSALS_PER_FLIGHT = model_default("tello.refusals_per_flight", _registry.T)
REFUSALS_PER_FLIGHT_RANGE = (0, 6)

#: Logged once per flight guard setting that is not its default, at startup.
EVENT_GUARD_SETTING_NON_DEFAULT = "edge_flight_guard_setting_non_default"
#: Operator text of a nudge, manual step or way back refused in the hover-only test.
HOVER_TEST_DETAIL = "hover-only test (FLIGHTRISK_HOVER_TEST=1): this drone only hovers; land it to end the test"

# The lowest a descent to the slot may bring the drone above whatever its
# downward distance sensor sees: the room profile's lowest altitude slot
# (``safety.MIN_ALT_SLOT_CM``), the Vision Positioning System's lower limit
# of 30 cm plus 20 cm for height error and overshoot.
DESCENT_CLEARANCE_CM = MIN_ALT_SLOT_CM

# Refusals the Tello gives for a move it may accept a moment later. Neither
# is in the Tello SDK documentation. Community reports link "No valid imu" to
# an IMU that needs calibrating, poor light or a floor without texture, and
# "Not joystick" to a drone that is not ready for the command yet. A drone
# that keeps giving them needs attention, which is why each one is counted
# in telemetry rather than retried silently.
TRANSIENT_REFUSALS = ("error no valid imu", "error not joystick")


def reply_text(value: object) -> str | None:
    """*value* as printable text with its whitespace collapsed, at most ``MAX_TEXT_LEN`` characters, or None.

    A drone's reply goes into telemetry and logs this way only, so a reply
    with control characters or odd spacing cannot make a message invalid.
    """
    if value is None:
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())
    return text[:MAX_TEXT_LEN] or None


def transient_refusal(reply: object) -> bool:
    """Whether *reply* is one of ``TRANSIENT_REFUSALS`` (case and surrounding spaces ignored)."""
    if not isinstance(reply, str):
        return False
    text = " ".join(reply.split()).lower()
    return any(text.startswith(prefix) for prefix in TRANSIENT_REFUSALS)


@dataclass(frozen=True)
class FlightGuards:
    """The guard settings one lane executor uses (see the module docstring)."""

    settle_samples: int = DEFAULT_SETTLE_SAMPLES
    settle_tolerance_cm: float = DEFAULT_SETTLE_TOLERANCE_CM
    settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S
    takeoff_late_s: float = DEFAULT_TAKEOFF_LATE_S
    refusal_retries: int = DEFAULT_REFUSAL_RETRIES
    refusal_hold_s: float = DEFAULT_REFUSAL_HOLD_S
    refusals_per_flight: int = DEFAULT_REFUSALS_PER_FLIGHT
    hover_test: bool = False

    def __post_init__(self) -> None:
        checks = (
            ("settle_samples", self.settle_samples, SETTLE_SAMPLES_RANGE),
            ("settle_tolerance_cm", self.settle_tolerance_cm, SETTLE_TOLERANCE_RANGE_CM),
            ("settle_timeout_s", self.settle_timeout_s, SETTLE_TIMEOUT_RANGE_S),
            ("takeoff_late_s", self.takeoff_late_s, TAKEOFF_LATE_RANGE_S),
            ("refusal_retries", self.refusal_retries, REFUSAL_RETRIES_RANGE),
            ("refusal_hold_s", self.refusal_hold_s, REFUSAL_HOLD_RANGE_S),
            ("refusals_per_flight", self.refusals_per_flight, REFUSALS_PER_FLIGHT_RANGE),
        )
        for name, value, (lo, hi) in checks:
            # One ValueError for a wrong type or range, as the envelope's own checks raise.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                raise ValueError(f"{name} must be from {lo:g} to {hi:g}, got {value!r}")
        for name in ("settle_samples", "refusal_retries", "refusals_per_flight"):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be a whole number, got {getattr(self, name)!r}")  # noqa: TRY004
