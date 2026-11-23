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
  the battery reading of its telemetry snapshot, which must be no older
  than ``MAX_BATTERY_AGE_S`` (a MAVLink autopilot's battery stream that
  stopped leaves an old value behind). No reading, or one that is not a
  finite number of at least 0, is refused as no battery report; the legacy
  state cache's 0 for "unknown" never becomes a snapshot reading, so a 0
  that is read is a real 0 %. A reading below ``MIN_START_BATTERY`` is
  refused as too low, and so is one at or below the battery monitor's
  critical level (``battery_critical_threshold``), which lands a flying
  drone at once, should that level ever be set above the start minimum.
* **Obstacle view.** The search checks the path ahead with the ground's
  obstacle guard on the drone's video, so it needs a current frame.
  ``get_frame()`` must return one (it returns None, for example, while the
  video is down or the Tello's decoder is recovering a frozen stream). When
  the controller reports how current its newest frame is
  (``frame_freshness()``, the Tello controller), the frame must also have
  been decoded (not the decoder's black placeholder from before the first
  frame), carry no ``stale_cause`` from the bound reader (decoder backlog,
  dropped video data, a corrupt frame, or no key frame yet), and be no
  older than ``MAX_FRAME_AGE_S``. A ``frame_freshness()`` that raises or
  returns something else counts as no current frame. A controller that
  does not report it is not refused for it, as with the drone link. Without
  a current frame the guard cannot look, so the search is refused, as a
  fleet start is refused for an edge whose obstacle sensor cannot look.

Nothing here sends a flight command. Reading the frame is the same
``get_frame()`` call that the dashboard's frame loop makes all the time.
"""

from __future__ import annotations

import math
from typing import Any

from flightrisk.config import DroneConfig, get_config
from flightrisk.drone.controller import DroneLinkStatus, FrameFreshness
from flightrisk.drone.coordinator import MIN_START_BATTERY
from flightrisk.drone.snapshot import read_telemetry

__all__ = ["MAX_BATTERY_AGE_S", "MAX_FRAME_AGE_S", "single_search_refusal"]

# TODO(fix/geofence-slot-battery-consistency): that parallel PR unifies the 30 %
# start minimum (MIN_START_BATTERY) and its environment override; read the
# minimum through its helper here once it has merged.

# Operator texts are cut to this many characters, like the edge's link detail.
_MAX_DETAIL_LEN = 200

#: The oldest video frame, in seconds, the obstacle guard may check the path
#: on before the search starts. The Tello sends about 30 frames a second, so
#: a working stream is never near it.
MAX_FRAME_AGE_S = 1.0

#: The oldest battery reading, in seconds, from a controller's telemetry
#: snapshot (a controller without ``read_battery``, such as MAVLink). A
#: working autopilot link refreshes it about once a second.
MAX_BATTERY_AGE_S = 5.0


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


def _battery(drone: Any) -> tuple[float | None, str | None]:
    """``(percent, None)`` with the drone's battery as the edge would report it, else ``(None, why)``.

    A real 0 % is 0, not "no report": ``read_battery()`` returns None when
    it knows nothing, and the legacy state cache's 0 for "unknown" never
    becomes a snapshot reading (``snapshot_from_state``).
    """
    reader = getattr(drone, "read_battery", None)
    if callable(reader):
        try:
            value = reader()
        except Exception:  # noqa: BLE001 - an unreadable battery is an unknown battery
            return None, "no battery report from the drone"
    else:
        reading = read_telemetry(drone).battery_pct
        if reading is None:
            return None, "no battery report from the drone"
        age = reading.age_s()
        if age > MAX_BATTERY_AGE_S:
            return None, (f"the drone's last battery report is {age:.0f} s old; wait for a fresh report, "
                          "or check the drone's telemetry link")
        value = reading.value
    number = _number(value)
    if number is None or number < 0:
        return None, "no battery report from the drone"
    return number, None


def _critical_battery_pct() -> float:
    """The battery monitor's critical level (``battery_critical_threshold``), which lands a flying drone at once."""
    try:
        return float(get_config().drone.battery_critical_threshold)
    except Exception:  # noqa: BLE001 - an unreadable config falls back to the default level
        return float(DroneConfig().battery_critical_threshold)


def _video_problem(drone: Any) -> str | None:
    """Why the drone gives no current video frame for the obstacle guard, or None when it does."""
    try:
        frame = drone.get_frame()
    except Exception as exc:  # noqa: BLE001 - no view means no obstacle check
        return f"the drone's video is unreadable ({type(exc).__name__}), so the obstacle guard cannot check the path"
    if frame is None:
        return "no video frame from the drone, so the obstacle guard cannot check the path"
    probe = getattr(drone, "frame_freshness", None)
    if not callable(probe):
        return None
    try:
        fresh = probe()
    except Exception as exc:  # noqa: BLE001 - an unknown frame age is not a current frame
        return (f"the age of the drone's video frame is unreadable ({type(exc).__name__}), so the obstacle guard "
                "cannot check the path")
    if not isinstance(fresh, FrameFreshness):
        return "the age of the drone's video frame is unreadable, so the obstacle guard cannot check the path"
    if not fresh.decoded:
        return ("no video frame decoded from the drone yet, so the obstacle guard cannot check the path; wait for "
                "its video to show, then start again")
    if fresh.stale_cause:
        cause = _printable(fresh.stale_cause, "stale")
        return (f"the drone's newest video frame cannot be trusted ({cause}), so the obstacle guard cannot check "
                "the path; wait for its video to recover, then start again")
    age = _number(fresh.age_s)
    if age is None:
        return "the age of the drone's video frame is unknown, so the obstacle guard cannot check the path"
    if age > MAX_FRAME_AGE_S:
        return (f"the drone's newest video frame is {age:.1f} s old (at most {MAX_FRAME_AGE_S:g} s is allowed), so "
                "the obstacle guard cannot check the path; wait for its video to recover, then start again")
    return None


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
    battery, unknown = _battery(drone)
    if battery is None:
        return unknown
    if battery < min_battery:
        return f"battery {battery:g}% is below the {min_battery:g}% needed to start a search; charge or swap it"
    critical = _critical_battery_pct()
    if battery <= critical:
        return (f"battery {battery:g}% is at or below the {critical:g}% critical level, which lands the drone at "
                "once; charge or swap it")
    return _video_problem(drone)
