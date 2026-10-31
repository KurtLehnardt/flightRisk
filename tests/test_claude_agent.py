"""Tests for the Claude reasoning backend and the provider switch.

The Anthropic client is always mocked: no test here makes a network call or
needs a real API key.

Claude is a cloud backend and is opt-in (PR #88 review, finding B): the local
Gemma backend is the default, a stray ANTHROPIC_API_KEY never enables uploads,
and Claude is refused unless FLIGHTRISK_ALLOW_CLOUD_LLM=1 is set.
"""

import base64
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from flightrisk.config import FlightRiskConfig, reset_config
from flightrisk.reasoning import (
    PROVIDERS,
    cloud_llm_allowed,
    create_reasoning_agent,
    normalize_provider,
    provider_unavailable_reason,
)
from flightrisk.reasoning.claude_agent import ClaudeReasoningAgent

FAKE_KEY = "sk-ant-test-not-a-real-key"
MATCH_TEXT = "MATCH: yes\nCONFIDENCE: high\nREASONING: same red jacket"


@contextmanager
def fake_anthropic():
    """Stand in for the anthropic SDK, installed or not; yields the client class mock."""
    module = MagicMock(name="anthropic")
    with patch.dict(sys.modules, {"anthropic": module}):
        yield module.Anthropic


def _response(text=MATCH_TEXT, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        content=[SimpleNamespace(type="text", text=text)] if text is not None else [],
    )


