"""Normalized, backend-independent drone telemetry.

A :class:`TelemetrySnapshot` is what the dashboard and the safety logic read
about a drone. Every measurement is an optional :class:`Reading` carrying its
value in SI units, the unit, where it came from (``source``, e.g. ``tello.h``
or ``mavlink.position``) and when it was measured, so consumers can judge
staleness from :meth:`Reading.age_s`. A sensor a backend does not have, or
has not reported yet, is ``None``; it is never filled with a zero.

Backends produce snapshots through the ``TelemetrySource`` role
(``snapshot()``). :func:`read_telemetry` is the one entry point callers use:
it also adapts legacy objects that only expose the raw ``DroneState`` cache.

:func:`legacy_telemetry_payload` turns a snapshot into the dictionary the web
UI has always received as ``frame.telemetry`` and ``fleet_telemetry``; it is
the single place that shape is produced.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, fields
from typing import Any

from flightrisk.drone.controller import TelemetrySource

logger = logging.getLogger(__name__)

# Units used by every reading. Kept as plain strings so they serialize as-is.
PERCENT = "%"
METRE = "m"
METRE_PER_SECOND = "m/s"
DEGREE = "deg"
CELSIUS = "degC"
SECOND = "s"

#: How ``is_flying`` was determined.
IS_FLYING_COMMANDED = "commanded"
IS_FLYING_SENSED = "sensed"


@dataclass(frozen=True)
class Reading:
    """One measured value."""

    value: float
    unit: str
    source: str
    measured_at: float  # epoch seconds (time.time())

    def age_s(self, now: float | None = None) -> float:
        """Seconds since the value was measured (never negative)."""
        if now is None:
            now = time.time()
        return max(0.0, now - self.measured_at)

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "measured_at": self.measured_at,
            "age_s": round(self.age_s(now), 3),
        }


#: Expected unit for every reading field; used by contract tests and to keep
#: backends honest about SI units.
READING_UNITS: dict[str, str] = {
    "battery_pct": PERCENT,
    "height_m": METRE,
    "tof_m": METRE,
    "baro_altitude_m": METRE,
    "altitude_msl_m": METRE,
    "latitude_deg": DEGREE,
    "longitude_deg": DEGREE,
    "heading_deg": DEGREE,
    "vx_mps": METRE_PER_SECOND,
    "vy_mps": METRE_PER_SECOND,
    "vz_mps": METRE_PER_SECOND,
    "ground_speed_mps": METRE_PER_SECOND,
    "pitch_deg": DEGREE,
    "roll_deg": DEGREE,
    "yaw_deg": DEGREE,
    "temperature_c": CELSIUS,
    "flight_time_s": SECOND,
}


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Point-in-time view of one drone, independent of the backend."""

    backend: str
    drone_id: str
    captured_at: float
    connected: bool = False
    is_flying: bool = False
    #: ``"commanded"`` when ``is_flying`` follows takeoff/land commands
    #: (possibly corrected by a heuristic), ``"sensed"`` when it comes from
    #: the vehicle itself.
    is_flying_source: str = IS_FLYING_COMMANDED
    flight_mode: str | None = None

    battery_pct: Reading | None = None
    #: Height above the takeoff point.
    height_m: Reading | None = None
    #: Distance to the surface directly below (range sensor).
    tof_m: Reading | None = None
    #: Barometric altitude (not relative to takeoff).
    baro_altitude_m: Reading | None = None
    altitude_msl_m: Reading | None = None
    latitude_deg: Reading | None = None
    longitude_deg: Reading | None = None
    heading_deg: Reading | None = None
    vx_mps: Reading | None = None
    vy_mps: Reading | None = None
    vz_mps: Reading | None = None
    ground_speed_mps: Reading | None = None
    pitch_deg: Reading | None = None
    roll_deg: Reading | None = None
    yaw_deg: Reading | None = None
    temperature_c: Reading | None = None
    flight_time_s: Reading | None = None

    def readings(self) -> dict[str, Reading | None]:
        """All reading fields by name (``None`` for unavailable sensors)."""
        return {name: getattr(self, name) for name in READING_UNITS}

    def oldest_age_s(self, now: float | None = None) -> float | None:
        """Age of the stalest available reading, or ``None`` if there are none."""
        ages = [r.age_s(now) for r in self.readings().values() if r is not None]
        return max(ages) if ages else None

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        """JSON-friendly form, for logs, metrics exporters and test harnesses."""
        if now is None:
            now = time.time()
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = value.to_dict(now) if isinstance(value, Reading) else value
        return out


