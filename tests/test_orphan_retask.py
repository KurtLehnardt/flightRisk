"""The lanes of a drone the orphan countdown landed wait for the operator's confirmation, then are re-tasked.

Before, a drone landed by the ground because nobody controlled it (#184)
stayed held on the ground, was never declared lost, and its unflown lanes sat
unsearched and unlisted until someone pressed Resume search. Now it is
declared lost once it reports landed with lanes left, its lanes are listed
as ``needs_confirmation`` and offered to no drone until the operator
confirms it (``confirm_retask``), as a person cut's re-flight is. The same
holds for a held drone that lands by itself while its countdown runs (a
battery landing). None of its unflown lanes is given to another drone
unless a person confirms it. A lost drone never resumes its search, and one
whose link comes back while it flies is sent ``land`` again and blocks the
confirmation until it has landed.

Uses the fake ground transport of ``tests/test_fleet_dashboard.py``; nothing
connects to an edge or a drone.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import (
    ORPHAN_LANDED_CONFIRM,
    ORPHAN_LOST_CONFIRM,
    ORPHAN_SELF_LANDED_CONFIRM,
    app,
    socketio,
)
from flightrisk.dashboard.manual_control import (
    ORPHAN_LANDED_BY_GROUND,
    ORPHAN_LANDED_ITSELF,
)
from flightrisk.dashboard.state import app_state
from flightrisk.drone.coordinator import LOST_OBSTACLE_S, TELEMETRY_FRESH_S
from tests.test_coordinator import make_fleet
from tests.test_fleet_dashboard import (
    _started_supervisor,
    _supervisor,
    _telemetry,
    _wait_for,
)
from tests.test_manual_ground import _manual_ready, _orphan_lands, _sent

HELD = "drone-2"
OTHER = "drone-1"
NOTHING_WAITING = "drone-2 has no lanes waiting for confirmation; the NOT SEARCHED list shows what happened to its lanes"


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
    # Resume search is refused: the landed drone is lost already, so its lanes are no longer its own.
    refused = supervisor.manual.resume_search(HELD)
    assert refused["ok"] is False and refused["reason"] == "lost"
    assert _sent(transport, HELD, fp.RESUME_SEARCH) == []
    # Even an edge that reports autonomous control again on the ground changes nothing.
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
    assert not ok and detail == NOTHING_WAITING


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
    supervisor, transport, _sio, _clock = _started_supervisor()
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
    assert supervisor.manual.orphan_landing(HELD) == ORPHAN_LANDED_BY_GROUND
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
    supervisor, _transport, sio, clock = _started_supervisor()
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
    assert not ok and detail == NOTHING_WAITING


def test_land_all_fails_the_lanes_that_wait_for_confirmation():
    coord, edges, events = _started_fleet()
    coord.on_drone_lost("drone-2", wait_s=0, confirm=ORPHAN_LANDED_CONFIRM)
    before = len(_assigned(edges))
    coord.land_all()
    ok, detail = coord.confirm_retask("drone-2")
    assert not ok
    assert detail == "LAND ALL was pressed, so nothing is re-tasked; make a new plan to search these lanes"
    assert len(_assigned(edges)) == before
    uncovered = coord.current_plan()["uncovered"]
    assert uncovered and all(u.get("status") != "needs_confirmation" for u in uncovered)
    assert all(u["reason"] == "LAND ALL issued" for u in uncovered)
    # The browser is told the lanes are uncovered now.
    [failed] = [p for name, p in events if name == "fleet_event" and p["event"] == "retask_failed"]
    assert sorted(lane["index"] for lane in failed["lanes"]) == [2, 3, 4]
    ok, detail = coord.confirm_retask("drone-2")
    assert not ok and detail == NOTHING_WAITING


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


def _ui_js() -> str:
    return app_module.Path(app_module.__file__).with_name("static").joinpath("ui", "ui.js").read_text()


def test_the_dashboard_offers_one_re_task_button_per_drone_for_lanes_waiting_for_confirmation():
    js = _ui_js()
    render = js[js.index("function renderUncovered()"):js.index("const CONFIRM_ARM_MS")]
    # One button per drone, on its own row, not one per lane.
    assert "if (!e.seg && e.status === 'needs_confirmation') {" in render
    assert "const row = el('div', 'retask-confirm-row'" in render and "row.appendChild(retaskConfirmButton(d));" in render
    assert "retaskConfirmButton(e)" not in render
    button = js[js.index("function retaskConfirmButton(d)"):js.index("socket.on('retask_confirm_result'")]
    assert "'retask-confirm'" in button and "socket.emit('confirm_retask', { drone_id: d.lost_id })" in button
    css = app_module.Path(app_module.__file__).with_name("static").joinpath("ui", "ui.css").read_text()
    assert ".uncovered .retask-confirm {" in css and ".uncovered .retask-confirm-row {" in css


def test_the_not_searched_header_counts_the_lanes_waiting_for_confirmation_on_their_own():
    render = _ui_js()[_ui_js().index("function renderUncovered()"):_ui_js().index("const CONFIRM_ARM_MS")]
    assert "const confirming = keys.filter((k) => uncoveredLanes[k].status === 'needs_confirmation').length;" in render
    assert "' waiting for your confirmation'" in render
    assert "const failed = keys.length - awaiting - confirming;" in render


def test_confirmations_never_use_a_blocking_dialog():
    """window.confirm would block the page, and with it LAND ALL and Shift+L, while drones fly."""
    js = _ui_js()
    confirms = js[js.index("const CONFIRM_ARM_MS"):js.index("socket.on('retask_confirm_result'")]
    assert "window.confirm" not in confirms
    assert "function twoStepConfirmButton(key, cls, label, armedLabel, warning, send)" in confirms
    assert "reflightConfirmButton(e)" in confirms and "retaskConfirmButton(d)" in confirms


def test_a_healthy_grounded_drone_is_not_announced_as_lost():
    js = _ui_js()
    assert "showFleetBanner('DRONE LOST:' + who)" not in js
    assert "} else if (kind === 'drone_lost') showFleetBanner(droneLostText(ev.drone_id));" in js
    text = js[js.index("function droneLostText(id)"):js.index("socket.on('fleet_event'")]
    assert "d.connected === true && d.is_flying === false" in text and "'OUT OF THE SEARCH: '" in text
    events = js[js.index("socket.on('fleet_event'"):]
    assert "kind === 'retask_needs_confirmation'" in events
    assert "markLanes(ev.drone_id, ev.lanes, 'needs_confirmation');" in events
    # A reloaded page shows them waiting for confirmation, not as uncovered.
    assert "u.status === 'needs_confirmation' ? 'needs_confirmation' : 'uncovered'" in js


# ---------------------------------------------------------------------------
# A held drone that lands by itself while its countdown runs (review round 1, B1)
# ---------------------------------------------------------------------------


def _held_with_no_controller():
    """A started mission whose HELD drone flies its lane 1, held, after its controller went away."""
    supervisor, transport, sio, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    lanes = len(supervisor.coordinator.lanes(HELD))
    supervisor.manual.take_control(HELD, sid="browser-1")
    assert supervisor.manual.controller_disconnected("browser-1") == HELD
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=True, lane_index=1, lanes_total=lanes),
                                   control_mode="held"))
    assert supervisor.manual.orphaned(HELD)
    return supervisor, transport, sio, clock, lanes


def _battery_landing(supervisor, clock, lanes, after_s=60):
    """The edge lands HELD on its own (a battery landing) *after_s* into the countdown; it stays held."""
    clock.now += after_s
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes, battery=9),
                                   control_mode="held"))


def test_a_battery_landing_during_the_countdown_waits_for_the_operator():
    supervisor, transport, sio, clock, lanes = _held_with_no_controller()
    _battery_landing(supervisor, clock, lanes)
    assert supervisor.manual.orphan_landing(HELD) == ORPHAN_LANDED_ITSELF
    assert supervisor.manual.drone_status(HELD)["orphan_land_in_s"] is None  # the countdown is over
    offers_before = len(_offers(transport))
    assert supervisor.check_losses() == [HELD]
    assert len(_offers(transport)) == offers_before
    waiting = _waiting(supervisor)
    assert sorted(u["index"] for u in waiting) == list(range(1, lanes))
    assert {u["reason"] for u in waiting} == {ORPHAN_SELF_LANDED_CONFIRM}
    [event] = _events(sio, "retask_needs_confirmation")
    assert ORPHAN_SELF_LANDED_CONFIRM in event["detail"]
    assert _events(sio, "retask") == []
    # The ground sent no landing of its own: the drone was already down.
    assert _orphan_lands(transport, HELD) == []


def test_resume_search_from_the_phone_after_a_battery_landing_re_tasks_nothing(clean_app_state):
    supervisor, transport, _sio, clock, lanes = _held_with_no_controller()
    _battery_landing(supervisor, clock, lanes)
    supervisor.check_losses()
    offers_before = len(_offers(transport))
    app_state.fleet = None
    app_state.fleet_supervisor = supervisor
    phone = socketio.test_client(app)
    try:
        phone.get_received()
        phone.emit("manual_resume_search", {"drone_id": HELD})
        received = phone.get_received()
    finally:
        phone.disconnect()
    acks = [r["args"][0] for r in received if r["name"] == "manual_ack"]
    assert acks and acks[0]["ok"] is False and acks[0]["reason"] == "lost"
    assert "land it, then make a new plan" in acks[0]["detail"]
    assert _sent(transport, HELD, fp.RESUME_SEARCH) == []
    clock.now += 30
    assert supervisor.check_losses() == []
    assert len(_offers(transport)) == offers_before
    assert len(_waiting(supervisor)) == lanes - 1


def test_resume_search_pressed_before_the_loss_check_still_re_tasks_nothing():
    """The order of the critic's probe: landed, Resume search, and only then the next monitor tick."""
    supervisor, transport, sio, clock, lanes = _held_with_no_controller()
    _battery_landing(supervisor, clock, lanes)
    offers_before = len(_offers(transport))
    assert supervisor.manual.resume_search(HELD)["ok"] is True  # not lost yet
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="autonomous"))
    assert supervisor.check_losses() == [HELD]
    assert len(_offers(transport)) == offers_before
    assert {u["reason"] for u in _waiting(supervisor)} == {ORPHAN_SELF_LANDED_CONFIRM}
    assert _events(sio, "retask") == []


