"""Edge device wiring for several ground stations (``--ground-urls``/``FLIGHTRISK_GROUND_URLS``)."""
from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import pytest

from flightrisk import edge_device
from flightrisk.edge_device import (
    _build_parser,
    _build_transport,
    _control_pump,
    _handle_control,
)
from flightrisk.edge_uplink import EdgeUplink, default_node_id
from flightrisk.observability import StructuredLogger
from flightrisk.transport import EdgeTransportSync


def args_for(argv, monkeypatch, env=None):
    for name in ("FLIGHTRISK_GROUND_URLS", "FLIGHTRISK_FLEET_LEGACY", "FLIGHTRISK_NODE_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    return _build_parser().parse_args(argv)


def test_flying_edge_with_ground_urls_gets_the_uplink(monkeypatch):
    args = args_for(["--source", "fake", "--drone-id", "drone-2", "--node-id", "win"], monkeypatch,
                    {"FLIGHTRISK_GROUND_URLS": "ws://127.0.0.1:9000, ws://10.42.0.1:9000",
                     "FLIGHTRISK_FLEET_LEGACY": "1"})
    transport = _build_transport(args, MagicMock(), "tok")
    assert isinstance(transport, EdgeUplink)
    assert transport.urls == ("ws://127.0.0.1:9000", "ws://10.42.0.1:9000")
    assert transport.authority.node_id == "win" and transport.authority.drone_id == "drone-2"
    assert transport.authority.legacy is True
    assert transport.feeds_watchdog is True


def test_cli_ground_urls_override_and_single_url_path_is_unchanged(monkeypatch):
    args = args_for(["--source", "tello", "--ground-urls", "ws://127.0.0.1:9000"], monkeypatch)
    assert isinstance(_build_transport(args, MagicMock(), None), EdgeUplink)
    plain = args_for(["--source", "fake", "--ws-url", "ws://10.42.0.1:9000"], monkeypatch)
    transport = _build_transport(plain, MagicMock(), None)
    assert isinstance(transport, EdgeTransportSync)
    assert transport._transport._ws_url == "ws://10.42.0.1:9000"
    assert not getattr(transport, "feeds_watchdog", False)


def test_non_flying_sources_ignore_the_ground_list(monkeypatch):
    args = args_for(["--source", "camera"], monkeypatch, {"FLIGHTRISK_GROUND_URLS": "ws://127.0.0.1:9000"})
    assert isinstance(_build_transport(args, MagicMock(), None), EdgeTransportSync)


@pytest.mark.parametrize("argv,env", [
    (["--source", "fake"], {"FLIGHTRISK_GROUND_URLS": "ws://a:1,,ws://b:2"}),
    (["--source", "fake", "--ground-urls", "http://a:1"], {}),
    (["--source", "fake", "--node-id", "bad id"], {}),
])
def test_bad_ground_settings_fail_at_startup(argv, env, monkeypatch):
    for name in ("FLIGHTRISK_GROUND_URLS", "FLIGHTRISK_NODE_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as info:
        edge_device.main(argv)
    assert info.value.code == 2


def test_handle_control_feeds_the_watchdog_only_when_asked():
    flight = MagicMock()
    log = StructuredLogger(component="test")
    _handle_control({"type": "heartbeat", "seq": 1}, MagicMock(), log, flight=flight, feed_watchdog=False)
    flight.note_ground_message.assert_not_called()
    _handle_control({"type": "heartbeat", "seq": 1}, MagicMock(), log, flight=flight)
    flight.note_ground_message.assert_called_once()


def test_control_pump_leaves_the_watchdog_to_an_uplink():
    flight = MagicMock()
    flight.now.return_value = 1.0
    stop = MagicMock()
    stop.is_set.side_effect = [False, True]
    transport = argparse.Namespace(feeds_watchdog=True, recv=lambda recv_timeout: {"type": "heartbeat", "seq": 1})
    _control_pump(transport, MagicMock(), StructuredLogger(component="test"), flight, stop)
    flight.handle_message.assert_called_once()
    flight.note_ground_message.assert_not_called()


def test_default_node_id(monkeypatch):
    assert default_node_id({"FLIGHTRISK_NODE_ID": "mac"}) == "mac"
    monkeypatch.setattr("socket.gethostname", lambda: "Kurt's MacBook Pro.local")
    assert default_node_id({}) == "Kurt-s-MacBook-Pro"
    monkeypatch.setattr("socket.gethostname", lambda: "???")
    assert default_node_id({}) == "edge-node"
