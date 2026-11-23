"""Gemma 4 reasoning agent via Ollama.

Provides higher-level reasoning about detected persons — called
periodically on cropped detections, NOT every frame.

This is also the base class for other reasoning backends (see
``claude_agent.ClaudeReasoningAgent``): subclasses only override ``_ask``.
Use ``flightrisk.reasoning.create_reasoning_agent`` to pick one by config.

Usage:
    1. Install Ollama: https://ollama.com
    2. Pull Gemma 4: `ollama pull gemma4`
    3. Start Ollama (runs automatically on macOS)
"""

import base64
import logging
import os
import re
from typing import Any

import cv2
import numpy as np

from flightrisk.config import get_config
from flightrisk.obs_log import notice
from flightrisk.obs_redact import redact_text

_log = logging.getLogger(__name__)

try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False


# Operator texts for results that carry no verdict. They are built only from
# these fixed templates, the backend's label, status codes, short codes and
# integers, never from an exception's message: results go to every dashboard
# client, phones included, and exception text can carry request details such
# as a URL, a header or a key. The full exception is logged server-side only,
# redacted (``_call_failed``).
PAUSED_TEXT = "AI check paused ({label} unavailable, retrying in {seconds} s)."
RATE_CAP_TEXT = "AI check skipped (rate cap)."
SKIPPED_TEXT = "AI check skipped."
KEY_REJECTED_TEXT = "AI check off ({label} rejected the API key)."
UNAVAILABLE_TEXT = "AI check off ({label} unavailable)."
FAILED_STATUS_TEXT = "AI check failed ({label} error {status})."
FAILED_TYPE_TEXT = "AI check failed ({label} error: {error_type})."
FAILED_TEXT = "AI check failed."

_TYPE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def safe_type_name(exc: BaseException) -> str:
    """The exception's class name if it is a plain identifier, else ``Exception``."""
    name = type(exc).__name__
    return name if _TYPE_NAME_RE.fullmatch(name) else "Exception"


