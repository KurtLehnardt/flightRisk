"""Ground side of the ceiling height and altitude slots (flightrisk/dashboard/ceiling.py).

Covers the settings rules and their persistence, changes allowed only on the
ground, the Plan and Start refusals, the per-drone slots in the plan, the
``set_ceiling`` sync to edges that report a ceiling (never to older edges),
and an end-to-end exchange with a real lane executor on a fake Tello. No
test touches the operator's ``~/.flightrisk/ceiling.json`` (see
``tests/conftest.py``).
"""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard.app import FleetSupervisor
from flightrisk.dashboard.ceiling import (
    CeilingBoard,
    CeilingRefusal,
    CeilingSettings,
    CeilingStore,
    default_settings_path,
)
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from tests.lane_executor_support import QuietLogger
from tests.test_fleet_dashboard import (
    FakeClock,
    FakeGroundTransport,
    FakeSocketIO,
    _telemetry,
)

DRONES = ("drone-1", "drone-2")


class CommandTransport(FakeGroundTransport):
    """The fleet test transport plus ``send_command``, answered by ``answer(drone_id, msg)``."""

    def __init__(self, drone_ids, clock, answer=None):
        super().__init__(drone_ids, clock)
        self.commands: list[tuple[str, dict]] = []
        self.answer = answer
        self.identity = None

    def send_command(self, drone_id, msg, *, timeout_s=2.0, attempts=1, timeout=None):
        fp.validate_message(msg)
        self.commands.append((drone_id, dict(msg)))
        return self.answer(drone_id, msg) if self.answer is not None else None


def board_for(tmp_path, transport=None, *, drones=DRONES, **kwargs):
    return CeilingBoard(
        CeilingStore(tmp_path / "ceiling.json"), transport, None, fleet_drones=drones,
        spawn=lambda fn, *args: fn(*args), logger=QuietLogger(), **kwargs,
    )


def supervisor_with_board(tmp_path, *, answer=None, **board_kwargs):
    clock = FakeClock()
    transport = CommandTransport(DRONES, clock, answer)
    board = board_for(tmp_path, transport, **board_kwargs)
    sio = FakeSocketIO()
    supervisor = FleetSupervisor(transport, DRONES, sio, area_cm=400, strip_cm=75, clock=clock, ceiling=board)
    return supervisor, board, transport, sio, clock


def report(supervisor, board, msg):
    """Deliver one edge control message the way the dashboard's ``_on_edge_control`` does."""
    supervisor.handle_control(msg)
    board.observe_control(msg)


def assigned_alt(transport):
    return {d: m["alt_cm"] for d, m in transport.sent if m["type"] == "assign_lanes"}


def edge_telemetry(drone_id, *, flying=False, ceiling=200, margin=60, slot=100):
    msg = _telemetry(drone_id, is_flying=flying)
    if ceiling is not None:
        msg.update(ceiling_cm=ceiling, ceiling_margin_cm=margin, alt_slot_cm=slot)
    return msg


# ---------------------------------------------------------------------------
# Settings and persistence
# ---------------------------------------------------------------------------


def test_settings_validation():
    CeilingSettings().validate()
    assert CeilingSettings().max_slot_cm == 140
    CeilingSettings(200, 60, {"drone-1": 50, "drone-2": 140}).validate()
    with pytest.raises(ValueError, match=r"drone-2 slot 160 cm is above the ceiling limit 140 cm \(ceiling 200 cm minus margin 60 cm\)"):
        CeilingSettings(200, 60, {"drone-1": 50, "drone-2": 160}).validate()
    for bad in (CeilingSettings(140, 60), CeilingSettings(200, 20), CeilingSettings(200, 60, {"drone-1": 10}),
                CeilingSettings(200, 60, {"bad id!": 50})):
        with pytest.raises(ValueError):
            bad.validate()


def test_persistence_round_trip_is_atomic_and_outside_the_repo(tmp_path):
    store = CeilingStore(tmp_path / "nested" / "ceiling.json")
    assert store.load() == (CeilingSettings(), None)  # no file: the defaults
    settings = CeilingSettings(230, 50, {"drone-1": 50, "drone-2": 80})
    store.save(settings)
    assert store.load() == (settings, None)
    assert json.loads(store.path.read_text()) == {
        "version": 1, "ceiling_cm": 230, "margin_cm": 50, "slots_cm": {"drone-1": 50, "drone-2": 80},
    }
    assert os.listdir(store.path.parent) == ["ceiling.json"]  # no temporary file left behind
    # The default file lives in the user's state dir, never in the repository.
    default = default_settings_path({})
    assert default == Path.home() / ".flightrisk" / "ceiling.json"
    assert Path(__file__).resolve().parents[1] not in default.parents
    assert default_settings_path({"FLIGHTRISK_CEILING_FILE": str(tmp_path / "x.json")}) == tmp_path / "x.json"


