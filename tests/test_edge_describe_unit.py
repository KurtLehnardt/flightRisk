"""A single-ground edge answers describe_unit (PROTO-4, plan step S21)."""
from __future__ import annotations

import argparse
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from flightrisk import edge_query
from flightrisk import fleet_protocol as fp
from flightrisk.drone.safety import LinkWatchdog
from flightrisk.edge_device import _control_pump, _handle_control
from flightrisk.edge_query import EdgeQuery, LinkQueries
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.observability import StructuredLogger
from flightrisk.params.report import build_report
from flightrisk.params.resolver import resolve_unit

DRONE = "drone-1"


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def resolution():
    report = build_report(BUILTIN_MODELS["tello"], resolve_unit("tello", DRONE, {}))
    return SimpleNamespace(report=report)


def query(cmd_id="q1", drone_id=DRONE):
    return fp.make_describe_unit(drone_id, cmd_id=cmd_id)


def link(proto=2):
    """A stand-in transport: its current connection and protocol version."""
    return SimpleNamespace(proto=proto, websocket=object())


def queries(resolution, transport=None, clock=None, sent=None):
    sent = [] if sent is None else sent
    return LinkQueries(EdgeQuery(resolution, DRONE), transport or link(), sent.append, clock=clock or Clock())


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_ack_carries_the_resolved_unit_and_its_unit_rev(resolution):
    sent = []
    reply = queries(resolution, sent=sent).handle(query())
    fp.validate_describe_unit_reply(reply, proto=2)
    assert reply["type"] == "ack" and reply["cmd_id"] == "q1" and reply["drone_id"] == DRONE
    wire = resolution.report.wire()
    assert reply["unit_rev"] == resolution.report.unit_rev == wire["unit_rev"]
    assert reply["unit"] == wire["unit"] and reply["unit"]["model"] == "tello"
    assert reply["gates"] == wire["gates"] and reply["values"] == wire["values"]
    assert wait_for(lambda: sent == [reply])


def test_a_second_new_query_inside_2_s_gets_nack_busy(resolution):
    clock = Clock()
    handler = queries(resolution, clock=clock)
    assert handler.handle(query("q1"))["type"] == "ack"
    clock.now += 1.9
    busy = handler.handle(query("q2"))
    assert (busy["type"], busy["reason"], busy["cmd_id"]) == ("nack", "busy", "q2")
    fp.validate_describe_unit_reply(busy, proto=2)
    # A busy nack is not cached and does not restart the interval.
    clock.now += 0.1
    assert handler.handle(query("q2"))["type"] == "ack"


def test_a_repeated_cmd_id_is_answered_from_the_cache_inside_the_interval(resolution):
    clock = Clock()
    handler = queries(resolution, clock=clock)
    first = handler.handle(query("q1"))
    clock.now += 0.5
    assert handler.handle(query("q1")) == first


def test_the_cache_keeps_the_last_8_replies_per_connection(resolution):
    assert edge_query.MAX_CACHED == 8
    clock = Clock()
    handler = queries(resolution, clock=clock)
    for n in range(9):
        assert handler.handle(query(f"q{n}"))["type"] == "ack"
        clock.now += 2.0
    assert list(handler.state.replies) == [f"q{n}" for n in range(1, 9)]
    # q0 fell out: inside the interval it is a new query again, so it is busy.
    clock.now -= 1.0
    assert handler.handle(query("q0"))["reason"] == "busy"
    assert handler.handle(query("q8"))["type"] == "ack"


def test_a_new_connection_gets_its_own_cache_and_interval(resolution):
    transport, clock = link(), Clock()
    handler = queries(resolution, transport=transport, clock=clock)
    assert handler.handle(query("q1"))["type"] == "ack"
    transport.websocket = object()
    assert handler.handle(query("q2"))["type"] == "ack"
    assert list(handler.state.replies) == ["q2"]


@pytest.mark.parametrize("state", ["on_ground", "flying", "landing", "holding"])
def test_the_query_is_answered_in_every_state(resolution, state):
    handler = queries(resolution)
    runner = SimpleNamespace(edge_query=handler)
    flight = MagicMock()
    flight.state = state
    flight.is_active.return_value = state != "on_ground"
    _handle_control(query(f"q-{state}"), runner, StructuredLogger(component="test"), flight=flight)
    assert handler.state.replies[f"q-{state}"]["type"] == "ack"
    # A query is not a command: the executor never sees it.
    flight.handle_message.assert_not_called()
    flight.fail_safe.assert_not_called()
    flight.land_now.assert_not_called()


