"""Test doubles for the single-drone search (``start_search``).

``start_search`` refuses a drone whose link is down, whose battery is
unknown or low, or that gives no video frame (``flightrisk/dashboard/single_search.py``).
A bare ``MagicMock`` drone fails every one of those checks, so a test that
wants the search to run makes its double report a healthy drone with
``ready_single_drone``.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from flightrisk.drone.controller import DroneLinkStatus

#: A battery well above ``MIN_START_BATTERY`` (30 %).
HEALTHY_BATTERY = 80


def ready_single_drone(drone: Any, *, frame: Any = None) -> Any:
    """Make the ``MagicMock`` *drone* report a healthy link, a fresh battery and a video frame.

    The frame is *frame* when given, else the double's own ``get_frame``
    value when the test already set one, else a small black frame.
    """
    drone.link_status.return_value = DroneLinkStatus(True, 0.1)
    drone.read_battery.return_value = HEALTHY_BATTERY
    if frame is not None:
        drone.get_frame.return_value = frame
    elif not isinstance(getattr(drone.get_frame, "return_value", None), np.ndarray):
        drone.get_frame.return_value = np.zeros((4, 4, 3), dtype=np.uint8)
    return drone
