"""Edge authority (``flightrisk/fleet_authority.py``): rule tests and a model-based random walk.

The rule tests pin each decision of plan section 2.3 and the
"What PR-4/5 consumers must do" list in ``docs/fleet-protocol-v2.md``. The
model-based test drives the authority with seeded random sequences of
hellos, heartbeats, fenced and unfenced commands, landings, epoch changes,
handovers, flight-state changes and connection drops from several grounds,
compares every decision with a small reference model written from the plan,
and checks the safety invariants after every step.
"""
from __future__ import annotations

import random
import threading

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.fleet_authority import (
    ACCEPT,
    CLOSE,
    EPOCH_ADOPT_HEADROOM,
    HANDLED,
    IGNORE,
    LAND,
    REFUSE,
    EdgeAuthority,
)

DRONE = "drone-2"
ZONE = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
LANES = [{"direction": "forward", "distance_cm": 200}]


class FakeHooks:
    def __init__(self):
        self.active = False
        self.supported = set(fp.FENCED_COMMAND_TYPES)
        self.invalidated: list[str] = []
        self.raise_active = False

    def is_active(self):
        if self.raise_active:
            raise RuntimeError("state unreadable")
        return self.active

    def supports(self, mtype):
        return mtype in self.supported

    def snapshot(self):
        return {"is_flying": self.active, "lane_index": 0, "battery": 77}

    def fleet_status(self):
        return None

    def invalidate_plan(self, reason):
        self.invalidated.append(reason)


def make(legacy=False, inflight=False):
    hooks = FakeHooks()
    auth = EdgeAuthority(DRONE, "win", hooks=hooks, legacy=legacy, inflight_handover=inflight)
    return auth, hooks


def hello(node, role="leader", epoch=1, proto=2):
    return fp.make_ground_hello(node, role, epoch, proto=proto)


def fence(epoch, leader):
    return fp.Fence(epoch, leader)


def command(mtype, epoch, leader, *, plan_id="p-1", cmd_id=None):
    f = fence(epoch, leader)
    if mtype == fp.TAKEOFF:
        return fp.make_takeoff(fence=f, cmd_id=cmd_id)
    if mtype == fp.ASSIGN_LANES:
        return fp.make_assign_lanes(DRONE, 100, LANES, ZONE, fence=f, plan_id=plan_id, plan_seq=1, cmd_id=cmd_id)
    if mtype == fp.START_AT:
        return fp.make_start_at(10.0, fence=f, plan_id=plan_id, cmd_id=cmd_id)
    if mtype == fp.HOLD:
        return fp.make_hold(f, cmd_id=cmd_id)
    if mtype == fp.RESUME:
        return fp.make_resume(f, plan_id=plan_id, cmd_id=cmd_id)
    if mtype == fp.NUDGE:
        return fp.make_nudge(f, "left", 50, cmd_id=cmd_id)
    raise AssertionError(mtype)


def connect(auth, key, node, role="leader", epoch=1, proto=2):
    auth.opened(key, f"ws://{key}")
    return auth.on_message(key, hello(node, role, epoch, proto))


def accept_plan(auth, key, epoch, leader, plan_id):
    """Assign lanes from *key* and let the 'executor' ack them, as the pump thread would."""
    msg = command(fp.ASSIGN_LANES, epoch, leader, plan_id=plan_id)
    decision = auth.on_message(key, msg)
    if decision.action != ACCEPT:
        return decision
    assert auth.begin_dispatch(msg, decision.admitted) is msg
    auth.outbound({"type": "ack", "for": "assign_lanes", "drone_id": DRONE, "battery": 80, "lanes_total": 1})
    auth.begin_dispatch(None)
    return decision


# ---------------------------------------------------------------------------
# Landing: always executed, never nacked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mtype", sorted(fp.FAILSAFE_TYPES))
def test_landing_from_a_connection_without_hello_is_executed_silently(mtype):
    auth, _ = make()
    auth.opened("k", "ws://k")
    decision = auth.on_message("k", {"type": mtype})
    assert decision.action == LAND
    assert decision.replies == ()
    assert decision.feed_watchdog is False


