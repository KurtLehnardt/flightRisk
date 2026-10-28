"""Ground half of manual takeover: ``ManualControl`` in the fleet supervisor, and its socket events.

Uses the fake ground transport of ``tests/test_fleet_dashboard.py``; nothing
connects to an edge or a drone. Covers single-controller switching, release,
the controller disconnect rule, refusals (unsupported edge, dead drone link),
landing never being gated, reply routing, status, Plan/Start gating, and the
regression that a held drone is never declared lost.
"""
from __future__ import annotations

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.state import app_state
from tests.test_fleet_dashboard import _started_supervisor, _supervisor, _telemetry


def _manual_ready(supervisor, *drone_ids, **fields):
    """Telemetry from each drone's edge saying it takes manual commands."""
    supervisor.manual._background = False  # send inline, so the test sees the order
    for drone_id in drone_ids or tuple(supervisor.links):
        msg = dict(_telemetry(drone_id, is_flying=fields.get("is_flying", True)), control_mode="autonomous")
        msg.update({k: v for k, v in fields.items() if k != "is_flying"})
        supervisor.handle_control(msg)


def _sent(transport, drone_id, mtype=None):
    return [m for d, m in transport.sent if d == drone_id and (mtype is None or m["type"] == mtype)]


def _answer(supervisor, drone_id, msg, ok=True, **fields):
    if ok:
        reply = fp.make_manual_ack(msg["type"], drone_id, msg["cmd_id"], **fields)
    else:
        reply = fp.make_manual_nack(msg["type"], drone_id, msg["cmd_id"], fields.pop("reason", "busy"), **fields)
    supervisor.handle_control(reply)


def test_take_control_holds_the_drone_and_switching_resumes_the_previous_one():
    supervisor, transport, sio, _ = _supervisor()
    _manual_ready(supervisor)
    assert supervisor.manual.take_control("drone-1", sid="a")["ok"]
    assert [m["type"] for m in _sent(transport, "drone-1")] == ["manual_hold"]
    assert supervisor.manual.controlled == "drone-1"

    assert supervisor.manual.take_control("drone-2", sid="a")["ok"]
    assert [m["type"] for m in _sent(transport, "drone-1")] == ["manual_hold", "resume_search"]
    assert [m["type"] for m in _sent(transport, "drone-2")] == ["manual_hold"]
    assert supervisor.manual.controlled == "drone-2"
    assert sio.named("manual_status")[-1]["controlled"] == "drone-2"


def test_taking_control_of_the_controlled_drone_again_does_nothing():
    supervisor, transport, _, _ = _supervisor()
    _manual_ready(supervisor)
    supervisor.manual.take_control("drone-1", sid="a")
    result = supervisor.manual.take_control("drone-1", sid="a")
    assert result["ok"] and result.get("noop")
    assert [m["type"] for m in _sent(transport, "drone-1")] == ["manual_hold"]
    assert supervisor.manual.controlled == "drone-1"


def test_resume_search_releases_only_that_drone():
    supervisor, transport, _, _ = _supervisor()
    _manual_ready(supervisor)
    supervisor.manual.take_control("drone-1")
    assert not supervisor.manual.resume_search("drone-2")["ok"]  # not controlled, not paused
    assert supervisor.manual.controlled == "drone-1"
    assert _sent(transport, "drone-2") == []
    assert supervisor.manual.resume_search("drone-1")["ok"]
    assert supervisor.manual.controlled is None
    assert _sent(transport, "drone-1")[-1]["type"] == "resume_search"


def test_commands_go_only_to_the_controlled_drone_with_bounded_steps():
    supervisor, transport, _, _ = _supervisor()
    _manual_ready(supervisor)
    assert not supervisor.manual.command("drone-1", "forward")["ok"]  # not under control
    supervisor.manual.take_control("drone-1")
    supervisor.manual.command("drone-1", "forward", distance_cm=500)
    supervisor.manual.command("drone-1", "ccw", degrees=3)
    supervisor.manual.command("drone-1", "takeoff")
    moves = _sent(transport, "drone-1")
    assert moves[1] == dict(moves[1], type="manual_move", direction="forward", distance_cm=100)
    assert moves[2]["degrees"] == 5 and moves[2]["direction"] == "ccw"
    assert moves[3]["type"] == "manual_takeoff"


