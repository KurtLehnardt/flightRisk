"""Follow-ups to the final review of #200: a lost drone flown by hand, a held drone landing, and Re-task.

* The ground never sends ``land`` again to a lost drone a person flies by
  hand (its telemetry says ``manual``, or the dashboard controls it): a Wi-Fi
  blip shorter than the edge's 3 s link-loss landing declares the drone
  lost, and a ``land`` when the link comes back could put it down in the
  middle of a manoeuvre. A lost drone under autonomous control, or one whose
  telemetry has no control mode, is still landed, and a ``lost_drone_relanded``
  fleet event tells the operator why.
* A drone held with no controller that was already landing gets no orphan
  countdown; when it reports itself on the ground it is marked as landed by
  itself all the same, so its lanes wait for the operator.
* Re-task is refused while the lost drone's last report said it was flying
  and came less than ``LOST_OBSTACLE_S`` ago, not only during the first 2 s.

The P-numbered tests are the critic's probes of the review, adopted as
regression tests. Uses the fake ground transport of
``tests/test_fleet_dashboard.py``; nothing connects to an edge or a drone.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk import obs_events
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard import manual_control
from flightrisk.dashboard.app import ORPHAN_LOST_CONFIRM, ORPHAN_SELF_LANDED_CONFIRM, app, socketio
from flightrisk.dashboard.manual_control import ORPHAN_LANDED_BY_GROUND, ORPHAN_LANDED_ITSELF
from flightrisk.dashboard.state import app_state
from flightrisk.drone.coordinator import LOST_OBSTACLE_S, TELEMETRY_FRESH_S
from tests.test_fleet_dashboard import _started_supervisor, _telemetry
from tests.test_manual_ground import _answer, _manual_ready, _sent
from tests.test_orphan_retask import _started_fleet

HELD = "drone-2"
OTHER = "drone-1"


def _held_no_ctrl():
    """A started mission whose HELD drone flies its lane 1, held, after its controller went away."""
    supervisor, transport, sio, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    lanes = len(supervisor.coordinator.lanes(HELD))
    supervisor.manual.take_control(HELD, sid="browser-1")
    assert supervisor.manual.controller_disconnected("browser-1") == HELD
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))
    assert supervisor.manual.orphaned(HELD)
    return supervisor, transport, sio, clock, lanes


def _report(drone_id, lanes, *, is_flying, mode, lane_index=1, state=None, **fields):
    msg = dict(_telemetry(drone_id, is_flying=is_flying, lane_index=lane_index, lanes_total=lanes, **fields),
               control_mode=mode)
    if state is not None:
        msg["state"] = state
    return msg


def _orphan_lands(transport, drone_id=HELD):
    return [m for m in _sent(transport, drone_id, "land") if m.get("reason") == fp.ORPHAN_LAND_REASON]


def _lost_flown_by_hand():
    """HELD, taken control of and flown by hand, is declared lost for a link blip and comes back still flown by hand."""
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    supervisor.manual.take_control(HELD, sid="browser-2")
    _answer(supervisor, HELD, _sent(transport, HELD, fp.MANUAL_HOLD)[-1])
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual"))
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual"))
    return supervisor, transport, sio, clock, lanes


def _fly_for(supervisor, clock, lanes, seconds, mode="held", step=0.5):
    """HELD reports flying in *mode* every *step* s for *seconds*, with the monitor's orphan and timeout checks."""
    elapsed = 0.0
    while elapsed < seconds:
        clock.now += step
        elapsed += step
        supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode=mode))
        supervisor.manual.check_orphans()
        supervisor.manual.check_timeouts()


def _plain_lands(transport, drone_id=HELD):
    """The coordinator's ``land`` messages (the operator's and the orphan countdown's carry a ``cmd_id``)."""
    return [m for m in _sent(transport, drone_id, "land") if "cmd_id" not in m]


def _relanded(sio):
    return [p for p in sio.named("fleet_event") if p.get("event") == "lost_drone_relanded"]


def _waiting(supervisor):
    return [u for u in supervisor.coordinator.current_plan()["uncovered"] if u.get("status") == "needs_confirmation"]


def _offers(transport, drone_id=OTHER):
    return _sent(transport, drone_id, "assign_lanes")


