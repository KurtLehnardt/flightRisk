"""Multi-drone fleet manager.

Manages multiple DroneController instances for coordinated search.
A fleet of 1 behaves identically to single-drone setup.

Backends are pluggable: pass a `factory` callable to build drones other
than the default Tello (e.g. MAVLink, simulated) without touching this
module. Any object satisfying the `DroneController` protocol works.
"""

import logging
import threading
from typing import Callable

from amber.drone.controller import DroneController

logger = logging.getLogger(__name__)


def _default_factory(name: str, host: str) -> DroneController:
    """Build a TelloController. Imported lazily so DroneFleet has no hard
    dependency on the Tello backend (or its djitellopy dependency)."""
    from amber.drone.tello import TelloController
    return TelloController(name=name, host=host)


class DroneFleet:
    def __init__(self, factory: Callable[[str, str], DroneController] | None = None):
        self._factory: Callable[[str, str], DroneController] = factory or _default_factory
        self._drones: dict[str, DroneController] = {}
        self._lock = threading.Lock()
        self._primary_id: str | None = None
        self._pending: set[str] = set()

    def has_host(self, host: str) -> bool:
        """Check if a drone is already registered at this host IP."""
        with self._lock:
            return any(ctrl.host == host for ctrl in self._drones.values())

    def register(self, drone_id: str, host: str = "192.168.10.1") -> bool:
        with self._lock:
            if drone_id in self._drones or drone_id in self._pending:
                return False
            if any(ctrl.host == host for ctrl in self._drones.values()):
                return False
            self._pending.add(drone_id)
        ctrl = self._factory(drone_id, host)
        try:
            if ctrl.connect():
                with self._lock:
                    self._drones[drone_id] = ctrl
                    if self._primary_id is None:
                        self._primary_id = drone_id
                return True
            return False
        finally:
            with self._lock:
                self._pending.discard(drone_id)

    def deregister(self, drone_id: str) -> bool:
        with self._lock:
            if drone_id not in self._drones:
                return False
            ctrl = self._drones.pop(drone_id)
            if self._primary_id == drone_id:
                self._primary_id = next(iter(self._drones), None)
        # Disconnect outside lock — may hang on dead drone
        try:
            ctrl.disconnect()
        except Exception:
            pass
        return True

    def get(self, drone_id: str) -> DroneController | None:
        return self._drones.get(drone_id)

    @property
    def primary(self) -> DroneController | None:
        return self._drones.get(self._primary_id) if self._primary_id else None

    def set_primary(self, drone_id: str) -> bool:
        with self._lock:
            if drone_id not in self._drones:
                return False
            self._primary_id = drone_id
            return True

    @property
    def count(self) -> int:
        return len(self._drones)

    @property
    def drone_ids(self) -> list[str]:
        return list(self._drones.keys())

    def get_all_telemetry(self) -> dict[str, dict]:
        result = {}
        for did, ctrl in self._drones.items():
            result[did] = {
                "battery": ctrl.state.battery,
                "height": ctrl.state.height,
                "temperature": ctrl.state.temperature,
                "flight_time": ctrl.state.flight_time,
                "is_flying": ctrl.state.is_flying,
                "is_connected": ctrl.state.is_connected,
            }
        return result

    def broadcast_command(self, command: str, *args, **kwargs) -> dict[str, Exception | None]:
        """Call `command` on every registered drone.

        Unlike a fire-and-forget broadcast, failures are neither raised
        nor silently swallowed — each drone's outcome is reported back so
        callers can surface partial failures (e.g. "land succeeded on
        drone-1 but drone-2 failed to respond").

        Returns:
            Mapping of drone_id -> None (success) or the Exception raised.
        """
        results: dict[str, Exception | None] = {}
        for drone_id, drone in self._drones.items():
            try:
                getattr(drone, command)(*args, **kwargs)
                results[drone_id] = None
            except Exception as e:
                logger.error("Command %s failed on %s: %s", command, drone_id, e)
                results[drone_id] = e
        return results

    def disconnect_all(self):
        with self._lock:
            drones = list(self._drones.values())
            self._drones.clear()
            self._primary_id = None
        for ctrl in drones:
            try:
                ctrl.disconnect()
            except Exception:
                pass
