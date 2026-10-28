"""Ceiling height and altitude slots on the edge and in the protocol (docs/ceiling-and-altitude.md).

On 2026-10-28 a Tello climbed to its 160 cm slot and hit the ceiling. These
tests pin the edge's own enforcement, independent of any ground: the
envelope's ceiling rules, ``set_ceiling`` (only on the ground), the plan's
``alt_cm``, the climb after takeoff, the landing above the ceiling, startup
with a slot above the limit, the protocol's compatibility rules, and that
landing is never affected. Every drone is a ``FakeTelloController``; time is
a fake clock wherever it matters.
"""
from __future__ import annotations

import argparse
import math
import threading

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import (
    CeilingError,
    SafetyEnvelope,
    slot_above_ceiling,
    validate_ceiling,
)
from flightrisk.drone.tello import FakeTelloController
from flightrisk.edge_device import _handle_control, _startup_envelope
from flightrisk.fleet_authority import ACCEPT, IGNORE, EdgeAuthority
from tests.lane_executor_support import FakeClock, QuietLogger, of_type, wait_for

WAIT_S = 10.0
ZONE = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
LANES = [{"direction": "forward", "distance_cm": 100}]


@pytest.fixture
def make_executor():
    created: list[LaneExecutor] = []

    def _make(*, alt_slot_cm=100, takeoff_height_cm=80, ceiling_cm=200, margin_cm=60, authority=None):
        clock = FakeClock()
        drone = FakeTelloController(name="drone-1", battery=90, takeoff_height_cm=takeoff_height_cm)
        drone.connect()
        envelope = SafetyEnvelope(
            alt_slot_cm=alt_slot_cm, clock=clock, ceiling_cm=ceiling_cm, ceiling_margin_cm=margin_cm
        )
        sent: list[dict] = []
        log = QuietLogger()
        executor = LaneExecutor(
            drone, envelope, "drone-1", send=sent.append, clock=clock, logger=log, authority=authority
        )
        created.append(executor)
        return executor, drone, sent, log

    yield _make
    for executor in created:
        executor.shutdown(timeout=2.0)


def set_ceiling(ceiling=200, margin=60, slot=80, **kwargs):
    return fp.make_set_ceiling(ceiling, margin, slot, drone_id="drone-1", **kwargs)


def replies(sent, cmd_id):
    return [m for m in sent if m.get("type") in ("ack", "nack") and m.get("cmd_id") == cmd_id]


def events(log, name):
    return [fields for _, event, fields in log.events if event == name]


def fly(executor, drone, height):
    """Put the drone in the air at *height* without a flight sequence (as after a takeoff)."""
    drone.takeoff()
    drone.state.height = height
    executor.tick()


# ---------------------------------------------------------------------------
# Envelope rules
# ---------------------------------------------------------------------------


def test_defaults_give_an_effective_maximum_of_140_cm():
    envelope = SafetyEnvelope()
    assert (envelope.ceiling_cm, envelope.ceiling_margin_cm, envelope.max_alt_slot_cm) == (200, 60, 140)
    assert SafetyEnvelope(alt_slot_cm=140).alt_slot_cm == 140  # exactly at the limit is allowed
    with pytest.raises(CeilingError, match="slot 141 cm is above the ceiling limit 140 cm"):
        SafetyEnvelope(alt_slot_cm=141)
    # The incident's slot is refused out of the box.
    with pytest.raises(CeilingError, match=r"slot 160 cm is above the ceiling limit 140 cm \(ceiling 200 cm minus margin 60 cm\)"):
        SafetyEnvelope(alt_slot_cm=160)