@pytest.mark.parametrize("mtype", sorted(fp.FAILSAFE_TYPES))
def test_landing_from_a_stale_ground_is_executed_and_acked_never_nacked(mtype):
    auth, _ = make()
    connect(auth, "a", "mac", epoch=5)
    connect(auth, "b", "old", epoch=2)
    msg = {"type": mtype, "cmd_id": "c-1", "epoch": 1, "leader_id": "old"}
    decision = auth.on_message("b", msg)
    assert decision.action == LAND
    assert decision.feed_watchdog is False
    (ack,) = decision.replies
    assert ack["type"] == "ack" and ack["for"] == mtype and ack["cmd_id"] == "c-1" and ack["drone_id"] == DRONE
    assert fp.validate_message(ack, proto=2) == "ack"


def test_landing_without_cmd_id_is_acked_without_one_and_feeds_only_from_the_authority():
    auth, _ = make()
    connect(auth, "a", "mac")
    decision = auth.on_message("a", {"type": "land"})
    assert decision.action == LAND and decision.feed_watchdog is True
    assert "cmd_id" not in decision.replies[0]


def test_duplicate_landing_cmd_id_is_executed_again():
    auth, _ = make()
    connect(auth, "a", "mac")
    for _ in range(2):
        assert auth.on_message("a", fp.make_land(cmd_id="same")).action == LAND


# ---------------------------------------------------------------------------
# Hello, versions and node ids
# ---------------------------------------------------------------------------


def test_no_hello_means_no_motion_and_strict_mode_closes_a_v1_link():
    auth, _ = make()
    auth.opened("k", "ws://k")
    assert auth.on_message("k", {"type": "ping", "t1": 1.0}).action == IGNORE
    decision = auth.on_message("k", fp.make_heartbeat(0))
    assert decision.action == CLOSE and decision.feed_watchdog is False


def test_legacy_mode_keeps_a_v1_link_as_monitor_only():
    auth, _ = make(legacy=True)
    auth.opened("k", "ws://k")
    takeoff = auth.on_message("k", fp.make_takeoff())
    assert takeoff.action == REFUSE and takeoff.replies == () and not takeoff.feed_watchdog
    assert auth.on_message("k", fp.make_heartbeat(1)).action == IGNORE
    assert "k" in auth.broadcast_keys()
    assert auth.on_message("k", {"type": "land"}).action == LAND
    # A hello after the link was treated as v1 closes it.
    assert auth.on_message("k", hello("mac")).action == CLOSE


def test_hello_announcing_v1_is_refused_without_legacy_and_accepted_with_it():
    strict, _ = make()
    assert connect(strict, "k", "mac", proto=1).action == CLOSE
    legacy, _ = make(legacy=True)
    assert connect(legacy, "k", "mac", proto=1).action == HANDLED
    assert legacy.proto_for("k") == 1


def test_hello_reply_is_a_valid_edge_hello_for_the_agreed_version():
    auth, _ = make()
    decision = connect(auth, "k", "mac", epoch=3)
    assert decision.action == HANDLED
    (reply,) = decision.replies
    parsed = fp.parse_hello(reply)
    assert isinstance(parsed, fp.EdgeHello)
    assert parsed.drone_id == DRONE and parsed.node_id == "win" and parsed.epoch_seen == 3 and parsed.battery == 77


def test_node_id_change_on_a_connection_closes_it():
    auth, _ = make()
    connect(auth, "k", "mac")
    assert auth.on_message("k", hello("evil", epoch=9)).action == CLOSE
    assert auth.epoch_seen == 1


def test_version_change_on_a_live_connection_closes_it():
    auth, _ = make(legacy=True)
    connect(auth, "k", "mac", proto=2)
    assert auth.on_message("k", hello("mac", proto=1)).action == CLOSE


def test_nothing_is_broadcast_to_a_connection_before_its_hello():
    auth, _ = make()
    auth.opened("k", "ws://k")
    assert auth.broadcast_keys() == []
    connect(auth, "j", "mac", role="standby", epoch=0)
    assert auth.broadcast_keys() == ["j"]


# ---------------------------------------------------------------------------
# Epochs, adoption and the authority
# ---------------------------------------------------------------------------


