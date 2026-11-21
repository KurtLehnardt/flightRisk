"""Flight-control fixes from the post-merge review of PR #177 (findings 1 to 6).

1. A drone the edge reports held with no controller gets the orphan-landing
   countdown however it got there, including a ``resume_search`` way back
   that an obstacle blocked after the edge had acknowledged it.
2. A person cut is never offered for a re-flight on its own; the operator
   confirms it on the dashboard. Obstacle cuts keep the old behaviour.
3. A lost drone's last position stays an obstacle for re-task and re-flight
   paths for ``LOST_OBSTACLE_S`` (15 s), or until it reports not flying.
4. A gate stopped by a link-loss hold stops the plan loop, instead of
   sensing again at once, sense call after sense call.
5. One probe look before an autonomous takeoff; a sensor that cannot see a
   fresh view refuses the takeoff with nothing sent.
6. The dashboard's single-drone search gives ``ObstacleGuard`` BGR frames.

Fake edges, fake drones, a fake clock and scripted sensors; nothing touches
a network, a camera, a model or a drone.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.manual_control import ManualControl
from flightrisk.dashboard.state import app_state
from flightrisk.drone import coordinator as coordinator_module
from flightrisk.drone.coordinator import LOST_OBSTACLE_S
from flightrisk.drone.lane_flight import EVENT_TAKEOFF_SENSE, TAKEOFF_SENSE_DETAIL
from flightrisk.drone.obstacle_camera import ForwardCameraSensor
from flightrisk.drone.obstacle_sensor import (
    CHANNEL_ORDER_BGR,
    CHANNEL_ORDER_RGB,
    CONFIRM_REFLIGHT_CUT_REASONS,
    CUE_DEPTH,
    CUT_OBSTACLE,
    CUT_PERSON,
    EVENT_START_REFUSED,
    OBSTACLE_SETTLE_S,
    REFLIGHT_CUT_REASONS,
    START_REFUSED_REASON,
    BlockedZone,
    FrameStamp,
    LatestFrame,
    SenseResult,
)
from flightrisk.drone.tello import FakeTelloController, TelloController
from tests import test_lane_executor_obstacle as obstacle_tests
from tests.lane_executor_support import FENCE, LANES, reply_for, send, takeoff, wait_for
from tests.test_coordinator import make_fleet
from tests.test_coordinator_reflight import LANES as REFLIGHT_PLAN_LANES
from tests.test_coordinator_reflight import REFLIGHT, TRANSIT, Fleet
from tests.test_fleet_dashboard import FakeClock as GroundClock
from tests.test_fleet_dashboard import _supervisor, _telemetry, _wait_for
from tests.test_fleet_dashboard_obstacle import NODE, _run
from tests.test_lane_executor_obstacle import answered, events, finished, start
from tests.test_manual_ground import _answer, _manual_ready, _orphan_lands, _sent
from tests.test_review177_flight import take_over_at_gate

rigs = obstacle_tests.rigs


# ---------------------------------------------------------------------------
# Finding 1: held with no controller, whatever the cause, gets the countdown
# ---------------------------------------------------------------------------


def _held(drone_id="drone-1", mode="manual", **fields):
    return dict(_telemetry(drone_id, is_flying=fields.pop("is_flying", True)), control_mode=mode, **fields)


def _countdown(supervisor, drone_id="drone-1"):
    return supervisor.status_payload()["drones"][drone_id]["orphan_land_in_s"]


def test_f1_a_way_back_blocked_after_the_resume_ack_starts_the_countdown_and_lands_the_drone():
    supervisor, transport, sio, clock = _supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control("drone-1", sid="op")
    supervisor.handle_control(_held())
    assert supervisor.manual.resume_search("drone-1", sid="op")["ok"]
    _answer(supervisor, "drone-1", _sent(transport, "drone-1", "resume_search")[-1], ok=True)
    supervisor.handle_control(_held(mode="autonomous"))  # the edge flies the way back
    # An obstacle blocks the way back: the edge puts the drone under manual
    # control by itself, and no dashboard controls it.
    clock.now += 4.0
    supervisor.handle_control(_held(mode="manual"))
    assert supervisor.manual.controlled is None
    assert _countdown(supervisor) == 120
    assert any(e.get("drones", {}).get("drone-1", {}).get("orphan_land_in_s") == 120
               for e in sio.named("manual_status"))
    clock.now += 60.0
    supervisor.handle_control(_held(mode="held"))  # the dead-man: still the same countdown
    assert _countdown(supervisor) == 60
    clock.now += 60.0
    assert supervisor.manual.check_orphans() == ["drone-1"]
    (land,) = _orphan_lands(transport, "drone-1")
    fp.validate_ground_to_edge(land, proto=2, require_fence=True)


def test_f1_no_countdown_while_the_resume_awaits_its_answer_or_just_after_its_ack():
    supervisor, transport, _sio, clock = _supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control("drone-1", sid="op")
    supervisor.manual.resume_search("drone-1", sid="op")
    # Telemetry the edge sent before it took the resume still says manual.
    supervisor.handle_control(_held())
    assert _countdown(supervisor) is None
    _answer(supervisor, "drone-1", _sent(transport, "drone-1", "resume_search")[-1], ok=True)
    clock.now += 1.0
    supervisor.handle_control(_held())
    assert _countdown(supervisor) is None
    clock.now += 2.0  # 3 s after the ack: the edge must have released it by now
    supervisor.handle_control(_held())
    assert _countdown(supervisor) == 120


def test_f1_a_resume_refused_on_a_tile_switch_gives_the_old_drone_a_countdown():
    supervisor, transport, _sio, _clock = _supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.manual.take_control("drone-1", sid="op")
    supervisor.handle_control(_held("drone-1"))
    supervisor.manual.take_control("drone-2", sid="op")  # resumes drone-1
    _answer(supervisor, "drone-1", _sent(transport, "drone-1", "resume_search")[-1], ok=False, reason="geofence")
    supervisor.handle_control(_held("drone-1"))
    supervisor.handle_control(_held("drone-2"))
    assert supervisor.manual.controlled == "drone-2"
    assert _countdown(supervisor, "drone-1") == 120
    assert _countdown(supervisor, "drone-2") is None


def test_f1_a_landing_or_grounded_drone_gets_none_and_telemetry_never_restarts_a_running_one():
    supervisor, transport, _sio, clock = _supervisor()
    _manual_ready(supervisor, is_flying=True)
    supervisor.handle_control(_held(state="landing"))
    assert _countdown(supervisor) is None
    supervisor.handle_control(_held(is_flying=False))
    assert _countdown(supervisor) is None
    supervisor.manual.command("drone-1", "land")  # a land awaits its answer
    supervisor.handle_control(_held())
    assert _countdown(supervisor) is None
    _answer(supervisor, "drone-1", _sent(transport, "drone-1", "land")[-1], ok=True)
    supervisor.handle_control(_held())
    assert _countdown(supervisor) == 120
    clock.now += 50.0
    supervisor.handle_control(_held(mode="held"))
    assert _countdown(supervisor) == 70
    # The edge releases it (a later Resume search elsewhere): the countdown ends.
    supervisor.handle_control(_held(mode="autonomous"))
    assert _countdown(supervisor) is None


def test_f1_end_to_end_a_real_edges_blocked_way_back_is_landed_by_the_ground(rigs):
    """The review's scenario on a real LaneExecutor: resume_search acked, way back blocked, then the countdown."""
    rig = rigs(blocked=[BlockedZone(heading_deg=270.0)])  # anything seen facing left blocks
    take_over_at_gate(rig, 3, ("right", 50))
    ground = GroundClock()
    to_edge: list[dict] = []

    def deliver(drone_id, msg):
        to_edge.append(msg)
        send(rig.executor, msg)

    manual = ManualControl(["drone-1"], deliver, lambda name, payload: None, clock=ground,
                           fence=lambda: FENCE, background=False)

    def answer_last():
        msg = to_edge[-1]
        assert wait_for(lambda: reply_for(rig.sent, msg["cmd_id"]) is not None), msg
        assert manual.on_reply(reply_for(rig.sent, msg["cmd_id"]))
        return reply_for(rig.sent, msg["cmd_id"])

    manual.observe_telemetry("drone-1", rig.executor.telemetry())
    assert manual.take_control("drone-1", sid="op")["ok"]
    assert answer_last()["type"] == "ack"
    assert manual.resume_search("drone-1", sid="op")["ok"]
    assert answer_last()["type"] == "ack"  # acked at launch, before the way back is flown
    assert wait_for(lambda: events(rig, "edge_resume_search_blocked") and not rig.executor.is_busy), rig.timeline
    telemetry = rig.executor.telemetry()
    assert telemetry["control_mode"] == "manual" and telemetry["is_flying"] is True
    ground.now += 3.0
    manual.observe_telemetry("drone-1", telemetry)
    assert manual.drone_status("drone-1")["orphan_land_in_s"] == 120
    ground.now += 120.0
    assert manual.check_orphans() == ["drone-1"]
    assert to_edge[-1]["type"] == "land" and to_edge[-1]["reason"] == fp.ORPHAN_LAND_REASON
    assert wait_for(lambda: ("land",) in rig.timeline), rig.timeline
    assert events(rig, "edge_land")[-1]["reason"] == fp.ORPHAN_LAND_REASON


