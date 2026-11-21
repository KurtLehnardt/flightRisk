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

import logging
import os
import threading
import time
from typing import Any

import cv2
import numpy as np

from flightrisk.config import get_config
from flightrisk.obs_log import notice
from flightrisk.reasoning.agent import FlightRiskAgent

_log = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback (Claude API only).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Thinking tokens count toward max_tokens. The visible reply is three short
# lines (well under 200 tokens), so the rest is headroom for adaptive thinking
# at high effort: a truncated thinking block would leave no text at all.
_MAX_TOKENS = 8192

# Short timeout and a single retry keep the single serial reasoning worker from
# stalling for minutes on a sick API (worst case about 2 x 15 s per call).
_TIMEOUT_S = 15.0
_MAX_RETRIES = 1

# Long-edge cap for images sent to the cloud. Smaller uploads are faster and
# cheaper and also limit how much of the reference photo leaves the machine.
_MAX_IMAGE_EDGE = 768

_RATE_WINDOW_S = 60.0
RATE_CAP_MESSAGE = "skipped: rate cap"
BREAKER_OPEN_MESSAGE = "skipped: Claude circuit breaker open"


class ClaudeCallSkipped(RuntimeError):
    """A Claude call was skipped locally (breaker open or rate cap); nothing was sent."""


def _is_outage_error(exc: Exception) -> bool:
    """True for failures that mean the API is unhealthy: timeout, connection, 429, 5xx."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    names = {c.__name__ for c in type(exc).__mro__}
    return bool(names & {"APITimeoutError", "APIConnectionError", "TimeoutError"})


def downscale_for_upload(image: np.ndarray, max_edge: int = _MAX_IMAGE_EDGE) -> np.ndarray:
    """Shrink ``image`` so its long edge is at most ``max_edge``; never upscales."""
    h, w = image.shape[:2]
    long_edge = max(h, w)
    if long_edge <= max_edge:
        return image
    scale = max_edge / long_edge
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


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
        self._client = anthropic.Anthropic(
            api_key=key, timeout=_TIMEOUT_S, max_retries=_MAX_RETRIES)
        self._breaker_threshold = max(1, int(cfg.claude_breaker_threshold))
        self._breaker_cooloff = float(cfg.claude_breaker_cooloff)
        self._max_calls_per_minute = int(cfg.claude_max_calls_per_minute)
        self._guard = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._call_times: list[float] = []
        self._available = True
        self.last_usage: dict[str, Any] = {}
        # Say where the key came from (never the key itself) and that data
        # leaves the device, so the operator can see cloud use is active.
        notice(
            _log, "reasoning_ready",
            f"[reasoning] Claude ({self.model}) ready; API key from {key_source}. "
            "Cloud reasoning is ON: person crops and the reference photo are sent to Anthropic.",
            provider=self.provider, model=self.model, key_source=key_source, cloud=True,
        )

    def _image_to_base64(self, image: np.ndarray) -> str:
        """Downscale to the upload cap, then encode as base64 JPEG."""
        return super()._image_to_base64(downscale_for_upload(image))

    def _admit(self) -> None:
        """Raise ClaudeCallSkipped if the breaker is open or the minute cap is hit."""
        now = time.monotonic()
        with self._guard:
            if now < self._open_until:
                raise ClaudeCallSkipped(BREAKER_OPEN_MESSAGE)
            self._call_times = [t for t in self._call_times if now - t < _RATE_WINDOW_S]
            if len(self._call_times) >= self._max_calls_per_minute:
                raise ClaudeCallSkipped(RATE_CAP_MESSAGE)
            self._call_times.append(now)

    def _record_result(self, exc: Exception | None) -> None:
        """Update the breaker: success resets it; an outage error counts toward opening it."""
        with self._guard:
            if exc is None:
                self._consecutive_failures = 0
                self._open_until = 0.0
            elif _is_outage_error(exc):
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._breaker_threshold:
                    self._open_until = time.monotonic() + self._breaker_cooloff
                    _log.warning(
                        "claude_breaker_open failures=%d cooloff_s=%.0f",
                        self._consecutive_failures, self._breaker_cooloff)

    def _ask(self, prompt: str, images_b64: list[str]) -> str:
        self._admit()
        try:
            text = self._send(prompt, images_b64)
        except Exception as e:
            self._record_result(e)
            raise
        self._record_result(None)
        return text

    def _send(self, prompt: str, images_b64: list[str]) -> str:
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
        usage = getattr(response, "usage", None)
        self.last_usage = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise RuntimeError(f"Claude declined this request ({category or 'policy'})")

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise RuntimeError(f"Claude returned no text (stop_reason={response.stop_reason})")
        return text