def test_critic_probe_self_landed_during_countdown_then_resume():
    """The round-1 probe, as a regression test: nothing is re-tasked without the operator's confirmation."""
    supervisor, transport, sio, clock, lanes = _held_with_no_controller()
    clock.now += 60
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="held"))
    assert supervisor.check_losses() == [HELD]
    before = len(_sent(transport, OTHER, "assign_lanes"))
    supervisor.manual.resume_search(HELD)
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="autonomous"))
    assert supervisor.check_losses() == []
    assert len(_sent(transport, OTHER, "assign_lanes")) == before
    assert len(_waiting(supervisor)) == lanes - 1
    kinds = [p.get("event") for p in sio.named("fleet_event")]
    assert "retask_needs_confirmation" in kinds and "retask" not in kinds


# ---------------------------------------------------------------------------
# The orphan-landing mark ends when the drone flies again (review round 1, item 5)
# ---------------------------------------------------------------------------


def test_the_orphan_landing_mark_ends_when_the_drone_flies_again_after_its_landing():
    supervisor, _transport, _sio, clock, lanes = _held_with_no_controller()
    clock.now += supervisor.manual.orphan_land_s + 1
    assert supervisor.manual.check_orphans() == [HELD]
    # Telemetry sent before the landing took effect still says flying: the mark stays.
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=True, lane_index=lanes, lanes_total=lanes),
                                   control_mode="held"))
    assert supervisor.manual.orphan_landing(HELD) == ORPHAN_LANDED_BY_GROUND
    # The landing is seen...
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=lanes, lanes_total=lanes),
                                   control_mode="held"))
    assert supervisor.manual.orphan_landed(HELD)
    # ...and a person takes control and flies it again: it is no longer orphan-landed.
    supervisor.manual.take_control(HELD, sid="browser-2")
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=True, lane_index=lanes, lanes_total=lanes),
                                   control_mode="manual"))
    assert supervisor.manual.orphan_landing(HELD) is None and not supervisor.manual.orphaned(HELD)
    # A later loss of its link is an ordinary loss of a drone under control, with no orphan reason.
    supervisor.manual.controller_disconnected("browser-2")
    assert supervisor.manual.orphaned(HELD)  # a new countdown, from this disconnect
    supervisor.manual.cancel_orphan(HELD, "test")
    assert not supervisor.manual.orphaned(HELD)


