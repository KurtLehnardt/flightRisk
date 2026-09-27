"""Centralized configuration for the FlightRisk system.

Thresholds, model names, intervals, grid sizes, and queue limits used to be
scattered as magic literals across `flightrisk/vision/*`, `flightrisk/reasoning/*`,
`flightrisk/drone/*`, and `flightrisk/dashboard/app.py`. This module is the single
place to read (and eventually tune, per deployment profile) those values.

Every default below mirrors the value that was already hardcoded at its
call site before this module existed, so introducing `AmberConfig` is
behavior-neutral: nothing changes until a value is overridden, either by
constructing `AmberConfig`/`VisionConfig`/etc. directly or via `FLIGHTRISK_*`
environment variables (see `AmberConfig.from_env`).

Usage in a module that owns a tunable constant:

    from flightrisk.config import get_config

    class PersonReID:
        def __init__(self, match_threshold: float | None = None):
            if match_threshold is None:
                match_threshold = get_config().vision.reid_threshold
            self.match_threshold = match_threshold

Callers that already pass an explicit value (e.g. a test constructing
`PersonReID(match_threshold=0.9)`) are unaffected — the explicit value
always wins over the config default.
"""

import os
import threading
import warnings
from dataclasses import dataclass, field


@dataclass
class SignalConfig:
    """Per-signal toggle and weight.

    Each matching signal (reid, face, reasoning, clothing_color, etc.)
    can be individually enabled/disabled and its weight tuned.
    """
    enabled: bool = True
    weight: float = 0.0


@dataclass
class VisionConfig:
    """Detection, ReID, scoring, and tracking parameters."""

    detector_model: str = "yolo11n.pt"
    detector_confidence: float = 0.4
    reid_threshold: float = 0.55
    reid_model: str = "ViT-B-16"
    face_det_size: tuple[int, int] = (640, 640)
    face_match_threshold: float = 0.45
    scorer_match_threshold: float = 0.45
    scorer_reid_weight: float = 0.35
    scorer_face_weight: float = 0.40
    scorer_reasoning_weight: float = 0.25
    tracker_iou_threshold: float = 0.3
    tracker_max_missing: int = 15
    tracker_score_window: int = 8

    # -- Configurable signal toggles and weights --
    # Built-in signals default to enabled; new signals default to enabled
    # but will be skipped at runtime if their model is unavailable.
    signal_reid: SignalConfig = field(
        default_factory=lambda: SignalConfig(enabled=True, weight=0.35)
    )
    signal_face: SignalConfig = field(
        default_factory=lambda: SignalConfig(enabled=True, weight=0.40)
    )
    signal_reasoning: SignalConfig = field(
        default_factory=lambda: SignalConfig(enabled=True, weight=0.25)
    )
    signal_clothing_color: SignalConfig = field(
        default_factory=lambda: SignalConfig(enabled=True, weight=0.15)
    )
    signal_height_ratio: SignalConfig = field(
        default_factory=lambda: SignalConfig(enabled=True, weight=0.05)
    )
    signal_osnet_reid: SignalConfig = field(
        default_factory=lambda: SignalConfig(enabled=True, weight=0.30)
    )
    signal_insightface_face: SignalConfig = field(
        default_factory=lambda: SignalConfig(enabled=True, weight=0.35)
    )

    # -- ONNX model paths --
    osnet_model_path: str = "models/osnet_x1_0.onnx"
    insightface_r18_model_path: str = "models/insightface_r18.onnx"


@dataclass
class ReasoningConfig:
    """Gemma reasoning worker: model, queueing, and timing parameters."""

    model: str = "gemma4:latest"
    queue_maxsize: int = 10
    alert_cooldown: float = 10.0
    gemma_rate_limit: float = 5.0
    spatial_grid_size: int = 50
    track_update_interval: float = 1.0
    reasoning_interval: float = 5.0
    metrics_interval: float = 10.0
    corroboration_threshold: int = 3


@dataclass
class DroneConfig:
    """Drone connection, telemetry, and safety thresholds."""

    auto_connect_interval: float = 5.0
    battery_warn_threshold: int = 20
    battery_critical_threshold: int = 10
    tello_default_host: str = "192.168.10.1"
    mavlink_default_address: str = "udp://:14540"
    mavlink_cmd_timeout: float = 30.0


