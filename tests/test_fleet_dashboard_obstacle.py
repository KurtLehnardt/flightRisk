"""The fleet dashboard's view of per-lane obstacle sensing (design section 6, task T5).

Python side: what ``FleetSupervisor`` passes to the browsers (the obstacle status keys, the ``lane_blocked`` and
``reflight_*`` events, and the segment list that rides in the ``fleet_status`` that follows them) and the Start gate. Browser side: the
helpers of ``static/ui/ui.js`` between the ``obstacle-display`` markers run under Node with stub inputs, and the
wiring is checked as text, the way ``tests/test_land_all_failure_display.py`` does it. No drone, no network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flightrisk.dashboard.app import OBSTACLE_PLAN_EVENTS, app, socketio
from flightrisk.dashboard.state import app_state
from tests.test_fleet_dashboard import _started_supervisor, _supervisor

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
JS = (DASH / "static" / "ui" / "ui.js").read_text()
MAP_JS = (DASH / "static" / "ui" / "map_plan.js").read_text()
PAGE = (DASH / "templates" / "index.html").read_text()


def _telemetry(drone_id, **fields):
    msg = {
        "type": "telemetry", "drone_id": drone_id, "battery": 90, "height_cm": 100, "is_flying": True,
        "rel_x_cm": 0.0, "rel_y_cm": 0.0, "heading_deg": 0.0, "lane_index": 0, "lanes_total": 3, "link_age_s": 0.1,
    }
    msg.update(fields)
    return msg


def _sensing(**fields):
    base = {"obstacle_sensing": "stepped", "obstacle_sensor_ok": True, "obstacle_state": "clear",
            "obstacle_latency_ms": 220}
    base.update(fields)
    return base


def _cut(supervisor, drone_id="drone-1", flown_cm=100):
    supervisor.coordinator.handle_message(drone_id, _telemetry(
        drone_id, rel_y_cm=float(flown_cm), **_sensing(
            obstacle_state="blocked", obstacle_seq=1, obstacle_cuts=[0, flown_cm], obstacle_cuts_total=1,
            obstacle_cut_reasons="obstacle")))


def _named(sio, name):
    return sio.named(name)


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------


def test_fleet_status_carries_the_obstacle_keys_when_the_drone_reported_them():
    supervisor, _, _, _ = _started_supervisor()
    supervisor.coordinator.handle_message("drone-1", _telemetry("drone-1", **_sensing()))
    drones = supervisor.status_payload()["drones"]
    info = drones["drone-1"]
    assert info["obstacle_sensing"] == "stepped"
    assert info["obstacle_sensor_ok"] is True
    assert info["obstacle_state"] == "clear"
    assert info["obstacle_latency_ms"] == 220
    assert info["reflights"] == {"offered": 0, "reflown": 0, "blocked_again": 0}
    # The other drone reported nothing: its keys are absent, not null, and the payload stays valid JSON.
    assert "obstacle_sensing" not in drones["drone-2"] and "reflights" not in drones["drone-2"]
    json.dumps(drones)


def _segments(payload):
    return [u for u in payload["uncovered"] if u["kind"] == "segment"]


def test_a_cut_sends_lane_blocked_then_a_status_with_the_segments():
    supervisor, _, sio, _ = _started_supervisor()
    _cut(supervisor)
    events = [p for p in _named(sio, "fleet_event") if p.get("event") == "lane_blocked"]
    assert len(events) == 1
    event = events[0]
    assert (event["drone_id"], event["lane_index"], event["x_cm"], event["y_cm"], event["unflown_cm"]) == (
        "drone-1", 0, 0, 100, 300)
    index = next(i for i, (name, p) in enumerate(sio.events) if name == "fleet_event" and p.get("event") == "lane_blocked")
    # The status that carries the segments follows the event at once.
    follow = next(p for name, p in sio.events[index + 1:] if name == "fleet_status")
    segments = _segments(follow)
    assert [(u["index"], u["status"]) for u in segments] == [(0, "waiting"), (2, "waiting")]
    assert segments[0]["waiting_for"] == "drone-1"
    assert follow["drones"]["drone-1"]["obstacle_state"] == "blocked"


def test_the_status_has_no_segments_before_a_cut_and_never_the_area_entries():
    supervisor, _, _, _ = _started_supervisor()
    assert supervisor.status_payload()["uncovered"] == []
    _cut(supervisor)
    payload = supervisor.status_payload()
    assert payload["uncovered"] and all(u["kind"] == "segment" for u in payload["uncovered"])
    json.dumps(payload)


def test_the_status_of_a_supervisor_without_a_plan_has_no_uncovered_key():
    supervisor, _, _, _ = _supervisor()
    assert "uncovered" not in supervisor.status_payload()


@pytest.mark.parametrize("kind", sorted(k for k in OBSTACLE_PLAN_EVENTS if k.startswith("reflight_")))
def test_each_reflight_event_is_passed_through_unchanged_and_followed_by_a_status(kind):
    supervisor, _, sio, _ = _started_supervisor()
    _cut(supervisor)
    sio.events.clear()
    payload = {"event": kind, "drone_id": "drone-1", "receiver": "drone-2", "cut_seq": 1, "lanes": [], "detail": "x"}
    supervisor._on_coordinator_event("fleet_event", payload)
    assert sio.events[0] == ("fleet_event", payload)
    assert sio.events[1][0] == "fleet_status" and _segments(sio.events[1][1])


def test_an_event_of_another_kind_is_not_followed_by_a_status():
    supervisor, _, sio, _ = _started_supervisor()
    sio.events.clear()
    supervisor._on_coordinator_event("fleet_event", {"event": "retask", "drone_id": "drone-2"})
    assert [name for name, _ in sio.events] == ["fleet_event"]


def test_a_status_change_that_has_no_event_reaches_the_next_status():
    supervisor, _, _, _ = _started_supervisor()
    _cut(supervisor)
    original = supervisor.coordinator.current_plan

    def changed():
        plan = original()
        for entry in plan["uncovered"]:
            if entry.get("kind") == "segment":
                entry.update(status="flying", receiver="drone-2")
        return plan

    supervisor.coordinator.current_plan = changed
    assert {u["status"] for u in supervisor.status_payload()["uncovered"]} == {"flying"}


def test_a_reloaded_browser_gets_the_segments_with_the_plan():
    supervisor, _, sio, _ = _started_supervisor()
    _cut(supervisor)
    app_state.fleet_supervisor = supervisor
    app_state.fleet = None
    try:
        client = socketio.test_client(app)
        client.get_received()
        client.emit("get_fleet_status")
        received = client.get_received()
        client.disconnect()
    finally:
        app_state.fleet_supervisor = None
    plans = [r["args"][0] for r in received if r["name"] == "fleet_plan"]
    assert plans and any(u.get("kind") == "segment" for u in plans[-1]["uncovered"])
    status = next(r["args"][0] for r in received if r["name"] == "fleet_status")
    assert status["drones"]["drone-1"]["obstacle_state"] == "blocked"


# ---------------------------------------------------------------------------
# Start gate
# ---------------------------------------------------------------------------


def _acked_supervisor():
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    from flightrisk import fleet_protocol as fp
    for drone_id in supervisor.links:
        lanes = len(supervisor.coordinator.lanes(drone_id))
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, lanes))
    return supervisor


def test_start_is_refused_for_a_stepped_drone_whose_sensor_is_not_ok_and_the_refusal_reaches_the_browser():
    supervisor = _acked_supervisor()
    sio = supervisor._socketio
    supervisor.coordinator.handle_message("drone-1", _telemetry(
        "drone-1", is_flying=False, **_sensing(obstacle_sensor_ok=False, obstacle_state="unavailable")))
    result = supervisor.start()
    assert not result.ok and "drone-1" in result.refusals
    (refused,) = [p for p in _named(sio, "fleet_event") if p.get("event") == "start_refused"]
    assert "drone-1" in refused["detail"]


def test_start_is_not_refused_for_sensing_off():
    supervisor = _acked_supervisor()
    for drone_id in ("drone-1", "drone-2"):
        supervisor.coordinator.handle_message(drone_id, _telemetry(
            drone_id, is_flying=False, **_sensing(obstacle_sensing="off", obstacle_sensor_ok=False,
                                                  obstacle_state="unavailable")))
    assert supervisor.start().ok


# ---------------------------------------------------------------------------
# Browser helpers under Node
# ---------------------------------------------------------------------------

NODE = shutil.which("node")
HARNESS = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const cases = JSON.parse(process.argv[3]);
const f = new Function('fmt', src + '; return {senseBadge, obstacleStopBadge, laneBlockedText, reflightSentence, '
    + 'reflightBannerText, unsensedWarning, REFLIGHT_AWAITING, REFLIGHT_SETTLED};')(
    (v) => (v === undefined || v === null) ? '--' : (typeof v === 'number' ? Math.round(v * 10) / 10 : v));
console.log(JSON.stringify(cases.map((c) => f[c.fn].apply(null, c.args))));
"""


