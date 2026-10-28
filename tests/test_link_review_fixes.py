"""Regression tests for the retroactive review findings on the edge-auth rejection work."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import warnings

import pytest
import websockets

from flightrisk import edge_uplink
from flightrisk import link_log_limits as limits
from flightrisk import transport as transport_mod
from flightrisk.async_bridge import AsyncBridge
from flightrisk.edge_uplink import EdgeUplink
from flightrisk.link_log_limits import (
    RateLimitedLog,
    auth_rejected_line,
    closed_line,
    safe_url,
)
from flightrisk.transport import EdgeTransport, EdgeTransportSync, GroundTransport


class _Ws:
    def __init__(self, code=1006, reason=""):
        self.close_code = code
        self.close_reason = reason


async def _until(predicate, timeout_s, interval_s=0.05):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return predicate()


def _edge(clock=None, **kwargs):
    return EdgeTransport(ws_url="ws://localhost:1", **({"clock": clock} if clock else {}), **kwargs)


# --- F1: non-ASCII tokens -------------------------------------------------


@pytest.mark.parametrize(
    ("ground_token", "edge_token", "ok"),
    [
        ("abc123", "abc123’", False),  # non-ASCII on the edge side
        ("abc123’", "abc123", False),  # non-ASCII on the ground side
        ("abc123’", "abc123’", True),  # identical non-ASCII tokens authenticate
    ],
)
def test_non_ascii_tokens_compare_without_crashing(ground_token, edge_token, ok, caplog):
    caplog.set_level(logging.INFO, logger="flightrisk.transport")

    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token=ground_token)
        await gt.start()
        et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token=edge_token)
        try:
            await et.connect()
            if ok:
                return await _until(lambda: len(gt._authenticated_clients) == 1, 3.0), et
            await _until(lambda: et._auth_rejections > 0, 3.0)
            return False, et
        finally:
            await et.disconnect()
            await gt.stop()

    authenticated, et = asyncio.run(scenario())
    assert authenticated is ok
    if not ok:
        assert et._auth_rejections > 0  # the edge saw close code 4001 and backs off
        assert any("edge_auth_rejected" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.exc_info]  # no traceback from the ground handler


def test_token_matches_treats_any_failure_as_a_mismatch():
    gt = GroundTransport(host="localhost", port=0, token="abc’")
    assert gt._token_matches("abc’")
    assert not gt._token_matches("abc")
    assert not gt._token_matches("abc\ud800")  # a lone surrogate cannot be encoded
    assert not gt._token_matches(None)


# --- F2: close log is keyed by close code ---------------------------------


def test_distinct_close_codes_each_log(caplog):
    caplog.set_level(logging.INFO, logger="flightrisk.transport")
    et = _edge()
    et._connected_at_s = et._clock()
    for code in (1006, 4009, 1011, 4009):
        et._log_close_and_back_off(_Ws(code))
    messages = [r.getMessage() for r in caplog.records if "Connection to ground closed" in r.getMessage()]
    assert any("code=1006" in m for m in messages)
    assert any("code=4009" in m for m in messages)
    assert any("code=1011" in m for m in messages)


def test_close_log_state_resets_on_stable_uptime(caplog):
    caplog.set_level(logging.INFO, logger="flightrisk.transport")
    clock = [100.0]
    et = _edge(lambda: clock[0])
    et._connected_at_s = 100.0
    et._log_close_and_back_off(_Ws(1006))
    et._log_close_and_back_off(_Ws(1006))  # suppressed
    clock[0] += limits.STABLE_UPTIME_S
    et._log_close_and_back_off(_Ws(1006))  # a healthy link ended: a new episode logs again
    messages = [r for r in caplog.records if "Connection to ground closed" in r.getMessage()]
    assert len(messages) == 2


def test_zero_connect_time_is_not_treated_as_unset():
    clock = [0.0]
    et = _edge(lambda: clock[0])
    et._connected_at_s = 0.0
    et._auth_rejections = 3
    clock[0] = limits.STABLE_UPTIME_S + 1
    et._log_close_and_back_off(_Ws(1006))
    assert et._auth_rejections == 0


def test_a_logging_failure_cannot_prevent_the_reconnect(monkeypatch):
    async def scenario():
        et = _edge()
        et._should_reconnect = True
        et._connected = True
        scheduled = []

        def boom(_ws):
            raise RuntimeError("log formatting failed")

        class _Stream:
            close_code = 1006

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise websockets.ConnectionClosed(None, None)

        stream = _Stream()
        et._ws = stream
        monkeypatch.setattr(et, "_log_close_and_back_off", boom)
        monkeypatch.setattr(et, "_schedule_reconnect", lambda: scheduled.append(True))
        await et._read_loop(stream)
        return scheduled

    assert asyncio.run(scenario()) == [True]


# --- F3: backoff cap matches the uplink's refusal cap ----------------------


def test_auth_backoff_cap_matches_the_uplink_refusal_cap():
    assert limits.AUTH_BACKOFF_MAX_S == edge_uplink.REFUSED_RECONNECT_MAX_S == 10.0
    assert limits.auth_backoff_s(40) == 10.0


def test_edge_connect_log_reports_the_auth_backoff_cap():
    from flightrisk import edge_device

    assert edge_device.AUTH_BACKOFF_MAX_S == limits.AUTH_BACKOFF_MAX_S
    assert edge_device._FLEET_RECONNECT_MAX_DELAY_S == 2.0


# --- F4: credentials and forged reasons stay out of the log ----------------


def test_error_line_hides_url_credentials_and_escapes_the_reason():
    line = auth_rejected_line("ws://opuser:oppass@host:9000", 4001, "x\nFORGED edge_auth_rejected", 1)
    assert "opuser" not in line and "oppass" not in line
    assert "ground=ws://host:9000" in line
    assert "\n" not in line
    assert "\\n" in line


def test_closed_line_hides_credentials_and_truncates_the_reason():
    line = closed_line("ws://opuser:oppass@host:9000", 1011, "a\nb" + "z" * 500, 1)
    assert "oppass" not in line and "\n" not in line
    assert len(line) < 300


def test_safe_url_is_shared_by_the_uplink():
    assert edge_uplink.safe_url is safe_url
    assert safe_url("ws://u:p@h:1/x") == "ws://h:1/x"


# --- F5: is_running reads the loop once; run() closes the coroutine --------


def test_is_running_survives_a_flip_between_reads():
    class _Flip:
        def __init__(self, bridge):
            self.bridge = bridge

        def is_running(self):
            return True

    bridge = AsyncBridge()
    reads = [0]

    class _Bridge(AsyncBridge):
        @property
        def loop(self):
            reads[0] += 1
            return _Flip(self) if reads[0] == 1 else None

        @loop.setter
        def loop(self, value):
            pass

    assert _Bridge().is_running is True  # a second read would return None and raise AttributeError
    assert reads[0] == 1
    assert bridge.is_running is False


@pytest.mark.filterwarnings("error::RuntimeWarning")
def test_run_closes_the_coroutine_when_scheduling_fails():
    async def work():
        return 1

    class _Loop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, *a, **k):
            raise RuntimeError("Event loop is closed")

    bridge = AsyncBridge()
    bridge.loop = _Loop()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError):
            bridge.run(work())
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)]


def test_sync_send_survives_stop_loop_race():
    sync = EdgeTransportSync(ws_url="ws://localhost:1")
    sync._bridge.start_loop()
    sync._bridge.stop_loop()
    assert sync.send({"type": "telemetry"}) is False


# --- F6: ground limiter reset on success; missing first message ------------


def test_successful_auth_resets_the_ground_rejection_limiter(caplog):
    caplog.set_level(logging.INFO, logger="flightrisk.transport")

    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token="right")
        await gt.start()
        try:
            bad = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token="wrong")
            await bad.connect()
            await _until(lambda: gt._reject_log._state, 3.0)
            await bad.disconnect()
            assert gt._reject_log._state
            good = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token="right")
            await good.connect()
            await _until(lambda: len(gt._authenticated_clients) == 1, 3.0)
            state = dict(gt._reject_log._state)
            await good.disconnect()
            return state
        finally:
            await gt.stop()

    assert asyncio.run(scenario()) == {}


def test_client_that_never_sends_an_auth_message_is_closed(monkeypatch):
    monkeypatch.setattr(transport_mod, "_AUTH_RECV_TIMEOUT_S", 0.3)

    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token="right")
        await gt.start()
        try:
            async with websockets.connect(f"ws://localhost:{gt.port}") as ws:
                try:
                    await asyncio.wait_for(ws.recv(), 3.0)
                except websockets.ConnectionClosed:
                    pass
                return ws.close_code, ws.close_reason
        finally:
            await gt.stop()

    code, reason = asyncio.run(scenario())
    assert code == limits.CLOSE_UNAUTHORIZED
    assert reason == "unauthorized: no auth message"


# --- test gaps --------------------------------------------------------------


def test_auth_error_line_repeats_every_twentieth_rejection(caplog):
    caplog.set_level(logging.INFO, logger="flightrisk.transport")
    et = _edge()
    et._connected_at_s = et._clock()
    for _ in range(41):
        et._log_close_and_back_off(_Ws(limits.CLOSE_UNAUTHORIZED, "unauthorized: edge token mismatch"))
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "edge_auth_rejected" in r.getMessage()]
    assert len(errors) == 3  # rejections 1, 21 and 41


def test_auth_rejection_after_a_stable_connection_starts_a_new_episode(caplog):
    caplog.set_level(logging.INFO, logger="flightrisk.transport")
    clock = [10.0]
    et = _edge(lambda: clock[0])
    et._connected_at_s = 10.0
    et._auth_rejections = 7
    clock[0] += limits.STABLE_UPTIME_S + 1
    et._log_close_and_back_off(_Ws(limits.CLOSE_UNAUTHORIZED, "r"))
    assert et._auth_rejections == 1
    assert any("attempts=1 " in r.getMessage() for r in caplog.records)


def test_rate_limiter_evicts_the_oldest_key_at_capacity():
    rl = RateLimitedLog(every=10, min_interval_s=60.0, clock=lambda: 0.0, max_keys=256)
    for i in range(256):
        assert rl.should_log(i) == 1
    assert rl.should_log("new") == 1
    assert len(rl._state) == 256 and 0 not in rl._state
    assert rl.should_log(0) == 1  # an evicted key is treated as new


def test_uplink_logs_4001_and_backs_off_on_the_multi_ground_path(caplog):
    caplog.set_level(logging.INFO, logger="flightrisk.transport")

    async def scenario():
        gt = GroundTransport(host="127.0.0.1", port=0, token="right")
        await gt.start()
        uplink = EdgeUplink([f"ws://127.0.0.1:{gt.port}"], drone_id="d1", node_id="n1", token="wrong")
        try:
            await asyncio.to_thread(uplink.connect, 5.0)
            transport = uplink._transports[0]
            await _until(lambda: transport._auth_rejections > 0, 5.0)
            return transport._auth_rejections
        finally:
            await asyncio.to_thread(uplink.disconnect)
            await gt.stop()

    rejections = asyncio.run(scenario())
    assert rejections >= 1
    assert any("edge_auth_rejected" in r.getMessage() for r in caplog.records)
    assert threading.active_count() >= 1
