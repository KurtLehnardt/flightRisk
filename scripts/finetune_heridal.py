#!/usr/bin/env python3
"""Fine-tune the person detector on HERIDAL aerial imagery and evaluate it tiled.

HERIDAL (Zenodo record 5662351, CC-BY-3.0) is an 8.3 GB zip of 4000x3000 UAV
images with person boxes in Pascal VOC XML. Persons are 24 to about 150 px tall
in those frames, so a stock COCO detector misses most of them. This script
turns a train subset into 640 px tiles that match the tiled inference in
``flightrisk.vision.detector.PersonDetector``, fine-tunes a YOLO model on them,
and scores the result with the same altitude benchmark code as t11.

Subcommands:

    fetch     Fetch a subset of one HERIDAL split from the zip on Zenodo with
              HTTP range requests, without downloading the whole archive.
    build     Build a tiled Ultralytics dataset (VOC boxes to YOLO labels,
              tiles that hold persons kept, empty tiles subsampled).
    train     Fine-tune a YOLO model on the tiled dataset and save the weights
              under the gitignored models/ directory.
    evaluate  Pick the confidence threshold on the held-out validation images,
              then score the weights tiled on the test split once and write
              results/altitude_finetuned.json plus a recall plot.

The test split is never fetched, tiled or trained on by this script; t11's
scripts/run_altitude_benchmark.sh fetches it. Fetching train or val excludes
every name in the official test list.

Typical run from the repository root:

    D=datasets/heridal
    python scripts/finetune_heridal.py fetch --split val --limit 50 --out $D/val
    python scripts/finetune_heridal.py fetch --split train --limit 400 --positives-only --out $D/train
    python scripts/finetune_heridal.py build --train $D/train --val $D/val --out $D/yolo_tiles
    python scripts/finetune_heridal.py train --data $D/yolo_tiles/data.yaml --model models/yolo11s.pt --epochs 20
    python scripts/finetune_heridal.py evaluate --weights models/heridal_yolo11s.pt \\
        --val $D/val --test $D/test
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shutil
import struct
import sys
import time
import urllib.request
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

HERIDAL_URL = "https://zenodo.org/records/5662351/files/heridal_keras_retinanet_voc.zip?download=1"
ZIP_ROOT = "heridal_keras_retinanet_voc/"
SPLITS = ("train", "val")

Box = tuple[float, float, float, float]
TileLabel = tuple[int, float, float, float, float]


# --------------------------------------------------------------------------
# fetch: range requests into the HERIDAL zip
# --------------------------------------------------------------------------


def _http_range(url: str, start: int, end: int, retries: int = 10) -> bytes:
    """Return bytes [start, end] of url, retrying transient failures."""
    request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - network errors vary by platform
            print(f"[fetch] retry {attempt + 1}/{retries} for bytes {start}-{end}: {exc}",
                  file=sys.stderr, flush=True)
            time.sleep(3)
    raise OSError(f"range request failed for bytes {start}-{end}")


def _remote_size(url: str) -> int:
    """Read the archive size from the Content-Range header of a one-byte request."""
    request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        content_range = response.headers.get("Content-Range", "")
    return int(content_range.rsplit("/", 1)[1])


class _RangeReader(io.RawIOBase):
    """Seekable read-only file over an HTTP resource, one range request per read."""

    def __init__(self, url: str, size: int):
        self.url = url
        self.size = size
        self.pos = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        data = _http_range(self.url, self.pos, self.pos + n - 1)
        self.pos += len(data)
        return data


def _extract_local(raw: bytes, offset: int, info: zipfile.ZipInfo) -> bytes | None:
    """Decompress the member whose local header starts at raw[offset].

    Returns None when raw ends before the member's data does.
    """
    name_len, extra_len = struct.unpack("<HH", raw[offset + 26: offset + 30])
    begin = offset + 30 + name_len + extra_len
    if begin + info.compress_size > len(raw):
        return None
    body = raw[begin: begin + info.compress_size]
    data = body if info.compress_type == zipfile.ZIP_STORED else zlib.decompress(body, -15)
    if len(data) != info.file_size:
        raise OSError(f"{info.filename}: got {len(data)} bytes, expected {info.file_size}")
    return data


def _read_member(url: str, info: zipfile.ZipInfo) -> bytes:
    """Fetch and decompress one zip member with a single range request."""
    start = info.header_offset
    raw = _http_range(url, start, start + 30 + 1024 + info.compress_size)
    data = _extract_local(raw, 0, info)
    if data is None:
        name_len, extra_len = struct.unpack("<HH", raw[26:30])
        raw = _http_range(url, start, start + 30 + name_len + extra_len + info.compress_size)
        data = _extract_local(raw, 0, info)
    return data


def _read_small_members(url: str, infos: Sequence[zipfile.ZipInfo],
                        max_span: int = 32 << 20) -> dict[str, bytes]:
    """Fetch many small members with a few large range requests.

    Zenodo answers bursts of small range requests with HTTP 429 and 504, so
    members that sit close together in the archive (the VOC XML files do) are
    read as contiguous spans of up to max_span bytes and sliced locally.
    """
    ordered = sorted(infos, key=lambda i: i.header_offset)
    spans: list[list[zipfile.ZipInfo]] = []
    for i in ordered:
        if spans and i.header_offset + i.compress_size + 2048 - spans[-1][0].header_offset <= max_span:
            spans[-1].append(i)
        else:
            spans.append([i])
    out: dict[str, bytes] = {}
    for span in spans:
        start = span[0].header_offset
        end = span[-1].header_offset + 30 + 1024 + span[-1].compress_size
        raw = _http_range(url, start, end)
        for i in span:
            data = _extract_local(raw, i.header_offset - start, i)
            out[i.filename] = data if data is not None else _read_member(url, i)
    return out


def select_subset(names: Iterable[str], n: int, seed: int,
                  exclude: Iterable[str] = ()) -> list[str]:
    """Pick n names at random with a fixed seed, never one in exclude.

    The pool is sorted before shuffling, so the result does not depend on the
    input order, and a larger n extends a smaller n's selection.
    """
    banned = set(exclude)
    pool = sorted(set(names) - banned)
    random.Random(seed).shuffle(pool)
    return pool[: max(0, n)]


def count_voc_objects(xml_bytes: bytes) -> int:
    """Number of annotated objects in one VOC XML document."""
    import xml.etree.ElementTree as ET

    return len(ET.fromstring(xml_bytes).findall("object"))


def fetch_split(out_dir: Path, split: str, limit: int, seed: int = 0,
                workers: int = 8, url: str = HERIDAL_URL,
                positives_only: bool = False) -> list[str]:
    """Fetch up to limit images and VOC XML of one split into out_dir.

    With positives_only, every candidate's XML is read first (a few KB each)
    and the subset is drawn only from images with at least one person. About
    40% of HERIDAL train images have no person, and the person images already
    yield far more empty tiles than training needs, so this spends the slow
    download on images that carry labels.

    Files already on disk are skipped, so an interrupted fetch can be resumed.
    """
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}, got {split!r}")
    size = _remote_size(url)
    archive = zipfile.ZipFile(io.BufferedReader(_RangeReader(url, size), buffer_size=1 << 20))
    info = {i.filename: i for i in archive.infolist()}

    def split_names(name: str) -> list[str]:
        return archive.read(f"{ZIP_ROOT}ImageSets/Main/{name}.txt").decode().split()

    exclude = set(split_names("test"))
    if split == "train":
        # Keep the official validation images out of the training subset.
        exclude |= set(split_names("val"))
    names = [
        n for n in split_names(split)
        if f"{ZIP_ROOT}JPEGImages/{n}.jpg" in info and f"{ZIP_ROOT}Annotations/{n}.xml" in info
    ]
    if positives_only:
        candidates = sorted(set(names) - exclude)
        xml = _read_small_members(url, [info[f"{ZIP_ROOT}Annotations/{n}.xml"] for n in candidates])
        names = [n for n in candidates if count_voc_objects(xml[f"{ZIP_ROOT}Annotations/{n}.xml"]) > 0]
        print(f"[fetch] {len(names)} of {len(candidates)} {split} images have a person", flush=True)
    chosen = select_subset(names, limit, seed, exclude)

    for sub in ("JPEGImages", "Annotations"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    (out_dir / "subset.txt").write_text("\n".join(chosen) + "\n")

    def save(dst: Path, data: bytes) -> None:
        tmp = dst.with_suffix(dst.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dst)

    xml = _read_small_members(url, [info[f"{ZIP_ROOT}Annotations/{n}.xml"] for n in chosen])
    for n in chosen:
        save(out_dir / "Annotations" / f"{n}.xml", xml[f"{ZIP_ROOT}Annotations/{n}.xml"])

    def job(name: str) -> None:
        dst = out_dir / "JPEGImages" / f"{name}.jpg"
        if dst.exists() and dst.stat().st_size > 0:
            return
        save(dst, _read_member(url, info[f"{ZIP_ROOT}JPEGImages/{name}.jpg"]))
        print(f"[fetch] ok {name}.jpg", flush=True)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(job, chosen))
    print(f"[fetch] DONE {len(chosen)} {split} images in {out_dir}", flush=True)
    return chosen


# --------------------------------------------------------------------------
# build: VOC images to a tiled Ultralytics dataset
# --------------------------------------------------------------------------


def voc_boxes_to_tile_labels(boxes: Sequence[Box], tile: tuple[int, int, int, int],
                             min_visible: float = 0.5) -> list[TileLabel]:
    """Convert frame-coordinate VOC boxes to YOLO labels for one tile.

    Each box is clipped to the tile. A box is kept when at least min_visible of
    its area lies inside the tile. Labels are (class, cx, cy, w, h), class 0
    for person, with coordinates normalized to the tile size.
    """
    tx1, ty1, tx2, ty2 = tile
    tw, th = tx2 - tx1, ty2 - ty1
    labels: list[TileLabel] = []
    for x1, y1, x2, y2 in boxes:
        area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        if area <= 0:
            continue
        cx1, cy1, cx2, cy2 = max(x1, tx1), max(y1, ty1), min(x2, tx2), min(y2, ty2)
        if cx2 <= cx1 or cy2 <= cy1:
            continue
        if (cx2 - cx1) * (cy2 - cy1) < min_visible * area:
            continue
        labels.append((
            0,
            ((cx1 + cx2) / 2 - tx1) / tw,
            ((cy1 + cy2) / 2 - ty1) / th,
            (cx2 - cx1) / tw,
            (cy2 - cy1) / th,
        ))
    return labels


def _touches_any(boxes: Sequence[Box], tile: tuple[int, int, int, int]) -> bool:
    """True when any box overlaps the tile at all."""
    tx1, ty1, tx2, ty2 = tile
    return any(min(x2, tx2) > max(x1, tx1) and min(y2, ty2) > max(y1, ty1)
               for x1, y1, x2, y2 in boxes)


def compute_tiles(height: int, width: int, tile_size: int, overlap: float) -> list[tuple[int, int, int, int]]:
    """Tile windows identical to the ones PersonDetector uses at inference."""
    from flightrisk.vision.detector import PersonDetector

    return PersonDetector.compute_tiles(height, width, tile_size, overlap)


def _build_split(samples, out_dir: Path, split: str, tile_size: int, overlap: float,
                 empty_ratio: float, min_visible: float, rng: random.Random) -> dict[str, int]:
    """Write the tiles and labels of one split; return its counts."""
    import cv2

    img_dir = out_dir / "images" / split
    lbl_dir = out_dir / "labels" / split
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)

    positives: list[tuple[int, tuple[int, int, int, int], list[TileLabel]]] = []
    empties: list[tuple[int, tuple[int, int, int, int], list[TileLabel]]] = []
    for idx, s in enumerate(samples):
        width, height = s.width, s.height
        if width <= 0 or height <= 0:
            frame = cv2.imread(str(s.image_path))
            height, width = frame.shape[:2]
        for tile in compute_tiles(height, width, tile_size, overlap):
            labels = voc_boxes_to_tile_labels(s.boxes, tile, min_visible)
            if labels:
                positives.append((idx, tile, labels))
            elif not _touches_any(s.boxes, tile):
                # Tiles that cut through a person without labelling it are
                # ambiguous, so only tiles clear of every box count as empty.
                empties.append((idx, tile, []))
    n_empty = min(len(empties), int(round(empty_ratio * len(positives))))
    chosen = positives + rng.sample(empties, n_empty)

    by_image: dict[int, list] = {}
    for idx, tile, labels in chosen:
        by_image.setdefault(idx, []).append((tile, labels))
    for idx in sorted(by_image):
        s = samples[idx]
        frame = cv2.imread(str(s.image_path))
        if frame is None:
            raise ValueError(f"Could not read image {s.image_path}")
        for (x1, y1, x2, y2), labels in by_image[idx]:
            stem = f"{s.image_path.stem}_{x1}_{y1}"
            cv2.imwrite(str(img_dir / f"{stem}.jpg"), frame[y1:y2, x1:x2],
                        [cv2.IMWRITE_JPEG_QUALITY, 95])
            (lbl_dir / f"{stem}.txt").write_text(
                "".join(f"{c} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n" for c, cx, cy, w, h in labels)
            )
    return {
        "images": len(samples),
        "boxes": sum(len(s.boxes) for s in samples),
        "positive_tiles": len(positives),
        "empty_tiles_available": len(empties),
        "empty_tiles_kept": n_empty,
        "labels": sum(len(lbls) for _, _, lbls in positives),
    }


def build_tiled_dataset(train_dir: str | Path, val_dir: str | Path, out_dir: str | Path,
                        tile_size: int = 640, overlap: float = 0.2, empty_ratio: float = 1.0,
                        min_visible: float = 0.5, seed: int = 0) -> Path:
    """Build an Ultralytics dataset of tiles from two VOC directories.

    Every tile with a person label is kept. Tiles clear of every person box
    are subsampled to empty_ratio times the number of person tiles. Returns
    the path of the generated data.yaml.
    """
    from flightrisk.evaluation.detection import load_voc_dir

    out = Path(out_dir).resolve()
    if out.exists():
        shutil.rmtree(out)
    rng = random.Random(seed)
    stats: dict[str, object] = {
        "tile_size": tile_size, "overlap": overlap, "empty_ratio": empty_ratio,
        "min_visible": min_visible, "seed": seed,
    }
    for split, src in (("train", train_dir), ("val", val_dir)):
        samples = load_voc_dir(src)
        if not samples:
            raise SystemExit(f"no VOC annotations with images found in {src}")
        stats[split] = _build_split(samples, out, split, tile_size, overlap,
                                    empty_ratio, min_visible, rng)
    data_yaml = out / "data.yaml"
    data_yaml.write_text(
        "# Tiled HERIDAL person dataset generated by scripts/finetune_heridal.py.\n"
        f"path: {out}\n"
        "train: images/train\n"
        "val: images/val\n"
        "nc: 1\n"
        "names:\n"
        "  0: person\n"
    )
    (out / "stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(f"[build] wrote {data_yaml}: {json.dumps(stats)}", flush=True)
    return data_yaml


# --------------------------------------------------------------------------
# train: fine-tune on MPS, weights under models/
# --------------------------------------------------------------------------


def train(data_yaml: str | Path, base_model: str, epochs: int, imgsz: int = 640,
          batch: int = 16, device: str = "mps", models_dir: str | Path = "models",
          name: str | None = None, workers: int = 4, seed: int = 0) -> Path:
    """Fine-tune base_model on the tiled dataset; return the saved weights path.

    The best checkpoint by Ultralytics validation fitness (on the tiled val
    split from the train subset, never the test split) is copied to
    models/<name>.pt, next to a JSON sidecar describing the run.
    """
    from ultralytics import YOLO

    models = Path(models_dir).resolve()
    models.mkdir(parents=True, exist_ok=True)
    name = name or f"heridal_{Path(base_model).stem}"
    started = time.time()
    model = YOLO(base_model)
    model.train(data=str(data_yaml), epochs=epochs, imgsz=imgsz, batch=batch, device=device,
                project=str(models / "runs"), name=name, exist_ok=True, workers=workers,
                seed=seed, deterministic=False, plots=False, verbose=False)
    seconds = time.time() - started
    run_dir = models / "runs" / name
    best = run_dir / "weights" / "best.pt"
    weights = models / f"{name}.pt"
    shutil.copyfile(best, weights)

    stats_path = Path(data_yaml).resolve().parent / "stats.json"
    metrics = {}
    results_csv = run_dir / "results.csv"
    if results_csv.is_file():
        import csv

        rows = list(csv.DictReader(results_csv.open()))
        if rows:
            metrics = {k.strip(): float(v) for k, v in rows[-1].items() if k.strip() and v.strip()}
    sidecar = {
        "weights": weights.name,
        "base_model": Path(base_model).name,
        "epochs": epochs,
        "imgsz": imgsz,
        "batch": batch,
        "device": device,
        "seed": seed,
        "train_seconds": round(seconds),
        "dataset": json.loads(stats_path.read_text()) if stats_path.is_file() else None,
        "last_epoch_val_metrics": metrics,
    }
    weights.with_suffix(".json").write_text(json.dumps(sidecar, indent=2) + "\n")
    print(f"[train] saved {weights} after {seconds / 60:.1f} min", flush=True)
    return weights


# --------------------------------------------------------------------------
# evaluate: pick confidence on val, score test once, write JSON and plot
# --------------------------------------------------------------------------


def select_confidence(val_results: dict[float, object]) -> tuple[float, list[dict]]:
    """Pick the confidence with the highest overall F1 on the validation images.

    Ties go to the higher confidence. Returns the choice and one summary row
    per candidate.
    """
    from sweep_altitude import RECALL_FLOOR, overall
    from flightrisk.evaluation.detection import min_person_px

    rows = []
    for conf, result in sorted(val_results.items()):
        rows.append({"confidence": conf, "overall": overall(result),
                     "min_person_px": min_person_px(result, RECALL_FLOOR)})
    best = max(rows, key=lambda r: (r["overall"]["f1"], r["confidence"]))
    return best["confidence"], rows


def evaluate_finetuned(weights: str | Path, val_dir: str | Path, test_dir: str | Path,
                       confs: Sequence[float], output: str | Path,
                       tile_size: int = 640, overlap: float = 0.2,
                       detector_factory=None) -> dict:
    """Choose the confidence on val, then score the test split once with it."""
    from sweep_altitude import default_detector_factory, evaluate_confidences, run_record
    from flightrisk.evaluation.detection import evaluate, load_voc_dir

    factory = detector_factory or default_detector_factory(tile_size, overlap)
    weights = Path(weights)
    val_samples = load_voc_dir(val_dir)
    test_samples = load_voc_dir(test_dir)
    if not val_samples or not test_samples:
        raise SystemExit("validation or test split has no VOC annotations with images")

    val_results, _ = evaluate_confidences(factory(str(weights), min(confs)), val_samples, confs)
    chosen, candidates = select_confidence(val_results)
    print(f"[evaluate] confidence {chosen:g} chosen on {len(val_samples)} validation images", flush=True)

    started = time.perf_counter()
    result = evaluate(factory(str(weights), chosen), test_samples)
    record = run_record(weights.name, chosen, result, time.perf_counter() - started)
    sidecar = weights.with_suffix(".json")
    payload = {
        "dataset": "HERIDAL",
        "split": "test",
        "n_gt": sum(len(s.boxes) for s in test_samples),
        "tile_size": tile_size,
        "overlap": overlap,
        "iou_threshold": 0.3,
        **record,
        "confidence_selection": {
            "split": "val",
            "n_images": len(val_samples),
            "criterion": "highest overall F1 on validation images held out from training",
            "candidates": candidates,
        },
        "training": json.loads(sidecar.read_text()) if sidecar.is_file() else None,
    }
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    o = record["overall"]
    print(f"[evaluate] test recall={o['recall']:.3f} precision={o['precision']:.3f} "
          f"min_person_px={record['min_person_px']}; wrote {out}", flush=True)
    return payload


def bin_label(b: dict) -> str:
    """Bin label in the benchmark's notation, for example [32,48) or [64,inf)."""
    hi = "inf" if b["hi"] is None else format(b["hi"], "g")
    return f"[{b['lo']:g},{hi})"