def _phone_resume(supervisor):
    app_state.fleet = None
    app_state.fleet_supervisor = supervisor
    phone = socketio.test_client(app)
    try:
        phone.get_received()
        phone.emit("manual_resume_search", {"drone_id": HELD})
        received = phone.get_received()
    finally:
        phone.disconnect()
    return [r["args"][0] for r in received if r["name"] == "manual_ack"]


# ---------------------------------------------------------------------------
# 1. A lost drone a person flies by hand is never landed by the ground
# ---------------------------------------------------------------------------


def test_critic_p3_a_lost_drone_flown_by_hand_through_link_blips_is_never_landed():
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    # The operator comes back and flies it by hand; a link blip (shorter than the edge's 3 s watchdog) declares it lost.
    supervisor.manual.take_control(HELD, sid="browser-2")
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual"))
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    before = len(_plain_lands(transport))
    for step in range(40):
        # Telemetry every 0.5 s, and every tenth report the link stalls for 2.5 s (Wi-Fi at range).
        clock.now += 2.5 if step % 10 == 9 else 0.5
        supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual"))
    assert len(_plain_lands(transport)) == before
    assert _relanded(sio) == []
    assert supervisor.manual.controlled == HELD


def test_critic_p3b_a_landed_lost_drone_taken_off_by_hand_is_not_landed_at_once():
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    clock.now += supervisor.manual.orphan_land_s + 1
    supervisor.manual.check_orphans()
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held"))
    assert supervisor.check_losses() == [HELD]
    before = len(_plain_lands(transport))
    supervisor.manual.take_control(HELD, sid="browser-2")
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="manual"))
    assert supervisor.manual.command(HELD, "takeoff", sid="browser-2")["ok"]
    clock.now += 3
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual"))
    assert len(_plain_lands(transport)) == before
    assert _relanded(sio) == []


def test_the_drone_this_dashboard_controls_is_not_landed_before_its_telemetry_says_manual():
    """Telemetry sent before the edge applied the hold still says held: the ground's control counts."""
    supervisor, transport, sio, _clock, lanes = _held_no_ctrl()
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    before = len(_plain_lands(transport))
    supervisor.manual.take_control(HELD, sid="browser-2")
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))
    assert len(_plain_lands(transport)) == before
    assert _relanded(sio) == []


def test_a_hold_that_never_took_does_not_spare_a_lost_drone_flying_autonomously():
    """Taking control while its link is down: the hold never arrives, and the drone flies on autonomously."""
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    supervisor.manual.take_control(HELD, sid="browser-2")  # the manual_hold is never answered
    transport.connected[HELD] = True
    before = len(_plain_lands(transport))
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous"))
    assert len(_plain_lands(transport)) == before  # the dashboard has it: nothing is sent yet
    # The hold times out and the edge still says autonomous: the dashboard lets go of it, and it is landed.
    clock.now += 3.5
    assert supervisor.manual.check_timeouts() == ["take_control"]
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous"))
    assert supervisor.manual.controlled is None
    assert len(_plain_lands(transport)) == before + 1
    assert [e["reason"] for e in _relanded(sio)] == ["flying_again"]


def test_a_lost_drone_flying_autonomously_is_still_landed_and_the_operator_is_told_why():
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    before = len(_plain_lands(transport))
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous", lane_index=2))
    assert len(_plain_lands(transport)) == before + 1
    [event] = _relanded(sio)
    assert event["drone_id"] == HELD and event["reason"] == "flying_again"
    assert event["detail"] == (f"{HELD} was declared lost and reports flying again; the ground sent it land, "
                               "because its unflown lanes are no longer its own")
    # Telemetry that keeps coming sends nothing more; a gap (the link came back once more) sends land again.
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous", lane_index=2))
    assert len(_plain_lands(transport)) == before + 1
    clock.now += TELEMETRY_FRESH_S + 1
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous", lane_index=2))
    assert len(_plain_lands(transport)) == before + 2
    assert [e["reason"] for e in _relanded(sio)] == ["flying_again", "link_back"]


def test_a_lost_drone_with_no_control_mode_is_still_landed_and_announced():
    coord, edges, emitted = _started_fleet()
    coord.on_drone_lost("drone-2", wait_s=0)
    assert len(edges["drone-2"].of_type("land")) == 1
    edges["drone-2"].telemetry(lane_index=2, is_flying=True)  # an edge without manual takeover: no control_mode
    assert len(edges["drone-2"].of_type("land")) == 2
    relanded = [p for name, p in emitted if name == "fleet_event" and p.get("event") == "lost_drone_relanded"]
    assert [(p["drone_id"], p["reason"]) for p in relanded] == [("drone-2", "flying_again")]