# ---------------------------------------------------------------------------
# Finding 2: a person cut waits for the operator
# ---------------------------------------------------------------------------


def test_f2_person_cuts_need_confirmation_and_obstacle_cuts_do_not():
    assert CUT_PERSON in CONFIRM_REFLIGHT_CUT_REASONS and CUT_PERSON not in REFLIGHT_CUT_REASONS
    assert CUT_OBSTACLE in REFLIGHT_CUT_REASONS and CUT_OBSTACLE not in CONFIRM_REFLIGHT_CUT_REASONS


def test_f2_a_person_cut_is_never_offered_until_the_operator_confirms_it():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_PERSON, lane_index=2)
    assert {(s["reason"], s.get("status")) for s in fleet.segments()} == {
        (CUT_PERSON, "needs_confirmation"), ("skipped", "needs_confirmation")}
    # drone-1 lands after its lanes and drone-2 hovers done: nothing goes out.
    fleet.done("drone-1", is_flying=False)
    fleet.done("drone-2")
    assert len(fleet.assigns("drone-2")) == 1 and fleet.reflight_events() == []

    ok, detail = fleet.coord.confirm_reflight("drone-1", 1)
    assert ok and "offered to drone-2" in detail
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(REFLIGHT_PLAN_LANES) + TRANSIT + REFLIGHT
    assert [e["event"] for e in fleet.reflight_events()] == ["reflight_offered", "reflight_accepted"]
    assert {s.get("status") for s in fleet.segments()} == {"acked"}
    # Confirmed once: a second confirmation changes nothing.
    assert fleet.coord.confirm_reflight("drone-1", 1) == (False, "drone-1's cut 1 needs no confirmation")