def test_a_self_landed_drone_that_flies_again_loses_its_mark():
    supervisor, _transport, _sio, clock, lanes = _held_with_no_controller()
    _battery_landing(supervisor, clock, lanes)
    assert supervisor.manual.orphan_landing(HELD) == ORPHAN_LANDED_ITSELF
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=True, lane_index=1, lanes_total=lanes),
                                   control_mode="held"))
    assert supervisor.manual.orphan_landing(HELD) is None


# ---------------------------------------------------------------------------
# A lost drone whose link comes back (review round 1, item 3)
# ---------------------------------------------------------------------------


def _lost_while_held_then_back():
    """HELD is lost for a link blip while held with no controller, and comes back still hovering."""
    supervisor, transport, sio, clock, lanes = _held_with_no_controller()
    transport.connected[HELD] = False  # shorter than the edge's own 3 s link-loss landing
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    return supervisor, transport, sio, clock, lanes


def _plain_lands(transport, drone_id=HELD):
    return [m for m in _sent(transport, drone_id, "land") if "reason" not in m]


def test_critic_probe_a_lost_drone_back_online_is_landed_and_cannot_resume_or_be_confirmed():
    supervisor, transport, _sio, _clock, lanes = _lost_while_held_then_back()
    lands_before = len(_plain_lands(transport))
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=True, lane_index=1, lanes_total=lanes),
                                   control_mode="held"))
    # Its link is back and it is flying: it is sent land again, once.
    assert len(_plain_lands(transport)) == lands_before + 1
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=True, lane_index=1, lanes_total=lanes),
                                   control_mode="held"))
    assert len(_plain_lands(transport)) == lands_before + 1
    # The operator returns: Resume search is refused, so it cannot fly lanes 1..n itself.
    supervisor.manual.take_control(HELD, sid="browser-2")
    refused = supervisor.manual.resume_search(HELD)
    assert refused["ok"] is False and refused["reason"] == "lost"
    assert _sent(transport, HELD, fp.RESUME_SEARCH) == []
    # Re-task is refused while it flies, and nothing is offered.
    offers_before = len(_offers(transport))
    ok, detail = supervisor.confirm_retask(HELD)
    assert not ok and detail == "Land drone-2 first; it is flying again. Confirm the re-task once it reports landed."
    assert len(_offers(transport)) == offers_before
    assert len(_waiting(supervisor)) == lanes - 1
    # Once it reports landed, the operator may confirm.
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="manual"))
    ok, detail = supervisor.confirm_retask(HELD)
    assert ok and "offered to drone-1" in detail


