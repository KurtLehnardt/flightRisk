"""Gemma 4 reasoning agent via Ollama.

Provides higher-level reasoning about detected persons — called
periodically on cropped detections, NOT every frame.

Usage:
    1. Install Ollama: https://ollama.com
    2. Pull Gemma 4: `ollama pull gemma4`
    3. Start Ollama (runs automatically on macOS)
"""

import base64
import io
from typing import Any

import cv2
import numpy as np

try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False


class AmberAgent:
    """LLM-powered reasoning for person matching and scene analysis."""

    def __init__(self, model: str = "gemma4:latest"):
        """Initialize the reasoning agent.

        Args:
            model: Ollama model name. Options:
                   - 'gemma4:latest' — 12B, 9.6GB, good balance
                   - 'gemma4:e2b' — smallest with vision, 7.2GB
                   - 'gemma4:26b-mlx' — best quality, ~18GB, MLX optimized
        """
        if not HAS_OLLAMA:
            raise RuntimeError("pip install ollama && brew install ollama")

        self.model = model
        self._available = self._check_model()
        if self._available:
            print(f"[reasoning] Gemma 4 ({model}) ready")
        else:
            print(f"[reasoning] Model {model} not found. Run: ollama pull {model}")

    def _check_model(self) -> bool:
        """Check if the model is available in Ollama."""
        try:
            models = ollama.list()
            available = [m.model for m in models.models]
            return any(self.model in m for m in available)
        except Exception:
            return False

    def _image_to_base64(self, image: np.ndarray) -> str:
        """Convert a BGR numpy array to base64-encoded JPEG."""
        _, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return base64.b64encode(buffer).decode("utf-8")

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
            "You are helping find a lost child. "
            "Image 1 is the reference photo of the child we are looking for. "
            "Image 2 is a person detected by a drone camera.\n\n"
            "Compare the two people. Consider: clothing color/type, hair, "
            "build, height, backpack/accessories, and any distinguishing features.\n\n"
            "Respond in this exact format:\n"
            "MATCH: yes or no\n"
            "CONFIDENCE: high, medium, or low\n"
            "REASONING: one sentence explaining why"
        )

        try:
            response = ollama.chat(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": prompt,
                    "images": [ref_b64, cand_b64],
                }],
            )
            text = response.message.content.strip()
            return self._parse_match_response(text)
        except Exception as e:
            return {"match": False, "confidence": "error", "reasoning": str(e)}

    def describe_person(self, image: np.ndarray) -> str:
        """Generate a text description of a detected person."""
        if not self._available:
            return "Model not available"

        b64 = self._image_to_base64(image)
        try:
            response = ollama.chat(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": (
                        "Describe this person's appearance in one sentence. "
                        "Include: clothing colors, hair, approximate age, "
                        "and any visible accessories (backpack, hat, etc)."
                    ),
                    "images": [b64],
                }],
            )
            return response.message.content.strip()
        except Exception as e:
            return f"Error: {e}"

    def analyze_scene(self, image: np.ndarray) -> str:
        """Analyze the scene for search planning."""
        if not self._available:
            return "Model not available"

        b64 = self._image_to_base64(image)
        try:
            response = ollama.chat(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": (
                        "Briefly describe this aerial/drone view scene. "
                        "Is it a park, field, forest, crowd, parking lot? "
                        "How many people are visible? Any obstacles? "
                        "One short paragraph."
                    ),
                    "images": [b64],
                }],
            )
            return response.message.content.strip()
        except Exception as e:
            return f"Error: {e}"

    def _parse_match_response(self, text: str) -> dict[str, Any]:
        """Parse the structured response from analyze_match."""
        result = {"match": False, "confidence": "unknown", "reasoning": text}

        for line in text.split("\n"):
            line = line.strip().upper()
            if line.startswith("MATCH:"):
                result["match"] = "YES" in line
            elif line.startswith("CONFIDENCE:"):
                result["confidence"] = line.split(":", 1)[1].strip().lower()
            elif line.startswith("REASONING:"):
                result["reasoning"] = line.split(":", 1)[1].strip()

        return result
