"""set_target handler behaviour when the face recognizer rejects the photo."""

import base64
from unittest.mock import MagicMock

import cv2
import numpy as np

from flightrisk.dashboard.app import app, socketio, _state
from flightrisk.vision.face import TargetReport

# Reuse the fixture + helper from the edge-mode tests.
from tests.test_dashboard_edge import _jpeg_bytes


def _run(monkeypatch, face):
    monkeypatch.setattr(cv2, "imwrite", MagicMock())
    reid = MagicMock()
    reid.extract_embedding.return_value = np.array([1.0, 2.0, 3.0])
    _state["reid"] = reid
    _state["face"] = face
    _state["canon"] = None
    _state["logger"] = None
    transport = MagicMock()
    _state["ground_transport"] = transport
    _state["ground_station"] = MagicMock()
    img_b64 = base64.b64encode(_jpeg_bytes()).decode("utf-8")
    client = socketio.test_client(app)
    client.get_received()
    client.emit("set_target", {"image": img_b64})
    received = client.get_received()
    client.disconnect()
    return transport, received


def test_rejected_photo_pushes_no_face_target_and_reports_it(clean_app_state, monkeypatch):
    face = MagicMock()
    face.set_target.return_value = False
    face.target_embedding = None
    face.target_report = TargetReport(ok=False, reason="no_face", message="No face found")
    transport, received = _run(monkeypatch, face)

    face.extract_embedding.assert_not_called()  # must not bypass the quality gate
    _, face_arg = transport.broadcast_target.call_args[0]
    assert face_arg is None
    ev = [r for r in received if r["name"] == "target_set"][0]["args"][0]
    assert ev["face_detected"] is False
    assert ev["face_report"]["ok"] is False
    assert ev["face_report"]["message"] == "No face found"


def test_enrolled_photo_pushes_template_embedding(clean_app_state, monkeypatch):
    face = MagicMock()
    face.set_target.return_value = True
    face.target_embedding = np.array([0.5, 0.5], dtype=np.float32)
    transport, _ = _run(monkeypatch, face)
    _, face_arg = transport.broadcast_target.call_args[0]
    assert face_arg == [0.5, 0.5]
