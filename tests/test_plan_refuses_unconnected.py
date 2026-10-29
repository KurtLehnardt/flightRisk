"""Plan and Start must not assign any of the search area to a drone that is not ready.

Before this fix, a ground started with ``--fleet drone-1,drone-2`` and only
drone-1 connected still gave drone-2 half of the area and reported full
coverage. These tests cover the supervisor with in-process fakes and one run
with a real ``GroundTransportSync`` and a single fake edge on loopback.

No real drone is involved.
"""

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard.app import FleetSupervisor, SourceConfig  # noqa: F401
from flightrisk.dashboard.state import app_state
from tests.test_fleet_dashboard import (  # noqa: F401
    WAIT_S,
    _started_supervisor,
    _supervisor,
    _telemetry,
    _wait_for,
    edge_pipeline,
)

GUIDANCE = "restart the ground with a fleet list containing only the drones that are connected"


def _ack_all(supervisor):
    for drone_id in supervisor.links:
        lanes = len(supervisor.coordinator.lanes(drone_id))
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, lanes))


def _plan_refusal(sio):
    events = [e for e in sio.named("fleet_event") if e.get("event") == "plan_refused"]
    assert len(events) == 1, sio.named("fleet_event")
    return events[0]["detail"]


def test_plan_is_refused_when_a_configured_drone_is_not_connected():
    supervisor, transport, sio, _clock = _supervisor()
    transport.connected["drone-2"] = False

    with pytest.raises(RuntimeError, match="Plan refused"):
        supervisor.plan()

    detail = _plan_refusal(sio)
    assert "drone-2: not connected" in detail
    assert "drone-1" not in detail
    assert "Connect the listed drones" in detail
    assert GUIDANCE in detail
    assert "launcher does this automatically" in detail
    # Nothing was sent to either drone, no clock sync was tried, and no plan exists.
    assert transport.sent == []
    assert transport.sync_calls == []
    assert sio.named("fleet_plan") == []
    assert supervisor.coordinator.current_plan() is None


def test_plan_refusal_names_every_missing_drone():
    supervisor, transport, sio, _clock = _supervisor(("drone-1", "drone-2", "drone-3"))
    transport.connected["drone-2"] = False
    transport.connected["drone-3"] = False

    with pytest.raises(RuntimeError):
        supervisor.plan()

    detail = _plan_refusal(sio)
    assert "drone-2: not connected" in detail and "drone-3: not connected" in detail


def test_plan_is_refused_when_the_transport_cannot_report_its_links():
    supervisor, transport, _sio, _clock = _supervisor()

    def broken(timeout=None):
        raise TimeoutError("transport busy")

    transport.fleet_status = broken

    with pytest.raises(RuntimeError, match="Plan refused"):
        supervisor.plan()

    assert transport.sent == []
    assert supervisor.coordinator.current_plan() is None


def test_plan_is_refused_for_a_connected_drone_that_has_gone_silent():
    supervisor, transport, sio, _clock = _supervisor()
    transport.link_age_s["drone-2"] = 60.0

    with pytest.raises(RuntimeError, match="Plan refused"):
        supervisor.plan()

    assert "drone-2: connected but silent for 60 s" in _plan_refusal(sio)
    assert transport.sent == []
    assert supervisor.coordinator.current_plan() is None


def test_plan_is_refused_when_a_clock_sync_fails_and_no_lanes_are_sent():
    supervisor, transport, sio, _clock = _supervisor()
    real_sync = transport.sync_clock

    def sync(drone_id, *args, **kwargs):
        if drone_id == "drone-2":
            raise TimeoutError("no pong received")
        return real_sync(drone_id, *args, **kwargs)

    transport.sync_clock = sync

    with pytest.raises(RuntimeError, match="Plan refused"):
        supervisor.plan()

    assert "drone-2: clock not synced" in _plan_refusal(sio)
    assert "assign_lanes" not in transport.sent_types("drone-1")
    assert "assign_lanes" not in transport.sent_types("drone-2")
    assert supervisor.coordinator.current_plan() is None
    assert sio.named("fleet_plan") == []


def test_plan_with_every_drone_connected_is_unchanged():
    supervisor, transport, sio, _clock = _supervisor()

    supervisor.plan()

    plan = sio.named("fleet_plan")[-1]
    assert [(z["drone_id"], z["x_cm"], z["w_cm"], len(z["lanes"]), z["lost"]) for z in plan["zones"]] == [
        ("drone-1", 0, 150, 3, False),
        ("drone-2", 250, 150, 3, False),
    ]
    assert plan["uncovered"] == []
    assert [e for e in sio.named("fleet_event") if e.get("event") == "plan_refused"] == []
    for drone_id in ("drone-1", "drone-2"):
        assert transport.sent_types(drone_id).count("assign_lanes") == 1