_ENV_VARS = (
    "FLIGHTRISK_ALLOW_CLOUD_LLM",
    "FLIGHTRISK_CLAUDE_API_KEY",
    "FLIGHTRISK_REASONING_PROVIDER",
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_reasoning_env(monkeypatch):
    """Every test starts with no cloud opt-in, no keys and a fresh config."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reset_config()
    yield
    reset_config()


@pytest.fixture
def allow_cloud(monkeypatch):
    """Opt in to cloud reasoning, as an operator would with the env var."""
    monkeypatch.setenv("FLIGHTRISK_ALLOW_CLOUD_LLM", "1")
    reset_config()


def _ollama_with_gemma():
    client = MagicMock()
    client.list.return_value = SimpleNamespace(models=[SimpleNamespace(model="gemma4:latest")])
    return client


@pytest.fixture
def make_agent(allow_cloud):
    """Build a ClaudeReasoningAgent whose Anthropic client is a MagicMock."""

    def _make(response=None, **kwargs):
        with fake_anthropic() as mock_cls:
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
        cfg = FlightRiskConfig()
        cfg.reasoning.provider = "gemma"
        with patch("flightrisk.reasoning.get_config", return_value=cfg):
            assert normalize_provider(None) == "gemma"

    def test_local_gemma_is_the_shipped_default_and_cloud_is_off(self):
        assert FlightRiskConfig().reasoning.provider == "gemma"
        assert FlightRiskConfig().reasoning.allow_cloud is False
        assert PROVIDERS == ("claude", "gemma", "off")


class TestCloudOptIn:
    """Person crops and the reference photo must not go to Anthropic without opt-in."""

    def test_default_provider_is_local_with_stray_anthropic_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stray-key")
        monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-stray-key")
        reset_config()

        assert normalize_provider(None) == "gemma"
        assert not cloud_llm_allowed()
        with patch("flightrisk.reasoning.agent.ollama.Client", return_value=_ollama_with_gemma()), \
                fake_anthropic() as anthropic_cls:
            agent = create_reasoning_agent()
        assert agent.provider == "gemma"
        anthropic_cls.assert_not_called()

    def test_claude_refused_without_opt_in_even_with_key(self, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
        with fake_anthropic() as anthropic_cls:
            with pytest.raises(RuntimeError, match="FLIGHTRISK_ALLOW_CLOUD_LLM=1"):
                create_reasoning_agent("claude")
            with pytest.raises(RuntimeError, match="FLIGHTRISK_ALLOW_CLOUD_LLM=1"):
                ClaudeReasoningAgent(api_key=FAKE_KEY)
        anthropic_cls.assert_not_called()

    def test_configured_claude_provider_still_refused_without_opt_in(self, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_REASONING_PROVIDER", "claude")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        reset_config()
        with fake_anthropic() as anthropic_cls, pytest.raises(RuntimeError, match="not enabled"):
            create_reasoning_agent()
        anthropic_cls.assert_not_called()

    @pytest.mark.parametrize("value", ["0", "false", "no", ""])
    def test_opt_in_must_be_truthy(self, monkeypatch, value):
        monkeypatch.setenv("FLIGHTRISK_ALLOW_CLOUD_LLM", value)
        reset_config()
        assert not cloud_llm_allowed()

    def test_claude_allowed_with_opt_in_and_key(self, monkeypatch, allow_cloud):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        with fake_anthropic() as anthropic_cls:
            agent = create_reasoning_agent("claude")
        assert isinstance(agent, ClaudeReasoningAgent)
        assert anthropic_cls.call_args.kwargs["api_key"] == FAKE_KEY

    def test_unavailable_reason_reported_for_selector(self, monkeypatch):
        assert "FLIGHTRISK_ALLOW_CLOUD_LLM" in provider_unavailable_reason("claude")
        assert provider_unavailable_reason("gemma") is None
        assert provider_unavailable_reason("off") is None

    def test_unavailable_reason_names_missing_key_after_opt_in(self, allow_cloud):
        assert provider_unavailable_reason("claude") == "FLIGHTRISK_CLAUDE_API_KEY is not set."

    def test_available_after_opt_in_and_key(self, monkeypatch, allow_cloud):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        assert provider_unavailable_reason("claude") is None


class TestCreateReasoningAgent:
    def test_off_returns_none(self):
        assert create_reasoning_agent("off") is None

    def test_claude_without_key_raises_clear_error(self, allow_cloud):
        with pytest.raises(RuntimeError, match="FLIGHTRISK_CLAUDE_API_KEY is not set"):
            create_reasoning_agent("claude")

    def test_claude_with_key_builds_agent(self, monkeypatch, allow_cloud):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        with fake_anthropic():
            agent = create_reasoning_agent("claude")
        assert isinstance(agent, ClaudeReasoningAgent)
        assert (agent.provider, agent.label) == ("claude", "Claude")

    def test_gemma_unavailable_raises_instead_of_returning_dead_agent(self):
        """A Gemma agent whose model isn't pulled used to be returned anyway,
        leaving every match stuck on 'Awaiting Gemma reasoning...'."""
        client = MagicMock()
        client.list.return_value = SimpleNamespace(models=[])
        with patch("flightrisk.reasoning.agent.ollama.Client", return_value=client), \
                pytest.raises(RuntimeError, match="not available in Ollama"):
            create_reasoning_agent("gemma")

    def test_gemma_available_returns_agent(self):
        with patch("flightrisk.reasoning.agent.ollama.Client", return_value=_ollama_with_gemma()):
            agent = create_reasoning_agent("gemma")
        assert (agent.provider, agent.label) == ("gemma", "Gemma")


class TestApiKeyResolution:
    def test_flightrisk_key_is_used(self, monkeypatch, allow_cloud):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", "sk-ant-from-flightrisk-var")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-anthropic-var")
        with fake_anthropic() as mock_cls:
            ClaudeReasoningAgent()
        assert mock_cls.call_args.kwargs["api_key"] == "sk-ant-from-flightrisk-var"

    @pytest.mark.parametrize("stray_var", ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"])
    def test_generic_key_variables_are_ignored(self, monkeypatch, allow_cloud, stray_var):
        monkeypatch.setenv(stray_var, "sk-ant-stray")
        with fake_anthropic() as mock_cls, \
                pytest.raises(RuntimeError, match="FLIGHTRISK_CLAUDE_API_KEY is not set"):
            ClaudeReasoningAgent()
        mock_cls.assert_not_called()

    def test_key_never_appears_in_missing_key_error(self, monkeypatch, allow_cloud):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stray")
        with pytest.raises(RuntimeError) as exc:
            ClaudeReasoningAgent()
        assert "sk-ant" not in str(exc.value)

    def test_startup_log_names_key_variable_not_key(self, monkeypatch, allow_cloud, capsys):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        with fake_anthropic():
            ClaudeReasoningAgent()
        out = capsys.readouterr().out
        assert "FLIGHTRISK_CLAUDE_API_KEY" in out
        assert "Cloud reasoning is ON" in out
        assert FAKE_KEY not in out


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
        monkeypatch.setenv("FLIGHTRISK_REASONING_PROVIDER", "claude")
        monkeypatch.setenv("FLIGHTRISK_ALLOW_CLOUD_LLM", "1")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_MODEL", "claude-sonnet-5-5")
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_EFFORT", "high")
        cfg = FlightRiskConfig.from_env()
        assert cfg.reasoning.provider == "claude"
        assert cfg.reasoning.allow_cloud is True
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

    def test_switch_to_claude_refused_without_opt_in(self, sio, monkeypatch):
        from flightrisk.dashboard.app import _state
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        current = MagicMock(provider="gemma", label="Gemma", model="gemma4:latest")
        _state["reasoning"] = current
        sio.get_received()

        with patch("flightrisk.reasoning.create_reasoning_agent") as create, \
                fake_anthropic() as anthropic_cls:
            sio.emit("set_reasoning_provider", {"provider": "claude"})

        state = self._last(sio)
        assert state["provider"] == "gemma"
        assert "FLIGHTRISK_ALLOW_CLOUD_LLM=1" in state["error"]
        assert _state["reasoning"] is current
        create.assert_not_called()
        anthropic_cls.assert_not_called()

    def test_switch_to_claude_with_opt_in_uses_real_factory(self, sio, monkeypatch, allow_cloud):
        from flightrisk.dashboard import app as app_mod
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        sio.get_received()

        with fake_anthropic(), \
                patch.object(app_mod, "_ensure_reasoning_worker") as ensure:
            sio.emit("set_reasoning_provider", {"provider": "claude"})

        state = self._last(sio)
        assert "error" not in state
        assert state["provider"] == "claude"
        assert isinstance(app_mod._state["reasoning"], ClaudeReasoningAgent)
        ensure.assert_called_once()

    def test_state_marks_claude_unavailable_with_reason(self, sio):
        sio.get_received()
        sio.emit("get_reasoning_provider")
        state = self._last(sio)
        assert "claude" not in state["available"]
        assert "FLIGHTRISK_ALLOW_CLOUD_LLM" in state["unavailable"]["claude"]
        assert state["cloud_allowed"] is False
        assert {"gemma", "off"} <= set(state["available"])

    def test_state_marks_claude_available_after_opt_in(self, sio, monkeypatch, allow_cloud):
        monkeypatch.setenv("FLIGHTRISK_CLAUDE_API_KEY", FAKE_KEY)
        sio.get_received()
        sio.emit("get_reasoning_provider")
        state = self._last(sio)
        assert "claude" in state["available"]
        assert state["unavailable"] == {}
        assert state["cloud_allowed"] is True

    def test_unauthenticated_client_cannot_switch(self, clean_app_state, monkeypatch, allow_cloud):
        """The switch follows the SocketIO connect-time API-key gate."""
        from flightrisk.dashboard import app as app_mod
        monkeypatch.setattr(app_mod, "_FLIGHTRISK_API_KEY", "dashboard-secret")
        client = app_mod.socketio.test_client(app_mod.app)
        assert not client.is_connected()

    def test_switch_to_claude_installs_agent_and_worker(self, sio, monkeypatch, allow_cloud):
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

    def test_failed_switch_keeps_current_backend_and_reports_error(self, sio, allow_cloud):
        from flightrisk.dashboard.app import _state
        current = MagicMock(provider="gemma", label="Gemma", model="gemma4:latest")
        _state["reasoning"] = current
        sio.get_received()

        with patch("flightrisk.reasoning.create_reasoning_agent",
                   side_effect=RuntimeError("FLIGHTRISK_CLAUDE_API_KEY is not set.")):
            sio.emit("set_reasoning_provider", {"provider": "claude"})

        state = self._last(sio)
        assert state["provider"] == "gemma"
        assert "FLIGHTRISK_CLAUDE_API_KEY" in state["error"]
        assert _state["reasoning"] is current

    @pytest.mark.parametrize("payload", [{}, {"provider": ""}, {"provider": "gpt"}, None])
    def test_bad_payload_rejected(self, sio, payload):
        sio.get_received()
        sio.emit("set_reasoning_provider", payload)
        assert "error" in self._last(sio)

    def test_status_and_health_expose_provider(self, clean_app_state):
        from flightrisk.dashboard.app import _state, app
        _state["reasoning"] = MagicMock(provider="claude", label="Claude", model="m")
        client = app.test_client()
        assert client.get("/api/status").get_json()["reasoning_provider"] == "claude"
        assert client.get("/api/health").get_json()["reasoning_provider"] == "claude"
