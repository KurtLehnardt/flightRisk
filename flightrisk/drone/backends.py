"""Built-in drone backends: the one place that names concrete drone modules.

Each entry registers a backend with :mod:`flightrisk.drone.registry`. The
controller modules are imported lazily inside the factories, so listing the
backends (for example to validate ``--source``) does not pull in djitellopy,
MAVSDK or OpenCV video capture.

To add a drone: write ``flightrisk/drone/<name>.py`` implementing the role
protocols in :mod:`flightrisk.drone.controller`, then add one ``register()``
call below. See ``docs/drone-interface.md``.
"""

from __future__ import annotations

import importlib
from typing import Any, Mapping

from flightrisk.config import get_config
from flightrisk.drone.registry import register


# --- DJI Tello (djitellopy) -------------------------------------------------

def _tello_options(settings: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"host": settings.get("tello_host") or settings.get("host")}


def _tello_host(options: Mapping[str, Any]) -> str:
    return options.get("host") or get_config().drone.tello_default_host


def _create_tello(drone_id: str, host: str, options: Mapping[str, Any]):
    tello = importlib.import_module("flightrisk.drone.tello")
    return tello.TelloController(drone_id, host)


register(
    "tello",
    _create_tello,
    default_host=_tello_host,
    options_from=_tello_options,
    description="DJI Tello over Wi-Fi (djitellopy)",
)


# --- MAVLink (MAVSDK) --------------------------------------------------------

def _mavlink_options(settings: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "address": (
            settings.get("address")
            or settings.get("mavlink_address")
            or get_config().drone.mavlink_default_address
        ),
        "rtsp_url": settings.get("rtsp_url"),
    }


def _mavlink_host(options: Mapping[str, Any]) -> str:
    return options.get("address") or get_config().drone.mavlink_default_address


def _create_mavlink(drone_id: str, host: str, options: Mapping[str, Any]):
    mavlink = importlib.import_module("flightrisk.drone.mavlink")
    return mavlink.MavlinkController(drone_id, host, rtsp_url=options.get("rtsp_url"))


register(
    "mavlink",
    _create_mavlink,
    default_host=_mavlink_host,
    options_from=_mavlink_options,
    description="MAVLink autopilot via MAVSDK, video over RTSP",
)


# --- Simulated drone ---------------------------------------------------------

def _fake_options(settings: Mapping[str, Any]) -> Mapping[str, Any]:
    fake = importlib.import_module("flightrisk.drone.fake")
    return {"config": fake.FakeDroneConfig.from_settings(settings), "host": settings.get("host")}


def _fake_host(options: Mapping[str, Any]) -> str:
    return options.get("host") or "fake://drone"


def _create_fake(drone_id: str, host: str, options: Mapping[str, Any]):
    fake = importlib.import_module("flightrisk.drone.fake")
    return fake.create(drone_id, host, options)


register(
    "fake",
    _create_fake,
    default_host=_fake_host,
    options_from=_fake_options,
    description="Deterministic simulated drone for tests and demos (no hardware)",
)
