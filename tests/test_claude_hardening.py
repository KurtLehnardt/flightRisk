"""Hardening tests for the Claude reasoning client.

Covers the streamed call, the per-read timeout, the call deadline, the single
capped retry, the circuit breaker, the per-minute cap, key rejection, the
environment isolation of the SDK client, and image handling.

Most tests use a mocked Anthropic client. The ``TestRealClient`` and
``TestEnvironmentIsolation`` tests run the real SDK client against a local fake
server on 127.0.0.1; no test reaches the internet.
"""

import base64
import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from flightrisk.config import FlightRiskConfig, reset_config
from flightrisk.reasoning import claude_agent
from flightrisk.reasoning.claude_agent import (
    BREAKER_OPEN_MESSAGE,
    DEADLINE_MESSAGE,
    KEY_REJECTED_MESSAGE,
    RATE_CAP_MESSAGE,
    ClaudeReasoningAgent,
    downscale_for_upload,
)

FAKE_KEY = "sk-ant-test-not-a-real-key"
OK_TEXT = "MATCH: yes\nCONFIDENCE: high\nREASONING: same red jacket"
API_URL = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class RateLimitError(Exception):
    status_code = 429


class InternalServerError(Exception):
    status_code = 503


class BadRequestError(Exception):
    status_code = 400


def _block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def _message(text=OK_TEXT, stop_reason="end_turn", content=None, stop_details=None):
    if content is None:
        content = [_block("thinking", thinking=""), _block("text", text=text)]
    return SimpleNamespace(stop_reason=stop_reason, stop_details=stop_details, usage=None, content=content)


def _request():
    return httpx2.Request("POST", API_URL)


def _status_error(cls, status, headers=None):
    response = httpx2.Response(status, headers=headers or {}, request=_request())
    return cls(f"Error code: {status}", response=response, body=None)