def test_an_edge_without_manual_support_is_refused_clearly():
    supervisor, transport, _sio, _ = _supervisor()
    supervisor.manual._background = False
    supervisor.handle_control(_telemetry("drone-1", is_flying=True))  # no control_mode: an older edge
    result = supervisor.manual.take_control("drone-1")
    assert not result["ok"] and result["reason"] == "unsupported" and "update the edge" in result["detail"]
    assert transport.sent == [] and supervisor.manual.controlled is None


def test_a_dead_drone_link_refuses_steps_but_land_is_never_gated():
    supervisor, transport, _, _ = _supervisor()
    _manual_ready(supervisor, drone_link_ok=False, drone_link_detail="Tello unreachable (no state for 12 s)")
    supervisor.manual.take_control("drone-1")
    result = supervisor.manual.command("drone-1", "left")
    assert not result["ok"] and result["reason"] == "drone_unreachable" and "unreachable" in result["detail"]
    assert supervisor.manual.command("drone-1", "land")["ok"]
    # Land goes to any fleet drone, controlled or not.
    assert supervisor.manual.command("drone-2", "land")["ok"]
    lands = [m for _, m in transport.sent if m["type"] == "land"]
    assert len(lands) == 2 and all(fp.is_manual_land(m) for m in lands)


def test_replies_are_routed_to_manual_control_and_reported():
    supervisor, transport, sio, _ = _supervisor()
    _manual_ready(supervisor)
    supervisor.manual.take_control("drone-1")
    supervisor.manual.command("drone-1", "forward")
    move = _sent(transport, "drone-1")[-1]
    _answer(supervisor, "drone-1", move, ok=False, reason="geofence", detail="outside the 450 cm geofence")
    result = sio.named("manual_result")[-1]
    assert result["ok"] is False and result["reason"] == "geofence" and "450 cm" in result["detail"]
    assert supervisor.status_payload()["drones"]["drone-1"]["manual_last"]["reason"] == "geofence"


def test_a_refused_take_control_releases_the_drone():
    supervisor, transport, _, _ = _supervisor()
    _manual_ready(supervisor)
    supervisor.manual.take_control("drone-1")
    hold = _sent(transport, "drone-1")[-1]
    _answer(supervisor, "drone-1", hold, ok=False, reason="landing")
    assert supervisor.manual.controlled is None


def test_a_command_without_an_answer_times_out():
    supervisor, _transport, sio, clock = _supervisor()
    _manual_ready(supervisor)
    supervisor.manual.take_control("drone-1")
    clock.now += 10
    assert supervisor.manual.check_timeouts() == ["take_control"]
    assert sio.named("manual_result")[-1]["reason"] == "no_reply"


def test_controller_disconnect_keeps_the_drone_held_and_shows_it():
    supervisor, transport, _, _ = _supervisor()
    _manual_ready(supervisor)
    supervisor.manual.take_control("drone-1", sid="browser-1")
    assert supervisor.manual.controller_disconnected("someone-else") is None
    assert supervisor.manual.controller_disconnected("browser-1") == "drone-1"
    assert [m["type"] for m in _sent(transport, "drone-1")] == ["manual_hold"]  # nothing more sent
    supervisor.handle_control(dict(_telemetry("drone-1", is_flying=True), control_mode="held"))
    status = supervisor.status_payload()
    assert status["manual_controlled"] is None
    assert status["drones"]["drone-1"]["control_mode"] == "held"
    # After a reload the operator can resume it without taking control first.
    assert supervisor.manual.resume_search("drone-1")["ok"]


def test_status_payload_reports_each_drone_mode():
    supervisor, _, _, _ = _supervisor()
    _manual_ready(supervisor)
    supervisor.manual.take_control("drone-2")
    drones = supervisor.status_payload()["drones"]
    assert drones["drone-2"]["control_mode"] == "manual" and drones["drone-1"]["control_mode"] == "autonomous"
    assert drones["drone-1"]["manual_supported"] is True


