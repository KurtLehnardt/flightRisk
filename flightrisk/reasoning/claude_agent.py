"""Claude reasoning agent via the Anthropic API.

Drop-in alternative to the Ollama/Gemma ``FlightRiskAgent``: same methods
(``analyze_match``, ``match_description``, ...), only the model call differs.

Cloud use is opt-in: the agent refuses to start unless
``FLIGHTRISK_ALLOW_CLOUD_LLM=1`` is set, because every call sends the detected
person's crop and the missing child's reference photo to Anthropic.

The API key is read only from ``FLIGHTRISK_CLAUDE_API_KEY``. The SDK's usual
``ANTHROPIC_API_KEY`` is deliberately ignored so a key exported for other tools
never enables uploads by accident. The key is never logged or included in
error results.
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


API_KEY_ENV = "FLIGHTRISK_CLAUDE_API_KEY"


def resolve_api_key() -> str | None:
    """The Claude API key from ``FLIGHTRISK_CLAUDE_API_KEY`` (empty counts as unset)."""
    return os.environ.get(API_KEY_ENV) or None


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
        from flightrisk.reasoning import cloud_block_reason

        blocked = cloud_block_reason(self.provider)
        if blocked:
            raise RuntimeError(blocked)

        key = api_key or resolve_api_key()
        if not key:
            raise RuntimeError(
                f"{API_KEY_ENV} is not set. Add it to your environment "
                "(ANTHROPIC_API_KEY is not used), or set "
                "FLIGHTRISK_REASONING_PROVIDER=gemma to use local Ollama."
            )
        key_source = "api_key argument" if api_key else API_KEY_ENV

        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("pip install anthropic") from e

        cfg = get_config().reasoning
        self.model = model or cfg.claude_model
        self.effort = effort or cfg.claude_effort
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=2)
        self._available = True
        # Say where the key came from (never the key itself) and that data
        # leaves the device, so the operator can see cloud use is active.
        print(
            f"[reasoning] Claude ({self.model}) ready; API key from {key_source}. "
            "Cloud reasoning is ON: person crops and the reference photo are sent to Anthropic."
        )

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
