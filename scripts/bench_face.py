"""Synthetic drone-conditions benchmark for FaceRecognizer.

Uses only insightface's bundled sample image (``t1``, six adults, public).
Each of the six people becomes an identity. Probe crops are degraded copies
(downscaled so the face is 30-80 px wide, blurred, JPEG-compressed, rotated,
flipped, brightness-shifted) of a person-style crop; the enrolment photo is a
clean higher-resolution head-and-shoulders crop. Genuine pairs share an
identity, impostor pairs do not.

Caveats (also in docs/face-recognition-notes.md): only 6 identities, all
adults and mostly frontal, and enrolment/probe come from the same photo, so
absolute scores are optimistic; use it for relative comparison and timing.

Usage: python scripts/bench_face.py [--n 6] [--sizes 30 45 60 80] [--only old|new]
"""
from __future__ import annotations

import argparse
import time

import cv2
import numpy as np

SIZES = (22, 30, 45, 60, 80)


def _identities():
    import insightface
    from insightface.app import FaceAnalysis

    img = insightface.data.get_image("t1")
    app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"],
                       providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(640, 640))
    faces = sorted(app.get(img), key=lambda f: f.bbox[0])
    return img, [f.bbox.astype(int) for f in faces]


def _window(img, b, mx0, my0, mx1, my1):
    x0, y0, x1, y1 = b
    w, h = x1 - x0, y1 - y0
    H, W = img.shape[:2]
    xa, xb = max(0, int(x0 - mx0 * w)), min(W, int(x1 + mx1 * w))
    ya, yb = max(0, int(y0 - my0 * h)), min(H, int(y1 + my1 * h))
    return img[ya:yb, xa:xb].copy(), w


def make_reference(img, b):
    """Clean head-and-shoulders photo."""
    crop, _ = _window(img, b, 1.0, 0.8, 1.0, 1.2)
    return crop


def degrade(img, b, face_w, rng):
    crop, w = _window(img, b, 1.3, 0.6, 1.3, 2.6)  # person-ish: head + torso
    s = face_w / float(w)
    crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    if rng.random() < 0.5:
        crop = crop[:, ::-1].copy()
    ang = rng.uniform(-20, 20)
    h, w2 = crop.shape[:2]
    M = cv2.getRotationMatrix2D((w2 / 2, h / 2), ang, 1.0)
    crop = cv2.warpAffine(crop, M, (w2, h), borderMode=cv2.BORDER_REFLECT)
    sig = rng.uniform(0, 1.4)
    if sig > 0.2:
        crop = cv2.GaussianBlur(crop, (0, 0), sig)
    crop = np.clip(crop.astype(np.float32) * rng.uniform(0.7, 1.25)
                   + rng.uniform(-15, 15), 0, 255).astype(np.uint8)
    _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(40, 85))])
    return cv2.imdecode(buf, 1)


def _auc(pos, neg):
    pos, neg = np.asarray(pos), np.asarray(neg)
    if not len(pos) or not len(neg):
        return float("nan")
    return float((pos[:, None] > neg[None, :]).mean() + 0.5 * (pos[:, None] == neg[None, :]).mean())


def run(make_rec, n=6, sizes=SIZES, seed=0, label=""):
    img, boxes = _identities()
    rng = np.random.default_rng(seed)
    refs = [make_reference(img, b) for b in boxes]
    probes = {s: [[degrade(img, b, s, rng) for _ in range(n)] for b in boxes] for s in sizes}
    rec = make_rec()
    rec.set_target(refs[0])
    rec.compare(probes[sizes[0]][0][0])  # warm up
    per_size = {s: ([], []) for s in sizes}
    times = []
    for ti, ref in enumerate(refs):
        if not rec.set_target(ref):
            print(f"  [warn] enrolment failed for identity {ti}")
            continue
        for s in sizes:
            for pi, plist in enumerate(probes[s]):
                for p in plist:
                    t = time.perf_counter()
                    sc = rec.compare(p)
                    times.append((time.perf_counter() - t) * 1000)
                    per_size[s][0 if pi == ti else 1].append(sc)
    print(f"== {label}")
    allp, alln = [], []
    for s in sizes:
        pos, neg = per_size[s]
        allp += pos
        alln += neg
        thr = float(np.percentile(neg, 99)) if neg else 1
        tpr = float(np.mean(np.asarray(pos) > thr)) if pos else float("nan")
        print(f"  face {s:>2}px  AUC={_auc(pos, neg):.3f}  TPR@FPR1%={tpr:.2f}  "
              f"found={np.mean(np.asarray(pos) > 0):.2f}  mean_pos={np.mean(pos):.2f} mean_neg={np.mean(neg):.2f}"
              f"  recall@.35={np.mean(np.asarray(pos) >= .35):.2f} FP@.35={np.mean(np.asarray(neg) >= .35):.3f}"
              f"  recall@.45={np.mean(np.asarray(pos) >= .45):.2f} FP@.45={np.mean(np.asarray(neg) >= .45):.3f}")
    thr = float(np.percentile(alln, 99))
    print(f"  ALL       AUC={_auc(allp, alln):.3f}  TPR@FPR1%={np.mean(np.asarray(allp) > thr):.2f}  "
          f"ms/crop mean={np.mean(times):.1f} p50={np.median(times):.1f} p95={np.percentile(times, 95):.1f}")
    return {"auc": _auc(allp, alln), "ms": float(np.mean(times))}


