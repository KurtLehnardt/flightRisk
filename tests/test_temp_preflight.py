"""The Tello temperature check before a takeoff (``flightrisk/drone/temp_preflight.py``).

* parsing the ``temp?`` reply (``53~56C``, a single value, malformed text);
* the warn and refusal levels from the environment, and their validation;
* the judgement: ok, warn, refuse, and unknown (no reply, a timeout, a query that raised), which never refuses;
* the lane executor: a hot drone's ``start_at``, ``takeoff`` and ``manual_takeoff`` are refused with an operator
  message and nothing is sent; a warm, unknown or unreadable one takes off with a warning; telemetry carries the
  check as ``temp_preflight``; a drone without ``query_temperature`` is never checked;
* ``TelloController.query_temperature`` on the fake wire: it sends only ``temp?``, skips a stray ``ok``, times out,
  and sends nothing during a land or while the channel stays busy;
* the ground's ``fleet_status`` pass-through, and the edge's start-time validation.

No drone, no network.
"""
from __future__ import annotations

import threading
import time

import pytest

from flightrisk import edge_device
from flightrisk import fleet_protocol as fp
from flightrisk.drone import tello as tello_mod
from flightrisk.drone.temp_preflight import (
    DEFAULT_TEMP_REFUSE_C,
    DEFAULT_TEMP_WARN_C,
    ENV_TELLO_TEMP_REFUSE_C,
    ENV_TELLO_TEMP_WARN_C,
    EVENT_TEMP_PREFLIGHT,
    LEVEL_OK,
    LEVEL_REFUSE,
    LEVEL_UNKNOWN,
    LEVEL_WARN,
    TELEMETRY_TEMP,
    TempLimits,
    TempReading,
    judge_temperature,
    parse_temp_reply,
    temp_status,
)
from tests.lane_executor_support import (
    SteppedTello,
    assign,
    make_v2_executor,
    reply_for,
    send,
    start_at,
    takeoff,
    validate_sent,
    wait_for,
)

# ---------------------------------------------------------------------------
# Parsing the reply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply, low, high", [
    ("53~56C", 53, 56),
    ("91~93C", 91, 93),
    ("56C", 56, 56),
    ("56", 56, 56),
    (" 86~88C\r\n", 86, 88),
    ("53 ~ 56 C", 53, 56),
    ("53~56°C", 53, 56),
    ("93~91C", 91, 93),  # put in order
    (b"60~62C", 60, 62),
    ("74~76c", 74, 76),
])
def test_a_temperature_reply_is_parsed(reply, low, high):
    assert parse_temp_reply(reply) == TempReading(low, high)


@pytest.mark.parametrize("reply", [
    "ok", "error", "unknown command", "", "   ", "abc", "53~", "~56C", "53~56~60C", "5_3C",
    "٥٦C",  # Arabic-Indic digits
    "999C", "53~999C", "56F", "56 C extra", None, 56, 56.0, b"\xff\xfe",
])
def test_anything_else_is_not_a_temperature(reply):
    assert parse_temp_reply(reply) is None


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


def test_the_defaults_are_the_documented_range_top_and_the_lowest_reported_shutdown():
    limits = TempLimits()
    assert (limits.warn_c, limits.refuse_c) == (DEFAULT_TEMP_WARN_C, DEFAULT_TEMP_REFUSE_C) == (90, 95)
    assert TempLimits.from_env({}) == limits
    assert TempLimits.from_env({ENV_TELLO_TEMP_WARN_C: " ", ENV_TELLO_TEMP_REFUSE_C: ""}) == limits


@pytest.mark.parametrize("env, warn, refuse", [
    ({ENV_TELLO_TEMP_WARN_C: "85"}, 85, 95),
    ({ENV_TELLO_TEMP_REFUSE_C: "100"}, 90, 100),
    ({ENV_TELLO_TEMP_REFUSE_C: "off"}, 90, None),
    ({ENV_TELLO_TEMP_REFUSE_C: "OFF"}, 90, None),
    ({ENV_TELLO_TEMP_WARN_C: "60", ENV_TELLO_TEMP_REFUSE_C: "70"}, 60, 70),
    ({ENV_TELLO_TEMP_WARN_C: "95", ENV_TELLO_TEMP_REFUSE_C: "95"}, 95, 95),
])
def test_levels_from_the_environment(env, warn, refuse):
    limits = TempLimits.from_env(env)
    assert (limits.warn_c, limits.refuse_c) == (warn, refuse)


