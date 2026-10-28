"""Dashboard-side regression tests for the face-recognition review (upload, revert, clear, connect).

Model-free: the face recognizer is a stub, the transport is a MagicMock.
"""

import io
from unittest.mock import MagicMock

import cv2
import numpy as np

from flightrisk.dashboard.app import app, socketio
from flightrisk.dashboard.state import app_state
from flightrisk.vision.face import TargetReport

from tests.test_dashboard_edge import _jpeg_bytes


def _wire(monkeypatch, face_report=None):
    monkeypatch.setattr(cv2, "imwrite", MagicMock())
    reid = MagicMock()
    reid.extract_embedding.return_value = np.array([1.0, 2.0, 3.0])
    face = MagicMock()
    face.set_target.return_value = face_report is None or face_report.ok
    face.target_embedding = np.array([0.5, 0.5], dtype=np.float32)
    face.target_report = face_report or TargetReport(ok=True, reason="ok", message="ok")
    transport, station = MagicMock(), MagicMock()
    app_state.reid, app_state.face = reid, face
    app_state.ground_transport, app_state.ground_station = transport, station
    app_state.canon = None
    app_state.signal_registry = None
    app_state.logger = None
    return face, transport, station


def test_upload_target_returns_face_report(clean_app_state, monkeypatch):
    rep = TargetReport(ok=False, reason="no_face", message="No face found")
    _wire(monkeypatch, rep)
    resp = app.test_client().post(
        "/api/upload-target", data={"file": (io.BytesIO(_jpeg_bytes()), "t.jpg")},
        content_type="multipart/form-data")
    body = resp.get_json()
    assert body["face_detected"] is False
    assert body["face_report"]["reason"] == "no_face"


def test_revert_target_reports_face_and_pushes_to_edges(clean_app_state, monkeypatch):
    rep = TargetReport(ok=False, reason="face_too_small", message="small")
    face, transport, station = _wire(monkeypatch, rep)
    face.target_embedding = None
    canon = MagicMock()
    canon.revert_to.return_value = np.random.randint(0, 255, (20, 20, 3), dtype=np.uint8)
    app_state.canon = canon
    client = socketio.test_client(app)
    client.get_received()
    client.emit("revert_target", {"version_id": "v1"})
    ev = [r for r in client.get_received() if r["name"] == "target_set"][0]["args"][0]
    client.disconnect()
    assert ev["reverted_to"] == "v1"
    assert ev["face_report"]["reason"] == "face_too_small"
    # The edge and ground scorers must follow the revert (here: no face target).
    reid_arg, face_arg = station.set_target.call_args[0]
    assert reid_arg == [1.0, 2.0, 3.0] and face_arg is None
    assert transport.broadcast_target.call_args[0] == (reid_arg, None)


def test_clear_target_pushes_no_target_to_ground_and_edges(clean_app_state, monkeypatch):
    face, transport, station = _wire(monkeypatch)
    resp = app.test_client().post("/api/clear-target")
    assert resp.get_json()["success"] is True
    station.set_target.assert_called_once_with(None, None)
    transport.broadcast_target.assert_called_once_with(None, None)


def test_broadcast_failure_does_not_fail_clear(clean_app_state, monkeypatch):
    face, transport, station = _wire(monkeypatch)
    transport.broadcast_target.side_effect = TimeoutError("edge")
    assert app.test_client().post("/api/clear-target").get_json()["success"] is True


def test_connect_sends_face_report_when_target_present(clean_app_state, monkeypatch):
    rep = TargetReport(ok=False, reason="no_face", message="No face found")
    _wire(monkeypatch, rep)
    app_state.target_photo = "abc"
    client = socketio.test_client(app)
    names = {r["name"]: r["args"][0] for r in client.get_received()}
    client.disconnect()
    assert names["face_report"]["face_report"]["reason"] == "no_face"


