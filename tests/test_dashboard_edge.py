"""Tests for the dashboard's ``edge`` (ground-station) source mode.

Covers the edge detection consumer that scores incoming
``DetectionMessage``s via ``GroundStation`` and streams results to the
browser, plus the operator-facing SocketIO handlers (`set_stream_video`,
`set_target` in edge mode).

Hermetic: no real WebSockets, no real ML models. The scoring path uses a
real ``MatchScorer`` + ``GroundStation`` (pure numpy), the transport is a
``MagicMock``, and SocketIO emits are captured with a fake recorder or the
flask-socketio test client.
"""

import base64
from unittest.mock import MagicMock

import cv2
import numpy as np

from amber.dashboard import app as app_module
from amber.dashboard.app import app, socketio, _state
from amber.dashboard.state import app_state
from amber.edge import Detection, DetectionMessage
from amber.ground import GroundStation
from amber.vision.scorer import MatchScorer


def _jpeg_bytes(size=(20, 20)):
    img = np.random.randint(0, 255, (*size, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


class FakeSocketIO:
    """Records ``emit(event, payload)`` calls for assertions.

    Stands in for the real Flask-SocketIO instance the edge adapter emits
    on. The adapter runs off-loop in an executor thread and uses plain
    ``socketio.emit(...)``, so a recorder is all that's needed here.
    """

    def __init__(self):
        self.events = []

    def emit(self, event, payload=None, *args, **kwargs):
        self.events.append((event, payload))

    def named(self, name):
        return [payload for (event, payload) in self.events if event == name]


# ---------------------------------------------------------------------------
# Edge adapter: _edge_process_and_emit
# ---------------------------------------------------------------------------

class TestEdgeFramePayload:
    _FRAME_KEYS = {
        "image", "boxes", "fps", "persons", "match", "match_score",
        "stream_video", "telemetry", "recording",
    }

    def test_frame_image_none_when_no_thumbnail(self, clean_app_state):
        app_state.ground_station = GroundStation()  # no scorer
        app_state.stream_video = False

        det = Detection(bbox=(10, 20, 60, 45), confidence=0.9)
        msg = DetectionMessage(
            timestamp=1000.0, frame_id=1, thumbnail_jpeg=None,
            detections=[det], frame_width=100, frame_height=50,
        )

        sio = FakeSocketIO()
        app_module._edge_process_and_emit(sio, msg, {"last": None})

        frames = sio.named("frame")
        assert len(frames) == 1
        payload = frames[0]
        # Exact browser emit contract keys.
        assert set(payload) == self._FRAME_KEYS
        assert payload["image"] is None
        assert payload["persons"] == 1
        assert payload["stream_video"] is False
        assert payload["telemetry"] == {}
        assert payload["recording"] is False
        assert payload["match"] is False

        assert len(payload["boxes"]) == 1
        box = payload["boxes"][0]
        assert set(box) == {"bbox_norm", "score", "matched", "track_id"}
        assert box["bbox_norm"] == [10 / 100, 20 / 50, 60 / 100, 45 / 50]
        assert all(0.0 <= c <= 1.0 for c in box["bbox_norm"])
        assert box["track_id"] is None
        assert box["matched"] is False

    def test_frame_image_base64_when_thumbnail_present(self, clean_app_state):
        app_state.ground_station = GroundStation()
        thumb = _jpeg_bytes()
        msg = DetectionMessage(
            timestamp=1.0, frame_id=1, thumbnail_jpeg=thumb,
            detections=[], frame_width=320, frame_height=180,
        )

        sio = FakeSocketIO()
        app_module._edge_process_and_emit(sio, msg, None)

        payload = sio.named("frame")[0]
        assert payload["image"] == base64.b64encode(thumb).decode("utf-8")
        assert payload["persons"] == 0
        assert payload["boxes"] == []

    def test_bbox_norm_zero_when_frame_dims_missing(self, clean_app_state):
        # Divide-by-zero guard: edge device reported no frame dimensions.
        app_state.ground_station = GroundStation()
        det = Detection(bbox=(10, 20, 60, 45), confidence=0.5)
        msg = DetectionMessage(
            timestamp=1.0, frame_id=1, detections=[det],
            frame_width=0, frame_height=0,
        )

        sio = FakeSocketIO()
        app_module._edge_process_and_emit(sio, msg, None)

        box = sio.named("frame")[0]["boxes"][0]
        assert box["bbox_norm"] == [0.0, 0.0, 0.0, 0.0]

    def test_no_ground_station_is_noop(self, clean_app_state):
        app_state.ground_station = None
        sio = FakeSocketIO()
        msg = DetectionMessage(timestamp=1.0, frame_id=1, detections=[])
        app_module._edge_process_and_emit(sio, msg, None)
        assert sio.events == []


class TestEdgeMatchAlert:
    def _matching_message(self):
        crop = _jpeg_bytes()
        det = Detection(
            bbox=(10, 20, 60, 45), confidence=0.95,
            reid_embedding=[1.0, 0.0, 0.0], crop_jpeg=crop,
        )
        msg = DetectionMessage(
            timestamp=1.0, frame_id=1, detections=[det],
            frame_width=100, frame_height=50,
        )
        return msg, crop

    def test_emits_match_alert_when_scorer_reports_match(self, clean_app_state):
        app_module._alerted_tracks.clear()
        scorer = MatchScorer(match_threshold=0.45)
        station = GroundStation(scorer=scorer)
        # Identical target/candidate embedding -> cosine similarity 1.0.
        station.set_target(reid_embedding=[1.0, 0.0, 0.0])
        app_state.ground_station = station
        app_state.scorer = scorer
        app_state.db = None
        app_state.match_history = []
        app_state.stream_video = False

        msg, crop = self._matching_message()
        sio = FakeSocketIO()
        app_module._edge_process_and_emit(sio, msg, None)

        frame = sio.named("frame")[0]
        assert frame["match"] is True
        assert frame["match_score"] >= 0.45
        assert frame["boxes"][0]["matched"] is True

        alerts = sio.named("match_alert")
        assert len(alerts) == 1
        entry = alerts[0]
        # Same match_entry shape as pipeline.py.
        assert entry["snapshot"] == base64.b64encode(crop).decode("utf-8")
        assert entry["track_id"] == app_module._compute_track_key((10, 20, 60, 45))
        assert entry["type"] == "edge"
        assert entry["alert_level"] in ("possible_match", "confirmed_match")
        assert entry["reid_score"] == 1.0
        assert entry in app_state.match_history

    def test_match_alert_respects_cooldown(self, clean_app_state):
        app_module._alerted_tracks.clear()
        scorer = MatchScorer(match_threshold=0.45)
        station = GroundStation(scorer=scorer)
        station.set_target(reid_embedding=[1.0, 0.0, 0.0])
        app_state.ground_station = station
        app_state.scorer = scorer
        app_state.db = None
        app_state.match_history = []

        msg, _ = self._matching_message()

        first = FakeSocketIO()
        app_module._edge_process_and_emit(first, msg, None)
        assert len(first.named("match_alert")) == 1

        # Same spatial track within ALERT_COOLDOWN -> no duplicate alert.
        second = FakeSocketIO()
        app_module._edge_process_and_emit(second, msg, None)
        assert second.named("match_alert") == []
        # ...but the frame event still fires every time.
        assert len(second.named("frame")) == 1


# ---------------------------------------------------------------------------
# SocketIO handlers
# ---------------------------------------------------------------------------

class TestSetStreamVideoHandler:
    def test_broadcasts_toggle_and_acks_state(self, clean_app_state):
        transport = MagicMock()
        _state["ground_transport"] = transport
        _state["logger"] = None

        sio_client = socketio.test_client(app)
        sio_client.get_received()  # drain connect-time "status"
        sio_client.emit("set_stream_video", {"enabled": True})
        received = sio_client.get_received()
        sio_client.disconnect()

        transport.broadcast_stream_video.assert_called_once_with(True)
        assert app_state.stream_video is True

        acks = [r for r in received if r["name"] == "stream_video_state"]
        assert len(acks) == 1
        assert acks[0]["args"][0]["enabled"] is True

    def test_toggle_off_without_transport_still_acks(self, clean_app_state):
        _state["ground_transport"] = None
        _state["logger"] = None
        app_state.stream_video = True

        sio_client = socketio.test_client(app)
        sio_client.get_received()
        sio_client.emit("set_stream_video", {"enabled": False})
        received = sio_client.get_received()
        sio_client.disconnect()

        assert app_state.stream_video is False
        acks = [r for r in received if r["name"] == "stream_video_state"]
        assert acks and acks[0]["args"][0]["enabled"] is False


class TestSetTargetEdgeMode:
    def test_edge_mode_broadcasts_target_embeddings(self, clean_app_state, monkeypatch):
        # Don't touch disk with the reference-photo write.
        monkeypatch.setattr(cv2, "imwrite", MagicMock())

        reid = MagicMock()
        reid.extract_embedding.return_value = np.array([1.0, 2.0, 3.0])
        _state["reid"] = reid
        _state["face"] = None
        _state["canon"] = None
        _state["logger"] = None

        transport = MagicMock()
        station = MagicMock()
        _state["ground_transport"] = transport
        _state["ground_station"] = station

        img_b64 = base64.b64encode(_jpeg_bytes()).decode("utf-8")
        sio_client = socketio.test_client(app)
        sio_client.get_received()
        sio_client.emit("set_target", {"image": img_b64})
        sio_client.get_received()
        sio_client.disconnect()

        transport.broadcast_target.assert_called_once()
        station.set_target.assert_called_once()
        reid_arg, face_arg = transport.broadcast_target.call_args[0]
        assert reid_arg == [1.0, 2.0, 3.0]
        assert face_arg is None
        # Preserves existing non-edge behavior: ReID target still set locally.
        reid.set_target.assert_called_once()

    def test_non_edge_mode_does_not_broadcast(self, clean_app_state, monkeypatch):
        monkeypatch.setattr(cv2, "imwrite", MagicMock())
        reid = MagicMock()
        _state["reid"] = reid
        _state["face"] = None
        _state["canon"] = None
        _state["logger"] = None
        _state["ground_transport"] = None  # not edge mode

        img_b64 = base64.b64encode(_jpeg_bytes()).decode("utf-8")
        sio_client = socketio.test_client(app)
        sio_client.get_received()
        sio_client.emit("set_target", {"image": img_b64})
        sio_client.get_received()
        sio_client.disconnect()

        reid.set_target.assert_called_once()  # local behavior intact
