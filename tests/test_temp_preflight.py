"""The Tello temperature check before a takeoff (``flightrisk/drone/temp_preflight.py``).

* the reading: ``templ``/``temph`` from a fresh state packet; parsing the launcher probe's ``temp?`` reply;
* the warn and refusal levels from the environment, and their validation;
* the judgement: ok, warn, refuse, and unknown (no fresh state, a failed read), which never refuses;
* the lane executor: a hot drone's ``start_at``, ``takeoff`` and ``manual_takeoff`` are refused with an operator
  message and nothing is sent; a warm or unreadable one takes off with a warning; the check runs first, so the link,
  sensor and battery checks stay last; telemetry carries the check and the live reading as ``temp_preflight``; a drone
  without ``state_temperature`` is never checked;
* ``TelloController.state_temperature`` and the executor on the fake wire: the reading comes from the state packet,
  stale state is unknown, and nothing goes on the command channel between the check and the takeoff;
* the ground's ``fleet_status`` pass-through and its start refusal for a drone that is too hot now, and the edge's
  start-time validation.

No drone, no network.
"""
from __future__ import annotations

import pytest

from flightrisk import edge_device
from flightrisk import fleet_protocol as fp
from flightrisk.drone.controller import DroneLinkStatus, SettledHeight
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
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
    TEMP_KEYS,
    TempLimits,
    TempReading,
    TempSample,
    judge_reading,
    judge_temperature,
    live_refusal,
    parse_temp_reply,
    reading_from,
    temp_status,
    temp_telemetry,
)
from tests.lane_executor_support import (
    WAIT_S,
    FakeClock,
    QuietLogger,
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
# Readings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("low, high, expected", [
    (53, 56, TempReading(53, 56)),
    (93, 91, TempReading(91, 93)),  # put in order
    (40, 40, TempReading(40, 40)),
    (None, 56, None),
    (53, None, None),
    (True, 56, None),
    (53.5, 56, None),
    ("53", 56, None),
    (53, 999, None),
    (-41, 20, None),
])
def test_a_reading_is_two_whole_numbers_in_range(low, high, expected):
    assert reading_from(low, high) == expected


@pytest.mark.parametrize("reply, low, high", [
    ("53~56C", 53, 56),
    ("91~93C", 91, 93),
    ("56C", 56, 56),
    ("56", 56, 56),
    (" 86~88C\r\n", 86, 88),
    ("53 ~ 56 C", 53, 56),
    ("53~56°C", 53, 56),
    ("53~56℃", 53, 56),  # U+2103
    ("56 ℃", 56, 56),
    ("93~91C", 91, 93),  # put in order
    (b"60~62C", 60, 62),
    ("60~62℃".encode("utf-8"), 60, 62),
    ("74~76c", 74, 76),
])
def test_a_probe_reply_is_parsed(reply, low, high):
    assert parse_temp_reply(reply) == TempReading(low, high)


@pytest.mark.parametrize("reply", [
    "ok", "error", "unknown command", "", "   ", "abc", "53~", "~56C", "53~56~60C", "5_3C",
    "٥٦C",  # Arabic-Indic digits
    "999C", "53~999C", "56F", "56 C extra", "56℃℃", None, 56, 56.0, b"\xff\xfe",
])
def test_anything_else_is_not_a_temperature(reply):
    assert parse_temp_reply(reply) is None


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


def test_the_defaults_warn_near_the_top_of_drone_1s_idle_band_and_refuse_at_the_lowest_reported_shutdown():
    limits = TempLimits()
    assert (limits.warn_c, limits.refuse_c) == (DEFAULT_TEMP_WARN_C, DEFAULT_TEMP_REFUSE_C) == (92, 95)
    assert TempLimits.from_env({}) == limits
    assert TempLimits.from_env({ENV_TELLO_TEMP_WARN_C: " ", ENV_TELLO_TEMP_REFUSE_C: ""}) == limits