def test_after_flying_it_by_hand_the_browser_going_away_does_not_land_it_at_once():
    """The person's browser went away: the drone hovers held, and the orphan countdown lands it, not the coordinator."""
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    supervisor.manual.take_control(HELD, sid="browser-2")
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    before = len(_plain_lands(transport))  # the land sent at the loss, which the dead link dropped
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual"))
    assert supervisor.manual.controller_disconnected("browser-2") == HELD
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))
    assert len(_plain_lands(transport)) == before and _relanded(sio) == []
    assert supervisor.manual.drone_status(HELD)["orphan_land_in_s"] is not None


def test_critic_a_after_a_disconnect_the_countdown_lands_the_lost_drone_and_the_coordinator_sends_nothing():
    supervisor, transport, _sio, clock, lanes = _lost_flown_by_hand()
    plain_before = len(_plain_lands(transport))
    assert supervisor.manual.controller_disconnected("browser-2") == HELD
    _fly_for(supervisor, clock, lanes, supervisor.manual.orphan_land_s - 1)
    assert _orphan_lands(transport) == []
    _fly_for(supervisor, clock, lanes, 2)
    assert len(_orphan_lands(transport)) == 1
    assert len(_plain_lands(transport)) == plain_before
    assert supervisor.manual.orphan_landing(HELD) == ORPHAN_LANDED_BY_GROUND


def test_critic_b_switching_tiles_away_from_the_lost_drone_leaves_it_to_the_countdown():
    supervisor, transport, _sio, clock, lanes = _lost_flown_by_hand()
    plain_before = len(_plain_lands(transport))
    supervisor.manual.take_control(OTHER, sid="browser-2")
    _fly_for(supervisor, clock, lanes, 1.0, mode="manual")  # telemetry from before the switch still says manual
    _fly_for(supervisor, clock, lanes, supervisor.manual.orphan_land_s + 2)
    assert len(_plain_lands(transport)) == plain_before
    assert len(_orphan_lands(transport)) == 1


def test_an_idle_tab_that_controls_a_lost_drone_gets_the_countdown_after_the_idle_time():
    """The tab stays connected but sends nothing: the edge holds the drone, and the ground lands it in the end."""
    supervisor, transport, sio, clock, lanes = _lost_flown_by_hand()
    manual = supervisor.manual
    assert manual.lost_idle_s == manual_control.DEFAULT_LOST_IDLE_S == 30
    plain_before = len(_plain_lands(transport))
    _fly_for(supervisor, clock, lanes, manual.lost_idle_s - 1)
    assert manual.drone_status(HELD)["orphan_land_in_s"] is None
    _fly_for(supervisor, clock, lanes, 1.5)
    assert manual.drone_status(HELD)["orphan_land_in_s"] == manual.orphan_land_s
    assert manual.controlled == HELD
    # Any manual command cancels it, and the idle time starts again from the next held report.
    assert manual.command(HELD, "hover", sid="browser-2")["ok"]
    _answer(supervisor, HELD, _sent(transport, HELD, fp.MANUAL_HOLD)[-1])
    assert manual.drone_status(HELD)["orphan_land_in_s"] is None
    _fly_for(supervisor, clock, lanes, manual.lost_idle_s - 1)
    assert manual.drone_status(HELD)["orphan_land_in_s"] is None
    _fly_for(supervisor, clock, lanes, 1.5)
    assert manual.drone_status(HELD)["orphan_land_in_s"] is not None
    # Left alone, the countdown lands it; the coordinator itself sends nothing.
    _fly_for(supervisor, clock, lanes, manual.orphan_land_s + 1)
    assert len(_orphan_lands(transport)) == 1
    assert len(_plain_lands(transport)) == plain_before and _relanded(sio) == []


