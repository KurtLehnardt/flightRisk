"""Multi-drone fleet manager.

Manages multiple TelloController instances for coordinated search.
A fleet of 1 behaves identically to single-drone setup.
"""

import threading
from amber.drone.tello import TelloController


class DroneFleet:
    def __init__(self):
        self._drones: dict[str, TelloController] = {}
        self._lock = threading.Lock()
        self._primary_id: str | None = None

    def register(self, drone_id: str, host: str = "192.168.10.1") -> bool:
        with self._lock:
            if drone_id in self._drones:
                return False
            ctrl = TelloController(name=drone_id, host=host)
            if ctrl.connect():
                self._drones[drone_id] = ctrl
                if self._primary_id is None:
                    self._primary_id = drone_id
                return True
            return False

    def deregister(self, drone_id: str) -> bool:
        with self._lock:
            if drone_id not in self._drones:
                return False
            self._drones[drone_id].disconnect()
            del self._drones[drone_id]
            if self._primary_id == drone_id:
                self._primary_id = next(iter(self._drones), None)
            return True

    def get(self, drone_id: str) -> TelloController | None:
        return self._drones.get(drone_id)

    @property
    def primary(self) -> TelloController | None:
        return self._drones.get(self._primary_id) if self._primary_id else None

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

    def broadcast_command(self, command: str, **kwargs):
        for ctrl in self._drones.values():
            method = getattr(ctrl, command, None)
            if method and callable(method):
                try:
                    method(**kwargs)
                except Exception:
                    pass

    def disconnect_all(self):
        with self._lock:
            for ctrl in self._drones.values():
                try:
                    ctrl.disconnect()
                except Exception:
                    pass
            self._drones.clear()
            self._primary_id = None