def test_a_fleet_of_one_connected_drone_gets_the_whole_area():
    supervisor, _transport, sio, _clock = _supervisor(("drone-1",))

    supervisor.plan()

    (zone,) = sio.named("fleet_plan")[-1]["zones"]
    assert (zone["drone_id"], zone["x_cm"], zone["w_cm"], zone["h_cm"], len(zone["lanes"])) == (
        "drone-1", 0, 400, 400, 9,
    )


def test_start_is_refused_when_a_planned_drone_disconnects_after_the_plan():
    supervisor, transport, sio, _clock = _supervisor()
    supervisor.plan()
    _ack_all(supervisor)
    transport.connected["drone-2"] = False
    sent_before = len(transport.sent)

    result = supervisor.start()

    assert not result.ok
    assert list(result.refusals) == ["drone-2"]
    assert result.refusals["drone-2"].startswith("not connected")
    events = [e for e in sio.named("fleet_event") if e.get("event") == "start_refused"]
    assert len(events) == 1 and "drone-2" in events[0]["detail"]
    # No start_at, no clock message and no sync attempt reached anyone.
    assert len(transport.sent) == sent_before
    assert "start_at" not in transport.sent_types("drone-1")
    assert supervisor.coordinator.started is False


def test_start_is_refused_for_a_planned_drone_that_went_silent():
    supervisor, transport, _sio, _clock = _supervisor()
    supervisor.plan()
    _ack_all(supervisor)
    transport.link_age_s["drone-1"] = 45.0

    result = supervisor.start()

    assert not result.ok and "drone-1" in result.refusals
    assert "silent" in result.refusals["drone-1"]
    assert "start_at" not in transport.sent_types("drone-1")
    assert "start_at" not in transport.sent_types("drone-2")


def test_start_is_refused_with_clock_not_synced_when_the_sync_at_start_fails():
    supervisor, transport, _sio, _clock = _supervisor()
    supervisor.plan()
    _ack_all(supervisor)
    real_sync = transport.sync_clock

    def sync(drone_id, *args, **kwargs):
        if drone_id == "drone-2":
            raise TimeoutError("no pong received")
        return real_sync(drone_id, *args, **kwargs)

    transport.sync_clock = sync

    result = supervisor.start()

    assert not result.ok
    assert result.refusals == {"drone-2": "clock not synced"}
    assert "start_at" not in transport.sent_types("drone-1")


def test_start_with_every_drone_ready_still_works():
    supervisor, transport, _sio, _clock = _started_supervisor()

    assert supervisor.coordinator.started is True
    for drone_id in ("drone-1", "drone-2"):
        assert "start_at" in transport.sent_types(drone_id)


def test_landing_still_reaches_the_connected_drone_while_the_plan_is_refused():
    supervisor, transport, _sio, _clock = _supervisor()
    transport.connected["drone-2"] = False

    with pytest.raises(RuntimeError, match="Plan refused"):
        supervisor.plan()
    supervisor.land_all()

    assert "emergency_land_all" in transport.sent_types("drone-1")


def test_start_refusal_after_a_disconnect_does_not_block_landing():
    supervisor, transport, _sio, _clock = _supervisor()
    supervisor.plan()
    _ack_all(supervisor)
    transport.connected["drone-2"] = False
    assert not supervisor.start().ok

    supervisor.land_all()

    assert "emergency_land_all" in transport.sent_types("drone-1")


def test_loopback_two_configured_drones_with_one_edge_connected_refuses_the_plan(edge_pipeline):  # noqa: F811
    e = edge_pipeline
    e.start(fleet_drones=("drone-1", "drone-2"))
    supervisor = app_state.fleet_supervisor
    assert supervisor is not None
    edge = e.connect_edge()
    assert edge.send(_telemetry("drone-1"))
    assert _wait_for(lambda: supervisor.status_payload()["drones"]["drone-1"]["connected"])

    with pytest.raises(RuntimeError, match="Plan refused"):
        supervisor.plan()

    detail = _plan_refusal(e.sio)
    assert "drone-2: not connected" in detail and "drone-1" not in detail
    assert e.sio.named("fleet_plan") == []
    assert supervisor.coordinator.current_plan() is None
