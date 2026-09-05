"""Tests for flightrisk.reasoning.agent.FlightRiskAgent — Gemma 4 reasoning via Ollama.

All Ollama I/O is mocked (`ollama.Client` is patched at construction time),
so this suite never contacts a real Ollama server and does not require one
to be running.
"""

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from flightrisk.reasoning.agent import FlightRiskAgent


def _mock_client(model_names=("gemma4:latest",), chat_response_text="MATCH: yes\nCONFIDENCE: high\nREASONING: same jacket"):
    """Build a MagicMock standing in for an ollama.Client() instance."""
    client = MagicMock()
    client.list.return_value = SimpleNamespace(
        models=[SimpleNamespace(model=name) for name in model_names]
    )
    chat_resp = MagicMock()
    chat_resp.message.content = chat_response_text
    client.chat.return_value = chat_resp
    return client


@pytest.fixture
def make_agent(monkeypatch):
    """Factory fixture: build an FlightRiskAgent with a mocked ollama.Client.

    Returns (agent, mock_client) so tests can assert on calls made to the
    underlying (fake) Ollama client.
    """
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    def _make(model="gemma4:latest", model_names=("gemma4:latest",), chat_response_text=None):
        client = _mock_client(
            model_names=model_names,
            chat_response_text=chat_response_text
            or "MATCH: yes\nCONFIDENCE: high\nREASONING: same jacket",
        )
        with patch("flightrisk.reasoning.agent.ollama.Client", return_value=client):
            agent = FlightRiskAgent(model=model)
        return agent, client

    return _make


@pytest.fixture
def sample_image():
    return np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8)


class TestCheckModel:
    def test_returns_true_when_model_in_list(self, make_agent):
        agent, _client = make_agent(
            model="gemma4:latest", model_names=("gemma4:latest", "llama3:8b")
        )
        assert agent._available is True
        assert agent._check_model() is True

    def test_returns_false_when_model_missing(self, make_agent):
        agent, _client = make_agent(model="gemma4:latest", model_names=("llama3:8b",))
        assert agent._available is False
        assert agent._check_model() is False

    def test_returns_false_when_ollama_unreachable(self, make_agent):
        agent, client = make_agent(model="gemma4:latest")
        client.list.side_effect = ConnectionError("no ollama server running")
        assert agent._check_model() is False


class TestOllamaUnavailable:
    """Error handling when the `ollama` package/server is unavailable."""

    def test_init_raises_when_ollama_package_missing(self, monkeypatch):
        monkeypatch.setattr("flightrisk.reasoning.agent.HAS_OLLAMA", False)
        with pytest.raises(RuntimeError):
            FlightRiskAgent()

    def test_methods_degrade_gracefully_when_model_unavailable(self, make_agent, sample_image):
        agent, client = make_agent(model_names=("some-other-model",))
        assert agent._available is False

        assert agent.analyze_match(sample_image, sample_image) == {
            "match": False,
            "confidence": "unavailable",
            "reasoning": "Model not loaded",
        }
        assert agent.describe_person(sample_image) == "Model not available"
        assert agent.match_description(sample_image, "blue jacket") == {
            "match": False,
            "confidence": "unavailable",
            "reasoning": "Model not loaded",
        }
        client.chat.assert_not_called()


class TestImageToBase64:
    def test_produces_valid_base64_jpeg(self, make_agent, sample_image):
        agent, _client = make_agent()
        encoded = agent._image_to_base64(sample_image)

        assert isinstance(encoded, str)
        raw = base64.b64decode(encoded, validate=True)
        nparr = np.frombuffer(raw, np.uint8)
        decoded = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        assert decoded is not None
        assert decoded.shape == sample_image.shape


class TestAnalyzeMatch:
    def test_uses_correct_prompt_and_parses_response(self, make_agent, sample_image):
        agent, client = make_agent(
            chat_response_text="MATCH: yes\nCONFIDENCE: high\nREASONING: same red jacket"
        )
        reference = sample_image
        candidate = sample_image

        result = agent.analyze_match(reference, candidate)

        client.chat.assert_called_once()
        _, kwargs = client.chat.call_args
        assert kwargs["model"] == agent.model
        messages = kwargs["messages"]
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "user"
        assert "MATCH: yes or no" in msg["content"]
        assert "CONFIDENCE: high, medium, or low" in msg["content"]
        assert msg["images"] == [
            agent._image_to_base64(reference),
            agent._image_to_base64(candidate),
        ]

        assert result == {"match": True, "confidence": "high", "reasoning": "SAME RED JACKET"}

    def test_malformed_response_defaults_gracefully(self, make_agent, sample_image):
        text = "I think this might be the same person, hard to tell."
        agent, _client = make_agent(chat_response_text=text)

        result = agent.analyze_match(sample_image, sample_image)

        assert result == {"match": False, "confidence": "unknown", "reasoning": text}

    def test_chat_exception_returns_error_dict(self, make_agent, sample_image):
        agent, client = make_agent()
        client.chat.side_effect = RuntimeError("connection refused")

        result = agent.analyze_match(sample_image, sample_image)

        assert result["match"] is False
        assert result["confidence"] == "error"
        assert "connection refused" in result["reasoning"]