@dataclass
class DashboardConfig:
    """Dashboard server, streaming, and capture parameters."""

    port: int = 5555
    frame_emit_interval: float = 0.05
    jpeg_quality: int = 70
    captures_dir: str = "captures"


@dataclass
class AmberConfig:
    """Top-level configuration bundle for the whole system."""

    vision: VisionConfig = field(default_factory=VisionConfig)
    reasoning: ReasoningConfig = field(default_factory=ReasoningConfig)
    drone: DroneConfig = field(default_factory=DroneConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)

    @classmethod
    def from_env(cls) -> "AmberConfig":
        """Build a config with values overridden from `FLIGHTRISK_*` env vars.

        Only a curated subset of the most commonly-tuned values is exposed
        via environment variables today. Anything not listed here can still
        be overridden by constructing `AmberConfig`/`VisionConfig`/etc.
        directly (e.g. for tests or deployment-profile presets); YAML-based
        profile loading is a possible future extension, not implemented
        here.
        """
        config = cls()
        env_map = {
            "FLIGHTRISK_DETECTOR_MODEL": ("vision", "detector_model", str),
            "FLIGHTRISK_DETECTOR_CONFIDENCE": ("vision", "detector_confidence", float),
            "FLIGHTRISK_REID_THRESHOLD": ("vision", "reid_threshold", float),
            "FLIGHTRISK_SCORER_THRESHOLD": ("vision", "scorer_match_threshold", float),
            "FLIGHTRISK_GEMMA_MODEL": ("reasoning", "model", str),
            "FLIGHTRISK_ALERT_COOLDOWN": ("reasoning", "alert_cooldown", float),
            "FLIGHTRISK_QUEUE_SIZE": ("reasoning", "queue_maxsize", int),
            "FLIGHTRISK_PORT": ("dashboard", "port", int),
            "FLIGHTRISK_MAVLINK_ADDRESS": ("drone", "mavlink_default_address", str),
            "FLIGHTRISK_OSNET_MODEL_PATH": ("vision", "osnet_model_path", str),
            "FLIGHTRISK_INSIGHTFACE_R18_MODEL_PATH": ("vision", "insightface_r18_model_path", str),
        }

        # Signal enable/weight overrides: FLIGHTRISK_SIGNAL_<NAME>_ENABLED,
        # FLIGHTRISK_SIGNAL_<NAME>_WEIGHT for each configurable signal.
        _signal_names = [
            "reid", "face", "reasoning", "clothing_color",
            "height_ratio", "osnet_reid", "insightface_face",
        ]
        for sig_name in _signal_names:
            env_prefix = f"FLIGHTRISK_SIGNAL_{sig_name.upper()}"
            sig_cfg: SignalConfig = getattr(config.vision, f"signal_{sig_name}")

            enabled_val = os.environ.get(f"{env_prefix}_ENABLED")
            if enabled_val is not None:
                sig_cfg.enabled = enabled_val.lower() in ("1", "true", "yes")

            weight_val = os.environ.get(f"{env_prefix}_WEIGHT")
            if weight_val is not None:
                try:
                    sig_cfg.weight = float(weight_val)
                except (ValueError, TypeError):
                    warnings.warn(
                        f"Invalid value for {env_prefix}_WEIGHT={weight_val!r}, "
                        f"using default",
                        stacklevel=2,
                    )
        for env_key, (section, attr, type_fn) in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                try:
                    setattr(getattr(config, section), attr, type_fn(val))
                except (ValueError, TypeError):
                    warnings.warn(
                        f"Invalid value for {env_key}={val!r}, using default",
                        stacklevel=2,
                    )
        return config


# Module-level singleton, lazily initialized on first use so importing this
# module never has side effects (e.g. reading env vars) on its own.
_config: AmberConfig | None = None
_config_lock = threading.Lock()


def get_config() -> AmberConfig:
    """Return the process-wide `AmberConfig` singleton.

    Loads from environment variables on first call; subsequent calls
    return the same cached instance. Thread-safe via double-checked
    locking so concurrent first-callers can't race and construct two
    separate instances.
    """
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                _config = AmberConfig.from_env()
    return _config


def reset_config() -> None:
    """Clear the cached singleton so the next `get_config()` reloads it.

    Mainly for tests that need to change `FLIGHTRISK_*` env vars mid-run and
    observe a fresh config.
    """
    global _config
    _config = None