def test_f2_confirmed_early_it_still_waits_for_the_cutting_drone_to_finish():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_PERSON, lane_index=1)
    ok, detail = fleet.coord.confirm_reflight("drone-1", 1)
    assert ok and "once a drone can take it" in detail
    assert {(s.get("status"), s.get("waiting_for")) for s in fleet.segments()} == {("waiting", "drone-1")}
    assert len(fleet.assigns("drone-2")) == 1
    fleet.done("drone-1", is_flying=False)
    assert fleet.assigns("drone-2")[-1]["lanes"][-len(REFLIGHT):] == REFLIGHT


def test_f2_an_obstacle_cut_is_still_offered_on_its_own_next_to_a_waiting_person_cut():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250), (2, 100)], f"{CUT_OBSTACLE},{CUT_PERSON}", lane_index=3)
    fleet.done("drone-1", is_flying=False)
    with fleet.coord._lock:
        chunks = {chunk.cut_seq: (chunk.confirm, chunk.status) for chunk in fleet.coord._reflights}
    assert chunks == {1: (False, "acked"), 2: (True, "queued")}
    assert [e["event"] for e in fleet.reflight_events()] == ["reflight_offered", "reflight_accepted"]


@pytest.mark.parametrize("drone_id, cut_seq, why", [
    ("drone-1", 1, "needs no confirmation"),  # an obstacle cut
    ("drone-1", 9, "no cut 9"),
    ("drone-2", 1, "no cut 1"),
    ("drone-1", "1", "invalid cut number"),
    ("drone-1", True, "invalid cut number"),
])
def test_f2_only_a_waiting_person_cut_can_be_confirmed(drone_id, cut_seq, why):
    fleet = Fleet()
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    ok, detail = fleet.coord.confirm_reflight(drone_id, cut_seq)
    assert not ok and why in detail


