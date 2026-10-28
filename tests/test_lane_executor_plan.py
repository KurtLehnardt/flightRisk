"""Protocol v2 plans, command ids, authority and telemetry of the lane executor.

Covers plan ordering by ``(epoch, plan_seq)`` (``stale_plan``,
``plan_mismatch``, the same plan resent), plan validity across every kind of
landing (the stale-plan restart fix), at-most-once execution per ``cmd_id``,
the authority hook, the v2 telemetry fields and states, and the v1
behaviour that must not change. Executors are real ``LaneExecutor``s on
fake Tellos with a fake clock; every message they send is validated.
"""
from __future__ import annotations

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.command_cache import DONE, MISMATCH, NEW, PENDING, CommandCache
from flightrisk.drone.lane_executor import (
    ALL_CONTROL_TYPES,
    CONTROL_TYPES,
    V2_CONTROL_TYPES,
)
from flightrisk.drone.lane_plan import (
    PlanIdentity,
    classify_plan,
    parse_on_complete,
    plan_identity,
)
from tests.lane_executor_support import (
    LANES,
    WAIT_S,
    SteppedTello,
    assign,
    hold,
    horizontal_moves,
    make_v2_executor,
    nudge,
    of_type,
    replies_for,
    reply_for,
    resume,
    send,
    start_at,
    takeoff,
    validate_sent,
    wait_for,
)

# The optional ceiling fields (docs/ceiling-and-altitude.md) come with every telemetry message.
CEILING_KEYS = {"ceiling_cm", "ceiling_margin_cm", "alt_slot_cm"}
# So does the optional manual-control mode (docs/manual-takeover.md).
TELEMETRY_V2_KEYS = set(fp.TELEMETRY_FIELDS_V2) | {"type", "control_mode"} | CEILING_KEYS


@pytest.fixture
def executors():
    created = []

    def _make(drone=None, **kwargs):
        made = make_v2_executor(drone=drone, **kwargs)
        created.append(made)
        return made

    yield _make
    for executor, drone, sent, _, _ in created:
        if isinstance(drone, SteppedTello):
            drone.auto = True
            drone.release(50)
        executor.shutdown(timeout=2.0)
        validate_sent(sent)


def fly(executor, clock, plan_id="p-1", cmd_id=None):
    send(executor, start_at(clock() + 0.5, plan_id=plan_id, cmd_id=cmd_id))
    clock.advance(0.5)
    assert wait_for(lambda: executor.lane_index == executor.lanes_total and not executor.is_busy)


def ticks(executor, clock, seconds, step=0.5, feed=True):
    for _ in range(round(seconds / step)):
        clock.advance(step)
        if feed:
            executor.note_ground_message()
        executor.tick()


# ---------------------------------------------------------------------------
# Plan identity and ordering
# ---------------------------------------------------------------------------


def test_plan_identity_parsing():
    assert plan_identity({"plan_id": "p", "plan_seq": 2, "epoch": 4}, v2=True) == PlanIdentity("p", (4, 2))
    assert plan_identity({}, v2=False) == PlanIdentity(None, None)
    assert plan_identity({"plan_seq": 1}, v2=False) == PlanIdentity(None, (0, 1))
    for bad in ({"plan_seq": 1}, {"plan_id": "p"}, {"plan_id": "p", "plan_seq": 0},
                {"plan_id": "p", "plan_seq": True}, {"plan_id": "p p", "plan_seq": 1},
                {"plan_id": "p", "plan_seq": 1, "epoch": -1}):
        with pytest.raises(ValueError):
            plan_identity(bad, v2=True)


def test_classify_plan_rules():
    cur = PlanIdentity("b", (3, 2))
    kw = {"current_valid": True, "same_content": True}
    assert classify_plan(PlanIdentity("a", (3, 1)), cur, (3, 2), **kw) == "stale_plan"
    assert classify_plan(PlanIdentity("z", (2, 9)), cur, (3, 2), **kw) == "stale_plan"
    assert classify_plan(PlanIdentity("c", (3, 2)), cur, (3, 2), **kw) == "plan_mismatch"
    assert classify_plan(PlanIdentity("b", (3, 2)), cur, (3, 2), **kw) == "same"
    assert classify_plan(PlanIdentity("b", (3, 2)), cur, (3, 2), current_valid=True, same_content=False) == "plan_mismatch"
    assert classify_plan(PlanIdentity("b", (3, 2)), cur, (3, 2), current_valid=False, same_content=True) == "stale_plan"
    assert classify_plan(PlanIdentity("b", (3, 3)), cur, (3, 2), **kw) == "plan_mismatch"
    assert classify_plan(PlanIdentity("c", (3, 3)), cur, (3, 2), **kw) == "new"
    assert classify_plan(PlanIdentity("c", (4, 1)), cur, (3, 2), **kw) == "new"
    assert classify_plan(PlanIdentity(None, None), cur, (3, 2), **kw) == "new"
    # A v1 plan replaced the newest ordered plan: a copy of that plan is stale.
    assert classify_plan(PlanIdentity("b", (3, 2)), PlanIdentity(None, None), (3, 2), **kw) == "stale_plan"


