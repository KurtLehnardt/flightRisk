"""Behavior-level tests for Flask routes and SocketIO event handlers in
amber.dashboard.app.

Unlike tests/e2e/test_dashboard.py (which only checks route registration),
these tests drive real request/event handling via Flask's and
flask-socketio's test clients. Heavy ML components (`_state["detector"]`,
`_state["reid"]`, etc.) are stubbed with MagicMocks through `_state`
directly, so no models are loaded and `_init_pipeline()` is never called.
"""

import io
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from amber.dashboard.app import app, socketio, _state


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def _jpeg_bytes(size=(20, 20)):
    img = np.random.randint(0, 255, (*size, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


class TestHealthRoute:
    def test_health_returns_200_with_expected_shape(self, client, clean_app_state):
        resp = client.get("/api/health")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert set(data["components"]) >= {"detector", "reid", "face", "reasoning", "db"}

    def test_health_reflects_state_components(self, client, clean_app_state):
        _state["detector"] = MagicMock()
        _state["reasoning"] = None

        resp = client.get("/api/health")
        data = resp.get_json()

        assert data["components"]["detector"] is True
        assert data["components"]["reasoning"] is False


class TestStatusRoute:
    def test_status_returns_match_history_and_source(self, client, clean_app_state):
        _state["match_history"] = [{"time": "12:00:00", "score": 0.9}]
        _state["source"] = "webcam"

        resp = client.get("/api/status")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["match_history"] == [{"time": "12:00:00", "score": 0.9}]
        assert data["source"] == "webcam"


class TestSessionMatchesRoute:
    """`/api/sessions/<id>` is the route that returns a session's match
    list (there is no separate `/api/session/matches` endpoint)."""

    def test_session_detail_returns_matches(self, client, clean_app_state):
        db = MagicMock()
        db.get_session.return_value = {"id": "s1", "source": "webcam"}
        db.get_session_matches.return_value = [{"id": 1, "match_type": "reid"}]
        _state["db"] = db

        resp = client.get("/api/sessions/s1")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["matches"] == [{"id": 1, "match_type": "reid"}]
        db.get_session_matches.assert_called_once_with("s1")

    def test_session_detail_missing_session_returns_404(self, client, clean_app_state):
        db = MagicMock()
        db.get_session.return_value = None
        _state["db"] = db

        resp = client.get("/api/sessions/unknown")

        assert resp.status_code == 404

    def test_session_detail_no_db_returns_500(self, client, clean_app_state):
        _state["db"] = None

        resp = client.get("/api/sessions/s1")

        assert resp.status_code == 500


class TestUploadTarget:
    def test_upload_valid_image(self, client, clean_app_state, monkeypatch):
        monkeypatch.setattr(cv2, "imwrite", MagicMock())
        reid = MagicMock()
        _state["reid"] = reid
        _state["face"] = None
        _state["canon"] = None

        data = {"file": (io.BytesIO(_jpeg_bytes()), "target.jpg")}
        resp = client.post(
            "/api/upload-target", data=data, content_type="multipart/form-data"
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        reid.set_target.assert_called_once()
        uploaded_img = reid.set_target.call_args[0][0]
        assert uploaded_img.shape[2] == 3

    def test_upload_no_file_returns_400(self, client, clean_app_state):
        resp = client.post(
            "/api/upload-target", data={}, content_type="multipart/form-data"
        )

        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_upload_invalid_image_returns_400(self, client, clean_app_state):
        data = {"file": (io.BytesIO(b"not a real image"), "bad.jpg")}
        resp = client.post(
            "/api/upload-target", data=data, content_type="multipart/form-data"
        )

        assert resp.status_code == 400
        assert "error" in resp.get_json()


class TestClearTarget:
    def test_clear_target_resets_state(self, client, clean_app_state, monkeypatch):
        # The handler unconditionally targets the repo root's
        # target_reference.jpg via `Path(__file__).parent.parent.parent /
        # "target_reference.jpg"` -- it does not read
        # `_state["target_photo_path"]`. Mock Path.exists/unlink so this test
        # can never delete a real file on disk.
        mock_unlink = MagicMock()
        monkeypatch.setattr("amber.dashboard.app.Path.exists", MagicMock(return_value=True))
        monkeypatch.setattr("amber.dashboard.app.Path.unlink", mock_unlink)

        reid = MagicMock()
        face = MagicMock()
        _state["reid"] = reid
        _state["face"] = face
        _state["target_photo"] = "somebase64data"
        _state["target_photo_path"] = "/tmp/nonexistent-target.jpg"

        resp = client.post("/api/clear-target")

        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert _state["target_photo"] is None
        assert _state["target_photo_path"] is None
        reid.clear_target.assert_called_once()
        face.clear_target.assert_called_once()
        mock_unlink.assert_called_once()


class TestDroneCommandSocket:
    def test_valid_command_is_executed_and_acked(self, clean_app_state):
        drone = MagicMock()
        fleet = MagicMock()
        fleet.primary = drone
        _state["fleet"] = fleet
        _state["logger"] = None

        sio_client = socketio.test_client(app)
        sio_client.get_received()  # drain the connect-time "status" event
        sio_client.emit("drone_command", {"command": "takeoff"})
        received = sio_client.get_received()
        sio_client.disconnect()

        acks = [r for r in received if r["name"] == "command_ack"]
        assert len(acks) == 1
        assert acks[0]["args"][0]["command"] == "takeoff"
        drone.takeoff.assert_called_once()

    def test_no_fleet_connected_emits_error(self, clean_app_state):
        _state["fleet"] = None

        sio_client = socketio.test_client(app)
        sio_client.get_received()
        sio_client.emit("drone_command", {"command": "takeoff"})
        received = sio_client.get_received()
        sio_client.disconnect()

        errors = [r for r in received if r["name"] == "error"]
        assert len(errors) == 1
        assert "No drones connected" in errors[0]["args"][0]["message"]

    def test_command_exception_emits_error_not_ack(self, clean_app_state):
        drone = MagicMock()
        drone.land.side_effect = RuntimeError("motor fault")
        fleet = MagicMock()
        fleet.primary = drone
        _state["fleet"] = fleet
        _state["logger"] = None

        sio_client = socketio.test_client(app)
        sio_client.get_received()
        sio_client.emit("drone_command", {"command": "land"})
        received = sio_client.get_received()
        sio_client.disconnect()

        names = [r["name"] for r in received]
        assert "error" in names
        assert "command_ack" not in names


class TestRestartDashboardSocket:
    def test_restart_dashboard_reinitializes_pipeline(self, clean_app_state, monkeypatch):
        calls = {}

        def fake_init_pipeline(source_config, target_path=None):
            calls["source"] = source_config.source
            calls["target_path"] = target_path

        monkeypatch.setattr("amber.dashboard.app._init_pipeline", fake_init_pipeline)
        _state["fleet"] = None
        _state["cap"] = None
        _state["source"] = "webcam"
        _state["source_config"] = None
        _state["target_photo_path"] = None

        sio_client = socketio.test_client(app)
        sio_client.get_received()
        sio_client.emit("restart_dashboard")
        received = sio_client.get_received()
        sio_client.disconnect()

        names = [r["name"] for r in received]
        assert "dashboard_restarted" in names
        assert calls["source"] == "webcam"

    def test_restart_dashboard_disconnects_existing_fleet(self, clean_app_state, monkeypatch):
        monkeypatch.setattr("amber.dashboard.app._init_pipeline", MagicMock())
        fleet = MagicMock()
        _state["fleet"] = fleet
        _state["cap"] = None
        _state["source"] = "tello"
        _state["source_config"] = None

        sio_client = socketio.test_client(app)
        sio_client.get_received()
        sio_client.emit("restart_dashboard")
        sio_client.get_received()
        sio_client.disconnect()

        fleet.disconnect_all.assert_called_once()