def test_a_failed_save_keeps_the_old_file(tmp_path, monkeypatch):
    store = CeilingStore(tmp_path / "ceiling.json")
    store.save(CeilingSettings(230, 50))

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.save(CeilingSettings(300, 50))
    monkeypatch.undo()
    assert store.load() == (CeilingSettings(230, 50), None)
    assert os.listdir(tmp_path) == ["ceiling.json"]


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"version": 1, "ceiling_cm": 200, "margin_cm": 60, "slots_cm": {"drone-2": 160}}',
     '{"version": 9, "ceiling_cm": 200, "margin_cm": 60}'],
)
def test_an_unreadable_file_refuses_plan_and_start_instead_of_using_defaults(tmp_path, content):
    (tmp_path / "ceiling.json").write_text(content)
    supervisor, board, transport, _sio, _ = supervisor_with_board(tmp_path)
    assert board.load_error
    with pytest.raises(CeilingRefusal, match="could not be loaded"):
        supervisor.plan()
    assert assigned_alt(transport) == {}
    assert "could not be loaded" in board.check_start({"zones": []})["fleet"]
    # Saving valid settings clears it.
    assert board.update({"ceiling_cm": 200, "margin_cm": 60}) is None
    assert board.load_error is None
    supervisor.plan()


def test_settings_survive_a_restart(tmp_path):
    board = board_for(tmp_path)
    assert board.update({"ceiling_cm": 180, "margin_cm": 60, "slots_cm": {"drone-1": 50, "drone-2": 80}}) is None
    restarted = board_for(tmp_path)
    assert restarted.settings == CeilingSettings(180, 60, {"drone-1": 50, "drone-2": 80})
    assert restarted.payload()["max_slot_cm"] == 120


# ---------------------------------------------------------------------------
# Operator changes: validated on the server, only on the ground
# ---------------------------------------------------------------------------


def test_update_validates_every_value_and_explains_refusals(tmp_path):
    board = board_for(tmp_path)
    error = board.update({"slots_cm": {"drone-2": 160}})
    assert error == (
        "Not changed: drone-2 slot 160 cm is above the ceiling limit 140 cm (ceiling 200 cm minus margin 60 cm)."
    )
    # A lower ceiling that the default slot of drone-2's zone (130 cm) would exceed.
    error = board.update({"ceiling_cm": 180})
    assert "drone-2 slot 130 cm is above the ceiling limit 120 cm" in error
    assert "ceiling height must be within 150-1000 cm" in board.update({"ceiling_cm": 120})
    assert "margin must be at least 30 cm" in board.update({"margin_cm": 10})
    assert "whole number" in board.update({"ceiling_cm": 200.5})
    assert "whole number" in board.update({"slots_cm": {"drone-1": "high"}})
    assert board.update("nonsense") == "Ceiling settings must be an object."
    assert board.settings == CeilingSettings()  # nothing changed
    assert not (tmp_path / "ceiling.json").exists()
    # Valid together: lower ceiling with a lower slot for drone-2.
    assert board.update({"ceiling_cm": 180, "slots_cm": {"drone-2": 110}}) is None
    assert board.settings == CeilingSettings(180, 60, {"drone-2": 110})
    # An empty slot goes back to the default.
    assert board.update({"ceiling_cm": 200, "slots_cm": {"drone-2": ""}}) is None
    assert board.settings.slots_cm == {}


def test_changes_are_refused_while_a_drone_flies_or_a_start_is_scheduled(tmp_path):
    supervisor, board, _transport, _sio, clock = supervisor_with_board(tmp_path)
    report(supervisor, board, edge_telemetry("drone-1", flying=True, ceiling=None))
    error = board.update({"ceiling_cm": 250})
    assert "only while every drone is on the ground" in error and "drone-1 reported flying" in error
    assert board.payload()["editable"] is False
    report(supervisor, board, edge_telemetry("drone-1", flying=False, ceiling=None))
    assert board.update({"ceiling_cm": 250}) is None

    supervisor.plan()
    for drone_id in DRONES:
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, len(supervisor.coordinator.lanes(drone_id))))
    assert supervisor.start().ok
    assert "a start is scheduled" in board.update({"ceiling_cm": 260})
    clock.now += 60.0  # long after the start time, every drone on the ground
    assert board.update({"ceiling_cm": 260}) is None