def test_parse_on_complete():
    assert parse_on_complete({}) == (None, 20.0)
    assert parse_on_complete({"on_complete": "land"}) == ("land", 20.0)
    assert parse_on_complete({"on_complete": "hover"}) == ("hover", 20.0)
    assert parse_on_complete({"on_complete": "hover", "hover_timeout_s": 120}) == ("hover", 120.0)
    for bad in ({"on_complete": "orbit"}, {"on_complete": None}, {"hover_timeout_s": 5},
                {"on_complete": "land", "hover_timeout_s": 5}, {"on_complete": "hover", "hover_timeout_s": 0},
                {"on_complete": "hover", "hover_timeout_s": 121}, {"on_complete": "hover", "hover_timeout_s": "5"}):
        with pytest.raises(ValueError):
            parse_on_complete(bad)


def test_older_and_conflicting_plans_are_refused(executors):
    executor, _, sent, _, _ = executors()
    send(executor, assign(plan_id="p-5", plan_seq=5, cmd_id="a5"))
    assert reply_for(sent, "a5")["type"] == "ack"
    cases = [
        (assign(plan_id="p-4", plan_seq=4, cmd_id="a4"), "stale_plan"),
        (assign(plan_id="p-9", plan_seq=9, fence=fp.Fence(2, "mac"), cmd_id="e2"), "stale_plan"),
        (assign(plan_id="p-x", plan_seq=5, cmd_id="x5"), "plan_mismatch"),
        (assign(lanes=LANES[:1], plan_id="p-5", plan_seq=5, cmd_id="c5"), "plan_mismatch"),
        (assign(plan_id="p-5", plan_seq=6, cmd_id="r6"), "plan_mismatch"),
    ]
    for msg, reason in cases:
        send(executor, msg)
        nack = reply_for(sent, msg["cmd_id"])
        assert (nack["type"], nack["reason"], nack["lanes_total"]) == ("nack", reason, len(msg["lanes"]))
        assert executor.plan_id == "p-5" and executor.lanes_total == 3
    # A newer epoch needs no knowledge of the old leader's plan_seq.
    send(executor, assign(plan_id="q-1", plan_seq=1, fence=fp.Fence(4, "win"), cmd_id="q1"))
    assert reply_for(sent, "q1")["type"] == "ack" and executor.plan_id == "q-1"


def test_the_same_plan_resent_is_acked_and_changes_nothing(executors):
    executor, drone, sent, clock, _ = executors(drone=SteppedTello(name="drone-1", battery=90))
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    drone.release()  # climb
    assert wait_for(lambda: len(drone.move_log) == 2)
    drone.release()
    assert wait_for(lambda: len(drone.move_log) == 3)  # lane 1 in flight
    gen = executor._plan_gen
    send(executor, assign(cmd_id="a1-again"))  # same plan, new cmd_id
    ack = reply_for(sent, "a1-again")
    assert ack["type"] == "ack" and ack["lanes_total"] == 3 and ack["plan_id"] == "p-1"
    assert executor._plan_gen == gen and executor.lane_index == 1
    drone.auto = True
    drone.release()
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    assert horizontal_moves(drone) == [("move", "forward", 200), ("move", "right", 100), ("move", "back", 200)]


# ---------------------------------------------------------------------------
# Plan validity across landings (the stale-plan restart fix, plan 2.8)
# ---------------------------------------------------------------------------