def test_a_v1_query_is_refused_with_no_reply(resolution):
    sent = []
    handler = queries(resolution, transport=link(proto=1), sent=sent)
    assert handler.handle(query()) is None
    assert handler.state.replies == {}
    time.sleep(0.05)
    assert sent == []


def test_a_malformed_or_foreign_v2_query_gets_nack_invalid(resolution):
    handler = queries(resolution)
    foreign = handler.handle(query("q1", drone_id="drone-9"))
    assert (foreign["type"], foreign["reason"]) == ("nack", "invalid")
    # With no valid cmd_id there is nothing to answer.
    assert handler.handle(dict(query("q2"), cmd_id="bad id")) is None
    assert handler.handle({"type": "describe_unit"}) is None
    # Refusals use no answer slot.
    assert handler.handle(query("q3"))["type"] == "ack"


def test_a_failing_handler_never_lands_the_drone(resolution):
    handler = queries(resolution)
    handler.query = MagicMock()
    handler.query.answer.side_effect = RuntimeError("boom")
    flight = MagicMock()
    _handle_control(query(), SimpleNamespace(edge_query=handler), StructuredLogger(component="test"), flight=flight)
    flight.fail_safe.assert_not_called()
    flight.note_ground_message.assert_not_called()


def test_a_non_string_type_reaches_the_fail_safe_instead_of_killing_the_control_thread():
    # An unhashable type used to raise before the try, which killed the control pump.
    flight = MagicMock()
    _handle_control({"type": ["x"]}, MagicMock(), StructuredLogger(component="test"), flight=flight)
    flight.fail_safe.assert_called_once_with("control_error")


def test_an_edge_without_a_resolution_ignores_the_query():
    assert edge_query.for_link(None, DRONE, link(), print) is None
    flight = MagicMock()
    _handle_control(query(), MagicMock(), StructuredLogger(component="test"), flight=flight)
    flight.note_ground_message.assert_not_called()
    flight.handle_message.assert_not_called()


def test_a_blocked_send_never_delays_the_control_thread(resolution):
    gate = threading.Event()
    handler = LinkQueries(EdgeQuery(resolution, DRONE), link(), lambda reply: gate.wait(5), clock=Clock())
    started = time.monotonic()
    handler.handle(query("q1"))
    handler.handle(query("q2"))
    assert time.monotonic() - started < 0.5
    gate.set()


def test_queries_never_feed_the_watchdog_so_the_link_loss_landing_is_on_time(resolution):
    """Only queries arrive, every 0.2 s: the watchdog expires at its timeout exactly as with silence.

    The lane executor lands when ``envelope.link_lost()`` is True, which is this watchdog expiring.
    """
    clock = Clock(0.0)
    watchdog = LinkWatchdog(1.5, clock=clock)
    flight = MagicMock()
    flight.note_ground_message.side_effect = watchdog.feed
    flight.now.side_effect = clock
    handler = queries(resolution, clock=clock)
    runner = SimpleNamespace(edge_query=handler)
    log = StructuredLogger(component="test")
    inbox = []

    def recv(recv_timeout):
        return inbox.pop(0) if inbox else None

    transport = argparse.Namespace(recv=recv)
    expired_at = None
    for tick in range(1, 11):
        clock.now = round(tick * 0.2, 3)
        inbox.append(query(f"q{tick}"))
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]
        _control_pump(transport, runner, log, flight, stop)
        _handle_control(query(f"h{tick}"), runner, log, flight=flight)
        if expired_at is None and watchdog.expired():
            expired_at = clock.now
    flight.note_ground_message.assert_not_called()
    flight.handle_message.assert_not_called()
    assert expired_at == 1.6  # the first 0.2 s tick at or past the 1.5 s timeout
    # A real ground message still feeds it.
    _handle_control({"type": "heartbeat", "seq": 1}, runner, log, flight=flight)
    assert not watchdog.expired()