@pytest.mark.parametrize("env, named", [
    ({ENV_TELLO_TEMP_WARN_C: "70.5"}, ENV_TELLO_TEMP_WARN_C),
    ({ENV_TELLO_TEMP_WARN_C: "abc"}, ENV_TELLO_TEMP_WARN_C),
    ({ENV_TELLO_TEMP_WARN_C: "off"}, ENV_TELLO_TEMP_WARN_C),
    ({ENV_TELLO_TEMP_WARN_C: "30"}, ENV_TELLO_TEMP_WARN_C),
    ({ENV_TELLO_TEMP_REFUSE_C: "200"}, ENV_TELLO_TEMP_REFUSE_C),
    ({ENV_TELLO_TEMP_REFUSE_C: "-5"}, ENV_TELLO_TEMP_REFUSE_C),
    ({ENV_TELLO_TEMP_REFUSE_C: "٩٥"}, ENV_TELLO_TEMP_REFUSE_C),
    ({ENV_TELLO_TEMP_REFUSE_C: "70"}, "below the warn level"),
])
def test_bad_levels_are_refused_with_the_variable_named(env, named):
    with pytest.raises(ValueError, match=named):
        TempLimits.from_env(env)


def test_the_edge_refuses_to_start_on_a_bad_level_and_logs_a_non_default_one():
    with pytest.raises(SystemExit, match=ENV_TELLO_TEMP_REFUSE_C):
        edge_device._temp_limits(environ={ENV_TELLO_TEMP_REFUSE_C: "hot"})

    class Log:
        def __init__(self):
            self.events = []

        def warning(self, event, **fields):
            self.events.append((event, fields))

    log = Log()
    limits = edge_device._temp_limits(log, "drone-1", environ={ENV_TELLO_TEMP_REFUSE_C: "off"})
    assert limits.refuse_c is None
    assert [(e, f["variable"], f["value"]) for e, f in log.events] == [
        ("edge_temp_setting_non_default", ENV_TELLO_TEMP_REFUSE_C, "off")]
    log = Log()
    assert edge_device._temp_limits(log, "drone-1", environ={}) == TempLimits()
    assert log.events == []


# ---------------------------------------------------------------------------
# Judgement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply, level", [
    ("53~56C", LEVEL_OK),
    ("74~76C", LEVEL_OK),
    ("86~88C", LEVEL_OK),
    ("88~89C", LEVEL_OK),
    ("89~90C", LEVEL_WARN),   # the highest value is judged, at or above
    ("91~93C", LEVEL_WARN),   # drone-1 at rest: hot, but it flies
    ("93~94C", LEVEL_WARN),
    ("93~95C", LEVEL_REFUSE),
    ("96C", LEVEL_REFUSE),
])
def test_the_highest_value_is_judged_against_the_default_levels(reply, level):
    check = judge_temperature(reply, TempLimits())
    assert check.level == level
    assert check.refused is (level == LEVEL_REFUSE)


def test_with_the_refusal_off_a_very_hot_drone_only_warns():
    check = judge_temperature("100~104C", TempLimits(refuse_c=None))
    assert check.level == LEVEL_WARN and not check.refused
    assert check.to_telemetry()["refuse_c"] is None


@pytest.mark.parametrize("reply, failure, detail", [
    (None, "no reply to temp? within 2.5 s", "no reply to temp? within 2.5 s"),
    (None, "temp? failed: OSError: [Errno 51] Network is unreachable", "temp? failed: OSError"),
    (None, None, "no reply to temp?"),
    ("error", None, "not a temperature: 'error'"),
    ("53~", None, "not a temperature"),
])
def test_an_unreadable_temperature_is_unknown_and_never_refuses(reply, failure, detail):
    check = judge_temperature(reply, TempLimits(warn_c=40, refuse_c=40), failure=failure)
    assert check.level == LEVEL_UNKNOWN and not check.refused
    assert detail in check.detail
    assert check.to_telemetry() == {"level": "unknown", "low_c": None, "high_c": None, "warn_c": 40,
                                    "refuse_c": 40, "detail": check.detail}


