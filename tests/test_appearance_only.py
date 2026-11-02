"""Tests for the dashboard's privacy defaults and status fields (R-001, R-019, R-020).

Covers appearance-only mode (face recognition is never constructed and the
face signals stay disabled), ``--no-llm`` (no reasoning agent and no Gemma
worker thread), the ``appearance_only``/``tiled``/``fleet`` status fields,
the launcher defaults in ``python -m flightrisk.dashboard``, and the frame
loop's match snapshot going through the captures gate.

Hermetic: heavy components (YOLO, ReID, the session database, the obstacle
guard) are patched or pre-populated with mocks, and the configuration
singleton is replaced by a fresh one for each test so the face-signal switch
never leaks into other tests.
"""

import os
import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import flightrisk.config as config_module
import flightrisk.dashboard.__main__ as dashboard_main
from flightrisk.dashboard import alerts
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import SourceConfig, _init_pipeline, app, socketio
from flightrisk.dashboard.pipeline import _frame_loop
from flightrisk.dashboard.state import alerted_tracks, app_state


@pytest.fixture
def fresh_config(monkeypatch):
    """Give the test its own configuration singleton, restored afterwards."""
    for name in ("FLIGHTRISK_APPEARANCE_ONLY", "FLIGHTRISK_DETECTOR_TILED", "FLIGHTRISK_SAVE_CAPTURES"):
        monkeypatch.delenv(name, raising=False)
    cfg = config_module.FlightRiskConfig.from_env()
    monkeypatch.setattr(config_module, "_config", cfg)
    return cfg


@pytest.fixture
def fake_face_module(monkeypatch):
    """Replace ``flightrisk.vision.face`` so constructing FaceRecognizer is observable."""
    module = types.ModuleType("flightrisk.vision.face")
    module.FaceRecognizer = MagicMock(name="FaceRecognizer")
    monkeypatch.setitem(sys.modules, "flightrisk.vision.face", module)
    return module.FaceRecognizer


@pytest.fixture
def stubbed_pipeline(clean_app_state, fresh_config):
    """Pre-populate the components ``_init_pipeline`` would otherwise build from models."""
    app_state.running = False
    app_state.detector = None
    app_state.reid = None
    app_state.face = None
    app_state.scorer = None
    app_state.signal_registry = None
    app_state.tracker = MagicMock()
    app_state.reasoning = None
    app_state.gemma_thread = None
    app_state.logger = MagicMock()
    app_state.metrics = MagicMock()
    app_state.obstacle_guard = MagicMock()
    app_state.canon = MagicMock()
    app_state.db = None
    app_state.fleet_supervisor = None
    with patch.object(app_module, "PersonDetector") as detector, \
            patch.object(app_module, "PersonReID") as reid, \
            patch.object(app_module, "SessionDB") as session_db, \
            patch.object(app_module, "SignalRegistry") as registry, \
            patch("cv2.VideoCapture"):
        session_db.return_value.create_session.return_value = "session-1"
        yield types.SimpleNamespace(detector=detector, reid=reid, registry=registry)
    stop = app_state.auto_connect_stop
    if stop is not None:
        stop.set()


# ---------------------------------------------------------------------------
# R-001: appearance-only mode
# ---------------------------------------------------------------------------

def test_init_pipeline_skips_face_when_appearance_only(stubbed_pipeline, fake_face_module, fresh_config):
    _init_pipeline(SourceConfig(source="file", appearance_only=True, enable_llm=False))

    assert app_state.face is None
    fake_face_module.assert_not_called()
    stubbed_pipeline.registry.return_value.disable_signal.assert_called_with("insightface_face")
    # The face signal is switched off before the registry is built, so its
    # model is never loaded either.
    assert fresh_config.vision.signal_insightface_face.enabled is False
    assert app_state.appearance_only is True


def test_init_pipeline_appearance_only_drops_an_existing_face_recognizer(stubbed_pipeline, fake_face_module):
    app_state.face = MagicMock(name="previous FaceRecognizer")

    _init_pipeline(SourceConfig(source="file", appearance_only=True, enable_llm=False))

    assert app_state.face is None
    fake_face_module.assert_not_called()


def test_init_pipeline_builds_face_when_appearance_only_is_off(stubbed_pipeline, fake_face_module, fresh_config):
    # Control for the test above: the patch point is the one _init_pipeline uses.
    _init_pipeline(SourceConfig(source="file", appearance_only=False, enable_llm=False))

    fake_face_module.assert_called_once()
    assert app_state.face is fake_face_module.return_value
    stubbed_pipeline.registry.return_value.disable_signal.assert_not_called()
    assert fresh_config.vision.signal_insightface_face.enabled is True
    assert app_state.appearance_only is False


def test_init_pipeline_appearance_only_falls_back_to_vision_config(stubbed_pipeline, fake_face_module, fresh_config):
    fresh_config.vision.appearance_only = True

    _init_pipeline(SourceConfig(source="file", enable_llm=False))  # appearance_only=None

    assert app_state.face is None
    fake_face_module.assert_not_called()
    assert app_state.appearance_only is True


