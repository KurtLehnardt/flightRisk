"""Tests for the dashboard's side-by-side per-drone video tiles.

Two edges push frames into the same ``frame`` SocketIO event. Before tiles,
the browser drew every frame into one image, so the view alternated between
drones. These tests cover the server tagging each frame with its drone id and
the template/JS contract that keeps one tile per drone.

Hermetic: the scoring path is a plain ``GroundStation`` and emits are captured
with a recorder; the template, script and stylesheet are checked as text.
"""

import re
from pathlib import Path

from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.state import app_state
from flightrisk.edge import Detection, DetectionMessage
from flightrisk.ground import GroundStation

DASH = Path(__file__).resolve().parent.parent / "flightrisk" / "dashboard"
TEMPLATE = DASH / "templates" / "index.html"
SCRIPT = DASH / "static" / "ui" / "ui.js"
STYLES = DASH / "static" / "ui" / "ui.css"


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None, *args, **kwargs):
        self.events.append((event, payload))

    def named(self, name):
        return [p for (e, p) in self.events if e == name]


def _msg(drone_id, ts, width, height, bbox):
    return DetectionMessage(
        timestamp=ts, frame_id=1, drone_id=drone_id,
        detections=[Detection(bbox=bbox, confidence=0.9)],
        frame_width=width, frame_height=height,
    )


class TestFramesCarryDroneId:
    def test_frame_event_tagged_with_drone_id_and_dimensions(self, clean_app_state):
        app_state.ground_station = GroundStation()
        sio = FakeSocketIO()
        app_module._edge_process_and_emit(
            sio, _msg("drone-1", 1.0, 640, 480, (0, 0, 64, 48)), {})
        frame = sio.named("frame")[0]
        assert frame["drone_id"] == "drone-1"
        assert (frame["frame_width"], frame["frame_height"]) == (640, 480)

    def test_frame_without_drone_id_has_none(self, clean_app_state):
        app_state.ground_station = GroundStation()
        sio = FakeSocketIO()
        app_module._edge_process_and_emit(
            sio, _msg(None, 1.0, 100, 50, (0, 0, 10, 5)), {})
        assert sio.named("frame")[0]["drone_id"] is None

    def test_two_drones_emit_distinct_ids_with_own_normalised_boxes(self, clean_app_state):
        app_state.ground_station = GroundStation()
        sio = FakeSocketIO()
        fps_state = {}
        app_module._edge_process_and_emit(
            sio, _msg("drone-1", 1.0, 100, 100, (10, 10, 50, 50)), fps_state)
        app_module._edge_process_and_emit(
            sio, _msg("drone-2", 1.0, 200, 400, (10, 10, 50, 50)), fps_state)
        f1, f2 = sio.named("frame")
        assert {f1["drone_id"], f2["drone_id"]} == {"drone-1", "drone-2"}
        assert f1["boxes"][0]["bbox_norm"] == [0.1, 0.1, 0.5, 0.5]
        assert f2["boxes"][0]["bbox_norm"] == [0.05, 0.025, 0.25, 0.125]
        assert (f2["frame_width"], f2["frame_height"]) == (200, 400)

    def test_fps_is_computed_per_drone(self, clean_app_state):
        # Interleaved drones must not derive a frame rate from each other.
        app_state.ground_station = GroundStation()
        sio = FakeSocketIO()
        fps_state = {}
        for drone, ts in (("drone-1", 10.0), ("drone-2", 10.4),
                          ("drone-1", 10.5), ("drone-2", 10.9)):
            app_module._edge_process_and_emit(
                sio, _msg(drone, ts, 100, 100, (0, 0, 10, 10)), fps_state)
        frames = sio.named("frame")
        assert frames[0]["fps"] == 0.0 and frames[1]["fps"] == 0.0
        assert frames[2]["fps"] == 2.0  # drone-1: 0.5 s apart
        assert frames[3]["fps"] == 2.0  # drone-2: 0.5 s apart


class TestTemplateTileContract:
    @staticmethod
    def _html():
        return TEMPLATE.read_text(encoding="utf-8")

    @staticmethod
    def _js():
        return SCRIPT.read_text(encoding="utf-8")

    def test_tile_container_and_grid_layout(self):
        assert 'id="video-tiles"' in self._html()
        assert re.search(r"#video-tiles\s*\{[^}]*grid-template-columns: repeat\(auto-fit",
                         STYLES.read_text(encoding="utf-8"))

    def test_legacy_ids_still_present(self):
        html = self._html()
        for dom_id in ("video-feed", "detection-overlay", "video-placeholder",
                       "detections-only-indicator", "video-toggle",
                       "appearance-only-badge"):
            assert f'id="{dom_id}"' in html

    def test_frame_handler_picks_tile_by_drone_id(self):
        html = self._js()
        assert "getTile(data.drone_id)" in html
        # No single shared box/image state left to be overwritten by the other drone.
        assert "latestBoxes" not in html
        assert "latestHasImage" not in html

    def test_each_tile_has_label_and_no_video_placeholder(self):
        html, js = self._html(), self._js()
        # The static first tile and every created tile carry a label and a "No video" placeholder.
        assert '<div class="tile-label"></div>' in html
        assert '<div class="tile-nodata">No video</div>' in html
        assert "el('div', 'tile-label')" in js
        assert "el('div', 'tile-nodata', 'No video')" in js
        assert re.search(r"TILE_STALE_MS\s*=\s*3000", js)

    def test_boxes_drawn_on_own_tile_canvas(self):
        html = self._js()
        assert "function renderDetectionOverlay(t)" in html
        assert "t.canvas.getContext('2d')" in html
        assert "drawDetectionBoxes(ctx, t.boxes, rect)" in html
        # The letterbox rect comes from this tile's own image size.
        assert "t.feed.naturalWidth" in html

    def test_video_toggle_still_fleet_wide(self):
        html = self._js()
        assert "socket.emit('set_stream_video'" in html
        assert "syncVideoToggle(data.stream_video)" in html