def test_plan_and_start_are_refused_while_a_drone_is_under_manual_control():
    supervisor, _, _, _ = _supervisor()
    _manual_ready(supervisor, is_flying=False)
    supervisor.manual.take_control("drone-1")
    with pytest.raises(RuntimeError, match="manual control"):
        supervisor.plan()
    result = supervisor.start()
    assert not result.ok and "manual control" in result.refusals["drone-1"]


def test_land_all_drops_manual_commands_not_yet_sent():
    supervisor, transport, _, _ = _supervisor()
    _manual_ready(supervisor)
    supervisor.manual._background = True
    # A worker that never runs: the commands stay queued.
    supervisor.manual._worker = type("Alive", (), {"is_alive": lambda self: True})()
    supervisor.manual.take_control("drone-1")
    supervisor.manual.command("drone-1", "takeoff")
    assert supervisor.manual.on_land_all() == 2
    assert transport.sent == []


# ---------------------------------------------------------------------------
# Regression: a held drone is never re-tasked as lost
# ---------------------------------------------------------------------------


def test_a_held_drone_is_not_declared_lost_for_landing_or_not_flying_its_lanes():
    supervisor, transport, _, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control("drone-2")
    lanes = len(supervisor.coordinator.lanes("drone-2"))
    # drone-2 is landed by hand with lanes left, and stays on the ground past the takeoff grace.
    supervisor.handle_control(dict(_telemetry("drone-2", is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="manual"))
    clock.now += 60
    assert supervisor.check_losses() == []
    assert not supervisor.coordinator.status()["drone-2"]["lost"]
    assert [m for m in _sent(transport, "drone-2") if m["type"] == "land" and not fp.is_manual_land(m)] == []


def test_a_held_drone_with_a_lost_link_is_still_lost():
    supervisor, transport, _, _ = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control("drone-2")
    transport.connected["drone-2"] = False
    assert supervisor.check_losses() == ["drone-2"]


def test_after_resume_search_a_landed_drone_is_lost_again_so_its_lanes_are_covered():
    supervisor, _transport, _, _clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control("drone-2")
    lanes = len(supervisor.coordinator.lanes("drone-2"))
    supervisor.handle_control(dict(_telemetry("drone-2", is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="manual"))
    supervisor.manual.resume_search("drone-2")
    supervisor.handle_control(dict(_telemetry("drone-2", is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="autonomous"))
    assert supervisor.check_losses() == ["drone-2"]


# ---------------------------------------------------------------------------
# Socket events
# ---------------------------------------------------------------------------


@pytest.fixture
def fleet_client():
    previous = (app_state.fleet, app_state.fleet_supervisor)
    supervisor, transport, _sio, _clock = _supervisor()
    _manual_ready(supervisor)
    app_state.fleet = None
    app_state.fleet_supervisor = supervisor
    client = socketio.test_client(app)
    client.get_received()
    try:
        yield client, supervisor, transport
    finally:
        if client.is_connected():
            client.disconnect()
        app_state.fleet, app_state.fleet_supervisor = previous


def test_socket_events_take_control_route_drone_command_and_release_on_disconnect(fleet_client):
    client, supervisor, transport = fleet_client
    client.emit("manual_take_control", {"drone_id": "drone-1"})
    assert supervisor.manual.controlled == "drone-1"
    # drone_command (the sidebar Flight Controls) goes to the controlled drone in fleet mode.
    client.emit("drone_command", {"command": "up", "distance": 30})
    assert _sent(transport, "drone-1")[-1]["direction"] == "up"
    client.emit("manual_command", {"drone_id": "drone-1", "command": "land"})
    assert fp.is_manual_land(_sent(transport, "drone-1")[-1])
    client.disconnect()
    assert supervisor.manual.controlled is None  # held, no controller
    assert [m["type"] for m in _sent(transport, "drone-1")] == ["manual_hold", "manual_move", "land"]


def test_land_all_still_works_with_a_drone_under_manual_control(fleet_client):
    client, supervisor, transport = fleet_client
    client.emit("manual_take_control", {"drone_id": "drone-1"})
    assert supervisor.manual.controlled == "drone-1"
    app_state.ground_transport = transport
    try:
        report = app_module._land_all()
    finally:
        app_state.ground_transport = None
    assert report["transport"] == 2 and report["coordinator"] == 2