def test_a_controlled_drone_that_is_not_lost_gets_no_idle_countdown():
    supervisor, transport, _sio, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    lanes = len(supervisor.coordinator.lanes(HELD))
    supervisor.manual.take_control(HELD, sid="browser-1")
    _answer(supervisor, HELD, _sent(transport, HELD, fp.MANUAL_HOLD)[-1])
    _fly_for(supervisor, clock, lanes, supervisor.manual.lost_idle_s + 60)
    assert supervisor.manual.drone_status(HELD)["orphan_land_in_s"] is None
    assert _orphan_lands(transport) == []


# 30 s by default (DEFAULT_LOST_IDLE_S), never below 5 s (MIN_LOST_IDLE_S).
@pytest.mark.parametrize(("raw", "expected"), [(None, 30.0), ("", 30.0), ("45", 45.0), ("1", 5.0)])
def test_the_lost_idle_time_comes_from_the_environment(raw, expected):
    environ = {} if raw is None else {"FLIGHTRISK_MANUAL_LOST_IDLE_S": raw}
    assert manual_control.manual_lost_idle_s(environ) == expected


@pytest.mark.parametrize("raw", ["soon", "nan", "inf"])
def test_a_lost_idle_time_that_is_not_a_number_is_an_error(raw):
    with pytest.raises(ValueError, match="FLIGHTRISK_MANUAL_LOST_IDLE_S"):
        manual_control.manual_lost_idle_s({"FLIGHTRISK_MANUAL_LOST_IDLE_S": raw})


def test_critic_d_an_edge_that_leaves_manual_control_while_a_hold_waits_is_landed_once_the_dashboard_lets_go():
    supervisor, transport, _sio, clock, lanes = _lost_flown_by_hand()
    plain_before = len(_plain_lands(transport))
    assert supervisor.manual.command(HELD, "hover", sid="browser-2")["ok"]  # a manual_hold now waits for its answer
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous"))  # the edge left manual control
    assert supervisor.manual.controlled == HELD
    assert len(_plain_lands(transport)) == plain_before
    for _ in range(8):  # the hold waits up to 4 s for its answer
        clock.now += 0.5
        supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous"))
        supervisor.manual.check_timeouts()
    for _ in range(20):
        clock.now += 0.5
        supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous"))
    assert supervisor.manual.controlled is None
    assert len(_plain_lands(transport)) == plain_before + 1


def test_a_lost_drone_that_leaves_manual_control_for_autonomous_in_the_air_is_landed():
    """Its edge went back to its search by itself, with nobody controlling it: it would fly lanes now given away."""
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual"))
    before = len(_plain_lands(transport))
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="autonomous"))
    assert len(_plain_lands(transport)) == before + 1
    assert [e["reason"] for e in _relanded(sio)] == ["autonomous_again"]


def test_a_land_that_could_not_be_sent_is_tried_again_on_the_next_report():
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    # The fake transport records every attempt, delivered or not.
    before = len(_plain_lands(transport))
    # Telemetry arrives while the ground's send path to the drone is still down: the land is not delivered.
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))
    assert len(_plain_lands(transport)) == before + 1
    assert _relanded(sio) == []
    with supervisor.coordinator._lock:
        assert supervisor.coordinator._drones[HELD].relanded is False
    # The next report, with no gap, tries again; this time it is delivered and announced.
    transport.connected[HELD] = True
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))
    assert len(_plain_lands(transport)) == before + 2
    assert [e["reason"] for e in _relanded(sio)] == ["flying_again"]
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))
    assert len(_plain_lands(transport)) == before + 2


def test_the_resent_land_is_logged_after_the_send_with_its_outcome(monkeypatch):
    supervisor, transport, _sio, clock, lanes = _held_no_ctrl()
    logged = []
    real = obs_events.flight_safety

    def record(kind, **fields):
        if kind == "lost_drone_land_resent":
            logged.append((fields["outcome"], fields["reason"], len(_plain_lands(transport))))
        return real(kind, **fields)

    monkeypatch.setattr(obs_events, "flight_safety", record)
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))  # the send fails
    attempts = len(_plain_lands(transport))
    transport.connected[HELD] = True
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))
    # Each event comes after its attempt is on the wire (the fake transport records every attempt).
    assert logged == [("not_delivered", "flying_again", attempts), ("sent", "flying_again", attempts + 1)]


