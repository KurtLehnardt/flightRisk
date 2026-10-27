"""Tests for the Claude reasoning backend and the provider switch.

The Anthropic client is always mocked: no test here makes a network call or
needs a real API key.
"""

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from flightrisk.config import AmberConfig
from flightrisk.reasoning import PROVIDERS, create_reasoning_agent, normalize_provider
from flightrisk.reasoning.claude_agent import ClaudeReasoningAgent

FAKE_KEY = "sk-ant-test-not-a-real-key"
MATCH_TEXT = "MATCH: yes\nCONFIDENCE: high\nREASONING: same red jacket"


def _response(text=MATCH_TEXT, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        content=[SimpleNamespace(type="text", text=text)] if text is not None else [],
    )


@pytest.fixture
def make_agent():
    """Build a ClaudeReasoningAgent whose Anthropic client is a MagicMock."""

    def _make(response=None, **kwargs):
        with patch("anthropic.Anthropic") as mock_cls:
            client = mock_cls.return_value
            client.beta.messages.create.return_value = response or _response()
            agent = ClaudeReasoningAgent(api_key=FAKE_KEY, **kwargs)
        return agent, client

    return _make


@pytest.fixture
def img():
    return np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8)


class TestNormalizeProvider:
    @pytest.mark.parametrize("raw,expected", [
        ("claude", "claude"), (" Claude ", "claude"),
        ("gemma", "gemma"), ("ollama", "gemma"),
        ("off", "off"), ("none", "off"), ("disabled", "off"), ("", "off"),
    ])
    def test_aliases(self, raw, expected):
        assert normalize_provider(raw) == expected

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError, match="Unknown reasoning provider"):
            normalize_provider("gpt")

    def test_none_uses_configured_default(self):
        cfg = AmberConfig()
        cfg.reasoning.provider = "gemma"
        with patch("flightrisk.reasoning.get_config", return_value=cfg):
            assert normalize_provider(None) == "gemma"

    def test_claude_is_the_shipped_default(self):
        assert AmberConfig().reasoning.provider == "claude"
        assert PROVIDERS == ("claude", "gemma", "off")


