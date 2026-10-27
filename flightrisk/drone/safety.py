"""Edge-side flight safety envelope for relative-move Tello flights.

The Tello has no GPS, so the edge tracks the drone's position by dead
reckoning: every commanded relative move updates a pose that starts at the
spot where the edge process began (the takeoff point). The envelope uses that
pose and a few configured limits to decide what the edge may do:

* Soft geofence: ``check_move`` refuses a horizontal move whose dead-reckoned
  endpoint would leave a square of half-width ``geofence_cm`` around the
  takeoff point. Lanes may not move vertically at all; altitude changes come
  only from the altitude-slot correction, which keeps the two drones in a
  fleet at separate heights.
* Altitude slot: after takeoff the drone climbs (or descends) from its
  measured takeoff height to ``alt_slot_cm`` (``slot_move``).
* Battery gates: ``can_takeoff`` is false below ``min_takeoff_battery`` and
  ``should_land`` is true below ``land_battery``. An unknown battery level
  never permits a takeoff.
* Link-loss watchdog: ``link_watchdog`` records the time of the last ground
  message; ``link_lost`` reports when it is older than ``link_loss_land_s``.

Design reference: hackathon design section 10. The limits can be overridden
with the environment variables ``FLIGHTRISK_GEOFENCE_CM``,
``FLIGHTRISK_MIN_TAKEOFF_BATTERY`` and ``FLIGHTRISK_LINK_LOSS_LAND_S`` through
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
    "DEFAULT_ALT_SLOT_CM",
    "DEFAULT_GEOFENCE_CM",
    "DEFAULT_LAND_BATTERY",
    "DEFAULT_LINK_LOSS_LAND_S",
    "DEFAULT_MIN_TAKEOFF_BATTERY",
    "GEOFENCE_CM_ENV",
    "HORIZONTAL_DIRECTIONS",
    "LINK_LOSS_LAND_S_ENV",
    "LinkWatchdog",
    "MAX_MOVE_CM",
    "MIN_MOVE_CM",
    "MIN_TAKEOFF_BATTERY_ENV",
    "RelativePose",
    "SafetyEnvelope",
]

LINK_LOSS_LAND_S_ENV = "FLIGHTRISK_LINK_LOSS_LAND_S"
MIN_TAKEOFF_BATTERY_ENV = "FLIGHTRISK_MIN_TAKEOFF_BATTERY"
GEOFENCE_CM_ENV = "FLIGHTRISK_GEOFENCE_CM"

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

HORIZONTAL_DIRECTIONS = ("forward", "back", "left", "right")

# Unit vectors (right, ahead) for each horizontal direction relative to the
# drone's current heading.
_DIRECTION_VECTORS: dict[str, tuple[float, float]] = {
    "forward": (0.0, 1.0),
    "back": (0.0, -1.0),
    "right": (1.0, 0.0),
    "left": (-1.0, 0.0),
}


def _is_number(value: object) -> bool:
    """Return True for a finite int or float (booleans are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


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
        if not _is_number(timeout_s) or timeout_s <= 0:
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
    """Geofence, altitude slot, battery gates and link watchdog for one drone."""

    def __init__(
        self,
        geofence_cm: float = DEFAULT_GEOFENCE_CM,
        alt_slot_cm: float = DEFAULT_ALT_SLOT_CM,
        min_takeoff_battery: float = DEFAULT_MIN_TAKEOFF_BATTERY,
        land_battery: float = DEFAULT_LAND_BATTERY,
        link_loss_land_s: float = DEFAULT_LINK_LOSS_LAND_S,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not _is_number(geofence_cm) or geofence_cm <= 0:
            raise ValueError(f"geofence_cm must be a positive number, got {geofence_cm!r}")
        if not _is_number(min_takeoff_battery) or not 0 <= min_takeoff_battery <= 100:
            raise ValueError(f"min_takeoff_battery must be within 0-100, got {min_takeoff_battery!r}")
        if not _is_number(land_battery) or not 0 <= land_battery <= 100:
            raise ValueError(f"land_battery must be within 0-100, got {land_battery!r}")
        if land_battery > min_takeoff_battery:
            raise ValueError("land_battery must not exceed min_takeoff_battery")
        self.geofence_cm = float(geofence_cm)
        self.min_takeoff_battery = float(min_takeoff_battery)
        self.land_battery = float(land_battery)
        self._lock = threading.Lock()
        self.alt_slot_cm = self._validated_slot(alt_slot_cm)
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
        )

    # --- Altitude slot ---

    @staticmethod
    def _validated_slot(alt_slot_cm: float) -> float:
        if not _is_number(alt_slot_cm) or not MIN_ALT_SLOT_CM <= alt_slot_cm <= MAX_ALT_SLOT_CM:
            raise ValueError(
                f"altitude slot must be within {MIN_ALT_SLOT_CM}-{MAX_ALT_SLOT_CM} cm, got {alt_slot_cm!r}"
            )
        return float(alt_slot_cm)

    def set_alt_slot(self, alt_slot_cm: float) -> None:
        """Change the altitude slot. Raises ``ValueError`` for an out-of-range slot."""
        self.alt_slot_cm = self._validated_slot(alt_slot_cm)

    def slot_move(self, height_cm: float | None) -> tuple[str, int] | None:
        """Return the ``(direction, distance_cm)`` move that reaches the slot.

        Returns ``None`` when the drone is already within the minimum move
        size of its slot. Raises ``ValueError`` when the height is unknown or
        not positive, because the climb cannot be computed safely.
        """
        if not _is_number(height_cm) or height_cm <= 0:
            raise ValueError(f"height is unknown, cannot reach the altitude slot (got {height_cm!r})")
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
        if not _is_number(distance_cm) or not MIN_MOVE_CM <= distance_cm <= MAX_MOVE_CM:
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

    def pose_snapshot(self) -> RelativePose:
        """Return a consistent copy of the current pose."""
        with self._lock:
            return RelativePose(self.pose.x_cm, self.pose.y_cm, self.pose.heading_deg)

    # --- Battery ---

    def can_takeoff(self, battery: float | None) -> bool:
        """True only for a known battery level at or above the takeoff minimum."""
        return _is_number(battery) and battery >= self.min_takeoff_battery

    def should_land(self, battery: float | None) -> bool:
        """True when a known battery level is below the landing threshold."""
        return _is_number(battery) and battery < self.land_battery

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
