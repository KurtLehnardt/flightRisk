"""Store-safe mobile model pipeline (scripts/mobile_models): pins, fail-closed checksums,
the licence guard, parity metrics, install, the preprocessing contracts and the vendored
YOLOX code.

Nothing here downloads anything or needs the real weights. Downloads are served by a fake
opener from bytes made in the test, and every model file the licence guard is shown is a
tiny synthetic file. The conversion tests at the end build small or randomly initialised
models and need the conversion tools (scripts/mobile_models/requirements.lock, coremltools
in particular); they are skipped when those are not installed, which is the case in
FlightRisk's own venv. In the conversion venv run::

    python -m pytest --noconftest tests/test_mobile_models.py
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import json
import lzma
import os
import platform
import re
import shutil
import subprocess
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
        assert len(mm.DOWNLOADS) == 4
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
        assert mm.PERSON_PHOTO.sha256 == "0b7230075f66aeda713b53924e6b78ccb768e7f0875e83cca7bfbfe1939be581"
        assert mm.PERSON_PHOTO.size == 1_567_122

    def test_the_person_photo_is_public_domain_and_never_installed(self):
        photo = mm.PERSON_PHOTO
        assert photo.url.startswith("https://upload.wikimedia.org/wikipedia/commons/")
        assert "Public domain" in photo.licence
        assert photo.filename not in {o.filename for o in mm.OUTPUTS} | {o.installed_name for o in mm.OUTPUTS}
        assert not photo.filename.endswith(tuple(lg.MODEL_EXTENSIONS))

    def test_the_guard_knows_the_pinned_weights(self):
        assert set(lg.PINNED_WEIGHTS_SHA256) == {mm.YOLOX_S_WEIGHTS.sha256, mm.CLIP_WEIGHTS.sha256}
        assert mm.MANIFEST_NAME == lg.MANIFEST_NAME == "mobile-models.json"

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

    def test_the_pipeline_never_installs_or_imports_ultralytics_or_insightface(self):
        text = "\n".join(p.read_text() for p in (PIPELINE / "requirements.txt", PIPELINE / "make_models.py"))
        assert not re.search(r"^\s*(import|from)\s+(ultralytics|insightface)", text, re.MULTILINE)
        for name in ("requirements.txt", "requirements.lock"):
            lower = (PIPELINE / name).read_text().lower()
            assert "ultralytics" not in lower and "insightface" not in lower, name

    def test_the_script_imports_no_heavy_dependency_at_load(self):
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
# Hash-locked tools
# ---------------------------------------------------------------------------


class TestToolLock:
    def test_requirements_pin_every_top_level_tool_exactly(self):
        pins = {}
        for line in (PIPELINE / "requirements.txt").read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name, sep, version = line.partition("==")
            assert sep == "==", f"not pinned exactly: {line}"
            pins[mm.normalise_name(name)] = version.strip()
        assert pins == {mm.normalise_name(n): v for n, v in mm.PINNED_TOOLS}
        for tool in ("torch", "open-clip-torch", "onnx", "onnxruntime", "coremltools", "pytest"):
            assert tool in pins

    def test_the_lock_pins_every_package_with_hashes(self):
        text = (PIPELINE / "requirements.lock").read_text()
        locked = mm.read_lock(text)
        assert len(locked) >= len(mm.PINNED_TOOLS)
        for name, version in mm.PINNED_TOOLS:
            assert locked[mm.normalise_name(name)][0] == version, name
        # every requirement line is followed by at least one --hash
        blocks = re.split(r"\n(?=[a-z])", text.split("\n", 6)[-1])
        for block in blocks:
            if block.strip() and block[0].isalpha():
                assert "--hash=sha256:" in block, block.splitlines()[0]
        assert "/private/" not in text and "/Users/" not in text  # no machine paths

    def test_read_lock_keeps_markers_and_refuses_unpinned_lines(self):
        lock = (
            "# comment\n"
            "numpy==2.3.5 \\\n    --hash=sha256:" + "a" * 64 + "\n"
            "triton==3.3.0 ; platform_machine == 'x86_64' and sys_platform == 'linux' \\\n    --hash=sha256:" + "b" * 64 + "\n"
            "Open_Clip.Torch==3.3.0 \\\n    --hash=sha256:" + "c" * 64 + "\n"
        )
        locked = mm.read_lock(lock)
        assert locked["numpy"] == ("2.3.5", None)
        assert locked["triton"][1].startswith("platform_machine")
        assert "open-clip-torch" in locked
        with pytest.raises(mm.ToolVersionMismatch):
            mm.read_lock("torch>=2.0 \\\n    --hash=sha256:" + "d" * 64 + "\n")

    def test_markers_decide_what_is_needed_here(self):
        lock = (
            "numpy==2.3.5 \\\n    --hash=sha256:" + "a" * 64 + "\n"
            "never==1.0 ; sys_platform == 'no-such-platform' \\\n    --hash=sha256:" + "b" * 64 + "\n"
        )
        assert mm.locked_here(lock) == {"numpy": "2.3.5"}

    def test_differences_cover_missing_wrong_and_extra_packages(self):
        locked = {"numpy": "2.3.5", "torch": "2.7.0", "onnx": "1.18.0"}
        installed = {"numpy": "2.3.5", "torch": "2.8.0", "pip": "26.3", "requests": "2.33.0"}
        diff = mm.tool_differences(locked, installed)
        assert "onnx missing (locked 1.18.0)" in diff
        assert "torch 2.8.0 (locked 2.7.0)" in diff
        assert "requests 2.33.0 is not in the lock" in diff
        assert not any(d.startswith("pip") for d in diff)
        assert mm.tool_differences(locked, {**locked, "pip": "1"}) == []

    def test_check_tool_versions_compares_the_whole_installed_set(self, monkeypatch):
        locked = {mm.normalise_name(n): v for n, v in mm.PINNED_TOOLS} | {"protobuf": "7.36.2"}
        monkeypatch.setattr(mm, "locked_here", lambda text=None: dict(locked))
        monkeypatch.setattr(mm, "installed_distributions", lambda: dict(locked))
        ok = mm.check_tool_versions(allow_unpinned=False)
        assert ok["pinned"] is True and ok["differences"] == []
        assert ok["versions"]["coremltools"] == "9.0"
        # a transitive dependency at another version is enough to refuse
        monkeypatch.setattr(mm, "installed_distributions", lambda: {**locked, "protobuf": "6.0.0"})
        with pytest.raises(mm.ToolVersionMismatch, match="protobuf 6.0.0"):
            mm.check_tool_versions(allow_unpinned=False)
        unpinned = mm.check_tool_versions(allow_unpinned=True)
        assert unpinned["pinned"] is False and unpinned["differences"] == ["protobuf 6.0.0 (locked 7.36.2)"]
        # and so is an extra package
        monkeypatch.setattr(mm, "installed_distributions", lambda: {**locked, "ultralytics": "8.3.0"})
        with pytest.raises(mm.ToolVersionMismatch, match="ultralytics 8.3.0 is not in the lock"):
            mm.check_tool_versions(allow_unpinned=False)

    def test_convert_records_an_unpinned_build(self, tmp_path, monkeypatch):
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        monkeypatch.setattr(mm, "check_tool_versions",
                            lambda allow: {"pinned": False, "differences": ["x"], "versions": {}})
        monkeypatch.setattr(mm, "read_verified", lambda d, folder: b"")
        monkeypatch.setattr(mm, "convert_yolox", lambda *a, **k: None)
        monkeypatch.setattr(mm, "convert_clip", lambda *a, **k: None)
        monkeypatch.setattr(mm, "outputs_for", lambda targets: ())
        assert mm.main(["convert", "--work-dir", str(tmp_path), "--allow-unpinned-tools"]) == 0
        assert json.loads((tmp_path / mm.BUILD_NAME).read_text())["tools_pinned"] is False


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
    """A stand-in for https_open that serves ``body`` and counts calls."""

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

    def test_a_folder_where_the_download_belongs_is_refused_and_left_alone(self, tmp_path):
        pin = _pin()
        (tmp_path / pin.filename).mkdir()
        (tmp_path / pin.filename / "keep.txt").write_text("someone's files")
        opener = _Opener(PAYLOAD)
        with pytest.raises(mm.DownloadFailed, match="is a folder"):
            mm.fetch_one(pin, tmp_path, opener=opener)
        assert opener.calls == 0
        assert (tmp_path / pin.filename / "keep.txt").read_text() == "someone's files"

    def test_a_plain_http_address_is_refused_before_connecting(self, monkeypatch):
        monkeypatch.setattr(mm.urllib.request, "build_opener",
                            lambda *a: pytest.fail("no connection may be made"))
        with pytest.raises(mm.DownloadFailed, match="not an https address"):
            mm.https_open("http://example.invalid/w.bin", timeout=1)

    def test_a_redirect_to_http_is_refused(self):
        import urllib.request

        handler = mm._HttpsOnlyRedirects()
        req = urllib.request.Request("https://github.com/x/releases/download/w.bin")
        with pytest.raises(mm.DownloadFailed, match="not https"):
            handler.redirect_request(req, None, 302, "Found", {}, "http://objects.example.invalid/w.bin")
        follow = handler.redirect_request(req, None, 302, "Found", {}, "https://objects.example.invalid/w.bin")
        assert follow.full_url == "https://objects.example.invalid/w.bin"

    def test_downloads_send_a_user_agent_and_check_where_they_ended(self, monkeypatch):
        seen = {}

        class _Response(io.BytesIO):
            def __init__(self, final):
                super().__init__(b"x")
                self.final = final

            def geturl(self):
                return self.final

        class _FakeOpener:
            def __init__(self, final):
                self.final = final

            def open(self, req, timeout=None):
                seen["ua"] = req.get_header("User-agent")
                return _Response(self.final)

        monkeypatch.setattr(mm.urllib.request, "build_opener", lambda *h: _FakeOpener("https://cdn.example.invalid/w"))
        assert mm.https_open("https://example.invalid/w", timeout=1).read() == b"x"
        assert seen["ua"] == mm.USER_AGENT
        monkeypatch.setattr(mm.urllib.request, "build_opener", lambda *h: _FakeOpener("http://cdn.example.invalid/w"))
        with pytest.raises(mm.DownloadFailed, match="not https"):
            mm.https_open("https://example.invalid/w", timeout=1)


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
        monkeypatch.setattr(mm, "check_tool_versions", lambda allow: {"pinned": True, "differences": [], "versions": {}})
        touched = []
        monkeypatch.setattr(mm, "convert_yolox", lambda *a, **k: touched.append("yolox"))
        monkeypatch.setattr(mm, "convert_clip", lambda *a, **k: touched.append("clip"))
        assert mm.main(["convert", "--work-dir", str(tmp_path)]) == 1
        assert touched == []
        assert "ChecksumMismatch" in capsys.readouterr().err


class TestNetworkGuard:
    def test_downloaders_raise_while_the_embedder_is_built_and_come_back_after(self, monkeypatch):
        open_clip = pytest.importorskip("open_clip")
        factory = __import__("open_clip.factory", fromlist=["factory"])
        pretrained = __import__("open_clip.pretrained", fromlist=["pretrained"])

        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        before = (pretrained.download_pretrained_from_hf, factory.download_pretrained)
        with mm.network_refused():
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            with pytest.raises(mm.VerificationFailed, match="refused open_clip.pretrained.download_pretrained_from_hf"):
                pretrained.download_pretrained_from_hf("laion/anything")
            with pytest.raises(mm.VerificationFailed, match="refused"):
                factory.download_pretrained({"url": "https://example.invalid/x"})
        assert (pretrained.download_pretrained_from_hf, factory.download_pretrained) == before
        assert "HF_HUB_OFFLINE" not in os.environ
        assert open_clip is not None

    def test_the_embedder_is_built_inside_the_guard(self):
        import inspect

        source = inspect.getsource(mm.load_clip)
        assert "with network_refused():" in source and 'pretrained=None' in source


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

    def test_the_order_is_by_path_components(self, tmp_path):
        # "Data/x" sorts before "Data.bin" by components, after it by plain string order.
        pkg = tmp_path / "P"
        (pkg / "Data").mkdir(parents=True)
        (pkg / "Data" / "x").write_bytes(b"1")
        (pkg / "Data.bin").write_bytes(b"2")
        items = [("Data/x", hashlib.sha256(b"1").hexdigest()), ("Data.bin", hashlib.sha256(b"2").hexdigest())]
        assert mm.tree_sha256(pkg) == lg.tree_digest(items) == lg.tree_digest(list(reversed(items)))


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _coreml_bytes(weights_sha: str | None = mm.YOLOX_S_WEIGHTS.sha256, filler: bytes = b"") -> bytes:
    """Stand-in Core ML model bytes: a protobuf-shaped header and, optionally, the pinned metadata."""
    meta = b"" if weights_sha is None else b"\x12\x19flightrisk.weights_sha256\x1a\x40" + weights_sha.encode()
    return b"\x08\x08\x12\x04desc" + meta + filler


def _fake_outputs(work: Path, passed: bool = True, tools_pinned: bool = True) -> dict:
    """Small stand-ins for every output, and a verify.json that passed them."""
    out = work / "out"
    report = {"passed": passed, "tools_pinned": tools_pinned, "outputs": {},
              "detector": {"outputs": {}}, "embedder": {"outputs": {}}}
    for o in mm.OUTPUTS:
        path = out / o.platform / o.filename
        if o.filename.endswith(".mlpackage"):
            weights = mm.YOLOX_S_WEIGHTS if o.role == "detector" else mm.CLIP_WEIGHTS
            (path / "Data" / "com.apple.CoreML").mkdir(parents=True, exist_ok=True)
            (path / "Manifest.json").write_text(json.dumps({"name": o.filename}))
            (path / "Data" / "com.apple.CoreML" / "model.mlmodel").write_bytes(
                _coreml_bytes(weights.sha256, o.filename.encode() * 3))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(o.filename.encode() * 5)
        report["outputs"][o.key] = {"sha256": mm.tree_sha256(path), "bytes": mm.tree_size(path)}
    (work / mm.VERIFY_NAME).write_text(json.dumps(report))
    return report


class TestInstall:
    def _run(self, tmp_path, **kw):
        android, ios = tmp_path / "android-assets", tmp_path / "ios-models"
        installed = mm.run_install(tmp_path / "work", android, ios, "all", {}, lambda m: None, **kw)
        return android, ios, installed

    def test_installs_the_default_variants_under_fixed_names(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        android, ios, _ = self._run(tmp_path)
        assert sorted(p.name for p in android.iterdir()) == [
            "clip_vit_b16_laion2b.onnx", mm.MANIFEST_NAME, mm.NOTICES_INSTALLED_NAME, "yolox_s.onnx",
        ]
        assert sorted(p.name for p in ios.iterdir()) == [
            "CLIPViTB16LAION.mlpackage", "YOLOXs.mlpackage", mm.MANIFEST_NAME, mm.NOTICES_INSTALLED_NAME,
        ]
        # the defaults: fp32 detector and weight-only int8 embedder on Android,
        # fp32 detector and palettised embedder on iOS
        assert (android / "yolox_s.onnx").read_bytes() == b"yolox_s.onnx" * 5
        assert (android / "clip_vit_b16_laion2b.onnx").read_bytes() == b"clip_vit_b16_laion2b_w8.onnx" * 5
        manifest = json.loads((ios / mm.MANIFEST_NAME).read_text())
        assert {m["file"]: (m["variant"], m["built_as"], m["coreml_compute_units"]) for m in manifest["models"]} == {
            "YOLOXs.mlpackage": ("fp32", "YOLOXs.mlpackage", "all"),
            "CLIPViTB16LAION.mlpackage": ("palettized8", "CLIPViTB16LAION8bit.mlpackage", "all"),
        }
        assert manifest["tools_pinned"] is True
        assert manifest["generated_by"] == lg.MANIFEST_GENERATOR
        assert manifest["contract"]["detector"]["letterbox"]["pad_value"] == 114
        assert manifest["contract"]["detector"]["coreml_compute_units"] == "all"
        assert manifest["contract"]["embedder"]["model"] == "open_clip:ViT-B-16:laion2b_s34b_b88k"
        assert manifest["contract"]["embedder"]["crop_from_detection"] == mp.CROP_RULE
        assert "coreml_compute_units" not in json.loads((android / mm.MANIFEST_NAME).read_text())["contract"]["detector"]
        assert (android / mm.NOTICES_INSTALLED_NAME).read_text() == (PIPELINE / "NOTICES.md").read_text()
        assert lg.scan([android, ios]) == []

    def test_a_variant_can_be_chosen(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        overrides = {("android", "detector"): "fp16", ("ios", "embedder"): "fp16", ("ios", "detector"): "fp16"}
        android, ios = tmp_path / "a", tmp_path / "i"
        mm.run_install(tmp_path / "work", android, ios, "all", overrides, lambda m: None)
        assert (android / "yolox_s.onnx").read_bytes() == b"yolox_s_fp16.onnx" * 5
        manifest = json.loads((ios / mm.MANIFEST_NAME).read_text())
        clip = next(m for m in manifest["models"] if m["role"] == "embedder")
        assert clip == {**clip, "file": "CLIPViTB16LAION.mlpackage", "variant": "fp16",
                        "built_as": "CLIPViTB16LAION.mlpackage"}
        det = next(m for m in manifest["models"] if m["role"] == "detector")
        assert det["built_as"] == "YOLOXsFP16.mlpackage" and det["coreml_compute_units"] == "cpuAndGPU"
        assert manifest["contract"]["detector"]["coreml_compute_units"] == "cpuAndGPU"

    def test_reinstall_replaces_the_previous_package(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        _, ios, _ = self._run(tmp_path)
        (ios / "YOLOXs.mlpackage" / "stale.bin").write_bytes(b"old")
        self._run(tmp_path)
        assert not (ios / "YOLOXs.mlpackage" / "stale.bin").exists()
        assert sorted(p.name for p in ios.iterdir() if p.name.startswith(".")) == []

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

    def test_an_unpinned_build_needs_an_explicit_flag(self, tmp_path, capsys):
        _fake_outputs(tmp_path / "work", tools_pinned=False)
        with pytest.raises(mm.VerificationFailed, match="--allow-unpinned-build"):
            self._run(tmp_path)
        assert not (tmp_path / "android-assets").exists()
        android, _, _ = self._run(tmp_path, allow_unpinned_build=True)
        assert json.loads((android / mm.MANIFEST_NAME).read_text())["tools_pinned"] is False
        # an older verify.json without the field counts as unpinned
        report = json.loads((tmp_path / "work" / mm.VERIFY_NAME).read_text())
        del report["tools_pinned"]
        (tmp_path / "work" / mm.VERIFY_NAME).write_text(json.dumps(report))
        assert mm.main(["install", "--work-dir", str(tmp_path / "work"), "--android", str(tmp_path / "a2"),
                        "--ios", str(tmp_path / "i2")]) == 1
        assert "--allow-unpinned-build" in capsys.readouterr().err

    def test_an_ungated_android_embedder_needs_an_explicit_flag(self, tmp_path, capsys):
        _fake_outputs(tmp_path / "work")
        dynamic = {("android", "embedder"): "int8-dynamic"}
        with pytest.raises(mm.VerificationFailed, match="--allow-ungated-embedder"):
            mm.run_install(tmp_path / "work", tmp_path / "a", None, "android", dynamic, lambda m: None)
        assert not (tmp_path / "a").exists()
        assert mm.main(["install", "--work-dir", str(tmp_path / "work"), "--targets", "android",
                        "--android", str(tmp_path / "a"), "--android-embedder", "int8-dynamic"]) == 1
        assert "not gated on the person parity" in capsys.readouterr().err
        assert mm.main(["install", "--work-dir", str(tmp_path / "work"), "--targets", "android",
                        "--android", str(tmp_path / "a"), "--android-embedder", "int8-dynamic",
                        "--allow-ungated-embedder"]) == 0
        assert (tmp_path / "a" / "clip_vit_b16_laion2b.onnx").read_bytes() == b"clip_vit_b16_laion2b_int8.onnx" * 5
        # the person-gated fp32 embedder needs no flag
        mm.run_install(tmp_path / "work", tmp_path / "b", None, "android", {("android", "embedder"): "fp32"}, lambda m: None)
        assert (tmp_path / "b" / "clip_vit_b16_laion2b.onnx").read_bytes() == b"clip_vit_b16_laion2b.onnx" * 5

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
        (android / "old_detector.dat").write_bytes(b"\x08\x07\x3a\x02ab" + b"\x00" * 64)  # a renamed ONNX
        rc = mm.main(["install", "--work-dir", str(tmp_path / "work"), "--android", str(android),
                      "--ios", str(tmp_path / "ios-models")])
        assert rc == 1
        err = capsys.readouterr().err
        assert "yolo11n.onnx" in err and "old_detector.dat" in err and "allowlist:unlisted" in err
        assert (android / "yolo11n.onnx").exists()  # install never deletes files it did not install

    def test_targets_android_installs_only_android(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        android, ios = tmp_path / "a", tmp_path / "i"
        mm.run_install(tmp_path / "work", android, ios, "android", {}, lambda m: None)
        assert android.is_dir() and not ios.exists()

    def test_a_staged_copy_that_does_not_match_is_never_moved_into_place(self, tmp_path, monkeypatch):
        _fake_outputs(tmp_path / "work")
        android, _, _ = self._run(tmp_path)
        before = (android / "yolox_s.onnx").read_bytes()
        real_copy = shutil.copy2

        def corrupting_copy(src, dst, *a, **k):
            real_copy(src, dst, *a, **k)
            if Path(src).name == "yolox_s.onnx":
                Path(dst).write_bytes(b"corrupted while copying")
            return dst

        monkeypatch.setattr(mm.shutil, "copy2", corrupting_copy)
        with pytest.raises(mm.VerificationFailed, match="staged copy"):
            self._run(tmp_path)
        assert (android / "yolox_s.onnx").read_bytes() == before  # the old file is untouched
        assert not (android / mm.MANIFEST_NAME).exists()  # the old manifest went first, so the folder lists nothing
        assert [p.name for p in android.iterdir() if p.name.startswith(".") and p.name != ".gitkeep"] == []
        assert lg.scan([android])  # and the guard refuses the half-installed folder

    def test_a_failure_replacing_a_package_puts_the_old_one_back(self, tmp_path, monkeypatch):
        _fake_outputs(tmp_path / "work")
        _, ios, _ = self._run(tmp_path)
        old = mm.tree_sha256(ios / "YOLOXs.mlpackage")
        real_replace = os.replace
        calls = []

        def failing_replace(src, dst):
            calls.append((Path(src).name, Path(dst).name))
            if Path(dst).name == "YOLOXs.mlpackage" and ".staging-" in Path(src).name:
                raise OSError("disk full")
            return real_replace(src, dst)

        monkeypatch.setattr(mm.os, "replace", failing_replace)
        with pytest.raises(OSError, match="disk full"):
            self._run(tmp_path)
        assert mm.tree_sha256(ios / "YOLOXs.mlpackage") == old
        assert [p.name for p in ios.iterdir() if p.name.startswith(".")] == []

    def test_the_manifest_is_written_last(self, tmp_path, monkeypatch):
        _fake_outputs(tmp_path / "work")
        order = []
        real_install = mm.install_atomically
        real_write = mm._write_atomically
        monkeypatch.setattr(mm, "install_atomically", lambda s, d, h: (order.append(d.name), real_install(s, d, h))[1])
        monkeypatch.setattr(mm, "_write_atomically", lambda d, b: (order.append(d.name), real_write(d, b))[1])
        mm.run_install(tmp_path / "work", tmp_path / "a", None, "android", {}, lambda m: None)
        assert order == ["yolox_s.onnx", "clip_vit_b16_laion2b.onnx", mm.NOTICES_INSTALLED_NAME, mm.MANIFEST_NAME]

    def test_leftovers_of_a_crashed_install_are_cleared(self, tmp_path):
        _fake_outputs(tmp_path / "work")
        android = tmp_path / "android-assets"
        android.mkdir()
        (android / ".yolox_s.onnx.staging-1-abcd").write_bytes(b"\x08\x07\x3a\x02ab")
        (android / f".{mm.MANIFEST_NAME}.tmp-1-abcd").write_text("{}")
        self._run(tmp_path)
        assert [p.name for p in android.iterdir() if p.name.startswith(".")] == []

    def test_the_install_defaults_are_the_app_folders_git_ignores(self):
        assert mm.DEFAULT_ANDROID_DIR == ROOT / "mobile" / "app" / "src" / "main" / "assets"
        assert mm.DEFAULT_IOS_DIR == ROOT / "ios" / "FlightRisk" / "Models"
        if not (ROOT / ".git").exists():
            pytest.skip("not a git checkout")
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
            f"build/mobile_models/downloads/{mm.PERSON_PHOTO.filename}",
            f"build/mobile_models/{mm.GOLDEN_DIR_NAME}/frame.png",
        ]
        out = subprocess.run(["git", "check-ignore", *paths], cwd=ROOT, capture_output=True, text=True, check=False)
        assert sorted(out.stdout.split()) == sorted(paths)


# ---------------------------------------------------------------------------
# Licence guard: names
# ---------------------------------------------------------------------------

FORBIDDEN = [
    "yolo11n.onnx", "yolov8n.pt", "YOLOPersonDetector.mlmodelc", "best_yolov5s.tflite", "YOLO11n.mlpackage",
    "ultralytics_export.onnx", "scrfd_500m.onnx", "SCRFDFaceDetector.mlpackage", "arcface_mobilefacenet.onnx",
    "ArcFaceMobile.mlmodelc", "insightface_r18.tflite", "InsightFaceR18.mlpackage", "buffalo_sc.zip",
    "buffalo_l", "antelopev2", "mobileclip_s2.onnx", "MobileCLIP-S2.mlpackage", "osnet_x1_0.onnx", "OSNetReID.mlpackage",
    "det_500m.onnx", "w600k_mbf.onnx", "det_2.5g.onnx", "glintr100.onnx", "clip_visual.onnx", "CLIPVisual.mlmodelc",
]
ALLOWED = [
    "yolox_s.onnx", "yolox_s_fp16.onnx", "YOLOXs.mlpackage", "YOLOXsFP16.mlpackage", "YOLOXs.mlmodelc", "yolox-nano.onnx",
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
        names |= {Path(m).name for m in re.findall(r"^#\s+((?:models|ios|mobile)/\S+)", script, re.MULTILINE)}
        # PR #214: the script passes --skip-tflite, so it writes only the ONNX and Core ML files.
        assert names == {
            "osnet_x1_0.onnx", "insightface_r18.onnx", "OSNetReID.mlpackage", "InsightFaceR18.mlpackage",
        }
        for name in sorted(names):
            assert lg.check_name(name) is not None, name

    def test_every_model_the_apps_load_is_classified(self):
        """A model name in the app code is either forbidden or one this pipeline (or MiDaS) provides."""
        allowed_in_apps = {o.installed_name for o in mm.OUTPUTS} | {"midas_small.onnx"}
        allowed_in_apps |= {Path(n).stem for n in allowed_in_apps}
        # detector.onnx is the on-device name of the user's own imported model (ByoDetectorStore); no
        # model of that name ships in the app or the repo. It is added after the stems, so a bare
        # "detector" resource name is still checked.
        allowed_in_apps |= {"detector.onnx"}
        found: set[str] = set()
        srcs = list((ROOT / "mobile").rglob("*.kt")) + list((ROOT / "ios").rglob("*.swift"))
        # Only code that ships: unit-test fixtures (src/test, src/androidTest, iOS test targets) use
        # throwaway names such as bad.onnx and are not models the apps load.
        test_roots = ("mobile/app/src/test/", "mobile/app/src/androidTest/", "mobile/contract/src/test/",
                      "ios/FlightRiskTests/", "ios/FlightRiskUITests/", "ios/FlightRiskGround/Tests/")
        srcs = [s for s in srcs if not s.relative_to(ROOT).as_posix().startswith(test_roots)]
        for src in srcs:
            text = src.read_text(errors="replace")
            found |= set(re.findall(r'"([A-Za-z0-9_.\-]+\.(?:onnx|tflite|ort))"', text))
            found |= set(re.findall(r'forResource:\s*"([A-Za-z0-9_\-]+)",\s*withExtension:\s*"mlmodelc"', text))
            found |= set(re.findall(r'modelName:\s*String\s*=\s*"([A-Za-z0-9_\-]+)"', text))
        assert found, "no model names found in the app code"
        unclassified = sorted(n for n in found if lg.check_name(n) is None and n not in allowed_in_apps)
        assert unclassified == []


# ---------------------------------------------------------------------------
# Licence guard: what is model-like
# ---------------------------------------------------------------------------

#: A tiny ONNX-shaped protobuf: ir_version 8, producer "pytorch", a graph field.
ONNX_LIKE = b"\x08\x08\x12\x07pytorch\x3a\x04abcd" + b"\x00" * 32
#: A tiny Core ML-shaped protobuf: specificationVersion 8, a description, model type field 502.
COREML_LIKE = b"\x08\x08\x12\x02ab\xb2\x1f\x02xy"


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.write_bytes(_zip_bytes(entries))
    return path


def _manifest(folder: Path, files: dict[str, str], generator: str = lg.MANIFEST_GENERATOR) -> Path:
    doc = {"generated_by": generator, "models": [{"file": n, "sha256": h} for n, h in files.items()]}
    path = folder / lg.MANIFEST_NAME
    path.write_text(json.dumps(doc))
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestSniff:
    @pytest.mark.parametrize("head,what", [
        (ONNX_LIKE, "ONNX or Core ML model (protobuf)"),
        (COREML_LIKE, "ONNX or Core ML model (protobuf)"),
        (b"\x1c\x00\x00\x00TFL3" + b"\x00" * 40, "TFLite model"),
        (b"\x1c\x00\x00\x00ORTM" + b"\x00" * 40, "ONNX Runtime model"),
        (b"GGUF\x03\x00\x00\x00" + b"\x00" * 40, "GGUF model"),
        (b"\x89HDF\r\n\x1a\n" + b"\x00" * 40, "HDF5 file"),
        (b"\x93NUMPY\x01\x00" + b"\x00" * 40, "NumPy array"),
        (b"\x80\x02}q\x00.", "Python pickle (a PyTorch checkpoint)"),
        ((10).to_bytes(8, "little") + b'{"a": "b"}' + b"\x00" * 16, "safetensors file"),
        (b"7767517\n3 3\n", "ncnn model"),
        (_zip_bytes({"a": b"x"}), "zip archive"),
        (gzip.compress(b"hello"), "gzip archive"),
        (lzma.compress(b"hello"), "xz archive"),
        (bz2.compress(b"hello"), "bzip2 archive"),
        (b"\x28\xb5\x2f\xfd" + b"\x00" * 16, "zstd archive"),
        (b"7z\xbc\xaf\x27\x1c" + b"\x00" * 16, "7z archive"),
    ])
    def test_models_and_archives_are_recognised_by_their_bytes(self, head, what):
        assert lg.sniff(head, len(head)) == what
        assert lg.model_like("innocent.png", head, len(head)) == what

    @pytest.mark.parametrize("head", [
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,  # PNG
        b"\xff\xd8\xff\xe0" + b"\x00" * 40,  # JPEG
        b'{"generated_by": "x"}',  # JSON
        b"# Third-party notices\n",  # Markdown
        b"dex\n035\x00" + b"\x00" * 40,  # Android dex
        b"\x7fELF\x02\x01\x01" + b"\x00" * 40,  # a native library
        b"\x0a\x05hello\x12\x02ab",  # an AAB's protobuf metadata (field 1 is a message)
        b"\x08\x08\x12\x09too long",  # starts like a model but is malformed
        b"\x08\x08\x12\x02ab",  # a complete, small protobuf with no graph or model type
        b"\x02\x00\x0c\x00" + b"\x00" * 40,  # resources.arsc
        b"",
    ])
    def test_other_files_are_not_model_like(self, head):
        assert lg.sniff(head, len(head)) is None
        assert lg.model_like("data.bin", head, len(head)) is None

    def test_a_model_header_longer_than_what_is_read_fails_closed(self):
        head = b"\x08\x08\x32\xff\xff\x03" + b"x" * 100  # field 6 (doc string) runs past the bytes read
        assert lg.sniff(head, 10 << 20) is not None
        assert lg.sniff(head, len(head)) is None  # the whole file: the field runs past its end

    def test_model_extensions_are_model_like_whatever_their_bytes(self):
        for ext in (".onnx", ".tflite", ".pt", ".safetensors", ".mlmodel", ".gguf", ".zip", ".gz", ".7z"):
            assert lg.model_like(f"x{ext}", b"plain text", 10) is not None, ext
        assert lg.model_like("x.bin", b"plain text", 10) is None


# ---------------------------------------------------------------------------
# Licence guard: scans
# ---------------------------------------------------------------------------


def _installed_android(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    det, emb = ONNX_LIKE + b"detector", ONNX_LIKE + b"embedder"
    (folder / "yolox_s.onnx").write_bytes(det)
    (folder / "clip_vit_b16_laion2b.onnx").write_bytes(emb)
    (folder / "model-notices.md").write_text("notices")
    (folder / ".gitkeep").write_bytes(b"")
    _manifest(folder, {"yolox_s.onnx": _sha(det), "clip_vit_b16_laion2b.onnx": _sha(emb)})
    return folder


def _coreml_package(path: Path, weights_sha: str | None = mm.YOLOX_S_WEIGHTS.sha256) -> Path:
    (path / "Data" / "com.apple.CoreML" / "weights").mkdir(parents=True)
    (path / "Manifest.json").write_text("{}")
    (path / "Data" / "com.apple.CoreML" / "model.mlmodel").write_bytes(_coreml_bytes(weights_sha))
    (path / "Data" / "com.apple.CoreML" / "weights" / "weight.bin").write_bytes(b"\x00" * 128)
    return path


class TestLicenceGuardScan:
    def test_an_installed_asset_folder_passes(self, tmp_path, capsys):
        assets = _installed_android(tmp_path / "assets")
        assert lg.main([str(assets)]) == 0
        assert "no forbidden or unlisted model files" in capsys.readouterr().out

    def test_a_forbidden_file_fails_and_is_named(self, tmp_path, capsys):
        assets = _installed_android(tmp_path / "assets")
        (assets / "yolo11n.onnx").write_bytes(b"no")
        assert lg.main([str(assets)]) == 1
        err = capsys.readouterr().err
        assert "yolo11n.onnx" in err and "yolox_s.onnx" not in err

    def test_a_forbidden_folder_name_is_caught_once(self, tmp_path):
        (tmp_path / "insightface" / "models").mkdir(parents=True)
        (tmp_path / "insightface" / "models" / "model.onnx").write_bytes(b"x")
        findings = lg.scan([tmp_path])
        assert [Path(f.location).name for f in findings] == ["insightface"]

    def test_a_built_app_bundle_with_a_compiled_forbidden_model_fails(self, tmp_path):
        app = tmp_path / "FlightRisk.app"
        (app / "YOLOXs.mlmodelc").mkdir(parents=True)
        (app / "YOLOXs.mlmodelc" / "metadata.json").write_bytes(
            b'[{"userDefinedMetadata": {"flightrisk.weights_sha256": "' + mm.YOLOX_S_WEIGHTS.sha256.encode() + b'"}}]')
        (app / "YOLOXs.mlmodelc" / "coremldata.bin").write_bytes(b"ok")
        assert lg.scan([app]) == []
        (app / "SCRFDFaceDetector.mlmodelc").mkdir()
        (app / "SCRFDFaceDetector.mlmodelc" / "coremldata.bin").write_bytes(b"x")
        findings = lg.scan([app])
        assert len(findings) == 1 and findings[0].rule == "name:scrfdfacedetector"

    def test_an_apk_is_read_entry_by_entry(self, tmp_path):
        model = ONNX_LIKE + b"detector"
        manifest = json.dumps({"generated_by": lg.MANIFEST_GENERATOR,
                               "models": [{"file": "yolox_s.onnx", "sha256": _sha(model)}]}).encode()
        clean = _zip(tmp_path / "clean.apk", {"assets/yolox_s.onnx": model, "assets/mobile-models.json": manifest,
                                              "classes.dex": b"dex\n035\x00", "res/raw/alarm.mp3": b"a"})
        dirty = _zip(tmp_path / "dirty.apk", {"assets/yolox_s.onnx": model, "assets/mobile-models.json": manifest,
                                              "assets/arcface_mobilefacenet.onnx": b"y"})
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
        (dist / "output-metadata.json").write_text("{}")
        findings = lg.scan([dist])
        assert len(findings) == 1 and findings[0].location.endswith("!assets/mobileclip_s2.onnx")

    def test_a_nested_archive_is_read(self, tmp_path):
        inner = _zip_bytes({"assets/scrfd_500m.onnx": b"x"})
        outer = _zip(tmp_path / "bundle.apks", {"splits/base-master.apk": inner})
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
        monkeypatch.setattr(lg, "_HEAD", 8)
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
        (tmp_path / "about.json").write_text('{"note": "no ultralytics"}')
        assert lg.scan([tmp_path]) == []

    def test_a_missing_path_is_an_error_not_a_pass(self, tmp_path, capsys):
        assert lg.main([str(tmp_path / "nope")]) == 2
        assert "does not exist" in capsys.readouterr().err

    def test_a_broken_archive_is_an_error_not_a_pass(self, tmp_path):
        (tmp_path / "broken.apk").write_bytes(b"PK\x03\x04 truncated")
        assert lg.main([str(tmp_path / "broken.apk")]) == 2

    def test_the_command_line_runs_without_the_conversion_tools(self, tmp_path):
        (tmp_path / "yolov8n.tflite").write_bytes(b"x")
        out = subprocess.run([sys.executable, str(PIPELINE / "licence_guard.py"), str(tmp_path)],
                             capture_output=True, text=True, check=False)
        assert out.returncode == 1 and "yolov8n.tflite" in out.stderr

    def test_the_guard_uses_only_the_standard_library(self):
        import ast

        tree = ast.parse((PIPELINE / "licence_guard.py").read_text())
        modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert modules <= set(sys.stdlib_module_names) | {"__future__"}

    @pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python")
    def test_the_guard_runs_on_the_system_python_xcode_uses(self, tmp_path):
        version = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info[:2] >= (3, 9))"],
                                 capture_output=True, text=True, check=False)
        if version.stdout.strip() != "True":
            pytest.skip("the system Python is older than 3.9")
        assets = _installed_android(tmp_path / "assets")
        ok = subprocess.run(["/usr/bin/python3", str(PIPELINE / "licence_guard.py"), str(assets)], capture_output=True, text=True, check=False)
        assert ok.returncode == 0, ok.stderr
        (assets / "weights.dat").write_bytes(ONNX_LIKE)
        bad = subprocess.run(["/usr/bin/python3", str(PIPELINE / "licence_guard.py"), str(assets)], capture_output=True, text=True, check=False)
        assert bad.returncode == 1 and "weights.dat" in bad.stderr

    @pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python")
    def test_the_system_python_opens_archives_found_in_a_folder(self, tmp_path):
        """Before 3.11 a SpooledTemporaryFile cannot be opened as a zip; the guard must not depend on it."""
        version = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info[:2] >= (3, 9))"],
                                 capture_output=True, text=True, check=False)
        if version.stdout.strip() != "True":
            pytest.skip("the system Python is older than 3.9")
        guard = [str(PIPELINE / "licence_guard.py")]
        model = ONNX_LIKE + b"detector"
        manifest = json.dumps({"generated_by": lg.MANIFEST_GENERATOR,
                               "models": [{"file": "yolox_s.onnx", "sha256": _sha(model)}]}).encode()
        apk_entries = {"assets/yolox_s.onnx": model, "assets/mobile-models.json": manifest,
                       "okhttp3/internal/publicsuffix/publicsuffixes.gz": gzip.compress(b"com\nnet\n"),
                       "classes.dex": b"dex\n035\x00"}
        # an output folder as Gradle hands it over: the APK (a zip inside the folder) and its metadata
        release = tmp_path / "apk" / "release"
        release.mkdir(parents=True)
        _zip(release / "app-release.apk", apk_entries)
        (release / "output-metadata.json").write_text("{}")
        (release / "baselineProfiles" / "0").mkdir(parents=True)
        _zip(release / "baselineProfiles" / "0" / "app-release.dm", {"primary.prof": b"pro\x00010"})
        ok = subprocess.run(["/usr/bin/python3", *guard, str(release)], capture_output=True, text=True, check=False)
        assert ok.returncode == 0, ok.stderr
        # a zip nested in the APK, holding a model, is opened and caught
        _zip(release / "app-release.apk", {**apk_entries, "assets/textures.bin": _zip_bytes({"det_10g.onnx": b"x"})})
        bad = subprocess.run(["/usr/bin/python3", *guard, str(release)], capture_output=True, text=True, check=False)
        assert bad.returncode == 1, bad.stderr
        assert "textures.bin!det_10g.onnx" in bad.stderr and "Traceback" not in bad.stderr
        # and a broken gzip is an error, not a traceback
        _zip(release / "app-release.apk", {**apk_entries, "okhttp3/broken.gz": gzip.compress(b"text" * 100)[:-12] + b"\x00" * 12})
        broken = subprocess.run(["/usr/bin/python3", *guard, str(release)], capture_output=True, text=True, check=False)
        assert broken.returncode == 2 and "Traceback" not in broken.stderr, broken.stderr


class TestLicenceGuardAllowlist:
    """Only what make_models.py installed may ship: renamed and archived models are caught."""

    def test_an_unlisted_model_fails(self, tmp_path):
        assets = _installed_android(tmp_path / "assets")
        (assets / "extra.onnx").write_bytes(ONNX_LIKE)
        assert [(Path(f.location).name, f.rule) for f in lg.scan([assets])] == [("extra.onnx", "allowlist:unlisted")]

    def test_a_listed_model_with_another_digest_fails(self, tmp_path):
        assets = _installed_android(tmp_path / "assets")
        (assets / "yolox_s.onnx").write_bytes(ONNX_LIKE + b"swapped")
        assert [(Path(f.location).name, f.rule) for f in lg.scan([assets])] == [("yolox_s.onnx", "allowlist:digest")]

    def test_a_model_without_any_manifest_fails(self, tmp_path):
        (tmp_path / "yolox_s.onnx").write_bytes(ONNX_LIKE)
        assert [f.rule for f in lg.scan([tmp_path])] == ["allowlist:unlisted"]

    def test_a_renamed_scrfd_is_caught_by_its_digest(self, tmp_path, monkeypatch):
        fake_scrfd = ONNX_LIKE + b"synthetic stand-in for scrfd_500m.onnx"
        monkeypatch.setitem(lg.DENYLIST_SHA256, _sha(fake_scrfd), (len(fake_scrfd), "test: SCRFD 500M"))
        assets = _installed_android(tmp_path / "assets")
        (assets / "person_detector_v2.onnx").write_bytes(fake_scrfd)
        # even listed in a manifest with its own digest, it is refused
        listed = json.loads((assets / lg.MANIFEST_NAME).read_text())
        listed["models"].append({"file": "person_detector_v2.onnx", "sha256": _sha(fake_scrfd)})
        (assets / lg.MANIFEST_NAME).write_text(json.dumps(listed))
        findings = lg.scan([assets])
        assert [(Path(f.location).name, f.rule) for f in findings] == [("person_detector_v2.onnx", "denylist")]
        assert "SCRFD 500M" in findings[0].reason

    def test_a_dat_file_holding_a_model_is_caught_by_its_bytes(self, tmp_path):
        assets = _installed_android(tmp_path / "assets")
        (assets / "config.dat").write_bytes(ONNX_LIKE)
        (assets / "labels.dat").write_bytes(b"person\nbicycle\n")
        assert [(Path(f.location).name, f.rule) for f in lg.scan([assets])] == [("config.dat", "allowlist:unlisted")]

    def test_a_gz_archive_in_an_asset_folder_is_caught(self, tmp_path):
        assets = _installed_android(tmp_path / "assets")
        (assets / "dictionary.gz").write_bytes(gzip.compress(b"plain words"))
        (assets / "readme.txt").write_bytes(gzip.compress(ONNX_LIKE))  # an archive under a text name
        found = sorted((Path(f.location).name, f.rule) for f in lg.scan([assets]))
        assert found == [("dictionary.gz", "allowlist:unlisted"), ("readme.txt", "allowlist:unlisted")]

    def test_a_zip_with_another_name_is_opened(self, tmp_path):
        assets = _installed_android(tmp_path / "assets")
        (assets / "textures.bin").write_bytes(_zip_bytes({"det_10g.onnx": b"x", "notes.txt": b"y"}))
        found = sorted((f.location.split("/")[-1], f.rule) for f in lg.scan([assets]))
        assert ("textures.bin", "allowlist:unlisted") in found
        assert ("textures.bin!det_10g.onnx", "name:det_10g") in found

    def test_a_symlinked_folder_is_caught(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "scrfd_500m.onnx").write_bytes(b"x")
        assets = _installed_android(tmp_path / "assets")
        (assets / "extra").symlink_to(outside, target_is_directory=True)
        (assets / "linked.onnx").symlink_to(outside / "scrfd_500m.onnx")
        found = sorted((Path(f.location).name, f.rule) for f in lg.scan([assets]))
        assert found == [("extra", "symlink"), ("linked.onnx", "symlink")]

    def test_a_symlink_given_as_the_path_is_caught(self, tmp_path):
        assets = _installed_android(tmp_path / "assets")
        (tmp_path / "link").symlink_to(assets, target_is_directory=True)
        assert [f.rule for f in lg.scan([tmp_path / "link"])] == ["symlink"]

    def test_a_symlink_inside_an_archive_is_caught(self, tmp_path):
        apk = tmp_path / "app.apk"
        with zipfile.ZipFile(apk, "w") as zf:
            info = zipfile.ZipInfo("assets/linked")
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
        assert [f.rule for f in lg.scan([apk])] == ["symlink"]

    @pytest.mark.parametrize("blob", [
        gzip.compress(b"plain text " * 200)[:40],  # cut short
        b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\xff" * 64,  # a gzip header and no valid deflate data
        bz2.compress(b"plain text " * 200)[:30],
        lzma.compress(b"plain text " * 200)[:40],
    ])
    def test_a_broken_compressed_file_is_an_error_not_a_crash(self, tmp_path, blob):
        apk = _zip(tmp_path / "app.apk", {"okhttp3/data.gz": blob})
        with pytest.raises(lg.GuardError, match="cannot be read"):
            lg.scan([apk])
        assert lg.main([str(apk)]) == 2

    def test_core_ml_models_need_the_pinned_weights_in_their_metadata(self, tmp_path):
        models = tmp_path / "Models"
        models.mkdir()
        _coreml_package(models / "YOLOXs.mlpackage")
        _coreml_package(models / "CLIPViTB16LAION.mlpackage", mm.CLIP_WEIGHTS.sha256)
        assert lg.scan([models]) == []
        _coreml_package(models / "Detector.mlpackage", weights_sha=None)
        _coreml_package(models / "Other.mlpackage", weights_sha="0" * 64)
        found = sorted((Path(f.location).name, f.rule) for f in lg.scan([models]))
        assert found == [("Detector.mlpackage", "allowlist:coreml-metadata"), ("Other.mlpackage", "allowlist:coreml-metadata")]

    def test_a_listed_core_ml_package_must_have_the_listed_digest(self, tmp_path):
        models = tmp_path / "Models"
        models.mkdir()
        pkg = _coreml_package(models / "YOLOXs.mlpackage")
        _manifest(models, {"YOLOXs.mlpackage": mm.tree_sha256(pkg)})
        assert lg.scan([models]) == []
        (pkg / "Data" / "com.apple.CoreML" / "weights" / "weight.bin").write_bytes(b"\x01" * 128)
        assert [f.rule for f in lg.scan([models])] == ["allowlist:digest"]

    def test_an_archive_inside_a_core_ml_folder_is_caught(self, tmp_path):
        pkg = _coreml_package(tmp_path / "Models" / "YOLOXs.mlpackage")
        (pkg / "Data" / "extra.bin").write_bytes(_zip_bytes({"w600k_mbf.onnx": b"x"}))
        found = sorted((f.location.split("/")[-1], f.rule) for f in lg.scan([tmp_path / "Models"]))
        assert ("extra.bin", "allowlist:unlisted") in found and ("extra.bin!w600k_mbf.onnx", "name:w600k_mbf") in found

    def test_a_manifest_install_did_not_write_is_refused(self, tmp_path):
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / "yolox_s.onnx").write_bytes(ONNX_LIKE)
        _manifest(assets, {"yolox_s.onnx": _sha(ONNX_LIKE)}, generator="hand written")
        assert sorted(f.rule for f in lg.scan([assets])) == ["allowlist:manifest", "allowlist:unlisted"]
        _manifest(assets, {"../yolox_s.onnx": _sha(ONNX_LIKE)})
        assert "allowlist:manifest" in [f.rule for f in lg.scan([assets])]

    def test_a_manifest_lists_only_its_own_folder(self, tmp_path):
        assets = _installed_android(tmp_path / "assets")
        (assets / "sub").mkdir()
        shutil.copy2(assets / "yolox_s.onnx", assets / "sub" / "yolox_s.onnx")
        assert [f.location.endswith("sub/yolox_s.onnx") for f in lg.scan([assets])] == [True]

    def test_outside_an_apks_assets_a_compressed_file_is_judged_by_what_it_holds(self, tmp_path):
        model = ONNX_LIKE + b"detector"
        manifest = json.dumps({"generated_by": lg.MANIFEST_GENERATOR,
                               "models": [{"file": "yolox_s.onnx", "sha256": _sha(model)}]}).encode()
        base = {"assets/yolox_s.onnx": model, "assets/mobile-models.json": manifest}
        # OkHttp ships its public suffix list gzipped among the Java resources
        ok = _zip(tmp_path / "ok.apk", {**base, "okhttp3/internal/publicsuffix/publicsuffixes.gz": gzip.compress(b"com\nnet\n")})
        assert lg.scan([ok]) == []
        bad = _zip(tmp_path / "bad.apk", {**base, "okhttp3/data.gz": gzip.compress(ONNX_LIKE),
                                          "lib/blob.xz": lzma.compress(_zip_bytes({"a": b"b"})),
                                          "lib/blob.zst": b"\x28\xb5\x2f\xfd" + b"\x00" * 16,
                                          "res/raw/detector.tflite": b"\x1c\x00\x00\x00TFL3" + b"\x00" * 40})
        found = sorted((f.location.split("!")[-1], f.rule) for f in lg.scan([bad]))
        assert found == [("lib/blob.xz", "allowlist:compressed"), ("lib/blob.zst", "allowlist:uninspectable"),
                         ("okhttp3/data.gz", "allowlist:compressed"), ("res/raw/detector.tflite", "allowlist:unlisted")]
        # inside the APK's asset folder, the same gzip of a word list is refused
        strict = _zip(tmp_path / "strict.apk", {**base, "assets/words.gz": gzip.compress(b"com\nnet\n")})
        assert [(f.location.split("!")[-1], f.rule) for f in lg.scan([strict])] == [("assets/words.gz", "allowlist:unlisted")]

    def test_an_aab_keeps_its_assets_under_the_module(self, tmp_path):
        aab = _zip(tmp_path / "app.aab", {"base/root/okhttp3/x.gz": gzip.compress(b"text"),
                                          "base/assets/words.gz": gzip.compress(b"text"),
                                          "BundleConfig.pb": b"\x0a\x02\x08\x01"})
        assert [(f.location.split("!")[-1], f.rule) for f in lg.scan([aab])] == [("base/assets/words.gz", "allowlist:unlisted")]

    def test_denylisted_files_are_caught_anywhere_and_by_size_first(self, tmp_path, monkeypatch):
        blob = b"synthetic stand-in for w600k_mbf.onnx " * 4
        monkeypatch.setitem(lg.DENYLIST_SHA256, _sha(blob), (len(blob), "test: ArcFace MBF"))
        apk = _zip(tmp_path / "app.apk", {"res/raw/sound.ogg": blob})
        assert [f.rule for f in lg.scan([apk])] == ["denylist"]
        assert [f.rule for f in lg.scan([apk], allowlist=False)] == ["denylist"]

    def test_the_denylist_covers_the_insightface_packs_and_the_old_app_models(self):
        labels = " ".join(label for _, label in lg.DENYLIST_SHA256.values())
        for pack in ("buffalo_l.zip", "buffalo_s.zip", "buffalo_sc.zip", "antelopev2.zip", "buffalo_m.zip",
                     "det_500m.onnx", "w600k_mbf.onnx", "w600k_r50.onnx", "det_10g.onnx", "glintr100.onnx",
                     "1k3d68.onnx", "2d106det.onnx", "genderage.onnx", "det_2.5g.onnx", "scrfd_10g_bnkps.onnx",
                     "yolo11n.onnx", "clip_visual.onnx", "scrfd_500m.onnx", "arcface_mobilefacenet.onnx",
                     "YOLOPersonDetector.mlmodelc", "SCRFDFaceDetector.mlmodelc", "ArcFaceMobile.mlmodelc",
                     "CLIPVisual.mlmodelc"):
            assert pack in labels, pack
        for sha, (size, _) in lg.DENYLIST_SHA256.items():
            assert HEX64.match(sha) and size >= 65536

    def test_verify_checks_its_outputs_without_the_allowlist(self, tmp_path):
        out = tmp_path / "android"
        out.mkdir()
        (out / "yolox_s.onnx").write_bytes(ONNX_LIKE)
        assert lg.scan([out], allowlist=False) == []
        assert [f.rule for f in lg.scan([out])] == ["allowlist:unlisted"]
        (out / "yolo11n.onnx").write_bytes(ONNX_LIKE)
        assert [f.rule for f in lg.scan([out], allowlist=False)] == ["name:yolo11n"]


# ---------------------------------------------------------------------------
# Parity metrics and verify, with fake runners
# ---------------------------------------------------------------------------


def _det_output(seed: int) -> np.ndarray:
    """A plausible [8400, 85] detector output with a few confident boxes."""
    rng = np.random.default_rng(seed)
    out = np.zeros((mp.YOLOX_NUM_ANCHORS, mp.YOLOX_ROW_LENGTH), dtype=np.float32)
    out[:, :2] = rng.uniform(0, 640, size=(mp.YOLOX_NUM_ANCHORS, 2))
    out[:, 2:4] = rng.uniform(8, 60, size=(mp.YOLOX_NUM_ANCHORS, 2))
    out[:, 4:] = rng.uniform(0, 0.02, size=(mp.YOLOX_NUM_ANCHORS, 81))
    for i, row in enumerate((100, 2000, 7000)):
        out[row, :4] = [100 + 150 * i, 200, 40, 90]
        out[row, 4], out[row, 5] = 0.95, 0.9
    return out


class _FakeRunner:
    """Returns the reference output (optionally changed) instead of running a model."""

    def __init__(self, det=None, emb=None, change=None):
        self.det, self.emb, self.change = det, emb, change or (lambda a: a)

    def detector(self, canvas):
        return self.change(self.det[int(canvas[0, 0, 0])].copy())

    def embedder(self, tensor):
        return self.change(self.emb[round(float(tensor[0, 0, 0, 0]))].copy())


def _canvases(n: int):
    """``n`` canvases whose first pixel names the reference output they map to."""
    out = []
    for i in range(n):
        c = np.zeros((640, 640, 3), dtype=np.uint8)
        c[0, 0, 0] = i
        out.append((f"in{i}", c, 1.0))
    return out


TOL_DET = {"score_max_abs": 1e-3, "box_max_abs_px": 0.05, "match_iou_min": 0.999}
TOL_EMB = {"cosine_min": 0.995, "similarity_max_abs": 0.01}


class TestDetectorMetrics:
    def setup_method(self):
        self.refs_list = [_det_output(s) for s in range(3)]
        self.refs = {f"in{i}": r for i, r in enumerate(self.refs_list)}
        self.canvases = _canvases(3)

    def test_identical_outputs_are_within_tolerance(self):
        m = mm._detector_metrics(_FakeRunner(det=self.refs_list), self.canvases, self.refs, TOL_DET)
        assert m["within_tolerance"] and m["score_max_abs"] == 0 and m["matched_detections"] == 9
        assert m["missing_detections"] == m["extra_detections"] == 0

    def test_a_score_change_is_measured(self):
        def nudge(a):
            a[100, 5] -= 0.01
            return a

        m = mm._detector_metrics(_FakeRunner(det=self.refs_list, change=nudge), self.canvases, self.refs, TOL_DET)
        assert not m["within_tolerance"] and m["score_max_abs"] == pytest.approx(0.0095, abs=1e-4)

    @pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
    def test_a_value_that_is_not_finite_fails(self, bad):
        def poison(a):
            a[5000, 7] = bad  # one class score of a row no detection uses
            return a

        m = mm._detector_metrics(_FakeRunner(det=self.refs_list, change=poison), self.canvases, self.refs, TOL_DET)
        assert m["within_tolerance"] is False
        assert len(m["problems"]) == 3 and "not finite" in m["problems"][0]

    def test_an_all_nan_output_fails(self):
        m = mm._detector_metrics(_FakeRunner(det=self.refs_list, change=lambda a: a * np.nan),
                                 self.canvases, self.refs, TOL_DET)
        assert m["within_tolerance"] is False

    def test_nan_survives_the_running_maximum(self):
        # Python's max(0.0, nan) is 0.0; the metrics combine with numpy so a NaN that got through still fails.
        assert max(0.0, float("nan")) == 0.0
        assert np.isnan(mm._finite_max(0.0, float("nan")))
        assert np.isnan(mm._finite_min(1.0, float("nan")))

    def test_the_sample_sanity_check_fails_on_nan(self):
        assert mm._sample_sanity_problem({"dog": 0.91, "bicycle": 0.95}) is None
        assert mm._sample_sanity_problem({"dog": 0.91, "bicycle": 0.5})
        assert mm._sample_sanity_problem({"dog": float("nan"), "bicycle": 0.95})
        assert mm._sample_sanity_problem({"dog": 0.95, "bicycle": float("nan")})

    def test_a_wrong_shape_fails(self):
        m = mm._detector_metrics(_FakeRunner(det=self.refs_list, change=lambda a: a[:100]),
                                 self.canvases, self.refs, TOL_DET)
        assert m["within_tolerance"] is False and "shape" in m["problems"][0]


class TestEmbedderMetrics:
    def setup_method(self):
        rng = np.random.default_rng(1)
        self.emb = [mp.l2_normalize(rng.normal(size=512)) for _ in range(4)]
        self.tensors = [np.full((1, 3, 224, 224), float(i), dtype=np.float32) for i in range(4)]
        self.ref = np.stack(self.emb)
        self.sims = self.ref @ self.ref.T

    def test_identical_embeddings_are_within_tolerance(self):
        m = mm._embedder_metrics(_FakeRunner(emb=self.emb), self.tensors, self.ref, self.sims, TOL_EMB)
        assert m["within_tolerance"] and m["cosine_min"] == pytest.approx(1.0)

    @pytest.mark.parametrize("bad", [np.nan, np.inf])
    def test_an_embedding_that_is_not_finite_fails(self, bad):
        def poison(a):
            a[3] = bad
            return a

        m = mm._embedder_metrics(_FakeRunner(emb=self.emb, change=poison), self.tensors, self.ref, self.sims, TOL_EMB)
        assert m["within_tolerance"] is False and "not finite" in m["problems"][0]

    def test_a_nan_embedding_is_not_normalised_into_zeros_first(self):
        # l2_normalize turns a NaN vector into zeros; the check runs before it.
        assert not np.isnan(mp.l2_normalize(np.full(4, np.nan))).any()
        m = mm._embedder_metrics(_FakeRunner(emb=self.emb, change=lambda a: a * np.nan),
                                 self.tensors, self.ref, self.sims, TOL_EMB)
        assert m["within_tolerance"] is False

    def test_a_change_near_the_match_threshold_is_gated_on_its_own(self):
        # two embeddings whose PyTorch similarity is 0.56, near the ground's 0.55, and one far from it
        a = np.zeros(512)
        a[0] = 1.0
        b = np.zeros(512)
        b[0], b[1] = 0.56, np.sqrt(1 - 0.56 ** 2)
        c = np.zeros(512)
        c[2] = 1.0
        emb = [a, b, c]
        tensors = [np.full((1, 3, 224, 224), float(i), dtype=np.float32) for i in range(3)]
        ref = np.stack(emb)
        sims = ref @ ref.T

        def nudge(v, idx, amount):
            v = v.copy()
            v[idx] += amount
            return mp.l2_normalize(v)

        tol = {"cosine_min": 0.99, "similarity_max_abs": 0.02, "similarity_near_threshold_max_abs": 0.01}
        # b moves towards a by about 0.015: within 0.02 overall, but a near-threshold pair moves 0.015
        moved = [a, nudge(b, 0, 0.018), c]
        m = mm._embedder_metrics(_FakeRunner(emb=moved), tensors, ref, sims, tol)
        assert m["pairs_near_threshold"] == 2  # (a, b) and (b, a)
        assert 0.01 < m["similarity_near_threshold_max_abs"] < 0.02 and m["similarity_max_abs"] < 0.02
        assert m["within_tolerance"] is False
        assert mm._embedder_metrics(_FakeRunner(emb=moved), tensors, ref, sims,
                                    {"cosine_min": 0.99, "similarity_max_abs": 0.02})["within_tolerance"] is True
        # the same size of change on a pair far from the threshold passes
        far = [a, b, nudge(c, 3, 0.12)]
        m = mm._embedder_metrics(_FakeRunner(emb=far), tensors, ref, sims, tol)
        assert m["similarity_near_threshold_max_abs"] < 1e-9 and m["within_tolerance"] is True

    def test_the_android_default_is_gated_near_the_threshold_on_the_person_inputs(self):
        person = mm.PERSON_TOLERANCES["android/clip_vit_b16_laion2b_w8.onnx"]
        assert person["similarity_near_threshold_max_abs"] == 0.01 and person["similarity_max_abs"] == 0.02
        assert mm.GROUND_MATCH_THRESHOLD == 0.55 and mm.NEAR_THRESHOLD_BAND == 0.05
        from flightrisk.config import VisionConfig

        assert VisionConfig().reid_threshold == mm.GROUND_MATCH_THRESHOLD

    def test_a_drift_is_measured(self):
        rng = np.random.default_rng(2)
        m = mm._embedder_metrics(_FakeRunner(emb=self.emb, change=lambda a: a + rng.normal(scale=0.002, size=512)),
                                 self.tensors, self.ref, self.sims, TOL_EMB)
        assert 0.99 < m["cosine_min"] < 1.0


def _fake_references():
    """A detector and an embedder reference that depend on their input's colours."""

    def detector(canvas):
        out = np.zeros((mp.YOLOX_NUM_ANCHORS, mp.YOLOX_ROW_LENGTH), dtype=np.float32)
        b, r = float(canvas[..., 0].mean()), float(canvas[..., 2].mean())
        out[:, 2:4] = 10
        out[:, 4] = 0.5 + (b - r) / 100.0
        out[:, 5] = 0.5
        out[42, :6] = [320, 240, 40, 120, 0.9, 0.9]  # one person
        return out

    def preprocess(img_rgb):
        return mp.clip_preprocess(img_rgb)

    def encode(tensor):
        a, c = float(tensor[0, 0].mean()), float(tensor[0, 2].mean())
        v = np.zeros(mp.CLIP_EMBEDDING_DIM)
        v[0], v[1] = 1.0, 10.0 * (a - c)
        return mp.l2_normalize(v)

    return detector, mm.EmbedderReference(preprocess, encode)