class ReasoningCallSkipped(RuntimeError):
    """The backend chose not to send this call, for example while a cloud API is paused.

    Nothing was sent. ``reason`` is ``"paused"`` (with ``retry_in_s``) or
    ``"rate_cap"``; the result text is built from these fields and carries
    ``confidence="skipped"``, so a skip never reads as a failure.
    """

    def __init__(self, reason: str, retry_in_s: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_in_s = retry_in_s


class ReasoningUnavailable(RuntimeError):
    """The backend can no longer answer; ``reason`` is a short code such as ``"key_rejected"``.

    The result carries ``confidence="unavailable"`` and a text built from ``reason``.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FlightRiskAgent:
    """LLM-powered reasoning for person matching and scene analysis."""

    provider = "gemma"
    label = "Gemma"  # shown in the dashboard ("Awaiting Gemma reasoning...")
    #: Result text while ``_available`` is False.
    _unavailable_reason = "Model not loaded"
    #: ``(reference array, base64 JPEG)`` of the last read-only reference photo encoded.
    _ref_cache: tuple[np.ndarray, str] | None = None

    def __init__(self, model: str | None = None):
        """Initialize the reasoning agent.

        Args:
            model: Ollama model name. Options:
                   - 'gemma4:latest' — 12B, 9.6GB, good balance
                   - 'gemma4:e2b' — smallest with vision, 7.2GB
                   - 'gemma4:26b-mlx' — best quality, ~18GB, MLX optimized
                   Defaults to `config.reasoning.model`.
        """
        if not HAS_OLLAMA:
            raise RuntimeError("pip install ollama && brew install ollama")

        if model is None:
            model = get_config().reasoning.model
        self.model = model
        host = os.environ.get("OLLAMA_HOST")
        if host:
            self._client = ollama.Client(host=host)
        else:
            self._client = ollama.Client()
        self._available = self._check_model()
        #: Metadata of the last call (token counts, error type); never its text.
        self.last_usage: dict[str, Any] = {}
        if self._available:
            notice(_log, "reasoning_ready", f"[reasoning] Gemma 4 ({model}) ready", provider=self.provider, model=model)
        else:
            notice(_log, "reasoning_model_missing", f"[reasoning] Model {model} not found. Run: ollama pull {model}",
                   level=logging.WARNING, provider=self.provider, model=model)

    def _check_model(self) -> bool:
        """Check if the model is available in Ollama."""
        try:
            models = self._client.list()
            available = [m.model for m in models.models]
            return any(self.model in m for m in available)
        except Exception:
            return False

    def _image_to_base64(self, image: np.ndarray) -> str:
        """Convert a BGR numpy array to base64-encoded JPEG."""
        _, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return base64.b64encode(buffer).decode("utf-8")

    def _reference_b64(self, reference: np.ndarray) -> str:
        """``_image_to_base64(reference)``, reused while the same read-only photo comes back.

        The dashboard hands every job the same cached, read-only reference
        array (``load_reference_photo``), so it is resized and encoded once,
        not once per call. The cache holds the array itself, so identity can't
        be confused with a recycled ``id``; a writable array is never cached
        because it could change in place.
        """
        cached = self._ref_cache
        if cached is not None and cached[0] is reference:
            return cached[1]
        b64 = self._image_to_base64(reference)
        if not reference.flags.writeable:
            self._ref_cache = (reference, b64)
        return b64

    def _admit(self) -> None:
        """Hook run before any image is encoded; raise ReasoningCallSkipped to skip the call."""

    def _ask(self, prompt: str, images_b64: list[str]) -> str:
        """Send one prompt with JPEG images (base64) and return the reply text.

        The only backend-specific call; raises on failure. Subclasses override.
        """
        response = self._client.chat(
            model=self.model,
            messages=[{
                "role": "user",
                "content": prompt,
                "images": images_b64,
            }],
        )
        self.last_usage = {
            "input_tokens": getattr(response, "prompt_eval_count", None),
            "output_tokens": getattr(response, "eval_count", None),
        }
        return response.message.content.strip()

    def _call_failed(self, kind: str, exc: Exception) -> None:
        """Log a failed call server-side: type and redacted message, never the prompt.

        ``error_type`` is merged into ``last_usage`` so token counts the call
        already reported (a reply with no usable verdict) are kept.
        """
        usage = self.last_usage if isinstance(self.last_usage, dict) else {}
        self.last_usage = {**usage, "error_type": safe_type_name(exc)}
        _log.warning("reasoning_call_failed provider=%s kind=%s error_type=%s error=%s",
                     self.provider, kind, safe_type_name(exc), redact_text(exc))

    def _skip_text(self, exc: ReasoningCallSkipped) -> str:
        if exc.reason == "paused" and isinstance(exc.retry_in_s, int):
            return PAUSED_TEXT.format(label=self.label, seconds=max(0, exc.retry_in_s))
        if exc.reason == "rate_cap":
            return RATE_CAP_TEXT
        return SKIPPED_TEXT

    def _unavailable_text(self, exc: ReasoningUnavailable) -> str:
        if exc.reason == "key_rejected":
            return KEY_REJECTED_TEXT.format(label=self.label)
        return UNAVAILABLE_TEXT.format(label=self.label)

    def _error_text(self, exc: Exception) -> str:
        """Operator text for a failed call, from its status code or class name only.

        Backends may override this with a finer mapping; it must never return
        any part of ``str(exc)``.
        """
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return FAILED_STATUS_TEXT.format(label=self.label, status=status)
        return FAILED_TYPE_TEXT.format(label=self.label, error_type=safe_type_name(exc))

    def _failure_result(self, kind: str, exc: Exception) -> dict[str, Any]:
        """The result for a call that produced no verdict; it never carries the exception's message."""
        if isinstance(exc, ReasoningCallSkipped):
            # Expected, not a fault: INFO, and the reason is a fixed code.
            self.last_usage = {}
            _log.info("reasoning_call_skipped provider=%s kind=%s reason=%s",
                      self.provider, kind, redact_text(exc.reason, 64))
            return {"match": False, "confidence": "skipped", "reasoning": self._skip_text(exc)}
        self._call_failed(kind, exc)
        if isinstance(exc, ReasoningUnavailable):
            return {"match": False, "confidence": "unavailable", "reasoning": self._unavailable_text(exc)}
        try:
            text = self._error_text(exc)
        except Exception:  # noqa: BLE001 - the result must always be built
            text = FAILED_TEXT
        return {"match": False, "confidence": "error", "reasoning": text}

    def _unavailable_result(self) -> dict[str, Any]:
        return {"match": False, "confidence": "unavailable", "reasoning": self._unavailable_reason}

    def analyze_match(
        self, reference: np.ndarray, candidate: np.ndarray
    ) -> dict[str, Any]:
        """Ask Gemma 4 whether a detected person matches the reference.

        This is called on promising detections (above ReID threshold)
        for a second opinion with reasoning.

        Returns:
            dict with keys: match (bool), confidence (str), reasoning (str)
        """
        if not self._available:
            return self._unavailable_result()

        prompt = (
            "You are helping find a missing person. "
            "Image 1 is the reference photo of the person we are looking for. "
            "Image 2 is a person detected by a drone camera.\n\n"
            "Compare the two people. Consider: clothing color/type, hair, "
            "build, height, backpack/accessories, and any distinguishing features.\n\n"
            "Respond in this exact format:\n"
            "MATCH: yes or no\n"
            "CONFIDENCE: high, medium, or low\n"
            "REASONING: one sentence explaining why"
        )

        try:
            self.last_usage = {}
            # Admit first, so a skipped call never pays for resizing and encoding.
            self._admit()
            images = [self._reference_b64(reference), self._image_to_base64(candidate)]
            text = self._ask(prompt, images)
            return self._parse_match_response(text)
        except Exception as e:
            return self._failure_result("analyze", e)

    def describe_person(self, image: np.ndarray) -> str:
        """Generate a text description of a detected person."""
        if not self._available:
            return "Model not available"

        try:
            self.last_usage = {}
            self._admit()
            return self._ask(
                "Describe this person's appearance in one sentence. "
                "Include: clothing colors, hair, approximate age, "
                "and any visible accessories (backpack, hat, etc).",
                [self._image_to_base64(image)],
            )
        except Exception as e:
            return self._failure_result("describe_person", e)["reasoning"]

    def analyze_scene(self, image: np.ndarray) -> str:
        """Analyze the scene for search planning."""
        if not self._available:
            return "Model not available"

        try:
            self.last_usage = {}
            self._admit()
            return self._ask(
                "Briefly describe this aerial/drone view scene. "
                "Is it a park, field, forest, crowd, parking lot? "
                "How many people are visible? Any obstacles? "
                "One short paragraph.",
                [self._image_to_base64(image)],
            )
        except Exception as e:
            return self._failure_result("scene", e)["reasoning"]

    def match_description(
        self, image: np.ndarray, description: str
    ) -> dict[str, Any]:
        """Check if a detected person matches a text description.

        Used when no reference photo is available — the parent describes
        the child and Gemma 4 compares against each detection.
        """
        if not self._available:
            return self._unavailable_result()

        prompt = (
            "You are helping find a missing person. "
            f"The person's description: {description}\n\n"
            "Look at this image of a person detected by a drone camera. "
            "Does this person match the description above?\n\n"
            "Consider: clothing color/type, hair color/style, approximate age, "
            "build, backpack/accessories, and any distinguishing features.\n\n"
            "Respond in this exact format:\n"
            "MATCH: yes or no\n"
            "CONFIDENCE: high, medium, or low\n"
            "REASONING: one sentence explaining why"
        )

        try:
            self.last_usage = {}
            self._admit()
            text = self._ask(prompt, [self._image_to_base64(image)])
            return self._parse_match_response(text)
        except Exception as e:
            return self._failure_result("describe", e)

    def _parse_match_response(self, text: str) -> dict[str, Any]:
        """Parse the structured response from analyze_match."""
        result = {"match": False, "confidence": "unknown", "reasoning": text}

        for line in text.split("\n"):
            line = line.strip()
            upper = line.upper()  # match keys case-insensitively, keep the text's own case
            if upper.startswith("MATCH:"):
                result["match"] = "YES" in upper
            elif upper.startswith("CONFIDENCE:"):
                result["confidence"] = line.split(":", 1)[1].strip().lower()
            elif upper.startswith("REASONING:"):
                result["reasoning"] = line.split(":", 1)[1].strip()

        return result