def _land_by(kind, executor, drone, clock):
    if kind == "ground_land":
        send(executor, fp.make_land(cmd_id="l1"))
    elif kind == "emergency_land_all":
        send(executor, fp.make_emergency_land_all())
    elif kind == "local_key":
        executor.land_now("local_key")
    elif kind == "link_loss":
        ticks(executor, clock, 4.0, feed=False)
    elif kind == "low_battery":
        drone.state.battery = 10
        executor.tick()
    elif kind == "unplanned":
        drone.land()  # the Tello's own auto-land, not requested by the executor
        executor.tick()
    elif kind == "hover_timeout":
        ticks(executor, clock, 21.0)
    assert wait_for(lambda: not drone.state.is_flying)


@pytest.mark.parametrize(
    "kind", ["ground_land", "emergency_land_all", "local_key", "link_loss", "low_battery", "unplanned", "hover_timeout"]
)
def test_every_landing_invalidates_the_plan_until_a_new_one(executors, kind):
    executor, drone, sent, clock, _log = executors()
    send(executor, assign(cmd_id="a1", on_complete="hover"))
    fly(executor, clock, cmd_id="s1")
    assert executor.plan_valid and drone.state.is_flying
    _land_by(kind, executor, drone, clock)
    executor.tick()
    assert not executor.plan_valid and executor.telemetry()["plan_valid"] is False
    calls = list(drone.calls)

    # The old plan cannot restart: not by start_at, not by resume, not by a
    # retried or late copy of its assign_lanes.
    send(executor, start_at(clock() + 0.5, cmd_id="s2"))
    assert reply_for(sent, "s2")["reason"] == "no_valid_plan"
    send(executor, resume("p-1", cmd_id="r2"))
    assert reply_for(sent, "r2")["reason"] in ("no_valid_plan", "landing")
    send(executor, assign(cmd_id="a1-retry", on_complete="hover"))
    assert reply_for(sent, "a1-retry")["reason"] == "stale_plan"
    clock.advance(1.0)
    assert not wait_for(lambda: drone.calls != calls, timeout=0.2)
    # The plan identity is kept for the hello and telemetry.
    assert executor.plan_id == "p-1"

    # A new plan (new plan_id, higher plan_seq) flies again.
    drone.state.battery = 90
    send(executor, assign(plan_id="p-2", plan_seq=2, cmd_id="a2"))
    assert reply_for(sent, "a2")["type"] == "ack" and executor.plan_valid
    send(executor, start_at(clock() + 0.5, plan_id="p-1", cmd_id="s3"))
    assert reply_for(sent, "s3")["reason"] == "plan_mismatch"
    fly(executor, clock, plan_id="p-2", cmd_id="s4")
    assert reply_for(sent, "s4")["type"] == "ack"
    assert drone.state.is_flying


def test_start_refused_when_a_nudge_moved_the_plan_out_of_the_geofence(executors):
    executor, drone, sent, clock, _ = executors(geofence_cm=300)
    send(executor, takeoff(cmd_id="t1"))
    assert executor.wait_idle(WAIT_S)
    send(executor, assign(lanes=[{"direction": "forward", "distance_cm": 250}], cmd_id="a1"))
    send(executor, nudge("forward", 100, cmd_id="n1"))
    assert wait_for(lambda: reply_for(sent, "n1") is not None)
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    assert reply_for(sent, "s1")["reason"] == "geofence"
    assert executor.plan_valid and drone.state.is_flying and not executor.is_busy


