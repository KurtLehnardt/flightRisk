"""Tests for the dashboard's bind address (R-107).

The dashboard binds 127.0.0.1 unless ``--host`` or
``FLIGHTRISK_DASHBOARD_HOST`` names another address, and a non-loopback bind
without ``FLIGHTRISK_API_KEY`` refuses to start before anything else runs.
No server is started: ``socketio.run``, the pipeline set-up and the frame
loop are replaced with recorders.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

import flightrisk.dashboard.__main__ as dashboard_main
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import SourceConfig, app, check_dashboard_bind, resolve_dashboard_host, run_dashboard
from flightrisk.dashboard.state import app_state


@pytest.fixture
def no_server(clean_app_state, monkeypatch):
    """Replace everything ``run_dashboard`` starts; return the ``socketio.run`` mock."""
    monkeypatch.delenv("FLIGHTRISK_DASHBOARD_HOST", raising=False)
    init = MagicMock()
    monkeypatch.setattr(app_module, "_init_pipeline", init)
    monkeypatch.setattr(app_module, "_frame_loop", lambda socketio: None)
    run = MagicMock()
    monkeypatch.setattr(app_module.socketio, "run", run)
    run.init_pipeline = init
    return run


def _run_main(argv):
    with patch.object(sys, "argv", ["flightrisk-dashboard"] + argv):
        dashboard_main.main()


def _bound_host(run):
    run.assert_called_once()
    return run.call_args.kwargs["host"]


def test_default_bind_is_loopback(no_server, monkeypatch):
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", None)

    run_dashboard(SourceConfig(source="webcam"), port=5999)
    assert _bound_host(no_server) == "127.0.0.1"
    assert resolve_dashboard_host() == "127.0.0.1"

    # The CLI passes the same default on.
    with patch.object(dashboard_main, "run_dashboard") as mock_run:
        _run_main([])
    assert mock_run.call_args.kwargs["host"] == "127.0.0.1"

    # Every loopback spelling is allowed without a key.
    for host in ("127.0.0.1", "localhost", "::1"):
        check_dashboard_bind(host)


@pytest.mark.parametrize("how", ["argument", "env", "cli"])
def test_non_loopback_without_key_refuses(no_server, monkeypatch, capsys, how):
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", None)

    if how == "cli":
        with patch.object(dashboard_main, "run_dashboard") as mock_run, pytest.raises(SystemExit) as exit_info:
            _run_main(["--host", "0.0.0.0"])
        assert exit_info.value.code == 2
        mock_run.assert_not_called()
        err = capsys.readouterr().err
    else:
        if how == "env":
            monkeypatch.setenv("FLIGHTRISK_DASHBOARD_HOST", "0.0.0.0")
            kwargs = {}
        else:
            kwargs = {"host": "0.0.0.0"}
        with pytest.raises(ValueError) as exc_info:
            run_dashboard(SourceConfig(source="webcam"), port=5999, **kwargs)
        err = str(exc_info.value)
        # Refused before any drone, camera or transport was touched.
        no_server.init_pipeline.assert_not_called()
        no_server.assert_not_called()

    assert "0.0.0.0" in err
    assert "FLIGHTRISK_API_KEY" in err
    assert "non-loopback" in err


def test_non_loopback_with_key_allowed(no_server, monkeypatch):
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", "venue-key")

    run_dashboard(SourceConfig(source="webcam"), port=5999, host="0.0.0.0")
    assert _bound_host(no_server) == "0.0.0.0"

    monkeypatch.setenv("FLIGHTRISK_DASHBOARD_HOST", "192.168.1.20")
    with patch.object(dashboard_main, "run_dashboard") as mock_run:
        _run_main([])
    assert mock_run.call_args.kwargs["host"] == "192.168.1.20"
    with patch.object(dashboard_main, "run_dashboard") as mock_run:
        _run_main(["--host", "10.42.0.1"])
    assert mock_run.call_args.kwargs["host"] == "10.42.0.1"

    # The key gates every Socket.IO client, and a client holding it can
    # still reach LAND ALL.
    app_state.fleet_supervisor = None
    app_state.ground_transport = None
    app_state.fleet = None
    stranger = app_module.socketio.test_client(app)
    assert not stranger.is_connected()
    operator = app_module.socketio.test_client(app, auth={"api_key": "venue-key"})
    assert operator.is_connected()
    operator.get_received()
    operator.emit("land_all")
    acks = [r["args"][0] for r in operator.get_received() if r["name"] == "land_all_ack"]
    operator.disconnect()
    assert len(acks) == 1 and "reached_any" in acks[0]