def test_operator_text_is_plain_and_fits_a_nack():
    refused = judge_temperature("96~98C", TempLimits())
    text = refused.operator_text("drone-1")
    assert text.startswith("Takeoff refused: drone-1 reports 96 to 98 C, at or above the refusal level of 95 C")
    assert ENV_TELLO_TEMP_REFUSE_C in text and "cool" in text
    assert len(f"drone-1: {text}") <= fp.MAX_TEXT_LEN
    warm = judge_temperature("91~93C", TempLimits()).operator_text("drone-1")
    assert "drone-1 is hot: 91 to 93 C, at or above the warn level of 90 C" in warm
    assert "could not be read" in judge_temperature(None, TempLimits(), failure="timeout").operator_text("drone-1")


def test_the_ground_keeps_only_the_known_keys_of_the_telemetry():
    good = judge_temperature("91~93C", TempLimits()).to_telemetry()
    assert temp_status(good) == good
    assert temp_status({**good, "extra": 1, "detail": "x" * 500})["detail"] == "x" * 120
    assert "extra" not in temp_status({**good, "extra": 1})
    assert temp_status({**good, "high_c": float("nan"), "low_c": True}) == {**good, "high_c": None, "low_c": None}
    for junk in (None, "warn", {"level": "boiling"}, {"high_c": 99}, []):
        assert temp_status(junk) is None


# ---------------------------------------------------------------------------
# The lane executor
# ---------------------------------------------------------------------------


class TempTello(SteppedTello):
    """A fake Tello that answers ``temp?`` with the reply the test sets (or raises it)."""

    def __init__(self, reply="53~56C", **kwargs):
        kwargs.setdefault("auto", True)
        kwargs.setdefault("name", "drone-1")
        kwargs.setdefault("battery", 90)
        super().__init__(**kwargs)
        self.reply = reply
        self.queries: list[float] = []

    def query_temperature(self, timeout_s: float = 2.5):
        self.queries.append(timeout_s)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def executors():
    created = []

    def _make(drone=None, **kwargs):
        made = make_v2_executor(drone=drone if drone is not None else TempTello(), **kwargs)
        created.append(made)
        return made

    yield _make
    for executor, drone, sent, _, _ in created:
        if isinstance(drone, SteppedTello):
            drone.auto = True
            drone.release(50)
        executor.shutdown(timeout=2.0)
        validate_sent(sent)


def _events(log, name):
    return [fields for _, event, fields in log.events if event == name]


def _start(executor, clock, sent, cmd_id="s1"):
    send(executor, assign(cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id=cmd_id))
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, cmd_id) is not None)
    return reply_for(sent, cmd_id)


def test_a_hot_drone_is_refused_with_the_operator_message_and_nothing_is_sent(executors):
    executor, drone, sent, clock, log = executors(TempTello("96~98C"))
    nack = _start(executor, clock, sent)
    assert (nack["type"], nack["reason"]) == ("nack", "drone_unreachable")
    assert nack["detail"].startswith("Takeoff refused: drone-1 reports 96 to 98 C, at or above the refusal level of 95 C")
    fp.validate_edge_to_ground(nack, proto=2)
    assert ("takeoff",) not in drone.calls and drone.queries == [2.5]
    (refused,) = _events(log, "edge_takeoff_refused")
    assert (refused["reason"], refused["high_c"], refused["refuse_c"]) == ("temperature", 98, 95)
    (check,) = _events(log, EVENT_TEMP_PREFLIGHT)
    assert (check["temp_level"], check["low_c"], check["high_c"], check["kind"]) == ("refuse", 96, 98, "start_at")
    telemetry = executor.telemetry()
    assert telemetry[TELEMETRY_TEMP] == {"level": "refuse", "low_c": 96, "high_c": 98, "warn_c": 90,
                                         "refuse_c": 95, "detail": None}
    fp.validate_edge_to_ground(telemetry, proto=2)
    # The plan stays valid: once the drone has cooled, the next start flies.
    drone.reply = "70~72C"
    send(executor, start_at(clock() + 0.5, cmd_id="s2"))
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s2") is not None)
    assert reply_for(sent, "s2")["type"] == "ack"
    assert drone.calls.count(("takeoff",)) == 1
    assert executor.telemetry()[TELEMETRY_TEMP]["level"] == LEVEL_OK