class TestCreateReasoningAgent:
    def test_off_returns_none(self):
        assert create_reasoning_agent("off") is None

    def test_claude_without_key_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="CLAUDE_API_KEY is not set"):
            create_reasoning_agent("claude")

    def test_claude_with_key_builds_agent(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_API_KEY", FAKE_KEY)
        with patch("anthropic.Anthropic"):
            agent = create_reasoning_agent("claude")
        assert isinstance(agent, ClaudeReasoningAgent)
        assert (agent.provider, agent.label) == ("claude", "Claude")

    def test_gemma_unavailable_raises_instead_of_returning_dead_agent(self):
        """A Gemma agent whose model isn't pulled used to be returned anyway,
        leaving every match stuck on 'Awaiting Gemma reasoning...'."""
        client = MagicMock()
        client.list.return_value = SimpleNamespace(models=[])
        with patch("flightrisk.reasoning.agent.ollama.Client", return_value=client):
            with pytest.raises(RuntimeError, match="not available in Ollama"):
                create_reasoning_agent("gemma")

    def test_gemma_available_returns_agent(self):
        client = MagicMock()
        client.list.return_value = SimpleNamespace(models=[SimpleNamespace(model="gemma4:latest")])
        with patch("flightrisk.reasoning.agent.ollama.Client", return_value=client):
            agent = create_reasoning_agent("gemma")
        assert (agent.provider, agent.label) == ("gemma", "Gemma")


class TestApiKeyResolution:
    def test_claude_api_key_preferred_over_anthropic_api_key(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-from-claude-var")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-anthropic-var")
        with patch("anthropic.Anthropic") as mock_cls:
            ClaudeReasoningAgent()
        assert mock_cls.call_args.kwargs["api_key"] == "sk-ant-from-claude-var"

    def test_falls_back_to_anthropic_api_key(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-anthropic-var")
        with patch("anthropic.Anthropic") as mock_cls:
            ClaudeReasoningAgent()
        assert mock_cls.call_args.kwargs["api_key"] == "sk-ant-from-anthropic-var"

    def test_key_never_appears_in_missing_key_error(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as exc:
            ClaudeReasoningAgent()
        assert "sk-ant" not in str(exc.value)


class TestRequestShape:
    def test_images_precede_prompt_as_base64_jpeg(self, make_agent, img):
        agent, client = make_agent()
        agent.analyze_match(img, img)

        kwargs = client.beta.messages.create.call_args.kwargs
        content = kwargs["messages"][0]["content"]
        assert [b["type"] for b in content] == ["image", "image", "text"]
        for block in content[:2]:
            assert block["source"]["media_type"] == "image/jpeg"
            base64.b64decode(block["source"]["data"])  # valid base64
        assert "missing person" in content[2]["text"]

    def test_default_model_and_effort_come_from_config(self, make_agent, img):
        agent, client = make_agent()
        agent.match_description(img, "red jacket")

        kwargs = client.beta.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-opus-5-5"
        assert kwargs["output_config"] == {"effort": "medium"}
        assert "thinking" not in kwargs  # always-on adaptive; must not be sent
        assert "temperature" not in kwargs

    def test_model_and_effort_overridable(self, make_agent, img):
        agent, client = make_agent(model="claude-sonnet-5-5", effort="low")
        agent.match_description(img, "red jacket")

        kwargs = client.beta.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-5-5"
        assert kwargs["output_config"] == {"effort": "low"}

    def test_refusal_fallback_enabled_for_supported_model(self, make_agent, img):
        agent, client = make_agent()
        agent.analyze_match(img, img)

        kwargs = client.beta.messages.create.call_args.kwargs
        assert kwargs["fallbacks"] == "default"
        assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]

    def test_no_fallback_param_for_unsupported_model(self, make_agent, img):
        agent, client = make_agent(model="claude-haiku-5-5")
        agent.analyze_match(img, img)

        kwargs = client.beta.messages.create.call_args.kwargs
        assert "fallbacks" not in kwargs
        assert "betas" not in kwargs


class TestResults:
    def test_parses_structured_match_reply(self, make_agent, img):
        agent, _ = make_agent()
        result = agent.analyze_match(img, img)
        assert result["match"] is True
        assert result["confidence"] == "high"
        assert result["reasoning"] == "same red jacket"  # original case preserved

    def test_no_match_reply(self, make_agent, img):
        agent, _ = make_agent(response=_response("MATCH: no\nCONFIDENCE: low\nREASONING: different hair"))
        assert agent.analyze_match(img, img)["match"] is False

    def test_refusal_becomes_error_result_not_exception(self, make_agent, img):
        refusal = _response(text=None, stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
        agent, _ = make_agent(response=refusal)

        result = agent.analyze_match(img, img)

        assert result["match"] is False
        assert result["confidence"] == "error"
        assert "declined" in result["reasoning"]

    def test_empty_reply_becomes_error_result(self, make_agent, img):
        agent, _ = make_agent(response=_response(text=None, stop_reason="max_tokens"))
        result = agent.match_description(img, "red jacket")
        assert result["confidence"] == "error"
        assert "max_tokens" in result["reasoning"]

    def test_api_exception_becomes_error_result(self, make_agent, img):
        agent, client = make_agent()
        client.beta.messages.create.side_effect = RuntimeError("boom")
        result = agent.analyze_match(img, img)
        assert result == {"match": False, "confidence": "error", "reasoning": "boom"}

    def test_describe_person_returns_text(self, make_agent, img):
        agent, _ = make_agent(response=_response("A child in a red jacket."))
        assert agent.describe_person(img) == "A child in a red jacket."


class TestConfigEnv:
    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_REASONING_PROVIDER", "gemma")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MODEL", "claude-sonnet-5-5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_EFFORT", "high")
        cfg = AmberConfig.from_env()
        assert cfg.reasoning.provider == "gemma"
        assert cfg.reasoning.claude_model == "claude-sonnet-5-5"
        assert cfg.reasoning.claude_effort == "high"


class TestRuntimeSwitch:
    """The set_reasoning_provider socket event."""

    @pytest.fixture
    def sio(self, clean_app_state):
        from flightrisk.dashboard.app import app, socketio
        return socketio.test_client(app)

    @staticmethod
    def _last(sio):
        events = [e for e in sio.get_received() if e["name"] == "reasoning_provider"]
        assert events, "no reasoning_provider event received"
        return events[-1]["args"][0]

    def test_switch_to_off(self, sio):
        from flightrisk.dashboard.app import _state
        _state["reasoning"] = MagicMock(provider="claude", label="Claude", model="m")
        sio.get_received()

        sio.emit("set_reasoning_provider", {"provider": "off"})

        assert self._last(sio)["provider"] == "off"
        assert _state["reasoning"] is None

    def test_switch_to_claude_installs_agent_and_worker(self, sio, monkeypatch):
        from flightrisk.dashboard import app as app_mod
        agent = MagicMock(provider="claude", label="Claude", model="claude-opus-5-5")
        sio.get_received()

        with patch("flightrisk.reasoning.create_reasoning_agent", return_value=agent), \
                patch.object(app_mod, "_ensure_reasoning_worker") as ensure:
            sio.emit("set_reasoning_provider", {"provider": "claude"})

        state = self._last(sio)
        assert (state["provider"], state["label"]) == ("claude", "Claude")
        assert app_mod._state["reasoning"] is agent
        ensure.assert_called_once()

    def test_failed_switch_keeps_current_backend_and_reports_error(self, sio):
        from flightrisk.dashboard.app import _state
        current = MagicMock(provider="gemma", label="Gemma", model="gemma4:latest")
        _state["reasoning"] = current
        sio.get_received()

        with patch("flightrisk.reasoning.create_reasoning_agent",
                   side_effect=RuntimeError("CLAUDE_API_KEY is not set.")):
            sio.emit("set_reasoning_provider", {"provider": "claude"})

        state = self._last(sio)
        assert state["provider"] == "gemma"
        assert "CLAUDE_API_KEY" in state["error"]
        assert _state["reasoning"] is current

    @pytest.mark.parametrize("payload", [{}, {"provider": ""}, {"provider": "gpt"}, None])
    def test_bad_payload_rejected(self, sio, payload):
        sio.get_received()
        sio.emit("set_reasoning_provider", payload)
        assert "error" in self._last(sio)

    def test_status_and_health_expose_provider(self, clean_app_state):
        from flightrisk.dashboard.app import app, _state
        _state["reasoning"] = MagicMock(provider="claude", label="Claude", model="m")
        client = app.test_client()
        assert client.get("/api/status").get_json()["reasoning_provider"] == "claude"
        assert client.get("/api/health").get_json()["reasoning_provider"] == "claude"
