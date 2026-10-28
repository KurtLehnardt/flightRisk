"""The edge refuses to take off when it cannot talk to its Tello, and says so.

Incident (Mac edge-1, 2026-10-28): the Mac was not on the Tello Wi-Fi. The
state stream had stopped, the keepalive and stream recovery failed with
``OSError(51)`` (network unreachable), and the telemetry sent to the ground
still carried the last cached battery, so the ground scheduled a start. The
edge accepted the ``start_at``, sent ``takeoff`` into the dead link (which
raised ``OSError(51)``) and then ran a land retry loop for a drone that had
never left the ground.

These tests pin the fix:

* ``TelloController.link_status()`` reports the drone link as unhealthy when
  no state packet has arrived for ``_STATE_LOST_S``, when no state packet
  ever arrived, when the controller is not connected, and when the last
  command-channel exchange (keepalive, stream command, flight command) raised
  or got no reply, until a later exchange is answered.
* ``LaneExecutor`` refuses ``start_at`` (and a standalone ``takeoff``) with
  ``nack{reason: drone_unreachable, detail: ...}`` while the link is
  unhealthy, re-checks right before the takeoff, and never sends a takeoff
  into a dead link. Landing is never gated.
* Telemetry carries ``drone_link_ok``, ``drone_link_age_s`` and
  ``drone_link_detail`` for a drone that reports its link, and nothing new
  for one that does not (the fake Tello, MAVLink).
* The ground coordinator refuses a start for a drone whose fresh telemetry
  says the drone link is down, and shows a ``start_at`` nack for
  ``drone_unreachable`` as a ``start_refused`` fleet event.
"""
from __future__ import annotations

import threading
import time
import types

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone import tello as tello_mod
from flightrisk.drone.controller import DroneLinkStatus
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.obs_events import SAFETY_EVENTS
from tests.lane_executor_support import (
    FakeClock,
    QuietLogger,
    SteppedTello,
    assign,
    make_v2_executor,
    of_type,
    reply_for,
    send,
    start_at,
    takeoff,
    validate_sent,
    wait_for,
)