def test_the_dashboard_shows_why_a_lost_drone_was_landed():
    js = app_module.Path(app_module.__file__).with_name("static").joinpath("ui", "ui.js").read_text()
    handler = js[js.index("socket.on('fleet_event', (ev) => {"):js.index("// ========== PLAN AND START")]
    branch = handler[handler.index("else if (kind === 'lost_drone_relanded') {"):]
    branch = branch[:branch.index("else if (kind === 'land_all')")]
    assert ("if (!landAllBannerHeld()) showFleetBanner('LANDING LOST DRONE: ' + (ev.detail || ev.drone_id || ''), "
            "15000);") in branch
    # A LAND ALL failure, from the event or the ack, is held on screen; the re-land banner never replaces it.
    assert js.count("landAllBannerUntil = Date.now() + LAND_ALL_FAILURE_HOLD_MS;") == 2
    assert "function landAllBannerHeld() { return Date.now() < landAllBannerUntil; }" in js


# ---------------------------------------------------------------------------
# 2. A held drone that was already landing is marked when it lands
# ---------------------------------------------------------------------------


def _blocked_way_back_while_landing():
    """The critic's P6: Resume search acked, the way back blocked, and the edge holds the drone while it lands."""
    supervisor, transport, sio, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    lanes = len(supervisor.coordinator.lanes(HELD))
    supervisor.manual.take_control(HELD, sid="browser-1")
    _answer(supervisor, HELD, _sent(transport, HELD, fp.MANUAL_HOLD)[-1])
    supervisor.manual.resume_search(HELD)
    _answer(supervisor, HELD, _sent(transport, HELD, fp.RESUME_SEARCH)[-1], detail="returning")
    clock.now += 5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held", battery=9, state="landing"))
    # Landing already: no countdown starts.
    assert not supervisor.manual.orphaned(HELD)
    return supervisor, transport, sio, clock, lanes


def test_critic_p6_a_held_drone_that_was_already_landing_is_marked_when_it_lands(clean_app_state):
    supervisor, transport, sio, clock, lanes = _blocked_way_back_while_landing()
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held", battery=8))
    assert supervisor.manual.orphan_landing(HELD) == ORPHAN_LANDED_ITSELF
    offers_before = len(_offers(transport))
    assert supervisor.check_losses() == [HELD]
    assert {u["reason"] for u in _waiting(supervisor)} == {ORPHAN_SELF_LANDED_CONFIRM}
    # A Resume search from the phone later is refused: the lanes are not re-tasked behind the operator's back.
    acks = _phone_resume(supervisor)
    assert acks and acks[0]["ok"] is False and acks[0]["reason"] == "lost"
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="autonomous", battery=8))
    clock.now += 1
    assert supervisor.check_losses() == []
    assert len(_offers(transport)) == offers_before
    assert len(_waiting(supervisor)) == lanes - 1
    assert [p for p in sio.named("fleet_event") if p.get("event") == "retask"] == []


def test_critic_p6_order_a_phone_resume_before_the_loss_check_re_tasks_nothing():
    """The probe's own order: landed, Resume search from the phone, autonomous on the ground, then the loss check."""
    supervisor, transport, _sio, clock, lanes = _blocked_way_back_while_landing()
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held", battery=8))
    offers_before = len(_offers(transport))
    assert supervisor.manual.resume_search(HELD)["ok"] is True  # not lost yet
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="autonomous", battery=8))
    clock.now += 1
    assert supervisor.check_losses() == [HELD]
    assert len(_offers(transport)) == offers_before
    assert {u["reason"] for u in _waiting(supervisor)} == {ORPHAN_SELF_LANDED_CONFIRM}


def test_a_held_drone_the_operator_lands_is_not_marked():
    supervisor, transport, _sio, _clock, lanes = _blocked_way_back_while_landing()
    assert supervisor.manual.command(HELD, "land")["ok"]
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held", battery=8))
    assert supervisor.manual.orphan_landing(HELD) is None


def test_a_held_drone_the_operator_lands_is_not_marked_when_the_answer_comes_first():
    supervisor, transport, _sio, clock, lanes = _blocked_way_back_while_landing()
    assert supervisor.manual.command(HELD, "land")["ok"]
    land = [m for m in _sent(transport, HELD, "land") if m.get("reason") == fp.MANUAL_LAND_REASON][-1]
    _answer(supervisor, HELD, land)  # the edge answers once the drone's land command returns
    clock.now += 0.3
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held", battery=8))
    assert supervisor.manual.orphan_landing(HELD) is None


