"""Tests for the altitude benchmark runner (R-070, R-071)."""

import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from flightrisk.evaluation.detection import (
    BinResult,
    DetectionEvalResult,
    bin_by_height,
    evaluate,
    load_voc_dir,
    match_detections,
    min_person_px,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "aerial_tiny"


def test_benchmark_doc_lists_candidates():
    text = (ROOT / "docs" / "altitude-benchmark.md").read_text()
    for name in ("HERIDAL", "WiSARD", "LADD", "SARD"):
        assert name in text
    assert "CC-BY-3.0" in text
    assert "zenodo.org/records/5662351" in text


def test_download_script_syntax():
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "download_heridal.sh")]).returncode == 0


def test_download_script_with_mocked_http(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload = tmp_path / "payload.zip"
    with zipfile.ZipFile(payload, "w") as z:
        z.writestr("JPEGImages/a.jpg", "x")
    curl = bin_dir / "curl"
    # Fake curl: copy the prepared zip to the --output path, no network.
    curl.write_text(
        '#!/bin/bash\nwhile [ $# -gt 0 ]; do [ "$1" = "--output" ] && out="$2"; shift; done\n'
        f'cp "{payload}" "$out"\n'
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", DATASETS_DIR=str(tmp_path / "ds"))
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "download_heridal.sh")], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "ds" / "heridal" / "JPEGImages" / "a.jpg").exists()


def test_voc_loader_reads_fixture():
    samples = load_voc_dir(FIXTURES)
    assert len(samples) == 3
    by_name = {s.image_path.stem: s for s in samples}
    assert by_name["img_a"].boxes == [(20.0, 20.0, 24.0, 26.0), (80.0, 50.0, 88.0, 70.0)]
    assert len(by_name["img_c"].boxes) == 2
    assert by_name["img_b"].width == 160 and by_name["img_b"].height == 120


class FakeDetector:
    """Returns ground truth boxes at least 10 px tall, plus a stray box on img_b."""

    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def detect(self, frame):
        s = self.samples[self.calls]
        self.calls += 1
        preds = [b for b in s.boxes if b[3] - b[1] >= 10]
        if s.image_path.stem == "img_b":
            preds.append((140, 100, 150, 112))  # false positive, 12 px tall
        return [{"bbox": list(b), "confidence": 0.9, "crop": None} for b in preds]


def test_matching_and_binning_with_mocked_detector():
    matches, miss_gt, extra = match_detections(
        [(0, 0, 10, 10), (50, 50, 60, 60)], [(1, 1, 10, 10), (100, 100, 110, 110)]
    )
    assert matches == [(0, 0)]
    assert miss_gt == [1]
    assert extra == [1]

    samples = load_voc_dir(FIXTURES)
    assert bin_by_height(samples) == [1, 1, 1, 0, 1, 1, 0]

    samples = sorted(samples, key=lambda s: s.image_path.name)
    result = evaluate(FakeDetector(samples), samples)
    b = result.bins
    assert (b[0].n_gt, b[0].tp, b[0].fn) == (1, 0, 1)  # 6 px person missed
    assert (b[1].n_gt, b[1].tp, b[1].fp) == (1, 1, 1)  # 12 px found, plus stray box
    assert b[1].recall == 1.0 and b[1].precision == 0.5
    assert b[4].recall == 1.0 and b[5].recall == 1.0
    assert sum(x.n_gt for x in b) == 5


def _result(recalls):
    bins = []
    for i, r in enumerate(recalls):
        bins.append(BinResult(lo=i * 8, hi=(i + 1) * 8, n_gt=10, tp=int(r * 10), fn=10 - int(r * 10)))
    return DetectionEvalResult(bins=bins)


def test_min_person_px_threshold():
    assert min_person_px(_result([0.0, 0.2, 0.5, 0.9])) == 16
    assert min_person_px(_result([0.0, 0.2, 0.4]), recall_floor=0.5) is None
    assert min_person_px(_result([0.0, 0.2, 0.4]), recall_floor=0.3) == 16
    empty_first = _result([0.0, 0.8])
    empty_first.bins[0].n_gt = 0
    assert min_person_px(empty_first) == 8


def test_cli_detect_dispatch(tmp_path, monkeypatch):
    import sys
    import types

    from flightrisk.evaluation import cli

    created = {}

    class Stub:
        def __init__(self, tiled=False):
            created["tiled"] = tiled

        def detect(self, frame):
            return []

    stub_module = types.ModuleType("flightrisk.vision.detector")
    stub_module.PersonDetector = Stub
    monkeypatch.setitem(sys.modules, "flightrisk.vision.detector", stub_module)
    out = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["x", "detect", "--dataset", str(FIXTURES), "--tiled", "1", "--output", str(out)])
    cli.main()
    data = json.loads(out.read_text())
    assert created["tiled"] is True
    assert data["tiled"] is True and data["n_images"] == 3
