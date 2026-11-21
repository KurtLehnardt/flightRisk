"""Hardening tests for the Claude reasoning client: timeouts, breaker, rate cap, downscale.

The Anthropic client is always mocked; no test makes a network call.
"""

import base64
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from flightrisk.config import FlightRiskConfig, reset_config
from flightrisk.reasoning import claude_agent
from flightrisk.reasoning.claude_agent import (
    BREAKER_OPEN_MESSAGE,
    RATE_CAP_MESSAGE,
    ClaudeReasoningAgent,
    downscale_for_upload,
)

FAKE_KEY = "sk-ant-test-not-a-real-key"
OK_TEXT = "MATCH: yes\nCONFIDENCE: high\nREASONING: same red jacket"


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


def _ok():
    return SimpleNamespace(
        stop_reason="end_turn", stop_details=None, usage=None,
        content=[SimpleNamespace(type="text", text=OK_TEXT)],
    )


@contextmanager
def fake_anthropic():
    module = MagicMock(name="anthropic")
    with patch.dict(sys.modules, {"anthropic": module}):
        yield module.Anthropic


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for var in ("FLIGHTRISK_CLAUDE_API_KEY", "FLIGHTRISK_CLAUDE_MODEL",
                "FLIGHTRISK_CLAUDE_EFFORT", "FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD",
                "FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE",
                "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FLIGHTRISK_ALLOW_CLOUD_LLM", "1")
    reset_config()
    yield
    reset_config()


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock for the breaker and rate window."""
    now = [1000.0]
    monkeypatch.setattr(claude_agent.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def make_agent():
    def _make(**kwargs):
        with fake_anthropic() as cls:
            client = cls.return_value
            client.beta.messages.create.return_value = _ok()
            agent = ClaudeReasoningAgent(api_key=FAKE_KEY, **kwargs)
        return agent, client, cls
    return _make


@pytest.fixture
def img():
    return np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8)


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

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MODEL", "claude-opus-5-5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_EFFORT", "low")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", "5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "12.5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "7")
        cfg = FlightRiskConfig.from_env().reasoning
        assert (cfg.claude_model, cfg.claude_effort) == ("claude-opus-5-5", "low")
        assert cfg.claude_breaker_threshold == 5
        assert cfg.claude_breaker_cooloff == 12.5
        assert cfg.claude_max_calls_per_minute == 7

    def test_agent_uses_defaults_in_request(self, make_agent, img):
        agent, client, _ = make_agent()
        agent.analyze_match(img, img)
        kwargs = client.beta.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-5-5"
        assert kwargs["output_config"] == {"effort": "high"}
        assert kwargs["max_tokens"] == 8192

    def test_client_built_with_short_timeout_and_one_retry(self, make_agent):
        _, _, cls = make_agent()
        kwargs = cls.call_args.kwargs
        assert kwargs["timeout"] == 15.0
        assert kwargs["max_retries"] == 1


class TestFailureResults:
    @pytest.mark.parametrize("exc", [
        APITimeoutError("timed out"),
        RateLimitError("429"),
        APIConnectionError("no route"),
        InternalServerError("503"),
    ])
    def test_api_failures_return_error_shaped_result(self, make_agent, img, exc):
        agent, client, _ = make_agent()
        client.beta.messages.create.side_effect = exc
        result = agent.analyze_match(img, img)
        assert result["match"] is False
        assert result["confidence"] == "error"
        assert FAKE_KEY not in result["reasoning"]


class TestCircuitBreaker:
    def _fail(self, agent, client, img, n, exc=None):
        client.beta.messages.create.side_effect = exc or APITimeoutError("t")
        for _ in range(n):
            agent.analyze_match(img, img)

    def test_opens_after_threshold_and_skips_without_calling(self, make_agent, img, clock):
        agent, client, _ = make_agent()
        self._fail(agent, client, img, 3)
        assert client.beta.messages.create.call_count == 3

        result = agent.analyze_match(img, img)
        assert client.beta.messages.create.call_count == 3  # no new call
        assert result["confidence"] == "error"
        assert result["reasoning"] == BREAKER_OPEN_MESSAGE

    def test_stays_closed_below_threshold(self, make_agent, img, clock):
        agent, client, _ = make_agent()
        self._fail(agent, client, img, 2)
        client.beta.messages.create.side_effect = None
        assert agent.analyze_match(img, img)["match"] is True

    def test_non_outage_errors_do_not_open_breaker(self, make_agent, img, clock):
        agent, client, _ = make_agent()
        self._fail(agent, client, img, 5, exc=BadRequestError("bad"))
        assert client.beta.messages.create.call_count == 5

    def test_success_resets_failure_count(self, make_agent, img, clock):
        agent, client, _ = make_agent()
        self._fail(agent, client, img, 2)
        client.beta.messages.create.side_effect = None
        agent.analyze_match(img, img)  # success resets
        self._fail(agent, client, img, 2)
        client.beta.messages.create.side_effect = None
        assert agent.analyze_match(img, img)["match"] is True  # still closed

    def test_resets_after_cooloff(self, make_agent, img, clock):
        agent, client, _ = make_agent()
        self._fail(agent, client, img, 3)
        clock[0] += 59
        assert agent.analyze_match(img, img)["reasoning"] == BREAKER_OPEN_MESSAGE
        clock[0] += 2  # past the 60 s cool-off
        client.beta.messages.create.side_effect = None
        assert agent.analyze_match(img, img)["match"] is True
        # Recovered: a later single failure does not reopen it.
        self._fail(agent, client, img, 1)
        client.beta.messages.create.side_effect = None
        assert agent.analyze_match(img, img)["match"] is True

    def test_failure_after_cooloff_reopens_immediately(self, make_agent, img, clock):
        agent, client, _ = make_agent()
        self._fail(agent, client, img, 3)
        clock[0] += 61
        self._fail(agent, client, img, 1)
        assert agent.analyze_match(img, img)["reasoning"] == BREAKER_OPEN_MESSAGE

    def test_threshold_and_cooloff_come_from_config(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", "1")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", "5")
        reset_config()
        agent, client, _ = make_agent()
        self._fail(agent, client, img, 1)
        assert agent.analyze_match(img, img)["reasoning"] == BREAKER_OPEN_MESSAGE
        clock[0] += 6
        client.beta.messages.create.side_effect = None
        assert agent.analyze_match(img, img)["match"] is True


class TestRateCap:
    def test_over_cap_skips_without_calling(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "2")
        reset_config()
        agent, client, _ = make_agent()
        assert agent.analyze_match(img, img)["match"] is True
        assert agent.analyze_match(img, img)["match"] is True
        result = agent.analyze_match(img, img)
        assert client.beta.messages.create.call_count == 2
        assert result["confidence"] == "error"
        assert result["reasoning"] == RATE_CAP_MESSAGE

    def test_window_rolls_over(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "1")
        reset_config()
        agent, client, _ = make_agent()
        agent.analyze_match(img, img)
        assert agent.analyze_match(img, img)["reasoning"] == RATE_CAP_MESSAGE
        clock[0] += 61
        assert agent.analyze_match(img, img)["match"] is True

    def test_skipped_calls_do_not_trip_breaker(self, monkeypatch, make_agent, img, clock):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", "1")
        reset_config()
        agent, client, _ = make_agent()
        agent.analyze_match(img, img)
        for _ in range(10):
            agent.analyze_match(img, img)
        clock[0] += 61
        assert agent.analyze_match(img, img)["match"] is True


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
        agent, client, _ = make_agent()
        big = np.random.randint(0, 255, (1500, 2000, 3), dtype=np.uint8)
        agent.analyze_match(big, big)
        content = client.beta.messages.create.call_args.kwargs["messages"][0]["content"]
        for block in content[:2]:
            raw = base64.b64decode(block["source"]["data"])
            decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            assert max(decoded.shape[:2]) == 768
