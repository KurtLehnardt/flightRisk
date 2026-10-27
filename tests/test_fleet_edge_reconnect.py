"""Tests for the fleet edge's reconnect backoff cap.

A flying edge (``--source tello`` or ``fake``) retries its ground link at
least every 2 s, so it rejoins quickly after a cable or LAN blip instead of
backing off to the transport's 30 s default. Other sources keep that default.
"""

import asyncio

import pytest

from flightrisk.edge_device import _FLEET_RECONNECT_MAX_DELAY_S, _build_parser, _build_transport
from flightrisk.transport import EdgeTransport


@pytest.mark.parametrize("source", ["tello", "fake"])
def test_flying_edge_reconnect_backoff_capped_at_two_seconds(source):
    args = _build_parser().parse_args(["--source", source, "--ws-url", "ws://127.0.0.1:1"])
    transport = _build_transport(args, runner=None, token=None)
    assert _FLEET_RECONNECT_MAX_DELAY_S == 2.0
    assert transport._transport._max_delay == 2.0
    assert transport._transport._base_delay == 1.0
    assert transport._transport._max_retries is None


@pytest.mark.parametrize("argv", [["--source", "camera"], ["--source", "file", "--video", "clip.mp4"]])
def test_non_flying_edge_keeps_default_backoff(argv):
    args = _build_parser().parse_args(argv + ["--ws-url", "ws://127.0.0.1:1"])
    transport = _build_transport(args, runner=None, token=None)
    assert transport._transport._max_delay == 30.0


def test_fleet_backoff_sequence(monkeypatch):
    delays = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def scenario():
        edge = EdgeTransport(ws_url="ws://127.0.0.1:1", max_retries=5, max_delay=_FLEET_RECONNECT_MAX_DELAY_S)
        edge._should_reconnect = True
        assert await edge._reconnect() is False

    asyncio.run(scenario())
    assert delays == [1.0, 2.0, 2.0, 2.0, 2.0]


class _Built(Exception):
    """Raised by the patched transport builder to stop the program right after it."""


def _capture_transport(monkeypatch):
    import flightrisk.edge_device as edge_device

    built = []
    real_build = edge_device._build_transport

    def build(args, runner, token):
        transport = real_build(args, runner, token)
        built.append(transport._transport._max_delay)
        raise _Built

    monkeypatch.setattr(edge_device, "_build_transport", build)
    monkeypatch.setattr(edge_device, "_build_runner", lambda *args, **kwargs: None)
    monkeypatch.setattr(edge_device, "_build_drone", lambda *args, **kwargs: object())
    monkeypatch.setattr(edge_device, "_build_source", lambda *args, **kwargs: iter(()))
    return edge_device, built


@pytest.mark.parametrize(("argv", "expected"), [
    (["--source", "fake"], 2.0),
    (["--source", "tello"], 2.0),
    (["--source", "camera"], 30.0),
])
def test_entry_points_use_the_source_specific_backoff(monkeypatch, argv, expected):
    edge_device, built = _capture_transport(monkeypatch)
    with pytest.raises(_Built):
        edge_device.main(argv + ["--ws-url", "ws://127.0.0.1:1"])
    assert built == [expected]
