"""Tests for the HERIDAL fine-tuning and sweep scripts (R-085).

All fixtures are tiny synthetic VOC images under pytest's tmp_path. Nothing
here downloads data, loads YOLO weights or trains a model; detectors are fakes.
"""

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from flightrisk.evaluation.detection import evaluate, load_voc_dir

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


finetune = _load("finetune_heridal")
sweep = _load("sweep_altitude")


def _write_voc(root: Path, stem: str, size: tuple[int, int], boxes: list[tuple[int, int, int, int]]) -> None:
    """Write one synthetic VOC sample: a gray image and its XML."""
    width, height = size
    (root / "JPEGImages").mkdir(parents=True, exist_ok=True)
    (root / "Annotations").mkdir(parents=True, exist_ok=True)
    image = np.full((height, width, 3), 90, dtype=np.uint8)
    for x1, y1, x2, y2 in boxes:
        image[y1:y2, x1:x2] = (30, 30, 200)
    cv2.imwrite(str(root / "JPEGImages" / f"{stem}.jpg"), image)
    objects = "".join(
        f"<object><name>person</name><bndbox><xmin>{x1}</xmin><ymin>{y1}</ymin>"
        f"<xmax>{x2}</xmax><ymax>{y2}</ymax></bndbox></object>"
        for x1, y1, x2, y2 in boxes
    )
    (root / "Annotations" / f"{stem}.xml").write_text(
        f"<annotation><filename>{stem}.jpg</filename><size><width>{width}</width>"
        f"<height>{height}</height><depth>3</depth></size>{objects}</annotation>"
    )


def test_voc_to_yolo_tile_labels():
    tile = (100, 200, 420, 520)  # 320 px square tile
    inside = (150, 250, 182, 314)  # 32x64, fully inside
    labels = finetune.voc_boxes_to_tile_labels([inside], tile)
    assert len(labels) == 1
    cls, cx, cy, w, h = labels[0]
    assert cls == 0
    assert cx == pytest.approx((166 - 100) / 320)
    assert cy == pytest.approx((282 - 200) / 320)
    assert w == pytest.approx(32 / 320)
    assert h == pytest.approx(64 / 320)

    # 75% of this box lies inside the tile: kept, clipped to the tile edge.
    mostly_in = (404, 300, 436, 340)
    (_, cx, _, w, _), = finetune.voc_boxes_to_tile_labels([mostly_in], tile)
    assert w == pytest.approx(16 / 320)
    assert cx == pytest.approx((412 - 100) / 320)

    # 25% inside: dropped at the default 0.5 visibility, kept at 0.2.
    mostly_out = (412, 300, 444, 340)
    assert finetune.voc_boxes_to_tile_labels([mostly_out], tile) == []
    assert len(finetune.voc_boxes_to_tile_labels([mostly_out], tile, min_visible=0.2)) == 1

    # Outside entirely, or degenerate: no label.
    assert finetune.voc_boxes_to_tile_labels([(0, 0, 50, 50), (150, 250, 150, 300)], tile) == []

    for _, cx, cy, w, h in labels:
        assert 0.0 <= cx - w / 2 and cx + w / 2 <= 1.0
        assert 0.0 <= cy - h / 2 and cy + h / 2 <= 1.0


def test_dataset_yaml_generated(tmp_path):
    train_dir, val_dir, out = tmp_path / "train", tmp_path / "val", tmp_path / "tiles"
    _write_voc(train_dir, "a", (1000, 700), [(100, 100, 130, 160), (700, 400, 740, 470)])
    _write_voc(train_dir, "b", (1000, 700), [])
    # HERIDAL ships images with no person as a bare <annotation/> without a size.
    (train_dir / "Annotations" / "b.xml").write_text('<?xml version="1.0"?>\n<annotation/>')
    _write_voc(val_dir, "c", (1000, 700), [(500, 300, 530, 360)])

    data_yaml = finetune.build_tiled_dataset(train_dir, val_dir, out, tile_size=320,
                                             overlap=0.2, empty_ratio=1.0, seed=0)

    assert data_yaml == out.resolve() / "data.yaml"
    text = data_yaml.read_text()
    assert f"path: {out.resolve()}" in text
    assert "train: images/train" in text and "val: images/val" in text
    assert "nc: 1" in text and "0: person" in text

    stats = json.loads((out / "stats.json").read_text())
    for split in ("train", "val"):
        images = sorted((out / "images" / split).glob("*.jpg"))
        labels = sorted((out / "labels" / split).glob("*.txt"))
        assert images and [p.stem for p in images] == [p.stem for p in labels]
        positives = [p for p in labels if p.read_text().strip()]
        empties = [p for p in labels if not p.read_text().strip()]
        assert len(positives) == stats[split]["positive_tiles"]
        # Empty tiles are subsampled to empty_ratio times the person tiles.
        assert len(empties) == stats[split]["empty_tiles_kept"] == min(
            stats[split]["empty_tiles_available"], len(positives))
        assert stats[split]["empty_tiles_available"] > len(empties)
        for p in images:
            assert cv2.imread(str(p)).shape[:2] == (320, 320)
        for p in positives:
            for line in p.read_text().splitlines():
                cls, *coords = line.split()
                assert cls == "0" and len(coords) == 4
                assert all(0.0 <= float(c) <= 1.0 for c in coords)
    # Every training person appears in at least one tile.
    assert stats["train"]["labels"] >= 2
    # Tile names carry the source image and the tile origin.
    assert any(p.stem.startswith("a_") for p in (out / "images" / "train").glob("*.jpg"))