def test_a_lost_drone_that_comes_back_again_after_a_gap_is_sent_land_again():
    supervisor, transport, _sio, clock, lanes = _lost_while_held_then_back()
    flying = dict(_telemetry(HELD, is_flying=True, lane_index=1, lanes_total=lanes), control_mode="held")
    supervisor.handle_control(flying)
    count = len(_plain_lands(transport))
    clock.now += TELEMETRY_FRESH_S + 1  # silent again, then back
    supervisor.handle_control(flying)
    assert len(_plain_lands(transport)) == count + 1


def test_a_lost_drone_reporting_autonomous_control_blocks_the_confirmation():
    supervisor, _transport, _sio, _clock, lanes = _lost_while_held_then_back()
    supervisor.handle_control(dict(_telemetry(HELD, is_flying=False, lane_index=1, lanes_total=lanes),
                                   control_mode="autonomous"))
    ok, detail = supervisor.confirm_retask(HELD)
    assert not ok and detail.startswith("drone-2 reports autonomous control again, so it may fly these lanes itself")
    assert "take control of it and land it" in detail


def test_switching_control_away_from_a_lost_drone_does_not_resume_it():
    supervisor, transport, _sio, _clock, _lanes = _lost_while_held_then_back()
    supervisor.manual.take_control(HELD, sid="browser-2")
    supervisor.manual.take_control(OTHER, sid="browser-2")
    assert _sent(transport, HELD, fp.RESUME_SEARCH) == []
    assert _sent(transport, OTHER, fp.MANUAL_HOLD)