def run_tracks(make_rec, frames=8, sizes=(22, 30, 45, 80), thr=0.35, seed=1, label=""):
    """~1 s tracks (``frames`` degraded views per identity) fed through
    find_match with a track_id; compares per-frame vs track-level decisions
    and counts embedding work."""
    img, boxes = _identities()
    rng = np.random.default_rng(seed)
    refs = [make_reference(img, b) for b in boxes]
    rec = make_rec()
    stats = {s: dict(pf_hit=0, tr_hit=0, pf_fp=0, tr_fp=0, n_pos=0, n_neg=0, ms=[]) for s in sizes}
    seqs = {(i, s): [degrade(img, b, s, rng) for _ in range(frames)] for i, b in enumerate(boxes) for s in sizes}
    for ti, ref in enumerate(refs):
        rec.set_target(ref)
        for (pi, s), seq in seqs.items():
            st = stats[s]
            last = 0.0
            rec._tracks.clear()
            for f in seq:
                t = time.perf_counter()
                _, last = rec.find_match([{"crop": f, "track_id": 7}])
                st["ms"].append((time.perf_counter() - t) * 1000)
            # per-frame (stateless) decision: any frame above thr
            pf = max(rec_compare_nomemo(rec, f) for f in seq)
            if pi == ti:
                st["n_pos"] += 1; st["pf_hit"] += pf >= thr; st["tr_hit"] += last >= thr
            else:
                st["n_neg"] += 1; st["pf_fp"] += pf >= thr; st["tr_fp"] += last >= thr
    print(f"== tracks: {label}")
    for s in sizes:
        st = stats[s]
        print(f"  face {s:>2}px  per-frame(any of {frames}) recall={st['pf_hit']/st['n_pos']:.2f} FP={st['pf_fp']/st['n_neg']:.2f}"
              f" | track-level recall={st['tr_hit']/st['n_pos']:.2f} FP={st['tr_fp']/st['n_neg']:.2f}"
              f" | find_match ms/frame={np.mean(st['ms']):.1f}")


def rec_compare_nomemo(rec, crop):
    rec._memo = {}
    return rec.compare(crop)


class LegacyFaceRecognizer:
    """Pre-change behaviour (full buffalo_l pack, 640 det, largest face, no
    quality handling), with CPU-only providers so it runs on Windows."""

    def __init__(self, det_size=(640, 640)):
        from insightface.app import FaceAnalysis

        self.app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=det_size)
        self.t = None

    def _emb(self, im):
        fs = self.app.get(im)
        if not fs:
            return None
        f = max(fs, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        return f.embedding / np.linalg.norm(f.embedding)

    def set_target(self, im):
        self.t = self._emb(im)
        return self.t is not None

    def compare(self, im):
        e = self._emb(im)
        return 0.0 if e is None else max(0.0, float(self.t @ e))


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    ap.add_argument("--which", choices=["old", "new", "both"], default="both")
    ap.add_argument("--tracks", action="store_true", help="track-level (~1 s) benchmark")
    ap.add_argument("kw", nargs="*", help="FaceRecognizer kwargs as key=value")
    a = ap.parse_args()
    from flightrisk.vision.face import FaceRecognizer

    if a.tracks:
        kw = dict(kv.split("=") for kv in a.kw)
        kw = {k: (v if k == "model_name" else (v == "True" if v in ("True", "False") else int(v))) for k, v in kw.items()}
        run_tracks(lambda: FaceRecognizer(**kw), label=str(kw))
        raise SystemExit
    if a.which in ("old", "both"):
        run(LegacyFaceRecognizer, a.n, tuple(a.sizes), label="legacy (pre-change)")
    if a.which in ("new", "both"):
        kw = dict(kv.split("=") for kv in a.kw)
        kw = {k: (v if k == "model_name" else (v == "True" if v in ("True", "False") else float(v)
                                               if "." in v else int(v))) for k, v in kw.items()}
        run(lambda: FaceRecognizer(**kw), a.n, tuple(a.sizes), label=f"FaceRecognizer {kw}")