class TestRunVerify:
    """run_verify end to end with fake references and runners: nothing is loaded or downloaded."""

    def _setup(self, tmp_path, monkeypatch, change=None, emb_change=None):
        work = tmp_path / "work"
        for o in mm.outputs_for("android"):
            (work / "out" / o.platform).mkdir(parents=True, exist_ok=True)
            (work / "out" / o.platform / o.filename).write_bytes(ONNX_LIKE + o.filename.encode())
        (work / mm.BUILD_NAME).write_text(json.dumps({"tools_pinned": True, "tools": {}}))
        detector, embedder = _fake_references()
        rng = np.random.default_rng(3)
        photo = rng.integers(0, 256, size=(360, 480, 3), dtype=np.uint8)
        monkeypatch.setattr(mm, "check_tool_versions", lambda allow: {"pinned": True, "differences": [], "versions": {}})
        monkeypatch.setattr(mm, "detector_reference", lambda d: detector)
        monkeypatch.setattr(mm, "embedder_reference", lambda d: embedder)
        monkeypatch.setattr(mm, "_sample_image_bgr", lambda d: photo)
        monkeypatch.setattr(mm, "_person_photo_bgr", lambda d: photo)
        monkeypatch.setattr(mm, "_detector_inputs", lambda d: [("synthetic:a", photo)])
        monkeypatch.setattr(mm, "_clip_inputs_rgb", lambda d: [("synthetic:a", photo), ("synthetic:b", photo[:100, :50])])
        monkeypatch.setattr(mm, "check_onnx_interface", lambda *a: {"problems": []})

        class Runner:
            def __init__(self, path, units=None):
                self.path = path

            def detector(self, canvas):
                out = detector(canvas)
                return (change or {}).get(self.path.name, lambda a: a)(out)

            def embedder(self, tensor):
                out = embedder.encode(tensor)
                return (emb_change or {}).get(self.path.name, lambda a: a)(out)

        monkeypatch.setattr(mm, "_Runner", Runner)
        return work

    def _sanity_ok(self, monkeypatch):
        # The fake detector finds one "person" and no dog: accept the sanity checks for this test.
        real = mm.verify_detector

        def verify_detector(*a, **k):
            r = real(*a, **k)
            r["problems"] = [p for p in r["problems"] if "PyTorch YOLOX-S" not in p]
            return r

        monkeypatch.setattr(mm, "verify_detector", verify_detector)

    def test_a_clean_run_passes_and_records_everything(self, tmp_path, monkeypatch):
        work = self._setup(tmp_path, monkeypatch)
        self._sanity_ok(monkeypatch)
        report = mm.run_verify(work, "android", lambda m: None)
        assert report["passed"], report["problems"]
        assert report["tools_pinned"] is True
        saved = json.loads((work / mm.VERIFY_NAME).read_text())
        assert saved["detector"]["outputs"]["android/yolox_s.onnx"]["person_gated"] is True
        assert saved["detector"]["outputs"]["android/yolox_s_fp16.onnx"]["person_gated"] is False
        assert set(saved["embedder"]["outputs"]["android/clip_vit_b16_laion2b_w8.onnx"]) >= {"person", "silhouette", "cosine_min"}
        assert (work / mm.GOLDEN_DIR_NAME / "golden.json").is_file()

    def test_a_nan_from_one_output_fails_the_run(self, tmp_path, monkeypatch):
        def poison(a):
            a = a.copy()
            a[7, 9] = np.nan
            return a

        work = self._setup(tmp_path, monkeypatch, change={"yolox_s_fp16.onnx": poison})
        self._sanity_ok(monkeypatch)
        report = mm.run_verify(work, "android", lambda m: None)
        assert report["passed"] is False
        assert any("android/yolox_s_fp16.onnx" in p and "not finite" in p for p in report["problems"])
        assert mm.main(["install", "--work-dir", str(work), "--android", str(tmp_path / "a"), "--targets", "android"]) == 1

    def test_a_nan_embedding_fails_the_run(self, tmp_path, monkeypatch):
        work = self._setup(tmp_path, monkeypatch, emb_change={"clip_vit_b16_laion2b_int8.onnx": lambda a: a * np.nan})
        self._sanity_ok(monkeypatch)
        report = mm.run_verify(work, "android", lambda m: None)
        assert report["passed"] is False
        assert any("android/clip_vit_b16_laion2b_int8.onnx" in p for p in report["problems"])

    def test_the_person_inputs_gate_the_android_embedder(self, tmp_path, monkeypatch):
        def drift(a):
            if a.ndim == 1 and abs(a[0]) > 0:  # every embedding: move it a little
                a = mp.l2_normalize(a + np.r_[0, 0, 0.2, np.zeros(mp.CLIP_EMBEDDING_DIM - 3)])
            return a

        work = self._setup(tmp_path, monkeypatch, emb_change={"clip_vit_b16_laion2b_w8.onnx": drift,
                                                              "clip_vit_b16_laion2b_int8.onnx": drift})
        self._sanity_ok(monkeypatch)
        report = mm.run_verify(work, "android", lambda m: None)
        failed = [p for p in report["problems"] if "outside" in p]
        assert any("clip_vit_b16_laion2b_w8.onnx" in p and "gated on general, person" in p for p in failed)
        w8 = report["embedder"]["outputs"]["android/clip_vit_b16_laion2b_w8.onnx"]
        assert w8["person_gated"] and w8["person"]["cosine_min"] < w8["person_tolerance"]["cosine_min"]
        assert w8["person_tolerance"] == mm.PERSON_TOLERANCES["android/clip_vit_b16_laion2b_w8.onnx"]
        assert report["embedder"]["outputs"]["android/clip_vit_b16_laion2b_int8.onnx"]["person_gated"] is False

    def test_an_unpinned_build_is_recorded(self, tmp_path, monkeypatch):
        work = self._setup(tmp_path, monkeypatch)
        self._sanity_ok(monkeypatch)
        (work / mm.BUILD_NAME).write_text(json.dumps({"tools_pinned": False}))
        assert mm.run_verify(work, "android", lambda m: None)["tools_pinned"] is False
        (work / mm.BUILD_NAME).unlink()
        assert mm.run_verify(work, "android", lambda m: None)["tools_pinned"] is False

    def test_the_sanity_checks_fail_a_wrong_reference(self, tmp_path, monkeypatch):
        work = self._setup(tmp_path, monkeypatch)
        report = mm.run_verify(work, "android", lambda m: None)
        assert report["passed"] is False
        assert any("dog and bicycle" in p for p in report["problems"])
        assert any("people" in p for p in report["problems"])