def test_critic_e_land_then_switch_tiles_is_the_operators_landing_not_an_orphan_one():
    supervisor, transport, _sio, clock = _started_supervisor()
    _manual_ready(supervisor, is_flying=True)
    lanes = len(supervisor.coordinator.lanes(HELD))
    supervisor.manual.take_control(HELD, sid="browser-1")
    _answer(supervisor, HELD, _sent(transport, HELD, fp.MANUAL_HOLD)[-1])
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual"))
    assert supervisor.manual.command(HELD, "land", sid="browser-1")["ok"]
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="manual", state="landing"))
    supervisor.manual.take_control(OTHER, sid="browser-1")
    _answer(supervisor, HELD, _sent(transport, HELD, fp.RESUME_SEARCH)[-1], ok=False, reason="landing")
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held", state="landing"))
    land = [m for m in _sent(transport, HELD, "land") if m.get("reason") == fp.MANUAL_LAND_REASON][-1]
    _answer(supervisor, HELD, land)
    clock.now += 0.3
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held"))
    assert supervisor.manual.orphan_landing(HELD) is None
    # Once seen on the ground, the operator's land is forgotten: a later unattended landing is marked again.
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held", state="landing"))
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held"))
    assert supervisor.manual.orphan_landing(HELD) == ORPHAN_LANDED_ITSELF


def test_critic_f_a_held_landing_drone_lost_before_touchdown_waits_for_the_operator():
    supervisor, transport, _sio, _clock, lanes = _blocked_way_back_while_landing()
    offers_before = len(_offers(transport))
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    assert len(_offers(transport)) == offers_before
    waiting = _waiting(supervisor)
    assert sorted(u["index"] for u in waiting) == list(range(1, lanes))
    assert {u["reason"] for u in waiting} == {f"{ORPHAN_LOST_CONFIRM} (disconnected)"}


def test_a_drone_this_dashboard_controls_is_not_marked_when_it_lands():
    supervisor, _transport, _sio, _clock, lanes = _blocked_way_back_while_landing()
    supervisor.manual.take_control(HELD, sid="browser-2")
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="manual", battery=8))
    assert supervisor.manual.orphan_landing(HELD) is None


def test_a_lost_drone_the_ground_landed_is_not_logged_as_landed_by_itself(caplog):
    """A lost held drone whose link came back is landed by the coordinator; the log must not say it landed by itself."""
    supervisor, transport, _sio, clock, lanes = _held_no_ctrl()
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held"))
    assert _plain_lands(transport)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="flightrisk.dashboard.manual"):
        clock.now += 0.5
        supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held"))
    messages = [r.getMessage() for r in caplog.records if r.name == "flightrisk.dashboard.manual"]
    assert f"Held {HELD} landed while nobody controlled it, after it was declared lost and the ground sent it land" \
        in messages
    assert not [m for m in messages if "landed by itself" in m]


# ---------------------------------------------------------------------------
# 3. Re-task waits LOST_OBSTACLE_S after the last report that the drone flies
# ---------------------------------------------------------------------------


def _lost_and_back_flying():
    supervisor, transport, sio, clock, lanes = _held_no_ctrl()
    transport.connected[HELD] = False
    assert supervisor.check_losses() == [HELD]
    transport.connected[HELD] = True
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held", rel_x_cm=50, rel_y_cm=80))
    return supervisor, transport, sio, clock, lanes


def test_critic_p2_re_task_stays_refused_after_the_link_drops_again_while_it_flew():
    supervisor, transport, _sio, clock, _lanes = _lost_and_back_flying()
    offers_before = len(_offers(transport))
    # Its link drops again: no telemetry for 3 s, more than the 2 s the check used to look at.
    clock.now += TELEMETRY_FRESH_S + 1
    ok, detail = supervisor.confirm_retask(HELD)
    assert not ok
    assert detail == (f"{HELD} may still be in the air: its last report, 3 s ago, said it was flying, and nothing has "
                      f"come from it since. Wait {LOST_OBSTACLE_S - 3:g} s, or confirm the re-task once it reports "
                      f"landed.")
    clock.now += LOST_OBSTACLE_S - (TELEMETRY_FRESH_S + 1) - 0.5
    ok, detail = supervisor.confirm_retask(HELD)
    assert not ok and "Wait 1 s" in detail
    assert len(_offers(transport)) == offers_before
    # LOST_OBSTACLE_S after that report its edge has landed it by itself: the operator may confirm.
    clock.now += 1
    ok, detail = supervisor.confirm_retask(HELD)
    assert ok and "offered to drone-1" in detail


