"""Drone-agnostic flight safety policy.

Battery thresholds and the return-to-home rule used by the frame loop
(:mod:`flightrisk.dashboard.pipeline`) and the confirm-match recovery
sequence (:mod:`flightrisk.dashboard.recovery`). The policy reads only the
normalized telemetry snapshot and declared capabilities, so it applies to any
backend without knowing which drone it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from flightrisk.config import AmberConfig, get_config
from flightrisk.drone.controller import Capability, DroneCapabilities
from flightrisk.drone.snapshot import TelemetrySnapshot

#: Minimum battery percentage required to attempt a return-to-home before
#: landing. See :data:`flightrisk.dashboard.recovery.RTH_MIN_BATTERY_PCT` for
#: the reasoning behind the value.
DEFAULT_RTH_MIN_BATTERY_PCT = 40


@dataclass(frozen=True)
class SafetyPolicy:
    battery_warn_pct: int = 20
    battery_critical_pct: int = 10
    rth_min_battery_pct: int = DEFAULT_RTH_MIN_BATTERY_PCT

    @classmethod
    def from_config(cls, config: AmberConfig | None = None, **overrides) -> "SafetyPolicy":
        """Thresholds from ``config.drone`` (``battery_warn_threshold`` and
        ``battery_critical_threshold``), with optional keyword overrides."""
        drone = (config or get_config()).drone
        values = {
            "battery_warn_pct": drone.battery_warn_threshold,
            "battery_critical_pct": drone.battery_critical_threshold,
        }
        values.update(overrides)
        return cls(**values)

    @staticmethod
    def battery_pct(snapshot: TelemetrySnapshot) -> float | None:
        """Known battery percentage, or ``None`` when not reported.

        A reading of 0 is treated as unknown, as the safety code always has.
        """
        r = snapshot.battery_pct
        if r is None or r.value <= 0:
            return None
        return r.value

    def battery_monitored(self, snapshot: TelemetrySnapshot) -> bool:
        """Battery limits apply only while flying with a known battery level."""
        return snapshot.is_flying and self.battery_pct(snapshot) is not None

    def battery_critical(self, snapshot: TelemetrySnapshot) -> bool:
        """At or below the critical level: land now."""
        pct = self.battery_pct(snapshot)
        return self.battery_monitored(snapshot) and pct <= self.battery_critical_pct

    def battery_low(self, snapshot: TelemetrySnapshot) -> bool:
        """At or below the warning level."""
        pct = self.battery_pct(snapshot)
        return self.battery_monitored(snapshot) and pct <= self.battery_warn_pct

    def can_return_home(self, capabilities: DroneCapabilities, battery_pct: float) -> bool:
        """Return home only if the drone supports it and has the reserve for it."""
        return capabilities.supports(Capability.RETURN_TO_HOME) and battery_pct >= self.rth_min_battery_pct


__all__ = ["DEFAULT_RTH_MIN_BATTERY_PCT", "SafetyPolicy"]