def test_f2_the_supervisor_confirms_through_the_coordinator_and_never_after_land_all():
    supervisor, _transport, sio, _clock = _supervisor()
    supervisor.coordinator.confirm_reflight = MagicMock(return_value=(True, "drone-1's cut 4 confirmed"))
    assert supervisor.confirm_reflight("drone-1", 4) == (True, "drone-1's cut 4 confirmed")
    supervisor.coordinator.confirm_reflight.assert_called_once_with("drone-1", 4)
    assert sio.named("fleet_status")
    supervisor.land_all()
    ok, detail = supervisor.confirm_reflight("drone-1", 4)
    assert not ok and "LAND ALL" in detail
    assert supervisor.coordinator.confirm_reflight.call_count == 1


def test_f2_the_confirm_reflight_socket_event_answers_the_browser(clean_app_state):
    supervisor, _transport, _sio, _clock = _supervisor()
    supervisor.confirm_reflight = MagicMock(return_value=(True, "drone-1's cut 3 confirmed and offered to drone-2"))
    app_state.fleet = None
    app_state.fleet_supervisor = supervisor
    client = socketio.test_client(app)
    try:
        client.get_received()
        client.emit("confirm_reflight", {"drone_id": "drone-1", "cut_seq": 3})
        received: list = []

        def answered_result():
            received.extend(r["args"][0] for r in client.get_received() if r["name"] == "reflight_confirm_result")
            return received

        assert _wait_for(answered_result)
        assert received[0] == {"drone_id": "drone-1", "cut_seq": 3, "ok": True,
                               "detail": "drone-1's cut 3 confirmed and offered to drone-2"}
        supervisor.confirm_reflight.assert_called_once_with("drone-1", 3)
    finally:
        client.disconnect()


def test_f2_the_dashboard_offers_a_re_fly_button_only_for_a_person_cut_waiting_for_confirmation():
    js = app_module.Path(app_module.__file__).with_name("static").joinpath("ui", "ui.js").read_text()
    render = js[js.index("function renderUncovered()"):js.index("socket.on('reflight_confirm_result'")]
    assert "e.status === 'needs_confirmation'" in render and "reflightConfirmButton(e)" in render
    assert "window.confirm(" in render and "socket.emit('confirm_reflight', { drone_id:" in render
    assert "cut_seq: e.cut_seq" in render


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_f2_the_waiting_person_cut_has_its_own_sentence_and_banner(tmp_path):
    sentence, person, plant = _run(tmp_path, [
        ("reflightSentence", [{"status": "needs_confirmation"}]),
        ("laneBlockedText", [{"event": "lane_blocked", "drone_id": "drone-1", "lane_index": 0, "x_cm": 0,
                              "y_cm": 250, "unflown_cm": 150, "reason": "person"}]),
        ("laneBlockedText", [{"event": "lane_blocked", "drone_id": "drone-1", "lane_index": 0, "x_cm": 0,
                              "y_cm": 250, "unflown_cm": 150, "reason": "obstacle"}]),
    ])
    assert sentence == "a person stopped the drone here; not re-flown unless you confirm it"
    assert person.endswith("; a person: re-flown only if you confirm it under NOT SEARCHED")
    assert plant == "OBSTACLE drone-1: lane 0 stopped at (0, 250) cm, 150 cm not searched"


# ---------------------------------------------------------------------------
# Finding 3: a lost drone may still hover for 15 s
# ---------------------------------------------------------------------------


def test_f3_a_lost_drone_is_an_obstacle_for_15_s():
    assert coordinator_module.LOST_OBSTACLE_S == LOST_OBSTACLE_S == 15.0


def test_f3_a_lost_cutters_last_position_holds_the_re_flight_for_15_s_only():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1", rel_x_cm=50, rel_y_cm=300)  # done, hovering beside the segment
    fleet.coord.on_drone_lost("drone-1", wait_s=0)  # an edge crash: lost at once
    (segment, *_) = fleet.segments()
    assert segment["status"] == "waiting" and "lost; it may still hover there" in segment["detail"]
    fleet.clock.advance(LOST_OBSTACLE_S - 1.0)
    fleet.done("drone-2")
    assert len(fleet.assigns("drone-2")) == 1
    fleet.clock.advance(1.0)
    fleet.done("drone-2")
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(REFLIGHT_PLAN_LANES) + TRANSIT + REFLIGHT