def test_face_signals_cannot_be_enabled_in_appearance_only_mode(clean_app_state, fresh_config):
    app_state.appearance_only = True
    registry = MagicMock()
    app_state.signal_registry = registry
    app_state.scorer = None
    app_state.logger = None
    fresh_config.vision.signal_insightface_face.enabled = False

    client = socketio.test_client(app)
    client.get_received()
    client.emit("set_signal_config", {"signal": "insightface_face", "enabled": True})
    received = client.get_received()
    client.disconnect()

    assert [r["name"] for r in received] == ["error"]
    assert "Appearance-only" in received[0]["args"][0]["message"]
    registry.enable_signal.assert_not_called()
    assert fresh_config.vision.signal_insightface_face.enabled is False


# ---------------------------------------------------------------------------
# R-019: --no-llm
# ---------------------------------------------------------------------------

def test_no_llm_skips_reasoning(stubbed_pipeline, fake_face_module, monkeypatch):
    agent_module = types.ModuleType("flightrisk.reasoning.agent")
    agent_module.FlightRiskAgent = MagicMock(name="FlightRiskAgent")
    monkeypatch.setitem(sys.modules, "flightrisk.reasoning.agent", agent_module)
    app_state.reasoning = MagicMock(name="reasoning left from an earlier pipeline")

    _init_pipeline(SourceConfig(source="file", appearance_only=True, enable_llm=False))

    assert app_state.reasoning is None
    assert app_state.gemma_thread is None
    agent_module.FlightRiskAgent.assert_not_called()


def test_llm_enabled_by_default_constructs_reasoning(stubbed_pipeline, fake_face_module, monkeypatch):
    # Control for the test above.
    agent_module = types.ModuleType("flightrisk.reasoning.agent")
    agent_module.FlightRiskAgent = MagicMock(name="FlightRiskAgent")
    monkeypatch.setitem(sys.modules, "flightrisk.reasoning.agent", agent_module)

    # running is False, so the worker thread exits as soon as it starts.
    _init_pipeline(SourceConfig(source="file", appearance_only=True))

    agent_module.FlightRiskAgent.assert_called_once()
    assert app_state.reasoning is agent_module.FlightRiskAgent.return_value
    assert app_state.gemma_thread is not None
    app_state.gemma_thread.join(timeout=10)


# ---------------------------------------------------------------------------
# R-020: status fields
# ---------------------------------------------------------------------------

def test_status_reports_privacy_and_fleet_fields(clean_app_state, fresh_config):
    app_state.appearance_only = True
    app_state.detector = types.SimpleNamespace(tiled=True)
    app_state.fleet_supervisor = None
    app_state.source_config = SourceConfig(source="edge", fleet_net="ap")
    app_state.face = None
    app_state.match_history = []

    data = app.test_client().get("/api/status").get_json()

    assert data["appearance_only"] is True
    assert data["tiled"] is True
    assert data["fleet"] == {"drones": 0, "net": "ap", "synced": False}

    # With a fleet, the summary comes from the supervisor.
    supervisor = MagicMock()
    supervisor.summary.return_value = {"drones": 2, "net": "station", "synced": True}
    app_state.fleet_supervisor = supervisor
    app_state.appearance_only = False
    app_state.detector = None  # falls back to VisionConfig.detector_tiled
    fresh_config.vision.detector_tiled = False

    data = app.test_client().get("/api/status").get_json()

    assert data["appearance_only"] is False
    assert data["tiled"] is False
    assert data["fleet"] == {"drones": 2, "net": "station", "synced": True}

    # The socket status event carries the same fields.
    client = socketio.test_client(app)
    received = client.get_received()
    client.disconnect()
    status = [r["args"][0] for r in received if r["name"] == "status"]
    assert status and status[0]["connected"] is True
    assert status[0]["appearance_only"] is False
    assert status[0]["tiled"] is False
    assert status[0]["fleet"] == {"drones": 2, "net": "station", "synced": True}


# ---------------------------------------------------------------------------
# Launcher defaults: python -m flightrisk.dashboard
# ---------------------------------------------------------------------------

def _run_main(argv, env=None):
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("FLIGHTRISK_")}
    clean_env.update(env or {})
    with patch.object(sys, "argv", ["flightrisk-dashboard"] + argv), \
            patch.dict(os.environ, clean_env, clear=True), \
            patch.object(dashboard_main, "run_dashboard") as run:
        dashboard_main.main()
    run.assert_called_once()
    return run.call_args[0][0]


def test_launcher_defaults_to_appearance_only_with_llm():
    cfg = _run_main([])
    assert cfg.appearance_only is True
    assert cfg.enable_llm is True
    assert cfg.fleet_drones == ()


