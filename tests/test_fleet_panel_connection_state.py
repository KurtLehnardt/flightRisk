"""The fleet panel shows whether each drone's edge link is actually up.

After a link drops the coordinator keeps the drone's last telemetry, so the
status payload carries an explicit ``connected`` flag and the page renders it.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.state import app_state
from tests.test_fleet_dashboard import _supervisor, _telemetry

INDEX = (Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard" / "templates" / "index.html").read_text()


def test_status_payload_marks_dropped_link_disconnected_and_live_link_connected():
    supervisor, transport, _sio, _clock = _supervisor()
    supervisor.plan()
    supervisor.handle_control(_telemetry("drone-1", battery=80))
    supervisor.handle_control(_telemetry("drone-2", battery=70))
    transport.connected["drone-2"] = False
    transport.link_age_s["drone-2"] = 42.0

    drones = supervisor.status_payload()["drones"]

    assert drones["drone-1"]["connected"] is True
    assert drones["drone-2"]["connected"] is False
    assert drones["drone-2"]["link_age_s"] == 42.0
    # The last telemetry stays, and existing fields are kept.
    assert drones["drone-2"]["battery"] == 70
    assert "offset_ms" in drones["drone-2"] and "lost" in drones["drone-2"]


def test_status_payload_treats_unknown_link_as_disconnected():
    supervisor, transport, _sio, _clock = _supervisor()
    transport.fleet_status = MagicMock(side_effect=RuntimeError("bridge down"))

    drones = supervisor.status_payload()["drones"]

    assert drones and all(d["connected"] is False for d in drones.values())


def test_side_list_does_not_depend_on_a_count_key():
    assert "data.count" not in INDEX
    assert "droneIds.length > 1" in INDEX


def test_fleet_panel_has_a_status_column_rendered_with_text_content():
    assert "<th>Status</th>" in INDEX
    for label in ("CONNECTED", "DOWN", "LOST"):
        assert f"text: '{label}'" in INDEX
    body = INDEX[INDEX.index("function renderFleetPanel"):INDEX.index("socket.on('fleet_status', (data) => {\n            const list")]
    assert "innerHTML = ''" in body
    assert not re.search(r"innerHTML\s*\+?=\s*[^'\s]", body)


def test_manual_command_in_fleet_mode_explains_how_to_fly():
    previous = (app_state.fleet, app_state.fleet_supervisor)
    app_state.fleet = None
    app_state.fleet_supervisor = _supervisor()[0]
    try:
        client = socketio.test_client(app)
        client.get_received()
        client.emit("drone_command", {"command": "takeoff"})
        received = client.get_received()
        client.disconnect()
    finally:
        app_state.fleet, app_state.fleet_supervisor = previous

    errors = [r for r in received if r["name"] == "error"]
    assert len(errors) == 1
    message = errors[0]["args"][0]["message"]
    assert "not available in fleet mode" in message
    assert "Plan and Start" in message
    assert "LAND ALL" in message