def test_geofence_landing_in_flight_invalidates_the_plan(executors):
    # The in-flight geofence check is the backstop: put the dead-reckoned
    # pose next to the fence after the start was accepted.
    executor, drone, _sent, clock, _ = executors(drone=SteppedTello(name="drone-1", battery=90), geofence_cm=300)
    send(executor, assign(lanes=[{"direction": "forward", "distance_cm": 250}], cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: drone.waiting)  # the climb
    executor.envelope.record_move("forward", 100)
    drone.release()
    assert wait_for(lambda: ("land",) in drone.calls and not executor.is_busy)
    assert not executor.plan_valid and horizontal_moves(drone) == []


def test_v1_start_after_a_landing_needs_a_new_assignment(executors):
    executor, drone, sent, clock, log = executors(fleet_v2=False)
    v1_assign = {"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES}
    assert executor.handle_assign_lanes(v1_assign)
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": clock() + 0.5})
    clock.advance(0.5)
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    executor.handle_message({"type": "land"})
    takeoffs = drone.calls.count(("takeoff",))
    # Before the fix a v1 start_at here restarted the old lanes.
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": clock() + 0.5}) is False
    clock.advance(1.0)
    assert not wait_for(lambda: drone.calls.count(("takeoff",)) > takeoffs, timeout=0.2)
    assert any(e == "edge_start_refused" and kw["reason"] == "no_valid_plan" for _, e, kw in log.events)
    # No v2 reply is sent for a v1 command, and telemetry stays v1.
    assert of_type(sent, "nack") == []
    assert "plan_valid" not in executor.telemetry()
    assert executor.handle_assign_lanes(v1_assign)
    assert executor.handle_start_at({"type": "start_at", "t_ground_s": clock() + 0.5})


def test_invalidate_plan_is_the_authority_entry_point(executors):
    executor, _, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    assert executor.invalidate_plan("epoch_adopted") is True
    assert executor.invalidate_plan("epoch_adopted") is False
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    assert reply_for(sent, "s1")["reason"] == "no_valid_plan"


def test_start_at_plan_checks_and_refusal_reasons(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, start_at(clock() + 0.5, cmd_id="s0"))
    assert reply_for(sent, "s0")["reason"] == "no_valid_plan"
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, plan_id="p-9", cmd_id="s1"))
    assert reply_for(sent, "s1")["reason"] == "plan_mismatch"
    no_plan_id = {"type": "start_at", "t_ground_s": clock() + 0.5, "epoch": 3, "leader_id": "mac", "cmd_id": "s2"}
    send(executor, no_plan_id)
    assert (reply_for(sent, "s2")["reason"], reply_for(sent, "s2")["detail"]) == ("invalid", "invalid_plan_id")
    send(executor, start_at(clock() + 500, cmd_id="s3"))
    assert (reply_for(sent, "s3")["reason"], reply_for(sent, "s3")["detail"]) == ("invalid", "too_far_ahead")
    send(executor, start_at(clock() + 0.5, cmd_id="s4"))
    send(executor, start_at(clock() + 0.5, cmd_id="s5"))
    assert reply_for(sent, "s5")["reason"] == "busy"
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s4") is not None)
    ack = reply_for(sent, "s4")
    assert ack["type"] == "ack" and ack["executed_at_ground_s"] == pytest.approx(clock()) and ack["plan_id"] == "p-1"
    assert drone.calls.count(("takeoff",)) == 1