def test_leader_hello_is_adopted_on_the_ground_and_invalidates_the_plan():
    auth, hooks = make()
    connect(auth, "a", "mac", epoch=4)
    assert auth.epoch_seen == 4 and auth.authority_leader == "mac" and auth.authority_key == "a"
    assert hooks.invalidated == ["epoch_4_adopted"]


def test_standby_hello_is_never_adopted():
    auth, _ = make()
    connect(auth, "b", "win", role="standby", epoch=9)
    assert auth.epoch_seen == 0 and auth.authority_key is None


def test_higher_epoch_in_flight_is_refused_and_the_old_authority_keeps_control():
    auth, hooks = make()
    connect(auth, "a", "mac", epoch=1)
    hooks.active = True
    connect(auth, "b", "win", epoch=2)
    assert auth.epoch_seen == 1 and auth.authority_key == "a"
    assert auth.on_message("a", command(fp.HOLD, 1, "mac")).action == ACCEPT
    nack = auth.on_message("b", command(fp.HOLD, 2, "win")).replies[0]
    assert nack["reason"] == "not_authority" and nack["epoch_seen"] == 1


def test_refused_epoch_is_adopted_by_a_heartbeat_once_the_drone_is_down():
    auth, hooks = make()
    connect(auth, "a", "mac", epoch=1)
    hooks.active = True
    connect(auth, "b", "win", epoch=2)
    hooks.active = False
    decision = auth.on_message("b", fp.make_heartbeat(3, fence=fence(2, "win")))
    assert decision.action == ACCEPT and decision.feed_watchdog
    assert auth.epoch_seen == 2 and auth.authority_key == "b"


def test_unreadable_flight_state_counts_as_flying():
    auth, hooks = make()
    hooks.raise_active = True
    connect(auth, "a", "mac", epoch=1)
    assert auth.epoch_seen == 0


def test_in_flight_handover_needs_the_flag_and_a_matching_announcement():
    for inflight, expect in ((False, 1), (True, 2)):
        auth, hooks = make(inflight=inflight)
        connect(auth, "a", "mac", epoch=1)
        ack = auth.on_message("a", fp.make_handover(fence(1, "mac"), "win")).replies[0]
        assert ack["for"] == "handover" and ack["result"] == "ok"
        hooks.active = True
        connect(auth, "b", "win", epoch=2)
        assert auth.epoch_seen == expect


def test_handover_naming_another_node_does_not_let_a_third_ground_in_flight():
    auth, hooks = make(inflight=True)
    connect(auth, "a", "mac", epoch=1)
    auth.on_message("a", fp.make_handover(fence(1, "mac"), "win"))
    hooks.active = True
    connect(auth, "c", "rogue", epoch=2)
    assert auth.epoch_seen == 1


def test_two_leaders_at_the_same_epoch_first_wins():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=3)
    connect(auth, "b", "win", epoch=3)
    assert auth.authority_leader == "mac"
    decision = auth.on_message("b", command(fp.TAKEOFF, 3, "win"))
    assert decision.action == REFUSE and decision.replies[0]["reason"] == "not_authority"
    assert auth.on_message("b", fp.make_heartbeat(1, fence=fence(3, "win"))).feed_watchdog is False


def test_stale_leader_gets_stale_epoch_with_the_epoch_it_lost_to():
    auth, _ = make()
    connect(auth, "a", "old", epoch=1)
    connect(auth, "b", "new", epoch=2)
    msg = command(fp.TAKEOFF, 1, "old", cmd_id="t-1")
    decision = auth.on_message("a", msg)
    assert decision.action == REFUSE and not decision.feed_watchdog
    (nack,) = decision.replies
    assert nack == {
        "type": "nack", "for": "takeoff", "drone_id": DRONE, "cmd_id": "t-1", "reason": "stale_epoch", "epoch_seen": 2,
    }


def test_same_leader_reconnecting_at_the_same_epoch_keeps_authority_in_flight():
    auth, hooks = make()
    connect(auth, "a1", "mac", epoch=2)
    hooks.active = True
    auth.closed("a1")
    assert auth.authority_key is None
    connect(auth, "a2", "mac", epoch=2)
    assert auth.authority_key == "a2"
    assert auth.on_message("a2", fp.make_heartbeat(5, fence=fence(2, "mac"))).feed_watchdog