class TestGoldenFixtures:
    def test_the_fixtures_chain_frame_detector_crop_and_embedder(self, tmp_path):
        cv2 = pytest.importorskip("cv2")
        detector, embedder = _fake_references()
        info = mm.write_golden(tmp_path / "golden", detector, embedder, lambda m: None)
        assert info["problems"] == []
        doc = json.loads((tmp_path / "golden" / "golden.json").read_text())
        for key, f in doc["files"].items():
            assert hashlib.sha256((tmp_path / "golden" / f["file"]).read_bytes()).hexdigest() == f["sha256"], key
        frame = cv2.imread(str(tmp_path / "golden" / "frame.png"), cv2.IMREAD_COLOR)
        expected, _ = mm.silhouette_scene()
        assert np.array_equal(frame, expected)  # drawn, not photographed, and lossless
        det_in = np.frombuffer((tmp_path / "golden" / "detector_input.f32").read_bytes(), dtype="<f4").reshape(1, 3, 640, 640)
        canvas, ratio = mp.yolox_letterbox(frame)
        np.testing.assert_array_equal(det_in, mp.yolox_input_tensor(canvas))
        assert doc["letterbox_ratio"] == ratio
        box = doc["crop_box"]["pixels"]
        crop = cv2.imread(str(tmp_path / "golden" / "crop.png"), cv2.IMREAD_COLOR)
        assert np.array_equal(crop, frame[box[1]:box[3], box[0]:box[2]])
        emb_in = np.frombuffer((tmp_path / "golden" / "embedder_input.f32").read_bytes(), dtype="<f4").reshape(1, 3, 224, 224)
        np.testing.assert_array_equal(emb_in, mp.clip_preprocess(np.ascontiguousarray(crop[:, :, ::-1])))
        assert len(doc["embedding"]) == mp.CLIP_EMBEDDING_DIM
        assert doc["channel_order_check"]["detector_score_max_abs_if_fed_rgb"] > 0.1
        assert doc["contract"]["embedder"]["crop_from_detection"] == mp.CROP_RULE

    def test_a_frame_that_does_not_show_a_channel_swap_is_a_problem(self, tmp_path):
        pytest.importorskip("cv2")
        _, embedder = _fake_references()

        def colour_blind(canvas):
            out = np.zeros((mp.YOLOX_NUM_ANCHORS, mp.YOLOX_ROW_LENGTH), dtype=np.float32)
            out[:, 4:6] = 0.5
            return out

        info = mm.write_golden(tmp_path / "golden", colour_blind, embedder, lambda m: None)
        assert info["problems"] and "channel swap" in info["problems"][0]

    def test_the_silhouette_scene_is_deterministic_and_in_frame(self):
        pytest.importorskip("cv2")
        a, boxes = mm.silhouette_scene()
        b, _ = mm.silhouette_scene()
        assert np.array_equal(a, b) and a.shape == (480, 800, 3)
        assert [y2 - y1 for _, y1, _, y2 in boxes] == [16, 24, 32, 48, 72, 96, 160]
        for x1, y1, x2, y2 in boxes:
            assert 0 <= x1 < x2 <= 800 and 0 <= y1 < y2 <= 480

    def test_person_inputs_are_640_frames_at_the_small_sizes(self):
        cv2 = pytest.importorskip("cv2")
        photo = np.random.default_rng(0).integers(0, 256, size=(1584, 2816, 3), dtype=np.uint8)
        persons = [{"box": [1200, 600, 1260, 800]}, {"box": [1400, 620, 1450, 780]}]
        inputs = mm._person_detector_inputs(photo, persons)
        assert [n for n, _ in inputs] == ["person:photo", "person:native", "person:h48", "person:h32", "person:h24", "person:h16"]
        assert all(img.shape == (640, 640, 3) for _, img in inputs[1:])
        copies = dict(mm._small_copies(np.zeros((180, 60, 3), dtype=np.uint8)))
        assert {k: v.shape[:2] for k, v in copies.items()} == {
            "native": (180, 60), "16px": (16, 5), "24px": (24, 8), "32px": (32, 11), "48px": (48, 16)}
        del cv2