@pytest.mark.parametrize(
    ("ceiling", "margin", "ok"),
    [
        (200, 60, True),
        (150, 30, True),  # the lowest room and the smallest margin
        (1000, 30, True),
        (149, 30, False),  # takeoff alone would come too close
        (1001, 60, False),
        (200, 29, False),  # margin below the floor
        (150, 131, False),  # leaves 19 cm: below the lowest slot
        (150, 130, True),  # leaves exactly the lowest slot (20 cm)
        (math.nan, 60, False),
        (math.inf, 60, False),
        (200, math.nan, False),
        (True, 60, False),
        ("200", 60, False),
        (None, 60, False),
    ],
)
def test_ceiling_and_margin_combinations(ceiling, margin, ok):
    if ok:
        assert validate_ceiling(ceiling, margin) == (float(ceiling), float(margin))
        SafetyEnvelope(alt_slot_cm=20, ceiling_cm=ceiling, ceiling_margin_cm=margin)
    else:
        with pytest.raises(ValueError):
            validate_ceiling(ceiling, margin)
        with pytest.raises(ValueError):
            SafetyEnvelope(alt_slot_cm=20, ceiling_cm=ceiling, ceiling_margin_cm=margin)


@pytest.mark.parametrize(
    ("slot", "ceiling", "margin", "above"),
    [(140, 200, 60, False), (141, 200, 60, True), (160, 250, 60, False), (191, 250, 60, True), (50, 150, 100, False)],
)
def test_slot_against_ceiling_combinations(slot, ceiling, margin, above):
    assert (slot_above_ceiling(slot, ceiling, margin) is not None) is above
    envelope = SafetyEnvelope(alt_slot_cm=20, ceiling_cm=ceiling, ceiling_margin_cm=margin)
    if above:
        with pytest.raises(CeilingError):
            envelope.set_alt_slot(slot)
        assert envelope.alt_slot_cm == 20  # unchanged
    else:
        envelope.set_alt_slot(slot)
        assert envelope.alt_slot_cm == slot


def test_configure_ceiling_is_all_or_nothing():
    envelope = SafetyEnvelope(alt_slot_cm=100)
    before = envelope.ceiling_status()
    with pytest.raises(CeilingError):
        envelope.configure_ceiling(150, 60, 100)  # 100 > 90
    with pytest.raises(ValueError):
        envelope.configure_ceiling(100, 60, 30)  # ceiling too low
    with pytest.raises(ValueError):
        envelope.configure_ceiling(200, 60, 10)  # slot below the slot range
    assert envelope.ceiling_status() == before
    envelope.configure_ceiling(250, 60, 160)
    assert envelope.ceiling_status() == {"ceiling_cm": 250, "ceiling_margin_cm": 60, "alt_slot_cm": 160}
    assert envelope.max_alt_slot_cm == 190


def test_climb_allowed_and_above_ceiling():
    envelope = SafetyEnvelope(alt_slot_cm=100)
    assert envelope.climb_allowed(80, 60)  # ends at 140, the limit
    assert not envelope.climb_allowed(80, 61)
    assert not envelope.climb_allowed(None, 20)
    assert not envelope.above_ceiling(200)
    assert envelope.above_ceiling(201)
    assert not envelope.above_ceiling(None)


# ---------------------------------------------------------------------------
# Edge enforcement, independent of the ground
# ---------------------------------------------------------------------------


def test_plan_slot_above_the_edge_ceiling_is_refused(make_executor):
    executor, _, sent, log = make_executor()
    # What an older ground sends drone-2 by default: 160 cm.
    msg = {"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 160, "lanes": LANES, "zone": ZONE}
    assert executor.handle_assign_lanes(msg) is False
    nacks = of_type(sent, "nack")
    assert nacks and nacks[-1]["reason"] == "altitude"
    assert executor.envelope.alt_slot_cm == 100
    assert events(log, "slot_refused_above_ceiling")[0]["source"] == "assign_lanes"

    # The same plan in a room configured for it is accepted.
    executor, _, sent, _ = make_executor(ceiling_cm=250)
    assert executor.handle_assign_lanes(msg) is True
    assert executor.envelope.alt_slot_cm == 160


def test_v2_plan_slot_above_the_edge_ceiling_is_refused(make_executor):
    executor, _, sent, _ = make_executor()
    msg = fp.make_assign_lanes(
        "drone-1", 150, LANES, ZONE, fence=fp.Fence(1, "mac"), plan_id="p-1", plan_seq=1, cmd_id="c-1"
    )
    executor.handle_message(msg)
    assert [r["reason"] for r in replies(sent, "c-1")] == ["altitude"]


def test_climb_to_a_slot_above_the_limit_is_refused_and_the_drone_lands(make_executor):
    executor, drone, _sent, log = make_executor(alt_slot_cm=130)
    # A slot above the limit can only get into the envelope through a bug;
    # the climb itself still refuses it.
    executor.envelope.alt_slot_cm = 300
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("land",)]
    assert events(log, "slot_refused_above_ceiling")[0]["source"] == "climb"
    assert ("warning", "edge_land", {"drone_id": "drone-1", "reason": "ceiling"}) in log.events