def test_a_standalone_takeoff_is_refused_too(executors):
    executor, drone, sent, _, _ = executors(TempTello("97C"))
    send(executor, takeoff(cmd_id="t1"))
    assert wait_for(lambda: reply_for(sent, "t1") is not None)
    assert reply_for(sent, "t1")["reason"] == "drone_unreachable"
    assert "97 C" in reply_for(sent, "t1")["detail"]
    assert ("takeoff",) not in drone.calls


@pytest.mark.parametrize("reply, level", [
    ("91~93C", LEVEL_WARN),
    (None, LEVEL_UNKNOWN),
    ("error", LEVEL_UNKNOWN),
    (TimeoutError("timed out"), LEVEL_UNKNOWN),
    (OSError(51, "Network is unreachable"), LEVEL_UNKNOWN),
])
def test_a_warm_or_unreadable_drone_takes_off_with_a_warning(executors, reply, level):
    executor, drone, sent, clock, log = executors(TempTello(reply))
    ack = _start(executor, clock, sent)
    assert ack["type"] == "ack"
    assert drone.calls.count(("takeoff",)) == 1
    (check,) = _events(log, EVENT_TEMP_PREFLIGHT)
    assert check["temp_level"] == level and check["operator_text"]
    assert not _events(log, "edge_takeoff_refused")
    assert executor.telemetry()[TELEMETRY_TEMP]["level"] == level


def test_an_ok_reading_is_logged_at_info_and_reported(executors):
    executor, drone, sent, clock, log = executors(TempTello("53~56C"))
    assert _start(executor, clock, sent)["type"] == "ack"
    (check,) = _events(log, EVENT_TEMP_PREFLIGHT)
    assert (check["temp_level"], check["low_c"], check["high_c"]) == ("ok", 53, 56)
    assert "operator_text" not in check
    assert executor.telemetry()[TELEMETRY_TEMP]["high_c"] == 56


def test_the_refusal_level_comes_from_the_executors_limits(executors):
    executor, drone, sent, clock, _ = executors(TempTello("100~104C"), temp_limits=TempLimits(refuse_c=None))
    assert _start(executor, clock, sent)["type"] == "ack"
    assert executor.telemetry()[TELEMETRY_TEMP]["level"] == LEVEL_WARN
    executor2, drone2, sent2, clock2, _ = executors(TempTello("71C"), temp_limits=TempLimits(warn_c=60, refuse_c=70))
    assert _start(executor2, clock2, sent2)["reason"] == "drone_unreachable"
    assert ("takeoff",) not in drone2.calls


def test_a_drone_without_the_query_is_never_checked(executors):
    executor, drone, sent, clock, log = executors(SteppedTello(auto=True, name="drone-1", battery=90))
    assert _start(executor, clock, sent)["type"] == "ack"
    assert not _events(log, EVENT_TEMP_PREFLIGHT)
    assert TELEMETRY_TEMP not in executor.telemetry()


def test_no_temperature_is_reported_before_the_first_takeoff(executors):
    executor, drone, _, _, _ = executors(TempTello("53~56C"))
    assert TELEMETRY_TEMP not in executor.telemetry() and drone.queries == []


def test_a_hot_drone_is_refused_for_the_operators_manual_takeoff(executors):
    fence = fp.Fence(3, "mac")
    executor, drone, sent, _, log = executors(TempTello("99C"))
    send(executor, fp.make_manual_hold(cmd_id="m1", fence=fence))
    send(executor, fp.make_manual_takeoff(cmd_id="t1", fence=fence))
    assert wait_for(lambda: reply_for(sent, "t1") is not None)
    nack = reply_for(sent, "t1")
    assert (nack["type"], nack["reason"]) == ("nack", "drone_unreachable") and "99 C" in nack["detail"]
    assert not any(c[0] == "takeoff" for c in drone.calls)
    assert _events(log, EVENT_TEMP_PREFLIGHT)[0]["kind"] == "manual_takeoff"
    # Cooled, the same manual takeoff flies.
    assert wait_for(lambda: not executor.is_busy)
    drone.reply = "60~62C"
    send(executor, fp.make_manual_takeoff(cmd_id="t2", fence=fence))
    assert wait_for(lambda: reply_for(sent, "t2") is not None)
    assert reply_for(sent, "t2")["type"] == "ack"


# ---------------------------------------------------------------------------
# TelloController.query_temperature on the fake wire
# ---------------------------------------------------------------------------


def _ready(tello_on_wire, **kwargs):
    ctrl, wire = tello_on_wire(**kwargs)
    ctrl.state.is_connected = True
    return ctrl, wire


