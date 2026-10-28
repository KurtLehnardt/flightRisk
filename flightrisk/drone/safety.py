"""Edge-side flight safety envelope for relative-move Tello flights.

The Tello has no GPS, so the edge tracks the drone's position by dead
reckoning: every commanded relative move updates a pose relative to the
point the drone took off from on its current flight (the takeoff point).
Every takeoff from the ground re-establishes that origin (``reset_pose``,
called by the lane executor), because a Tello lands where it is and the
edge cannot observe whether the drone was carried back to its start spot
before the next flight. The geofence is therefore always a square around the
spot the drone actually took off from, never around the spot an earlier
flight started from. The envelope uses that pose and a few configured limits
to decide what the edge may do:

* Soft geofence: ``check_move`` refuses a horizontal move whose dead-reckoned
  endpoint would leave a square of half-width ``geofence_cm`` around the
  takeoff point. Lanes may not move vertically at all; altitude changes come
  only from the altitude-slot correction, which keeps the two drones in a
  fleet at separate heights.
* Altitude slot: after takeoff the drone climbs (or descends) from its
  measured takeoff height to ``alt_slot_cm`` (``slot_move``).
* Ceiling: ``ceiling_cm`` is the room's ceiling height above the takeoff
  surface and ``ceiling_margin_cm`` the clearance kept below it. No altitude
  slot may be above ``ceiling_cm - ceiling_margin_cm`` (``max_alt_slot_cm``):
  the envelope refuses such a slot (``CeilingError``) wherever it comes
  from, ``climb_allowed`` refuses a climb that would end above that limit,
  and ``above_ceiling`` reports a measured height above ``ceiling_cm``, on
  which the executor lands. ``configure_ceiling`` changes the ceiling, the
  margin and the slot together; the executor calls it only on the ground.
  See ``docs/ceiling-and-altitude.md``.
* Battery gates: ``can_takeoff`` is false below ``min_takeoff_battery`` and
  ``should_land`` is true below ``land_battery``. An unknown battery level
  never permits a takeoff.
* Link-loss watchdog: ``link_watchdog`` records the time of the last ground
  message; ``link_lost`` reports when it is older than ``link_loss_land_s``.

Design reference: hackathon design section 10. The limits can be overridden
with the environment variables ``FLIGHTRISK_GEOFENCE_CM``,
``FLIGHTRISK_MIN_TAKEOFF_BATTERY``, ``FLIGHTRISK_LINK_LOSS_LAND_S``,
``FLIGHTRISK_CEILING_CM`` and ``FLIGHTRISK_CEILING_MARGIN_CM`` through
``SafetyEnvelope.from_env``.

Frame convention: ``x_cm`` is to the right of the takeoff heading, ``y_cm`` is
ahead along it, and ``heading_deg`` is measured clockwise from it.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping

__all__ = [
    "CEILING_CM_ENV",
    "CEILING_MARGIN_CM_ENV",
    "CeilingError",
    "DEFAULT_ALT_SLOT_CM",
    "DEFAULT_CEILING_CM",
    "DEFAULT_CEILING_MARGIN_CM",
    "DEFAULT_GEOFENCE_CM",
    "DEFAULT_LAND_BATTERY",
    "DEFAULT_LINK_LOSS_LAND_S",
    "DEFAULT_MIN_TAKEOFF_BATTERY",
    "format_cm",
    "GEOFENCE_CM_ENV",
    "HORIZONTAL_DIRECTIONS",
    "is_finite_number",
    "LINK_LOSS_LAND_S_ENV",
    "LinkWatchdog",
    "MAX_ALT_SLOT_CM",
    "MAX_CEILING_CM",
    "MAX_MOVE_CM",
    "MIN_ALT_SLOT_CM",
    "MIN_CEILING_CM",
    "MIN_CEILING_MARGIN_CM",
    "MIN_MOVE_CM",
    "MIN_TAKEOFF_BATTERY_ENV",
    "RelativePose",
    "SafetyEnvelope",
    "slot_above_ceiling",
    "validate_ceiling",
]

LINK_LOSS_LAND_S_ENV = "FLIGHTRISK_LINK_LOSS_LAND_S"
MIN_TAKEOFF_BATTERY_ENV = "FLIGHTRISK_MIN_TAKEOFF_BATTERY"
GEOFENCE_CM_ENV = "FLIGHTRISK_GEOFENCE_CM"
CEILING_CM_ENV = "FLIGHTRISK_CEILING_CM"
CEILING_MARGIN_CM_ENV = "FLIGHTRISK_CEILING_MARGIN_CM"

DEFAULT_LINK_LOSS_LAND_S = 3.0
DEFAULT_MIN_TAKEOFF_BATTERY = 30
DEFAULT_LAND_BATTERY = 15
DEFAULT_GEOFENCE_CM = 500
DEFAULT_ALT_SLOT_CM = 100

# The Tello SDK accepts relative moves between 20 cm and 500 cm. Shorter or
# longer requests are clamped by the controller, which would make the
# dead-reckoned position wrong, so the envelope refuses them instead.
MIN_MOVE_CM = 20
MAX_MOVE_CM = 500

# Altitude slots must be reachable with a single relative move from the
# ground and must leave room for the minimum move size.
MIN_ALT_SLOT_CM = MIN_MOVE_CM
MAX_ALT_SLOT_CM = MAX_MOVE_CM

# Ceiling (docs/ceiling-and-altitude.md). Heights are the Tello's own reading
# above the surface it took off from, so the ceiling is measured from that
# surface too. The default effective maximum slot is 200 - 60 = 140 cm. On
# 2026-10-28 a Tello sent to a 160 cm slot hit the ceiling, so the default is
# deliberately below that, and a lower room must be configured explicitly.
DEFAULT_CEILING_CM = 200
DEFAULT_CEILING_MARGIN_CM = 60
# The Tello's takeoff alone hovers at about 80 cm and can overshoot, so a
# room lower than this is not safe to fly in at all.
MIN_CEILING_CM = 150
# Far above any room; with it the 500 cm slot limit is the binding one.
MAX_CEILING_CM = 1000
# The clearance below the ceiling never drops below this: it covers the
# Tello's height error and its overshoot at the end of a climb.
MIN_CEILING_MARGIN_CM = 30

HORIZONTAL_DIRECTIONS = ("forward", "back", "left", "right")

# Unit vectors (right, ahead) for each horizontal direction relative to the
# drone's current heading.
_DIRECTION_VECTORS: dict[str, tuple[float, float]] = {
    "forward": (0.0, 1.0),
    "back": (0.0, -1.0),
    "right": (1.0, 0.0),
    "left": (-1.0, 0.0),
}


def is_finite_number(value: object) -> bool:
    """Return True for a finite int or float (booleans are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


class CeilingError(ValueError):
    """An altitude slot, or a climb, would end above the ceiling limit (ceiling minus margin)."""


def format_cm(value: float) -> str:
    """A height for messages: ``140`` rather than ``140.0``, one decimal otherwise."""
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def validate_ceiling(ceiling_cm: object, margin_cm: object) -> tuple[float, float]:
    """Check a ceiling and its safety margin; return them as floats.

    The ceiling must be within ``MIN_CEILING_CM``-``MAX_CEILING_CM``, the
    margin at least ``MIN_CEILING_MARGIN_CM``, and the effective maximum slot
    (ceiling minus margin) at least the lowest altitude slot. Raises
    ``ValueError`` with an operator-facing message otherwise.
    """
    if not is_finite_number(ceiling_cm) or not MIN_CEILING_CM <= ceiling_cm <= MAX_CEILING_CM:
        raise ValueError(f"ceiling height must be within {MIN_CEILING_CM}-{MAX_CEILING_CM} cm, got {ceiling_cm!r}")
    if not is_finite_number(margin_cm) or margin_cm < MIN_CEILING_MARGIN_CM:
        raise ValueError(f"ceiling margin must be at least {MIN_CEILING_MARGIN_CM} cm, got {margin_cm!r}")
    if ceiling_cm - margin_cm < MIN_ALT_SLOT_CM:
        raise ValueError(
            f"ceiling {format_cm(ceiling_cm)} cm minus margin {format_cm(margin_cm)} cm leaves "
            f"{format_cm(ceiling_cm - margin_cm)} cm, below the lowest altitude slot {MIN_ALT_SLOT_CM} cm"
        )
    return float(ceiling_cm), float(margin_cm)


def slot_above_ceiling(alt_slot_cm: float, ceiling_cm: float, margin_cm: float) -> str | None:
    """Why *alt_slot_cm* is above the ceiling limit, or None when it is not.

    The limit is ``ceiling_cm - margin_cm``; a slot exactly at it is allowed.
    """
    limit = ceiling_cm - margin_cm
    if alt_slot_cm <= limit:
        return None
    return (
        f"slot {format_cm(alt_slot_cm)} cm is above the ceiling limit {format_cm(limit)} cm "
        f"(ceiling {format_cm(ceiling_cm)} cm minus margin {format_cm(margin_cm)} cm)"
    )


@dataclass
class RelativePose:
    """Dead-reckoned position relative to the takeoff point."""

    x_cm: float = 0.0
    y_cm: float = 0.0
    heading_deg: float = 0.0

    def endpoint(self, direction: str, distance_cm: float) -> tuple[float, float]:
        """Return the ``(x_cm, y_cm)`` reached by a horizontal move.

        Raises ``ValueError`` for a direction that is not horizontal.
        """
        if direction not in _DIRECTION_VECTORS:
            raise ValueError(f"not a horizontal direction: {direction!r}")
        right, ahead = _DIRECTION_VECTORS[direction]
        rad = math.radians(self.heading_deg)
        # Rotate the body-frame vector clockwise by the heading.
        dx = right * math.cos(rad) + ahead * math.sin(rad)
        dy = -right * math.sin(rad) + ahead * math.cos(rad)
        return self.x_cm + dx * distance_cm, self.y_cm + dy * distance_cm

    def apply_move(self, direction: str, distance_cm: float) -> None:
        """Advance the pose by a completed horizontal move."""
        self.x_cm, self.y_cm = self.endpoint(direction, distance_cm)

    def apply_rotation(self, degrees: float) -> None:
        """Turn the pose clockwise by ``degrees`` (negative turns counter-clockwise)."""
        self.heading_deg = (self.heading_deg + degrees) % 360.0


class LinkWatchdog:
    """Tracks the age of the last message received from the ground station."""

    def __init__(self, timeout_s: float, clock: Callable[[], float] = time.monotonic):
        if not is_finite_number(timeout_s) or timeout_s <= 0:
            raise ValueError(f"link-loss timeout must be a positive number, got {timeout_s!r}")
        self.timeout_s = float(timeout_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_s = clock()

    def feed(self) -> None:
        """Record that a ground message arrived just now."""
        now = self._clock()
        with self._lock:
            self._last_s = now

    def age_s(self) -> float:
        """Seconds since the last ground message (or since construction)."""
        now = self._clock()
        with self._lock:
            return max(0.0, now - self._last_s)

    def expired(self) -> bool:
        """True once no ground message has arrived for ``timeout_s`` seconds."""
        return self.age_s() >= self.timeout_s


class SafetyEnvelope:
    """Geofence, altitude slot and ceiling, battery gates and link watchdog for one drone."""

    def __init__(
        self,
        geofence_cm: float = DEFAULT_GEOFENCE_CM,
        alt_slot_cm: float = DEFAULT_ALT_SLOT_CM,
        min_takeoff_battery: float = DEFAULT_MIN_TAKEOFF_BATTERY,
        land_battery: float = DEFAULT_LAND_BATTERY,
        link_loss_land_s: float = DEFAULT_LINK_LOSS_LAND_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        ceiling_cm: float = DEFAULT_CEILING_CM,
        ceiling_margin_cm: float = DEFAULT_CEILING_MARGIN_CM,
    ):
        if not is_finite_number(geofence_cm) or geofence_cm <= 0:
            raise ValueError(f"geofence_cm must be a positive number, got {geofence_cm!r}")
        if not is_finite_number(min_takeoff_battery) or not 0 <= min_takeoff_battery <= 100:
            raise ValueError(f"min_takeoff_battery must be within 0-100, got {min_takeoff_battery!r}")
        if not is_finite_number(land_battery) or not 0 <= land_battery <= 100:
            raise ValueError(f"land_battery must be within 0-100, got {land_battery!r}")
        if land_battery > min_takeoff_battery:
            raise ValueError("land_battery must not exceed min_takeoff_battery")
        self.geofence_cm = float(geofence_cm)
        self.min_takeoff_battery = float(min_takeoff_battery)
        self.land_battery = float(land_battery)
        self._lock = threading.Lock()
        self.ceiling_cm, self.ceiling_margin_cm = validate_ceiling(ceiling_cm, ceiling_margin_cm)
        self.alt_slot_cm = self.check_alt_slot(alt_slot_cm)
        self.pose = RelativePose()
        self.link_watchdog = LinkWatchdog(link_loss_land_s, clock=clock)

    @classmethod
    def from_env(
        cls,
        alt_slot_cm: float = DEFAULT_ALT_SLOT_CM,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> "SafetyEnvelope":
        """Build an envelope with the design defaults and environment overrides."""
        env = os.environ if environ is None else environ

        def _read(name: str, default: float) -> float:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be a number, got {raw!r}") from exc

        return cls(
            geofence_cm=_read(GEOFENCE_CM_ENV, DEFAULT_GEOFENCE_CM),
            alt_slot_cm=alt_slot_cm,
            min_takeoff_battery=_read(MIN_TAKEOFF_BATTERY_ENV, DEFAULT_MIN_TAKEOFF_BATTERY),
            land_battery=DEFAULT_LAND_BATTERY,
            link_loss_land_s=_read(LINK_LOSS_LAND_S_ENV, DEFAULT_LINK_LOSS_LAND_S),
            clock=clock,
            ceiling_cm=_read(CEILING_CM_ENV, DEFAULT_CEILING_CM),
            ceiling_margin_cm=_read(CEILING_MARGIN_CM_ENV, DEFAULT_CEILING_MARGIN_CM),
        )

    # --- Altitude slot ---

    @staticmethod
    def validate_alt_slot(alt_slot_cm: float) -> float:
        if not is_finite_number(alt_slot_cm) or not MIN_ALT_SLOT_CM <= alt_slot_cm <= MAX_ALT_SLOT_CM:
            raise ValueError(
                f"altitude slot must be within {MIN_ALT_SLOT_CM}-{MAX_ALT_SLOT_CM} cm, got {alt_slot_cm!r}"
            )
        return float(alt_slot_cm)

    @property
    def max_alt_slot_cm(self) -> float:
        """The highest altitude slot allowed: the ceiling minus its margin."""
        return self.ceiling_cm - self.ceiling_margin_cm

    def check_alt_slot(self, alt_slot_cm: float) -> float:
        """Validate a slot against the slot range and this envelope's ceiling; return it as a float.

        Raises ``ValueError`` for a slot outside the slot range and
        ``CeilingError`` (a ``ValueError``) for one above the ceiling limit.
        """
        slot = self.validate_alt_slot(alt_slot_cm)
        reason = slot_above_ceiling(slot, self.ceiling_cm, self.ceiling_margin_cm)
        if reason is not None:
            raise CeilingError(f"altitude {reason}")
        return slot

    def set_alt_slot(self, alt_slot_cm: float) -> None:
        """Change the altitude slot.

        Raises ``ValueError`` for an out-of-range slot and ``CeilingError``
        for one above the ceiling limit; the slot is then unchanged.
        """
        with self._lock:
            self.alt_slot_cm = self.check_alt_slot(alt_slot_cm)

    def configure_ceiling(self, ceiling_cm: float, margin_cm: float, alt_slot_cm: float) -> None:
        """Replace the ceiling, the margin and the altitude slot together.

        All three are checked first (``validate_ceiling``, the slot range,
        and the slot against the new limit); on any error nothing changes.
        Call this only while the drone is on the ground: the executor does.
        """
        ceiling, margin = validate_ceiling(ceiling_cm, margin_cm)
        slot = self.validate_alt_slot(alt_slot_cm)
        reason = slot_above_ceiling(slot, ceiling, margin)
        if reason is not None:
            raise CeilingError(f"altitude {reason}")
        with self._lock:
            self.ceiling_cm, self.ceiling_margin_cm, self.alt_slot_cm = ceiling, margin, slot

    def ceiling_status(self) -> dict[str, float]:
        """The active ``ceiling_cm``, ``ceiling_margin_cm`` and ``alt_slot_cm``, read together."""
        with self._lock:
            return {
                "ceiling_cm": self.ceiling_cm,
                "ceiling_margin_cm": self.ceiling_margin_cm,
                "alt_slot_cm": self.alt_slot_cm,
            }

    def climb_allowed(self, height_cm: float | None, distance_cm: float) -> bool:
        """True if climbing *distance_cm* from the measured *height_cm* stays at or below the limit.

        The limit is ``max_alt_slot_cm``. An unknown height never allows a
        climb.
        """
        if not is_finite_number(height_cm) or not is_finite_number(distance_cm):
            return False
        return height_cm + distance_cm <= self.max_alt_slot_cm

    def above_ceiling(self, height_cm: float | None) -> bool:
        """True when a measured height is above the ceiling itself (not just the slot limit)."""
        return is_finite_number(height_cm) and height_cm > self.ceiling_cm

    def slot_move(self, height_cm: float | None) -> tuple[str, int] | None:
        """Return the ``(direction, distance_cm)`` move that reaches the slot.

        Returns ``None`` when the drone is already within the minimum move
        size of its slot. Raises ``ValueError`` when the height is unknown or
        not positive, because the climb cannot be computed safely. Raises
        ``CeilingError`` when the slot is above the ceiling limit; the
        caller must also check a climb with ``climb_allowed`` before it is
        flown.
        """
        if not is_finite_number(height_cm) or height_cm <= 0:
            raise ValueError(f"height is unknown, cannot reach the altitude slot (got {height_cm!r})")
        reason = slot_above_ceiling(self.alt_slot_cm, self.ceiling_cm, self.ceiling_margin_cm)
        if reason is not None:
            raise CeilingError(f"altitude {reason}")
        delta = int(round(self.alt_slot_cm - height_cm))
        if abs(delta) < MIN_MOVE_CM:
            return None
        direction = "up" if delta > 0 else "down"
        return direction, min(abs(delta), MAX_MOVE_CM)

    # --- Geofence ---

    def check_move(self, direction: str, distance_cm: float) -> bool:
        """Return True if a lane move may be flown from the current pose.

        Refuses vertical moves (lanes never change altitude), distances the
        Tello cannot fly exactly, and any move whose dead-reckoned endpoint
        leaves the geofence.
        """
        if direction not in HORIZONTAL_DIRECTIONS:
            return False
        if not is_finite_number(distance_cm) or not MIN_MOVE_CM <= distance_cm <= MAX_MOVE_CM:
            return False
        with self._lock:
            x_cm, y_cm = self.pose.endpoint(direction, distance_cm)
        return self.inside_fence(x_cm, y_cm)

    def inside_fence(self, x_cm: float, y_cm: float) -> bool:
        """True if a point lies inside the geofence square around takeoff."""
        # A small tolerance absorbs floating-point error from rotated moves.
        limit = self.geofence_cm + 1e-6
        return abs(x_cm) <= limit and abs(y_cm) <= limit

    def record_move(self, direction: str, distance_cm: float) -> None:
        """Update the dead-reckoned pose after a horizontal move completed."""
        with self._lock:
            self.pose.apply_move(direction, distance_cm)

    def reset_pose(self) -> RelativePose:
        """Make the drone's current spot the takeoff point; return the pose it replaces.

        Call this only while the drone is on the ground, immediately before
        it takes off: the pose and the geofence are relative to the takeoff
        point of the current flight. The returned pose is where the previous
        flight ended relative to its own takeoff point.
        """
        with self._lock:
            previous, self.pose = self.pose, RelativePose()
        return previous

    def pose_snapshot(self) -> RelativePose:
        """Return a consistent copy of the current pose."""
        with self._lock:
            return RelativePose(self.pose.x_cm, self.pose.y_cm, self.pose.heading_deg)

    # --- Battery ---

    def can_takeoff(self, battery: float | None) -> bool:
        """True only for a known battery level at or above the takeoff minimum."""
        return is_finite_number(battery) and battery >= self.min_takeoff_battery

    def should_land(self, battery: float | None) -> bool:
        """True when a known battery level is below the landing threshold."""
        return is_finite_number(battery) and battery < self.land_battery

    # --- Link watchdog ---

    def note_ground_message(self) -> None:
        """Reset the link-loss watchdog; call this for every ground message."""
        self.link_watchdog.feed()

    def link_age_s(self) -> float:
        """Seconds since the last ground message."""
        return self.link_watchdog.age_s()

    def link_lost(self) -> bool:
        """True once the ground has been silent for the link-loss threshold."""
        return self.link_watchdog.expired()
