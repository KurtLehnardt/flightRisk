"""Tests for MavlinkController gimbal pitch and aerial frame replay.

No real MAVSDK or MAVLink connection is used; the System and gimbal plugin
are fakes.
"""

from __future__ import annotations

import asyncio
import enum
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import cv2
import numpy as np
import pytest

# Importing the sibling test module first installs its fake mavsdk tree, so the
# mavlink module binds the same fakes whichever test file is collected first.
import tests.test_mavlink  # noqa: F401  (imported for its side effect)
from flightrisk.drone import mavlink as mavlink_mod
from flightrisk.drone.mavlink import MavlinkController


class _ControlMode(enum.Enum):
    NONE = 0
    PRIMARY = 1


class _GimbalMode(enum.Enum):
    YAW_FOLLOW = 0
    YAW_LOCK = 1


class _SendMode(enum.Enum):
    ONCE = 0
    STREAM = 1


@pytest.fixture
def fake_gimbal_module(monkeypatch):
    module = types.ModuleType("mavsdk.gimbal")
    module.ControlMode = _ControlMode
    module.GimbalMode = _GimbalMode
    module.SendMode = _SendMode
    monkeypatch.setitem(sys.modules, "mavsdk.gimbal", module)
    return module


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(mavlink_mod, "HAS_MAVSDK", True)
    ctrl = MavlinkController(name="gimbal-test", host="udp://:14540")
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    ctrl._loop = loop
    ctrl._loop_thread = thread
    ctrl._running = True
    yield ctrl
    ctrl._running = False
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2.0)
    loop.close()
    ctrl._loop = None


def test_set_gimbal_pitch_calls_set_angles(controller, fake_gimbal_module):
    system = MagicMock()
    system.gimbal.take_control = AsyncMock()
    system.gimbal.set_angles = AsyncMock()
    controller._system = system

    controller.set_gimbal_pitch(-45.0)

    system.gimbal.take_control.assert_awaited_once_with(0, _ControlMode.PRIMARY)
    system.gimbal.set_angles.assert_awaited_once_with(
        0, 0.0, -45.0, 0.0, _GimbalMode.YAW_FOLLOW, _SendMode.ONCE
    )


def test_set_gimbal_pitch_requires_connection(controller, fake_gimbal_module):
    controller._system = None
    with pytest.raises(RuntimeError):
        controller.set_gimbal_pitch(-90.0)


def test_frame_replay_from_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(mavlink_mod, "HAS_MAVSDK", True)
    for i, name in enumerate(["b.png", "c.png", "a.png"]):
        cv2.imwrite(str(tmp_path / name), np.full((8, 8, 3), 40 * (i + 1), dtype=np.uint8))
    ctrl = MavlinkController(name="replay-test", host="udp://:14540", rtsp_url=f"dir://{tmp_path}")

    frames = [ctrl.get_frame() for _ in range(7)]

    assert all(f is not None for f in frames)
    # Sorted order a, b, c (values 120, 40, 80), looping.
    values = [int(f[0, 0, 0]) for f in frames]
    assert values == [120, 40, 80, 120, 40, 80, 120]
    assert ctrl._cap is None


def test_frame_replay_missing_directory_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mavlink_mod, "HAS_MAVSDK", True)
    ctrl = MavlinkController(
        name="replay-test", host="udp://:14540", rtsp_url=f"dir://{tmp_path / 'nope'}"
    )
    assert ctrl.get_frame() is None