def test_the_query_sends_only_temp_and_skips_a_stray_ok(tello_on_wire):
    ctrl, wire = _ready(tello_on_wire)
    wire.script = lambda command: [("ok", 0.0), ("53~56C", 0.05)] if command == "temp?" else None
    assert ctrl.query_temperature(timeout_s=2.0) == "53~56C"
    assert wire.sent == ["temp?"]


def test_the_query_returns_a_non_ok_reply_for_the_parser_to_judge(tello_on_wire):
    ctrl, wire = _ready(tello_on_wire)
    wire.script = lambda command: [("unknown command: temp?", 0.0)] if command == "temp?" else None
    assert ctrl.query_temperature(timeout_s=1.0) == "unknown command: temp?"


def test_the_query_times_out_with_none(tello_on_wire):
    ctrl, wire = _ready(tello_on_wire, drop={"temp?"})
    assert ctrl.query_temperature(timeout_s=0.2, late_reply_s=0.1) is None
    assert wire.sent == ["temp?"]
    assert not ctrl._cmd_lock.locked()


def test_a_late_reply_is_dropped_before_the_channel_is_released(tello_on_wire):
    """A temperature that comes after the timeout must not be left for the takeoff to read as its answer."""
    import djitellopy.tello as djitello

    ctrl, wire = _ready(tello_on_wire)
    wire.script = lambda command: [("91~93C", 0.3)] if command == "temp?" else None
    assert ctrl.query_temperature(timeout_s=0.1, late_reply_s=1.0) is None
    assert djitello.drones[wire.host]["responses"] == []
    assert not ctrl._cmd_lock.locked()


def test_the_query_sends_nothing_while_a_land_is_in_progress_or_unconfirmed(tello_on_wire):
    ctrl, wire = _ready(tello_on_wire)
    ctrl._lands_in_progress = 1
    assert ctrl.query_temperature(timeout_s=0.5) is None
    ctrl._lands_in_progress = 0
    ctrl._land_unconfirmed = True
    assert ctrl.query_temperature(timeout_s=0.5) is None
    assert wire.sent == []
    assert not ctrl._cmd_lock.locked()


def test_the_query_gives_up_when_the_channel_stays_busy(tello_on_wire, monkeypatch):
    monkeypatch.setattr(tello_mod, "_KEEPALIVE_WAIT_S", 0.2)
    ctrl, wire = _ready(tello_on_wire)
    taken = threading.Event()
    done = threading.Event()

    def hold():
        with ctrl._cmd_lock:
            taken.set()
            done.wait(5.0)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert taken.wait(2.0)
    try:
        assert ctrl.query_temperature(timeout_s=0.5) is None
        assert wire.sent == []
    finally:
        done.set()
        holder.join(2.0)
    wire.script = lambda command: [("60~62C", 0.0)] if command == "temp?" else None
    assert ctrl.query_temperature(timeout_s=1.0) == "60~62C"
    assert not ctrl._cmd_lock.locked()


# ---------------------------------------------------------------------------
# The ground
# ---------------------------------------------------------------------------


def test_fleet_status_carries_the_check_only_for_a_drone_that_reported_one():
    from tests.test_fleet_dashboard import _started_supervisor
    from tests.test_fleet_dashboard_obstacle import _telemetry

    supervisor, _, _, _ = _started_supervisor()
    check = judge_temperature("91~93C", TempLimits()).to_telemetry()
    supervisor.coordinator.handle_message("drone-1", _telemetry("drone-1", temp_preflight=check))
    supervisor.coordinator.handle_message("drone-2", _telemetry("drone-2", temp_preflight={"level": "boiling"}))
    drones = supervisor.status_payload()["drones"]
    assert drones["drone-1"][TELEMETRY_TEMP] == check
    assert TELEMETRY_TEMP not in drones["drone-2"]


def test_a_land_that_starts_during_the_query_ends_the_wait(tello_on_wire):
    ctrl, wire = _ready(tello_on_wire, drop={"temp?"})
    threading.Timer(0.2, lambda: setattr(ctrl, "_lands_in_progress", 1)).start()
    started = time.monotonic()
    assert ctrl.query_temperature(timeout_s=3.0, late_reply_s=3.0) is None
    assert time.monotonic() - started < 1.5
    assert not ctrl._cmd_lock.locked()
    ctrl._lands_in_progress = 0