def test_leader_turning_standby_releases_the_authority_but_not_the_epoch():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=2)
    auth.on_message("a", hello("mac", role="standby", epoch=2))
    assert auth.authority_key is None and auth.epoch_seen == 2
    assert auth.on_message("a", command(fp.TAKEOFF, 2, "mac")).action == REFUSE


def test_epoch_near_max_is_adopted_only_when_announced():
    near = fp.MAX_EPOCH - EPOCH_ADOPT_HEADROOM + 1
    auth, _ = make()
    connect(auth, "a", "mac", epoch=near)
    assert auth.epoch_seen == 0
    connect(auth, "b", "mac2", epoch=near - 1)
    auth.on_message("b", fp.make_handover(fence(near - 1, "mac2"), "mac3"))
    connect(auth, "c", "mac3", epoch=near)
    assert auth.epoch_seen == near


def test_motion_being_dispatched_blocks_adoption():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    msg = command(fp.TAKEOFF, 1, "mac")
    decision = auth.on_message("a", msg)
    assert auth.begin_dispatch(msg, decision.admitted) is msg
    connect(auth, "b", "win", epoch=2)
    assert auth.epoch_seen == 1
    auth.begin_dispatch(None)
    connect(auth, "c", "win2", epoch=2)
    assert auth.epoch_seen == 2


def test_command_accepted_before_an_adoption_is_dropped_at_dispatch_with_a_stale_nack():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    msg = command(fp.NUDGE, 1, "mac", cmd_id="n-1")
    decision = auth.on_message("a", msg)
    connect(auth, "b", "win", epoch=2)
    assert auth.begin_dispatch(msg, decision.admitted) is None
    nack = auth.stale_reply()
    assert nack["reason"] == "stale_epoch" and nack["cmd_id"] == "n-1" and nack["epoch_seen"] == 2


# ---------------------------------------------------------------------------
# Fenced commands, control and unfenced authority-only messages
# ---------------------------------------------------------------------------


def test_leader_id_must_match_the_connections_hello():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    decision = auth.on_message("a", command(fp.TAKEOFF, 1, "other"))
    assert decision.action == REFUSE and decision.replies[0]["reason"] == "not_authority"
    assert auth.on_message("a", fp.make_heartbeat(1, fence=fence(1, "other"))).feed_watchdog is False


def test_unfenced_motion_and_control_are_refused_on_a_v2_link():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    assert auth.on_message("a", fp.make_takeoff()).action == REFUSE
    assert auth.on_message("a", fp.make_heartbeat(1)).feed_watchdog is False
    assert auth.on_message("a", fp.make_clock(0.1)).action == IGNORE


@pytest.mark.parametrize("msg", [
    {"type": "ping", "t1": 1.0},
    {"type": "set_target", "reid_embedding": [0.1]},
    {"type": "stream_video", "enabled": True},
])
def test_ping_target_and_video_only_from_the_authority(msg):
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    connect(auth, "b", "win", role="standby", epoch=0)
    assert auth.on_message("b", msg).action == IGNORE
    accepted = auth.on_message("a", msg)
    assert accepted.action == ACCEPT and accepted.feed_watchdog


def test_unsupported_command_is_refused_with_invalid_not_silently_dropped():
    auth, hooks = make()
    hooks.supported.discard(fp.HOLD)
    connect(auth, "a", "mac", epoch=1)
    decision = auth.on_message("a", command(fp.HOLD, 1, "mac"))
    assert decision.action == REFUSE and decision.replies[0]["reason"] == "invalid"
    assert decision.feed_watchdog


def test_start_at_and_resume_need_the_plan_acked_in_this_epoch():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    assert auth.on_message("a", command(fp.START_AT, 1, "mac")).replies[0]["reason"] == "no_valid_plan"
    accept_plan(auth, "a", 1, "mac", "p-1")
    assert auth.on_message("a", command(fp.START_AT, 1, "mac", plan_id="p-2")).replies[0]["reason"] == "plan_mismatch"
    assert auth.on_message("a", command(fp.RESUME, 1, "mac", plan_id="p-1")).action == ACCEPT
    connect(auth, "b", "win", epoch=2)
    accept = auth.on_message("b", command(fp.START_AT, 2, "win", plan_id="p-1"))
    assert accept.replies[0]["reason"] == "no_valid_plan"