def test_f3_a_lost_cutter_that_reports_it_is_not_flying_releases_the_re_flight_at_once():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    fleet.done("drone-1", rel_x_cm=50, rel_y_cm=300)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    assert len(fleet.assigns("drone-2")) == 1
    fleet.done("drone-1", rel_x_cm=50, rel_y_cm=300, is_flying=False)  # it landed by itself
    assert fleet.assigns("drone-2")[-1]["lanes"] == list(REFLIGHT_PLAN_LANES) + TRANSIT + REFLIGHT


def test_f3_any_lost_drone_still_in_the_air_holds_a_re_flight_that_passes_it():
    fleet = Fleet(3)
    fleet.done("drone-2")
    fleet.done("drone-3")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    # drone-3 (takeoff point x = -350) hovers on drone-1's segment when it is lost.
    fleet.done("drone-3", rel_x_cm=350, rel_y_cm=300)
    fleet.coord.on_drone_lost("drone-3", wait_s=0)
    lanes = fleet.coord.lanes("drone-2") + TRANSIT + REFLIGHT
    with fleet.coord._lock:
        why = fleet.coord._near_lost_locked("drone-2", lanes, skip="drone-1")
    assert why is not None and "drone-3 (lost; it may still hover there)" in why
    fleet.clock.advance(LOST_OBSTACLE_S)
    with fleet.coord._lock:
        assert fleet.coord._near_lost_locked("drone-2", lanes, skip="drone-1") is None


def test_f3_a_lost_drones_lanes_wait_while_it_may_still_hover_on_the_way():
    coord, edges, clock, events_seen = make_fleet(n=2, area=(400, 400), strip=50)
    coord.sync()
    coord.plan()
    assert coord.start().ok
    edges["drone-1"].telemetry(lane_index=1, is_flying=True)
    lanes_before = len(coord.lanes("drone-1"))
    # drone-2 was flying lane 2 (at x = 50 cm of its zone, 200 cm up) when its edge crashed.
    edges["drone-2"].telemetry(lane_index=2, is_flying=True, rel_x_cm=50, rel_y_cm=200)
    assert coord.on_drone_lost("drone-2", wait_s=0) == {}
    waiting = [u for u in coord.current_plan()["uncovered"] if u.get("status") == "waiting"]
    assert waiting and all("drone-2 (lost; it may still hover there)" in u["reason"] for u in waiting)
    retask = [p for name, p in events_seen if name == "fleet_event" and p.get("event") == "retask"]
    assert retask and retask[-1]["awaiting"]
    clock.advance(LOST_OBSTACLE_S)
    edges["drone-1"].telemetry(lane_index=1, is_flying=True)
    assert len(coord.lanes("drone-1")) > lanes_before
    assert not [u for u in coord.current_plan()["uncovered"] if u.get("status") == "waiting"]


def test_f3_a_lost_drone_that_never_reported_a_position_holds_nothing():
    fleet = Fleet()
    fleet.done("drone-2")
    fleet.cut("drone-1", [(0, 250)], CUT_OBSTACLE, lane_index=2)
    with fleet.coord._lock:
        fleet.coord._drones["drone-1"].telemetry.pop("rel_x_cm", None)
    fleet.coord.on_drone_lost("drone-1", wait_s=0)
    with fleet.coord._lock:
        assert fleet.coord._lost_obstacle_locked("drone-1") is None


# ---------------------------------------------------------------------------
# Finding 4: a link-loss hold stops the plan loop instead of sensing in a loop
# ---------------------------------------------------------------------------


def test_f4_a_gate_stopped_by_a_link_loss_hold_parks_the_plan_without_sensing_again(rigs):
    rig = rigs()

    def hook(n):
        if n == 1:
            # The ground goes silent while the first gate looks, and the
            # sensor cannot see: the gate would retry.
            with rig.executor._lock:
                rig.executor._link_holding = True
            rig.sensor.queue("stale")

    rig.sensor.on_call = hook
    start(rig, LANES)
    assert wait_for(lambda: not rig.executor.is_busy), rig.timeline
    time.sleep(0.2)
    assert len(rig.sensor.calls) == 1, rig.sensor.states()
    assert events(rig, "edge_link_hold_stopped")
    assert rig.executor._link_parked is True
    assert not [e for e in rig.timeline if e[0] == "move" and e[1] != "up"]
    rig.sensor.on_call = None
    # The link comes back: the plan continues from the same step.
    rig.executor._end_link_hold()
    assert wait_for(lambda: finished(rig)), rig.timeline
    obstacle_tests.assert_invariants(rig)