class TestContract:
    def test_the_crop_rule_is_the_grounds(self):
        frame = np.arange(100 * 120 * 3, dtype=np.uint32).reshape(100, 120, 3)
        box = [10.9, 20.2, 50.99, 80.5]
        x1, y1, x2, y2 = np.array(box).astype(int)  # flightrisk/vision/detector.py
        np.testing.assert_array_equal(mp.crop_box(frame, box), frame[y1:y2, x1:x2])
        assert mp.crop_pixels(box, 100, 120) == (10, 20, 50, 80)
        # outside the frame: clipped first, as the ground's detector clips its boxes
        assert mp.crop_pixels([-5.5, -1, 130.2, 101], 100, 120) == (0, 0, 120, 100)
        assert "frame[y1:y2, x1:x2]" in mp.CROP_RULE and "no margin" in mp.CROP_RULE

    def test_the_contract_records_the_compute_units_of_the_ios_variant(self):
        by_key = {o.key: o for o in mm.OUTPUTS}
        assert "coreml_compute_units" not in mm.contract()["detector"]
        fp16 = mm.contract([by_key["ios/YOLOXsFP16.mlpackage"], by_key["ios/CLIPViTB16LAION8bit.mlpackage"]])
        assert fp16["detector"]["coreml_compute_units"] == "cpuAndGPU"
        assert fp16["embedder"]["coreml_compute_units"] == "all"
        assert mm.contract([by_key["ios/YOLOXs.mlpackage"]])["detector"]["coreml_compute_units"] == "all"
        assert "coreml_compute_units" not in mm.contract([by_key["android/yolox_s.onnx"]])["detector"]

    def test_gates_and_defaults(self):
        assert mm.DEFAULT_VARIANTS == {
            ("android", "detector"): "fp32", ("android", "embedder"): "int8-weights",
            ("ios", "detector"): "fp32", ("ios", "embedder"): "palettized8",
        }
        assert mm.COREML_GATE_UNITS["ios/YOLOXs.mlpackage"] == mm.COREML_ALL_UNITS
        assert mm.COREML_GATE_UNITS["ios/YOLOXsFP16.mlpackage"] == ("CPU_AND_GPU",)
        assert mm.COREML_COMPUTE_UNITS["ios/YOLOXsFP16.mlpackage"] == "cpuAndGPU"
        assert mm.PERSON_GATED == {"android/yolox_s.onnx", "android/clip_vit_b16_laion2b_w8.onnx",
                                   "android/clip_vit_b16_laion2b.onnx"}
        android_embedders = {o.key for o in mm.OUTPUTS if o.platform == "android" and o.role == "embedder"}
        assert android_embedders - mm.PERSON_GATED == {"android/clip_vit_b16_laion2b_int8.onnx"}
        assert set(mm.TOLERANCES) == {o.key for o in mm.OUTPUTS}
        assert set(mm.COREML_GATE_UNITS) == {o.key for o in mm.OUTPUTS if o.platform == "ios"}
        runs = mm._runs(next(o for o in mm.OUTPUTS if o.key == "ios/YOLOXsFP16.mlpackage"))
        assert [(label, gates) for label, _, gates in runs] == [
            ("coreml-CPU_AND_GPU", True), ("coreml-ALL", False), ("coreml-CPU_AND_NE", False), ("coreml-CPU_ONLY", False)]


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
# Docs, notices, build wiring and the old export script
# ---------------------------------------------------------------------------

