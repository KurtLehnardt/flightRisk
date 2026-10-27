"""Claude reasoning agent via the Anthropic API.

Drop-in alternative to the Ollama/Gemma ``FlightRiskAgent``: same methods
(``analyze_match``, ``match_description``, ...), only the model call differs.

The API key is read from ``CLAUDE_API_KEY`` (falling back to the SDK's usual
``ANTHROPIC_API_KEY``). It is never logged or included in error results.
"""

import os
from typing import Any

from flightrisk.config import get_config
from flightrisk.reasoning.agent import FlightRiskAgent

# Models that accept the server-side refusal fallback (Claude API only).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Thinking tokens count toward max_tokens, so leave headroom beyond the
# three-line MATCH / CONFIDENCE / REASONING reply.
_MAX_TOKENS = 4096


def _resolve_api_key() -> str | None:
    return os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")


class ClaudeReasoningAgent(FlightRiskAgent):
    """Person-matching reasoning backed by Claude (vision)."""

    provider = "claude"
    label = "Claude"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        effort: str | None = None,
    ):
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("pip install anthropic") from e

        key = api_key or _resolve_api_key()
        if not key:
            raise RuntimeError(
                "CLAUDE_API_KEY is not set. Add it to your environment "
                "(or set FLIGHTRISK_REASONING_PROVIDER=gemma to use local Ollama)."
            )

        cfg = get_config().reasoning
        self.model = model or cfg.claude_model
        self.effort = effort or cfg.claude_effort
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=2)
        self._available = True
        print(f"[reasoning] Claude ({self.model}) ready")

    def _ask(self, prompt: str, images_b64: list[str]) -> str:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            }
            for b64 in images_b64
        ]
        content.append({"type": "text", "text": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": _MAX_TOKENS,
            "output_config": {"effort": self.effort},
            "messages": [{"role": "user", "content": content}],
        }
        if self.model in _FALLBACK_MODELS:
            # A classifier decline is re-run server-side on a fallback model
            # instead of surfacing as a failed match check.
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        response = self._client.beta.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise RuntimeError(f"Claude declined this request ({category or 'policy'})")

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise RuntimeError(f"Claude returned no text (stop_reason={response.stop_reason})")
        return text