STALE_DETAIL = "Tello unreachable (no state for 12 s)"
ERRNO_51 = OSError(51, "Network is unreachable")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class LinkTello(SteppedTello):
    """A fake Tello that reports a drone link the test controls (healthy at first)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("auto", True)
        kwargs.setdefault("name", "drone-1")
        kwargs.setdefault("battery", 90)
        super().__init__(**kwargs)
        self.link = DroneLinkStatus(ok=True, age_s=0.1, reason=None, detail=None)
        self.link_reads = 0

    def link_status(self) -> DroneLinkStatus:
        self.link_reads += 1
        return self.link

    def cut_link(self, reason: str = "state_stale", detail: str = STALE_DETAIL, age_s: float | None = 12.0) -> None:
        self.link = DroneLinkStatus(ok=False, age_s=age_s, reason=reason, detail=detail)

    def restore_link(self) -> None:
        self.link = DroneLinkStatus(ok=True, age_s=0.1, reason=None, detail=None)


@pytest.fixture
def executors():
    created = []

    def _make(drone=None, **kwargs):
        made = make_v2_executor(drone=drone if drone is not None else LinkTello(), **kwargs)
        created.append(made)
        return made

    yield _make
    for executor, drone, sent, _, _ in created:
        if isinstance(drone, SteppedTello):
            drone.auto = True
            drone.release(50)
        executor.shutdown(timeout=2.0)
        validate_sent(sent)


def events_named(log: QuietLogger, name: str) -> list[dict]:
    return [fields for _, event, fields in log.events if event == name]


# ---------------------------------------------------------------------------
# TelloController.link_status() on the wire-level fake
# ---------------------------------------------------------------------------


def _connected(tello_on_wire, **kwargs):
    """A TelloController over the fake wire that is connected and receiving state."""
    ctrl, wire = tello_on_wire(**kwargs)
    ctrl.state.is_connected = True
    wire.start_state(height_cm=0)
    assert wait_for(lambda: ctrl._height_readings())
    return ctrl, wire


def _age_state(ctrl, seconds: float) -> None:
    """Make every recorded state packet ``seconds`` older, as if the stream stopped that long ago."""
    with ctrl._state_lock:
        aged = [r._replace(at=r.at - seconds) for r in ctrl._state_readings]
        ctrl._state_readings.clear()
        ctrl._state_readings.extend(aged)


def test_link_is_healthy_with_fresh_state_and_no_command_failure(tello_on_wire):
    ctrl, _wire = _connected(tello_on_wire)
    status = ctrl.link_status()
    assert status.ok and status.reason is None and status.detail is None
    assert status.age_s is not None and 0 <= status.age_s < 1.0


def test_link_is_unhealthy_once_no_state_arrived_for_the_lost_threshold(tello_on_wire):
    ctrl, wire = _connected(tello_on_wire)
    wire.stop_state()
    # Just inside the threshold the link still counts as up (a Wi-Fi gap).
    _age_state(ctrl, tello_mod._STATE_LOST_S - 0.5)
    assert ctrl.link_status().ok
    _age_state(ctrl, 12.0)
    status = ctrl.link_status()
    assert not status.ok
    assert status.reason == "state_stale"
    assert status.age_s == pytest.approx(12.0 + tello_mod._STATE_LOST_S - 0.5, abs=0.5)
    assert status.detail.startswith("Tello unreachable (no state for ")
    # The state stream coming back is enough: no command is needed.
    wire.start_state(height_cm=0)
    assert wait_for(lambda: ctrl.link_status().ok)


def test_link_is_unhealthy_with_no_state_ever_or_when_not_connected(tello_on_wire):
    ctrl, _wire = tello_on_wire()
    ctrl.state.is_connected = True
    status = ctrl.link_status()
    assert (status.ok, status.reason, status.age_s) == (False, "no_state", None)
    assert status.detail == "Tello unreachable (no state received)"
    ctrl.state.is_connected = False
    assert ctrl.link_status().reason == "not_connected"


def test_keepalive_send_raising_errno_51_marks_the_link_down_until_a_reply(tello_on_wire):
    ctrl, wire = _connected(tello_on_wire)

    def unreachable(command: str) -> None:
        raise ERRNO_51

    wire.on_send = unreachable
    assert ctrl._keepalive_once() is False
    status = ctrl.link_status()
    assert not status.ok and status.reason == "command_failed"
    assert "Network is unreachable" in status.detail and status.detail.startswith("Tello unreachable (")
    # The next keepalive that is answered clears it.
    wire.on_send = None
    assert ctrl._keepalive_once() is True
    assert ctrl.link_status().ok


def test_keepalive_without_reply_marks_the_link_down(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_mod, "_KEEPALIVE_TIMEOUT_S", 0.2)
    ctrl, _wire = _connected(tello_on_wire, drop={"command"})
    assert ctrl._keepalive_once() is False
    status = ctrl.link_status()
    assert not status.ok and status.reason == "command_failed"


def test_takeoff_send_raising_errno_51_marks_the_link_down(tello_on_wire):
    ctrl, wire = _connected(tello_on_wire)

    def unreachable(command: str) -> None:
        if command == "takeoff":
            raise ERRNO_51

    wire.on_send = unreachable
    with pytest.raises(OSError):
        ctrl.takeoff()
    assert ctrl.link_status().reason == "command_failed"
    assert not ctrl.state.is_flying


def test_link_status_does_not_wait_for_a_command_holding_the_channel(tello_on_wire):
    """A takeoff waiting for its reply holds the command lock (the state poller skips meanwhile);
    the link status reads only the state stream and the last outcome, so it answers at once."""
    ctrl, wire = _connected(tello_on_wire, hold={"takeoff"})
    worker = threading.Thread(target=ctrl.takeoff, daemon=True)
    worker.start()
    assert wire.entered.wait(5.0)
    assert ctrl._cmd_lock.locked()
    started = time.monotonic()
    status = ctrl.link_status()
    assert time.monotonic() - started < 0.5
    assert status.ok
    wire.release("takeoff")
    worker.join(5.0)
    assert ctrl.link_status().ok


# ---------------------------------------------------------------------------
# Executor: start_at and takeoff preflight
# ---------------------------------------------------------------------------


def test_start_at_is_refused_when_the_state_is_stale(executors):
    executor, drone, sent, clock, log = executors()
    send(executor, assign(cmd_id="a1"))
    drone.cut_link()
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    nack = reply_for(sent, "s1")
    assert nack is not None, "the refusal must be immediate, not at the scheduled instant"
    assert (nack["type"], nack["reason"], nack["detail"]) == ("nack", "drone_unreachable", STALE_DETAIL)
    fp.validate_edge_to_ground(nack, proto=2)
    assert not executor.is_busy
    clock.advance(1.0)
    executor.tick()
    assert ("takeoff",) not in drone.calls and ("land",) not in drone.calls
    (refused,) = events_named(log, "edge_start_refused")
    assert refused["reason"] == "drone_unreachable" and refused["link_reason"] == "state_stale"
    assert refused["detail"] == STALE_DETAIL


def test_start_at_is_refused_when_the_keepalive_is_failing(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    detail = "Tello unreachable (last command failed: OSError: [Errno 51] Network is unreachable)"
    drone.cut_link(reason="command_failed", detail=detail, age_s=0.2)
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    assert (reply_for(sent, "s1")["reason"], reply_for(sent, "s1")["detail"]) == ("drone_unreachable", detail)
    assert ("takeoff",) not in drone.calls


def test_takeoff_is_never_sent_when_the_link_goes_bad_after_scheduling(executors):
    executor, drone, sent, clock, log = executors()
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    assert reply_for(sent, "s1") is None and executor.is_busy  # scheduled
    drone.cut_link()
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s1") is not None)
    nack = reply_for(sent, "s1")
    assert (nack["reason"], nack["detail"]) == ("drone_unreachable", STALE_DETAIL)
    assert wait_for(lambda: not executor.is_busy)
    # Nothing at all was sent to the drone: no takeoff, and no land loop for
    # a drone that never left the ground.
    assert drone.calls == []
    for _ in range(5):
        clock.advance(executor.land_retry_s)
        executor.tick()
    assert drone.calls == []
    (refused,) = events_named(log, "edge_takeoff_refused")
    assert refused["reason"] == "drone_unreachable" and refused["detail"] == STALE_DETAIL
    assert "edge_executor_error" not in log.names() and "edge_land" not in log.names()
    # The state is clean: the plan is still valid and a start flies once the link is back.
    assert executor.fleet_status()["plan_valid"] is True
    drone.restore_link()
    send(executor, start_at(clock() + 0.5, cmd_id="s2"))
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s2") is not None)
    assert reply_for(sent, "s2")["type"] == "ack"
    assert drone.calls.count(("takeoff",)) == 1


def test_standalone_takeoff_is_refused_when_unreachable(executors):
    executor, drone, sent, _, _ = executors()
    drone.cut_link()
    send(executor, takeoff(cmd_id="t1"))
    assert wait_for(lambda: reply_for(sent, "t1") is not None)
    assert (reply_for(sent, "t1")["reason"], reply_for(sent, "t1")["detail"]) == ("drone_unreachable", STALE_DETAIL)
    assert ("takeoff",) not in drone.calls


def test_v1_start_at_is_refused_without_a_nack(executors):
    """A v1 link has no nack for start_at; the refusal is logged and nothing is sent to the drone."""
    executor, drone, sent, clock, log = executors()
    send(executor, assign(cmd_id="a1"))
    drone.cut_link()
    send(executor, {"type": "start_at", "t_ground_s": clock() + 0.5})
    clock.advance(1.0)
    assert of_type(sent, "nack", "start_at") == []
    assert ("takeoff",) not in drone.calls
    assert events_named(log, "edge_start_refused")[-1]["reason"] == "drone_unreachable"


def test_healthy_tello_is_unaffected(executors):
    executor, drone, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s1") is not None)
    assert reply_for(sent, "s1")["type"] == "ack"
    assert drone.calls.count(("takeoff",)) == 1
    # Checked when the start arrived and again right before the takeoff.
    assert drone.link_reads >= 2


def test_fake_source_without_link_status_is_unaffected(executors):
    executor, drone, sent, clock, _ = executors(drone=SteppedTello(auto=True, name="drone-1", battery=90))
    assert not hasattr(drone, "link_status")
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s1") is not None)
    assert reply_for(sent, "s1")["type"] == "ack"
    telemetry = executor.telemetry()
    assert not set(fp.TELEMETRY_DRONE_LINK_FIELDS) & set(telemetry)


def test_a_link_status_that_raises_refuses_takeoff(executors):
    drone = LinkTello()

    def broken() -> DroneLinkStatus:
        raise RuntimeError("boom")

    drone.link_status = broken
    executor, drone, sent, clock, _ = executors(drone=drone)
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    assert reply_for(sent, "s1")["reason"] == "drone_unreachable"
    assert ("takeoff",) not in drone.calls


@pytest.mark.parametrize("land", ["ground_land", "emergency", "local_key"])
def test_landing_still_works_with_an_unhealthy_link(executors, land):
    executor, drone, sent, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: drone.state.is_flying)
    drone.cut_link()
    if land == "ground_land":
        send(executor, fp.make_land(cmd_id="l1"))
        assert reply_for(sent, "l1")["type"] == "ack"
    elif land == "emergency":
        send(executor, fp.make_emergency_land_all())
    else:
        executor.land_now("local_key")
    assert ("land",) in drone.calls
    assert not drone.state.is_flying


def test_land_retry_runs_with_an_unhealthy_link(executors):
    """A landing that did not take effect is retried by the monitor whatever the link says."""
    executor, drone, _, clock, _ = executors()
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: drone.state.is_flying)
    drone.cut_link()
    drone.land = lambda: drone._record("land")  # a land that does not take effect
    executor.land_now("ground_land")
    clock.advance(executor.land_retry_s)
    executor.tick()
    assert drone.calls.count(("land",)) == 2


# ---------------------------------------------------------------------------
# Telemetry and logging
# ---------------------------------------------------------------------------


def test_telemetry_carries_the_drone_link_flag(executors):
    executor, drone, _, _, _ = executors()
    healthy = executor.telemetry()
    assert (healthy["drone_link_ok"], healthy["drone_link_detail"]) == (True, None)
    assert healthy["drone_link_age_s"] == pytest.approx(0.1)
    fp.validate_edge_to_ground(healthy, proto=2)
    drone.cut_link()
    down = executor.telemetry()
    assert (down["drone_link_ok"], down["drone_link_age_s"], down["drone_link_detail"]) == (False, 12.0, STALE_DETAIL)
    fp.validate_edge_to_ground(down, proto=2)
    fp.validate_edge_to_ground({k: v for k, v in down.items() if k not in fp.V2_FIELDS}, proto=1)
    # The cached values are still sent, next to the flag that says they are stale.
    assert down["battery"] == 90


def test_link_transitions_are_logged_once_each(executors):
    executor, drone, _, clock, log = executors()
    executor.tick()
    drone.cut_link()
    for _ in range(4):
        clock.advance(executor.telemetry_interval_s)
        executor.tick()
    drone.restore_link()
    for _ in range(4):
        clock.advance(executor.telemetry_interval_s)
        executor.tick()
    (down,) = events_named(log, "edge_drone_link_unhealthy")
    assert (down["drone_id"], down["reason"], down["detail"]) == ("drone-1", "state_stale", STALE_DETAIL)
    assert len(events_named(log, "edge_drone_link_restored")) == 1
    assert "edge_drone_link_unhealthy" in SAFETY_EVENTS


def test_unhealthy_link_in_flight_only_logs_and_never_lands(executors):
    executor, drone, _, clock, log = executors()
    send(executor, assign(cmd_id="a1", on_complete="hover", hover_timeout_s=100))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)
    assert wait_for(lambda: drone.state.is_flying and not executor.is_busy)
    drone.cut_link()
    for _ in range(4):
        clock.advance(executor.telemetry_interval_s)
        executor.note_ground_message()
        executor.tick()
    assert ("land",) not in drone.calls
    assert events_named(log, "edge_drone_link_unhealthy")


# ---------------------------------------------------------------------------
# Protocol: nack reason and telemetry fields
# ---------------------------------------------------------------------------


def test_drone_unreachable_nack_validates_and_is_v2_only():
    nack = fp.make_nack("start_at", "drone-1", "drone_unreachable", "c-1", detail=STALE_DETAIL)
    assert fp.validate_edge_to_ground(nack, proto=2) == "nack"
    assert "drone_unreachable" in fp.NACK_REASONS_V2
    # A v1 nack is for assign_lanes with a v1 reason only; the executor never
    # sends this nack to a v1 command (see test_v1_start_at_is_refused_without_a_nack).
    assert "drone_unreachable" not in fp.NACK_REASONS
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(nack, **{"for": "land"}), proto=2)


def test_old_ground_ignores_the_new_reason_without_dropping_the_link():
    """A ground without the new reason logs the nack as malformed and goes on; it never acted on
    a start_at nack anyway. Its coordinator ignores an unknown start_at nack."""
    from tests.test_coordinator import make_fleet

    _, edges, _, events = make_fleet(n=1)
    edges["drone-1"].reply({"type": "nack", "for": "start_at", "drone_id": "drone-1", "reason": "something_new"})
    assert events == []


@pytest.mark.parametrize("bad", [
    {"drone_link_ok": "yes"},
    {"drone_link_ok": None},
    {"drone_link_age_s": "12"},
    {"drone_link_age_s": -1},
    {"drone_link_detail": ""},
    {"drone_link_detail": 5},
])
def test_telemetry_link_fields_are_checked_when_present(bad):
    msg = {"type": "telemetry", "drone_id": "drone-1", "battery": 90, "height_cm": 0, "is_flying": False,
           "rel_x_cm": 0, "rel_y_cm": 0, "heading_deg": 0, "lane_index": 0, "lanes_total": 0,
           "link_age_s": 0.1, "takeoff_at_local_s": None}
    fp.validate_message(msg)  # without the fields: unchanged
    fp.validate_message(dict(msg, drone_link_ok=False, drone_link_age_s=None, drone_link_detail=None))
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(dict(msg, **bad))


# ---------------------------------------------------------------------------
# Ground coordinator
# ---------------------------------------------------------------------------


def test_coordinator_refuses_start_when_fresh_telemetry_says_the_drone_link_is_down():
    from tests.test_coordinator import make_fleet

    coord, edges, _, _ = make_fleet(n=2)
    coord.sync()
    coord.plan()
    edges["drone-1"].telemetry(drone_link_ok=False, drone_link_age_s=12.0, drone_link_detail=STALE_DETAIL)
    edges["drone-2"].telemetry(drone_link_ok=True, drone_link_age_s=0.1, drone_link_detail=None)
    result = coord.start()
    assert not result.ok
    assert result.refusals == {"drone-1": STALE_DETAIL}
    for edge in edges.values():
        assert edge.of_type("start_at") == []
    # Once the link is back the start goes ahead.
    edges["drone-1"].telemetry(drone_link_ok=True, drone_link_age_s=0.1, drone_link_detail=None)
    assert coord.start().ok


def test_coordinator_ignores_the_flag_when_absent_or_stale():
    from tests.test_coordinator import TELEMETRY_STALE_S, make_fleet

    coord, edges, clock, _ = make_fleet(n=1)
    coord.sync()
    coord.plan()
    edges["drone-1"].telemetry(drone_link_ok=False, drone_link_detail=STALE_DETAIL)
    clock.advance(TELEMETRY_STALE_S)
    assert coord.start().ok  # stale telemetry decides nothing, as for the battery


def test_coordinator_without_detail_still_names_the_problem():
    from tests.test_coordinator import make_fleet

    coord, edges, _, _ = make_fleet(n=1)
    coord.sync()
    coord.plan()
    edges["drone-1"].telemetry(drone_link_ok=False)
    assert coord.start().refusals == {"drone-1": "drone unreachable"}


def test_coordinator_shows_a_start_at_nack_for_drone_unreachable():
    from tests.test_coordinator import make_fleet

    coord, edges, _, events = make_fleet(n=2)
    coord.sync()
    coord.plan()
    assert coord.start().ok
    edges["drone-1"].reply(fp.make_nack("start_at", "drone-1", "drone_unreachable", "c-1", detail=STALE_DETAIL))
    assert ("fleet_event", {"event": "start_refused", "detail": f"drone-1: {STALE_DETAIL}"}) in events


def test_dashboard_shows_start_refused_with_the_edge_reason():
    """The existing refusal path: FleetSupervisor.start emits fleet_event start_refused 'drone-1: ...'."""
    from flightrisk.dashboard.app import FleetSupervisor
    from tests.test_coordinator import make_fleet

    class Socket:
        def __init__(self):
            self.emitted = []

        def emit(self, name, payload):
            self.emitted.append((name, payload))

    coord, edges, _, _ = make_fleet(n=1)
    coord.sync()
    coord.plan()
    edges["drone-1"].telemetry(drone_link_ok=False, drone_link_age_s=12.0, drone_link_detail=STALE_DETAIL)
    # Only the parts of the supervisor that start() uses; no transport or browser.
    supervisor = FleetSupervisor.__new__(FleetSupervisor)
    supervisor.coordinator = coord
    supervisor._socketio = socket = Socket()
    supervisor._transport = types.SimpleNamespace(fleet_status=lambda: {"drone-1": {"connected": True}})
    supervisor._busy = threading.Lock()
    supervisor._lock = threading.Lock()
    supervisor.net = "station"
    result = supervisor.start()
    assert not result.ok
    assert ("fleet_event", {"event": "start_refused", "detail": f"drone-1: {STALE_DETAIL}"}) in socket.emitted
    # fleet_status already carries the flag for a later dashboard change.
    (status,) = [payload for name, payload in socket.emitted if name == "fleet_status"]
    assert status["drones"]["drone-1"]["drone_link_ok"] is False


def test_coordinator_status_exposes_the_flag():
    from tests.test_coordinator import make_fleet

    coord, edges, _, _ = make_fleet(n=1)
    edges["drone-1"].telemetry(drone_link_ok=False, drone_link_detail=STALE_DETAIL)
    status = coord.status()["drone-1"]
    assert (status["drone_link_ok"], status["drone_link_detail"]) == (False, STALE_DETAIL)


# ---------------------------------------------------------------------------
# Real Tello driver under a real executor
# ---------------------------------------------------------------------------


def test_real_driver_never_puts_takeoff_on_the_wire_when_unreachable(tello_on_wire):
    """The incident end to end on the wire-level fake: the state stream is gone and the keepalive
    send raises OSError(51). The start is refused and no takeoff (and no land) reaches the socket."""
    ctrl, wire = _connected(tello_on_wire)
    ctrl.read_battery = lambda: 90
    clock = FakeClock()
    sent: list[dict] = []
    log = QuietLogger()
    executor = LaneExecutor(ctrl, SafetyEnvelope(alt_slot_cm=100, clock=clock), "drone-1", send=sent.append,
                            clock=clock, logger=log, fleet_v2=True)
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    try:
        send(executor, assign(lanes=[{"direction": "forward", "distance_cm": 100}], cmd_id="a1"))
        send(executor, start_at(clock() + 0.5, cmd_id="s1"))
        assert reply_for(sent, "s1") is None  # healthy: scheduled
        wire.stop_state()
        _age_state(ctrl, 12.0)

        def unreachable(command: str) -> None:
            raise ERRNO_51

        wire.on_send = unreachable
        ctrl._keepalive_once()
        before = list(wire.sent)
        clock.advance(0.5)
        assert wait_for(lambda: reply_for(sent, "s1") is not None)
        assert reply_for(sent, "s1")["reason"] == "drone_unreachable"
        assert "no state for" in reply_for(sent, "s1")["detail"]
        assert wait_for(lambda: not executor.is_busy)
        executor.tick()
        assert [c for c in wire.sent[len(before):] if c.split()[0] in ("takeoff", "land", "stop")] == []
        assert "takeoff" not in wire.sent
    finally:
        executor.shutdown(timeout=5.0)
        validate_sent(sent)