def test_climb_from_the_measured_height_is_checked_against_the_limit(make_executor, monkeypatch):
    executor, drone, _, log = make_executor(alt_slot_cm=130, takeoff_height_cm=80)
    # Whatever computed the move, a climb ending above 140 cm is not flown.
    monkeypatch.setattr(executor.envelope, "slot_move", lambda height: ("up", 70))
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert ("move", "up", 70) not in drone.calls
    assert drone.calls[-1] == ("land",)
    refused = events(log, "slot_refused_above_ceiling")[0]
    assert refused["height_cm"] == 80 and refused["distance_cm"] == 70


def test_climb_to_the_limit_and_descent_are_flown(make_executor):
    executor, drone, _, _ = make_executor(alt_slot_cm=140, takeoff_height_cm=80)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "up", 60)]
    # A takeoff that overshoots above the limit descends to the slot.
    executor, drone, _, _ = make_executor(alt_slot_cm=50, takeoff_height_cm=150)
    executor.handle_takeoff()
    assert executor.wait_idle(WAIT_S)
    assert drone.calls == [("takeoff",), ("move", "down", 100)]


def test_measured_height_above_the_ceiling_lands(make_executor):
    executor, drone, _, log = make_executor()
    fly(executor, drone, 200)  # at the ceiling, not above it
    assert ("land",) not in drone.calls
    drone.state.height = 201
    executor.tick()
    assert drone.calls[-1] == ("land",)
    exceeded = events(log, "edge_ceiling_exceeded")
    assert exceeded == [{"drone_id": "drone-1", "height_cm": 201, "ceiling_cm": 200, "alt_slot_cm": 100}]
    assert ("warning", "edge_land", {"drone_id": "drone-1", "reason": "ceiling_exceeded"}) in log.events


def test_above_the_ceiling_stops_a_running_climb(make_executor):
    executor, drone, _, _ = make_executor(alt_slot_cm=130)
    entered, release = threading.Event(), threading.Event()
    original_move = drone.move

    def slow_move(direction, distance):
        entered.set()
        release.wait(WAIT_S)
        return original_move(direction, distance)

    drone.move = slow_move
    executor.handle_takeoff()
    assert entered.wait(WAIT_S)
    drone.state.height = 230  # a bad reading or a drift: above the ceiling mid-climb
    executor.tick()
    release.set()
    assert executor.wait_idle(WAIT_S)
    assert ("land",) in drone.calls
    assert executor.state in ("idle", "landing")


@pytest.mark.parametrize("command", [{"type": "land"}, {"type": "emergency_land_all"}, {"type": "land", "cmd_id": "l-1"}])
def test_landing_is_unaffected_by_an_over_ceiling_state(make_executor, command):
    executor, drone, sent, _ = make_executor()
    fly(executor, drone, 260)  # above the ceiling: the monitor lands
    landings = drone.calls.count(("land",))
    assert landings == 1
    # The drone is still flying by its own report (a land that did not take).
    drone.state.is_flying = True
    drone.state.height = 260
    executor.handle_message(command)
    assert drone.calls.count(("land",)) == 2  # executed at once, not refused or delayed
    if "cmd_id" in command:
        assert [m["type"] for m in replies(sent, "l-1")] == ["ack"]
    assert of_type(sent, "nack") == []