#: The LAION model card at the pinned revision, quoted word for word.
LAION_CARD_QUOTES = (
    "**Any** deployed use case of the model - whether commercial or not - is currently out of scope.",
    ("Certain use cases which would fall under the domain of surveillance and facial recognition are always "
     "out-of-scope regardless of performance of the model."),
)


class TestDocsAndNotices:
    DOC = ROOT / "docs" / "mobile-models.md"

    def test_the_doc_carries_the_pins_and_contracts(self):
        doc = self.DOC.read_text()
        for d in mm.DOWNLOADS:
            assert d.sha256 in doc, d.key
        assert mm.CLIP_HF_REVISION in doc and mm.YOLOX_COMMIT in doc
        assert "make_models.py all" in doc
        for token in ("114", "top-left", "BGR", "[1, 8400, 85]", "objectness", "0.48145466", "0.26862954", "bicubic", "224",
                      "frame[y1:y2, x1:x2]", "no margin", "cpuAndGPU", "golden"):
            assert token in doc, token

    def test_the_doc_lists_the_follow_ups_and_legal_questions(self):
        doc = self.DOC.read_text()
        for token in ("D-FINE-N", "DINOv2", "HERIDAL", "12%", "0.8%", "GPL-3.0-or-later", "section 7",
                      "Free Software Foundation", "revision", "reid.py"):
            assert token in doc, token
        assert "not legal advice" in doc.lower()

    def test_the_laion_model_card_is_quoted_exactly(self):
        doc = self.DOC.read_text()
        for quote in LAION_CARD_QUOTES:
            assert quote in " ".join(doc.split()), quote

    def test_the_doc_records_the_measured_parity(self):
        doc = self.DOC.read_text()
        for o in mm.OUTPUTS:
            assert o.filename in doc, o.filename
        for token in ("16 px", "48 px", "silhouette", "person"):
            assert token in doc, token

    def test_the_doc_states_the_guards_limits_and_how_to_repin_the_photo(self):
        doc = " ".join(self.DOC.read_text().split())
        for token in ("out of scope", "raw deflate", "XOR", "zip or gzip anywhere in the iOS app",
                      "Re-pinning the person photo", "similarity_near_threshold_max_abs", "--allow-ungated-embedder",
                      ".licence-guard-rejected"):
            assert token in doc, token

    def test_the_doc_explains_the_tools_lock_and_the_test_command(self):
        doc = self.DOC.read_text()
        for token in ("requirements.lock", "--require-hashes", "--allow-unpinned-build",
                      "python -m pytest --noconftest tests/test_mobile_models.py", "onnxruntime-android` 1.23.2"):
            assert token in doc, token
        assert "1.18.0" not in doc  # the app's ONNX Runtime is 1.23.2
        assert "--require-hashes" in (PIPELINE / "requirements.txt").read_text()

    def test_the_doc_does_not_claim_the_sample_checks_the_channel_order(self):
        doc = " ".join(self.DOC.read_text().split())
        assert "BGR, 0..255 contract" not in doc
        assert "does not check the channel order" in doc

    def test_notices_carry_both_licences_and_every_copyright_line(self):
        notices = (PIPELINE / "NOTICES.md").read_text()
        assert "Apache License" in notices and "Version 2.0, January 2004" in notices
        # open_clip, OpenAI CLIP, LAION, mlx-swift, mlx-swift-examples, GzipSwift, ONNX Runtime
        assert notices.count("Permission is hereby granted, free of charge") == 7
        assert "MIT-CMU" in notices and "Copyright (c) 2014-2023 1024jp" in notices
        assert "Copyright (c) 2023 ml-explore" in notices and "Copyright 2022 Hugging Face SAS" in notices
        assert "https://github.com/KurtLehnardt/flightRisk" in notices and "has not been granted" in notices
        for line in ("Copyright (c) 2021-2022 Megvii Inc.", "Copyright (c) 2021 OpenAI",
                     "Copyright (c) 2012-2021 Gabriel Ilharco"):
            assert line in notices
        assert mm.YOLOX_S_WEIGHTS.sha256 in notices and mm.CLIP_WEIGHTS.sha256 in notices

    def test_notices_describe_the_changes_made(self):
        notices = " ".join((PIPELINE / "NOTICES.md").read_text().split())
        assert "constant grids" in notices  # the YOLOX decoding change
        assert "DequantizeLinear" in notices and "every inference" in notices  # the Android default embedder
        assert "dynamically quantised 8-bit integer weights)" not in notices

    def test_the_shipped_notices_cover_every_installed_model_and_pass_the_guard(self, tmp_path):
        """The installed notices name each installed file, and a folder holding them passes the guard."""
        notices = (PIPELINE / "NOTICES.md").read_text()
        for name in {o.installed_name for o in mm.OUTPUTS}:
            assert f"`{name}`" in notices, name
        data = notices.encode()
        assert lg.model_like(mm.NOTICES_INSTALLED_NAME, data[: 64 << 10], len(data)) is None
        _fake_outputs(tmp_path / "work")
        android, ios = tmp_path / "a", tmp_path / "i"
        mm.run_install(tmp_path / "work", android, ios, "all", {}, lambda m: None)
        assert (android / mm.NOTICES_INSTALLED_NAME).read_bytes() == data
        assert lg.scan([android, ios]) == []
        # the guard reads the notices like any other file: the same bytes under a model name are refused
        (android / "notices.onnx").write_bytes(data)
        assert [f.rule for f in lg.scan([android])] == ["allowlist:unlisted"]

    def test_the_old_clip_export_script_warns_against_store_builds(self):
        head = (ROOT / "scripts" / "export_clip_onnx.py").read_text()[:1500]
        assert "must not be used for store builds" in head
        assert "scripts/mobile_models/make_models.py" in head

    def test_the_release_builds_run_the_guard(self):
        gradle = (ROOT / "mobile" / "app" / "build.gradle.kts").read_text()
        assert "licence_guard.py" in gradle and "SingleArtifact.BUNDLE" in gradle and "SingleArtifact.APK" in gradle
        assert 'withBuildType("release")' in gradle
        assert '"bundle$name"' in gradle and '"assemble$name"' in gradle
        assert '".licence-guard-rejected"' in gradle and "Files.move" in gradle
        project = (ROOT / "ios" / "project.yml").read_text()
        assert "postBuildScripts" in project and "licence_guard.py" in project
        assert 'CONFIGURATION" != "Release"' in project
        assert "TARGET_BUILD_DIR" in project and "WRAPPER_NAME" in project
        pbxproj = (ROOT / "ios" / "FlightRisk.xcodeproj" / "project.pbxproj").read_text()
        assert "licence_guard.py" in pbxproj

    def test_the_release_docs_name_the_steps(self):
        signing = (ROOT / "docs" / "android-release-signing.md").read_text()
        assert "make_models.py" in signing and "licence_guard" in signing
        ios = (ROOT / "ios" / "README.md").read_text()
        assert "make_models.py" in ios and "licence guard" in ios.lower()
        compliance = " ".join((ROOT / "docs" / "app-store-compliance.md").read_text().split())
        assert "Which models ship, and their licences, is an open decision" not in compliance
        assert "YOLOX-S" in compliance and "docs/mobile-models.md" in compliance


