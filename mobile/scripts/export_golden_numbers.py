"""Exports the numbers the Android unit tests check against the golden fixtures.

scripts/mobile_models/make_models.py verify writes the golden fixtures into
build/mobile_models/golden (gitignored). The Android tests read them from there
when they exist. This script copies the part that is only numbers into the
repository, so the decoding, crop and embedding tests run on every checkout:
the letterbox ratio, the detections and the crop box of golden.json, every
detector output row whose best score (objectness times class score) is 0.05
or more, with its row index, and the reference embedding. No image is copied.

Standard library only. Run from the repository root after `verify`:

    python3 mobile/scripts/export_golden_numbers.py [build/mobile_models/golden]
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "mobile/app/src/test/resources/golden/golden-numbers.json"
ROW = 85
ROWS = 8400
MIN_SCORE = 0.05


def floats(path: Path) -> list[float]:
    data = path.read_bytes()
    return list(struct.unpack(f"<{len(data) // 4}f", data))


def f32(v: float) -> str:
    """The shortest decimal that reads back as the same float32."""
    for digits in range(6, 10):
        s = f"{v:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(s)))[0] == v:
            return s
    return repr(v)


def main() -> int:
    golden = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build/mobile_models/golden"
    meta = json.loads((golden / "golden.json").read_text())
    out = floats(golden / "detector_output.f32")
    if len(out) != ROWS * ROW:
        raise SystemExit("detector_output.f32 is not [1, 8400, 85]")
    rows = []
    for i in range(ROWS):
        r = out[i * ROW:(i + 1) * ROW]
        best = max(r[4] * c for c in r[5:])
        if best >= MIN_SCORE:
            rows.append({"index": i, "values": [f32(v) for v in r]})
    embedding = floats(golden / "embedding.f32")
    doc = {
        "what": "Numbers from the golden fixtures of scripts/mobile_models/make_models.py verify "
                "(a drawn frame, no photograph), exported by mobile/scripts/export_golden_numbers.py.",
        "source_sha256": {k: meta["files"][k]["sha256"] for k in ("frame", "detector_output", "embedding")},
        "frame_size": [meta["files"]["frame"]["shape"][1], meta["files"]["frame"]["shape"][0]],
        "letterbox_ratio": meta["letterbox_ratio"],
        "score_threshold": 0.25,
        "detections": meta["detections"],
        "crop_box": {"box": meta["crop_box"]["box"], "pixels": meta["crop_box"]["pixels"]},
        "embedder_model": meta["contract"]["embedder"]["model"],
        "rows_min_score": MIN_SCORE,
        "rows": "ROWS",
        "embedding": "EMBEDDING",
    }
    text = json.dumps(doc, indent=1)
    row_text = ",\n".join('  {"index": %d, "values": [%s]}' % (r["index"], ", ".join(r["values"])) for r in rows)
    text = text.replace('"ROWS"', "[\n" + row_text + "\n ]")
    text = text.replace('"EMBEDDING"', "[" + ", ".join(f32(v) for v in embedding) + "]")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text + "\n")
    print(f"{OUT}: {len(rows)} rows, {len(embedding)} embedding values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