# ---------------------------------------------------------------------------
# Command ids
# ---------------------------------------------------------------------------


def test_duplicate_while_pending_is_ignored_and_afterwards_gets_the_cached_reply():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    msg = command(fp.ASSIGN_LANES, 1, "mac", cmd_id="L-1")
    first = auth.on_message("a", msg)
    assert first.action == ACCEPT
    assert auth.on_message("a", msg).action == IGNORE
    auth.begin_dispatch(msg, first.admitted)
    sent = auth.outbound({"type": "ack", "for": "assign_lanes", "drone_id": DRONE, "battery": 80, "lanes_total": 1})
    assert sent["cmd_id"] == "L-1" and sent["result"] == "ok" and sent["epoch_seen"] == 1
    again = auth.on_message("a", msg)
    assert again.action == REFUSE and again.replies == (sent,)


def test_cmd_id_reused_for_another_type_is_invalid():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    auth.on_message("a", command(fp.TAKEOFF, 1, "mac", cmd_id="x"))
    decision = auth.on_message("a", command(fp.NUDGE, 1, "mac", cmd_id="x"))
    assert decision.replies[0]["reason"] == "invalid"


def test_a_refusal_is_final_for_its_cmd_id():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    connect(auth, "b", "win", epoch=1)
    msg = command(fp.TAKEOFF, 1, "win", cmd_id="t-9")
    first = auth.on_message("b", msg)
    assert auth.on_message("b", msg).replies == first.replies


def test_executor_reply_from_another_thread_is_stamped_with_the_latest_command():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=1)
    accept_plan(auth, "a", 1, "mac", "p-1")
    start = command(fp.START_AT, 1, "mac", cmd_id="s-1")
    auth.on_message("a", start)
    out = {}
    worker = threading.Thread(target=lambda: out.update(auth.outbound(
        {"type": "ack", "for": "start_at", "drone_id": DRONE, "executed_at_ground_s": 1.0})))
    worker.start()
    worker.join()
    assert out["cmd_id"] == "s-1" and fp.validate_message(out, proto=2) == "ack"


def test_telemetry_is_completed_for_v2():
    auth, _ = make()
    connect(auth, "a", "mac", epoch=3)
    accept_plan(auth, "a", 3, "mac", "p-7")
    tel = auth.outbound({
        "type": "telemetry", "drone_id": DRONE, "battery": 70, "height_cm": 100, "is_flying": True,
        "rel_x_cm": 0, "rel_y_cm": 0, "heading_deg": 0, "lane_index": 0, "lanes_total": 1,
        "link_age_s": 0.1, "takeoff_at_local_s": None,
    })
    assert tel["epoch"] == 3 and tel["plan_id"] == "p-7" and tel["state"] == "hovering" and tel["plan_valid"] is False
    assert fp.validate_message(tel, proto=2) == "telemetry"


# ---------------------------------------------------------------------------
# Model-based random walk
# ---------------------------------------------------------------------------

KEYS = ("c0", "c1", "c2", "c3")
NODES = ("mac", "win", "rogue")
MOTION = (fp.TAKEOFF, fp.ASSIGN_LANES, fp.START_AT, fp.HOLD, fp.RESUME, fp.NUDGE)