def test_launcher_with_face_and_env_overrides():
    assert _run_main(["--with-face"]).appearance_only is False
    assert _run_main([], {"FLIGHTRISK_APPEARANCE_ONLY": "false"}).appearance_only is False
    assert _run_main(["--appearance-only"], {"FLIGHTRISK_APPEARANCE_ONLY": "0"}).appearance_only is True
    with pytest.raises(SystemExit):
        _run_main([], {"FLIGHTRISK_APPEARANCE_ONLY": "maybe"})
    with pytest.raises(SystemExit):
        _run_main(["--with-face", "--appearance-only"])


def test_launcher_no_llm_flag():
    assert _run_main(["--no-llm"]).enable_llm is False
    assert _run_main([], {"FLIGHTRISK_NO_LLM": "1"}).enable_llm is False
    # --no-llm also forces the reasoning provider off, even when --reasoning asks for one.
    assert _run_main(["--no-llm"]).reasoning_provider == "off"
    assert _run_main(["--no-llm", "--reasoning", "claude"]).reasoning_provider == "off"
    assert _run_main([], {"FLIGHTRISK_NO_LLM": "1"}).reasoning_provider == "off"
    # Without --no-llm the provider stays the config default (None).
    assert _run_main([]).reasoning_provider is None


def test_launcher_fleet_flags():
    cfg = _run_main(
        ["--source", "edge", "--fleet", "drone-1, drone-2", "--fleet-area-cm", "600x400",
         "--fleet-strip-cm", "50"],
        {"FLIGHTRISK_FLEET_NET": "ap"},
    )
    assert cfg.fleet_drones == ("drone-1", "drone-2")
    assert cfg.fleet_area_cm == (600, 400)
    assert cfg.fleet_strip_cm == 50
    assert cfg.fleet_net == "ap"

    cfg = _run_main(["--source", "edge"], {"FLIGHTRISK_FLEET_DRONES": "drone-1,drone-2"})
    assert cfg.fleet_drones == ("drone-1", "drone-2")
    assert cfg.fleet_area_cm == (400, 400)
    assert cfg.fleet_net == "station"

    with pytest.raises(SystemExit):
        _run_main(["--source", "tello", "--fleet", "drone-1"])
    with pytest.raises(SystemExit):
        _run_main(["--source", "edge", "--fleet", "drone-1,drone-1"])
    with pytest.raises(SystemExit):
        _run_main(["--source", "edge", "--fleet-area-cm", "big"])
    with pytest.raises(SystemExit):
        _run_main(["--source", "edge"], {"FLIGHTRISK_FLEET_NET": "mesh"})


# ---------------------------------------------------------------------------
# F1: the frame loop's match snapshot goes through the captures gate
# ---------------------------------------------------------------------------

def _run_matching_frame(socketio_mock):
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    crop = np.zeros((50, 50, 3), dtype=np.uint8)
    detector = MagicMock()
    detector.detect.return_value = [{"bbox": [10, 10, 50, 50], "confidence": 0.9, "crop": crop}]
    detector.annotate.return_value = frame
    reid = MagicMock()
    reid.find_match.return_value = (0, 0.85)
    reid.compare.return_value = 0.85
    reid.match_threshold = 0.55
    scorer = MagicMock()
    scorer.score.return_value = {"combined_score": 0.85}
    scorer.alert_level.return_value = "possible_match"
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.read.return_value = (True, frame)

    for name, value in {
        "running": True, "detector": detector, "tracker": None, "reid": reid, "face": None,
        "scorer": scorer, "signal_registry": None, "target_photo": "base64data",
        "target_photo_path": "/fake/target.jpg", "cap": cap, "fleet": None, "logger": None,
        "metrics": None, "recorder": None, "tracer": None, "otel_metrics": None,
        "reasoning": None, "db": None, "session_id": None, "search_active": False,
        "target_description": None,
    }.items():
        setattr(app_state, name, value)
    alerted_tracks.clear()

    events = []

    def _emit(event, *args, **kwargs):
        events.append(event)
        if event == "frame":
            app_state.running = False

    socketio_mock.emit = _emit
    _frame_loop(socketio_mock)
    return events


def test_frame_loop_match_writes_no_snapshot_when_captures_are_off(clean_app_state, fresh_config):
    assert fresh_config.dashboard.save_captures is False

    events = _run_matching_frame(MagicMock())

    assert "match_alert" in events
    captures = alerts.CAPTURES_DIR
    assert not captures.exists() or not any(captures.iterdir())


def test_frame_loop_match_writes_snapshot_when_captures_are_on(clean_app_state, fresh_config):
    fresh_config.dashboard.save_captures = True

    events = _run_matching_frame(MagicMock())

    assert "match_alert" in events
    written = sorted(p.name for p in alerts.CAPTURES_DIR.iterdir())
    assert any(name.endswith("_frame.jpg") for name in written)
    assert any(name.endswith("_meta.json") for name in written)