def test_a_standby_ground_refuses_changes(tmp_path):
    transport = CommandTransport(DRONES, FakeClock())
    transport.identity = types.SimpleNamespace(role="standby", fence=None)
    board = board_for(tmp_path, transport)
    assert "standby" in board.update({"ceiling_cm": 250})
    assert board.payload()["editable"] is False


def test_changes_are_logged(tmp_path):
    board = board_for(tmp_path)
    board.update({"ceiling_cm": 250, "slots_cm": {"drone-2": 160}})
    names = board._log.names()
    assert "ceiling_changed" in names
    changed = [f for _, e, f in board._log.events if e == "slot_changed"]
    assert changed == [{"drone_id": "drone-2", "alt_slot_cm": 160, "previous_alt_slot_cm": None, "source": "dashboard"}]


# ---------------------------------------------------------------------------
# Plan and Start
# ---------------------------------------------------------------------------


def test_plan_uses_the_dashboard_slots(tmp_path):
    supervisor, board, transport, _, _ = supervisor_with_board(tmp_path)
    assert board.update({"slots_cm": {"drone-1": 50, "drone-2": 80}}) is None
    plan = supervisor.plan()
    assert assigned_alt(transport) == {"drone-1": 50, "drone-2": 80}
    assert [zone.alt_cm for zone in plan.zones] == [50, 80]


def test_plan_with_default_slots_is_unchanged_below_the_limit(tmp_path):
    supervisor, _, transport, _, _ = supervisor_with_board(tmp_path)
    supervisor.plan()
    assert assigned_alt(transport) == {"drone-1": 100, "drone-2": 130}


def test_plan_is_refused_when_a_slot_is_above_the_ceiling_limit(tmp_path):
    # The incident: the old default of 160 cm for drone-2.
    supervisor, board, transport, sio, _ = supervisor_with_board(tmp_path, default_slots_cm=(100, 160))
    with pytest.raises(CeilingRefusal) as info:
        supervisor.plan()
    assert str(info.value) == "drone-2 slot 160 cm is above the ceiling limit 140 cm (ceiling 200 cm minus margin 60 cm)"
    assert assigned_alt(transport) == {}  # nothing was sent to any drone
    assert transport.sync_calls == []
    assert sio.named("fleet_event")[-1] == {"event": "plan_refused", "detail": str(info.value)}
    refused = [f for _, e, f in board._log.events if e == "slot_refused_above_ceiling"]
    assert refused[0]["drone_id"] == "drone-2" and refused[0]["source"] == "plan"


def test_plan_is_refused_until_an_edge_applies_the_dashboard_ceiling(tmp_path):
    supervisor, board, transport, _, _ = supervisor_with_board(tmp_path)
    # drone-2's edge was restarted with another ceiling; it reports it.
    report(supervisor, board, edge_telemetry("drone-2", ceiling=300, margin=60, slot=130))
    transport.commands.clear()
    with pytest.raises(CeilingRefusal, match="drone-2 has not applied the dashboard ceiling yet"):
        supervisor.plan()
    assert assigned_alt(transport) == {}
    assert [(d, m["ceiling_cm"]) for d, m in transport.commands] == [("drone-2", 200)]


def test_start_is_refused_when_the_slots_changed_since_plan(tmp_path):
    supervisor, board, transport, sio, _ = supervisor_with_board(tmp_path)
    supervisor.plan()
    for drone_id in DRONES:
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, len(supervisor.coordinator.lanes(drone_id))))
    assert board.update({"slots_cm": {"drone-2": 110}}) is None
    result = supervisor.start()
    assert not result.ok
    assert result.refusals == {"drone-2": "drone-2 was planned at 130 cm but its slot is now 110 cm; press Plan again"}
    assert "start_at" not in transport.sent_types("drone-2")
    assert sio.named("fleet_event")[-1]["event"] == "start_refused"


def test_start_is_refused_for_a_planned_slot_above_the_limit(tmp_path):
    board = board_for(tmp_path)
    plan = {"zones": [{"drone_id": "drone-1", "alt_cm": 100}, {"drone_id": "drone-2", "alt_cm": 160}]}
    refusals = board.check_start(plan)
    assert refusals == {
        "drone-2": "drone-2 slot 160 cm is above the ceiling limit 140 cm (ceiling 200 cm minus margin 60 cm)"
    }


# ---------------------------------------------------------------------------
# Sync to the edges
# ---------------------------------------------------------------------------