# ---------------------------------------------------------------------------
# Finding 5: one probe look before an autonomous takeoff
# ---------------------------------------------------------------------------


class _SafeGuard:
    """An ObstacleGuard stand-in that calls every view clear."""

    def __init__(self):
        self.judged = 0

    def check_path(self, frame):
        self.judged += 1
        return {"safe": True, "center_depth": 0.2, "left_depth": 0.1, "right_depth": 0.1, "action": "clear",
                "confidence": 0.9, "center_p90": 0.2, "center_stat": "p90", "degenerate": False,
                "degenerate_reason": ""}


def _view() -> np.ndarray:
    frame = np.zeros((72, 96, 3), np.uint8)
    frame[:, :48] = 80  # not blank: a uniform frame is refused as a restarted decoder's
    return frame


def _camera(made: dict, guard, **kwargs):
    """A sensor factory for ``rigs``: the real camera sensor on a frame holder, both on the rig's clock."""

    def make(timeline, envelope, clock):
        kwargs.setdefault("frame_wait_s", 0.0)
        made["holder"] = LatestFrame(clock)
        made["sensor"] = ForwardCameraSensor(made["holder"], None, guard=guard, warm=False, clock=clock,
                                             detection_wait_s=0.0, **kwargs)
        return made["sensor"]

    return make


def test_f5_a_frozen_video_stream_refuses_the_takeoff_with_nothing_sent(rigs):
    guard = _SafeGuard()
    made: dict = {}
    rig = rigs(sensor=_camera(made, guard), obstacle_takeoff_probe=True)
    try:
        # The stream froze: its last frame was captured before the start.
        made["holder"].offer(_view(), stamp=FrameStamp(rig.clock()))
        start(rig, LANES)
        reply = answered(rig, "s-p-1")
        assert (reply["type"], reply["reason"]) == ("nack", "drone_unreachable"), reply
        assert reply["detail"].startswith(TAKEOFF_SENSE_DETAIL) and "stale" in reply["detail"]
        assert ("takeoff",) not in rig.drone.calls and not rig.drone.state.is_flying
        assert guard.judged == 0
        (look,) = events(rig, EVENT_TAKEOFF_SENSE)
        assert (look["ok"], look["state"], look["reason"], look["kind"]) == (False, "unavailable", "stale", "start_at")
        refused = events(rig, EVENT_START_REFUSED)[-1]
        assert refused["reason"] == START_REFUSED_REASON and refused["detail"] == reply["detail"]
        assert rig.executor._sensor_down_since is not None
    finally:
        made["sensor"].close()


def test_f5_a_live_stream_passes_the_probe_and_the_drone_flies(rigs):
    guard = _SafeGuard()
    made: dict = {}
    stop = threading.Event()
    rig = rigs(sensor=_camera(made, guard, frame_wait_s=1.0, settle_s=0.0), obstacle_takeoff_probe=True)

    def decoder():
        # A live stream: a new frame, captured now, every few milliseconds.
        while not stop.is_set():
            made["holder"].offer(_view(), stamp=FrameStamp(rig.clock()))
            stop.wait(0.003)

    feeder = threading.Thread(target=decoder, daemon=True)
    feeder.start()
    try:
        start(rig, LANES)
        assert answered(rig, "s-p-1")["type"] == "ack"
        assert wait_for(lambda: finished(rig)), rig.timeline
        (look,) = events(rig, EVENT_TAKEOFF_SENSE)
        assert (look["ok"], look["state"]) == (True, "clear")
        assert ("takeoff",) in rig.drone.calls
    finally:
        stop.set()
        feeder.join(2.0)
        made["sensor"].close()


def test_f5_the_probe_asks_for_a_view_no_older_than_itself(rigs):
    rig = rigs(obstacle_takeoff_probe=True)
    start(rig, LANES)
    t_takeoff = rig.clock()  # the fake clock stays at the start time
    assert answered(rig, "s-p-1")["type"] == "ack"
    assert wait_for(lambda: finished(rig)), rig.timeline
    probe = rig.sensor.calls[0]
    # A view captured at or after the look itself (the sensor adds the settle time back).
    assert probe.after == pytest.approx(t_takeoff - OBSTACLE_SETTLE_S)
    assert events(rig, EVENT_TAKEOFF_SENSE)[0]["ok"] is True
    # The look happened on the ground, before the takeoff, and is not a lane gate.
    assert rig.timeline[0][0] == "sense" and rig.timeline[1] == ("move", "up", 20)
    assert len(rig.sensor.calls) == len(events(rig, "edge_obstacle_gate")) + 1