# ---------------------------------------------------------------------------
# Conversion (needs the conversion tools; skipped without them)
# ---------------------------------------------------------------------------

needs_conversion_tools = pytest.mark.skipif(
    any(__import__("importlib").util.find_spec(m) is None for m in ("coremltools", "onnxruntime", "onnx", "torch", "open_clip")),
    reason="conversion tools from scripts/mobile_models/requirements.lock are not installed",
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
        assert lg.scan([tmp_path / "android"], allowlist=False) == []
        if on_macos:
            import coremltools as ct

            for name, units in (("YOLOXs.mlpackage", "all"), ("YOLOXsFP16.mlpackage", "cpuAndGPU")):
                pkg = tmp_path / "ios" / name
                assert mm.check_coreml_interface(pkg, True, mm.DETECTOR_INPUT, [1, 3, 640, 640], mm.DETECTOR_OUTPUT)["problems"] == []
                meta = ct.models.MLModel(str(pkg), skip_model_load=True).user_defined_metadata
                assert meta["flightrisk.coreml_compute_units"] == units, name
                assert meta["flightrisk.weights_sha256"] == mm.YOLOX_S_WEIGHTS.sha256
                out = mm._Runner(pkg, "CPU_AND_GPU").detector(canvas)
                assert out.shape == (mp.YOLOX_NUM_ANCHORS, mp.YOLOX_ROW_LENGTH) and np.isfinite(out).all()
            # the fp32 package matches PyTorch closely on the CPU
            fp32 = mm._Runner(tmp_path / "ios" / "YOLOXs.mlpackage", "CPU_ONLY").detector(canvas)
            np.testing.assert_allclose(fp32, ref, rtol=1e-3, atol=5e-2)
            # the converted packages carry the pinned metadata the guard's allowlist needs
            assert lg.scan([tmp_path / "ios"]) == []

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
        import onnx

        w8 = onnx.load(str(tmp_path / "android" / "clip_vit_b16_laion2b_w8.onnx"))
        assert sum(n.op_type == "DequantizeLinear" for n in w8.graph.node) > 0
        if on_macos:
            for name in ("CLIPViTB16LAION.mlpackage", "CLIPViTB16LAION8bit.mlpackage"):
                pkg = tmp_path / "ios" / name
                assert mm.check_coreml_interface(pkg, False, mm.EMBEDDER_INPUT, [1, 3, 224, 224], mm.EMBEDDER_OUTPUT)["problems"] == []
                assert mp.cosine(mm._Runner(pkg, "CPU_ONLY").embedder(x), ref) >= 0.98, name
            assert lg.scan([tmp_path / "ios"]) == []

    def test_dynamic_int8_refuses_an_unknown_node_to_keep(self, tmp_path):
        import torch
        from open_clip.transformer import VisionTransformer

        visual = VisionTransformer(image_size=224, patch_size=32, width=64, layers=1, heads=2, mlp_ratio=2.0, output_dim=512).eval()
        src = tmp_path / "e.onnx"
        mm.export_onnx(mm.clip_embedder_module(visual), torch.zeros(1, 3, 224, 224), src, "input", "embedding", {})
        with pytest.raises(mm.VerificationFailed, match="not in"):
            mm.onnx_to_dynamic_int8(src, tmp_path / "q.onnx", {}, ("/no/such/node",))

    def test_the_embedder_builds_with_the_network_refused(self, tmp_path):
        import torch

        state_path = tmp_path / "downloads"
        state_path.mkdir()
        # load_clip refuses unverified weights before building anything
        with pytest.raises(mm.ChecksumMismatch):
            mm.load_clip(state_path)
        # and building the model the way load_clip does fetches nothing
        import open_clip

        with mm.network_refused():
            model, _, _ = open_clip.create_model_and_transforms("ViT-B-16", pretrained=None)
        assert isinstance(model, torch.nn.Module)
