"""The checks a single-drone search must pass before it starts (the ``start_search`` socket event).

The fleet start gate (``FleetCoordinator._start_refusals``) refuses a drone
when its telemetry from the last ``TELEMETRY_FRESH_S`` says the edge cannot
reach its drone (``drone_link_ok: false``) or cannot sense obstacles
(``obstacle_sensor_ok: false``), and when its battery is unknown or below
``MIN_START_BATTERY``. The edge builds those fields from its drone
controller: ``link_status()``, its obstacle sensor and ``read_battery()``
(or ``state.battery``).

The single-drone search reads the controller in this process, so there is
no edge-to-ground hop whose telemetry could be old. It reads the same facts
from the controller at the moment the search starts and applies the same
rules:

* **Connected.** A controller that says it is not connected is refused.
* **Drone link.** When the controller reports one (``link_status()``; the
  Tello reports its link down when no state packet has arrived for 3 s or
  its last command got no answer), a link that is down is refused with the
  controller's own text. A controller that reports no link status is not
  refused for it, exactly as on the edge. A ``link_status()`` that raises or
  returns something else counts as down, so the search is refused rather
  than flown blind.
* **Battery.** ``read_battery()`` when the controller has it (the Tello reads
  it from its newest state packet, whose age the link check bounds), else
  the battery reading of its telemetry snapshot. No reading, or 0 (which the
  legacy state cache uses for "unknown"), is refused as no battery report,
  and a reading below ``MIN_START_BATTERY`` as too low.
* **Obstacle view.** The search checks the path ahead with the ground's
  obstacle guard on the drone's video. Without a current frame
  (``get_frame()`` returns None, for example while the video is down or the
  Tello's decoder is recovering a frozen stream) the guard cannot look, so
  the search is refused, as a fleet start is refused for an edge whose
  obstacle sensor cannot look.

Nothing here sends a flight command. Reading the frame is the same
``get_frame()`` call that the dashboard's frame loop makes all the time.
"""

from __future__ import annotations

import math
from typing import Any

from flightrisk.drone.controller import DroneLinkStatus
from flightrisk.drone.coordinator import MIN_START_BATTERY
from flightrisk.drone.snapshot import read_telemetry

__all__ = ["single_search_refusal"]

# Operator texts are cut to this many characters, like the edge's link detail.
_MAX_DETAIL_LEN = 200


def _number(value: Any) -> float | None:
    """*value* as a finite float, or None for anything that is not a real number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _printable(text: Any, default: str) -> str:
    cleaned = "".join(ch for ch in str(text or "") if ch.isprintable()).strip()[:_MAX_DETAIL_LEN]
    return cleaned or default


def _link_problem(drone: Any) -> str | None:
    """Why the controller says it cannot reach its drone, or None when it can or reports nothing."""
    probe = getattr(drone, "link_status", None)
    if not callable(probe):
        return None
    try:
        status = probe()
    except Exception as exc:  # noqa: BLE001 - an unreadable link refuses the search
        return f"the drone link status is unreadable ({type(exc).__name__})"
    if not isinstance(status, DroneLinkStatus):
        return "the drone link status is unreadable"
    if status.ok:
        return None
    return "the drone link is down: " + _printable(status.detail, "drone unreachable")


def _battery_pct(drone: Any) -> float | None:
    """The drone's battery percentage as the edge would report it, or None when unknown."""
    reader = getattr(drone, "read_battery", None)
    if callable(reader):
        try:
            value = reader()
        except Exception:  # noqa: BLE001 - an unreadable battery is an unknown battery
            return None
    else:
        reading = read_telemetry(drone).battery_pct
        value = None if reading is None else reading.value
    number = _number(value)
    return number if number is not None and number > 0 else None


def single_search_refusal(drone: Any, *, min_battery: float = MIN_START_BATTERY) -> str | None:
    """Why a single-drone search must not start on *drone* now, or None when it may.

    Reads the controller's connection, drone link, battery and video at the
    moment of the call (see the module docstring for the rules). Never
    raises and sends no flight command.
    """
    state = getattr(drone, "state", None)
    if state is not None and getattr(state, "is_connected", True) is False:
        return "the drone is not connected"
    link = _link_problem(drone)
    if link is not None:
        return link
    battery = _battery_pct(drone)
    if battery is None:
        return "no battery report from the drone"
    if battery < min_battery:
        return f"battery {battery:g}% is below the {min_battery:g}% needed to start a search"
    try:
        frame = drone.get_frame()
    except Exception as exc:  # noqa: BLE001 - no view means no obstacle check
        return f"the drone's video is unreadable ({type(exc).__name__}), so the obstacle guard cannot check the path"
    if frame is None:
        return "no video frame from the drone, so the obstacle guard cannot check the path"
    return None
