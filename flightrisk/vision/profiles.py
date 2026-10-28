"""Detection profiles: named sets of detector settings an operator picks per drone.

A profile bundles the person detector's model weights with the settings that
suit one way of looking at the ground, so the operator can say "this drone
looks straight down" or "this drone flies level" instead of tuning
thresholds. Profiles live in a registry (``ProfileRegistry``); adding one is
a ``register`` call, and nothing else in the code branches on profile names.

The built-in profiles are:

* ``default``: exactly the detector the edge built before profiles existed
  (``yolo11n.pt`` at confidence 0.4, tiled only when
  ``FLIGHTRISK_DETECTOR_TILED`` says so). An edge with no profile selected
  runs this one, so behaviour without a selection is unchanged.
* ``top_down``: people seen from above. The HERIDAL fine-tuned YOLO11s
  (``models/heridal_yolo11s.pt``, see ``docs/altitude-benchmark.md``) run on
  overlapping tiles at confidence 0.5. Its weights are not in git; when the
  file is missing the edge falls back to ``level`` and says so.
* ``level``: level flight with more direct views of faces. The general
  ``yolo11n.pt`` on the whole frame (no tiling) at confidence 0.4.

Tiling reuses ``PersonDetector``'s tiled mode. A profile that leaves the tile
size or overlap unset uses ``FLIGHTRISK_DETECTOR_TILE_SIZE`` and
``FLIGHTRISK_DETECTOR_TILE_OVERLAP``, and a profile that leaves tiling unset
follows ``FLIGHTRISK_DETECTOR_TILED`` (``flightrisk/config.py``), so those
existing settings keep working.

The face and ReID weight scales are hints for the ground's match scorer:
the ground multiplies the scorer's signal weights by them for detections
from a drone running the profile (``weight_scale``). Every built-in profile
uses 1.0 (no change) until scales are measured. A scale changes a score
only when both signals are present: the scorer normalises the weights of
the signals it has, so on an appearance-only edge (ReID alone) any scale
cancels out. They never turn face recognition on; the appearance-only rules
decide whether an edge sends face embeddings at all.

This module imports no model code at import time; ``build_detector`` imports
``PersonDetector`` (and so Ultralytics) only when it is called.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flightrisk.fleet_protocol import is_profile_name

__all__ = [
    "DEFAULT_PROFILE",
    "REGISTRY",
    "DetectionProfile",
    "ProfileRegistry",
    "ProfileUnavailableError",
    "Resolution",
    "UnknownProfileError",
    "build_detector",
    "profile_label",
    "resolve_weights",
    "weight_scale_for",
]

DEFAULT_PROFILE = "default"
# The repository root, for weights given relative to it (``models/...``)
# when the edge was started from another directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class UnknownProfileError(KeyError):
    """No profile of that name is registered."""


class ProfileUnavailableError(RuntimeError):
    """The profile's weights are missing and no fallback profile has its weights either."""