# Categorical slots in fixed order, validated for color vision deficiency on a
# light surface. Each series also gets its own marker shape and a direct label.
SERIES_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
SERIES_MARKERS = ("o", "s", "^", "D")


def plot_recall(series: Sequence[tuple[str, dict]], output: str | Path) -> None:
    """Draw recall by person pixel height for each (label, result JSON) pair.

    Bins with no ground truth are left out. Series take the categorical slots
    in the order given, so pass the headline series first.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    first = series[0][1]["bins"]
    keep = [i for i, b in enumerate(first) if b["n_gt"] > 0]
    ticks = [f"{bin_label(first[i])}\nn={first[i]['n_gt']}" for i in keep]
    xs = list(range(len(keep)))
    fig, ax = plt.subplots(figsize=(8, 4.4), dpi=150)
    ax.axhline(0.5, color="#8a8a85", linestyle="--", linewidth=1, label="Recall 0.5 floor")
    for (label, data), color, marker in zip(series, SERIES_COLORS, SERIES_MARKERS):
        ys = [data["bins"][i]["recall"] for i in keep]
        ax.plot(xs, ys, marker=marker, markersize=6, linewidth=2, label=label, color=color)
        ax.annotate(f"{ys[-1]:.2f}", (xs[-1], ys[-1]), xytext=(8, 0), textcoords="offset points",
                    va="center", fontsize=8, color="#3d3d3a")
    ax.set_xticks(xs)
    ax.set_xticklabels(ticks)
    ax.set_xlim(-0.3, len(xs) - 0.5)
    ax.set_ylim(0, 1.05)
    ax.set_xlabel("Person height in the frame (pixels); n = ground truth persons in the bin")
    ax.set_ylabel("Recall (IoU 0.3)")
    ax.set_title("Person detection recall by pixel height, HERIDAL test split (101 images)")
    ax.grid(alpha=0.25)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(fontsize=8, loc="upper left", frameon=False)
    fig.tight_layout()
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output)
    plt.close(fig)


def plot_before_after(results_dir: str | Path, output: str | Path) -> None:
    """Plot fine-tuned, best stock sweep, stock tiled and stock single recall."""
    results = Path(results_dir)
    single = json.loads((results / "altitude_single.json").read_text())
    tiled = json.loads((results / "altitude_tiled.json").read_text())
    sweep = json.loads((results / "altitude_sweep.json").read_text())
    tuned = json.loads((results / "altitude_finetuned.json").read_text())
    best = next(r for r in sweep["runs"]
                if r["model"] == sweep["best"]["model"] and r["confidence"] == sweep["best"]["confidence"])
    base = tuned["training"]["base_model"] if tuned.get("training") else tuned["model"]
    plot_recall([
        (f"Fine-tuned {Path(base).stem} on HERIDAL train tiles, tiled, conf {tuned['confidence']:g}", tuned),
        (f"Best stock sweep: {Path(best['model']).stem}, tiled, conf {best['confidence']:g}", best),
        ("Stock yolo11n, tiled, conf 0.4 (t11)", tiled),
        ("Stock yolo11n, single pass, conf 0.4 (t11)", single),
    ], output)
    print(f"[plot] wrote {output}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="fetch a subset of one split with range requests")
    p.add_argument("--split", choices=SPLITS, required=True)
    p.add_argument("--limit", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--positives-only", action="store_true",
                   help="draw the subset only from images with at least one person")

    p = sub.add_parser("build", help="build the tiled Ultralytics dataset")
    p.add_argument("--train", type=Path, required=True, help="VOC directory of training images")
    p.add_argument("--val", type=Path, required=True, help="VOC directory of validation images")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--tile-size", type=int, default=640)
    p.add_argument("--overlap", type=float, default=0.2)
    p.add_argument("--empty-ratio", type=float, default=1.0,
                   help="empty tiles kept per person tile (default 1.0)")
    p.add_argument("--min-visible", type=float, default=0.5)
    p.add_argument("--seed", type=int, default=0)

    p = sub.add_parser("train", help="fine-tune a YOLO model on the tiled dataset")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--model", default="yolo11s.pt")
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--batch", type=int, default=16)
    p.add_argument("--device", default="mps")
    p.add_argument("--models-dir", type=Path, default=ROOT / "models")
    p.add_argument("--name", default=None)
    p.add_argument("--workers", type=int, default=4)

    p = sub.add_parser("evaluate", help="pick confidence on val, score test, plot")
    p.add_argument("--weights", type=Path, required=True)
    p.add_argument("--val", type=Path, required=True)
    p.add_argument("--test", type=Path, required=True)
    p.add_argument("--conf", nargs="+", type=float, default=[0.1, 0.25, 0.4, 0.5])
    p.add_argument("--output", type=Path, default=ROOT / "results" / "altitude_finetuned.json")
    p.add_argument("--plot", type=Path, default=ROOT / "docs" / "img" / "altitude_recall_finetuned.png")
    p.add_argument("--tile-size", type=int, default=640)
    p.add_argument("--overlap", type=float, default=0.2)

    args = parser.parse_args(argv)
    if args.command == "fetch":
        fetch_split(args.out, args.split, args.limit, args.seed, args.workers,
                    positives_only=args.positives_only)
    elif args.command == "build":
        build_tiled_dataset(args.train, args.val, args.out, args.tile_size, args.overlap,
                            args.empty_ratio, args.min_visible, args.seed)
    elif args.command == "train":
        train(args.data, args.model, args.epochs, args.imgsz, args.batch, args.device,
              args.models_dir, args.name, args.workers)
    elif args.command == "evaluate":
        evaluate_finetuned(args.weights, args.val, args.test, args.conf, args.output,
                           args.tile_size, args.overlap)
        plot_before_after(Path(args.output).parent, args.plot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