class FakeClock:
    """A controllable monotonic clock, injected into the agent (never patched globally)."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SleepRecorder:
    def __init__(self, clock=None):
        self.waits = []
        self._clock = clock

    def __call__(self, seconds):
        self.waits.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


@contextmanager
def fake_anthropic():
    """Stand in for the anthropic module while the agent is built; keeps the real Timeout."""
    module = MagicMock(name="anthropic")
    with patch.dict(sys.modules, {"anthropic": module}):
        yield module


def _set_final_message(client, message):
    stream = client.beta.messages.stream.return_value.__enter__.return_value
    stream.get_final_message.return_value = message
    return stream


_ENV_VARS = (
    "FLIGHTRISK_CLAUDE_API_KEY", "FLIGHTRISK_CLAUDE_MODEL", "FLIGHTRISK_CLAUDE_EFFORT",
    "FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", "FLIGHTRISK_CLAUDE_BREAKER_COOLOFF",
    "FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "FLIGHTRISK_CLAUDE_DEADLINE", "FLIGHTRISK_CLAUDE_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE",
)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FLIGHTRISK_ALLOW_CLOUD_LLM", "1")
    reset_config()
    yield
    reset_config()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_agent(clock):
    """A ClaudeReasoningAgent with a mocked client, the fake clock and a recording sleep."""

    def _make(**kwargs):
        sleep = SleepRecorder(clock)
        with fake_anthropic() as module:
            client = module.Anthropic.return_value
            _set_final_message(client, _message())
            agent = ClaudeReasoningAgent(api_key=FAKE_KEY, clock=clock, sleep=sleep, **kwargs)
        return agent, client, module, sleep
    return _make


@pytest.fixture
def img():
    return np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8)


def _stream_calls(client):
    return client.beta.messages.stream.call_count


# ---------------------------------------------------------------------------
# Defaults and client settings
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_model_and_effort_defaults(self):
        cfg = FlightRiskConfig()
        assert cfg.reasoning.claude_model == "claude-sonnet-5-5"
        assert cfg.reasoning.claude_effort == "high"

    def test_hardening_defaults(self):
        cfg = FlightRiskConfig().reasoning
        assert cfg.claude_breaker_threshold == 3
        assert cfg.claude_breaker_cooloff == 60.0
        assert cfg.claude_max_calls_per_minute == 20
        assert cfg.claude_deadline == 90.0

    def test_env_overrides(self, monkeypatch):
        # Every value differs from its default, so an ignored variable fails the test.
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MODEL", "claude-opus-5-5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_EFFORT", "low")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", "5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "12.5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "7")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_DEADLINE", "45")
        cfg = FlightRiskConfig.from_env().reasoning
        assert (cfg.claude_model, cfg.claude_effort) == ("claude-opus-5-5", "low")
        assert cfg.claude_breaker_threshold == 5
        assert cfg.claude_breaker_cooloff == 12.5
        assert cfg.claude_max_calls_per_minute == 7
        assert cfg.claude_deadline == 45.0

    def test_agent_takes_settings_from_config(self, monkeypatch, make_agent):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", "5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "12.5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "7")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_DEADLINE", "45")
        reset_config()
        agent, *_ = make_agent()
        assert agent._breaker_threshold == 5
        assert agent._breaker_cooloff == 12.5
        assert agent._max_calls_per_minute == 7
        assert agent._deadline_s == 45.0

    def test_request_is_streamed_with_defaults_and_no_sampling(self, make_agent, img):
        agent, client, *_ = make_agent()
        agent.analyze_match(img, img)
        client.beta.messages.create.assert_not_called()
        kwargs = client.beta.messages.stream.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-5-5"
        assert kwargs["output_config"] == {"effort": "high"}
        assert kwargs["max_tokens"] == 4096
        for param in ("thinking", "temperature", "top_p", "top_k", "stream"):
            assert param not in kwargs

    def test_client_settings_are_explicit(self, make_agent):
        _, _, module, _ = make_agent()
        kwargs = module.Anthropic.call_args.kwargs
        assert kwargs["max_retries"] == 0
        assert kwargs["base_url"] == "https://api.anthropic.com"
        assert kwargs["default_headers"] == {}
        assert kwargs["open_telemetry"] == {"traces": {"content_mode": "metadata_only"}}
        assert module.Timeout.call_args.kwargs == {"connect": 5.0, "read": 20.0, "write": 10.0, "pool": 5.0}
        assert kwargs["timeout"] is module.Timeout.return_value


class TestSettingClamps:
    """Out-of-range or non-finite hardening settings are clamped, with a warning."""

    @pytest.mark.filterwarnings("ignore::UserWarning")  # int("nan") is rejected by the config loader
    @pytest.mark.parametrize("var,attr,raw,expected", [
        ("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "_max_calls_per_minute", "0", 1),
        ("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "_max_calls_per_minute", "-1", 1),
        ("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "_max_calls_per_minute", "nan", 20),
        ("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "_max_calls_per_minute", "inf", 20),
        ("FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", "_breaker_threshold", "0", 1),
        ("FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", "_breaker_threshold", "-1", 1),
        ("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "_breaker_cooloff", "0", 1.0),
        ("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "_breaker_cooloff", "-1", 1.0),
        ("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "_breaker_cooloff", "nan", 60.0),
        ("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "_breaker_cooloff", "inf", 3600.0),
        ("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "_breaker_cooloff", "99999", 3600.0),
        ("FLIGHTRISK_CLAUDE_DEADLINE", "_deadline_s", "0", 10.0),
        ("FLIGHTRISK_CLAUDE_DEADLINE", "_deadline_s", "-1", 10.0),
        ("FLIGHTRISK_CLAUDE_DEADLINE", "_deadline_s", "nan", 90.0),
        ("FLIGHTRISK_CLAUDE_DEADLINE", "_deadline_s", "inf", 600.0),
        ("FLIGHTRISK_CLAUDE_DEADLINE", "_deadline_s", "1e9", 600.0),
    ])
    def test_env_values_are_clamped(self, monkeypatch, make_agent, var, attr, raw, expected):
        monkeypatch.setenv(var, raw)
        reset_config()
        agent, *_ = make_agent()
        assert getattr(agent, attr) == expected

    @pytest.mark.parametrize("field,value,attr,expected", [
        ("claude_max_calls_per_minute", 0, "_max_calls_per_minute", 1),
        ("claude_max_calls_per_minute", float("inf"), "_max_calls_per_minute", 20),
        ("claude_breaker_threshold", -1, "_breaker_threshold", 1),
        ("claude_breaker_cooloff", float("nan"), "_breaker_cooloff", 60.0),
        ("claude_breaker_cooloff", float("inf"), "_breaker_cooloff", 3600.0),
        ("claude_deadline", -1.0, "_deadline_s", 10.0),
        ("claude_deadline", float("-inf"), "_deadline_s", 10.0),
    ])
    def test_clamp_logs_a_warning(self, make_agent, caplog, field, value, attr, expected):
        cfg = FlightRiskConfig()
        setattr(cfg.reasoning, field, value)
        with patch("flightrisk.reasoning.claude_agent.get_config", return_value=cfg), \
                caplog.at_level(logging.WARNING, logger="flightrisk.reasoning.claude_agent"):
            agent, *_ = make_agent()
        assert getattr(agent, attr) == expected
        assert any("claude_setting_clamped" in r.getMessage() for r in caplog.records)

    def test_in_range_values_do_not_warn(self, make_agent, caplog):
        with caplog.at_level(logging.WARNING, logger="flightrisk.reasoning.claude_agent"):
            make_agent()
        assert not [r for r in caplog.records if "claude_setting_clamped" in r.getMessage()]


# ---------------------------------------------------------------------------
# Failures, retry and key rejection
# ---------------------------------------------------------------------------

class TestFailureResults:
    @pytest.mark.parametrize("exc", [
        APITimeoutError("timed out"),
        RateLimitError("429"),
        APIConnectionError("no route"),
        InternalServerError("503"),
    ])
    def test_api_failures_return_error_shaped_result(self, make_agent, img, exc):
        agent, client, *_ = make_agent()
        client.beta.messages.stream.side_effect = exc
        result = agent.analyze_match(img, img)
        assert result["match"] is False
        assert result["confidence"] == "error"
        assert FAKE_KEY not in result["reasoning"]

    def test_real_sdk_timeout_counts_toward_breaker_and_is_not_retried(self, make_agent, img):
        agent, client, _, sleep = make_agent()
        client.beta.messages.stream.side_effect = anthropic.APITimeoutError(request=_request())
        result = agent.analyze_match(img, img)
        assert result["confidence"] == "error"
        assert _stream_calls(client) == 1
        assert sleep.waits == []
        assert agent.health()["consecutive_failures"] == 1

    def test_real_sdk_overloaded_529_is_retried_once_then_counts(self, make_agent, img):
        agent, client, _, sleep = make_agent()
        client.beta.messages.stream.side_effect = _status_error(anthropic.OverloadedError, 529)
        result = agent.analyze_match(img, img)
        assert result["confidence"] == "error"
        assert _stream_calls(client) == 2
        assert sleep.waits == [1.0]
        assert agent.health()["consecutive_failures"] == 1  # one logical call, one failure

    def test_real_sdk_authentication_error_turns_agent_off(self, make_agent, img, capsys):
        agent, client, _, sleep = make_agent()
        client.beta.messages.stream.side_effect = _status_error(anthropic.AuthenticationError, 401)
        result = agent.analyze_match(img, img)
        assert result == {"match": False, "confidence": "unavailable", "reasoning": KEY_REJECTED_MESSAGE}
        assert sleep.waits == []
        out = capsys.readouterr().out
        assert "rejected the API key" in out and "HTTP 401" in out
        assert FAKE_KEY not in out
        # Further calls are skipped without touching the API.
        assert agent.analyze_match(img, img)["confidence"] == "unavailable"
        assert agent.match_description(img, "red jacket")["reasoning"] == KEY_REJECTED_MESSAGE
        assert _stream_calls(client) == 1
        assert agent.health()["key_rejected"] is True

    def test_permission_denied_turns_agent_off(self, make_agent, img):
        agent, client, *_ = make_agent()
        client.beta.messages.stream.side_effect = _status_error(anthropic.PermissionDeniedError, 403)
        assert agent.analyze_match(img, img)["confidence"] == "unavailable"
        assert agent._available is False

    def test_key_rejection_notice_is_printed_once(self, make_agent, img, capsys):
        agent, client, *_ = make_agent()
        client.beta.messages.stream.side_effect = _status_error(anthropic.AuthenticationError, 401)
        with pytest.raises(claude_agent.ClaudeKeyRejected):
            agent._ask("p", [])
        with pytest.raises(claude_agent.ClaudeKeyRejected):
            agent._ask("p", [])
        assert capsys.readouterr().out.count("rejected the API key") == 1

    def test_a_new_agent_starts_with_the_key_accepted(self, make_agent, img):
        agent, client, *_ = make_agent()
        client.beta.messages.stream.side_effect = _status_error(anthropic.AuthenticationError, 401)
        agent.analyze_match(img, img)
        fresh, *_ = make_agent()  # what a restart or re-selecting Claude builds
        assert fresh.analyze_match(img, img)["match"] is True


class TestRetry:
    def test_connection_error_retried_once_then_succeeds(self, make_agent, img):
        agent, client, _, sleep = make_agent()
        client.beta.messages.stream.side_effect = _then_ok(client, anthropic.APIConnectionError(request=_request()))
        assert agent.analyze_match(img, img)["match"] is True
        assert _stream_calls(client) == 2
        assert sleep.waits == [1.0]
        assert agent.health()["consecutive_failures"] == 0

    def test_connect_timeout_is_a_connection_error_and_retried(self, make_agent, img):
        agent, client, _, sleep = make_agent()
        exc = anthropic.APITimeoutError(request=_request())
        exc.__cause__ = httpx2.ConnectTimeout("connect timed out")
        client.beta.messages.stream.side_effect = _then_ok(client, exc)
        assert agent.analyze_match(img, img)["match"] is True
        assert sleep.waits == [1.0]

    def test_429_retry_after_is_capped_at_two_seconds(self, make_agent, img):
        agent, client, _, sleep = make_agent()
        exc = _status_error(anthropic.RateLimitError, 429, {"retry-after": "300"})
        client.beta.messages.stream.side_effect = _then_ok(client, exc)
        assert agent.analyze_match(img, img)["match"] is True
        assert sleep.waits == [2.0]

    def test_short_retry_after_is_honoured(self, make_agent, img):
        agent, client, _, sleep = make_agent()
        exc = _status_error(anthropic.RateLimitError, 429, {"retry-after-ms": "250"})
        client.beta.messages.stream.side_effect = _then_ok(client, exc)
        agent.analyze_match(img, img)
        assert sleep.waits == [0.25]

    @pytest.mark.parametrize("exc", [
        lambda: anthropic.APITimeoutError(request=_request()),
        lambda: _status_error(anthropic.BadRequestError, 400),
        lambda: _status_error(anthropic.InternalServerError, 500),
        lambda: RuntimeError("local bug"),
    ])
    def test_other_errors_are_not_retried(self, make_agent, img, exc):
        agent, client, _, sleep = make_agent()
        client.beta.messages.stream.side_effect = exc()
        agent.analyze_match(img, img)
        assert _stream_calls(client) == 1
        assert sleep.waits == []

    def test_failure_after_the_stream_started_is_not_retried(self, make_agent, img):
        agent, client, _, sleep = make_agent()
        stream = client.beta.messages.stream.return_value.__enter__.return_value
        stream.__iter__.side_effect = lambda: iter_raising(httpx2.ReadError("connection reset"))
        result = agent.analyze_match(img, img)
        assert result["confidence"] == "error"
        assert _stream_calls(client) == 1
        assert sleep.waits == []
        assert agent.health()["consecutive_failures"] == 1  # a dropped stream is an outage

    def test_no_retry_when_the_wait_would_pass_the_deadline(self, make_agent, img, clock):
        agent, client, _, sleep = make_agent()

        def overloaded(**kwargs):
            clock.advance(agent._deadline_s - 0.5)
            raise _status_error(anthropic.OverloadedError, 529)

        client.beta.messages.stream.side_effect = overloaded
        agent.analyze_match(img, img)
        assert _stream_calls(client) == 1
        assert sleep.waits == []


def _then_ok(client, exc):
    """A stream side effect that raises ``exc`` once, then returns the normal stream manager."""
    calls = []

    def effect(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise exc
        return client.beta.messages.stream.return_value
    return effect


def iter_raising(exc):
    yield from ()
    raise exc


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------

class TestDeadline:
    def test_progressing_stream_is_stopped_at_the_deadline_and_closed(self, make_agent, img, clock):
        agent, client, _, sleep = make_agent()
        manager = client.beta.messages.stream.return_value

        def events():
            for _ in range(1000):
                clock.advance(1.0)  # a slow reply that keeps sending
                yield SimpleNamespace(type="content_block_delta")

        manager.__enter__.return_value.__iter__.side_effect = events
        result = agent.analyze_match(img, img)
        assert result["confidence"] == "error"
        assert result["reasoning"] == DEADLINE_MESSAGE.format(seconds=90)
        manager.__exit__.assert_called_once()  # the with block closed the stream
        assert sleep.waits == []  # never retried
        assert agent.health()["consecutive_failures"] == 0  # slow is not an outage

    def test_deadline_does_not_reset_a_running_failure_count(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        client.beta.messages.stream.side_effect = anthropic.APITimeoutError(request=_request())
        agent.analyze_match(img, img)
        client.beta.messages.stream.side_effect = None
        manager = client.beta.messages.stream.return_value

        def slow():
            clock.advance(100.0)
            yield SimpleNamespace(type="ping")

        manager.__enter__.return_value.__iter__.side_effect = slow
        agent.analyze_match(img, img)
        assert agent.health()["consecutive_failures"] == 1


# ---------------------------------------------------------------------------
# Circuit breaker and rate cap
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def _fail(self, agent, client, img, n, exc=None):
        client.beta.messages.stream.side_effect = exc or APITimeoutError("t")
        for _ in range(n):
            agent.analyze_match(img, img)
        client.beta.messages.stream.side_effect = None

    def test_opens_after_threshold_and_skips_without_calling(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 3)
        assert _stream_calls(client) == 3

        result = agent.analyze_match(img, img)
        assert _stream_calls(client) == 3  # no new call
        assert result == {"match": False, "confidence": "skipped",
                          "reasoning": BREAKER_OPEN_MESSAGE.format(seconds=60)}
        assert result["reasoning"] == "AI check paused (Claude unavailable, retrying in 60 s)."

    def test_skip_is_logged_at_info_not_warning(self, make_agent, img, clock, caplog):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 3)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="flightrisk.reasoning"):
            agent.analyze_match(img, img)
        skipped = [r for r in caplog.records if "reasoning_call_skipped" in r.getMessage()]
        assert skipped and all(r.levelno == logging.INFO for r in skipped)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_skipped_call_does_not_encode_images(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 3)
        with patch.object(agent, "_image_to_base64", wraps=agent._image_to_base64) as encode:
            agent.analyze_match(img, img)
            agent.match_description(img, "red jacket")
        encode.assert_not_called()

    def test_stays_closed_below_threshold(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 2)
        assert agent.analyze_match(img, img)["match"] is True

    def test_non_outage_errors_do_not_open_breaker(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 5, exc=BadRequestError("bad"))
        assert _stream_calls(client) == 5

    def test_success_resets_failure_count(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 2)
        agent.analyze_match(img, img)  # success resets
        self._fail(agent, client, img, 2)
        assert agent.analyze_match(img, img)["match"] is True  # still closed

    @pytest.mark.parametrize("answer", [
        lambda client: setattr(client.beta.messages.stream, "side_effect",
                               _status_error(anthropic.BadRequestError, 400)),
        lambda client: _set_final_message(client, _message(text=None, stop_reason="refusal", content=[],
                                                          stop_details=SimpleNamespace(category="cyber"))),
    ])
    def test_reachable_api_answer_resets_failure_count(self, make_agent, img, clock, answer):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 2)
        answer(client)
        assert agent.analyze_match(img, img)["confidence"] == "error"
        assert agent.health()["consecutive_failures"] == 0
        client.beta.messages.stream.side_effect = None
        _set_final_message(client, _message())
        self._fail(agent, client, img, 2)
        assert agent.analyze_match(img, img)["match"] is True  # 2 + 2 never reached 3

    def test_resets_after_cooloff(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 3)
        clock.advance(59)
        assert agent.analyze_match(img, img)["reasoning"] == BREAKER_OPEN_MESSAGE.format(seconds=1)
        clock.advance(2)  # past the 60 s cool-off
        assert agent.analyze_match(img, img)["match"] is True
        # Recovered: a later single failure does not reopen it.
        self._fail(agent, client, img, 1)
        assert agent.analyze_match(img, img)["match"] is True

    def test_failure_after_cooloff_reopens_immediately(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 3)
        clock.advance(61)
        self._fail(agent, client, img, 1)
        assert agent.analyze_match(img, img)["confidence"] == "skipped"

    def test_threshold_and_cooloff_come_from_config(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", "1")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "5")
        reset_config()
        agent, client, *_ = make_agent()
        self._fail(agent, client, img, 1)
        assert agent.analyze_match(img, img)["reasoning"] == BREAKER_OPEN_MESSAGE.format(seconds=5)
        clock.advance(6)
        assert agent.analyze_match(img, img)["match"] is True


class TestRateCap:
    def test_over_cap_skips_without_calling(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "2")
        reset_config()
        agent, client, *_ = make_agent()
        assert agent.analyze_match(img, img)["match"] is True
        assert agent.analyze_match(img, img)["match"] is True
        result = agent.analyze_match(img, img)
        assert _stream_calls(client) == 2
        assert result == {"match": False, "confidence": "skipped", "reasoning": RATE_CAP_MESSAGE}
        assert result["reasoning"] == "AI check skipped (rate cap)."

    def test_window_slides(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "2")
        reset_config()
        agent, *_ = make_agent()
        start = clock.now
        assert agent.analyze_match(img, img)["match"] is True          # t=0
        clock.now = start + 59
        assert agent.analyze_match(img, img)["match"] is True          # t=59
        clock.now = start + 59.5
        assert agent.analyze_match(img, img)["confidence"] == "skipped"
        clock.now = start + 61                                          # t=0 has left the window
        assert agent.analyze_match(img, img)["match"] is True
        assert agent.health()["calls_last_minute"] == 2                 # t=59 and t=61
        assert agent.analyze_match(img, img)["confidence"] == "skipped"

    def test_skipped_calls_do_not_trip_breaker(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "1")
        reset_config()
        agent, *_ = make_agent()
        agent.analyze_match(img, img)
        for _ in range(10):
            agent.analyze_match(img, img)
        clock.advance(61)
        assert agent.analyze_match(img, img)["match"] is True

    def test_cap_counts_logical_calls_not_retries(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "1")
        reset_config()
        agent, client, *_ = make_agent()
        client.beta.messages.stream.side_effect = _then_ok(client, _status_error(anthropic.OverloadedError, 529))
        assert agent.analyze_match(img, img)["match"] is True
        assert _stream_calls(client) == 2
        assert agent.health()["calls_last_minute"] == 1


class TestHealth:
    def test_closed_state(self, make_agent, img):
        agent, *_ = make_agent()
        agent.analyze_match(img, img)
        assert agent.health() == {
            "breaker": "closed", "breaker_seconds_left": 0, "consecutive_failures": 0,
            "calls_last_minute": 1, "max_calls_per_minute": 20, "key_rejected": False,
        }

    def test_open_state_counts_down(self, make_agent, img, clock):
        agent, client, *_ = make_agent()
        client.beta.messages.stream.side_effect = APITimeoutError("t")
        for _ in range(3):
            agent.analyze_match(img, img)
        clock.advance(20.5)
        health = agent.health()
        assert health["breaker"] == "open"
        assert health["breaker_seconds_left"] == 40
        assert health["consecutive_failures"] == 3

    def test_reasoning_state_exposes_health(self, make_agent, clean_app_state):
        from flightrisk.dashboard import app as app_module
        from flightrisk.dashboard.state import app_state

        agent, *_ = make_agent()
        app_state.reasoning = agent
        state = app_module._reasoning_state()
        assert state["health"]["breaker"] == "closed"
        assert state["health"]["max_calls_per_minute"] == 20
        app_state.reasoning = None
        assert app_module._reasoning_state()["health"] is None


# ---------------------------------------------------------------------------
# Reply text
# ---------------------------------------------------------------------------

class TestVerdictText:
    def test_thinking_blocks_are_not_part_of_the_verdict(self, make_agent, img):
        agent, client, *_ = make_agent()
        _set_final_message(client, _message(content=[
            _block("thinking", thinking="MATCH: no"), _block("text", text=OK_TEXT)]))
        result = agent.analyze_match(img, img)
        assert result["match"] is True and result["confidence"] == "high"

    def test_only_text_after_the_last_fallback_block_counts(self, make_agent, img):
        agent, client, *_ = make_agent()
        _set_final_message(client, _message(content=[
            _block("text", text="MATCH: no\nCONFIDENCE: low\nREASONING: partial from declined model"),
            _block("fallback"),
            _block("text", text="MATCH: no\nCONFIDENCE: low\nREASONING: second partial"),
            _block("fallback"),
            _block("thinking", thinking=""),
            _block("text", text=OK_TEXT),
        ]))
        result = agent.analyze_match(img, img)
        assert result == {"match": True, "confidence": "high", "reasoning": "same red jacket"}

    def test_fallback_with_no_text_after_it_is_an_error(self, make_agent, img):
        agent, client, *_ = make_agent()
        _set_final_message(client, _message(content=[_block("text", text=OK_TEXT), _block("fallback")]))
        assert agent.analyze_match(img, img)["confidence"] == "error"


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

class TestDownscale:
    @pytest.mark.parametrize("shape,expected", [
        ((2000, 1000, 3), (768, 384)),   # tall: h is long edge
        ((1000, 3000, 3), (256, 768)),   # wide: w is long edge
        ((768, 768, 3), (768, 768)),     # exactly at cap, untouched
        ((100, 50, 3), (100, 50)),       # small, never upscaled
    ])
    def test_dimensions(self, shape, expected):
        out = downscale_for_upload(np.zeros(shape, dtype=np.uint8))
        assert out.shape[:2] == expected

    def test_aspect_ratio_preserved(self):
        out = downscale_for_upload(np.zeros((1080, 1920, 3), dtype=np.uint8))
        assert max(out.shape[:2]) == 768
        assert abs(out.shape[1] / out.shape[0] - 1920 / 1080) < 0.01

    def test_sent_images_are_downscaled(self, make_agent):
        agent, client, *_ = make_agent()
        big = np.random.randint(0, 255, (1500, 2000, 3), dtype=np.uint8)
        agent.analyze_match(big, big)
        content = client.beta.messages.stream.call_args.kwargs["messages"][0]["content"]
        for block in content[:2]:
            raw = base64.b64decode(block["source"]["data"])
            decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            assert max(decoded.shape[:2]) == 768

    def test_edge_crop_validator_uses_the_same_helper(self):
        from flightrisk.dashboard import alerts
        from flightrisk.vision import resize

        assert alerts.downscale_long_edge is resize.downscale_long_edge
        assert claude_agent.downscale_long_edge is resize.downscale_long_edge


class TestReferenceCache:
    def test_read_only_reference_is_encoded_once(self, make_agent, img):
        agent, client, *_ = make_agent()
        ref = np.random.randint(0, 255, (1200, 900, 3), dtype=np.uint8)
        ref.setflags(write=False)
        with patch.object(claude_agent, "downscale_for_upload", wraps=claude_agent.downscale_for_upload) as down:
            agent.analyze_match(ref, img)
            agent.analyze_match(ref, img)
        resized_refs = [c for c in down.call_args_list if c.args[0] is ref]
        assert len(resized_refs) == 1
        first, second = (c.kwargs["messages"][0]["content"][0]["source"]["data"]
                         for c in client.beta.messages.stream.call_args_list)
        assert first == second

    def test_writable_reference_is_not_cached(self, make_agent, img):
        agent, *_ = make_agent()
        ref = np.random.randint(0, 255, (100, 80, 3), dtype=np.uint8)
        with patch.object(claude_agent, "downscale_for_upload", wraps=claude_agent.downscale_for_upload) as down:
            agent.analyze_match(ref, img)
            agent.analyze_match(ref, img)
        assert len([c for c in down.call_args_list if c.args[0] is ref]) == 2

    def test_a_new_reference_replaces_the_cache(self, make_agent, img):
        agent, *_ = make_agent()
        first = np.zeros((50, 50, 3), np.uint8)
        second = np.full((50, 50, 3), 255, np.uint8)
        for ref in (first, second):
            ref.setflags(write=False)
        assert agent._reference_b64(first) != agent._reference_b64(second)
        assert agent._ref_cache[0] is second


# ---------------------------------------------------------------------------
# Base URL and environment isolation
# ---------------------------------------------------------------------------

class TestBaseUrl:
    def test_https_override_is_used(self, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BASE_URL", "https://proxy.example.org/anthropic")
        agent = ClaudeReasoningAgent(api_key=FAKE_KEY)
        assert str(agent._client.base_url).startswith("https://proxy.example.org/anthropic")

    @pytest.mark.parametrize("url", [
        "http://api.anthropic.com",
        "ftp://api.anthropic.com",
        "https://user:pass@proxy.example.org",
        "api.anthropic.com",
    ])
    def test_unsafe_override_is_refused(self, monkeypatch, url):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BASE_URL", url)
        with pytest.raises(RuntimeError, match="FLIGHTRISK_CLAUDE_BASE_URL must be an https") as exc:
            ClaudeReasoningAgent(api_key=FAKE_KEY)
        assert "pass" not in str(exc.value)

    def test_startup_notice_names_an_overridden_host(self, monkeypatch, capsys):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BASE_URL", "https://proxy.example.org")
        ClaudeReasoningAgent(api_key=FAKE_KEY)
        out = capsys.readouterr().out
        assert "API host proxy.example.org" in out
        assert FAKE_KEY not in out


class TestEnvironmentIsolation:
    """ANTHROPIC_* variables must not change where the key goes or what is sent."""

    def test_anthropic_base_url_is_ignored(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/evil")
        agent = ClaudeReasoningAgent(api_key=FAKE_KEY)
        assert str(agent._client.base_url) == "https://api.anthropic.com"

    def test_tracing_content_mode_env_is_ignored(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
        agent = ClaudeReasoningAgent(api_key=FAKE_KEY)
        assert agent._client._open_telemetry["traces"]["content_mode"] == "metadata_only"

    def test_custom_headers_env_is_not_sent(self, monkeypatch, fake_api, img):
        monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-evil: 1\nx-api-key: stolen\nAuthorization: Bearer stolen")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stray")
        fake_api.script = [sse_reply(OK_TEXT)]
        agent = real_agent(fake_api)
        assert agent.analyze_match(img, img)["match"] is True
        headers = {k.lower(): v for k, v in fake_api.requests[0]["headers"].items()}
        assert "x-evil" not in headers
        assert "x-api-key" not in headers
        assert headers["authorization"] == f"Bearer {FAKE_KEY}"


# ---------------------------------------------------------------------------
# The real SDK client against a local fake API
# ---------------------------------------------------------------------------

def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _message_start():
    return _sse("message_start", {"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 1}}})


def sse_reply(text):
    """A streamed reply: a hidden thinking block, then the text, as the API sends it."""
    def script(handler):
        handler.start_stream()
        for chunk in (
            _message_start(),
            _sse("ping", {"type": "ping"}),
            _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
            _sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "signature_delta", "signature": "sig"}}),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse("content_block_start", {"type": "content_block_start", "index": 1,
                                         "content_block": {"type": "text", "text": ""}}),
            _sse("content_block_delta", {"type": "content_block_delta", "index": 1,
                                         "delta": {"type": "text_delta", "text": text}}),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 1}),
            _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                      "stop_sequence": None},
                                   "usage": {"output_tokens": 30}}),
            _sse("message_stop", {"type": "message_stop"}),
        ):
            handler.wfile.write(chunk)
    return script


def status_reply(status, headers=None):
    def script(handler):
        body = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}).encode()
        handler.send_response(status)
        for k, v in (headers or {}).items():
            handler.send_header(k, v)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    return script


def pings_only(interval=0.05, limit=10.0):
    """Start a reply, then send only keep-alive pings, as during hidden thinking."""
    def script(handler):
        handler.start_stream()
        handler.wfile.write(_message_start())
        end = time.monotonic() + limit
        while time.monotonic() < end and not handler.server.stopping.is_set():
            handler.wfile.write(_sse("ping", {"type": "ping"}))
            time.sleep(interval)
    return script


def stall_after_start():
    """Start a reply, then send nothing."""
    def script(handler):
        handler.start_stream()
        handler.wfile.write(_message_start())
        handler.server.stopping.wait(10.0)
    return script


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def start_stream(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        api = self.server.api
        with api.lock:
            api.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            script = api.script.pop(0) if api.script else status_reply(500)
        try:
            script(self)
        except (BrokenPipeError, ConnectionResetError):
            api.client_closed.set()


class FakeApi:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.script = []
        self.client_closed = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.server.api = self
        self.server.stopping = threading.Event()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_api():
    api = FakeApi()
    yield api
    api.close()


def real_agent(api, *, clock=time.monotonic, sleep=time.sleep, read_timeout=claude_agent._READ_TIMEOUT_S):
    """A real ClaudeReasoningAgent whose real SDK client points at the local fake API.

    ``resolve_base_url`` only accepts https, so the http test server is wired
    in through the same client builder the agent uses.
    """
    agent = ClaudeReasoningAgent(api_key=FAKE_KEY, clock=clock, sleep=sleep)
    agent._client = claude_agent._build_client(anthropic, FAKE_KEY, api.url, read_timeout=read_timeout)
    return agent


class TestRealClient:
    def test_streamed_reply_is_parsed(self, fake_api, img):
        fake_api.script = [sse_reply(OK_TEXT)]
        agent = real_agent(fake_api)
        result = agent.analyze_match(img, img)
        assert result == {"match": True, "confidence": "high", "reasoning": "same red jacket"}
        assert agent.last_usage == {"input_tokens": 12, "output_tokens": 30}
        body = fake_api.requests[0]["body"]
        assert fake_api.requests[0]["path"] == "/v1/messages"
        assert body["stream"] is True
        assert body["model"] == "claude-sonnet-5-5"
        assert body["max_tokens"] == 4096
        assert body["output_config"] == {"effort": "high"}
        assert body["fallbacks"] == "default"
        for param in ("temperature", "top_p", "top_k", "thinking"):
            assert param not in body
        headers = {k.lower(): v for k, v in fake_api.requests[0]["headers"].items()}
        assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
        assert headers["x-stainless-read-timeout"] == "20.0"

    def test_429_with_long_retry_after_ends_within_seconds(self, fake_api, img):
        fake_api.script = [status_reply(429, {"retry-after": "300"}), status_reply(429, {"retry-after": "300"})]
        agent = real_agent(fake_api)
        t0 = time.monotonic()
        result = agent.analyze_match(img, img)
        elapsed = time.monotonic() - t0
        assert result["confidence"] == "error"
        assert elapsed < 6.0  # the SDK would otherwise sleep for the full 300 s
        assert len(fake_api.requests) == 2  # one capped retry, no SDK retries
        assert agent.health()["consecutive_failures"] == 1

    def test_stalled_stream_fails_on_the_read_timeout_without_retry(self, fake_api, img):
        fake_api.script = [stall_after_start(), sse_reply(OK_TEXT)]
        agent = real_agent(fake_api, read_timeout=0.5)
        t0 = time.monotonic()
        result = agent.analyze_match(img, img)
        assert time.monotonic() - t0 < 5.0
        assert result["confidence"] == "error"
        assert len(fake_api.requests) == 1
        assert agent.health()["consecutive_failures"] == 1  # a stall is an outage

    def test_ping_only_stream_is_stopped_by_the_deadline(self, fake_api, img):
        fake_api.script = [pings_only(interval=0.05, limit=10.0)]
        agent = real_agent(fake_api)
        agent._deadline_s = 0.5  # below the 10 s minimum, set directly to keep the test short
        t0 = time.monotonic()
        result = agent.analyze_match(img, img)
        assert time.monotonic() - t0 < 3.0
        assert result["confidence"] == "error"
        assert result["reasoning"] == DEADLINE_MESSAGE.format(seconds=0)
        assert len(fake_api.requests) == 1
        assert agent.health()["consecutive_failures"] == 0
        assert fake_api.client_closed.wait(3.0)  # the agent closed the connection

    def test_401_turns_the_agent_off(self, fake_api, img, capsys):
        fake_api.script = [status_reply(401)]
        agent = real_agent(fake_api)
        assert agent.analyze_match(img, img)["confidence"] == "unavailable"
        assert agent.analyze_match(img, img)["reasoning"] == KEY_REJECTED_MESSAGE
        assert len(fake_api.requests) == 1
        assert FAKE_KEY not in capsys.readouterr().out
