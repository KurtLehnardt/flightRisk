"""Tests for amber.config — centralized configuration.

Covers WS6 (centralized configuration management): default values must
match what used to be hardcoded at each call site, `from_env()` overrides
must work, `get_config()` must behave as a singleton, and a handful of
constructors across the codebase must actually read from config instead
of silently keeping their old hardcoded literals.
"""

from unittest.mock import MagicMock, patch

import pytest

from amber.config import (
    AmberConfig,
    DashboardConfig,
    DroneConfig,
    ReasoningConfig,
    VisionConfig,
    get_config,
    reset_config,
)


@pytest.fixture(autouse=True)
def _isolated_config():
    """Reset the config singleton before and after every test in this file.

    `get_config()` caches a process-wide singleton, and many other test
    modules construct config-aware objects (PersonReID, MatchScorer, ...)
    with bare constructors that rely on it. Without resetting on both
    sides of each test, an env-var override made here could leak into
    unrelated tests that happen to run afterward in the same session.
    """
    reset_config()
    yield
    reset_config()


# ---------------------------------------------------------------------------
# Defaults must match what was previously hardcoded at each call site.
# ---------------------------------------------------------------------------


class TestDefaultsMatchCurrentHardcodedValues:
    def test_vision_defaults(self):
        v = VisionConfig()
        assert v.detector_model == "yolo11n.pt"
        assert v.detector_confidence == 0.4
        assert v.reid_threshold == 0.55
        assert v.reid_model == "ViT-B-16"
        assert v.face_det_size == (640, 640)
        assert v.scorer_match_threshold == 0.45
        assert v.scorer_reid_weight == 0.35
        assert v.scorer_face_weight == 0.40
        assert v.scorer_reasoning_weight == 0.25
        assert v.tracker_iou_threshold == 0.3
        assert v.tracker_max_missing == 15
        assert v.tracker_score_window == 8

    def test_reasoning_defaults(self):
        r = ReasoningConfig()
        assert r.model == "gemma4:latest"
        assert r.queue_maxsize == 10
        assert r.alert_cooldown == 10.0
        assert r.gemma_rate_limit == 5.0
        assert r.spatial_grid_size == 50
        assert r.track_update_interval == 1.0
        assert r.reasoning_interval == 5.0
        assert r.metrics_interval == 10.0
        assert r.corroboration_threshold == 3

    def test_drone_defaults(self):
        d = DroneConfig()
        assert d.auto_connect_interval == 5.0
        assert d.battery_warn_threshold == 20
        assert d.battery_critical_threshold == 10
        assert d.tello_default_host == "192.168.10.1"
        assert d.mavlink_default_address == "udp://:14540"
        assert d.mavlink_cmd_timeout == 30.0

    def test_dashboard_defaults(self):
        db = DashboardConfig()
        assert db.port == 5555
        assert db.frame_emit_interval == 0.05
        assert db.jpeg_quality == 70
        assert db.captures_dir == "captures"

    def test_amber_config_bundles_all_sections(self):
        cfg = AmberConfig()
        assert isinstance(cfg.vision, VisionConfig)
        assert isinstance(cfg.reasoning, ReasoningConfig)
        assert isinstance(cfg.drone, DroneConfig)
        assert isinstance(cfg.dashboard, DashboardConfig)

    def test_each_amber_config_instance_gets_independent_sections(self):
        """default_factory must prevent shared mutable state between instances."""
        a = AmberConfig()
        b = AmberConfig()
        a.vision.reid_threshold = 0.99
        assert b.vision.reid_threshold == 0.55


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_no_env_vars_uses_defaults(self, monkeypatch):
        for key in (
            "AMBER_DETECTOR_MODEL",
            "AMBER_DETECTOR_CONFIDENCE",
            "AMBER_REID_THRESHOLD",
            "AMBER_SCORER_THRESHOLD",
            "AMBER_GEMMA_MODEL",
            "AMBER_ALERT_COOLDOWN",
            "AMBER_QUEUE_SIZE",
            "AMBER_PORT",
            "AMBER_MAVLINK_ADDRESS",
        ):
            monkeypatch.delenv(key, raising=False)
        cfg = AmberConfig.from_env()
        assert cfg.vision.reid_threshold == 0.55
        assert cfg.dashboard.port == 5555

    def test_detector_model_override(self, monkeypatch):
        monkeypatch.setenv("AMBER_DETECTOR_MODEL", "yolo11s.pt")
        cfg = AmberConfig.from_env()
        assert cfg.vision.detector_model == "yolo11s.pt"

    def test_detector_confidence_override_casts_to_float(self, monkeypatch):
        monkeypatch.setenv("AMBER_DETECTOR_CONFIDENCE", "0.7")
        cfg = AmberConfig.from_env()
        assert cfg.vision.detector_confidence == 0.7
        assert isinstance(cfg.vision.detector_confidence, float)

    def test_reid_threshold_override(self, monkeypatch):
        monkeypatch.setenv("AMBER_REID_THRESHOLD", "0.8")
        cfg = AmberConfig.from_env()
        assert cfg.vision.reid_threshold == 0.8

    def test_scorer_threshold_override(self, monkeypatch):
        monkeypatch.setenv("AMBER_SCORER_THRESHOLD", "0.6")
        cfg = AmberConfig.from_env()
        assert cfg.vision.scorer_match_threshold == 0.6

    def test_gemma_model_override(self, monkeypatch):
        monkeypatch.setenv("AMBER_GEMMA_MODEL", "gemma4:e2b")
        cfg = AmberConfig.from_env()
        assert cfg.reasoning.model == "gemma4:e2b"

    def test_alert_cooldown_override(self, monkeypatch):
        monkeypatch.setenv("AMBER_ALERT_COOLDOWN", "20.5")
        cfg = AmberConfig.from_env()
        assert cfg.reasoning.alert_cooldown == 20.5

    def test_queue_size_override_casts_to_int(self, monkeypatch):
        monkeypatch.setenv("AMBER_QUEUE_SIZE", "25")
        cfg = AmberConfig.from_env()
        assert cfg.reasoning.queue_maxsize == 25
        assert isinstance(cfg.reasoning.queue_maxsize, int)

    def test_port_override_casts_to_int(self, monkeypatch):
        monkeypatch.setenv("AMBER_PORT", "9090")
        cfg = AmberConfig.from_env()
        assert cfg.dashboard.port == 9090
        assert isinstance(cfg.dashboard.port, int)

    def test_mavlink_address_override(self, monkeypatch):
        monkeypatch.setenv("AMBER_MAVLINK_ADDRESS", "udp://:99999")
        cfg = AmberConfig.from_env()
        assert cfg.drone.mavlink_default_address == "udp://:99999"

    def test_multiple_overrides_combine_and_others_stay_default(self, monkeypatch):
        monkeypatch.setenv("AMBER_PORT", "7000")
        monkeypatch.setenv("AMBER_REID_THRESHOLD", "0.9")
        cfg = AmberConfig.from_env()
        assert cfg.dashboard.port == 7000
        assert cfg.vision.reid_threshold == 0.9
        assert cfg.vision.detector_confidence == 0.4
        assert cfg.reasoning.alert_cooldown == 10.0