def test_landing_is_unaffected_by_a_bad_set_ceiling(make_executor):
    executor, drone, _, _ = make_executor()
    fly(executor, drone, 100)
    _handle_control({"type": "set_ceiling", "cmd_id": "x", "ceiling_cm": "high"}, None, QuietLogger(), flight=executor)
    executor.handle_message({"type": "land"})
    assert drone.calls[-1] == ("land",)


# ---------------------------------------------------------------------------
# set_ceiling: on the ground only
# ---------------------------------------------------------------------------


def test_set_ceiling_on_the_ground_is_applied_and_acknowledged(make_executor):
    executor, _, sent, log = make_executor(alt_slot_cm=100)
    msg = set_ceiling(250, 70, 50)
    _handle_control(msg, None, QuietLogger(), flight=executor)
    assert replies(sent, msg["cmd_id"]) == [{
        "type": "ack", "for": "set_ceiling", "drone_id": "drone-1", "cmd_id": msg["cmd_id"], "result": "ok",
        "ceiling_cm": 250, "ceiling_margin_cm": 70, "alt_slot_cm": 50,
    }]
    assert executor.envelope.ceiling_status() == {"ceiling_cm": 250, "ceiling_margin_cm": 70, "alt_slot_cm": 50}
    assert events(log, "ceiling_changed")[0]["drone_id"] == "drone-1"
    assert events(log, "slot_changed")[0] == {
        "drone_id": "drone-1", "alt_slot_cm": 50, "previous_alt_slot_cm": 100, "source": "set_ceiling"
    }
    for reply in sent:
        fp.validate_edge_to_ground(reply, proto=1)
        fp.validate_edge_to_ground(reply, proto=2)


def test_set_ceiling_in_flight_is_refused_and_accepted_after_landing(make_executor):
    executor, drone, sent, _ = make_executor()
    fly(executor, drone, 100)
    msg = set_ceiling(250, 60, 80)
    assert executor.handle_set_ceiling(msg) is False
    nack = replies(sent, msg["cmd_id"])[0]
    assert nack["reason"] == "in_flight"
    assert nack["ceiling_cm"] == 200 and nack["alt_slot_cm"] == 100  # what stays in force
    assert executor.envelope.alt_slot_cm == 100

    executor.land_now("test")
    executor.tick()
    again = set_ceiling(250, 60, 80)
    assert executor.handle_set_ceiling(again) is True
    assert executor.envelope.alt_slot_cm == 80


def test_set_ceiling_while_a_start_is_scheduled_is_refused(make_executor):
    executor, _, sent, _ = make_executor()
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    executor.handle_assign_lanes({"type": "assign_lanes", "drone_id": "drone-1", "alt_cm": 100, "lanes": LANES, "zone": ZONE})
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": executor.now() + 60.0})
    msg = set_ceiling(250, 60, 80)
    assert executor.handle_set_ceiling(msg) is False
    assert replies(sent, msg["cmd_id"])[0]["reason"] == "in_flight"


