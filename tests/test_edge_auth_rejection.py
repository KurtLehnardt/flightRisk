"""An edge the ground rejects (wrong token) backs off and says why, once."""

from __future__ import annotations

import asyncio
import gc
import logging
import warnings

import pytest
import websockets

from flightrisk import link_log_limits as limits
from flightrisk.async_bridge import AsyncBridge
from flightrisk.link_log_limits import RateLimitedLog, auth_backoff_s
from flightrisk.transport import EdgeTransport, EdgeTransportSync, GroundTransport

SECRET = "right-token-value"
WRONG = "wrong-token-value"


async def _until(predicate, timeout_s: float, interval_s: float = 0.05) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return predicate()


def _count_handshakes(gt: GroundTransport) -> list[int]:
    attempts = [0]
    original = gt._authenticate

    async def counting(websocket):
        attempts[0] += 1
        return await original(websocket)

    gt._authenticate = counting
    return attempts


def test_rejected_edge_backs_off_and_logs_one_clear_error(caplog):
    caplog.set_level(logging.INFO, logger="flightrisk.transport")

    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token=SECRET)
        await gt.start()
        attempts = _count_handshakes(gt)
        et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token=WRONG)
        try:
            await et.connect()
            await asyncio.sleep(6.0)
            return attempts[0]
        finally:
            await et.disconnect()
            await gt.stop()

    attempts = asyncio.run(scenario())
    # Before the fix the edge retried every second (7 attempts in 6 s).
    assert 2 <= attempts <= 4

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    line = errors[0].getMessage()
    assert line.startswith("edge_auth_rejected ground=ws://localhost:")
    assert "code=4001" in line and "FLIGHTRISK_EDGE_TOKEN" in line and "unauthorized" in line
    assert WRONG not in caplog.text and SECRET not in caplog.text
    # The generic closed warning is replaced by the rejection line.
    assert not [r for r in caplog.records if "Connection to ground closed" in r.getMessage()]
    # The ground logs the rejection once per remote address, with a count later.
    rejections = [r for r in caplog.records if "Rejecting client" in r.getMessage()]
    assert len(rejections) == 1 < attempts


def test_rejected_edge_recovers_once_ground_accepts_the_token(monkeypatch):
    monkeypatch.setattr(limits, "AUTH_BACKOFF_BASE_S", 0.2)

    async def scenario():
        gt = GroundTransport(host="localhost", port=0, token=SECRET)
        await gt.start()
        et = EdgeTransport(ws_url=f"ws://localhost:{gt.port}", token=WRONG, base_delay=0.1)
        try:
            await et.connect()
            assert not await _until(lambda: len(gt._authenticated_clients) > 0, 1.0)
            gt._token = WRONG  # operator fixes the token on the ground
            return await _until(lambda: len(gt._authenticated_clients) == 1 and et.connected, 8.0)
        finally:
            await et.disconnect()
            await gt.stop()

    assert asyncio.run(scenario())


def test_ordinary_close_keeps_fast_reconnect_and_rate_limits_the_warning(caplog):
    """A non-auth close (cable pull, ground restart) is retried at the base delay."""
    caplog.set_level(logging.INFO, logger="flightrisk.transport")

    async def scenario():
        connects = [0]

        async def handler(ws):
            connects[0] += 1
            await ws.close(code=4500, reason="restarting")

        async with websockets.serve(handler, "localhost", 0) as server:
            port = server.sockets[0].getsockname()[1]
            et = EdgeTransport(ws_url=f"ws://localhost:{port}", base_delay=1.0, max_delay=2.0)
            try:
                await et.connect()
                await asyncio.sleep(3.5)
            finally:
                await et.disconnect()
        return connects[0]

    connects = asyncio.run(scenario())
    assert connects >= 3  # 1 s spacing, not the auth backoff
    closed = [r.getMessage() for r in caplog.records if "Connection to ground closed" in r.getMessage()]
    assert len(closed) == 1
    assert "code=4500" in closed[0] and "restarting" in closed[0]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_auth_backoff_doubles_from_one_to_ten_seconds():
    assert [auth_backoff_s(n) for n in (0, 1, 2, 3, 4, 5, 6, 7, 40)] == [0, 1, 2, 4, 8, 10, 10, 10, 10]


def test_stable_connection_resets_auth_state():
    clock = [100.0]
    et = EdgeTransport(token=WRONG, clock=lambda: clock[0])
    et._auth_rejections = 5
    et._connected_at_s = 100.0
    clock[0] = 100.0 + limits.STABLE_UPTIME_S

    class _Ws:
        close_code = 1006
        close_reason = ""

    et._log_close_and_back_off(_Ws())
    assert et._auth_rejections == 0


def test_rate_limited_log_first_every_nth_and_time():
    now = [0.0]
    rl = RateLimitedLog(every=3, min_interval_s=60.0, clock=lambda: now[0])
    assert [rl.should_log("a") for _ in range(7)] == [1, 0, 0, 3, 0, 0, 3]
    assert rl.should_log("b") == 1
    now[0] = 61.0
    assert rl.should_log("a") == 1


def test_sync_send_before_connect_is_quiet():
    sync = EdgeTransportSync(ws_url="ws://localhost:1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert sync.send({"type": "telemetry"}) is False
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)]


@pytest.mark.filterwarnings("error::RuntimeWarning")
def test_bridge_run_without_loop_closes_the_coroutine():
    async def work():
        return 1

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError):
            AsyncBridge().run(work())
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)]