# ---------------------------------------------------------------------------
# get_config() / reset_config() singleton behavior
# ---------------------------------------------------------------------------


class TestGetConfigSingleton:
    def test_returns_same_instance_across_calls(self):
        a = get_config()
        b = get_config()
        assert a is b

    def test_reset_config_forces_a_new_instance(self, monkeypatch):
        a = get_config()
        monkeypatch.setenv("AMBER_PORT", "1234")
        reset_config()
        b = get_config()
        assert a is not b
        assert b.dashboard.port == 1234

    def test_get_config_reads_env_on_first_call_after_reset(self, monkeypatch):
        monkeypatch.setenv("AMBER_ALERT_COOLDOWN", "42")
        reset_config()
        cfg = get_config()
        assert cfg.reasoning.alert_cooldown == 42.0

    def test_mutating_singleton_persists_across_get_config_calls(self):
        cfg = get_config()
        cfg.vision.reid_threshold = 0.12345
        assert get_config().vision.reid_threshold == 0.12345


# ---------------------------------------------------------------------------
# Spot-check that real constructors actually consume config values.
# ---------------------------------------------------------------------------


class TestConfigActuallyUsedByConstructors:
    def test_match_scorer_uses_config_defaults(self):
        from amber.vision.scorer import MatchScorer

        cfg = get_config().vision
        scorer = MatchScorer()
        assert scorer.reid_weight == cfg.scorer_reid_weight
        assert scorer.face_weight == cfg.scorer_face_weight
        assert scorer.reasoning_weight == cfg.scorer_reasoning_weight
        assert scorer.match_threshold == cfg.scorer_match_threshold

    def test_match_scorer_explicit_args_override_config(self):
        from amber.vision.scorer import MatchScorer

        scorer = MatchScorer(match_threshold=0.99, reid_weight=0.1)
        assert scorer.match_threshold == 0.99
        assert scorer.reid_weight == 0.1

    def test_match_scorer_picks_up_env_override(self, monkeypatch):
        monkeypatch.setenv("AMBER_SCORER_THRESHOLD", "0.77")
        reset_config()
        from amber.vision.scorer import MatchScorer

        scorer = MatchScorer()
        assert scorer.match_threshold == 0.77

    def test_detection_tracker_uses_config_defaults(self):
        from amber.vision.tracker import DetectionTracker

        cfg = get_config().vision
        tracker = DetectionTracker()
        assert tracker.iou_threshold == cfg.tracker_iou_threshold
        assert tracker.max_age == cfg.tracker_max_missing
        assert tracker.vote_window == cfg.tracker_score_window

    def test_detection_tracker_explicit_args_override_config(self):
        from amber.vision.tracker import DetectionTracker

        tracker = DetectionTracker(max_age=999, iou_threshold=0.9)
        assert tracker.max_age == 999
        assert tracker.iou_threshold == 0.9

    def test_tello_controller_uses_config_default_host(self):
        with patch("amber.drone.tello.Tello"):
            from amber.drone.tello import TelloController

            ctrl = TelloController()
            assert ctrl.host == get_config().drone.tello_default_host

    def test_tello_controller_explicit_host_overrides_config(self):
        with patch("amber.drone.tello.Tello"):
            from amber.drone.tello import TelloController

            ctrl = TelloController(host="10.0.0.99")
            assert ctrl.host == "10.0.0.99"

    def test_mavlink_controller_uses_config_default_host(self):
        from amber.drone.mavlink import MavlinkController

        ctrl = MavlinkController(name="cfg-test-default")
        assert ctrl.host == get_config().drone.mavlink_default_address

    def test_mavlink_controller_explicit_host_overrides_config(self):
        from amber.drone.mavlink import MavlinkController

        ctrl = MavlinkController(name="cfg-test-explicit", host="udp://:55555")
        assert ctrl.host == "udp://:55555"

    def test_person_detector_uses_config_defaults(self):
        with patch("amber.vision.detector.YOLO") as mock_yolo:
            mock_yolo.return_value = MagicMock()
            from amber.vision.detector import PersonDetector

            cfg = get_config().vision
            detector = PersonDetector()
            mock_yolo.assert_called_once_with(cfg.detector_model)
            assert detector.confidence == cfg.detector_confidence

    def test_person_detector_explicit_args_override_config(self):
        with patch("amber.vision.detector.YOLO") as mock_yolo:
            mock_yolo.return_value = MagicMock()
            from amber.vision.detector import PersonDetector

            detector = PersonDetector(model_name="yolo11s.pt", confidence=0.9)
            mock_yolo.assert_called_once_with("yolo11s.pt")
            assert detector.confidence == 0.9

    def test_person_reid_uses_config_default_threshold(self):
        with patch("amber.vision.reid.open_clip") as mock_clip:
            mock_clip.create_model_and_transforms.return_value = (
                MagicMock(),
                None,
                MagicMock(),
            )
            from amber.vision.reid import PersonReID

            reid = PersonReID()
            assert reid.match_threshold == get_config().vision.reid_threshold

    def test_person_reid_explicit_threshold_overrides_config(self):
        with patch("amber.vision.reid.open_clip") as mock_clip:
            mock_clip.create_model_and_transforms.return_value = (
                MagicMock(),
                None,
                MagicMock(),
            )
            from amber.vision.reid import PersonReID

            reid = PersonReID(match_threshold=0.11)
            assert reid.match_threshold == 0.11

    def test_amber_agent_uses_config_default_model(self):
        with patch("amber.reasoning.agent.ollama") as mock_ollama:
            mock_client = MagicMock()
            mock_client.list.return_value = MagicMock(models=[])
            mock_ollama.Client.return_value = mock_client
            from amber.reasoning.agent import AmberAgent

            agent = AmberAgent()
            assert agent.model == get_config().reasoning.model

    def test_amber_agent_explicit_model_overrides_config(self):
        with patch("amber.reasoning.agent.ollama") as mock_ollama:
            mock_client = MagicMock()
            mock_client.list.return_value = MagicMock(models=[])
            mock_ollama.Client.return_value = mock_client
            from amber.reasoning.agent import AmberAgent

            agent = AmberAgent(model="gemma4:e2b")
            assert agent.model == "gemma4:e2b"