@dataclass(frozen=True)
class DetectionProfile:
    """One named set of detector settings.

    ``None`` in a detector setting means "use the detector's or the
    configuration's default", so a profile only states what it changes.

    Attributes:
        name: Registry key and wire name (``fleet_protocol.is_profile_name``).
        label: Short text for the dashboard.
        description: One sentence on when to use it.
        weights: Model weights: a file path (relative paths are tried from
            the working directory, then from the repository root) or an
            Ultralytics model name such as ``yolo11n.pt``.
        weights_required: True when the weights must already be on disk.
            False lets Ultralytics fetch a stock model by name when it is
            missing (that needs a network; without one the load fails).
        confidence: Minimum detection confidence.
        nms_iou: Non-maximum suppression IoU threshold, or None for the
            detector default.
        input_size: Model input size in pixels, or None for the model
            default (640 for these models).
        tiled: Run the model on overlapping tiles. None follows
            ``FLIGHTRISK_DETECTOR_TILED``.
        tile_size: Tile edge in pixels, or None for
            ``FLIGHTRISK_DETECTOR_TILE_SIZE`` (640).
        tile_overlap: Tile overlap in [0, 1), or None for
            ``FLIGHTRISK_DETECTOR_TILE_OVERLAP`` (0.2).
        min_person_px: Smallest person height, in pixels, the profile is
            expected to find reliably (a planning hint for altitude and
            coverage, not a filter), or None when unknown.
        view: ``nadir`` (looking down), ``oblique`` (level flight) or
            ``any``; informational.
        face_weight_scale: Multiplier for the ground's face signal weight.
        reid_weight_scale: Multiplier for the ground's ReID signal weight.
        fallback: Profile to use instead when this one's required weights
            are missing, or None.
    """

    name: str
    label: str
    description: str
    weights: str
    weights_required: bool = False
    confidence: float = 0.4
    nms_iou: float | None = None
    input_size: int | None = None
    tiled: bool | None = None
    tile_size: int | None = None
    tile_overlap: float | None = None
    min_person_px: int | None = None
    view: str = "any"
    face_weight_scale: float = 1.0
    reid_weight_scale: float = 1.0
    fallback: str | None = None

    def __post_init__(self) -> None:
        if not is_profile_name(self.name):
            raise ValueError(f"profile name {self.name!r} must be lowercase letters, digits and underscores")
        if self.fallback is not None and not is_profile_name(self.fallback):
            raise ValueError(f"fallback {self.fallback!r} is not a profile name")
        if self.fallback == self.name:
            raise ValueError("a profile cannot fall back to itself")
        if not self.weights:
            raise ValueError("weights must name a model")
        if not 0.0 < self.confidence < 1.0:
            raise ValueError("confidence must be in (0, 1)")
        if self.nms_iou is not None and not 0.0 < self.nms_iou <= 1.0:
            raise ValueError("nms_iou must be in (0, 1]")
        if self.input_size is not None and self.input_size < 32:
            raise ValueError("input_size must be at least 32")
        if self.tile_size is not None and self.tile_size < 1:
            raise ValueError("tile_size must be positive")
        if self.tile_overlap is not None and not 0.0 <= self.tile_overlap < 1.0:
            raise ValueError("tile_overlap must be in [0, 1)")
        if self.face_weight_scale < 0 or self.reid_weight_scale < 0:
            raise ValueError("weight scales must not be negative")

    def weight_scale(self) -> dict[str, float] | None:
        """The scorer weight multipliers, or None when they are all 1 (no change)."""
        scale = {"face": self.face_weight_scale, "reid": self.reid_weight_scale}
        if all(value == 1.0 for value in scale.values()):
            return None
        return scale

    def describe(self) -> dict[str, Any]:
        """The fields the dashboard shows (no paths beyond the configured weights name)."""
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "weights": self.weights,
            "weights_required": self.weights_required,
            "confidence": self.confidence,
            "tiled": self.tiled,
            "view": self.view,
            "min_person_px": self.min_person_px,
            "face_weight_scale": self.face_weight_scale,
            "reid_weight_scale": self.reid_weight_scale,
            "fallback": self.fallback,
        }


@dataclass(frozen=True)
class Resolution:
    """The profile to load for a request, and whether that is a fallback."""

    requested: str
    profile: DetectionProfile
    fallback_reason: str | None = None

    @property
    def fallback(self) -> bool:
        return self.profile.name != self.requested


def resolve_weights(weights: str, *, exists: Callable[[str], bool] = os.path.exists) -> str | None:
    """The path to load *weights* from, or None when the file is not on disk.

    An absolute path is used as is. A relative one is tried from the working
    directory, then from the repository root.
    """
    if os.path.isabs(weights):
        return weights if exists(weights) else None
    if exists(weights):
        return weights
    rooted = str(_REPO_ROOT / weights)
    return rooted if exists(rooted) else None