class Model:
    """The plan's rules, written plainly and independently of the implementation."""

    def __init__(self, legacy, inflight):
        self.legacy, self.inflight = legacy, inflight
        self.seen, self.leader, self.auth = 0, None, None
        self.conns = {}
        self.active = False
        self.dispatching = False
        self.handover = None
        self.plan = None

    def current(self, key, f):
        return f is not None and key == self.auth and f.epoch == self.seen and f.leader_id == self.leader

    def adopt(self, key, epoch, node, via):
        if epoch < self.seen:
            return
        if epoch == self.seen:
            if node == self.leader and self.auth is None:
                self.auth = key
            return
        announced = self.handover == (node, epoch, self.seen)
        if (self.active or self.dispatching) and not (self.inflight and announced):
            return
        self.seen, self.leader, self.auth, self.handover, self.plan = epoch, node, key, None, None

    def expect(self, key, msg):
        c = self.conns[key]
        t = msg["type"]
        if t in fp.FAILSAFE_TYPES:
            self.plan = None  # a landing ends the plan
            return LAND
        if t == fp.HELLO:
            if c["v1"] or (c["node"] not in (None, msg["node_id"])):
                return CLOSE
            if msg["proto"] < 2 and not self.legacy:
                return CLOSE
            agreed = min(2, msg["proto"])
            if c["proto"] is not None and agreed != c["proto"]:
                return CLOSE
            c.update(node=msg["node_id"], role=msg["role"], epoch=msg["epoch"], proto=agreed)
            if msg["role"] == "leader":
                self.adopt(key, msg["epoch"], msg["node_id"], "hello")
            elif key == self.auth:
                self.auth = None
            return HANDLED
        if c["proto"] is None:
            if t in fp.FENCED_COMMAND_TYPES or t in fp.FENCED_CONTROL_TYPES:
                c["v1"] = True
                if not self.legacy:
                    return CLOSE
                return REFUSE if t in fp.FENCED_COMMAND_TYPES else IGNORE
            return IGNORE
        f = fp.fence_of(msg)
        if t == fp.PING:
            return ACCEPT if key == self.auth else IGNORE
        if t == fp.HEARTBEAT:
            if f is None or f.leader_id != c["node"]:
                return IGNORE
            if c["role"] == "leader" and f.epoch == c["epoch"] and (f.epoch > self.seen or self.auth is None):
                self.adopt(key, f.epoch, f.leader_id, "heartbeat")
            return ACCEPT if self.current(key, f) else IGNORE
        if f is None or f.leader_id != c["node"] or not self.current(key, f):
            return REFUSE
        if t == fp.HANDOVER:
            self.handover = (msg["to_node"], msg["epoch_next"], f.epoch)
            return HANDLED
        if t in (fp.START_AT, fp.RESUME) and (self.plan is None or self.plan != (self.seen, msg["plan_id"])):
            return REFUSE
        return ACCEPT


def random_message(rng, model, key):
    kind = rng.random()
    if model.conns[key]["proto"] is None and rng.random() < 0.75:
        kind = 0.0  # a ground speaks first with its hello, most of the time
    epoch = rng.choice([0, 1, 1, 2, 2, 3, 4])
    node = model.conns[key]["node"] or rng.choice(NODES)
    leader = node if rng.random() < 0.8 else rng.choice(NODES)
    if kind < 0.18:
        role = "leader" if rng.random() < 0.7 else "standby"
        return hello(rng.choice(NODES) if rng.random() < 0.15 else node, role,
                     max(epoch, 1) if role == "leader" else epoch, proto=rng.choice([2, 2, 2, 1]))
    if kind < 0.35:
        return fp.make_heartbeat(rng.randrange(100), fence=fence(max(epoch, 1), leader) if rng.random() < 0.9 else None)
    if kind < 0.42:
        return {"type": rng.choice(sorted(fp.FAILSAFE_TYPES)), **({"cmd_id": "land-1"} if rng.random() < 0.5 else {})}
    if kind < 0.47:
        return {"type": "ping", "t1": 1.0}
    if kind < 0.52 and node != "rogue":
        return fp.make_handover(fence(max(epoch, 1), leader), "rogue" if leader != "rogue" else "mac")
    if rng.random() < 0.08:
        return fp.make_takeoff()
    mtype = rng.choice(MOTION)
    plan_id = rng.choice(["p-1", "p-2"])
    return command(mtype, max(epoch, 1), leader, plan_id=plan_id, cmd_id=f"c{rng.randrange(10**9)}")


def check_replies(msg, decision):
    for reply in decision.replies:
        assert reply["drone_id"] == DRONE
        if reply["type"] in ("ack", "nack"):
            fp.validate_message(reply, proto=2)
            assert not (reply["type"] == "nack" and reply["for"] in fp.FAILSAFE_TYPES), "a landing was nacked"
            assert reply.get("cmd_id") == fp.cmd_id_of(msg), "reply does not echo the command's cmd_id"