def test_a_flying_lost_drone_keeps_its_place_for_separation_after_the_lost_obstacle_time():
    coord, edges, _events = _started_fleet()
    edges["drone-2"].telemetry(lane_index=2, is_flying=True, rel_x_cm=40, rel_y_cm=60)
    coord.on_drone_lost("drone-2", wait_s=0, confirm=ORPHAN_LANDED_CONFIRM)
    edges["drone-2"].clock.advance(LOST_OBSTACLE_S + 5)
    with coord._lock:
        assert coord._lost_obstacle_locked("drone-2") is None  # silent: its last place no longer counts
    edges["drone-2"].telemetry(lane_index=2, is_flying=True, rel_x_cm=40, rel_y_cm=60)
    with coord._lock:
        place = coord._lost_obstacle_locked("drone-2")
        state = coord._drones["drone-2"]
        origin = coord._origin(state)
    assert place == (origin[0] + 40, origin[1] + 60)
    # Silent again while flying: it still counts until LOST_OBSTACLE_S after that last report.
    edges["drone-2"].clock.advance(LOST_OBSTACLE_S - 1)
    with coord._lock:
        assert coord._lost_obstacle_locked("drone-2") == place
    edges["drone-2"].clock.advance(2)
    with coord._lock:
        assert coord._lost_obstacle_locked("drone-2") is None
    # Landed, it no longer counts.
    edges["drone-2"].telemetry(lane_index=2, is_flying=False, rel_x_cm=40, rel_y_cm=60)
    with coord._lock:
        assert coord._lost_obstacle_locked("drone-2") is None


def test_the_coordinator_resends_land_to_a_lost_drone_that_reports_flying():
    coord, edges, _events = _started_fleet()
    coord.on_drone_lost("drone-2", wait_s=0, confirm=ORPHAN_LANDED_CONFIRM)
    assert edges["drone-2"].of_type("land") == [{"type": "land"}]
    edges["drone-2"].telemetry(lane_index=2, is_flying=False)
    assert len(edges["drone-2"].of_type("land")) == 1  # on the ground: nothing more
    edges["drone-2"].telemetry(lane_index=2, is_flying=True)
    assert len(edges["drone-2"].of_type("land")) == 2
    ok, detail = coord.confirm_retask("drone-2")
    assert not ok and detail.startswith("Land drone-2 first; it is flying again.")


# ---------------------------------------------------------------------------
# Waiting lanes are split for the drones there are at confirmation (review round 1, item 6)
# ---------------------------------------------------------------------------


def test_confirmed_lanes_are_split_for_the_drones_available_then():
    coord, edges, _clock, _emitted = make_fleet(n=3, area=(900, 400), strip=50)
    excluded = {"drone-2"}
    coord._retask_excluded = lambda drone_id: drone_id in excluded  # drone-2 held for manual control
    coord.sync()
    coord.plan()
    assert coord.start().ok
    for drone_id in ("drone-1", "drone-2"):
        edges[drone_id].telemetry(lane_index=1, is_flying=True)
    edges["drone-3"].telemetry(lane_index=0, is_flying=False)
    coord.on_drone_lost("drone-3", wait_s=0, confirm=ORPHAN_LANDED_CONFIRM)
    [retask] = coord._retasks
    assert len(retask.chunks) == 1  # one drone could take lanes at the loss
    lanes = sorted(u["index"] for u in coord.current_plan()["uncovered"] if u.get("status") == "needs_confirmation")
    excluded.clear()  # drone-2 is released before the operator confirms
    ok, _detail = coord.confirm_retask("drone-3")
    assert ok
    assert len(retask.chunks) == 2  # split again for the two drones there are now
    assert sorted(e["index"] for c in retask.chunks for e in c.lane_entries()) == lanes
    assert all(not c.confirm for c in retask.chunks)
