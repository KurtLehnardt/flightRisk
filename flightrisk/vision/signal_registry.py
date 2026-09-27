"""Signal registry -- wires configurable signal providers to the MatchScorer.

Called during pipeline initialization to create, configure, and register
all enabled signal providers. Each provider's ``set_target()`` is called
when a target photo is uploaded.

Usage in pipeline init:

    from flightrisk.vision.signal_registry import SignalRegistry

    registry = SignalRegistry()      # reads config, creates providers
    registry.register_all(scorer)    # registers active signals with scorer

    # Later, when a target photo is set:
    registry.set_target(image)

    # In the frame loop, score a detection:
    extra_signals = registry.score_detection(crop)
    scored = scorer.score(reid_score=..., face_score=..., **extra_signals)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

from flightrisk.config import get_config

if TYPE_CHECKING:
    from flightrisk.vision.scorer import MatchScorer

logger = logging.getLogger(__name__)


class SignalRegistry:
    """Manages configurable signal providers and their lifecycle.

    Reads :class:`~flightrisk.config.VisionConfig` signal settings on
    construction and lazily instantiates only the enabled providers.
    ONNX-based providers that fail to load (missing model file) are
    silently disabled.
    """

    def __init__(self) -> None:
        cfg = get_config().vision
        self._providers: dict[str, object] = {}

        for name in ("clothing_color", "height_ratio", "osnet_reid", "insightface_face"):
            sig_cfg = getattr(cfg, f"signal_{name}")
            if not sig_cfg.enabled:
                continue
            provider = self._create_provider(name)
            if provider is not None:
                self._providers[name] = provider
                logger.info("signal enabled: %s (weight=%.2f)", name, sig_cfg.weight)
            else:
                logger.info("signal disabled: %s (unavailable)", name)

    def _create_provider(self, name: str) -> object | None:
        """Instantiate the provider for `name` if it's available.

        Returns the provider instance, or None if the signal name is
        unknown or the provider reports itself unavailable (e.g. missing
        ONNX model file).
        """
        cfg = get_config().vision
        if name == "clothing_color":
            from flightrisk.vision.signals import ClothingColorSignal
            provider = ClothingColorSignal()
            return provider if provider.available else None

        if name == "height_ratio":
            from flightrisk.vision.signals import HeightRatioSignal
            provider = HeightRatioSignal()
            return provider if provider.available else None

        if name == "osnet_reid":
            from flightrisk.vision.signals import OSNetReIDSignal
            provider = OSNetReIDSignal(model_path=cfg.osnet_model_path)
            return provider if provider.available else None

        if name == "insightface_face":
            from flightrisk.vision.signals import InsightFaceR18Signal
            provider = InsightFaceR18Signal(model_path=cfg.insightface_r18_model_path)
            return provider if provider.available else None

        return None

    def enable_signal(self, name: str) -> bool:
        """Enable a signal that was disabled at runtime.

        Returns True if the signal provider exists and was re-added.
        """
        cfg = get_config().vision
        attr = f"signal_{name}"
        if not hasattr(cfg, attr):
            return False
        sig_cfg = getattr(cfg, attr)
        sig_cfg.enabled = True
        # If provider already active, nothing to do
        if name in self._providers:
            return True
        # Try to create the provider
        provider = self._create_provider(name)
        if provider is not None:
            self._providers[name] = provider
            return True
        return False

    def disable_signal(self, name: str) -> None:
        """Disable a signal at runtime."""
        cfg = get_config().vision
        attr = f"signal_{name}"
        if hasattr(cfg, attr):
            getattr(cfg, attr).enabled = False
        self._providers.pop(name, None)

    @property
    def active_signals(self) -> list[str]:
        """Names of all active (enabled + available) signal providers."""
        return list(self._providers.keys())

    def get_provider(self, name: str) -> object | None:
        """Return a signal provider by name, or None if not active."""
        return self._providers.get(name)

    def register_all(self, scorer: MatchScorer) -> None:
        """Register all active signals with the scorer.

        Args:
            scorer: The MatchScorer to register signals with.
        """
        cfg = get_config().vision
        for name in self._providers:
            sig_cfg = getattr(cfg, f"signal_{name}")
            scorer.register_signal(name, weight=sig_cfg.weight)
            logger.info("registered signal '%s' with scorer (weight=%.2f)", name, sig_cfg.weight)

    def set_target(self, image: np.ndarray) -> dict[str, bool]:
        """Set the target reference for all active signal providers.

        Args:
            image: BGR numpy array of the target person photo.

        Returns:
            Dict mapping signal name to success (True/False).
        """
        results: dict[str, bool] = {}
        for name, provider in self._providers.items():
            try:
                ret = provider.set_target(image)  # type: ignore[union-attr]
                # set_target returns bool for ONNX signals, None for others
                results[name] = ret if isinstance(ret, bool) else True
            except Exception:
                logger.warning("signal '%s' set_target failed", name, exc_info=True)
                results[name] = False
        return results

    def clear_targets(self) -> None:
        """Clear the target from all active signal providers."""
        for provider in self._providers.values():
            try:
                provider.clear_target()  # type: ignore[union-attr]
            except Exception:
                logger.warning("signal clear_target failed", exc_info=True)

    def score_detection(self, crop: np.ndarray) -> dict[str, float]:
        """Score a detection crop against the target using all active signals.

        Args:
            crop: BGR numpy array of a detected person.

        Returns:
            Dict mapping signal name to score (0-1), suitable for passing
            as **kwargs to ``MatchScorer.score()``.
        """
        scores: dict[str, float] = {}
        for name, provider in self._providers.items():
            try:
                if not getattr(provider, "has_target", False):
                    continue
                score = provider.compare(crop)  # type: ignore[union-attr]
                scores[name] = score
            except Exception:
                logger.warning("signal '%s' compare failed", name, exc_info=True)
        return scores