def test_select_subset_excludes_test_and_is_stable():
    names = [f"train_{i:04d}" for i in range(50)] + ["test_0001", "test_0002"]
    small = finetune.select_subset(names, 10, seed=0, exclude={"test_0001", "test_0002"})
    large = finetune.select_subset(list(reversed(names)), 20, seed=0, exclude={"test_0001", "test_0002"})
    assert len(small) == 10 and len(set(small)) == 10
    assert not any(n.startswith("test_") for n in large)
    assert large[:10] == small


class _FakeDetector:
    """Returns fixed boxes per image with scores, filtered at its confidence."""

    def __init__(self, per_image: list[list[tuple[list[int], float]]], confidence: float):
        self.per_image = per_image
        self.confidence = confidence
        self.calls = 0

    def detect(self, frame):
        dets = self.per_image[self.calls % len(self.per_image)]
        self.calls += 1
        return [{"bbox": b, "confidence": c, "crop": None} for b, c in dets if c >= self.confidence]


def _sweep_fixture(tmp_path: Path) -> tuple[Path, list[list[tuple[list[int], float]]]]:
    ds = tmp_path / "test"
    _write_voc(ds, "img_a", (400, 300), [(10, 10, 30, 50), (100, 100, 140, 180)])
    _write_voc(ds, "img_b", (400, 300), [(200, 50, 216, 90)])
    per_image = [
        [([10, 10, 30, 50], 0.9), ([100, 100, 140, 180], 0.2), ([300, 200, 320, 240], 0.15)],
        [([200, 50, 216, 90], 0.3)],
    ]
    return ds, per_image


def test_sweep_writes_results_schema(tmp_path):
    ds, per_image = _sweep_fixture(tmp_path)
    built = []

    def factory(model, confidence):
        built.append((model, confidence))
        return _FakeDetector(per_image, confidence)

    out = tmp_path / "results" / "altitude_sweep.json"
    sweep.run_sweep(ds, ["yolo11n.pt", "models/yolo11s.pt"], [0.25, 0.1], out, detector_factory=factory)

    # One detection pass per model, at the lowest confidence.
    assert built == [("yolo11n.pt", 0.1), ("models/yolo11s.pt", 0.1)]
    data = json.loads(out.read_text())
    assert data["split"] == "test" and "test split" in data["split_note"]
    assert data["n_images"] == 2 and data["n_gt"] == 3
    assert data["tile_size"] == 640 and data["overlap"] == 0.2
    assert [(r["model"], r["confidence"]) for r in data["runs"]] == [
        ("yolo11n.pt", 0.1), ("yolo11n.pt", 0.25), ("yolo11s.pt", 0.1), ("yolo11s.pt", 0.25)]
    for run in data["runs"]:
        assert run["tiled"] is True
        assert {"bins", "min_person_px", "overall", "n_images"} <= set(run)
        assert len(run["bins"]) == 7 and run["bins"][-1]["hi"] is None
        for b in run["bins"]:
            assert {"lo", "hi", "n_gt", "tp", "fp", "fn", "recall", "precision"} <= set(b)
        assert {"tp", "fp", "fn", "recall", "precision", "f1"} <= set(run["overall"])

    low, high = data["runs"][0], data["runs"][1]
    assert low["overall"]["tp"] == 3 and low["overall"]["fp"] == 1
    assert high["overall"]["tp"] == 2 and high["overall"]["fp"] == 0
    assert data["best"]["model"] in ("yolo11n.pt", "yolo11s.pt")
    assert data["best"]["min_person_px"] == min(
        r["min_person_px"] for r in data["runs"] if r["min_person_px"] is not None)


