"""Store-safe mobile model pipeline (scripts/mobile_models): pins, fail-closed checksums,
the licence guard, the preprocessing contracts and the vendored YOLOX code.

Nothing here downloads anything or needs the real weights. Downloads are served by a fake
opener from bytes made in the test. The conversion tests at the end build small or
randomly initialised models and need the pinned conversion tools
(scripts/mobile_models/requirements.txt, coremltools in particular); they are skipped
when those are not installed, which is the case in FlightRisk's own venv.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "scripts" / "mobile_models"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import licence_guard as lg  # noqa: E402
import make_models as mm  # noqa: E402
import mobile_preprocess as mp  # noqa: E402

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


class TestPins:
    def test_every_download_is_pinned_by_full_sha256_and_size(self):
        assert len(mm.DOWNLOADS) == 3
        for d in mm.DOWNLOADS:
            assert HEX64.match(d.sha256), d.key
            assert d.size > 0, d.key
            assert d.url.startswith("https://"), d.key

    def test_urls_name_fixed_releases_and_commits(self):
        assert "/releases/download/0.1.1rc0/yolox_s.pth" in mm.YOLOX_S_WEIGHTS.url
        assert HEX40.match(mm.CLIP_HF_REVISION)
        assert f"/resolve/{mm.CLIP_HF_REVISION}/" in mm.CLIP_WEIGHTS.url
        assert HEX40.match(mm.YOLOX_COMMIT)
        assert mm.YOLOX_COMMIT in mm.YOLOX_SAMPLE_IMAGE.url

    def test_the_recorded_checksums(self):
        # The values the PR records; changing a pin must be a visible, reviewed change.
        assert mm.YOLOX_S_WEIGHTS.sha256 == "f55ded7181e1b0c13285c56e7790b8f0e8f8db590fe4edb37f0b7f345c913a30"
        assert mm.CLIP_WEIGHTS.sha256 == "3f25d29d3cc74e1d25d47e0593b4dd0864ced1e9b4d8e486a247ca4502f227f1"
        assert mm.CLIP_HF_REVISION == "7288da5a0d6f0b51c4a2b27c624837a9236d0112"

    def test_the_embedder_is_the_grounds_model(self):
        from flightrisk.vision import reid_spec

        assert mp.CLIP_MODEL_ID == f"{reid_spec.REID_LIBRARY}:{reid_spec.REID_DEFAULT_MODEL}:{reid_spec.REID_PRETRAINED}"
        assert mp.CLIP_INPUT_SIZE == reid_spec.REID_INPUT_SIZE
        assert mp.CLIP_EMBEDDING_DIM == reid_spec.REID_EMBEDDING_DIM
        assert mp.CLIP_MEAN == reid_spec.REID_MEAN
        assert mp.CLIP_STD == reid_spec.REID_STD

    def test_the_hugging_face_repository_is_the_one_open_clip_names(self):
        open_clip = pytest.importorskip("open_clip")
        cfg = open_clip.get_pretrained_cfg("ViT-B-16", "laion2b_s34b_b88k")
        assert cfg["hf_hub"].rstrip("/") == mm.CLIP_HF_REPO
        assert tuple(cfg["mean"]) == mp.CLIP_MEAN and tuple(cfg["std"]) == mp.CLIP_STD

    def test_requirements_pin_every_tool_exactly(self):
        pins = {}
        for line in (PIPELINE / "requirements.txt").read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name, sep, version = line.partition("==")
            assert sep == "==", f"not pinned exactly: {line}"
            pins[name.strip().lower().replace("_", "-")] = version.strip()
        expected = {n.lower().replace("_", "-"): v for n, v in mm.PINNED_TOOLS}
        assert pins == expected
        for tool in ("torch", "open-clip-torch", "onnx", "onnxruntime", "coremltools"):
            assert tool in pins

    def test_the_pipeline_never_installs_or_imports_ultralytics_or_insightface(self):
        text = "\n".join(p.read_text() for p in (PIPELINE / "requirements.txt", PIPELINE / "make_models.py"))
        assert not re.search(r"^\s*(import|from)\s+(ultralytics|insightface)", text, re.M)
        assert "ultralytics" not in (PIPELINE / "requirements.txt").read_text().lower()
        assert "insightface" not in (PIPELINE / "requirements.txt").read_text().lower()

    def test_the_script_imports_no_heavy_dependency_at_load(self):
        import subprocess

        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import make_models; "
            "heavy = [m for m in ('torch', 'onnx', 'onnxruntime', 'coremltools', 'open_clip', 'cv2') if m in sys.modules]; "
            "print(','.join(heavy))"
        )
        out = subprocess.run([sys.executable, "-c", code, str(PIPELINE)], capture_output=True, text=True, check=True)
        assert out.stdout.strip() == ""

    def test_help_runs_without_the_conversion_tools(self):
        assert mm.build_parser().parse_args(["fetch"]).command == "fetch"
        with pytest.raises(SystemExit) as exc:
            mm.main(["--help"])
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# Checksums and downloads fail closed
# ---------------------------------------------------------------------------

PAYLOAD = b"pinned model bytes " * 1000


def _pin(data: bytes = PAYLOAD, name: str = "weights.bin") -> mm.Download:
    return mm.Download(
        key="test", url="https://example.invalid/weights.bin", sha256=hashlib.sha256(data).hexdigest(),
        size=len(data), filename=name, licence="MIT", what="test file",
    )


class _Opener:
    """A stand-in for urlopen that serves ``body`` and counts calls."""

    def __init__(self, body: bytes | Exception):
        self.body = body
        self.calls = 0

    def __call__(self, url, timeout=None):
        self.calls += 1
        if isinstance(self.body, Exception):
            raise self.body
        return io.BytesIO(self.body)


def _leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".part"))


class TestFetch:
    def test_a_matching_download_is_kept(self, tmp_path):
        pin = _pin()
        path = mm.fetch_one(pin, tmp_path, opener=_Opener(PAYLOAD))
        assert path.read_bytes() == PAYLOAD
        assert _leftovers(tmp_path) == []

    def test_a_different_download_of_the_same_size_is_refused_and_deleted(self, tmp_path):
        pin = _pin()
        bad = bytearray(PAYLOAD)
        bad[100] ^= 1
        with pytest.raises(mm.ChecksumMismatch, match="SHA-256"):
            mm.fetch_one(pin, tmp_path, opener=_Opener(bytes(bad)))
        assert not (tmp_path / pin.filename).exists()
        assert _leftovers(tmp_path) == []

    def test_an_oversized_download_is_cut_off_and_refused(self, tmp_path):
        pin = _pin()
        with pytest.raises(mm.ChecksumMismatch, match="more than the pinned"):
            mm.fetch_one(pin, tmp_path, opener=_Opener(PAYLOAD + b"x" * (2 << 20)))
        assert not (tmp_path / pin.filename).exists()
        assert _leftovers(tmp_path) == []

    def test_a_short_download_is_refused(self, tmp_path):
        pin = _pin()
        with pytest.raises(mm.ChecksumMismatch, match="bytes, expected"):
            mm.fetch_one(pin, tmp_path, opener=_Opener(PAYLOAD[:-1]))
        assert not (tmp_path / pin.filename).exists()

    def test_a_network_failure_says_so_and_leaves_nothing(self, tmp_path):
        pin = _pin()
        with pytest.raises(mm.DownloadFailed):
            mm.fetch_one(pin, tmp_path, opener=_Opener(OSError("network is unreachable")))
        assert not (tmp_path / pin.filename).exists()
        assert _leftovers(tmp_path) == []

    def test_a_verified_file_is_not_downloaded_again(self, tmp_path):
        pin = _pin()
        (tmp_path / pin.filename).write_bytes(PAYLOAD)
        opener = _Opener(b"never served")
        mm.fetch_one(pin, tmp_path, opener=opener)
        assert opener.calls == 0

    def test_a_wrong_file_in_place_is_replaced_by_the_verified_download(self, tmp_path):
        pin = _pin()
        (tmp_path / pin.filename).write_bytes(b"tampered")
        mm.fetch_one(pin, tmp_path, opener=_Opener(PAYLOAD))
        assert (tmp_path / pin.filename).read_bytes() == PAYLOAD

    def test_a_wrong_file_in_place_is_removed_even_when_the_download_fails(self, tmp_path):
        pin = _pin()
        (tmp_path / pin.filename).write_bytes(b"tampered")
        with pytest.raises(mm.ChecksumMismatch):
            mm.fetch_one(pin, tmp_path, opener=_Opener(b"also wrong"))
        assert not (tmp_path / pin.filename).exists()


class TestReadVerified:
    def test_returns_the_bytes_that_were_checked(self, tmp_path):
        pin = _pin()
        (tmp_path / pin.filename).write_bytes(PAYLOAD)
        assert mm.read_verified(pin, tmp_path) == PAYLOAD

    def test_missing_file_names_the_fetch_step(self, tmp_path):
        with pytest.raises(mm.ChecksumMismatch, match="make_models.py fetch"):
            mm.read_verified(_pin(), tmp_path)

    def test_one_flipped_byte_is_refused(self, tmp_path):
        pin = _pin()
        bad = bytearray(PAYLOAD)
        bad[-1] ^= 0x80
        (tmp_path / pin.filename).write_bytes(bytes(bad))
        with pytest.raises(mm.ChecksumMismatch, match="SHA-256"):
            mm.read_verified(pin, tmp_path)

    def test_wrong_size_is_refused_before_reading(self, tmp_path):
        pin = _pin()
        (tmp_path / pin.filename).write_bytes(PAYLOAD + b"!")
        with pytest.raises(mm.ChecksumMismatch, match="bytes, expected"):
            mm.read_verified(pin, tmp_path)

    def test_the_detector_weights_are_never_unpickled_unverified(self, tmp_path, monkeypatch):
        torch = pytest.importorskip("torch")
        calls = []
        monkeypatch.setattr(torch, "load", lambda *a, **k: calls.append(a) or {})
        (tmp_path / mm.YOLOX_S_WEIGHTS.filename).write_bytes(b"\x80\x04 not the pinned checkpoint")
        with pytest.raises(mm.ChecksumMismatch):
            mm.load_yolox_s(tmp_path)
        assert calls == []

    def test_convert_refuses_before_loading_anything(self, tmp_path, monkeypatch, capsys):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        for d in mm.DOWNLOADS:
            (downloads / d.filename).write_bytes(b"not it")
        monkeypatch.setattr(mm, "check_tool_versions", lambda allow: {})
        touched = []
        monkeypatch.setattr(mm, "convert_yolox", lambda *a, **k: touched.append("yolox"))
        monkeypatch.setattr(mm, "convert_clip", lambda *a, **k: touched.append("clip"))
        assert mm.main(["convert", "--work-dir", str(tmp_path)]) == 1
        assert touched == []
        assert "ChecksumMismatch" in capsys.readouterr().err


class TestToolVersions:
    def test_a_different_version_is_refused(self, monkeypatch):
        found = dict(mm.PINNED_TOOLS)
        found["coremltools"] = "8.3.0"
        monkeypatch.setattr(mm, "installed_versions", lambda names: dict(found))
        with pytest.raises(mm.ToolVersionMismatch, match="coremltools 8.3.0"):
            mm.check_tool_versions(allow_unpinned=False)
        assert mm.check_tool_versions(allow_unpinned=True)["coremltools"] == "8.3.0"

    def test_a_missing_tool_is_refused(self, monkeypatch):
        found = dict(mm.PINNED_TOOLS)
        found["onnxruntime"] = None
        monkeypatch.setattr(mm, "installed_versions", lambda names: dict(found))
        with pytest.raises(mm.ToolVersionMismatch, match="onnxruntime missing"):
            mm.check_tool_versions(allow_unpinned=False)

    def test_the_pinned_set_passes(self, monkeypatch):
        monkeypatch.setattr(mm, "installed_versions", lambda names: dict(mm.PINNED_TOOLS))
        assert mm.check_tool_versions(allow_unpinned=False) == dict(mm.PINNED_TOOLS)


class TestTreeDigest:
    def test_a_folder_digest_covers_names_and_contents(self, tmp_path):
        pkg = tmp_path / "M.mlpackage"
        (pkg / "Data").mkdir(parents=True)
        (pkg / "Manifest.json").write_text("{}")
        (pkg / "Data" / "weights.bin").write_bytes(b"abc")
        first = mm.tree_sha256(pkg)
        (pkg / "Data" / "weights.bin").write_bytes(b"abd")
        second = mm.tree_sha256(pkg)
        (pkg / "Data" / "weights.bin").rename(pkg / "Data" / "weight.bin")
        third = mm.tree_sha256(pkg)
        assert len({first, second, third}) == 3
        assert mm.tree_size(pkg) == 5


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _fake_outputs(work: Path, passed: bool = True) -> dict:
    """Small stand-ins for every output, and a verify.json that passed them."""
    out = work / "out"
    report = {"passed": passed, "outputs": {}, "detector": {"outputs": {}}, "embedder": {"outputs": {}}}
    for o in mm.OUTPUTS:
        path = out / o.platform / o.filename
        if o.filename.endswith(".mlpackage"):
            (path / "Data").mkdir(parents=True, exist_ok=True)
            (path / "Manifest.json").write_text(json.dumps({"name": o.filename}))
            (path / "Data" / "model.mlmodel").write_bytes(o.filename.encode() * 3)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(o.filename.encode() * 5)
        report["outputs"][o.key] = {"sha256": mm.tree_sha256(path), "bytes": mm.tree_size(path)}
    (work / mm.VERIFY_NAME).write_text(json.dumps(report))
    return report


class TestInstall:
    def _run(self, tmp_path):
        android, ios = tmp_path / "android-assets", tmp_path / "ios-models"
        installed = mm.run_install(tmp_path / "work", android, ios, "all", {}, lambda m: None)
        return android, ios, installed

    def test_installs_the_default_variants_under_fixed_names(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        android, ios, installed = self._run(tmp_path)
        assert sorted(p.name for p in android.iterdir()) == [
            "clip_vit_b16_laion2b.onnx", mm.MANIFEST_NAME, mm.NOTICES_INSTALLED_NAME, "yolox_s.onnx",
        ]
        assert sorted(p.name for p in ios.iterdir()) == [
            "CLIPViTB16LAION.mlpackage", "YOLOXs.mlpackage", mm.MANIFEST_NAME, mm.NOTICES_INSTALLED_NAME,
        ]
        # the defaults: fp32 detector and weight-only int8 embedder on Android, fp16 on iOS
        assert (android / "yolox_s.onnx").read_bytes() == b"yolox_s.onnx" * 5
        assert (android / "clip_vit_b16_laion2b.onnx").read_bytes() == b"clip_vit_b16_laion2b_w8.onnx" * 5
        manifest = json.loads((ios / mm.MANIFEST_NAME).read_text())
        assert {m["file"]: m["variant"] for m in manifest["models"]} == {
            "YOLOXs.mlpackage": "fp16", "CLIPViTB16LAION.mlpackage": "fp16",
        }
        assert manifest["contract"]["detector"]["letterbox"]["pad_value"] == 114
        assert manifest["contract"]["embedder"]["model"] == "open_clip:ViT-B-16:laion2b_s34b_b88k"
        assert (android / mm.NOTICES_INSTALLED_NAME).read_text() == (PIPELINE / "NOTICES.md").read_text()

    def test_a_variant_can_be_chosen(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        overrides = {("android", "detector"): "fp16", ("ios", "embedder"): "palettized8"}
        android, ios = tmp_path / "a", tmp_path / "i"
        mm.run_install(tmp_path / "work", android, ios, "all", overrides, lambda m: None)
        assert (android / "yolox_s.onnx").read_bytes() == b"yolox_s_fp16.onnx" * 5
        manifest = json.loads((ios / mm.MANIFEST_NAME).read_text())
        clip = next(m for m in manifest["models"] if m["role"] == "embedder")
        assert clip == {**clip, "file": "CLIPViTB16LAION.mlpackage", "variant": "palettized8",
                        "built_as": "CLIPViTB16LAION8bit.mlpackage"}

    def test_reinstall_replaces_the_previous_package(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        android, ios, _ = self._run(tmp_path)
        (ios / "YOLOXs.mlpackage" / "stale.bin").write_bytes(b"old")
        self._run(tmp_path)
        assert not (ios / "YOLOXs.mlpackage" / "stale.bin").exists()

    def test_nothing_is_installed_without_a_verification(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        (tmp_path / "work" / mm.VERIFY_NAME).unlink()
        with pytest.raises(mm.VerificationFailed, match="run verify first"):
            self._run(tmp_path)
        assert not (tmp_path / "android-assets").exists()

    def test_nothing_is_installed_after_a_failed_verification(self, tmp_path):
        _fake_outputs(tmp_path / "work", passed=False)
        with pytest.raises(mm.VerificationFailed, match="failed verification"):
            self._run(tmp_path)
        assert not (tmp_path / "android-assets").exists()

    def test_an_output_changed_after_verification_is_refused(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        (tmp_path / "work" / "out" / "android" / "yolox_s.onnx").write_bytes(b"swapped after verify")
        with pytest.raises(mm.VerificationFailed, match="not the file verify passed"):
            self._run(tmp_path)
        assert not (tmp_path / "android-assets").exists()

    def test_an_unverified_variant_is_refused(self, tmp_path):
        report = _fake_outputs(tmp_path / "work")
        del report["outputs"]["android/clip_vit_b16_laion2b_w8.onnx"]
        (tmp_path / "work" / mm.VERIFY_NAME).write_text(json.dumps(report))
        with pytest.raises(mm.VerificationFailed, match="was not verified"):
            self._run(tmp_path)

    def test_a_forbidden_model_already_in_the_app_folder_fails_the_install(self, tmp_path, capsys):
        _fake_outputs(tmp_path / "work")
        android = tmp_path / "android-assets"
        android.mkdir()
        (android / "yolo11n.onnx").write_bytes(b"stale")
        rc = mm.main(["install", "--work-dir", str(tmp_path / "work"), "--android", str(android),
                      "--ios", str(tmp_path / "ios-models")])
        assert rc == 1
        err = capsys.readouterr().err
        assert "yolo11n.onnx" in err and "forbidden" in err

    def test_targets_android_installs_only_android(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        android, ios = tmp_path / "a", tmp_path / "i"
        mm.run_install(tmp_path / "work", android, ios, "android", {}, lambda m: None)
        assert android.is_dir() and not ios.exists()

    def test_the_install_defaults_are_the_app_folders_git_ignores(self):
        import subprocess

        assert mm.DEFAULT_ANDROID_DIR == ROOT / "mobile" / "app" / "src" / "main" / "assets"
        assert mm.DEFAULT_IOS_DIR == ROOT / "ios" / "FlightRisk" / "Models"
        paths = [
            "mobile/app/src/main/assets/yolox_s.onnx",
            "mobile/app/src/main/assets/clip_vit_b16_laion2b.onnx",
            f"mobile/app/src/main/assets/{mm.MANIFEST_NAME}",
            f"mobile/app/src/main/assets/{mm.NOTICES_INSTALLED_NAME}",
            "ios/FlightRisk/Models/YOLOXs.mlpackage/Manifest.json",
            "ios/FlightRisk/Models/CLIPViTB16LAION.mlpackage/Manifest.json",
            f"ios/FlightRisk/Models/{mm.MANIFEST_NAME}",
            f"ios/FlightRisk/Models/{mm.NOTICES_INSTALLED_NAME}",
            "build/mobile_models/downloads/yolox_s.pth",
        ]
        out = subprocess.run(["git", "check-ignore", *paths], cwd=ROOT, capture_output=True, text=True)
        assert sorted(out.stdout.split()) == sorted(paths)


# ---------------------------------------------------------------------------
# Licence guard
# ---------------------------------------------------------------------------

FORBIDDEN = [
    "yolo11n.onnx", "yolov8n.pt", "YOLOPersonDetector.mlmodelc", "best_yolov5s.tflite", "YOLO11n.mlpackage",
    "ultralytics_export.onnx", "scrfd_500m.onnx", "SCRFDFaceDetector.mlpackage", "arcface_mobilefacenet.onnx",
    "ArcFaceMobile.mlmodelc", "insightface_r18.tflite", "InsightFaceR18.mlpackage", "buffalo_sc.zip",
    "buffalo_l", "mobileclip_s2.onnx", "MobileCLIP-S2.mlpackage", "osnet_x1_0.onnx", "OSNetReID.mlpackage",
    "det_500m.onnx", "w600k_mbf.onnx", "det_2.5g.onnx", "glintr100.onnx", "clip_visual.onnx", "CLIPVisual.mlmodelc",
]
ALLOWED = [
    "yolox_s.onnx", "yolox_s_fp16.onnx", "YOLOXs.mlpackage", "YOLOXs.mlmodelc", "yolox-nano.onnx",
    "clip_vit_b16_laion2b.onnx", "clip_vit_b16_laion2b_w8.onnx", "CLIPViTB16LAION.mlpackage",
    "CLIPViTB16LAION8bit.mlmodelc", "midas_small.onnx", "mobile-models.json", "model-notices.md", ".gitkeep",
    "alarm.mp3", "classes.dex", "Info.plist",
]


class TestLicenceGuardNames:
    @pytest.mark.parametrize("name", FORBIDDEN)
    def test_forbidden(self, name):
        assert lg.check_name(name) is not None

    @pytest.mark.parametrize("name", ALLOWED)
    def test_allowed(self, name):
        assert lg.check_name(name) is None

    def test_every_pipeline_output_is_allowed(self):
        for o in mm.OUTPUTS:
            assert lg.check_name(o.filename) is None
            assert lg.check_name(o.installed_name) is None

    def test_every_output_of_download_matching_models_is_forbidden(self):
        script = (ROOT / "scripts" / "download_matching_models.sh").read_text()
        names = {Path(m).name for m in re.findall(r'list_file\s+"\$REPO_ROOT/([^"]+)"', script)}
        names |= {Path(m).name for m in re.findall(r"^#\s+((?:models|ios|mobile)/\S+)", script, re.M)}
        assert len(names) >= 6
        for name in sorted(names):
            assert lg.check_name(name) is not None, name

    def test_every_model_the_apps_load_is_classified(self):
        """A model name in the app code is either forbidden or one this pipeline (or MiDaS) provides."""
        allowed_in_apps = {o.installed_name for o in mm.OUTPUTS} | {"midas_small.onnx"}
        allowed_in_apps |= {Path(n).stem for n in allowed_in_apps}
        found: set[str] = set()
        for src in list((ROOT / "mobile").rglob("*.kt")) + list((ROOT / "ios").rglob("*.swift")):
            text = src.read_text(errors="replace")
            found |= set(re.findall(r'"([A-Za-z0-9_.\-]+\.(?:onnx|tflite|ort))"', text))
            found |= set(re.findall(r'forResource:\s*"([A-Za-z0-9_\-]+)",\s*withExtension:\s*"mlmodelc"', text))
            found |= set(re.findall(r'modelName:\s*String\s*=\s*"([A-Za-z0-9_\-]+)"', text))
        assert found, "no model names found in the app code"
        unclassified = sorted(n for n in found if lg.check_name(n) is None and n not in allowed_in_apps)
        assert unclassified == []


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


class TestLicenceGuardScan:
    def test_a_clean_asset_folder_passes(self, tmp_path, capsys):
        for name in ("yolox_s.onnx", "clip_vit_b16_laion2b.onnx", "midas_small.onnx", ".gitkeep"):
            (tmp_path / name).write_bytes(b"\x08\x08 model")
        assert lg.main([str(tmp_path)]) == 0
        assert "no forbidden model files" in capsys.readouterr().out

    def test_a_forbidden_file_fails_and_is_named(self, tmp_path, capsys):
        (tmp_path / "yolox_s.onnx").write_bytes(b"ok")
        (tmp_path / "yolo11n.onnx").write_bytes(b"no")
        assert lg.main([str(tmp_path)]) == 1
        err = capsys.readouterr().err
        assert "yolo11n.onnx" in err and "yolox_s.onnx" not in err

    def test_a_forbidden_folder_name_is_caught(self, tmp_path):
        (tmp_path / "insightface" / "models").mkdir(parents=True)
        (tmp_path / "insightface" / "models" / "model.onnx").write_bytes(b"x")
        findings = lg.scan([tmp_path])
        assert [Path(f.location).name for f in findings] == ["insightface"]

    def test_a_built_app_bundle_with_a_compiled_forbidden_model_fails(self, tmp_path):
        app = tmp_path / "FlightRisk.app"
        (app / "YOLOXs.mlmodelc").mkdir(parents=True)
        (app / "YOLOXs.mlmodelc" / "coremldata.bin").write_bytes(b"ok")
        assert lg.scan([app]) == []
        (app / "SCRFDFaceDetector.mlmodelc").mkdir()
        (app / "SCRFDFaceDetector.mlmodelc" / "coremldata.bin").write_bytes(b"x")
        findings = lg.scan([app])
        assert len(findings) == 1 and findings[0].rule == "name:scrfdfacedetector"

    def test_an_apk_is_read_entry_by_entry(self, tmp_path):
        clean = _zip(tmp_path / "clean.apk", {"assets/yolox_s.onnx": b"x", "classes.dex": b"dex", "res/raw/alarm.mp3": b"a"})
        dirty = _zip(tmp_path / "dirty.apk", {"assets/yolox_s.onnx": b"x", "assets/arcface_mobilefacenet.onnx": b"y"})
        assert lg.scan([clean]) == []
        findings = lg.scan([dirty])
        assert [f.location for f in findings] == [f"{dirty}!assets/arcface_mobilefacenet.onnx"]

    def test_an_aab_and_an_ipa_are_read(self, tmp_path):
        aab = _zip(tmp_path / "app.aab", {"base/assets/w600k_mbf.onnx": b"x"})
        ipa = _zip(tmp_path / "app.ipa", {"Payload/FlightRisk.app/YOLOPersonDetector.mlmodelc/coremldata.bin": b"x"})
        assert lg.main([str(aab)]) == 1
        assert lg.main([str(ipa)]) == 1

    def test_an_archive_inside_a_folder_is_read(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        _zip(dist / "app-release.apk", {"assets/mobileclip_s2.onnx": b"x"})
        findings = lg.scan([dist])
        assert len(findings) == 1 and findings[0].location.endswith("!assets/mobileclip_s2.onnx")

    def test_a_nested_archive_is_read(self, tmp_path):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("assets/scrfd_500m.onnx", b"x")
        outer = _zip(tmp_path / "bundle.apks", {"splits/base-master.apk": inner.getvalue()})
        findings = lg.scan([outer])
        assert len(findings) == 1 and findings[0].location.endswith("splits/base-master.apk!assets/scrfd_500m.onnx")

    def test_a_renamed_ultralytics_export_is_caught_by_its_metadata(self, tmp_path):
        # Ultralytics ONNX exports record author "Ultralytics" in metadata_props.
        assets = tmp_path / "assets"  # pytest's folder for this test has "ultralytics" in its name
        assets.mkdir()
        (assets / "person_detector.onnx").write_bytes(b"\x08\x07" + b"\x00" * 5000 + b"author\x12\x0bUltralytics" + b"\x00" * 10)
        findings = lg.scan([assets])
        assert len(findings) == 1 and findings[0].rule == "content:ultralytics"

    def test_a_marker_across_a_read_boundary_is_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lg, "_CHUNK", 16)
        data = b"x" * 13 + b"InsightFace" + b"y" * 40
        (tmp_path / "recogniser.bin").write_bytes(data)
        assert [f.rule for f in lg.scan([tmp_path])] == ["content:insightface"]

    def test_files_inside_a_core_ml_package_are_searched(self, tmp_path):
        pkg = tmp_path / "Detector.mlpackage" / "Data" / "com.apple.CoreML"
        pkg.mkdir(parents=True)
        (pkg / "model.mlmodel").write_bytes(b"...license AGPL-3.0 ... author Ultralytics ...")
        assert [f.rule for f in lg.scan([tmp_path])] == ["content:ultralytics"]

    def test_a_marker_in_documentation_is_not_a_model(self, tmp_path):
        (tmp_path / "model-notices.md").write_text("No InsightFace, MobileCLIP or Ultralytics model ships.")
        (tmp_path / "mobile-models.json").write_text('{"note": "no ultralytics"}')
        assert lg.scan([tmp_path]) == []

    def test_the_shipped_notices_pass(self, tmp_path):
        (tmp_path / mm.NOTICES_INSTALLED_NAME).write_text((PIPELINE / "NOTICES.md").read_text())
        assert lg.scan([tmp_path]) == []

    def test_a_missing_path_is_an_error_not_a_pass(self, tmp_path, capsys):
        assert lg.main([str(tmp_path / "nope")]) == 2
        assert "does not exist" in capsys.readouterr().err

    def test_a_broken_archive_is_an_error_not_a_pass(self, tmp_path):
        (tmp_path / "broken.apk").write_bytes(b"PK\x03\x04 truncated")
        assert lg.main([str(tmp_path / "broken.apk")]) == 2

    def test_the_command_line_runs_without_the_conversion_tools(self, tmp_path):
        import subprocess

        (tmp_path / "yolov8n.tflite").write_bytes(b"x")
        out = subprocess.run([sys.executable, str(PIPELINE / "licence_guard.py"), str(tmp_path)],
                             capture_output=True, text=True)
        assert out.returncode == 1 and "yolov8n.tflite" in out.stderr

    def test_the_guard_uses_only_the_standard_library(self):
        import ast

        tree = ast.parse((PIPELINE / "licence_guard.py").read_text())
        modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert modules <= set(sys.stdlib_module_names) | {"__future__"}


# ---------------------------------------------------------------------------
# Preprocessing contracts
# ---------------------------------------------------------------------------


def _upstream_preproc(img, input_size, swap=(2, 0, 1)):
    """yolox/data/data_augment.py preproc at YOLOX 6ddff48 (Apache-2.0, Megvii Inc.), for comparison."""
    import cv2

    if len(img.shape) == 3:
        padded_img = np.ones((input_size[0], input_size[1], 3), dtype=np.uint8) * 114
    else:
        padded_img = np.ones(input_size, dtype=np.uint8) * 114

    r = min(input_size[0] / img.shape[0], input_size[1] / img.shape[1])
    resized_img = cv2.resize(
        img,
        (int(img.shape[1] * r), int(img.shape[0] * r)),
        interpolation=cv2.INTER_LINEAR,
    ).astype(np.uint8)
    padded_img[: int(img.shape[0] * r), : int(img.shape[1] * r)] = resized_img

    padded_img = padded_img.transpose(swap)
    padded_img = np.ascontiguousarray(padded_img, dtype=np.float32)
    return padded_img, r


class TestDetectorPreprocessing:
    @pytest.mark.parametrize("shape", [(480, 640), (1080, 1920), (720, 1280), (640, 640), (100, 333), (333, 100), (37, 41), (2000, 30)])
    def test_matches_upstream_preproc(self, shape):
        pytest.importorskip("cv2")
        rng = np.random.default_rng(sum(shape))
        img = rng.integers(0, 256, size=(*shape, 3), dtype=np.uint8)
        canvas, ratio = mp.yolox_letterbox(img)
        ours = mp.yolox_input_tensor(canvas)
        theirs, r = _upstream_preproc(img, (640, 640))
        assert ratio == r
        np.testing.assert_array_equal(ours[0], theirs)
        assert ours.dtype == np.float32 and ours.shape == (1, 3, 640, 640)

    def test_image_at_top_left_and_padding_114(self):
        pytest.importorskip("cv2")
        img = np.zeros((360, 640, 3), dtype=np.uint8)
        img[..., 0] = 10  # blue
        img[..., 2] = 200  # red
        canvas, ratio = mp.yolox_letterbox(img)
        assert ratio == 1.0
        assert (canvas[:360] == img).all()
        assert (canvas[360:] == 114).all()
        x = mp.yolox_input_tensor(canvas)
        # channels stay BGR and 0..255, not normalised
        assert x[0, 0, 0, 0] == 10.0 and x[0, 2, 0, 0] == 200.0 and x[0, 1, 639, 0] == 114.0

    def test_scale_truncates_like_upstream(self):
        assert mp.yolox_scale(1080, 1920) == (1 / 3, 360, 640)
        r, h, w = mp.yolox_scale(100, 333)
        assert (h, w) == (int(100 * r), int(333 * r))
        with pytest.raises(ValueError):
            mp.yolox_scale(0, 10)

    def test_anchor_grid_order(self):
        cells, strides = mp.yolox_anchor_grid()
        assert len(cells) == mp.YOLOX_NUM_ANCHORS == 8400
        assert cells[0].tolist() == [0, 0] and strides[0] == 8
        assert cells[1].tolist() == [1, 0]  # x varies fastest
        assert cells[80].tolist() == [0, 1]
        assert cells[6400].tolist() == [0, 0] and strides[6400] == 16
        assert cells[8000].tolist() == [0, 0] and strides[8000] == 32
        assert cells[-1].tolist() == [19, 19] and strides[-1] == 32

    @staticmethod
    def _row(cx, cy, w, h, obj, cls, p):
        row = np.zeros(85, dtype=np.float32)
        row[:5] = [cx, cy, w, h, obj]
        row[5 + cls] = p
        return row

    def test_postprocess_scores_thresholds_and_scales(self):
        raw = np.stack([
            self._row(100, 100, 40, 80, 0.9, 0, 0.9),  # person, 0.81
            self._row(102, 101, 40, 80, 0.8, 0, 0.9),  # overlaps the first: suppressed
            self._row(102, 101, 40, 80, 0.8, 16, 0.9),  # same box, other class: kept
            self._row(300, 300, 20, 20, 0.5, 0, 0.4),  # 0.20 < 0.25: dropped
        ])
        dets = mp.yolox_postprocess(raw, ratio=0.5)
        assert [(d["class"], round(d["score"], 2)) for d in dets] == [(0, 0.81), (16, 0.72)]
        assert dets[0]["box"] == pytest.approx([160, 120, 240, 280])  # (cx -/+ w/2) / ratio
        persons = mp.yolox_postprocess(raw, ratio=0.5, classes=(mp.COCO_PERSON,))
        assert [d["class"] for d in persons] == [0]

    def test_postprocess_rejects_a_wrong_layout(self):
        with pytest.raises(ValueError):
            mp.yolox_postprocess(np.zeros((8400, 84)), 1.0)

    def test_box_iou(self):
        a = np.array([[0, 0, 10, 10]])
        b = np.array([[0, 0, 10, 10], [5, 0, 15, 10], [20, 20, 30, 30]])
        assert mp.box_iou(a, b)[0].tolist() == pytest.approx([1.0, 1 / 3, 0.0])

    def test_detection_matching_counts_missing_and_extra(self):
        ref = [{"box": [0, 0, 10, 10], "score": 0.9, "class": 0}, {"box": [50, 50, 60, 60], "score": 0.8, "class": 0}]
        got = [{"box": [0, 0, 10, 11], "score": 0.88, "class": 0}, {"box": [80, 80, 90, 90], "score": 0.7, "class": 0}]
        m = mm._match_detections(ref, got, score_tol=0.02)
        assert m["matched"] == 1 and m["missing"] == 1 and m["extra"] == 1
        assert m["min_iou"] == pytest.approx(10 / 11)
        # a detection near the threshold may come and go without counting
        near = [{"box": [0, 0, 10, 10], "score": 0.26, "class": 0}]
        assert mm._match_detections(near, [], score_tol=0.02)["missing"] == 0


class TestEmbedderPreprocessing:
    @pytest.mark.parametrize("hw,resized,offsets", [
        ((224, 224), (224, 224), (0, 0)),
        ((225, 224), (225, 224), (0, 0)),  # surplus 1: round(0.5) is 0
        ((227, 224), (227, 224), (2, 0)),  # surplus 3: round(1.5) is 2
        ((300, 200), (336, 224), (56, 0)),
        ((120, 160), (224, 298), (0, 37)),
        ((1000, 333), (672, 224), (224, 0)),
    ])
    def test_resize_and_crop_geometry(self, hw, resized, offsets):
        assert mp.clip_resized_size(*hw) == resized
        assert mp.clip_crop_offsets(*resized) == offsets

    @pytest.mark.parametrize("shape", [(224, 224), (225, 224), (300, 227), (227, 300), (120, 160), (37, 91), (1000, 333), (640, 480)])
    def test_matches_open_clip_exactly(self, shape):
        open_clip = pytest.importorskip("open_clip")
        from PIL import Image

        _, _, preprocess = open_clip.create_model_and_transforms("ViT-B-16", pretrained=None)
        img = np.random.default_rng(shape[0] * 7 + shape[1]).integers(0, 256, size=(*shape, 3), dtype=np.uint8)
        ground = preprocess(Image.fromarray(img)).unsqueeze(0).numpy()
        ours = mp.clip_preprocess(img)
        assert ours.shape == (1, 3, 224, 224) and ours.dtype == np.float32
        np.testing.assert_array_equal(ours, ground)

    def test_the_grounds_bgr_crop_path(self):
        """reid.py converts a BGR crop to RGB before open_clip; the contract input is RGB."""
        pytest.importorskip("cv2")
        import cv2

        bgr = np.zeros((300, 120, 3), dtype=np.uint8)
        bgr[..., 2] = 255  # red in BGR
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        x = mp.clip_preprocess(rgb)
        assert x[0, 0].mean() == pytest.approx((1 - mp.CLIP_MEAN[0]) / mp.CLIP_STD[0], rel=1e-5)
        assert x[0, 2].mean() == pytest.approx((0 - mp.CLIP_MEAN[2]) / mp.CLIP_STD[2], rel=1e-5)

    def test_rejects_a_non_rgb_array(self):
        with pytest.raises(ValueError):
            mp.clip_resize_crop(np.zeros((10, 10), dtype=np.uint8))
        with pytest.raises(ValueError):
            mp.clip_resize_crop(np.zeros((10, 10, 3), dtype=np.float32))

    def test_l2_and_cosine(self):
        assert mp.l2_normalize(np.zeros(4)).tolist() == [0, 0, 0, 0]
        assert np.linalg.norm(mp.l2_normalize(np.arange(1, 6))) == pytest.approx(1.0)
        assert mp.cosine(np.array([1.0, 0]), np.array([2.0, 0])) == pytest.approx(1.0)
        assert mp.cosine(np.array([1.0, 0]), np.array([0, 3.0])) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Vendored YOLOX
# ---------------------------------------------------------------------------

VENDOR = PIPELINE / "yolox_vendor"

#: SHA-256 of the upstream files at YOLOX 6ddff48; these are vendored unchanged.
UNCHANGED_UPSTREAM = {
    "network_blocks.py": "f490240a674799f1b548ac4ca571c7f3e59d4e75efee8ebeb1e24e6e4e10d2a4",
    "darknet.py": "368c9db45166e863691cf7460b314d047928f90e8744c492a673b6eff9b809f3",
    "yolo_pafpn.py": "8571af557f6caacda61e54813be4be2d73c4a0889e1f876ec5586057eb2eebde",
    "LICENSE": "0ec3668d3274bcf29e8a29e9576d5a2cd96fc78d3c5bec4387355a796e5d9088",
}

#: SHA-256 of the sorted "name shape dtype" lines of the real yolox_s.pth "model" state dict.
PINNED_YOLOX_S_LAYOUT = "41146791a6fff17c6af553fa2478774cfb7a8b51f5665b37782a91f313730680"

#: Output of upstream YOLOX (6ddff48, yolox/models unmodified) built as YOLOX-S, with
#: _synthetic_state_dict weights, on _reference_input(), computed once with the upstream code.
UPSTREAM_REFERENCE = {
    "shape": [8400, 85],
    "mean": 8.12284888675571,
    "std": 55.38921396374613,
    "indices": [0, 1, 4, 5, 84, 6399 * 85 + 4, 6400 * 85, 8000 * 85 + 2, 8399 * 85 + 3, 8399 * 85 + 84,
                123457, 345678, 500000, 600000, 700000, 713999],
    "samples": [
        -0.15894152224063873, -0.5852535963058472, 0.5073757171630859, 0.4981502890586853,
        0.4853239953517914, 0.5072972178459167, 0.2602865993976593, 30.29242706298828,
        30.84148597717285, 0.49906036257743835, 0.5084258913993835, 0.5085049867630005,
        0.4983901381492615, 0.5095630884170532, 0.4961037337779999, 0.49906036257743835,
    ],
}


def _synthetic_state_dict(layout):
    import torch

    g = torch.Generator().manual_seed(1234)
    out = {}
    for name in sorted(layout):
        t = layout[name]
        if not t.is_floating_point():
            out[name] = torch.zeros_like(t)
            continue
        v = torch.randn(t.shape, generator=g, dtype=torch.float32) * 0.05
        if name.endswith("running_var"):
            v = v.abs() + 0.5
        out[name] = v
    return out


def _reference_input():
    import torch

    return torch.from_numpy(np.random.default_rng(7).integers(0, 256, size=(1, 3, 640, 640)).astype(np.float32))


class TestVendoredYolox:
    @pytest.mark.parametrize("name", sorted(UNCHANGED_UPSTREAM))
    def test_unchanged_files_are_byte_identical_to_upstream(self, name):
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == UNCHANGED_UPSTREAM[name]

    @pytest.mark.parametrize("name", ["yolo_head.py", "yolox.py"])
    def test_modified_files_say_so(self, name):
        head = (VENDOR / name).read_text()[:800]
        assert "MODIFIED BY THE FLIGHTRISK AUTHORS" in head and mm.YOLOX_COMMIT in head

    def test_notice_records_origin_and_changes(self):
        notice = (VENDOR / "NOTICE").read_text()
        assert "Copyright (c) 2021-2022 Megvii Inc." in notice
        assert mm.YOLOX_COMMIT in notice
        for name in ("network_blocks.py", "darknet.py", "yolo_pafpn.py", "yolo_head.py", "yolox.py", "__init__.py"):
            assert name in notice

    def test_vendored_code_imports_only_torch(self):
        import ast

        for path in VENDOR.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    roots = {node.module.split(".")[0]}
                else:
                    continue
                assert roots <= {"torch", "__future__"}, f"{path.name}: {roots}"

    def test_layout_matches_the_pinned_checkpoint(self):
        pytest.importorskip("torch")
        from yolox_vendor import build_yolox_s

        sd = build_yolox_s().state_dict()
        lines = sorted(f"{k} {tuple(v.shape)} {v.dtype}" for k, v in sd.items())
        assert hashlib.sha256("\n".join(lines).encode()).hexdigest() == PINNED_YOLOX_S_LAYOUT

    def test_output_matches_upstream_at_the_pinned_commit(self):
        torch = pytest.importorskip("torch")
        from yolox_vendor import build_yolox_s

        model = build_yolox_s()
        model.load_state_dict(_synthetic_state_dict(model.state_dict()), strict=True)
        with torch.no_grad():
            y = model(_reference_input())[0].numpy().astype(np.float64)
        assert list(y.shape) == UPSTREAM_REFERENCE["shape"]
        assert y.mean() == pytest.approx(UPSTREAM_REFERENCE["mean"], rel=1e-5)
        assert y.std() == pytest.approx(UPSTREAM_REFERENCE["std"], rel=1e-5)
        flat = y.reshape(-1)
        got = [flat[i] for i in UPSTREAM_REFERENCE["indices"]]
        assert got == pytest.approx(UPSTREAM_REFERENCE["samples"], rel=1e-4, abs=1e-5)

    def test_batchnorm_eps_is_upstreams(self):
        torch = pytest.importorskip("torch")
        from yolox_vendor import build_yolox_s

        bns = [m for m in build_yolox_s().modules() if isinstance(m, torch.nn.BatchNorm2d)]
        assert bns and all(m.eps == 1e-3 for m in bns)

    def test_the_export_module_decodes_like_upstream(self):
        torch = pytest.importorskip("torch")
        from yolox_vendor import build_yolox_s

        model = build_yolox_s()
        model.load_state_dict(_synthetic_state_dict(model.state_dict()), strict=True)
        export = mm.yolox_export_module(model)
        x = _reference_input()
        with torch.no_grad():
            ref, got = model(x), export(x)
        assert model.head.decode_in_inference  # the export module works on a copy
        torch.testing.assert_close(got, ref, rtol=1e-5, atol=1e-4)


# ---------------------------------------------------------------------------
# Docs, notices and the old export script
# ---------------------------------------------------------------------------


class TestDocsAndNotices:
    DOC = ROOT / "docs" / "mobile-models.md"

    def test_the_doc_carries_the_pins_and_contracts(self):
        doc = self.DOC.read_text()
        for d in mm.DOWNLOADS:
            assert d.sha256 in doc, d.key
        assert mm.CLIP_HF_REVISION in doc and mm.YOLOX_COMMIT in doc
        assert "make_models.py all" in doc
        for token in ("114", "top-left", "BGR", "[1, 8400, 85]", "objectness", "0.48145466", "0.26862954", "bicubic", "224"):
            assert token in doc, token

    def test_the_doc_lists_the_follow_ups_and_legal_questions(self):
        doc = self.DOC.read_text()
        for token in ("D-FINE-N", "DINOv2", "HERIDAL", "12%", "0.8%", "GPL-3.0-or-later", "section 7", "Free Software Foundation"):
            assert token in doc, token
        assert "not legal advice" in doc.lower()

    def test_the_doc_records_the_measured_parity(self):
        doc = self.DOC.read_text()
        for o in mm.OUTPUTS:
            assert o.filename in doc, o.filename

    def test_notices_carry_both_licences_and_every_copyright_line(self):
        notices = (PIPELINE / "NOTICES.md").read_text()
        assert "Apache License" in notices and "Version 2.0, January 2004" in notices
        assert notices.count("Permission is hereby granted, free of charge") == 3
        for line in ("Copyright (c) 2021-2022 Megvii Inc.", "Copyright (c) 2021 OpenAI",
                     "Copyright (c) 2012-2021 Gabriel Ilharco"):
            assert line in notices
        assert mm.YOLOX_S_WEIGHTS.sha256 in notices and mm.CLIP_WEIGHTS.sha256 in notices

    def test_the_old_clip_export_script_warns_against_store_builds(self):
        head = (ROOT / "scripts" / "export_clip_onnx.py").read_text()[:1500]
        assert "must not be used for store builds" in head
        assert "scripts/mobile_models/make_models.py" in head


# ---------------------------------------------------------------------------
# Conversion (needs the pinned conversion tools; skipped without them)
# ---------------------------------------------------------------------------

needs_conversion_tools = pytest.mark.skipif(
    any(__import__("importlib").util.find_spec(m) is None for m in ("coremltools", "onnxruntime", "onnx", "torch", "open_clip")),
    reason="conversion tools from scripts/mobile_models/requirements.txt are not installed",
)
on_macos = platform.system() == "Darwin"


@needs_conversion_tools
class TestConversion:
    def test_detector_round_trip(self, tmp_path):
        import torch
        from yolox_vendor import build_yolox_s

        model = build_yolox_s()
        model.load_state_dict(_synthetic_state_dict(model.state_dict()), strict=True)
        targets = "all" if on_macos else "android"
        mm.convert_yolox_model(model, tmp_path, targets, lambda m: None)
        shape = [1, mp.YOLOX_NUM_ANCHORS, mp.YOLOX_ROW_LENGTH]
        canvas = np.random.default_rng(7).integers(0, 256, size=(640, 640, 3), dtype=np.uint8)
        with torch.no_grad():
            ref = model(torch.from_numpy(mp.yolox_input_tensor(canvas)))[0].numpy()
        for name in ("yolox_s.onnx", "yolox_s_fp16.onnx"):
            iface = mm.check_onnx_interface(tmp_path / "android" / name, mm.DETECTOR_INPUT, [1, 3, 640, 640], mm.DETECTOR_OUTPUT, shape)
            assert iface["problems"] == [] and iface["ir_version"] <= mm.MAX_ONNX_IR_VERSION
        got = mm._Runner(tmp_path / "android" / "yolox_s.onnx").detector(canvas)
        np.testing.assert_allclose(got, ref, rtol=1e-3, atol=1e-2)
        assert np.isfinite(mm._Runner(tmp_path / "android" / "yolox_s_fp16.onnx").detector(canvas)).all()
        if on_macos:
            pkg = tmp_path / "ios" / "YOLOXs.mlpackage"
            assert mm.check_coreml_interface(pkg, True, mm.DETECTOR_INPUT, [1, 3, 640, 640], mm.DETECTOR_OUTPUT)["problems"] == []
            out = mm._Runner(pkg, "CPU_AND_GPU").detector(canvas)
            assert out.shape == (mp.YOLOX_NUM_ANCHORS, mp.YOLOX_ROW_LENGTH) and np.isfinite(out).all()

    def test_embedder_round_trip(self, tmp_path):
        import torch
        from open_clip.transformer import VisionTransformer

        torch.manual_seed(0)
        visual = VisionTransformer(image_size=224, patch_size=32, width=64, layers=2, heads=2, mlp_ratio=2.0, output_dim=512).eval()
        targets = "all" if on_macos else "android"
        mm.convert_clip_model(visual, tmp_path, targets, lambda m: None, int8_exclude=())
        x = mp.clip_preprocess(np.random.default_rng(3).integers(0, 256, size=(300, 200, 3), dtype=np.uint8))
        with torch.no_grad():
            ref = mm.clip_embedder_module(visual)(torch.from_numpy(x)).numpy().reshape(-1)
        for name, cos_min in (("clip_vit_b16_laion2b.onnx", 0.9999), ("clip_vit_b16_laion2b_w8.onnx", 0.99),
                              ("clip_vit_b16_laion2b_int8.onnx", 0.9)):
            path = tmp_path / "android" / name
            iface = mm.check_onnx_interface(path, mm.EMBEDDER_INPUT, [1, 3, 224, 224], mm.EMBEDDER_OUTPUT, [1, 512])
            assert iface["problems"] == [], name
            got = mm._Runner(path).embedder(x)
            assert mp.cosine(got, ref) >= cos_min, name
        assert abs(np.linalg.norm(mm._Runner(tmp_path / "android" / "clip_vit_b16_laion2b.onnx").embedder(x)) - 1) < 1e-5
        if on_macos:
            for name in ("CLIPViTB16LAION.mlpackage", "CLIPViTB16LAION8bit.mlpackage"):
                pkg = tmp_path / "ios" / name
                assert mm.check_coreml_interface(pkg, False, mm.EMBEDDER_INPUT, [1, 3, 224, 224], mm.EMBEDDER_OUTPUT)["problems"] == []
                assert mp.cosine(mm._Runner(pkg, "CPU_ONLY").embedder(x), ref) >= 0.98, name

    def test_dynamic_int8_refuses_an_unknown_node_to_keep(self, tmp_path):
        import torch
        from open_clip.transformer import VisionTransformer

        visual = VisionTransformer(image_size=224, patch_size=32, width=64, layers=1, heads=2, mlp_ratio=2.0, output_dim=512).eval()
        src = tmp_path / "e.onnx"
        mm.export_onnx(mm.clip_embedder_module(visual), torch.zeros(1, 3, 224, 224), src, "input", "embedding", {})
        with pytest.raises(mm.VerificationFailed, match="not in"):
            mm.onnx_to_dynamic_int8(src, tmp_path / "q.onnx", {}, ("/no/such/node",))