def _number(value: Any) -> float | None:
    """Return ``value`` if it is a real number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def reading(value: Any, unit: str, source: str, measured_at: float | None, scale: float = 1.0) -> Reading | None:
    """Build a :class:`Reading`, or ``None`` when there is no usable value.

    ``scale`` converts the raw value into the SI unit (for example ``0.01``
    for centimetres to metres). An unscaled value keeps its original type.
    """
    number = _number(value)
    if number is None or measured_at is None:
        return None
    if scale != 1.0:
        number = number * scale
    return Reading(value=number, unit=unit, source=source, measured_at=measured_at)


def snapshot_from_state(
    state: Any,
    backend: str = "legacy",
    drone_id: str = "",
    now: float | None = None,
) -> TelemetrySnapshot:
    """Adapt a legacy ``DroneState``-like object to a snapshot.

    Used only for controllers that do not implement ``TelemetrySource``.
    The raw cache carries no timestamps, so every reading is stamped with
    ``now`` (age 0). A battery value of 0 means "unknown" in the legacy
    cache (the safety code has always treated it that way), so it maps to
    ``None``.
    """
    if now is None:
        now = time.time()
    if state is None:
        return TelemetrySnapshot(backend=backend, drone_id=drone_id, captured_at=now)

    def get(name: str) -> Any:
        return getattr(state, name, None)

    battery = _number(get("battery"))
    flight_mode = get("flight_mode")
    return TelemetrySnapshot(
        backend=backend,
        drone_id=drone_id,
        captured_at=now,
        connected=bool(get("is_connected")),
        is_flying=bool(get("is_flying")),
        is_flying_source=IS_FLYING_COMMANDED,
        flight_mode=flight_mode if isinstance(flight_mode, str) else None,
        battery_pct=reading(battery if battery else None, PERCENT, "state.battery", now),
        height_m=reading(get("height"), METRE, "state.height", now, scale=0.01),
        temperature_c=reading(get("temperature"), CELSIUS, "state.temperature", now),
        flight_time_s=reading(get("flight_time"), SECOND, "state.flight_time", now),
        latitude_deg=reading(get("latitude"), DEGREE, "state.latitude", now),
        longitude_deg=reading(get("longitude"), DEGREE, "state.longitude", now),
        altitude_msl_m=reading(get("altitude_msl"), METRE, "state.altitude_msl", now),
        heading_deg=reading(get("heading"), DEGREE, "state.heading", now),
        ground_speed_mps=reading(get("ground_speed"), METRE_PER_SECOND, "state.ground_speed", now),
    )


def read_telemetry(drone: Any) -> TelemetrySnapshot:
    """Return the normalized telemetry for ``drone``.

    Uses the backend's own ``snapshot()`` when it implements
    ``TelemetrySource``; otherwise (legacy controllers, simple test doubles)
    adapts its ``state`` cache.
    """
    drone_id = getattr(drone, "name", "")
    if not isinstance(drone_id, str):
        drone_id = ""
    if isinstance(drone, TelemetrySource):
        try:
            snap = drone.snapshot()
        except Exception as exc:  # noqa: BLE001 -- telemetry must never take the caller down
            logger.warning("telemetry_snapshot_failed drone_id=%s error_type=%s", drone_id, type(exc).__name__)
            snap = None
        if isinstance(snap, TelemetrySnapshot):
            return snap
    return snapshot_from_state(getattr(drone, "state", None), drone_id=drone_id)


def _legacy_value(r: Reading | None) -> Any:
    return 0 if r is None else r.value


def legacy_telemetry_payload(snapshot: TelemetrySnapshot, include_connected: bool = False) -> dict[str, Any]:
    """Build the web UI's telemetry dictionary from a snapshot.

    Deprecated shape, kept only so the existing dashboard JavaScript keeps
    working: battery %, height in whole centimetres, temperature, flight
    time in seconds and the flying flag, with 0 standing in for "unknown".
    This is the single source of truth for ``frame.telemetry`` (and, with
    ``include_connected=True``, for each entry of ``fleet_telemetry`` /
    ``fleet_status``). New consumers should read the snapshot directly.
    """
    height = snapshot.height_m
    payload: dict[str, Any] = {
        "battery": _legacy_value(snapshot.battery_pct),
        "height": 0 if height is None else int(round(height.value * 100)),
        "temperature": _legacy_value(snapshot.temperature_c),
        "flight_time": _legacy_value(snapshot.flight_time_s),
        "is_flying": snapshot.is_flying,
    }
    if include_connected:
        payload["is_connected"] = snapshot.connected
    return payload


GPS_MAX_AGE_S = 5.0
"""A GPS position older than this is not offered as the map's anchor."""


def gps_payload(
    snapshot: TelemetrySnapshot, *, fix_ok: bool | None = None, now: float | None = None
) -> dict[str, Any] | None:
    """The GPS position of a drone that reports one (MAVLink), else None.

    Returns ``{"lat", "lon", "heading_deg", "ok", "reason"}``. The dashboard
    map offers the position as the plan's anchor only when ``ok`` is true:
    the drone has a 3D fix (*fix_ok*, when the backend knows it), the
    position is plausible (within range and not the 0, 0 a receiver reports
    before its first fix) and it was measured in the last ``GPS_MAX_AGE_S``
    seconds. Otherwise ``reason`` says why (docs/map-and-plan-editing.md).
    The legacy telemetry payload keeps its fixed shape; this is sent beside it.
    """
    lat_r, lon_r = snapshot.latitude_deg, snapshot.longitude_deg
    if lat_r is None or lon_r is None:
        return None
    lat, lon = float(lat_r.value), float(lon_r.value)
    out: dict[str, Any] = {"lat": lat, "lon": lon, "ok": False, "reason": None}
    if snapshot.heading_deg is not None:
        out["heading_deg"] = float(snapshot.heading_deg.value)
    age = max(lat_r.age_s(now), lon_r.age_s(now))
    if fix_ok is False:
        out["reason"] = "no 3D GPS fix"
    elif not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        out["reason"] = "the reported position is out of range"
    elif abs(lat) < 1e-6 and abs(lon) < 1e-6:
        out["reason"] = "the reported position is 0, 0 (no fix yet)"
    elif age > GPS_MAX_AGE_S:
        out["reason"] = f"the position is {age:.0f} s old"
    else:
        out["ok"] = True
    return out


__all__ = [
    "CELSIUS",
    "DEGREE",
    "IS_FLYING_COMMANDED",
    "IS_FLYING_SENSED",
    "METRE",
    "METRE_PER_SECOND",
    "PERCENT",
    "READING_UNITS",
    "SECOND",
    "Reading",
    "TelemetrySnapshot",
    "gps_payload",
    "legacy_telemetry_payload",
    "read_telemetry",
    "reading",
    "snapshot_from_state",
]
