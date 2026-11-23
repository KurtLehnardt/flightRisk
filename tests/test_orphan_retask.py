"""The lanes of a drone the orphan countdown landed wait for the operator's confirmation, then are re-tasked.

Before, a drone landed by the ground because nobody controlled it (#184)
stayed held on the ground, was never declared lost, and its unflown lanes sat
unsearched and unlisted until someone pressed Resume search. Now it is
declared lost once it reports landed with lanes left, its lanes are listed
as ``needs_confirmation`` and offered to no drone until the operator
confirms it (``confirm_retask``), as a person cut's re-flight is. No drone
flies into the area it was held over without a person deciding so.

Uses the fake ground transport of ``tests/test_fleet_dashboard.py``; nothing
connects to an edge or a drone.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import ORPHAN_LANDED_CONFIRM, ORPHAN_LOST_CONFIRM, app, socketio
from flightrisk.dashboard.state import app_state
from tests.test_coordinator import make_fleet
from tests.test_fleet_dashboard import _started_supervisor, _supervisor, _telemetry, _wait_for
from tests.test_manual_ground import _manual_ready, _orphan_lands, _sent

HELD = "drone-2"
OTHER = "drone-1"


def _orphan_landed(lane_index=1):
    """A started mission whose HELD drone's controller went away and which the countdown landed with lanes left.

    *lane_index* is the lane it was flying; ``"done"`` means it had finished its lanes.
    """
    supervisor, transport, sio, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    lanes = len(supervisor.coordinator.lanes(HELD))
    if lane_index == "done":
        lane_index = lanes
    supervisor.manual.take_control(HELD, sid="browser-1")
    assert supervisor.manual.controller_disconnected("browser-1") == HELD
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=True, lane_index=lane_index, lanes_total=lanes),
                                   control_mode="held"))
    clock.now += supervisor.manual.orphan_land_s + 1
    assert supervisor.manual.check_orphans() == [HELD]
    assert _orphan_lands(transport, HELD)
    # The edge lands it; manual control stays, so it still reports held, on the ground, with lanes left.
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=lane_index, lanes_total=lanes),
                                   control_mode="held"))
    return supervisor, transport, sio, clock, lanes


def _offers(transport, drone_id=OTHER):
    return _sent(transport, drone_id, "assign_lanes")


def _waiting(supervisor):
    return [u for u in supervisor.coordinator.current_plan()["uncovered"] if u.get("status") == "needs_confirmation"]


def _events(sio, kind):
    return [p for p in sio.named("fleet_event") if p.get("event") == kind]


# ---------------------------------------------------------------------------
# The orphan-landed drone's lanes wait, listed, for the operator
# ---------------------------------------------------------------------------


def test_an_orphan_landed_drone_is_lost_and_its_lanes_wait_for_the_operator():
    supervisor, transport, sio, _clock, lanes = _orphan_landed(lane_index=1)
    offers_before = len(_offers(transport))
    assert supervisor.check_losses() == [HELD]
    assert supervisor.coordinator.status()[HELD]["lost"] is True
    # Nothing is offered to the other drone.
    assert len(_offers(transport)) == offers_before
    # The unflown lanes, from the lane it was flying, are listed with the reason and a status to confirm.
    waiting = _waiting(supervisor)
    assert sorted(u["index"] for u in waiting) == list(range(1, lanes))
    assert all(u["drone_id"] == HELD and u["reason"] == ORPHAN_LANDED_CONFIRM for u in waiting)
    [event] = _events(sio, "retask_needs_confirmation")
    assert event["drone_id"] == HELD and sorted(lane["index"] for lane in event["lanes"]) == list(range(1, lanes))
    assert "only once you confirm it" in event["detail"] and ORPHAN_LANDED_CONFIRM in event["detail"]
    assert _events(sio, "retask") == [] and _events(sio, "retask_failed") == []


def test_waiting_lanes_are_not_offered_by_later_telemetry_resume_search_or_a_lost_link():
    supervisor, transport, sio, clock, lanes = _orphan_landed()
    supervisor.check_losses()
    offers_before = len(_offers(transport))
    # The other drone keeps reporting, flying and then done with its own lanes.
    own = len(supervisor.coordinator.lanes(OTHER))
    supervisor.handle_control(_telemetry(OTHER, is_flying=True, lane_index=own, lanes_total=own))
    # Resume search on the landed drone only releases it; it is lost already.
    supervisor.manual.resume_search(HELD)
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="autonomous"))
    transport.connected[HELD] = False
    clock.now += 60
    assert supervisor.check_losses() == []
    assert len(_offers(transport)) == offers_before
    assert len(_waiting(supervisor)) == lanes - 1
    assert len(_events(sio, "retask_needs_confirmation")) == 1


def test_confirming_offers_the_lanes_to_another_drone():
    supervisor, transport, sio, _clock, lanes = _orphan_landed()
    supervisor.check_losses()
    own = len(supervisor.coordinator.lanes(OTHER))
    ok, detail = supervisor.confirm_retask(HELD)
    assert ok and "offered to drone-1" in detail and f"{lanes - 1} unflown lanes confirmed" in detail
    [offer] = _offers(transport)[-1:]
    assert len(offer["lanes"]) > own  # its own lanes, a transit, then the re-tasked ones
    assert _waiting(supervisor) == []
    [awaiting] = _events(sio, "retask")
    assert sorted(lane["index"] for lane in awaiting["awaiting"]) == list(range(1, lanes))
    # The other drone accepts: the lanes are covered and reported as such.
    supervisor.handle_control(fp.make_ack_assign_lanes(OTHER, 90, len(offer["lanes"])))
    accepted = _events(sio, "retask")[-1]
    assert accepted["to"] and sorted(lane["index"] for lane in accepted["lanes"]) == list(range(1, lanes))
    assert supervisor.coordinator.current_plan()["uncovered"] == []
    # Nothing is left to confirm.
    ok, detail = supervisor.confirm_retask(HELD)
    assert not ok and detail == "drone-2 has no lanes waiting for confirmation"


def test_confirming_after_land_all_is_refused():
    supervisor, transport, _sio, _clock, _lanes = _orphan_landed()
    supervisor.check_losses()
    supervisor.land_all()
    offers_before = len(_offers(transport))
    ok, detail = supervisor.confirm_retask(HELD)
    assert not ok and "LAND ALL" in detail
    assert len(_offers(transport)) == offers_before


def test_a_drone_that_finished_its_lanes_before_the_orphan_landing_is_not_lost():
    supervisor, _transport, sio, _clock, _lanes = _orphan_landed(lane_index="done")
    assert supervisor.check_losses() == []
    assert _events(sio, "retask_needs_confirmation") == []


def test_a_held_drone_lost_while_its_countdown_runs_also_waits_for_the_operator():
    supervisor, transport, sio, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control(HELD, sid="browser-1")
    supervisor.manual.controller_disconnected("browser-1")
    offers_before = len(_offers(transport))
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    assert len(_offers(transport)) == offers_before
    waiting = _waiting(supervisor)
    assert waiting and all(u["reason"] == f"{ORPHAN_LOST_CONFIRM} (disconnected)" for u in waiting)
    # Its countdown is over: it is lost, and no landing is sent for having no controller.
    assert supervisor.manual.drone_status(HELD)["orphan_land_in_s"] is None


def test_a_new_plan_forgets_the_orphan_landings():
    supervisor, _transport, _sio, _clock, _lanes = _orphan_landed()
    assert supervisor.manual.orphan_landed(HELD) and supervisor.manual.orphaned(HELD)
    supervisor.manual.resume_search(HELD)
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False), control_mode="autonomous"))
    supervisor.plan()
    assert not supervisor.manual.orphan_landed(HELD) and not supervisor.manual.orphaned(HELD)


def test_an_orphan_land_that_was_not_delivered_is_not_remembered():
    supervisor, transport, _sio, clock = _supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control(HELD, sid="browser-1")
    supervisor.manual.controller_disconnected("browser-1")
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=True), control_mode="held"))
    transport.connected[HELD] = False
    clock.now += supervisor.manual.orphan_land_s + 1
    supervisor.manual.check_orphans()
    assert not supervisor.manual.orphan_landed(HELD)
    assert supervisor.manual.orphaned(HELD)  # the countdown retries the landing


# ---------------------------------------------------------------------------
# Unchanged paths
# ---------------------------------------------------------------------------


def test_a_hand_landed_held_drone_is_still_not_lost():
    supervisor, transport, sio, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control(HELD)
    lanes = len(supervisor.coordinator.lanes(HELD))
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="manual"))
    clock.now += 60
    assert supervisor.check_losses() == []
    assert _waiting(supervisor) == [] and _events(sio, "retask_needs_confirmation") == []


def test_after_resume_search_a_hand_landed_drone_is_re_tasked_without_confirmation():
    supervisor, transport, sio, _clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control(HELD)
    lanes = len(supervisor.coordinator.lanes(HELD))
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="manual"))
    supervisor.manual.resume_search(HELD)
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="autonomous"))
    offers_before = len(_offers(transport))
    assert supervisor.check_losses() == [HELD]
    assert len(_offers(transport)) == offers_before + 1
    assert _waiting(supervisor) == [] and _events(sio, "retask_needs_confirmation") == []


def test_a_drone_lost_for_its_link_without_manual_control_is_re_tasked_at_once():
    supervisor, transport, sio, _clock = _started_supervisor()
    offers_before = len(_offers(transport))
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    assert len(_offers(transport)) == offers_before + 1
    assert _events(sio, "retask_needs_confirmation") == []


# ---------------------------------------------------------------------------
# The coordinator
# ---------------------------------------------------------------------------


def _started_fleet():
    coord, edges, _clock, events = make_fleet(n=2, area=(400, 400), strip=50)
    coord.sync()
    coord.plan()
    assert coord.start().ok
    edges["drone-1"].telemetry(lane_index=1, is_flying=True)
    edges["drone-2"].telemetry(lane_index=2, is_flying=False)
    return coord, edges, events


def _assigned(edges, drone_id="drone-1"):
    return edges[drone_id].of_type("assign_lanes")


def test_the_coordinator_holds_confirm_lanes_until_confirm_retask():
    coord, edges, events = _started_fleet()
    before = len(_assigned(edges))
    assert coord.on_drone_lost("drone-2", wait_s=5.0, confirm=ORPHAN_LANDED_CONFIRM) == {}
    assert len(_assigned(edges)) == before
    assert coord.status()["drone-2"]["lost"] is True
    assert edges["drone-2"].of_type("land") == [{"type": "land"}]  # best effort, as for any lost drone
    kinds = [p["event"] for name, p in events if name == "fleet_event"]
    assert kinds[-2:] == ["drone_lost", "retask_needs_confirmation"]
    waiting = [u for u in coord.current_plan()["uncovered"] if u.get("status") == "needs_confirmation"]
    assert sorted(u["index"] for u in waiting) == [2, 3, 4]
    assert {u["reason"] for u in waiting} == {ORPHAN_LANDED_CONFIRM}
    # Telemetry from the other drone, which runs a dispatch, offers nothing.
    edges["drone-1"].telemetry(lane_index=2, is_flying=True)
    assert len(_assigned(edges)) == before
    ok, detail = coord.confirm_retask("drone-2")
    assert ok and "offered to drone-1" in detail
    assert len(_assigned(edges)) == before + 1  # FakeEdge accepts it at once
    assert not [u for u in coord.current_plan()["uncovered"] if u.get("status") == "needs_confirmation"]
    accepted = [p for name, p in events if name == "fleet_event" and p["event"] == "retask"][-1]
    assert sorted(lane["index"] for lane in accepted["lanes"]) == [2, 3, 4]


def test_without_confirm_the_lost_drones_lanes_are_offered_at_once():
    coord, edges, events = _started_fleet()
    before = len(_assigned(edges))
    assert set(coord.on_drone_lost("drone-2", wait_s=0)) <= {"drone-1"}
    assert len(_assigned(edges)) == before + 1
    assert [p for name, p in events if p.get("event") == "retask_needs_confirmation"] == []
    ok, detail = coord.confirm_retask("drone-2")
    assert not ok and detail == "drone-2 has no lanes waiting for confirmation"


def test_land_all_fails_the_lanes_that_wait_for_confirmation():
    coord, edges, _events = _started_fleet()
    coord.on_drone_lost("drone-2", wait_s=0, confirm=ORPHAN_LANDED_CONFIRM)
    before = len(_assigned(edges))
    coord.land_all()
    ok, detail = coord.confirm_retask("drone-2")
    assert len(_assigned(edges)) == before
    uncovered = coord.current_plan()["uncovered"]
    assert uncovered and all(u.get("status") != "needs_confirmation" for u in uncovered)
    assert all(u["reason"] == "LAND ALL issued" for u in uncovered)
    assert ok and "no drone can take them: LAND ALL issued" in detail


# ---------------------------------------------------------------------------
# The socket event and the page
# ---------------------------------------------------------------------------


def test_the_confirm_retask_socket_event_answers_the_browser(clean_app_state):
    supervisor, _transport, _sio, _clock = _supervisor()
    supervisor.confirm_retask = MagicMock(return_value=(True, "drone-2's 3 unflown lanes confirmed; offered to drone-1"))
    app_state.fleet = None
    app_state.fleet_supervisor = supervisor
    client = socketio.test_client(app)
    try:
        client.get_received()
        client.emit("confirm_retask", {"drone_id": HELD})
        received: list = []

        def answered():
            received.extend(r["args"][0] for r in client.get_received() if r["name"] == "retask_confirm_result")
            return received

        assert _wait_for(answered)
        assert received[0] == {"drone_id": HELD, "ok": True,
                               "detail": "drone-2's 3 unflown lanes confirmed; offered to drone-1"}
        supervisor.confirm_retask.assert_called_once_with(HELD)
    finally:
        client.disconnect()


def test_confirm_retask_without_a_fleet_is_an_error(clean_app_state):
    app_state.fleet_supervisor = None
    client = socketio.test_client(app)
    try:
        client.get_received()
        client.emit("confirm_retask", {"drone_id": HELD})
        errors = [r["args"][0]["message"] for r in client.get_received() if r["name"] == "error"]
        assert errors == ["Re-tasking needs fleet mode (--source edge --fleet ...)"]
    finally:
        client.disconnect()


def test_the_dashboard_offers_a_re_task_button_only_for_lanes_waiting_for_confirmation():
    js = app_module.Path(app_module.__file__).with_name("static").joinpath("ui", "ui.js").read_text()
    render = js[js.index("function renderUncovered()"):js.index("socket.on('reflight_confirm_result'")]
    assert "if (!e.seg && e.status === 'needs_confirmation') row.appendChild(retaskConfirmButton(e));" in render
    button = js[js.index("function retaskConfirmButton(e)"):js.index("socket.on('retask_confirm_result'")]
    assert "window.confirm(" in button and "socket.emit('confirm_retask', { drone_id: e.lost_id });" in button
    events = js[js.index("socket.on('fleet_event'"):]
    assert "kind === 'retask_needs_confirmation'" in events
    assert "markLanes(ev.drone_id, ev.lanes, 'needs_confirmation');" in events
    # A reloaded page shows them waiting for confirmation, not as uncovered.
    assert "u.status === 'needs_confirmation' ? 'needs_confirmation' : 'uncovered'" in js