def test_cached_confidences_match_direct_runs(tmp_path):
    ds, per_image = _sweep_fixture(tmp_path)
    samples = load_voc_dir(ds)
    cached, _ = sweep.evaluate_confidences(_FakeDetector(per_image, 0.1), samples, [0.1, 0.25, 0.5])
    for conf, result in cached.items():
        direct = evaluate(_FakeDetector(per_image, conf), samples)
        assert result.to_dict() == direct.to_dict()


def test_rank_prefers_lower_min_person_px_then_f1():
    def run(px, f1):
        return {"model": "m", "confidence": 0.1, "min_person_px": px, "overall": {"f1": f1}}

    runs = [run(None, 0.9), run(48, 0.2), run(32, 0.1), run(32, 0.3)]
    assert sweep.rank_key(min(runs, key=sweep.rank_key)) == (32.0, -0.3)


def test_select_confidence_uses_validation_f1(tmp_path):
    ds, per_image = _sweep_fixture(tmp_path)
    results, _ = sweep.evaluate_confidences(_FakeDetector(per_image, 0.1), load_voc_dir(ds), [0.1, 0.25, 0.5])
    chosen, rows = finetune.select_confidence(results)
    best_f1 = max(r["overall"]["f1"] for r in rows)
    assert [r["confidence"] for r in rows] == [0.1, 0.25, 0.5]
    assert next(r for r in rows if r["confidence"] == chosen)["overall"]["f1"] == best_f1


def _result(recalls: list[float]) -> dict:
    edges = [(0, 8), (8, 16), (16, 24), (24, 32), (32, 48), (48, 64), (64, None)]
    return {"bins": [{"lo": lo, "hi": hi, "n_gt": 0 if r is None else 10,
                      "recall": r or 0.0, "precision": 0.5} for (lo, hi), r in zip(edges, recalls)]}


def test_plot_before_after_writes_png(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    curve = [None, None, None, 0.1, 0.2, 0.4, 0.6]
    (results / "altitude_single.json").write_text(json.dumps(_result([None] * 3 + [0.0] * 4)))
    (results / "altitude_tiled.json").write_text(json.dumps(_result(curve)))
    sweep_run = {"model": "yolo11s.pt", "confidence": 0.1, **_result(curve)}
    (results / "altitude_sweep.json").write_text(json.dumps(
        {"runs": [sweep_run], "best": {"model": "yolo11s.pt", "confidence": 0.1}}))
    tuned = {"model": "heridal_yolo11s.pt", "confidence": 0.25,
             "training": {"base_model": "yolo11s.pt"}, **_result([None] * 3 + [0.5, 0.7, 0.8, 0.9])}
    (results / "altitude_finetuned.json").write_text(json.dumps(tuned))

    out = tmp_path / "img" / "altitude_recall_finetuned.png"
    finetune.plot_before_after(results, out)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_zip_members_read_from_local_ranges(tmp_path, monkeypatch):
    archive = tmp_path / "heridal.zip"
    members = {f"root/Annotations/train_{i:03d}.xml": f"<annotation>{'<object/>' * (i % 3)}{'<pad/>' * 50}</annotation>".encode()
               for i in range(12)}
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("root/JPEGImages/big.jpg", bytes(range(256)) * 400, compress_type=zipfile.ZIP_STORED)
        for name, data in members.items():
            z.writestr(name, data)
    blob = archive.read_bytes()
    calls = []

    def fake_range(url, start, end, retries=10):
        calls.append((start, end))
        return blob[start: end + 1]

    monkeypatch.setattr(finetune, "_http_range", fake_range)
    infos = {i.filename: i for i in zipfile.ZipFile(archive).infolist()}
    got = finetune._read_small_members("http://fake", [infos[n] for n in members])
    assert got == members
    assert len(calls) == 1  # Contiguous small members come back in one request.
    assert finetune._read_member("http://fake", infos["root/JPEGImages/big.jpg"]) == bytes(range(256)) * 400
    assert [finetune.count_voc_objects(members[n]) for n in sorted(members)][:3] == [0, 1, 2]


def test_fetch_refuses_the_test_split(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("fetch touched the network")

    monkeypatch.setattr(finetune, "_remote_size", no_network)
    with pytest.raises(ValueError):
        finetune.fetch_split(tmp_path / "test", "test", 5)
    assert not (tmp_path / "test").exists()