@pytest.mark.parametrize("legacy,inflight", [(False, False), (True, False), (False, True)])
def test_model_based_random_walk(legacy, inflight):
    totals = {ACCEPT: 0, REFUSE: 0, LAND: 0, CLOSE: 0, HANDLED: 0, IGNORE: 0, "adopted": 0, "dropped": 0}
    for seed in range(250):
        rng = random.Random(seed * 7919 + legacy * 3 + inflight * 5)
        auth, hooks = make(legacy=legacy, inflight=inflight)
        model = Model(legacy, inflight)
        leader_by_epoch: dict[int, str] = {}
        queue: list[tuple[dict, tuple]] = []
        for step in range(70):
            roll = rng.random()
            if roll < 0.06:
                key = rng.choice(KEYS)
                auth.opened(key, key)
                model.conns[key] = {"node": None, "role": None, "epoch": None, "proto": None, "v1": False}
                if model.auth == key:
                    model.auth = None
                continue
            if roll < 0.09 and model.conns:
                key = rng.choice(sorted(model.conns))
                auth.closed(key)
                del model.conns[key]
                if model.auth == key:
                    model.auth = None
                continue
            if roll < 0.13:
                hooks.active = model.active = rng.random() < 0.4
                continue
            if roll < 0.24 and queue:
                msg, admitted = queue.pop(0)
                out = auth.begin_dispatch(msg, admitted)
                current = admitted == (model.seen, model.leader)
                if msg["type"] in MOTION and msg["type"] != fp.HOLD and model.auth is None:
                    current = False
                assert (out is not None) == (current or msg["type"] in fp.FAILSAFE_TYPES)
                model.dispatching = out is not None and msg["type"] in fp.FENCED_COMMAND_TYPES - {fp.HANDOVER}
                totals["dropped"] += out is None
                if out is not None and msg["type"] == fp.ASSIGN_LANES and rng.random() < 0.7:
                    auth.outbound({"type": "ack", "for": "assign_lanes", "drone_id": DRONE,
                                   "battery": 80, "lanes_total": 1})
                    model.plan = (model.seen, msg["plan_id"])
                continue
            if roll < 0.27:
                auth.begin_dispatch(None)
                model.dispatching = False
                continue
            if not model.conns:
                continue
            key = rng.choice(sorted(model.conns))
            msg = random_message(rng, model, key)
            seen_before = auth.epoch_seen
            active_before = model.active or model.dispatching
            expected = model.expect(key, msg)
            decision = auth.on_message(key, msg)
            context = f"seed={seed} step={step} key={key} msg={msg} decision={decision}"
            assert decision.action == expected, context
            totals[decision.action] += 1
            # Invariants, independent of the model's bookkeeping.
            assert auth.epoch_seen == model.seen and auth.authority_leader == model.leader, context
            assert auth.authority_key == model.auth, context
            assert auth.epoch_seen >= seen_before, "epoch_seen decreased"
            if auth.epoch_seen > seen_before:
                totals["adopted"] += 1
                assert not active_before or inflight, "adopted a new epoch in flight without handover"
            if decision.feed_watchdog:
                assert key == auth.authority_key, "a non-authority fed the watchdog"
            if msg["type"] in fp.FAILSAFE_TYPES:
                assert decision.action == LAND
            if decision.action == ACCEPT and msg["type"] in fp.FENCED_COMMAND_TYPES | fp.FENCED_CONTROL_TYPES:
                f = fp.fence_of(msg)
                assert f.epoch == auth.epoch_seen and f.leader_id == auth.authority_leader, "stale epoch obeyed"
                assert leader_by_epoch.setdefault(f.epoch, f.leader_id) == f.leader_id, "two authorities at one epoch"
            if decision.action == CLOSE:
                # The uplink closes the connection; the ground reconnects later.
                auth.closed(key)
                del model.conns[key]
                if model.auth == key:
                    model.auth = None
            if decision.action == ACCEPT and msg["type"] != fp.PING:
                queue.append((msg, decision.admitted))
            if decision.action == LAND:
                queue.insert(0, (msg, decision.admitted))
            check_replies(msg, decision)
    # The generator must exercise every branch, or the walk proves little.
    minimum = {ACCEPT: 50, REFUSE: 50, LAND: 50, HANDLED: 50, IGNORE: 50, CLOSE: 20, "adopted": 50, "dropped": 3}
    for name, least in minimum.items():
        assert totals[name] >= least, f"{name}: {totals}"
