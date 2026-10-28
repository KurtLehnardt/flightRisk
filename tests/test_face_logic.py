"""Model-free unit tests for FaceRecognizer's matching / tracking logic.

The InsightFace models are replaced by a stub `_observe`, so these run
without any model download.
"""

import threading
import time
import types

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


# Tolerant lookup so these tests fail by behaviour (not AttributeError) on code
# without the expiry window.
_OBS_AGE = getattr(face_mod, "TRACK_OBS_MAX_AGE", 60)


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
    r._lock = threading.RLock()
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


class TestRobustness:
    def test_recycled_track_id_starts_fresh(self):
        t = _unit(1)
        r = _make(t)
        r._observe = lambda c: _obs(t)
        crop = np.zeros((80, 40, 3), np.uint8)
        r.find_match([{"crop": crop, "track_id": 5}])
        r._call += face_mod.TRACK_STALE_CALLS + 5
        other = _unit(7)
        r._observe = lambda c: _obs(other)
        _, s = r.find_match([{"crop": crop, "track_id": 5}])
        assert s < 0.2  # not contaminated by the old person's observation

    def test_track_id_switching_person_drops_old_obs(self):
        t = _unit(1)
        r = _make(t)
        crop = np.zeros((80, 40, 3), np.uint8)
        r._observe = lambda c: _obs(t)
        for _ in range(3):
            r.find_match([{"crop": crop, "track_id": 2}])
        other = _unit(9)
        r._observe = lambda c: _obs(other)
        for _ in range(4):  # refresh becomes due
            _, s = r.find_match([{"crop": crop, "track_id": 2}])
        assert s < 0.2 and len(r._tracks[2].obs) == 1

    def test_failed_enrolment_clears_target(self):
        r = _make(_unit(1))
        r._detect_reference = lambda img: (np.empty((0, 5)), np.empty((0, 5, 2)))
        assert r.set_target(np.zeros((100, 100, 3), np.uint8)) is False
        assert not r.has_target and r._target_templates is None
        assert r.target_report.reason == "no_face"

    def test_enrolment_exception_clears_target(self):
        r = _make(_unit(1))

        def boom(img):
            raise RuntimeError("onnx")

        r._detect_reference = boom
        assert r.set_target(np.zeros((100, 100, 3), np.uint8)) is False
        assert not r.has_target and r.target_report.reason == "error"

    def test_clear_target_waits_for_an_in_flight_match(self):
        """clear_target() racing a match in progress must block on the lock.

        The stubbed _observe parks inside find_match while another thread calls
        clear_target(). Without the lock the target vanishes mid-call and the
        similarity step raises; with it clear_target() only runs afterwards.
        """
        t = _unit(1)
        r = _make(t)
        inside, proceed = threading.Event(), threading.Event()
        errs, results = [], []

        def observe(c):
            inside.set()
            proceed.wait(2.0)
            return _obs(t)

        r._observe = observe
        crop = np.zeros((80, 40, 3), np.uint8)

        def match():
            try:
                results.append(r.find_match([{"crop": crop, "track_id": 1}]))
            except Exception as e:
                errs.append(e)

        def clear():
            inside.wait(2.0)
            r.clear_target()

        th = [threading.Thread(target=match), threading.Thread(target=clear)]
        [x.start() for x in th]
        inside.wait(2.0)
        time.sleep(0.2)          # give clear() time to run if nothing stops it
        proceed.set()
        [x.join() for x in th]
        assert not errs
        assert results and results[0][0] == 0  # the in-flight match completed
        assert not r.has_target                # and the clear applied after it


class TestStaleTrackObservations:
    """Item: a track must not keep (or hand on) a face score after the face is gone."""

    def _settle(self, r, t, tid=1, n=4):
        r._observe = lambda c: _obs(t)
        crop = np.zeros((80, 40, 3), np.uint8)
        for _ in range(n):
            r.find_match([{"crop": crop, "track_id": tid}])
        return crop

    def test_score_expires_after_face_is_no_longer_seen(self):
        t = _unit(1)
        r = _make(t)
        crop = self._settle(r, t)
        assert r.find_match([{"crop": crop, "track_id": 1}])[1] > 0.9
        r._observe = lambda c: None            # face no longer visible
        score = 1.0
        for _ in range(_OBS_AGE + 5):
            _, score = r.find_match([{"crop": crop, "track_id": 1}])
        assert score == 0.0

    def test_expired_track_does_not_lend_its_score_to_another_person(self):
        t = _unit(1)
        r = _make(t)
        crop = self._settle(r, t)
        r._observe = lambda c: None
        for _ in range(_OBS_AGE + 5):
            r.find_match([{"crop": crop, "track_id": 1}])
        sib = 0.3 * t + np.sqrt(1 - 0.09) * _unit(2)
        sib = (sib / np.linalg.norm(sib)).astype(np.float32)
        r._observe = lambda c: _obs(sib, q=0.5)
        _, score = r.find_match([{"crop": crop, "track_id": 1}])
        assert score < 0.5     # their own similarity, not the old person's 0.95

    def test_fresh_observations_are_kept(self):
        t = _unit(1)
        r = _make(t)
        crop = self._settle(r, t, n=3)
        r._observe = lambda c: _obs(t)
        for _ in range(_OBS_AGE + 20):   # continuously visible
            _, score = r.find_match([{"crop": crop, "track_id": 1}])
        assert score > 0.9


class TestMultiFaceWarning:
    def _enrol_two_faces(self, monkeypatch, w_second):
        t = _unit(1)
        r = _make(None)
        det = np.array([[0, 0, 200, 200, 0.99], [300, 0, 300 + w_second, w_second, 0.9]], np.float32)
        kps = np.stack([_kps(), _kps()]) * 1.0
        r._detect_reference = lambda img: (det, kps)
        r.template_lowres = False
        r._rec = types.SimpleNamespace(get_feat=lambda views: np.tile(t, (len(views), 1)))
        monkeypatch.setattr(face_mod, "face_align", types.SimpleNamespace(
            norm_crop=lambda img, k, s: np.random.default_rng(0).integers(0, 255, (112, 112, 3), dtype=np.uint8)),
            raising=False)
        assert r.set_target(np.zeros((300, 600, 3), np.uint8)) is True
        return r.target_report

    def test_warns_when_second_face_is_much_smaller(self, monkeypatch):
        rep = self._enrol_two_faces(monkeypatch, w_second=60)   # 0.3x: adult holding the child
        assert rep.ok and rep.n_faces == 2
        assert any("2 faces found" in w for w in rep.warnings)

    def test_warns_when_second_face_is_similar_in_size(self, monkeypatch):
        rep = self._enrol_two_faces(monkeypatch, w_second=180)
        assert any("2 faces found" in w for w in rep.warnings)
