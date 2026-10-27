"""Tests for tiled inference in PersonDetector and the related config flags."""

import numpy as np
import pytest

from flightrisk.config import get_config, reset_config
from flightrisk.vision import detector as detector_module
from flightrisk.vision.detector import PersonDetector


class _Tensor:
    def __init__(self, arr):
        self._arr = np.asarray(arr)

    def cpu(self):
        return self

    def numpy(self):
        return self._arr

    def __float__(self):
        return float(self._arr)


class _Boxes:
    def __init__(self, rows):
        self.xyxy = [_Tensor(r[:4]) for r in rows]
        self.conf = [_Tensor(r[4]) for r in rows]

    def __len__(self):
        return len(self.xyxy)


class _Result:
    def __init__(self, rows):
        self.boxes = _Boxes(rows)


class FakeModel:
    """Returns fixed boxes (tile-local xyxy + conf), chosen per call by a callback."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def __call__(self, image, **kwargs):
        self.calls.append((image.shape, kwargs))
        return [_Result(self.responder(len(self.calls) - 1, image))]


def _make_detector(monkeypatch, responder, **kwargs):
    model = FakeModel(responder)
    monkeypatch.setattr(detector_module, "YOLO", lambda name: model)
    monkeypatch.setattr(PersonDetector, "_select_device", lambda self: "cpu")
    return PersonDetector("fake.pt", 0.4, **kwargs), model


def test_config_defaults_and_env_overrides(monkeypatch):
    for key in (
        "FLIGHTRISK_APPEARANCE_ONLY", "FLIGHTRISK_DETECTOR_TILED",
        "FLIGHTRISK_DETECTOR_TILE_SIZE", "FLIGHTRISK_DETECTOR_TILE_OVERLAP",
        "FLIGHTRISK_SAVE_CAPTURES",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_config()
    cfg = get_config()
    assert cfg.vision.appearance_only is False
    assert cfg.vision.detector_tiled is False
    assert cfg.vision.detector_tile_size == 640
    assert cfg.vision.detector_tile_overlap == 0.2
    assert cfg.dashboard.save_captures is False

    monkeypatch.setenv("FLIGHTRISK_APPEARANCE_ONLY", "true")
    monkeypatch.setenv("FLIGHTRISK_DETECTOR_TILED", "1")
    monkeypatch.setenv("FLIGHTRISK_DETECTOR_TILE_SIZE", "320")
    monkeypatch.setenv("FLIGHTRISK_DETECTOR_TILE_OVERLAP", "0.35")
    monkeypatch.setenv("FLIGHTRISK_SAVE_CAPTURES", "yes")
    reset_config()
    cfg = get_config()
    assert cfg.vision.appearance_only is True
    assert cfg.vision.detector_tiled is True
    assert cfg.vision.detector_tile_size == 320
    assert cfg.vision.detector_tile_overlap == 0.35
    assert cfg.dashboard.save_captures is True

    monkeypatch.setenv("FLIGHTRISK_DETECTOR_TILED", "false")
    reset_config()
    assert get_config().vision.detector_tiled is False
    reset_config()


def test_tiles_cover_frame_with_overlap():
    h, w, size, overlap = 1080, 1920, 640, 0.2
    tiles = PersonDetector.compute_tiles(h, w, size, overlap)
    covered = np.zeros((h, w), dtype=bool)
    for x1, y1, x2, y2 in tiles:
        assert 0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h
        assert x2 - x1 <= size and y2 - y1 <= size
        covered[y1:y2, x1:x2] = True
    assert covered.all()

    xs = sorted({t[0] for t in tiles})
    for a, b in zip(xs, xs[1:]):
        assert (a + size) - b >= int(size * overlap) - 1  # neighbors overlap

    # A frame smaller than a tile yields a single full-frame tile.
    assert PersonDetector.compute_tiles(100, 200, 640, 0.2) == [(0, 0, 200, 100)]


def test_boxes_offset_to_frame_coords(monkeypatch):
    frame = np.zeros((640, 1000, 3), dtype=np.uint8)
    det, model = _make_detector(
        monkeypatch,
        lambda i, img: [(10, 20, 50, 80, 0.9)] if i == 1 else [],
        tiled=True, tile_size=640, overlap=0.2,
    )
    tiles = PersonDetector.compute_tiles(640, 1000, 640, 0.2)
    out = det.detect(frame)
    assert len(model.calls) == len(tiles)
    tx, ty = tiles[1][0], tiles[1][1]
    assert len(out) == 1
    assert out[0]["bbox"] == [10 + tx, 20 + ty, 50 + tx, 80 + ty]
    assert out[0]["confidence"] == pytest.approx(0.9)
    assert set(out[0]) == {"bbox", "confidence", "crop"}
    assert out[0]["crop"].shape == (60, 40, 3)


def test_cross_tile_duplicate_suppressed(monkeypatch):
    frame = np.zeros((640, 1000, 3), dtype=np.uint8)
    tiles = PersonDetector.compute_tiles(640, 1000, 640, 0.2)
    assert len(tiles) == 2
    # The same person at frame x 400..440 appears in both tiles (overlap region).
    def responder(i, img):
        x1 = 400 - tiles[i][0]
        return [(x1, 100, x1 + 40, 200, 0.8 if i == 0 else 0.6)]

    det, _ = _make_detector(monkeypatch, responder, tiled=True, tile_size=640, overlap=0.2)
    out = det.detect(frame)
    assert len(out) == 1
    assert out[0]["bbox"] == [400, 100, 440, 200]
    assert out[0]["confidence"] == pytest.approx(0.8)


def test_untiled_path_unchanged(monkeypatch):
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    det, model = _make_detector(
        monkeypatch, lambda i, img: [(100, 200, 160, 320, 0.7)], tiled=False
    )
    out = det.detect(frame)
    assert len(model.calls) == 1
    assert model.calls[0][0] == frame.shape
    assert model.calls[0][1] == {
        "classes": [0], "conf": 0.4, "device": "cpu", "verbose": False,
    }
    assert len(out) == 1
    assert out[0]["bbox"] == [100, 200, 160, 320]
    assert out[0]["crop"].shape == (120, 60, 3)