def test_critic_g_re_task_waits_as_long_as_the_lost_drone_counts_as_an_obstacle():
    """Lost by silence 6 s after its last flying report: the wait runs from the loss, as the obstacle check's does."""
    supervisor, transport, _sio, clock, lanes = _held_no_ctrl()
    supervisor.handle_control(_report(HELD, lanes, is_flying=True, mode="held", rel_x_cm=50, rel_y_cm=80))
    last = clock.now
    clock.now += 6
    transport.link_age_s[HELD] = 6.0
    assert supervisor.check_losses() == [HELD]
    clock.now = last + LOST_OBSTACLE_S + 0.5
    with supervisor.coordinator._lock:
        assert supervisor.coordinator._lost_obstacle_locked(HELD) is not None
    ok, detail = supervisor.confirm_retask(HELD)
    assert not ok and "Wait 6 s" in detail
    clock.now = last + 6 + LOST_OBSTACLE_S + 0.5
    with supervisor.coordinator._lock:
        assert supervisor.coordinator._lost_obstacle_locked(HELD) is None
    ok, detail = supervisor.confirm_retask(HELD)
    assert ok, detail


def test_re_task_is_accepted_at_once_when_the_last_report_says_landed():
    supervisor, _transport, _sio, clock, lanes = _lost_and_back_flying()
    clock.now += 0.5
    supervisor.handle_control(_report(HELD, lanes, is_flying=False, mode="held"))
    clock.now += TELEMETRY_FRESH_S + 1  # its link drops after it reported landed
    ok, detail = supervisor.confirm_retask(HELD)
    assert ok and "offered to drone-1" in detail


def test_re_task_of_a_flying_lost_drone_with_fresh_telemetry_keeps_its_message():
    supervisor, _transport, _sio, _clock, _lanes = _lost_and_back_flying()
    ok, detail = supervisor.confirm_retask(HELD)
    assert not ok and detail == f"Land {HELD} first; it is flying again. Confirm the re-task once it reports landed."


# ---------------------------------------------------------------------------
# Nits
# ---------------------------------------------------------------------------


NODE = shutil.which("node")
CONFIRM_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const clicks = JSON.parse(process.argv[3]);
let now = 1000000;
global.Date = { now: () => now };
function el(tag, cls, text) {
  return { tag, className: cls, textContent: text, title: '', disabled: false, isConnected: true, listeners: {},
           classList: { toggle() {}, remove() {} },
           addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); } };
}
function showFleetBanner() {}
function setTimeout() {}
const make = new Function('el', 'showFleetBanner', 'setTimeout', src + '; return twoStepConfirmButton;');
const twoStepConfirmButton = make(el, showFleetBanner, setTimeout);
const sent = [];
const button = twoStepConfirmButton('retask:drone-2', 'retask-confirm', 'Re-task', 'Confirm re-task', 'warning',
                                    () => sent.push(now));
const out = [];
for (const at of clicks) {
  now = 1000000 + at;
  button.listeners.click.forEach((f) => f());
  out.push(sent.length);
}
console.log(JSON.stringify(out));
"""


def _confirm_block() -> str:
    js = app_module.Path(app_module.__file__).with_name("static").joinpath("ui", "ui.js").read_text()
    start = js.index("const CONFIRM_ARM_MS")
    return js[start:js.index("// A person cut is never re-flown on its own")]


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.parametrize(("clicks", "sent"), [
    ([0, 120], [0, 0]),        # a double click only arms the button
    ([0, 499], [0, 0]),
    ([0, 120, 700], [0, 0, 1]),  # a deliberate second click after it confirms
    ([0, 500], [0, 1]),
    ([0, 9000], [0, 0]),        # the arming ran out: this click arms again
])
def test_a_second_click_within_500_ms_of_arming_is_ignored(tmp_path, clicks, sent):
    block = tmp_path / "confirm.js"
    block.write_text(_confirm_block())
    harness = tmp_path / "harness.js"
    harness.write_text(CONFIRM_HARNESS)
    result = subprocess.run([NODE, str(harness), str(block), json.dumps(clicks)],
                            capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout) == sent