def test_f5_a_blocked_view_is_a_working_sensor_and_the_drone_takes_off(rigs):
    rig = rigs(obstacle_takeoff_probe=True)
    rig.sensor.queue(SenseResult.make_blocked(CUE_DEPTH))
    start(rig, LANES)
    assert answered(rig, "s-p-1")["type"] == "ack"
    assert wait_for(lambda: finished(rig)), rig.timeline
    assert events(rig, EVENT_TAKEOFF_SENSE)[0]["state"] == "blocked"
    assert rig.executor._sensor_down_since is None


def test_f5_the_takeoff_command_is_refused_the_same_way(rigs):
    rig = rigs(obstacle_takeoff_probe=True)
    rig.sensor.queue("timeout")
    send(rig.executor, takeoff(cmd_id="t-1"))
    reply = answered(rig, "t-1")
    assert (reply["type"], reply["reason"]) == ("nack", "drone_unreachable")
    assert reply["detail"] == f"{TAKEOFF_SENSE_DETAIL} timeout"
    assert ("takeoff",) not in rig.drone.calls


def test_f5_the_operators_manual_takeoff_is_never_probed(rigs):
    rig = rigs(obstacle_takeoff_probe=True)
    rig.sensor.queue("stale")
    send(rig.executor, fp.make_manual_hold(cmd_id="m-hold", fence=FENCE))
    assert answered(rig, "m-hold")["type"] == "ack"
    send(rig.executor, fp.make_manual_takeoff(cmd_id="m-up", fence=FENCE))
    assert answered(rig, "m-up")["type"] == "ack"
    assert ("takeoff",) in rig.drone.calls
    assert rig.sensor.calls == [] and events(rig, EVENT_TAKEOFF_SENSE) == []


# ---------------------------------------------------------------------------
# Finding 6: the single-drone search gives ObstacleGuard BGR frames
# ---------------------------------------------------------------------------


def _rgb() -> np.ndarray:
    frame = np.zeros((4, 4, 3), np.uint8)
    frame[..., 0], frame[..., 1], frame[..., 2] = 200, 100, 10  # R, G, B
    return frame


def test_f6_tello_frames_are_declared_rgb():
    assert TelloController.frame_channel_order == CHANNEL_ORDER_RGB
    assert FakeTelloController.frame_channel_order == CHANNEL_ORDER_RGB


@pytest.mark.parametrize("order, first_channel", [
    (CHANNEL_ORDER_RGB, 10),  # a Tello: reversed to BGR
    (CHANNEL_ORDER_BGR, 200),  # already BGR
    (None, 200),  # declares nothing (MAVLink's OpenCV capture): taken as BGR
    ("weird", 200),  # an unknown declaration is not trusted to reverse anything
])
def test_f6_the_guard_frame_is_bgr_and_a_copy(order, first_channel):
    drone = type("Drone", (), {} if order is None else {"frame_channel_order": order})()
    frame = _rgb()
    out = app_module._guard_frame(drone, frame)
    assert int(out[0, 0, 0]) == first_channel and out is not frame


def test_f6_the_single_drone_search_checks_a_tellos_frame_in_bgr(clean_app_state, monkeypatch):
    drone = MagicMock()
    drone.frame_channel_order = CHANNEL_ORDER_RGB
    drone.state.is_flying = True
    drone.get_frame.return_value = _rgb()
    drone.move.return_value = True
    app_state.fleet = MagicMock(primary=drone)
    app_state.fleet_supervisor = None
    seen: list = []

    def check_path(frame):
        seen.append(frame.copy())
        return {"safe": True, "action": "none", "center_depth": 2.0}

    app_state.obstacle_guard = MagicMock(min_safe_depth=0.35, check_path=check_path)
    monkeypatch.setattr("flightrisk.dashboard.app.time.sleep", lambda s: None)
    client = socketio.test_client(app)
    try:
        client.emit("start_search", {"pattern": "parallel_track"})
        assert _wait_for(lambda: seen)
        assert seen[0][0, 0].tolist() == [10, 100, 200]  # B, G, R
    finally:
        app_state.search_active = False
        time.sleep(0.1)
        client.disconnect()