def _block() -> str:
    start = JS.index("// BEGIN obstacle-display")
    return JS[start : JS.index("// END obstacle-display")]


def _run(tmp_path, calls):
    block = tmp_path / "block.js"
    block.write_text(_block())
    harness = tmp_path / "h.js"
    harness.write_text(HARNESS)
    out = subprocess.run([NODE, str(harness), str(block), json.dumps([{"fn": f, "args": a} for f, a in calls])],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
class TestBrowserHelpers:
    def test_sense_badge_states(self, tmp_path):
        ok, stale, off, auto, fwd, none, unavailable = _run(tmp_path, [
            ("senseBadge", [_sensing()]),
            ("senseBadge", [_sensing(obstacle_sensor_ok=False)]),
            ("senseBadge", [_sensing(obstacle_sensing="off")]),
            ("senseBadge", [_sensing(obstacle_sensing="autopilot")]),
            ("senseBadge", [_sensing(obstacle_sensing="forward_only")]),
            ("senseBadge", [{"battery": 80}]),
            ("senseBadge", [_sensing(obstacle_state="unavailable")]),
        ])
        assert ok["text"] == "SENSE ok 220 ms"
        assert stale["text"] == "SENSE STALE" and unavailable["text"] == "SENSE STALE"
        assert off["text"] == "SENSE OFF"
        assert auto["text"] == "SENSE AUTOPILOT"
        assert fwd["text"] == "SENSE forward only ok 220 ms"
        assert none is None

    def test_stop_badge_only_while_blocked(self, tmp_path):
        blocked, clear, absent = _run(tmp_path, [
            ("obstacleStopBadge", [_sensing(obstacle_state="blocked")]),
            ("obstacleStopBadge", [_sensing()]),
            ("obstacleStopBadge", [{}]),
        ])
        assert blocked["text"] == "OBSTACLE STOP" and clear is None and absent is None

    def test_lane_blocked_banner_is_the_designs_sentence(self, tmp_path):
        (text,) = _run(tmp_path, [("laneBlockedText", [
            {"event": "lane_blocked", "drone_id": "drone-1", "lane_index": 2, "x_cm": 0, "y_cm": 250, "unflown_cm": 150}])])
        assert text == "OBSTACLE drone-1: lane 2 stopped at (0, 250) cm, 150 cm not searched"

    @pytest.mark.parametrize("entry, sentence", [
        ({"status": "waiting", "waiting_for": "drone-1"}, "waiting for drone-1 to finish its lanes"),
        ({"status": "waiting", "waiting_for": None}, "waiting for a free drone to re-fly it"),
        ({"status": "waiting", "waiting_for": "drone-1", "detail": "close to its path"},
         "waiting for drone-1 to finish its lanes (close to its path)"),
        ({"status": "offered", "receiver": "drone-2"}, "offered to drone-2"),
        ({"status": "acked", "receiver": "drone-2"}, "drone-2 will re-fly it after its lanes"),
        ({"status": "flying", "receiver": "drone-2"}, "drone-2 is re-flying it"),
        ({"status": "blocked_again", "receiver": "drone-2", "reason": "blocked again by drone-2"},
         "blocked again by drone-2, not offered again"),
        ({"status": "failed", "reason": "outside the fence"}, "not re-flown: outside the fence"),
        ({"status": "too_short", "reason": "too_short"}, "too short to re-fly"),
        ({"reason": "obstacle"}, ""),
    ])
    def test_reflight_sentences(self, tmp_path, entry, sentence):
        (text,) = _run(tmp_path, [("reflightSentence", [entry])])
        assert text == sentence

    @pytest.mark.parametrize("kind", ["reflight_offered", "reflight_accepted", "reflight_done",
                                      "reflight_blocked", "reflight_failed"])
    def test_every_reflight_event_has_its_own_banner(self, tmp_path, kind):
        (text,) = _run(tmp_path, [("reflightBannerText", [
            kind, {"drone_id": "drone-1", "receiver": "drone-2", "detail": "the detail"}])])
        assert text.startswith("RE-FLIGHT drone-1: ") and text.endswith("(the detail)")

    def test_banners_differ_and_unknown_kinds_have_none(self, tmp_path):
        kinds = ["reflight_offered", "reflight_accepted", "reflight_done", "reflight_blocked", "reflight_failed"]
        texts = _run(tmp_path, [("reflightBannerText", [k, {"drone_id": "drone-1", "receiver": "drone-2"}]) for k in kinds]
                     + [("reflightBannerText", ["reflight_other", {}])])
        assert len(set(texts[:5])) == 5 and texts[5] is None

    def test_start_warning_names_off_and_forward_only_drones(self, tmp_path):
        infos = {
            "drone-1": _sensing(obstacle_sensing="off"),
            "drone-2": _sensing(obstacle_sensing="forward_only"),
            "drone-3": _sensing(),
            "drone-4": {},
        }
        both, only_planned, none = _run(tmp_path, [
            ("unsensedWarning", [infos, []]),
            ("unsensedWarning", [infos, ["drone-2", "drone-3"]]),
            ("unsensedWarning", [{"drone-3": _sensing()}, []]),
        ])
        assert "drone-1 flies with obstacle sensing OFF" in both
        assert "drone-2 senses obstacles on forward lanes only" in both
        assert "drone-3" not in both and "drone-4" not in both
        assert "drone-1" not in only_planned and "drone-2" in only_planned
        assert none == ""


# ---------------------------------------------------------------------------
# Wiring, as text
# ---------------------------------------------------------------------------


def test_drone_card_shows_both_badges():
    card = JS[JS.index("function renderDroneStatus()"):]
    card = card[: card.index("function renderTimingTable")]
    assert "senseBadge(d)" in card and "obstacleStopBadge(d)" in card


def test_lane_blocked_and_reflight_events_raise_banners_and_the_single_drone_toast_is_unchanged():
    handler = JS[JS.index("socket.on('fleet_event'"):]
    handler = handler[: handler.index("// ========== PLAN AND START")]
    assert "kind === 'lane_blocked'" in handler and "laneBlockedText(ev)" in handler
    assert "kind.indexOf('reflight_') === 0" in handler and "reflightBannerText(kind, ev)" in handler
    assert "socket.on('obstacle_detected'" in JS


def test_uncovered_segments_reach_the_box_and_the_map_from_both_events():
    status = JS[JS.index("socket.on('fleet_status'"):]
    assert "applyUncovered(data.uncovered)" in status[: status.index("socket.on('fleet_telemetry'")]
    apply_fn = JS[JS.index("function applyUncovered"):]
    apply_fn = apply_fn[: apply_fn.index("socket.on('fleet_event'")]
    assert "mapPlan.setUncoveredSegments(segments)" in apply_fn and "renderUncovered()" in apply_fn
    plan = JS[JS.index("socket.on('fleet_plan'"):]
    assert "applyUncovered(" in plan[: plan.index("fleetPlanPending = false")]
    # A refresh of the segments never touches the Start button; a new plan always redraws.
    assert "fleetPlanReceived" not in apply_fn
    assert "(plan && plan.uncovered) || [], true)" in plan


def test_start_confirmation_includes_the_unsensed_warning_before_the_takeoff_text():
    body = JS[JS.index("function startFleet()"):]
    body = body[: body.index("showFleetError('')")]
    assert body.index("unsensedWarning(droneInfo") < body.index("takeoffInstructions()") < body.index("will take off")


def test_map_draws_segments_by_status():
    assert "setUncoveredSegments" in MAP_JS and "renderSegments(S.layers.plan)" in MAP_JS
    style = MAP_JS[MAP_JS.index("function segmentStyle"):]
    style = style[: style.index("function renderSegments")]
    amber = style[: style.index("blocked_again")]
    assert all(s in amber for s in ("'offered'", "'acked'", "'flying'", "#d97706", "dashArray"))
    red_solid = style[style.index("blocked_again"): style.index("plan-segment-final")]
    assert "'failed'" in red_solid and "#dc2626" in red_solid and "dashArray" not in red_solid
    assert "dashArray: '8 6', className: 'plan-segment plan-segment-uncovered'" in style
    # The existing area drawing stays.
    assert "u.kind !== 'area'" in MAP_JS


def test_not_searched_box_is_still_in_the_page():
    assert 'id="fleet-uncovered"' in PAGE
