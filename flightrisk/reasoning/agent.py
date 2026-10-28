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
from typing import Any

import cv2
import numpy as np

from flightrisk.config import get_config
from flightrisk.obs_log import notice

_log = logging.getLogger(__name__)

try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False


class FlightRiskAgent:
    """LLM-powered reasoning for person matching and scene analysis."""

    provider = "gemma"
    label = "Gemma"  # shown in the dashboard ("Awaiting Gemma reasoning...")

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
        """Log a reasoning call that failed (type and redacted message, never the prompt)."""
        self.last_usage = {"error_type": type(exc).__name__}
        _log.warning("reasoning_call_failed provider=%s kind=%s error_type=%s error=%s",
                     self.provider, kind, type(exc).__name__, exc)

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
            return {"match": False, "confidence": "unavailable", "reasoning": "Model not loaded"}

        ref_b64 = self._image_to_base64(reference)
        cand_b64 = self._image_to_base64(candidate)

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
            text = self._ask(prompt, [ref_b64, cand_b64])
            return self._parse_match_response(text)
        except Exception as e:
            self._call_failed("analyze", e)
            return {"match": False, "confidence": "error", "reasoning": str(e)}

    def describe_person(self, image: np.ndarray) -> str:
        """Generate a text description of a detected person."""
        if not self._available:
            return "Model not available"

        b64 = self._image_to_base64(image)
        try:
            return self._ask(
                "Describe this person's appearance in one sentence. "
                "Include: clothing colors, hair, approximate age, "
                "and any visible accessories (backpack, hat, etc).",
                [b64],
            )
        except Exception as e:
            return f"Error: {e}"

    def analyze_scene(self, image: np.ndarray) -> str:
        """Analyze the scene for search planning."""
        if not self._available:
            return "Model not available"

        b64 = self._image_to_base64(image)
        try:
            return self._ask(
                "Briefly describe this aerial/drone view scene. "
                "Is it a park, field, forest, crowd, parking lot? "
                "How many people are visible? Any obstacles? "
                "One short paragraph.",
                [b64],
            )
        except Exception as e:
            return f"Error: {e}"

    def match_description(
        self, image: np.ndarray, description: str
    ) -> dict[str, Any]:
        """Check if a detected person matches a text description.

        Used when no reference photo is available — the parent describes
        the child and Gemma 4 compares against each detection.
        """
        if not self._available:
            return {"match": False, "confidence": "unavailable", "reasoning": "Model not loaded"}

        b64 = self._image_to_base64(image)
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
            text = self._ask(prompt, [b64])
            return self._parse_match_response(text)
        except Exception as e:
            self._call_failed("describe", e)
            return {"match": False, "confidence": "error", "reasoning": str(e)}

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