def test_start_at_refused_with_battery_and_landing_reasons(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    drone.state.battery = 20
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s1") is not None)
    assert reply_for(sent, "s1")["reason"] == "battery"
    drone.state.battery = 90
    send(executor, start_at(clock() + 5, cmd_id="s2"))
    send(executor, fp.make_land(cmd_id="l1"))
    assert wait_for(lambda: reply_for(sent, "s2") is not None)
    assert reply_for(sent, "s2")["reason"] == "landing"
    assert ("takeoff",) not in drone.calls


# ---------------------------------------------------------------------------
# Command ids: at most once, first outcome final
# ---------------------------------------------------------------------------


def test_duplicate_commands_are_never_executed_twice(executors):
    executor, drone, sent, clock, _ = executors()
    first = assign(cmd_id="a1")
    send(executor, first)
    gen = executor._plan_gen
    send(executor, dict(first))
    assert replies_for(sent, "a1") == [reply_for(sent[:1], "a1")] * 2  # the cached ack, unchanged
    assert executor._plan_gen == gen

    # A start still pending: the duplicate gets nothing and starts nothing.
    start = start_at(clock() + 1.0, cmd_id="s1")
    send(executor, start)
    send(executor, dict(start))
    assert replies_for(sent, "s1") == []
    clock.advance(1.0)
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    assert drone.calls.count(("takeoff",)) == 1
    send(executor, dict(start))
    acks = replies_for(sent, "s1")
    assert len(acks) == 2 and acks[0] == acks[1]
    assert drone.calls.count(("takeoff",)) == 1

    # A refusal is final too: the same nack, even once the condition clears.
    send(executor, resume("p-1", cmd_id="r1"))
    send(executor, hold(cmd_id="h1"))
    send(executor, resume("p-1", cmd_id="r1"))
    assert [m["detail"] for m in replies_for(sent, "r1")] == ["not_holding", "not_holding"]
    assert executor.is_held

    # The same id for another type is a sender bug: refused, not executed.
    send(executor, nudge("left", 50, cmd_id="h1"))
    nack = replies_for(sent, "h1")[-1]
    assert (nack["type"], nack["for"], nack["reason"], nack["detail"]) == ("nack", "nudge", "invalid", "cmd_id_reused")
    assert horizontal_moves(drone)[-1] == ("move", "back", 200)


def test_landing_bypasses_the_cache_every_time(executors):
    executor, drone, sent, _, _ = executors()
    for _ in range(3):
        drone.takeoff()
        send(executor, fp.make_land(cmd_id="same"))
        assert not drone.state.is_flying
    assert drone.calls.count(("land",)) == 3
    assert len([m for m in of_type(sent, "ack", "land") if m["cmd_id"] == "same"]) == 3
    assert "same" not in executor._commands


def test_takeoff_replies_and_is_not_repeated(executors):
    executor, drone, sent, _, _ = executors()
    msg = takeoff(cmd_id="t1")
    send(executor, msg)
    assert executor.wait_idle(WAIT_S)
    send(executor, dict(msg))
    assert [m["type"] for m in replies_for(sent, "t1")] == ["ack", "ack"]
    assert drone.calls.count(("takeoff",)) == 1
    send(executor, takeoff(cmd_id="t2"))
    nack = reply_for(sent, "t2")
    assert (nack["reason"], nack["detail"]) == ("busy", "already_flying")
    executor.land_now("operator")
    drone.state.battery = 20
    send(executor, takeoff(cmd_id="t3"))
    assert wait_for(lambda: reply_for(sent, "t3") is not None)
    assert reply_for(sent, "t3")["reason"] == "battery"


def test_command_cache_lru():
    cache = CommandCache(size=3)
    assert cache.claim("a", "hold") == (NEW, None)
    assert cache.claim("a", "hold") == (PENDING, None)
    assert cache.claim("a", "nudge") == (MISMATCH, None)
    assert cache.finish("a", "hold", {"type": "ack"}) is True
    assert cache.finish("a", "hold", {"type": "nack"}) is False  # the first outcome is final
    assert cache.claim("a", "hold") == (DONE, {"type": "ack"})
    for cmd_id in "bcd":
        cache.claim(cmd_id, "hold")
    # Full: the oldest finished entry ("a") went first; pending ones stay.
    assert len(cache) == 3 and "a" not in cache and all(c in cache for c in "bcd")
    cache.finish("c", "hold", None)
    cache.claim("e", "hold")
    assert "c" not in cache and "b" in cache
    # A reply for an id that was never claimed is recorded as final.
    assert cache.finish("z", "hold", {"type": "ack"}) is True
    assert cache.claim("z", "hold") == (DONE, {"type": "ack"})
    assert len(CommandCache()._entries) == 0 and CommandCache()._size == fp.CMD_ID_CACHE_SIZE == 256
    big = CommandCache()
    for index in range(300):
        big.claim(f"c{index}", "hold")
        big.finish(f"c{index}", "hold", None)
    assert len(big) == 256 and "c299" in big and "c43" not in big and "c44" in big


# ---------------------------------------------------------------------------
# Authority hook
# ---------------------------------------------------------------------------


class FakeAuthority:
    def __init__(self, verdict=None, epoch=7):
        self.verdict = verdict
        self.epoch = epoch
        self.checked = []

    def check(self, msg):
        self.checked.append(msg["type"])
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict

    def epoch_seen(self):
        return self.epoch


def test_authority_refusal_is_a_cached_nack_and_landing_bypasses_it(executors):
    authority = FakeAuthority("stale_epoch")
    executor, drone, sent, _, _ = executors(authority=authority)
    send(executor, takeoff(cmd_id="t1"))
    nack = reply_for(sent, "t1")
    assert (nack["reason"], nack["epoch_seen"]) == ("stale_epoch", 7)
    assert drone.calls == []
    authority.verdict = None
    send(executor, takeoff(cmd_id="t1"))  # duplicate: still the first outcome
    assert [m["type"] for m in replies_for(sent, "t1")] == ["nack", "nack"]
    assert drone.calls == []
    drone.takeoff()
    authority.verdict = "not_authority"
    send(executor, fp.make_land(cmd_id="l1"))
    assert ("land",) in drone.calls and "land" not in authority.checked
    assert executor.telemetry()["epoch"] == 7


def test_authority_refusal_of_assign_lanes_is_answered_and_cached(executors):
    # A stale leader must get its stale_epoch nack (with lanes_total, which
    # every assign_lanes nack carries) so it can step down.
    authority = FakeAuthority("stale_epoch")
    executor, drone, sent, _, _ = executors(authority=authority)
    msg = assign(cmd_id="a1")
    send(executor, msg)
    nack = reply_for(sent, "a1")
    assert nack is not None
    assert (nack["type"], nack["for"], nack["reason"], nack["epoch_seen"], nack["lanes_total"]) == (
        "nack", "assign_lanes", "stale_epoch", 7, 3,
    )
    assert executor.plan_id is None and not executor.plan_valid
    # The retry of the same cmd_id gets the same nack, even once accepted.
    authority.verdict = None
    send(executor, dict(msg))
    assert replies_for(sent, "a1") == [nack, nack]
    assert executor.plan_id is None
    # A lane list that is not a list still gets a valid nack.
    authority.verdict = "not_authority"
    send(executor, dict(assign(cmd_id="a2"), lanes="not lanes"))
    assert (reply_for(sent, "a2")["reason"], reply_for(sent, "a2")["lanes_total"]) == ("not_authority", 0)
    assert drone.calls == []


def test_cmd_id_reused_for_assign_lanes_is_answered(executors):
    executor, _, sent, _, _ = executors()
    send(executor, hold(cmd_id="x1"))  # on the ground: not_flying
    send(executor, assign(cmd_id="x1"))
    nack = replies_for(sent, "x1")[-1]
    assert (nack["for"], nack["reason"], nack["detail"], nack["lanes_total"]) == (
        "assign_lanes", "invalid", "cmd_id_reused", 3,
    )
    assert executor.plan_id is None


@pytest.mark.parametrize("verdict", [RuntimeError("boom"), "nope", 3])
def test_authority_failures_fail_closed(executors, verdict):
    executor, drone, sent, _, _ = executors(authority=FakeAuthority(verdict))
    send(executor, takeoff(cmd_id="t1"))
    assert reply_for(sent, "t1")["reason"] == "not_authority"
    assert drone.calls == []


def test_permissive_authority_reports_the_highest_epoch(executors):
    executor, _, _sent, _, _ = executors()
    assert executor.epoch_seen == 0
    send(executor, assign(plan_id="p-1", plan_seq=1, fence=fp.Fence(5, "mac"), cmd_id="a1"))
    send(executor, hold(cmd_id="h1"))  # epoch 3
    assert executor.epoch_seen == 5 and executor.telemetry()["epoch"] == 5


# ---------------------------------------------------------------------------
# Telemetry and the v1 contract
# ---------------------------------------------------------------------------


def test_v2_telemetry_states_through_a_flight(executors):
    executor, drone, _sent, clock, _ = executors(drone=SteppedTello(name="drone-1", battery=90))
    states = [executor.telemetry()["state"]]
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    states.append(executor.state)
    clock.advance(0.5)
    assert wait_for(lambda: drone.waiting)
    states.append(executor.state)  # the climb belongs to the lanes sequence
    drone.release()
    assert wait_for(lambda: len(drone.move_log) == 2)
    send(executor, hold(cmd_id="h1"))
    states.append(executor.state)
    drone.release()
    assert wait_for(lambda: not executor.is_busy)
    states.append(executor.state)
    send(executor, resume("p-1", cmd_id="r1"))
    drone.auto = True
    drone.release(5)
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    states.append(executor.state)
    send(executor, fp.make_land())
    states.append(executor.state)
    assert states == ["idle", "idle", "flying_lanes", "holding", "holding", "hovering", "idle"]
    telemetry = executor.telemetry()
    assert set(telemetry) == TELEMETRY_V2_KEYS
    assert (telemetry["plan_id"], telemetry["plan_valid"], telemetry["epoch"]) == ("p-1", False, 3)
    fp.validate_edge_to_ground(telemetry, proto=2)


def test_fleet_status_and_is_active_through_a_flight(executors):
    drone = SteppedTello(name="drone-1", battery=90)
    executor, drone, _sent, clock, _ = executors(drone=drone, authority=FakeAuthority(epoch=7))
    assert executor.fleet_status() == {"plan_id": None, "state": "idle", "plan_valid": False, "epoch": 7}
    assert executor.is_active() is False
    send(executor, assign(cmd_id="a1"))
    assert executor.fleet_status() == {"plan_id": "p-1", "state": "idle", "plan_valid": True, "epoch": 7}
    assert executor.is_active() is False
    # A scheduled start counts as active while the drone is still on the ground.
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    assert executor.is_active() is True and not drone.state.is_flying
    clock.advance(0.5)
    assert wait_for(lambda: drone.waiting)
    status = executor.fleet_status()
    assert executor.is_active() is True and status["state"] == "flying_lanes" and status["state"] in fp.EDGE_STATES
    drone.auto = True
    drone.release()
    assert wait_for(lambda: executor.lane_index == 3 and not executor.is_busy)
    assert executor.is_active() is True and executor.fleet_status()["state"] == "hovering"
    send(executor, fp.make_land(cmd_id="l1"))
    assert executor.wait_idle(WAIT_S)
    assert executor.fleet_status() == {"plan_id": "p-1", "state": "idle", "plan_valid": False, "epoch": 7}
    assert executor.is_active() is False


def test_is_active_counts_an_unreadable_flight_state_as_active(executors):
    executor, drone, _, _, _ = executors()

    class Unreadable:
        @property
        def is_flying(self):
            raise RuntimeError("state stream lost")

    state, drone.state = drone.state, Unreadable()
    try:
        assert executor.is_active() is True
    finally:
        drone.state = state


def test_telemetry_switches_to_v2_on_the_first_v2_command(executors):
    executor, _, sent, _, _ = executors(fleet_v2=False)
    executor.tick()
    assert set(of_type(sent, "telemetry")[-1]) == set(fp.TELEMETRY_FIELDS) | {"type", "control_mode"} | CEILING_KEYS
    send(executor, hold(cmd_id="h1"))
    telemetry = executor.telemetry()
    assert set(telemetry) == TELEMETRY_V2_KEYS
    assert (telemetry["state"], telemetry["plan_id"], telemetry["plan_valid"]) == ("idle", None, False)


def test_v1_nack_keeps_v1_reasons(executors):
    executor, _, sent, _, _ = executors(fleet_v2=False)
    v1 = {"type": "assign_lanes", "drone_id": "drone-1", "lanes": LANES, "plan_seq": 2, "plan_id": "p-2"}
    assert executor.handle_assign_lanes(v1)
    older = dict(v1, plan_seq=1, plan_id="p-1")
    assert executor.handle_assign_lanes(older) is False
    nack = of_type(sent, "nack")[-1]
    assert nack == {"type": "nack", "for": "assign_lanes", "drone_id": "drone-1", "reason": "invalid", "lanes_total": 3}
    fp.validate_edge_to_ground(nack, proto=1)


def test_control_type_sets():
    # The v1 set is unchanged (the edge device routes these and the manual
    # commands); the executor also takes the v2 commands through
    # handle_message.
    assert CONTROL_TYPES == fp.GROUND_TO_EDGE_TYPES
    assert V2_CONTROL_TYPES == {"hold", "resume", "nudge"}
    assert ALL_CONTROL_TYPES == CONTROL_TYPES | V2_CONTROL_TYPES | fp.MANUAL_COMMAND_TYPES


def test_hold_without_cmd_id_still_holds(executors):
    executor, drone, sent, _, _ = executors(fleet_v2=False)
    drone.takeoff()
    send(executor, {"type": "hold", "epoch": 3, "leader_id": "mac"})
    assert executor.is_held and of_type(sent, "ack") == []


def test_malformed_messages_are_ignored_without_raising(executors):
    executor, drone, sent, _, _ = executors()
    drone.takeoff()
    for msg in ({"type": ["hold"]}, {"type": None}, {"type": {"a": 1}}, {"type": "handover"},
                {"type": "nudge", "direction": "left", "distance_cm": 50, "cmd_id": "bad id"}):
        executor.handle_message(msg)
    assert drone.calls == [("takeoff",)] and drone.state.is_flying
    assert [m for m in sent if m["type"] in ("ack", "nack")] == []


def test_a_refused_takeoff_after_a_landing_is_not_an_unplanned_landing(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    fly(executor, clock, cmd_id="s1")
    executor.tick()  # the monitor sees the drone flying
    send(executor, fp.make_land(cmd_id="l1"))
    send(executor, assign(plan_id="p-2", plan_seq=2, cmd_id="a2"))
    drone.takeoff = lambda abort=None: False  # the controller refuses (not executed)
    send(executor, start_at(clock() + 0.5, plan_id="p-2", cmd_id="s2"))
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s2") is not None)
    assert reply_for(sent, "s2")["reason"] == "landing"
    executor.tick()
    # Still the new plan, still valid: the drone never left the ground.
    assert executor.plan_valid and executor.plan_id == "p-2"
