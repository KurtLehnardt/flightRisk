"""Model-free unit tests for FaceRecognizer's matching / tracking logic.

The InsightFace models are replaced by a stub `_observe`, so these run
without any model download.
"""

import numpy as np
import pytest

from flightrisk.vision import face as face_mod
from flightrisk.vision.face import (
    FaceObservation,
    FaceRecognizer,
    TargetReport,
    face_quality,
    yaw_ratio,
)

pytestmark = pytest.mark.skipif(not face_mod.HAS_INSIGHTFACE, reason="insightface not installed")


def _unit(seed, d=512):
    v = np.random.default_rng(seed).normal(size=d).astype(np.float32)
    return v / np.linalg.norm(v)


def _kps(yaw=0.0):
    # left eye, right eye, nose, mouth l, mouth r (inter-eye distance 40)
    return np.array([[30, 40], [70, 40], [50 + yaw * 40, 60], [35, 80], [65, 80]], dtype=np.float32)


def _make(target):
    """FaceRecognizer without loading models; `_observe` is stubbed by tests."""
    r = FaceRecognizer.__new__(FaceRecognizer)
    r.match_threshold = 0.45
    r.track_reuse = True
    r.track_refresh_every = 4
    r.track_settled_obs = 3
    r.track_top_k = 5
    r.track_max = 4
    r._target_embedding = target
    r._target_templates = None if target is None else target[None, :]
    r.target_report = TargetReport()
    r._tracks = {}
    r._call = 0
    r._memo = {}
    r.calls = 0
    return r


def _obs(emb, q=0.9):
    return FaceObservation(np.zeros(4), _kps(), 0.9, emb, q, 60.0)


class TestPrimitives:
    def test_yaw_ratio_frontal_is_zero(self):
        assert yaw_ratio(_kps(0.0)) == pytest.approx(0.0)

    def test_yaw_ratio_is_signed_and_scaled(self):
        assert yaw_ratio(_kps(0.5)) == pytest.approx(0.5)
        assert yaw_ratio(_kps(-0.5)) == pytest.approx(-0.5)

    def test_quality_prefers_big_frontal_confident(self):
        good = face_quality(0.9, 80, _kps(0.0))
        assert good > face_quality(0.9, 20, _kps(0.0))   # smaller
        assert good > face_quality(0.9, 80, _kps(0.7))   # turned
        assert good > face_quality(0.5, 80, _kps(0.0))   # less confident
        assert 0.0 <= good <= 1.0


class TestMatching:
    def test_no_target_returns_none(self):
        r = _make(None)
        assert r.find_match([{"crop": np.zeros((50, 50, 3), np.uint8)}]) == (None, 0.0)
        assert r.compare(np.zeros((50, 50, 3), np.uint8)) == 0.0

    def test_find_match_picks_best_and_memoises_compare(self):
        t = _unit(1)
        r = _make(t)
        crops = [np.zeros((80, 40, 3), np.uint8) for _ in range(2)]
        embs = {id(crops[0]): _unit(2), id(crops[1]): t}
        calls = []

        def fake(crop):
            calls.append(1)
            return _obs(embs[id(crop)])

        r._observe = fake
        idx, score = r.find_match([{"crop": c} for c in crops])
        assert idx == 1 and score == pytest.approx(1.0, abs=1e-5)
        n = len(calls)
        # The dashboard re-queries the matched crop: must not re-run the models.
        assert r.compare(crops[1]) == pytest.approx(1.0, abs=1e-5)
        assert len(calls) == n

    def test_low_resolution_row_used_for_matching(self):
        t = _unit(1)
        r = _make(t)
        low = _unit(5)
        r._target_templates = np.stack([t, low])
        crop = np.zeros((80, 40, 3), np.uint8)
        r._observe = lambda c: _obs(low)
        assert r.compare(crop) == pytest.approx(1.0, abs=1e-5)


class TestTrackAggregation:
    def test_settled_track_skips_embedding(self):
        t = _unit(1)
        r = _make(t)
        n = {"c": 0}

        def fake(crop):
            n["c"] += 1
            return _obs(t)

        r._observe = fake
        crop = np.zeros((80, 40, 3), np.uint8)
        for _ in range(3):
            r.find_match([{"crop": crop, "track_id": 9}])
        assert n["c"] == 3
        for _ in range(3):  # settled and not yet due for a refresh
            _, s = r.find_match([{"crop": crop, "track_id": 9}])
            assert s == pytest.approx(1.0, abs=1e-5)
        assert n["c"] == 3
        r.find_match([{"crop": crop, "track_id": 9}])  # refresh due
        assert n["c"] == 4

    def test_aggregation_beats_single_noisy_frame(self):
        t = _unit(1)
        r = _make(t)
        rng = np.random.default_rng(0)
        seq = []
        for _ in range(5):
            e = t + rng.normal(scale=0.045, size=512).astype(np.float32)
            seq.append(e / np.linalg.norm(e))
        it = iter(seq)
        r._observe = lambda c: _obs(next(it))
        crop = np.zeros((80, 40, 3), np.uint8)
        singles = [float(t @ e) for e in seq]
        score = 0.0
        for _ in seq:
            _, score = r.find_match([{"crop": crop, "track_id": 3}])
        assert score > np.mean(singles)

    def test_misses_return_zero_and_backoff(self):
        r = _make(_unit(1))
        n = {"c": 0}

        def fake(crop):
            n["c"] += 1
            return None

        r._observe = fake
        crop = np.zeros((80, 40, 3), np.uint8)
        for _ in range(12):
            _, s = r.find_match([{"crop": crop, "track_id": 1}])
            assert s == 0.0
        assert n["c"] < 12  # back-off skipped some frames

    def test_track_table_is_bounded(self):
        r = _make(_unit(1))
        r._observe = lambda c: _obs(_unit(2))
        crop = np.zeros((80, 40, 3), np.uint8)
        for tid in range(10):
            r.find_match([{"crop": crop, "track_id": tid}])
        assert len(r._tracks) <= r.track_max

    def test_clear_target_drops_state(self):
        r = _make(_unit(1))
        r._observe = lambda c: _obs(_unit(1))
        r.find_match([{"crop": np.zeros((80, 40, 3), np.uint8), "track_id": 1}])
        r.clear_target()
        assert not r.has_target and not r._tracks and r._target_templates is None


class TestTargetReport:
    def test_as_dict_roundtrip(self):
        rep = TargetReport(ok=False, reason="no_face", message="m", warnings=["w"])
        d = rep.as_dict()
        assert d["reason"] == "no_face" and d["warnings"] == ["w"] and d["ok"] is False
