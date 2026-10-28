"""Face model identity on the wire, model loading, and detector threshold handling."""

import logging
import threading
import types

import numpy as np
import pytest

from flightrisk.edge import Detection, DetectionMessage, EdgeRunner
from flightrisk.ground import GroundStation
from flightrisk.vision import face as face_mod


def _runner(model_name="buffalo_l", appearance_only=False):
    face = types.SimpleNamespace(model_name=model_name, extract_embedding=lambda c: None)
    return EdgeRunner(face=face, appearance_only=appearance_only)


def _msg(face_model=None, drone_id="d1", emb=(1.0, 0.0)):
    return DetectionMessage(
        timestamp=1.0, frame_id=1, drone_id=drone_id, face_model=face_model,
        detections=[Detection(bbox=(0, 0, 10, 10), confidence=0.9, face_embedding=list(emb))])


class TestFaceModelOnTheWire:
    def test_edge_stamps_and_serialises_its_face_model(self):
        r = _runner("buffalo_s")
        r._detector = None
        msg = r.process_frame(np.zeros((20, 20, 3), np.uint8))
        assert msg.face_model == "buffalo_s"
        data = r.to_dict(msg)
        assert data["face_model"] == "buffalo_s"
        assert EdgeRunner.from_dict(data).face_model == "buffalo_s"

    def test_field_is_omitted_when_unknown_or_appearance_only(self):
        r = EdgeRunner()
        assert "face_model" not in r.to_dict(DetectionMessage(timestamp=1.0, frame_id=1))
        r2 = _runner(appearance_only=True)
        msg = r2.process_frame(np.zeros((20, 20, 3), np.uint8))
        assert "face_model" not in r2.to_dict(msg)

    def test_old_peer_payload_without_the_field_still_parses(self):
        data = EdgeRunner().to_dict(DetectionMessage(timestamp=1.0, frame_id=1))
        assert EdgeRunner.from_dict(data).face_model is None


class TestGroundIgnoresMismatchedModel:
    def _station(self, ground_model="buffalo_l"):
        return GroundStation(target_face_embedding=[1.0, 0.0], face_model=ground_model)

    def test_matching_model_is_scored(self):
        res = self._station().process_message(_msg("buffalo_l"))
        assert res[0]["face_score"] == pytest.approx(1.0)

    def test_unknown_edge_model_is_scored_as_before(self):
        res = self._station().process_message(_msg(None))
        assert res[0]["face_score"] == pytest.approx(1.0)

    def test_mismatch_is_ignored_and_logged_once(self, caplog):
        st = self._station("buffalo_l")
        with caplog.at_level(logging.ERROR):
            a = st.process_message(_msg("buffalo_s"))
            b = st.process_message(_msg("buffalo_s"))
        assert a[0]["face_score"] == 0.0 and b[0]["face_score"] == 0.0
        assert len([r for r in caplog.records if "face_model_mismatch" in r.getMessage()]) == 1

    def test_ground_without_known_model_does_not_block(self):
        res = self._station(None).process_message(_msg("buffalo_s"))
        assert res[0]["face_score"] == pytest.approx(1.0)


class TestLoadModels:
    def test_refuses_to_download_by_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(face_mod, "_pack_dir", lambda n: str(tmp_path / n))
        monkeypatch.delenv("FLIGHTRISK_ALLOW_MODEL_DOWNLOAD", raising=False)
        with pytest.raises(RuntimeError, match="FLIGHTRISK_ALLOW_MODEL_DOWNLOAD"):
            face_mod._load_models("no_such_pack", ["CPUExecutionProvider"])

    def test_download_only_with_explicit_opt_in(self, monkeypatch, tmp_path):
        pytest.importorskip("insightface")
        calls = []
        monkeypatch.setattr(face_mod, "_pack_dir", lambda n: str(tmp_path / n))
        monkeypatch.setenv("FLIGHTRISK_ALLOW_MODEL_DOWNLOAD", "1")

        def fake_ensure(sub, name, **kw):
            calls.append(name)
            raise RuntimeError("stop here, download attempted")

        import insightface.utils.storage as storage
        monkeypatch.setattr(storage, "ensure_available", fake_ensure)
        with pytest.raises(RuntimeError, match="download attempted"):
            face_mod._load_models("no_such_pack", ["CPUExecutionProvider"])
        assert calls == ["no_such_pack"]

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "", ".."])
    def test_rejects_path_like_model_names(self, bad):
        with pytest.raises(ValueError):
            face_mod._pack_dir(bad)

    def test_loads_only_detector_and_recogniser_files(self, monkeypatch, tmp_path):
        pytest.importorskip("insightface")
        pack = tmp_path / "pack"
        pack.mkdir()
        for n in ("det_10g.onnx", "w600k_r50.onnx", "1k3d68.onnx", "2d106det.onnx", "genderage.onnx"):
            (pack / n).write_bytes(b"x")
        monkeypatch.setattr(face_mod, "_pack_dir", lambda n: str(pack))
        loaded = []
        from insightface.model_zoo import model_zoo

        def fake_get_model(path, **kw):
            loaded.append((path.rsplit("/", 1)[-1], tuple(kw["providers"])))
            return object()

        monkeypatch.setattr(model_zoo, "get_model", fake_get_model)
        models = face_mod._load_models("pack", ["CoreMLExecutionProvider", "CPUExecutionProvider"])
        assert set(models) == {"detection", "recognition"}
        assert loaded == [("det_10g.onnx", ("CPUExecutionProvider",)),
                          ("w600k_r50.onnx", ("CoreMLExecutionProvider", "CPUExecutionProvider"))]


class TestDetThresh:
    def _rec(self, det):
        r = face_mod.FaceRecognizer.__new__(face_mod.FaceRecognizer)
        r._lock = threading.RLock()
        r._det = det
        r._det_accepts_thresh = face_mod._accepts_kwarg(det.detect, "det_thresh")
        return r

    def test_accepts_kwarg(self):
        assert face_mod._accepts_kwarg(lambda img, det_thresh=None: 0, "det_thresh")
        assert face_mod._accepts_kwarg(lambda img, **kw: 0, "det_thresh")
        assert not face_mod._accepts_kwarg(lambda img, input_size=None: 0, "det_thresh")

    def test_attribute_path_used_directly_without_raising_and_restored(self):
        seen = []

        class Det:
            det_thresh = 0.5

            def detect(self, img, input_size=None, max_num=0, metric="default"):
                seen.append((self.det_thresh, input_size))
                return np.empty((0, 5), np.float32), None

        det = Det()
        r = self._rec(det)
        r._detect(np.zeros((64, 64, 3), np.uint8), 64, thresh=0.3)
        assert seen == [(0.3, (64, 64))]      # one call, threshold in effect
        assert det.det_thresh == 0.5          # restored

    def test_kwarg_path_when_detector_supports_it(self):
        seen = []

        class Det:
            def detect(self, img, input_size=None, det_thresh=None):
                seen.append(det_thresh)
                return np.empty((0, 5), np.float32), None

        r = self._rec(Det())
        r._detect(np.zeros((64, 64, 3), np.uint8), 64, thresh=0.3)
        assert seen == [0.3]