class TestDescribePerson:
    def test_mocked_response(self, make_agent, sample_image):
        agent, client = make_agent(
            chat_response_text="A child wearing a blue jacket and red backpack."
        )
        result = agent.describe_person(sample_image)

        assert result == "A child wearing a blue jacket and red backpack."
        _, kwargs = client.chat.call_args
        assert kwargs["messages"][0]["images"] == [agent._image_to_base64(sample_image)]

    def test_chat_exception_returns_error_string(self, make_agent, sample_image):
        agent, client = make_agent()
        client.chat.side_effect = RuntimeError("boom")

        result = agent.describe_person(sample_image)

        assert result.startswith("Error:")
        assert "boom" in result


class TestAnalyzeScene:
    def test_mocked_response(self, make_agent, sample_image):
        agent, client = make_agent(
            chat_response_text="A wooded park with three people visible, no obstacles."
        )
        result = agent.analyze_scene(sample_image)

        assert result == "A wooded park with three people visible, no obstacles."
        _, kwargs = client.chat.call_args
        assert kwargs["messages"][0]["images"] == [agent._image_to_base64(sample_image)]

    def test_unavailable_returns_placeholder(self, make_agent, sample_image):
        agent, client = make_agent(model_names=("other-model",))
        result = agent.analyze_scene(sample_image)
        assert result == "Model not available"
        client.chat.assert_not_called()

    def test_chat_exception_returns_error_string(self, make_agent, sample_image):
        agent, client = make_agent()
        client.chat.side_effect = RuntimeError("scene analysis failed")

        result = agent.analyze_scene(sample_image)

        assert result.startswith("Error:")
        assert "scene analysis failed" in result


class TestMatchDescription:
    def test_mocked_response(self, make_agent, sample_image):
        agent, client = make_agent(
            chat_response_text="MATCH: no\nCONFIDENCE: low\nREASONING: different hair color"
        )
        result = agent.match_description(sample_image, "blonde hair, blue jacket")

        assert result == {"match": False, "confidence": "low", "reasoning": "DIFFERENT HAIR COLOR"}
        _, kwargs = client.chat.call_args
        assert "blonde hair, blue jacket" in kwargs["messages"][0]["content"]
        assert kwargs["messages"][0]["images"] == [agent._image_to_base64(sample_image)]

    def test_unavailable_short_circuits(self, make_agent, sample_image):
        agent, client = make_agent(model_names=("other-model",))
        result = agent.match_description(sample_image, "description")
        assert result == {"match": False, "confidence": "unavailable", "reasoning": "Model not loaded"}
        client.chat.assert_not_called()

    def test_chat_exception_returns_error_dict(self, make_agent, sample_image):
        agent, client = make_agent()
        client.chat.side_effect = ValueError("bad request")

        result = agent.match_description(sample_image, "desc")

        assert result["match"] is False
        assert result["confidence"] == "error"
        assert "bad request" in result["reasoning"]


class TestParseMatchResponse:
    def test_exact_format(self, make_agent):
        agent, _client = make_agent()
        text = "MATCH: yes\nCONFIDENCE: high\nREASONING: matching backpack"

        result = agent._parse_match_response(text)

        assert result == {"match": True, "confidence": "high", "reasoning": "MATCHING BACKPACK"}

    def test_extra_whitespace_is_tolerated(self, make_agent):
        agent, _client = make_agent()
        text = "  MATCH:   YES  \n  CONFIDENCE:  Medium \n  REASONING:   Same backpack visible  "

        result = agent._parse_match_response(text)

        assert result["match"] is True
        assert result["confidence"] == "medium"
        assert result["reasoning"] == "SAME BACKPACK VISIBLE"

    def test_missing_fields_fall_back_to_defaults(self, make_agent):
        agent, _client = make_agent()
        text = "MATCH: no"

        result = agent._parse_match_response(text)

        # No CONFIDENCE/REASONING lines present -> defaults are kept, and
        # "reasoning" falls back to the full original (un-uppercased) text.
        assert result == {"match": False, "confidence": "unknown", "reasoning": "MATCH: no"}

    def test_no_recognized_fields_returns_full_text_as_reasoning(self, make_agent):
        agent, _client = make_agent()
        text = "The two crops look similar but I can't be certain."

        result = agent._parse_match_response(text)

        assert result == {"match": False, "confidence": "unknown", "reasoning": text}

    def test_match_no_is_recognized(self, make_agent):
        agent, _client = make_agent()
        result = agent._parse_match_response("MATCH: no\nCONFIDENCE: low\nREASONING: no")
        assert result["match"] is False