def test_set_ceiling_slot_above_the_new_limit_is_refused(make_executor):
    executor, _, sent, log = make_executor()
    msg = set_ceiling(200, 60, 160)
    assert executor.handle_set_ceiling(msg) is False
    nack = replies(sent, msg["cmd_id"])[0]
    assert nack["reason"] == "above_ceiling"
    assert "slot 160 cm is above the ceiling limit 140 cm" in nack["detail"]
    assert executor.envelope.ceiling_status() == {"ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 100}
    assert events(log, "slot_refused_above_ceiling")[0]["source"] == "set_ceiling"


@pytest.mark.parametrize(
    "bad",
    [
        {"ceiling_cm": 120},  # ceiling below 150 cm
        {"ceiling_margin_cm": 10},  # margin below 30 cm
        {"alt_slot_cm": 5},  # slot below the slot range
        {"ceiling_cm": "200"},  # malformed
        {"drone_id": "drone-9"},  # another drone's
    ],
)
def test_set_ceiling_invalid_values_are_refused(make_executor, bad):
    executor, _, sent, _ = make_executor()
    msg = {**set_ceiling(), **bad}
    assert executor.handle_set_ceiling(msg) is False
    assert replies(sent, msg["cmd_id"])[0]["reason"] == "invalid"
    assert executor.envelope.ceiling_status() == {"ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 100}


def test_set_ceiling_duplicate_cmd_id_is_not_applied_twice(make_executor):
    executor, drone, sent, _ = make_executor()
    msg = set_ceiling(250, 60, 80)
    assert executor.handle_set_ceiling(msg) is True
    first = replies(sent, msg["cmd_id"])[0]
    fly(executor, drone, 100)  # even in flight the duplicate gets the first answer
    assert executor.handle_set_ceiling(dict(msg)) is False
    assert replies(sent, msg["cmd_id"]) == [first, first]


def test_set_ceiling_needs_the_authority(make_executor):
    class StaleAuthority:
        def check(self, msg):
            return "stale_epoch"

        def epoch_seen(self):
            return 7

    executor, _, sent, _ = make_executor(authority=StaleAuthority())
    msg = set_ceiling(250, 60, 80, fence=(3, "mac"))
    assert executor.handle_set_ceiling(msg) is False
    nack = replies(sent, msg["cmd_id"])[0]
    assert (nack["reason"], nack["epoch_seen"]) == ("stale_epoch", 7)
    assert executor.envelope.alt_slot_cm == 100


def test_set_ceiling_without_cmd_id_is_dropped(make_executor):
    executor, _, sent, _ = make_executor()
    assert executor.handle_set_ceiling({"type": "set_ceiling", "ceiling_cm": 250}) is False
    assert sent == []


def test_telemetry_reports_the_active_ceiling_and_slot(make_executor):
    executor, _, sent, _ = make_executor(alt_slot_cm=80)
    executor.tick()
    telemetry = of_type(sent, "telemetry")[-1]
    assert (telemetry["ceiling_cm"], telemetry["ceiling_margin_cm"], telemetry["alt_slot_cm"]) == (200, 60, 80)
    # Optional fields: valid for v1 and v2 grounds alike.
    fp.validate_edge_to_ground(telemetry, proto=1)
    fp.validate_edge_to_ground({**telemetry, "epoch": 0, "plan_id": None, "state": "idle", "plan_valid": False}, proto=2)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_startup_with_alt_cm_above_the_limit_fails_loudly():
    args = argparse.Namespace(alt_slot=160.0)
    with pytest.raises(SystemExit) as info:
        _startup_envelope(args, environ={})
    message = str(info.value)
    assert "slot 160 cm is above the ceiling limit 140 cm" in message and "Refusing to start" in message
    # Never clamped: in a room configured for it the same slot starts.
    envelope = _startup_envelope(args, environ={"FLIGHTRISK_CEILING_CM": "250"})
    assert envelope.alt_slot_cm == 160
    assert _startup_envelope(argparse.Namespace(alt_slot=140.0), environ={}).alt_slot_cm == 140


def test_main_refuses_a_flying_edge_with_alt_cm_above_the_limit(monkeypatch):
    from flightrisk import edge_device

    started = []
    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda *a, **k: started.append(a))
    monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: False)
    with pytest.raises(SystemExit, match="above the ceiling limit"):
        edge_device.main(["--source", "fake", "--alt-slot", "160"])
    assert started == []
    monkeypatch.setenv("FLIGHTRISK_CEILING_CM", "120")
    with pytest.raises(SystemExit, match="ceiling height must be within"):
        edge_device.main(["--source", "fake", "--alt-slot", "50"])
    assert started == []


# ---------------------------------------------------------------------------
# Protocol and compatibility
# ---------------------------------------------------------------------------


