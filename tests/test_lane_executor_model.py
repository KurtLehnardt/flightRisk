"""Model-based tests of the lane executor's protocol v2 state machine.

Seeded random interleavings of ``assign_lanes`` (new, stale, resent and
conflicting plans), ``start_at`` (right and wrong ``plan_id``), ``takeoff``,
``hold``, ``resume`` (right and wrong ``plan_id``), ``nudge``, ground and
emergency landings, link loss, monitor ticks (hover timeouts), duplicated
``cmd_id``s and individual move completions are driven against the real
``LaneExecutor`` on a ``SteppedTello``, whose moves stay "in flight" until the
test releases them. After every step the test waits until the executor is
quiescent (idle, waiting for a scheduled start, or blocked in a move) and
checks these invariants:

* the drone never starts a move while held, except the one already in
  flight when the hold was acknowledged (whose ack says ``finishing_move``)
  and nudges;
* a resume never restarts a plan other than the current valid one, and a
  plan older than the newest accepted is never accepted;
* a duplicated ``cmd_id`` never executes again and is answered with exactly
  its first reply (or nothing while the first copy is still running);
* every landing executes, clears the hold and invalidates the plan, which
  stays invalid until a new plan is accepted; a start or resume of an
  invalid plan is always refused;
* a hold holds: no hover-timeout landing and no move while held;
* the dead-reckoned pose equals the sum of the moves the drone flew and
  stays inside the geofence;
* every message sent is a valid protocol message, and every ``cmd_id`` gets
  at most one distinct reply.

``FLIGHTRISK_EXECUTOR_MODEL_SEEDS`` and ``FLIGHTRISK_EXECUTOR_MODEL_STEPS``
raise the number of runs and steps for a longer soak.
"""
from __future__ import annotations

import os
import random

import pytest

from flightrisk import fleet_protocol as fp
from tests.lane_executor_support import (
    WAIT_S,
    SteppedTello,
    assign,
    dead_reckoned,
    hold,
    make_v2_executor,
    nudge,
    replies_for,
    reply_for,
    resume,
    send,
    start_at,
    takeoff,
    validate_sent,
    wait_for,
)

SEEDS = int(os.environ.get("FLIGHTRISK_EXECUTOR_MODEL_SEEDS", "60"))
STEPS = int(os.environ.get("FLIGHTRISK_EXECUTOR_MODEL_STEPS", "60"))
DIRECTIONS = ("forward", "back", "left", "right")
GEOFENCE_CM = 400


