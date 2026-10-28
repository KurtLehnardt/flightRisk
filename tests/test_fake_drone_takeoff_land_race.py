"""FakeDrone.takeoff checks its abort flag under the command lock, like TelloController.

A land that arrives while a takeoff is checking ``abort`` must be ordered
before the check or after the whole takeoff, never between the check and the
takeoff. Before the lock, the land ran to completion inside the check and the
takeoff then lifted the drone after the landing had been requested.
"""
from __future__ import annotations

import threading

from flightrisk.drone.fake import FakeDrone


class LandDuringCheck(threading.Event):
    """An abort flag (never set) whose first check starts a ``land`` and gives it time to finish."""

    def __init__(self, drone: FakeDrone) -> None:
        super().__init__()
        self.drone = drone
        self.lander: threading.Thread | None = None

    def is_set(self) -> bool:
        if self.lander is None:
            self.lander = threading.Thread(target=self.drone.land, daemon=True)
            self.lander.start()
            self.lander.join(0.3)
        return False


def test_land_arriving_during_the_abort_check_is_ordered_after_the_takeoff():
    drone = FakeDrone()
    drone.connect()
    abort = LandDuringCheck(drone)

    drone.takeoff(abort)
    abort.lander.join(2.0)

    assert not abort.lander.is_alive()
    assert drone.calls[1:] == [("takeoff",), ("land",)]
    assert drone.state.is_flying is False