@pytest.mark.parametrize("env, warn, refuse", [
    ({ENV_TELLO_TEMP_WARN_C: "85"}, 85, 95),
    ({ENV_TELLO_TEMP_REFUSE_C: "100"}, 92, 100),
    ({ENV_TELLO_TEMP_REFUSE_C: "off"}, 92, None),
    ({ENV_TELLO_TEMP_REFUSE_C: "OFF"}, 92, None),
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


@pytest.mark.parametrize("low, high, level", [
    (53, 56, LEVEL_OK),
    (74, 76, LEVEL_OK),
    (86, 88, LEVEL_OK),
    (89, 91, LEVEL_OK),      # below the warn level of 92
    (90, 92, LEVEL_WARN),    # the highest value is judged, at or above
    (91, 93, LEVEL_WARN),    # drone-1 at rest: hot, but it flies
    (93, 94, LEVEL_WARN),
    (93, 95, LEVEL_REFUSE),
    (96, 96, LEVEL_REFUSE),
])
def test_the_highest_value_is_judged_against_the_default_levels(low, high, level):
    check = judge_reading(TempReading(low, high), TempLimits())
    assert check.level == level
    assert check.refused is (level == LEVEL_REFUSE)


def test_with_the_refusal_off_a_very_hot_drone_only_warns():
    check = judge_reading(TempReading(100, 104), TempLimits(refuse_c=None))
    assert check.level == LEVEL_WARN and not check.refused
    assert temp_telemetry(check, check.limits, None)["refuse_c"] is None


@pytest.mark.parametrize("detail", [
    "no fresh state packet (newest 1.2 s old)", "the state packet has no templ/temph", None,
])
def test_no_reading_is_unknown_and_never_refuses(detail):
    check = judge_reading(None, TempLimits(warn_c=40, refuse_c=40), detail=detail)
    assert check.level == LEVEL_UNKNOWN and not check.refused
    assert check.detail == (detail or "no reading")


@pytest.mark.parametrize("reply, failure, level, detail", [
    ("96~98C", None, LEVEL_REFUSE, None),
    ("91~93℃", None, LEVEL_WARN, None),
    (None, "timeout", LEVEL_UNKNOWN, "timeout"),
    (None, None, LEVEL_UNKNOWN, "no reply to temp?"),
    ("error", None, LEVEL_UNKNOWN, "not a temperature: 'error'"),
])
def test_the_launcher_probe_reply_is_judged_the_same_way(reply, failure, level, detail):
    check = judge_temperature(reply, TempLimits(), failure=failure)
    assert check.level == level and check.detail == detail


def test_operator_text_is_plain_does_not_name_the_drone_and_fits_a_nack_with_a_long_id():
    refused = judge_reading(TempReading(96, 98), TempLimits()).operator_text()
    assert refused == ("Takeoff refused: 96 to 98 C is at or above the refusal level of 95 C "
                       "(FLIGHTRISK_TELLO_TEMP_REFUSE_C). Let it cool with the battery out or on a fan, then start again.")
    assert "drone" not in refused.lower()
    # The nack detail is capped at MAX_TEXT_LEN; the ground's start_refused detail is "<drone id>: <detail>", and
    # even with a 30-character id it fits the same cap.
    assert len(f"{'d' * 30}: {refused}") <= fp.MAX_TEXT_LEN
    warm = judge_reading(TempReading(91, 93), TempLimits()).operator_text()
    assert warm.startswith("Hot: 91 to 93 C is at or above the warn level of 92 C.")
    assert "could not be read" in judge_reading(None, TempLimits(), detail="stale").operator_text()


def test_the_telemetry_object_has_every_key_and_the_ground_keeps_only_those():
    check = judge_reading(TempReading(91, 93), TempLimits())
    good = temp_telemetry(check, TempLimits(), TempReading(60, 62))
    assert tuple(good) == TEMP_KEYS
    assert good == {"level": "warn", "low_c": 91, "high_c": 93, "warn_c": 92, "refuse_c": 95, "detail": None,
                    "live_low_c": 60, "live_high_c": 62}
    assert temp_status(good) == good
    assert temp_status({**good, "extra": 1, "detail": "x" * 500})["detail"] == "x" * 120
    assert "extra" not in temp_status({**good, "extra": 1})
    assert temp_status({**good, "high_c": float("nan"), "live_low_c": True}) == {**good, "high_c": None, "live_low_c": None}
    before_the_first_check = temp_telemetry(None, TempLimits(), TempReading(53, 56))
    assert before_the_first_check["level"] is None and before_the_first_check["high_c"] is None
    assert temp_status(before_the_first_check) == before_the_first_check
    for junk in (None, "warn", {"level": "boiling"}, {"high_c": 99}, []):
        assert temp_status(junk) is None


@pytest.mark.parametrize("live, refuse_c, refused", [
    ((96, 98), 95, True),
    ((93, 95), 95, True),
    ((91, 93), 95, False),
    (None, 95, False),         # no live reading: the edge checks again before its takeoff
    ((100, 104), None, False),  # refusal off
])
def test_the_ground_refuses_only_on_a_live_reading_at_or_above_the_refusal_level(live, refuse_c, refused):
    limits = TempLimits(refuse_c=refuse_c)
    status = temp_status(temp_telemetry(None, limits, TempReading(*live) if live else None))
    text = live_refusal(status)
    assert (text is not None) is refused
    if refused:
        assert text.startswith(f"too hot: {live[0]} to {live[1]} C, at or above the refusal level of 95 C")
    assert live_refusal(None) is None


# ---------------------------------------------------------------------------
# The lane executor
# ---------------------------------------------------------------------------


class TempTello(SteppedTello):
    """A fake Tello whose state packet carries the temperature the test sets (or no reading, or an error)."""

    def __init__(self, reading=(53, 56), detail="no fresh state packet (newest 1.2 s old)", **kwargs):
        kwargs.setdefault("auto", True)
        kwargs.setdefault("name", "drone-1")
        kwargs.setdefault("battery", 90)
        super().__init__(**kwargs)
        self.reading = reading
        self.detail = detail
        self.reads = 0
        self.order: list[str] = []

    def state_temperature(self):
        self.reads += 1
        self.order.append("temp")
        if isinstance(self.reading, Exception):
            raise self.reading
        if self.reading is None:
            return TempSample(None, self.detail)
        return TempSample(TempReading(*self.reading))

    def link_status(self):
        self.order.append("link")
        return DroneLinkStatus(ok=True, age_s=0.1)

    def read_battery(self):
        self.order.append("battery")
        return self.state.battery

    def takeoff(self, abort=None):
        self.order.append("takeoff")
        return super().takeoff(abort)


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
    executor, drone, sent, clock, log = executors(TempTello((96, 98)))
    nack = _start(executor, clock, sent)
    assert (nack["type"], nack["reason"]) == ("nack", "drone_unreachable")
    assert nack["detail"].startswith("Takeoff refused: 96 to 98 C is at or above the refusal level of 95 C")
    fp.validate_edge_to_ground(nack, proto=2)
    assert ("takeoff",) not in drone.calls
    (refused,) = _events(log, "edge_takeoff_refused")
    assert (refused["reason"], refused["high_c"], refused["refuse_c"]) == ("temperature", 98, 95)
    (check,) = _events(log, EVENT_TEMP_PREFLIGHT)
    assert (check["temp_level"], check["low_c"], check["high_c"], check["kind"]) == ("refuse", 96, 98, "start_at")
    telemetry = executor.telemetry()
    assert telemetry[TELEMETRY_TEMP] == {"level": "refuse", "low_c": 96, "high_c": 98, "warn_c": 92,
                                         "refuse_c": 95, "detail": None, "live_low_c": 96, "live_high_c": 98}
    fp.validate_edge_to_ground(telemetry, proto=2)
    # The plan stays valid: once the drone has cooled, the next start flies.
    drone.reading = (70, 72)
    send(executor, start_at(clock() + 0.5, cmd_id="s2"))
    clock.advance(0.5)
    assert wait_for(lambda: reply_for(sent, "s2") is not None)
    assert reply_for(sent, "s2")["type"] == "ack"
    assert drone.calls.count(("takeoff",)) == 1
    assert executor.telemetry()[TELEMETRY_TEMP]["level"] == LEVEL_OK


def test_a_standalone_takeoff_is_refused_too(executors):
    executor, drone, sent, _, _ = executors(TempTello((97, 97)))
    send(executor, takeoff(cmd_id="t1"))
    assert wait_for(lambda: reply_for(sent, "t1") is not None)
    assert reply_for(sent, "t1")["reason"] == "drone_unreachable"
    assert "97 C" in reply_for(sent, "t1")["detail"]
    assert ("takeoff",) not in drone.calls


@pytest.mark.parametrize("reading, level", [
    ((91, 93), LEVEL_WARN),
    (None, LEVEL_UNKNOWN),               # stale state
    (RuntimeError("boom"), LEVEL_UNKNOWN),
])
def test_a_warm_or_unreadable_drone_takes_off_with_a_warning(executors, reading, level):
    executor, drone, sent, clock, log = executors(TempTello(reading))
    ack = _start(executor, clock, sent)
    assert ack["type"] == "ack"
    assert drone.calls.count(("takeoff",)) == 1
    (check,) = _events(log, EVENT_TEMP_PREFLIGHT)
    assert check["temp_level"] == level and check["operator_text"]
    assert not _events(log, "edge_takeoff_refused")
    assert executor.telemetry()[TELEMETRY_TEMP]["level"] == level
    if reading is None:
        assert check["detail"] == "no fresh state packet (newest 1.2 s old)"


def test_an_ok_reading_is_logged_at_info_and_reported(executors):
    executor, drone, sent, clock, log = executors(TempTello((53, 56)))
    assert _start(executor, clock, sent)["type"] == "ack"
    (check,) = _events(log, EVENT_TEMP_PREFLIGHT)
    assert (check["temp_level"], check["low_c"], check["high_c"]) == ("ok", 53, 56)
    assert "operator_text" not in check
    assert executor.telemetry()[TELEMETRY_TEMP]["high_c"] == 56


def test_the_refusal_level_comes_from_the_executors_limits(executors):
    executor, drone, sent, clock, _ = executors(TempTello((100, 104)), temp_limits=TempLimits(refuse_c=None))
    assert _start(executor, clock, sent)["type"] == "ack"
    assert executor.telemetry()[TELEMETRY_TEMP]["level"] == LEVEL_WARN
    executor2, drone2, sent2, clock2, _ = executors(TempTello((71, 71)), temp_limits=TempLimits(warn_c=60, refuse_c=70))
    assert _start(executor2, clock2, sent2)["reason"] == "drone_unreachable"
    assert ("takeoff",) not in drone2.calls


def test_a_drone_without_the_reader_is_never_checked(executors):
    executor, drone, sent, clock, log = executors(SteppedTello(auto=True, name="drone-1", battery=90))
    assert _start(executor, clock, sent)["type"] == "ack"
    assert not _events(log, EVENT_TEMP_PREFLIGHT)
    assert TELEMETRY_TEMP not in executor.telemetry()


def test_before_the_first_takeoff_telemetry_carries_only_the_live_reading(executors):
    executor, drone, _, _, log = executors(TempTello((53, 56)))
    temp = executor.telemetry()[TELEMETRY_TEMP]
    assert (temp["level"], temp["high_c"], temp["live_low_c"], temp["live_high_c"]) == (None, None, 53, 56)
    assert not _events(log, EVENT_TEMP_PREFLIGHT)
    drone.reading = None
    assert executor.telemetry()[TELEMETRY_TEMP]["live_high_c"] is None
    fp.validate_edge_to_ground(executor.telemetry(), proto=2)


def test_the_temperature_is_checked_first_so_the_link_and_battery_checks_stay_last(executors):
    executor, drone, sent, clock, _ = executors(TempTello((53, 56)))
    assert _start(executor, clock, sent)["type"] == "ack"
    order = drone.order[:drone.order.index("takeoff") + 1]
    last_temp = len(order) - 1 - order[::-1].index("temp")
    assert order[last_temp:] == ["temp", "link", "battery", "takeoff"]


def test_a_hot_drone_is_refused_for_the_operators_manual_takeoff(executors):
    fence = fp.Fence(3, "mac")
    executor, drone, sent, _, log = executors(TempTello((99, 99)))
    send(executor, fp.make_manual_hold(cmd_id="m1", fence=fence))
    send(executor, fp.make_manual_takeoff(cmd_id="t1", fence=fence))
    assert wait_for(lambda: reply_for(sent, "t1") is not None)
    nack = reply_for(sent, "t1")
    assert (nack["type"], nack["reason"]) == ("nack", "drone_unreachable") and "99 C" in nack["detail"]
    assert not any(c[0] == "takeoff" for c in drone.calls)
    assert _events(log, EVENT_TEMP_PREFLIGHT)[0]["kind"] == "manual_takeoff"
    # Cooled, the same manual takeoff flies.
    assert wait_for(lambda: not executor.is_busy)
    drone.reading = (60, 62)
    send(executor, fp.make_manual_takeoff(cmd_id="t2", fence=fence))
    assert wait_for(lambda: reply_for(sent, "t2") is not None)
    assert reply_for(sent, "t2")["type"] == "ack"


# ---------------------------------------------------------------------------
# TelloController.state_temperature and the executor on the fake wire
# ---------------------------------------------------------------------------


def _age_state(ctrl, seconds: float) -> None:
    """Make every recorded state packet ``seconds`` older, as if the stream stopped that long ago."""
    with ctrl._state_lock:
        aged = [r._replace(at=r.at - seconds) for r in ctrl._state_readings]
        ctrl._state_readings.clear()
        ctrl._state_readings.extend(aged)


def _streaming(tello_on_wire, templ="40", temph="42", **kwargs):
    """A connected TelloController over the fake wire whose state packets carry the given temperature."""
    ctrl, wire = tello_on_wire(**kwargs)
    ctrl.state.is_connected = True
    wire.state_overrides.update(templ=templ, temph=temph)
    wire.start_state(height_cm=0)
    assert wait_for(lambda: ctrl.state_temperature().reading is not None)
    return ctrl, wire


def test_the_reading_comes_from_a_fresh_state_packet_and_sends_nothing(tello_on_wire):
    ctrl, wire = _streaming(tello_on_wire)  # the fake wire's own templ:40;temph:42
    assert ctrl.state_temperature() == TempSample(TempReading(40, 42))
    wire.state_overrides.update(templ="93", temph="91")
    assert wait_for(lambda: ctrl.state_temperature().reading == TempReading(91, 93))
    assert wire.sent == []


def test_stale_state_is_no_reading(tello_on_wire):
    ctrl, wire = _streaming(tello_on_wire)
    wire.stop_state()
    _age_state(ctrl, 1.0)
    sample = ctrl.state_temperature()
    assert sample.reading is None and sample.detail.startswith("no fresh state packet (newest 1.")


@pytest.mark.parametrize("setup, detail", [
    (lambda wire: wire.omit_state_fields.add("temph"), "the state packet has no templ/temph"),
    (lambda wire: wire.state_overrides.update(templ="x"), "the state packet has no templ/temph"),
    (lambda wire: wire.state_overrides.update(temph="999"), "templ/temph out of range (40, 999)"),
])
def test_a_packet_without_a_usable_temperature_is_no_reading(tello_on_wire, setup, detail):
    ctrl, wire = tello_on_wire()
    setup(wire)
    wire.start_state(height_cm=0)
    assert wait_for(lambda: ctrl.state_temperature().detail == detail)
    assert ctrl.state_temperature().reading is None


def test_the_newest_fresh_packet_with_both_fields_is_used(tello_on_wire):
    import time

    from flightrisk.drone.tello import _StateReading

    ctrl, _ = tello_on_wire()
    now = time.monotonic()
    with ctrl._state_lock:
        ctrl._state_readings.extend([
            _StateReading(now - 0.3, 0, 10, 5, 60, 62),
            _StateReading(now - 0.1, 0, 10, 5, None, None),  # the newest packet lacks the fields
        ])
    assert ctrl.state_temperature() == TempSample(TempReading(60, 62))
    with ctrl._state_lock:
        ctrl._state_readings.clear()
        ctrl._state_readings.extend([
            _StateReading(now - 2.0, 0, 10, 5, 60, 62),     # has them, but stale
            _StateReading(now - 0.1, 0, 10, 5, None, None),
        ])
    assert ctrl.state_temperature() == TempSample(None, "the state packet has no templ/temph")


def test_no_state_packet_yet_is_no_reading(tello_on_wire):
    ctrl, _ = tello_on_wire()
    assert ctrl.state_temperature() == TempSample(None, "no state packet yet")
    ctrl._state_arrivals = False
    assert ctrl.state_temperature() == TempSample(None, "state packet arrivals are not recorded")


def _wired_executor(ctrl):
    ctrl.read_battery = lambda: 90
    ctrl.read_height_cm = lambda: 100 if ctrl.state.is_flying else 0
    # The state stream here never shows a climb, so the settled height the
    # climb waits for (flight_guards) is stubbed, as in test_lane_executor_wire.
    ctrl.settled_height = lambda **_: SettledHeight(100, 110, 5, 0.0)
    clock = FakeClock()
    sent: list[dict] = []
    log = QuietLogger()
    executor = LaneExecutor(ctrl, SafetyEnvelope(alt_slot_cm=100, clock=clock), "drone-1", send=sent.append,
                            clock=clock, logger=log, fleet_v2=True)
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    return executor, sent, clock, log


def _wire_start(executor, clock, sent):
    send(executor, assign(lanes=[{"direction": "forward", "distance_cm": 100}], cmd_id="a1"))
    send(executor, start_at(clock() + 0.5, cmd_id="s1"))
    clock.advance(0.5)


def _land_quickly(ctrl, wire, executor):
    """End the flight: with the state stream lost, a land is confirmed by its own reply."""
    wire.stop_state()
    _age_state(ctrl, 12.0)
    executor.shutdown(timeout=WAIT_S)


def test_on_the_wire_a_fresh_hot_reading_refuses_with_nothing_sent(tello_on_wire):
    ctrl, wire = _streaming(tello_on_wire, templ="96", temph="98")
    executor, sent, clock, log = _wired_executor(ctrl)
    try:
        _wire_start(executor, clock, sent)
        assert wait_for(lambda: reply_for(sent, "s1") is not None)
        assert reply_for(sent, "s1")["reason"] == "drone_unreachable"
        assert "96 to 98 C" in reply_for(sent, "s1")["detail"]
        assert wire.sent == []  # no temp?, no takeoff: nothing on the command channel
        assert executor.telemetry()[TELEMETRY_TEMP]["live_high_c"] == 98
    finally:
        _land_quickly(ctrl, wire, executor)


def test_on_the_wire_stale_state_warns_and_the_drone_flies(tello_on_wire):
    ctrl, wire = _streaming(tello_on_wire, templ="96", temph="98", hold={"forward 100"})
    executor, sent, clock, log = _wired_executor(ctrl)
    try:
        wire.stop_state()
        _age_state(ctrl, 1.0)  # stale for the temperature, still a healthy drone link (under 3 s)
        _wire_start(executor, clock, sent)
        assert wire.entered.wait(WAIT_S)
        assert wire.sent == ["takeoff", "forward 100"]
        (check,) = [f for _, e, f in log.events if e == EVENT_TEMP_PREFLIGHT]
        assert check["temp_level"] == LEVEL_UNKNOWN and check["detail"].startswith("no fresh state packet")
    finally:
        wire.release("forward 100")
        _land_quickly(ctrl, wire, executor)


def test_on_the_wire_nothing_goes_on_the_command_channel_between_the_check_and_the_takeoff(tello_on_wire):
    ctrl, wire = _streaming(tello_on_wire, templ="88", temph="90", hold={"forward 100"})
    executor, sent, clock, log = _wired_executor(ctrl)
    try:
        _wire_start(executor, clock, sent)
        assert wire.entered.wait(WAIT_S)
        # The takeoff is the first command the drone receives, and the first reply the controller reads is its own.
        assert wire.sent == ["takeoff", "forward 100"]
        assert reply_for(sent, "s1")["type"] == "ack"
        (check,) = [f for _, e, f in log.events if e == EVENT_TEMP_PREFLIGHT]
        assert (check["temp_level"], check["low_c"], check["high_c"]) == (LEVEL_OK, 88, 90)
        assert not [f for _, e, f in log.events if e == "edge_takeoff_refused"]
    finally:
        wire.release("forward 100")
        _land_quickly(ctrl, wire, executor)


# ---------------------------------------------------------------------------
# The ground
# ---------------------------------------------------------------------------


def test_fleet_status_carries_the_temperature_only_for_a_drone_that_reported_it():
    from tests.test_fleet_dashboard import _started_supervisor
    from tests.test_fleet_dashboard_obstacle import _telemetry

    supervisor, _, _, _ = _started_supervisor()
    temp = temp_telemetry(judge_reading(TempReading(91, 93), TempLimits()), TempLimits(), TempReading(60, 62))
    supervisor.coordinator.handle_message("drone-1", _telemetry("drone-1", temp_preflight=temp))
    supervisor.coordinator.handle_message("drone-2", _telemetry("drone-2", temp_preflight={"level": "boiling"}))
    drones = supervisor.status_payload()["drones"]
    assert drones["drone-1"][TELEMETRY_TEMP] == temp
    assert TELEMETRY_TEMP not in drones["drone-2"]


def test_the_ground_refuses_a_start_for_a_drone_that_is_too_hot_now():
    from tests.test_coordinator import make_fleet

    coord, edges, _, _ = make_fleet(n=2)
    coord.sync()
    coord.plan()
    hot = temp_telemetry(None, TempLimits(), TempReading(96, 98))
    edges["drone-1"].telemetry(temp_preflight=hot)
    edges["drone-2"].telemetry(temp_preflight=temp_telemetry(None, TempLimits(), TempReading(53, 56)))
    result = coord.start()
    assert not result.ok
    assert result.refusals == {"drone-1": "too hot: 96 to 98 C, at or above the refusal level of 95 C "
                               "(FLIGHTRISK_TELLO_TEMP_REFUSE_C); let it cool with the battery out or on a fan, "
                               "then start again"}
    for edge in edges.values():
        assert edge.of_type("start_at") == []
    # An old refused check with a cool live reading, or no live reading, does not refuse: the edge checks again.
    refused_then_cooled = temp_telemetry(judge_reading(TempReading(96, 98), TempLimits()), TempLimits(),
                                         TempReading(70, 72))
    edges["drone-1"].telemetry(temp_preflight=refused_then_cooled)
    assert coord.start().ok


def test_the_ground_ignores_a_missing_live_reading():
    from tests.test_coordinator import make_fleet

    coord, edges, _, _ = make_fleet(n=1)
    coord.sync()
    coord.plan()
    edges["drone-1"].telemetry(temp_preflight=temp_telemetry(None, TempLimits(), None))
    assert coord.start().ok


def test_the_ground_ignores_stale_telemetry():
    from tests.test_coordinator import TELEMETRY_STALE_S, make_fleet

    coord, edges, clock, _ = make_fleet(n=1)
    coord.sync()
    coord.plan()
    edges["drone-1"].telemetry(temp_preflight=temp_telemetry(None, TempLimits(), TempReading(99, 99)))
    clock.advance(TELEMETRY_STALE_S)
    assert coord.start().ok  # stale telemetry decides nothing, as for the battery and the link
