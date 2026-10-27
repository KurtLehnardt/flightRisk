"""Multi-drone fleet manager.

Manages multiple DroneController instances for coordinated search.
A fleet of 1 behaves identically to single-drone setup.

Backends are pluggable: pass a `factory` callable to build drones other
than the default Tello (e.g. MAVLink, simulated) without touching this
module. Any object satisfying the `DroneController` protocol works.
"""

import logging
import threading
import time
from typing import Callable

from flightrisk.drone.controller import DroneController
from flightrisk.drone.snapshot import legacy_telemetry_payload, read_telemetry

logger = logging.getLogger(__name__)

# How long ``broadcast_command`` and ``disconnect_all`` wait for every
# drone's call. A Tello ``land()`` takes at most 25 s (``_LAND_DEADLINE_S``
# in flightrisk/drone/tello.py) and a Tello takeoff waits at most 20 s for
# its reply, so this leaves a margin over the longest call a healthy
# backend makes. A call still running then (a MAVLink land, for example,
# waits for its reply with no timeout) is reported as a ``RuntimeError``
# for that drone, so LAND ALL reports the drone instead of hanging; the
# call's thread keeps running in the background.
_CALL_DEADLINE_S = 30.0


def _default_factory(name: str, host: str) -> DroneController:
    """Build a TelloController. Imported lazily so DroneFleet has no hard
    dependency on the Tello backend (or its djitellopy dependency).

    Deprecated fallback for callers that construct ``DroneFleet()`` without
    a factory; the dashboard always passes one from
    ``flightrisk.drone.registry``."""
    from flightrisk.drone.tello import TelloController
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
        # Legacy UI shape, built by the single snapshot adapter.
        return {
            did: legacy_telemetry_payload(read_telemetry(ctrl), include_connected=True)
            for did, ctrl in self._drones.items()
        }

    def broadcast_command(self, command: str, *args, **kwargs) -> dict[str, Exception | None]:
        """Call `command` on every registered drone, on every drone at once.

        Each drone's call runs on its own thread, so a drone whose call
        blocks (for example a land that waits for a drone that never
        confirms it) does not delay the command to any other drone. This
        returns once every call has finished, or after ``_CALL_DEADLINE_S``
        with each call still running reported as a ``RuntimeError``.

        Unlike a fire-and-forget broadcast, failures are neither raised
        nor silently swallowed — each drone's outcome is reported back so
        callers can surface partial failures (e.g. "land succeeded on
        drone-1 but drone-2 failed to respond").

        Returns:
            Mapping of drone_id -> None (success) or the Exception raised.
        """
        with self._lock:
            drones = list(self._drones.items())

        def call(drone: DroneController) -> None:
            getattr(drone, command)(*args, **kwargs)

        results = _call_each(drones, call, f"fleet-{command}")
        for drone_id, error in results.items():
            if error is not None:
                logger.error("Command %s failed on %s: %s", command, drone_id, error)
        return results

    def disconnect_all(self):
        """Disconnect every drone, all at once (each disconnect lands a flying drone first)."""
        with self._lock:
            drones = list(self._drones.items())
            self._drones.clear()
            self._primary_id = None
        _call_each(drones, lambda ctrl: ctrl.disconnect(), "fleet-disconnect")


def _call_each(
    drones: list[tuple[str, DroneController]],
    call: Callable[[DroneController], object],
    thread_name: str,
) -> dict[str, Exception | None]:
    """Run ``call`` on every drone at once, one thread per drone, and wait for all of them.

    Returns drone_id -> None when the call returned, or the exception it
    raised. A call that ends any other way (a ``BaseException`` that is not
    an ``Exception``) is reported as a ``RuntimeError``, never as success,
    and so is a call still running ``_CALL_DEADLINE_S`` after the calls
    started: this waits no longer than that, and a call that finishes
    later does not change the mapping returned. If a thread cannot be
    started, that drone's call runs on the calling thread instead of being
    skipped.
    """
    lock = threading.Lock()
    finished: dict[str, Exception | None] = {}

    def run(drone_id: str, drone: DroneController) -> None:
        try:
            call(drone)
        except Exception as e:
            outcome: Exception | None = e
        else:
            outcome = None
        with lock:
            finished[drone_id] = outcome

    deadline = time.monotonic() + _CALL_DEADLINE_S
    threads: dict[str, threading.Thread] = {}
    for drone_id, drone in drones:
        thread = threading.Thread(
            target=run, args=(drone_id, drone), name=f"{thread_name}-{drone_id}", daemon=True
        )
        try:
            thread.start()
        except RuntimeError:
            run(drone_id, drone)
            continue
        threads[drone_id] = thread
    for thread in threads.values():
        thread.join(max(0.0, deadline - time.monotonic()))

    results: dict[str, Exception | None] = {}
    with lock:
        for drone_id, _ in drones:
            if drone_id in finished:
                results[drone_id] = finished[drone_id]
            elif drone_id in threads and threads[drone_id].is_alive():
                results[drone_id] = RuntimeError(
                    f"{drone_id}: the call did not finish within {_CALL_DEADLINE_S:g} s"
                )
            else:
                results[drone_id] = RuntimeError(f"{drone_id}: the call did not finish")
    return results