def test_an_edge_that_reports_a_ceiling_is_sent_the_settings_once(tmp_path):
    supervisor, board, transport, _, _ = supervisor_with_board(tmp_path)
    assert board.update({"slots_cm": {"drone-1": 50}}) is None
    report(supervisor, board, edge_telemetry("drone-1", slot=100))
    assert len(transport.commands) == 1
    drone_id, msg = transport.commands[0]
    assert drone_id == "drone-1"
    assert (msg["type"], msg["ceiling_cm"], msg["ceiling_margin_cm"], msg["alt_slot_cm"]) == ("set_ceiling", 200, 60, 50)
    assert "epoch" not in msg  # a v1 ground sends no fence
    # The edge reports the new values: nothing more to send.
    report(supervisor, board, edge_telemetry("drone-1", slot=50))
    report(supervisor, board, edge_telemetry("drone-1", slot=50))
    assert len(transport.commands) == 1
    assert board.payload()["drones"][0]["edge"]["in_sync"] is True


def test_an_older_edge_is_never_sent_set_ceiling(tmp_path):
    supervisor, board, transport, _, _ = supervisor_with_board(tmp_path)
    report(supervisor, board, edge_telemetry("drone-2", ceiling=None))  # no ceiling fields
    assert board.update({"ceiling_cm": 250, "slots_cm": {"drone-2": 80}}) is None
    assert transport.commands == []
    assert board.payload()["drones"][1]["edge"]["supported"] is False
    supervisor.plan()  # it still flies the slot the ground checked
    assert assigned_alt(transport)["drone-2"] == 80


def test_a_flying_edge_is_not_sent_set_ceiling_until_it_lands(tmp_path):
    supervisor, board, transport, _, _ = supervisor_with_board(tmp_path)
    report(supervisor, board, edge_telemetry("drone-1", flying=True, ceiling=250))
    assert transport.commands == []
    report(supervisor, board, edge_telemetry("drone-1", flying=False, ceiling=250))
    assert [m["ceiling_cm"] for _, m in transport.commands] == [200]


def test_edge_replies_are_shown(tmp_path):
    def refuse(drone_id, msg):
        return fp.make_ceiling_nack(drone_id, msg["cmd_id"], "in_flight", detail="only on the ground")

    supervisor, board, transport, _, _ = supervisor_with_board(tmp_path, answer=refuse)
    report(supervisor, board, edge_telemetry("drone-1", ceiling=250))
    edge = board.payload()["drones"][0]["edge"]
    assert edge["pending"] is False
    assert edge["refusal"] == {"reason": "in_flight", "detail": "only on the ground"}

    transport.answer = lambda drone_id, msg: None  # no reply in time
    board.update({"slots_cm": {"drone-1": 60}})
    assert board.payload()["drones"][0]["edge"]["note"] == "no_reply"


def test_a_leader_ground_fences_set_ceiling(tmp_path):
    transport = CommandTransport(DRONES, FakeClock())
    transport.identity = types.SimpleNamespace(role="leader", fence=fp.Fence(5, "mac"))
    board = board_for(tmp_path, transport)
    board.observe_control(edge_telemetry("drone-1", ceiling=250))
    msg = transport.commands[0][1]
    assert (msg["epoch"], msg["leader_id"]) == (5, "mac")


def test_end_to_end_with_a_real_executor(tmp_path):
    """The ground's set_ceiling reaches a real lane executor and its ack comes back."""
    drone = FakeTelloController(name="drone-1", battery=90)
    drone.connect()
    sent: list[dict] = []
    executor = LaneExecutor(drone, SafetyEnvelope(alt_slot_cm=100), "drone-1", send=sent.append, logger=QuietLogger())

    def deliver(drone_id, msg):
        executor.handle_set_ceiling(json.loads(json.dumps(msg)))
        reply = [m for m in sent if m.get("cmd_id") == msg["cmd_id"]][-1]
        fp.validate_edge_to_ground(reply, proto=1)
        return reply

    try:
        _supervisor, board, _transport, _, _ = supervisor_with_board(tmp_path, answer=deliver)
        assert board.update({"ceiling_cm": 240, "margin_cm": 60, "slots_cm": {"drone-1": 50}}) is None
        executor.tick()
        board.observe_control(executor.telemetry())  # first report: the edge is sent the settings
        assert executor.envelope.ceiling_status() == {"ceiling_cm": 240, "ceiling_margin_cm": 60, "alt_slot_cm": 50}
        assert board.payload()["drones"][0]["edge"]["in_sync"] is True
        # In flight the edge refuses a change, whatever the ground thinks.
        drone.takeoff()
        board._edges["drone-1"].flying = False  # a stale ground view
        assert board.update({"slots_cm": {"drone-1": 60}}) is None
        assert board.payload()["drones"][0]["edge"]["refusal"]["reason"] == "in_flight"
        assert executor.envelope.alt_slot_cm == 50
    finally:
        executor.shutdown(timeout=2.0)