class Run:
    """One seeded run: the executor, the stepped drone and the test's model of them."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        drone = SteppedTello(name="drone-1", battery=90)
        self.ex, self.drone, self.sent, self.clock, self.log = make_v2_executor(drone=drone, geofence_cm=GEOFENCE_CM)
        self.seq = 0
        self.plans: list[dict] = []  # accepted assign_lanes messages, oldest first
        self.current: str | None = None
        self.valid = False
        self.held = False
        self.hold_mark = 0
        self.pending_t: float | None = None
        self.commands: dict[str, dict] = {}
        self.cmd_n = 0
        self.lands = 0
        self.trace: list[str] = []

    # -- plumbing ---------------------------------------------------------

    def cid(self) -> str:
        self.cmd_n += 1
        return f"c{self.cmd_n}"

    def deliver(self, msg: dict) -> dict | None:
        if "cmd_id" in msg and msg["type"] not in fp.FAILSAFE_TYPES:
            self.commands.setdefault(msg["cmd_id"], dict(msg))
        send(self.ex, msg)
        self.settle()
        return reply_for(self.sent, msg["cmd_id"]) if "cmd_id" in msg else None

    def settle(self) -> None:
        ex, drone = self.ex, self.drone

        def quiet() -> bool:
            with ex._lock:
                alive, phase = ex._worker_alive(), ex._phase
            worker = ex._worker
            if drone.waiting:
                return True
            if alive and phase == "pre_takeoff" and self.pending_t is not None and self.clock() < self.pending_t:
                return True
            return not alive and (worker is None or not worker.is_alive())

        assert wait_for(quiet), self.trace[-8:]
        if self.pending_t is not None and ex._phase != "pre_takeoff":
            self.pending_t = None

    def flying(self) -> bool:
        return bool(self.drone.state.is_flying)

    # -- operations ---------------------------------------------------------

    def op_assign_new(self) -> None:
        self.seq += 1
        plan_id = f"p{self.seq}"
        lanes = [
            {"direction": self.rng.choice(DIRECTIONS), "distance_cm": self.rng.randint(20, 150)}
            for _ in range(self.rng.randint(1, 3))
        ]
        kwargs = {}
        on_complete = self.rng.choice([None, None, "hover", "land"])
        if on_complete is not None:
            kwargs["on_complete"] = on_complete
            if on_complete == "hover" and self.rng.random() < 0.5:
                kwargs["hover_timeout_s"] = self.rng.choice([2, 5])
        msg = assign(lanes=lanes, plan_id=plan_id, plan_seq=self.seq, cmd_id=self.cid(), **kwargs)
        reply = self.deliver(msg)
        if reply["type"] == "ack":
            self.plans.append(msg)
            self.current, self.valid = plan_id, True
        else:
            assert reply["reason"] == "geofence", reply

    def op_assign_stale(self) -> None:
        if len(self.plans) < 2:
            return
        msg = dict(self.rng.choice(self.plans[:-1]), cmd_id=self.cid())
        reply = self.deliver(msg)
        assert (reply["type"], reply["reason"]) == ("nack", "stale_plan"), reply

    def op_assign_same(self) -> None:
        if not self.plans:
            return
        reply = self.deliver(dict(self.plans[-1], cmd_id=self.cid()))
        if self.valid:
            assert reply["type"] == "ack", reply
        else:
            assert (reply["type"], reply["reason"]) == ("nack", "stale_plan"), reply

    def op_assign_reuse_id(self) -> None:
        if self.current is None:
            return
        self.seq += 1
        reply = self.deliver(assign(plan_id=self.current, plan_seq=self.seq, cmd_id=self.cid()))
        assert (reply["type"], reply["reason"]) == ("nack", "plan_mismatch"), reply

    def op_start(self) -> None:
        plan_id = self.current if self.current and self.rng.random() < 0.8 else f"p{self.rng.randint(0, self.seq + 1)}"
        t = self.clock() + 0.5
        msg = start_at(t, plan_id=plan_id, cmd_id=self.cid())
        was_pending, old_t = self.ex._phase == "pre_takeoff", self.pending_t
        self.pending_t = old_t if was_pending else t  # lets settle() wait for the right start
        reply = self.deliver(msg)
        if reply is not None:
            self.pending_t = old_t
        if not self.valid:
            assert reply is not None and reply["reason"] == "no_valid_plan", reply
        elif plan_id != self.current:
            assert reply is not None and reply["reason"] == "plan_mismatch", reply
        elif reply is None:
            assert not was_pending

    def op_fire(self) -> None:
        if self.pending_t is not None:
            self.clock.set(self.pending_t)
            self.pending_t = None
            self.settle()

    def op_takeoff(self) -> None:
        flying = self.flying()
        reply = self.deliver(takeoff(cmd_id=self.cid()))
        if flying:
            assert (reply["reason"], reply.get("detail")) == ("busy", "already_flying"), reply

    def op_step(self) -> None:
        if self.drone.waiting:
            self.drone.release()
            self.settle()

    def op_hold(self) -> None:
        flying, inflight = self.flying(), self.drone.waiting
        mark = len(self.drone.move_log)
        reply = self.deliver(hold(cmd_id=self.cid(), reason=self.rng.choice(["alert", "operator"])))
        if reply["type"] == "nack":
            assert reply["reason"] in ("not_flying", "landing") and not (flying and reply["reason"] == "not_flying")
            return
        assert (reply.get("detail") == "finishing_move") == inflight, (reply, inflight)
        if flying:
            if not self.held:
                self.hold_mark = mark
            self.held = True
        else:
            assert reply.get("detail") == "start_cancelled", reply
            self.pending_t = None

    def op_resume(self) -> None:
        right = self.current is not None and self.rng.random() < 0.75
        plan_id = self.current if right else f"p{self.rng.randint(0, self.seq + 1)}"
        flying = self.flying()
        reply = self.deliver(resume(plan_id, cmd_id=self.cid()))
        if reply["type"] == "ack":
            # (The resumed lanes may already have ended in an on_complete landing.)
            assert self.valid and plan_id == self.current and flying, reply
            if reply.get("detail") == "not_holding":
                assert not self.held
            self.held = False
        else:
            if not self.valid:
                assert reply["reason"] in ("no_valid_plan", "landing"), reply
            elif plan_id != self.current:
                assert reply["reason"] == "plan_mismatch", reply

    def op_nudge(self) -> None:
        flying, busy = self.flying(), self.ex.is_busy
        self.drone.expect_nudge()
        msg = nudge(self.rng.choice(DIRECTIONS), self.rng.randint(20, 120), cmd_id=self.cid())
        reply = self.deliver(msg)
        self.drone.expect_nudge(False)
        if not flying:
            assert reply is not None and reply["reason"] in ("not_flying", "landing"), reply
        elif busy:
            assert reply is not None and reply["reason"] == "busy", reply

    def op_land(self) -> None:
        kind = self.rng.choice(["v2", "v1", "emergency"])
        msg = {"v2": fp.make_land(cmd_id=self.cid()), "v1": {"type": "land"}, "emergency": fp.make_emergency_land_all()}[kind]
        flying = self.flying()
        self.deliver(msg)
        assert not self.flying()
        if flying:
            assert self.drone.calls[-1] == ("land",) or ("land",) in self.drone.calls

    def op_link_loss(self) -> None:
        flying = self.flying()
        self.clock.advance(3.5)
        self.ex.tick()
        self.settle()
        if flying:
            assert not self.flying()

    def op_tick(self) -> None:
        lands_before = self.drone.calls.count(("land",))
        moves_before = len(self.drone.move_log)
        held = self.held
        self.clock.advance(self.rng.choice([0.2, 1.0, 3.0]))
        self.ex.note_ground_message()
        self.ex.tick()
        self.settle()
        if held and self.pending_t is None:
            # A hold holds: no landing, no move, however long it lasts.
            assert self.drone.calls.count(("land",)) == lands_before
            assert len(self.drone.move_log) == moves_before

    def op_duplicate(self) -> None:
        if not self.commands:
            return
        cmd_id = self.rng.choice(sorted(self.commands))
        before = list(replies_for(self.sent, cmd_id))
        calls, moves = list(self.drone.calls), len(self.drone.move_log)
        plan = (self.ex.plan_id, self.ex.plan_valid, self.ex.is_held, self.ex._plan_gen)
        send(self.ex, dict(self.commands[cmd_id]))
        self.settle()
        after = replies_for(self.sent, cmd_id)
        if before:
            assert after == before + [before[0]], (before, after)
        assert self.drone.calls == calls and len(self.drone.move_log) == moves
        assert (self.ex.plan_id, self.ex.plan_valid, self.ex.is_held, self.ex._plan_gen) == plan

    OPS = (
        ("op_assign_new", 6), ("op_assign_stale", 2), ("op_assign_same", 2), ("op_assign_reuse_id", 1),
        ("op_start", 5), ("op_fire", 5), ("op_takeoff", 2), ("op_step", 10), ("op_hold", 5), ("op_resume", 5),
        ("op_nudge", 4), ("op_land", 2), ("op_link_loss", 1), ("op_tick", 4), ("op_duplicate", 4),
    )

    # -- invariants ---------------------------------------------------------

    def check(self) -> None:
        ex, drone = self.ex, self.drone
        lands = drone.calls.count(("land",))
        if lands > self.lands:
            self.lands = lands
            self.valid = False
            self.held = False
            self.pending_t = None
        assert ex.plan_valid == self.valid, self.trace[-8:]
        assert ex.is_held == self.held, self.trace[-8:]
        if self.current is not None:
            assert ex.plan_id == self.current
        if self.held:
            sneaked = [m for m in drone.move_log[self.hold_mark:] if not m["nudge"]]
            # One move may already have been in flight when the hold arrived;
            # it is in the log before the mark.
            assert sneaked == [], (sneaked, self.trace[-8:])
        # A move still in flight is in the drone's log but not yet in the pose.
        in_flight = drone.move_log[-1] if drone.waiting else None
        x, y = dead_reckoned(drone)
        if in_flight is not None and in_flight["direction"] in DIRECTIONS:
            dx, dy = {"right": (1, 0), "left": (-1, 0), "forward": (0, 1), "back": (0, -1)}[in_flight["direction"]]
            x, y = x - dx * in_flight["distance_cm"], y - dy * in_flight["distance_cm"]
        pose = ex.envelope.pose_snapshot()
        assert (round(pose.x_cm, 6), round(pose.y_cm, 6)) == (x, y), self.trace[-8:]
        assert ex.envelope.inside_fence(pose.x_cm, pose.y_cm)
        telemetry = ex.telemetry()
        fp.validate_edge_to_ground(telemetry, proto=2)
        if self.held:
            assert telemetry["state"] == "holding"
        if not self.flying():
            assert telemetry["state"] in ("idle", "taking_off")
        for cmd_id in self.commands:
            replies = replies_for(self.sent, cmd_id)
            assert all(r == replies[0] for r in replies), replies

    def step(self) -> None:
        names, weights = zip(*self.OPS)
        name = self.rng.choices(names, weights)[0]
        self.trace.append(f"{name} flying={self.flying()} held={self.held} valid={self.valid} waiting={self.drone.waiting}")
        getattr(self, name)()
        self.check()

    def finish(self) -> None:
        self.drone.auto = True
        self.drone.release(100)
        self.ex.shutdown(timeout=WAIT_S)
        validate_sent(self.sent)
        errors = [e for e in self.log.names() if e in ("edge_executor_error", "edge_monitor_error")]
        assert errors == [], self.trace[-8:]


@pytest.mark.parametrize("seed", range(SEEDS))
def test_executor_state_machine_invariants(seed):
    run = Run(seed)
    try:
        for _ in range(STEPS):
            run.step()
    finally:
        run.finish()


def test_model_covers_every_operation():
    """The weighted mix reaches every operation and the interesting replies."""
    seen_ops, seen_replies = set(), set()
    for seed in range(6):
        run = Run(1000 + seed)
        try:
            for _ in range(STEPS):
                run.step()
                seen_ops.add(run.trace[-1].split()[0])
        finally:
            run.finish()
        for msg in run.sent:
            if msg["type"] == "nack":
                seen_replies.add((msg["for"], msg["reason"]))
            elif msg["type"] == "ack" and msg.get("detail"):
                seen_replies.add((msg["for"], msg["detail"]))
    assert seen_ops == {name for name, _ in Run.OPS}
    for expected in [("assign_lanes", "stale_plan"), ("assign_lanes", "plan_mismatch"), ("start_at", "no_valid_plan"),
                     ("resume", "plan_mismatch"), ("hold", "finishing_move"), ("nudge", "busy")]:
        assert expected in seen_replies, expected