class ProfileRegistry:
    """Profiles by name, in registration order. Thread-safe."""

    def __init__(self, profiles: tuple[DetectionProfile, ...] | list[DetectionProfile] = ()):
        self._lock = threading.Lock()
        self._profiles: dict[str, DetectionProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: DetectionProfile, *, replace: bool = False) -> None:
        """Add *profile*; a name already taken raises ``ValueError`` unless *replace*."""
        with self._lock:
            if profile.name in self._profiles and not replace:
                raise ValueError(f"a profile named {profile.name!r} is already registered")
            self._profiles[profile.name] = profile

    def get(self, name: Any) -> DetectionProfile:
        """The profile called *name*; raises ``UnknownProfileError``."""
        with self._lock:
            profile = self._profiles.get(name) if isinstance(name, str) else None
        if profile is None:
            raise UnknownProfileError(name)
        return profile

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._profiles)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return isinstance(name, str) and name in self._profiles

    def __iter__(self) -> Iterator[DetectionProfile]:
        with self._lock:
            return iter(tuple(self._profiles.values()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._profiles)

    def available(self, profile: DetectionProfile, *, exists: Callable[[str], bool] = os.path.exists) -> bool:
        """True when *profile* can be loaded without fetching anything it requires."""
        return not profile.weights_required or resolve_weights(profile.weights, exists=exists) is not None

    def resolve(self, name: str, *, exists: Callable[[str], bool] = os.path.exists) -> Resolution:
        """The profile to load for a request for *name*.

        When *name*'s required weights are missing, its ``fallback`` chain is
        followed (each profile at most once) to the first profile whose
        weights are available; the result then has ``fallback_reason``
        ``weights_missing``. Raises ``UnknownProfileError`` for an unknown
        name and ``ProfileUnavailableError`` when nothing in the chain can
        be loaded.
        """
        requested = self.get(name)
        seen: set[str] = set()
        profile: DetectionProfile | None = requested
        while profile is not None and profile.name not in seen:
            seen.add(profile.name)
            if self.available(profile, exists=exists):
                reason = None if profile is requested else "weights_missing"
                return Resolution(requested=requested.name, profile=profile, fallback_reason=reason)
            nxt = profile.fallback
            profile = self._profiles.get(nxt) if nxt is not None else None
        raise ProfileUnavailableError(
            f"the weights of profile {requested.name!r} ({requested.weights}) are missing and no fallback "
            f"profile can be loaded"
        )

    def describe(self, *, exists: Callable[[str], bool] = os.path.exists) -> list[dict[str, Any]]:
        """Every profile's dashboard fields, with ``available`` on this machine."""
        return [{**p.describe(), "available": self.available(p, exists=exists)} for p in self]


def builtin_profiles() -> tuple[DetectionProfile, ...]:
    """The profiles every edge and ground knows."""
    return (
        DetectionProfile(
            name=DEFAULT_PROFILE,
            label="Startup default (yolo11n)",
            description="The edge detector as it was before profiles: yolo11n at confidence 0.4, "
                        "tiled only when FLIGHTRISK_DETECTOR_TILED is on.",
            weights="yolo11n.pt",
            weights_required=False,
            confidence=0.4,
        ),
        DetectionProfile(
            name="top_down",
            label="Top-down search (HERIDAL)",
            description="People seen from above: the HERIDAL fine-tuned YOLO11s on overlapping tiles.",
            weights="models/heridal_yolo11s.pt",
            weights_required=True,
            confidence=0.5,
            tiled=True,
            min_person_px=32,
            view="nadir",
            fallback="level",
        ),
        DetectionProfile(
            name="level",
            label="Level flight (faces)",
            description="More direct views of people and faces: the general yolo11n on the whole frame.",
            weights="yolo11n.pt",
            weights_required=False,
            confidence=0.4,
            tiled=False,
            view="oblique",
        ),
    )


#: The process-wide registry. Register further profiles here at startup.
REGISTRY = ProfileRegistry(builtin_profiles())


def build_detector(profile: DetectionProfile, *, detector_cls: Any = None, exists: Callable[[str], bool] = os.path.exists):
    """Construct the person detector *profile* describes.

    Only settings the profile sets are passed, so the ``default`` profile
    builds exactly ``PersonDetector(model_name="yolo11n.pt", confidence=0.4)``,
    the edge's detector before profiles existed. Raises whatever the
    detector raises when the model cannot be loaded.
    """
    if detector_cls is None:
        from flightrisk.vision.detector import PersonDetector

        detector_cls = PersonDetector
    # Required weights are resolved to the file found on disk (the working
    # directory or the repository root). Stock model names go to Ultralytics
    # unchanged, exactly as before profiles existed.
    weights = profile.weights
    if profile.weights_required:
        weights = resolve_weights(profile.weights, exists=exists) or profile.weights
    kwargs: dict[str, Any] = {"model_name": weights, "confidence": profile.confidence}
    optional = {
        "tiled": profile.tiled,
        "tile_size": profile.tile_size,
        "overlap": profile.tile_overlap,
        "nms_iou": profile.nms_iou,
        "imgsz": profile.input_size,
    }
    kwargs.update({key: value for key, value in optional.items() if value is not None})
    return detector_cls(**kwargs)


def profile_label(value: Any, registry: ProfileRegistry | None = None) -> str:
    """A bounded metric label: a registered profile name, ``none`` or ``other``."""
    if value is None:
        return "none"
    return value if value in (registry if registry is not None else REGISTRY) else "other"


def weight_scale_for(name: Any, registry: ProfileRegistry | None = None) -> Mapping[str, float] | None:
    """The scorer weight multipliers for a profile name from the wire, or None (no change)."""
    if not isinstance(name, str):
        return None
    try:
        return (registry if registry is not None else REGISTRY).get(name).weight_scale()
    except UnknownProfileError:
        return None