def test_set_ceiling_is_valid_on_v1_and_v2_links_and_keeps_its_cmd_id():
    msg = fp.make_set_ceiling(200, 60, 80, drone_id="drone-2")
    for proto in (1, 2):
        assert fp.validate_ground_to_edge(msg, proto=proto) == "set_ceiling"
        assert fp.shape_for_peer(msg, proto) == msg  # the cmd_id survives a v1 link
    fenced = fp.make_set_ceiling(200, 60, 80, fence=(4, "mac"))
    raw = fp.encode(fenced, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=2)
    assert fp.decode(raw, direction=fp.DIRECTION_GROUND_TO_EDGE, proto=2, require_fence=True) == fenced
    for bad in ({"cmd_id": None}, {"ceiling_cm": -1}, {"alt_slot_cm": True}, {"epoch": 3}, {"leader_id": "mac"}):
        with pytest.raises(fp.ProtocolError):
            fp.validate_message({**msg, **bad})
    # Not a flight command: the v1 and v2 flight command sets are unchanged.
    assert "set_ceiling" not in fp.GROUND_TO_EDGE_TYPES_V2 | fp.COMMAND_TYPES | fp.FENCED_COMMAND_TYPES


def test_set_ceiling_replies_are_valid_on_both_versions():
    active = {"ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 100}
    ack = fp.make_ceiling_ack("drone-1", "c-1", active)
    nack = fp.make_ceiling_nack("drone-1", "c-1", "in_flight", active=active, detail="only on the ground")
    for reply in (ack, nack):
        for proto in (1, 2):
            fp.validate_edge_to_ground(reply, proto=proto)
            assert fp.shape_for_peer(reply, proto) == reply
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({**nack, "reason": "geofence"})
    with pytest.raises(fp.ProtocolError):
        fp.validate_message({k: v for k, v in ack.items() if k != "alt_slot_cm"})


def test_an_edge_without_a_flight_executor_ignores_set_ceiling():
    log = QuietLogger()
    _handle_control(fp.make_set_ceiling(200, 60, 80), None, log, flight=None)
    assert "edge_control_error" not in log.names()


def test_multi_ground_edge_takes_set_ceiling_only_from_its_authority():
    auth = EdgeAuthority("drone-1", "win")
    auth.opened("leader", "ws://leader")
    auth.on_message("leader", fp.make_ground_hello("mac", "leader", 2))
    auth.opened("standby", "ws://standby")
    auth.on_message("standby", fp.make_ground_hello("pc", "standby", 2))
    msg = fp.make_set_ceiling(200, 60, 80, fence=(2, "mac"))
    assert auth.on_message("leader", msg).action == ACCEPT
    assert auth.check(msg) is None
    assert auth.on_message("standby", fp.make_set_ceiling(200, 60, 80, fence=(2, "pc"))).action == IGNORE
    assert auth.check(fp.make_set_ceiling(200, 60, 80)) == "not_authority"  # no fence
    assert auth.check(fp.make_set_ceiling(200, 60, 80, fence=(1, "mac"))) == "stale_epoch"


def test_set_ceiling_from_the_authority_reaches_the_executor_and_is_answered(make_executor):
    """End to end on the edge: authority accepts, the executor applies, the reply goes out valid for v2."""
    executor, _, sent, _ = make_executor()
    auth = EdgeAuthority("drone-1", "win")
    auth.opened("leader", "ws://leader")
    auth.on_message("leader", fp.make_ground_hello("mac", "leader", 2))
    executor._authority = auth
    msg = fp.make_set_ceiling(220, 60, 120, drone_id="drone-1", fence=(2, "mac"))
    assert auth.on_message("leader", msg).action == ACCEPT
    assert executor.handle_set_ceiling(msg) is True
    out = auth.outbound(replies(sent, msg["cmd_id"])[0])
    fp.validate_edge_to_ground(out, proto=2)
    assert out["alt_slot_cm"] == 120 and out["epoch_seen"] == 2
    assert wait_for(lambda: executor.envelope.ceiling_cm == 220)
