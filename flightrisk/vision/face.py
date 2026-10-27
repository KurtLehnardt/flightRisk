"""Face recognition using InsightFace (SCRFD detector + ArcFace embeddings).

Extracts 512-d face embeddings from detected person crops and compares
them against a reference photo. Complements full-body ReID for higher
confidence matching, especially when clothing changes.

Design notes (see docs/face-recognition-notes.md for measurements):

* Only the detection and recognition models of the InsightFace pack are
  loaded. The default ``FaceAnalysis`` also runs 3D/2D landmark and
  gender/age networks whose output is never used here.
* Probe crops are searched at a size fitted to the crop (not a fixed 640x640
  canvas) and, for tall person crops, only in the upper part where a head can
  be.
* Embeddings are aggregated per tracker id (quality-weighted, best few) so a
  face that is only visible for about a second yields one stable score, and
  re-embedding is skipped once a track already has good observations.
* The reference photo goes through quality checks, picks the best (not just
  largest) face, and is enrolled as a small template (original + mirrored +
  low-resolution variants) because probes from a drone are low resolution.

Requires: pip install insightface onnxruntime
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import cv2
import numpy as np

try:
    from insightface.app import FaceAnalysis
    from insightface.utils import face_align

    HAS_INSIGHTFACE = True
except ImportError:  # pragma: no cover - exercised only without insightface
    HAS_INSIGHTFACE = False

logger = logging.getLogger(__name__)

# Reference-photo acceptance limits (pixels / unitless).
MIN_REF_FACE_PX = 32      # hard reject below this: embedding would be unreliable
WARN_REF_FACE_PX = 80     # warn below this
MIN_REF_DET_SCORE = 0.5
WARN_REF_YAW = 0.45       # |nose offset| / inter-eye distance (profile-ish)
WARN_REF_BLUR = 60.0      # Laplacian variance of the aligned 112px face

# Probe handling.
MIN_PROBE_SIDE = 24       # crops smaller than this cannot hold a usable face
TOP_REGION_ASPECT = 1.5   # crops taller than this are searched in their top part
TOP_REGION_FRACTION = 0.6

# Per-track cache safety: a track id unseen for this many find_match calls is
# treated as a new person (trackers can recycle ids), and a new observation that
# is nearly orthogonal to the track's aggregate means the id switched people.
TRACK_STALE_CALLS = 60
TRACK_SWITCH_SIM = 0.1


def _providers() -> list[str]:
    """Execution providers that actually exist in this onnxruntime build."""
    try:
        import onnxruntime as ort

        available = set(ort.get_available_providers())
    except Exception:  # pragma: no cover
        return ["CPUExecutionProvider"]
    wanted = ["CoreMLExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
    chosen = [p for p in wanted if p in available]
    return chosen or ["CPUExecutionProvider"]


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def yaw_ratio(kps: np.ndarray) -> float:
    """Signed nose offset from the eye midpoint, in inter-eye distances.

    0 is frontal; roughly +-0.5 and beyond is a strong turn / profile.
    """
    le, re, nose = kps[0], kps[1], kps[2]
    eye_dist = float(np.linalg.norm(re - le))
    if eye_dist < 1e-6:
        return 0.0
    return float((nose[0] - (le[0] + re[0]) / 2.0) / eye_dist)


def face_quality(det_score: float, face_w: float, kps: np.ndarray) -> float:
    """Cheap 0-1 quality estimate from detector confidence, size and pose."""
    size_term = 0.4 + 0.6 * min(1.0, face_w / 64.0)
    pose_term = max(0.3, 1.0 - min(abs(yaw_ratio(kps)), 1.0) * 0.9)
    return float(max(0.0, min(1.0, det_score)) * size_term * pose_term)


@dataclass
class FaceObservation:
    bbox: np.ndarray
    kps: np.ndarray
    det_score: float
    embedding: np.ndarray  # L2-normalised
    quality: float
    width: float


@dataclass
class _TrackFace:
    obs: list = field(default_factory=list)  # [(quality, emb)], best first
    last_call: int = 0
    last_embed_call: int = -10**9
    misses: int = 0


@dataclass
class TargetReport:
    """Outcome of enrolling a reference photo (see ``FaceRecognizer.set_target``)."""

    ok: bool = False
    reason: str = ""            # machine-readable: no_face / face_too_small / ...
    message: str = ""           # operator-facing text
    n_faces: int = 0
    face_px: int = 0
    det_score: float = 0.0
    yaw: float = 0.0
    blur: float = 0.0
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "reason": self.reason, "message": self.message,
            "n_faces": self.n_faces, "face_px": self.face_px,
            "det_score": round(self.det_score, 3), "yaw": round(self.yaw, 2),
            "blur": round(self.blur, 1), "warnings": list(self.warnings),
        }


class FaceRecognizer:
    """Face detection + ArcFace embedding extraction and matching."""

    def __init__(
        self,
        match_threshold: float | None = None,
        det_size: tuple[int, int] | None = None,
        model_name: str | None = None,
        probe_max_side: int = 320,
        probe_min_side: int = 192,
        track_reuse: bool = True,
        template_lowres: bool = True,
        probe_det_thresh: float = 0.5,
    ):
        """Initialize InsightFace.

        Args:
            match_threshold: Cosine similarity threshold for face match (0-1).
                Defaults to `config.vision.face_match_threshold`.
            det_size: Detection input size used for *reference photos*
                (probe crops use a size fitted to the crop). Defaults to
                `config.vision.face_det_size`.
            model_name: InsightFace pack (``buffalo_l`` ResNet50 or the much
                faster ``buffalo_s`` MobileFaceNet). Defaults to
                `config.vision.face_model`.
            probe_max_side / probe_min_side: Probe crops are resized so their
                searched region's longest side lies in this range.
            track_reuse: Aggregate embeddings per track id and skip
                re-embedding once a track has good observations.
            template_lowres: Add low-resolution variants of the reference face
                to the enrolled template.
            probe_det_thresh: Detector confidence for probe crops (0.4 vs 0.5
                made no difference on the synthetic benchmark).
        """
        if not HAS_INSIGHTFACE:
            raise RuntimeError("pip install insightface onnxruntime")

        from flightrisk.config import get_config

        vc = get_config().vision
        if match_threshold is None:
            match_threshold = vc.face_match_threshold
        if det_size is None:
            det_size = vc.face_det_size
        if model_name is None:
            model_name = getattr(vc, "face_model", "buffalo_l")

        self.match_threshold = match_threshold
        self.model_name = model_name
        self.probe_max_side = probe_max_side
        self.probe_min_side = probe_min_side
        self.track_reuse = track_reuse
        self.template_lowres = template_lowres
        self.probe_det_thresh = probe_det_thresh
        self.track_refresh_every = 4   # calls between re-embeds of a settled track
        self.track_settled_obs = 3     # observations before a track is "settled"
        self.track_top_k = 5
        self.track_max = 256

        self._target_embedding: np.ndarray | None = None  # unit mean of template
        self._target_templates: np.ndarray | None = None  # (n, 512) unit rows
        self.target_report = TargetReport()
        self._tracks: dict[int, _TrackFace] = {}
        self._call = 0
        self._memo: dict[int, tuple[np.ndarray, float]] = {}
        # Pipeline thread (find_match) and socket threads (set_target, ...)
        # share this object and the single SCRFD detector.
        self._lock = threading.RLock()

        # Detection + recognition only: landmark_3d_68 / landmark_2d_106 /
        # genderage are never used and cost ~2x the whole pipeline.
        self.app = FaceAnalysis(
            name=model_name,
            allowed_modules=["detection", "recognition"],
            providers=_providers(),
        )
        self.app.prepare(ctx_id=0, det_size=det_size)
        self._det = self.app.models["detection"]
        self._rec = self.app.models["recognition"]
        self._ref_det_size = tuple(det_size)
        logger.info("face_ready model=%s ref_det_size=%s", model_name, det_size)

    # ------------------------------------------------------------------
    # detection / embedding primitives
    # ------------------------------------------------------------------

    def _detect(self, img: np.ndarray, side: int, min_side: int = 0,
                thresh: float | None = None):
        """Run SCRFD on ``img`` resized so its longest side is in [min_side, side].

        Returns ``(det[n,5], kps[n,5,2])`` in the coordinates of ``img``.
        """
        h, w = img.shape[:2]
        longest = max(h, w)
        target = min(max(longest, min_side), side)
        scale = target / float(longest)
        if abs(scale - 1.0) > 1e-3:
            interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
            work = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))),
                              interpolation=interp)
        else:
            work = img
        wh, ww = work.shape[:2]
        inp = (int(-(-ww // 32) * 32), int(-(-wh // 32) * 32))
        kwargs = {"input_size": inp}
        if thresh is not None:
            kwargs["det_thresh"] = thresh
        try:
            det, kps = self._det.detect(work, **kwargs)
        except TypeError:
            # Older insightface: no per-call det_thresh.
            if thresh is not None:
                old = getattr(self._det, "det_thresh", None)
                self._det.det_thresh = thresh
                try:
                    det, kps = self._det.detect(work, input_size=inp)
                finally:
                    if old is not None:
                        self._det.det_thresh = old
            else:
                det, kps = self._det.detect(work, input_size=inp)
        if det is None or len(det) == 0:
            return np.empty((0, 5), np.float32), np.empty((0, 5, 2), np.float32)
        if kps is None:
            return np.empty((0, 5), np.float32), np.empty((0, 5, 2), np.float32)
        if scale != 1.0:
            det = det.copy()
            det[:, :4] /= scale
            kps = kps / scale
        return det, kps

    def _embed(self, img: np.ndarray, kps_list, flip: bool = False) -> np.ndarray:
        """Embed aligned faces; returns unit rows, shape (n * (2 if flip), 512)."""
        aligned = [face_align.norm_crop(img, k, 112) for k in kps_list]
        if flip:
            aligned = aligned + [a[:, ::-1] for a in aligned]
        feats = np.asarray(self._rec.get_feat(aligned), dtype=np.float32).reshape(len(aligned), -1)
        return feats / np.maximum(np.linalg.norm(feats, axis=1, keepdims=True), 1e-9)

    def _observe(self, crop: np.ndarray) -> FaceObservation | None:
        """Find the best face in a person crop and embed it."""
        if crop is None or crop.size == 0:
            return None
        h, w = crop.shape[:2]
        if max(h, w) < MIN_PROBE_SIDE:
            return None
        region = crop
        if h > TOP_REGION_ASPECT * w:
            region = crop[: max(1, int(h * TOP_REGION_FRACTION))]
        det, kps = self._detect(region, self.probe_max_side, self.probe_min_side,
                                thresh=self.probe_det_thresh)
        if len(det) == 0:
            return None
        widths = det[:, 2] - det[:, 0]
        quals = [face_quality(float(det[i, 4]), float(widths[i]), kps[i]) for i in range(len(det))]
        # A person crop's subject is roughly centred; neighbours' faces that
        # spill into the box sit toward the edges, so favour central faces.
        cx = (det[:, 0] + det[:, 2]) / 2.0
        centrality = np.maximum(0.2, 1.0 - 1.2 * np.abs(cx - region.shape[1] / 2.0) / region.shape[1])
        i = int(np.argmax([q * wd * c for q, wd, c in zip(quals, widths, centrality)]))
        emb = self._embed(region, [kps[i]])[0]
        return FaceObservation(det[i, :4].copy(), kps[i].copy(), float(det[i, 4]), emb,
                               quals[i], float(widths[i]))

    # ------------------------------------------------------------------
    # public embedding access
    # ------------------------------------------------------------------

    def _best_face_embedding(self, image: np.ndarray) -> np.ndarray | None:
        """Embedding of the best face in an image (None if no face)."""
        obs = self._observe(image)
        return None if obs is None else obs.embedding

    def extract_embedding(self, crop: np.ndarray) -> np.ndarray | None:
        """Public accessor for the raw face embedding of a person crop.

        Args:
            crop: BGR numpy array of a detected person.

        Returns:
            Normalized 512-d face embedding, or None if no face is found
            or extraction fails.
        """
        try:
            with self._lock:
                return self._best_face_embedding(crop)
        except Exception:
            logger.warning("face_embedding_failed", exc_info=True)
            return None

    @property
    def target_embedding(self) -> np.ndarray | None:
        """Unit-norm template embedding of the enrolled reference (or None)."""
        return self._target_embedding

    # ------------------------------------------------------------------
    # target enrolment
    # ------------------------------------------------------------------

    def _detect_reference(self, image: np.ndarray):
        h, w = image.shape[:2]
        for side in (640, 1024):
            det, kps = self._detect(image, side, min_side=side if max(h, w) < side else 0,
                                    thresh=0.5)
            if len(det):
                return det, kps
        return det, kps

    def set_target(self, image: np.ndarray) -> bool:
        """Enrol the reference face from a photo of the target child.

        Picks the best face (size x detector confidence x frontality), checks
        quality, and builds a template from the aligned face, its mirror image
        and low-resolution variants. Details are in ``self.target_report``.

        Returns:
            True if a usable face was found and enrolled, False otherwise
            (the face target is cleared on failure).
        """
        with self._lock:
            try:
                ok = self._enrol(image)
            except Exception:
                logger.warning("face_target_failed", exc_info=True)
                rep = self.target_report
                rep.ok, rep.reason = False, "error"
                rep.message = "The reference photo could not be processed."
                ok = False
            if not ok:
                # Other recognisers (ReID, canon) already switched to the new
                # photo; keeping the previous child's face would silently match
                # the wrong person, so a failed enrolment clears the face target.
                self._reset_target()
            return ok

    def _enrol(self, image: np.ndarray) -> bool:
        rep = TargetReport()
        self.target_report = rep
        if image is None or getattr(image, "size", 0) == 0:
            rep.reason, rep.message = "bad_image", "The reference image could not be read."
            return False
        det, kps = self._detect_reference(image)
        rep.n_faces = int(len(det))
        if rep.n_faces == 0:
            rep.reason = "no_face"
            rep.message = ("No face found in the reference photo. Use a clear, well-lit photo "
                           "where the child's face is visible and not covered.")
            logger.warning("face_target_rejected reason=no_face")
            return False

        widths = det[:, 2] - det[:, 0]
        score = [float(widths[i]) * float(det[i, 4]) * (1.0 - min(abs(yaw_ratio(kps[i])), 1.0) * 0.5)
                 for i in range(len(det))]
        i = int(np.argmax(score))
        k = kps[i]
        rep.face_px = int(widths[i])
        rep.det_score = float(det[i, 4])
        rep.yaw = yaw_ratio(k)
        aligned = face_align.norm_crop(image, k, 112)
        rep.blur = float(cv2.Laplacian(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())

        if rep.face_px < MIN_REF_FACE_PX:
            rep.reason = "face_too_small"
            rep.message = (f"The face is only {rep.face_px}px wide; at least {MIN_REF_FACE_PX}px "
                           "is needed. Use a closer or higher-resolution photo.")
            logger.warning("face_target_rejected reason=face_too_small px=%d", rep.face_px)
            return False
        if rep.det_score < MIN_REF_DET_SCORE:
            rep.reason = "low_confidence"
            rep.message = "The face in the photo is too unclear to use. Try a sharper photo."
            return False

        if rep.n_faces > 1:
            second = sorted(widths)[-2]
            if second >= 0.6 * widths[i]:
                rep.warnings.append(
                    f"{rep.n_faces} faces found in the photo; using the most prominent one. "
                    "Crop the photo to the child to be sure the right face is used.")
        if rep.face_px < WARN_REF_FACE_PX:
            rep.warnings.append(f"Face is small ({rep.face_px}px); a closer photo matches better.")
        if abs(rep.yaw) > WARN_REF_YAW:
            rep.warnings.append("Face is turned away from the camera; a frontal photo matches better.")
        if rep.blur < WARN_REF_BLUR:
            rep.warnings.append("Photo looks blurry; a sharper photo matches better.")

        views = [aligned, aligned[:, ::-1]]
        if self.template_lowres:
            for px in (56, 32):
                small = cv2.resize(aligned, (px, px), interpolation=cv2.INTER_AREA)
                low = cv2.resize(small, (112, 112), interpolation=cv2.INTER_CUBIC)
                views += [low, low[:, ::-1]]
        feats = np.asarray(self._rec.get_feat([np.ascontiguousarray(v) for v in views]),
                           dtype=np.float32).reshape(len(views), -1)
        feats /= np.maximum(np.linalg.norm(feats, axis=1, keepdims=True), 1e-9)
        # Mean of each resolution's (orig, mirror) pair -> one row per resolution.
        rows = [_l2(feats[j] + feats[j + 1]) for j in range(0, len(feats), 2)]
        self._target_templates = np.stack(rows)
        self._target_embedding = _l2(self._target_templates[0])
        self._tracks.clear()
        self._memo.clear()
        rep.ok = True
        rep.reason = "ok"
        rep.message = "Reference face enrolled." + (" " + " ".join(rep.warnings) if rep.warnings else "")
        logger.info("face_target_set px=%d det=%.2f yaw=%.2f blur=%.0f warnings=%d",
                    rep.face_px, rep.det_score, rep.yaw, rep.blur, len(rep.warnings))
        return True

    def set_target_from_file(self, path: str) -> bool:
        """Load a reference face from disk."""
        image = cv2.imread(path)
        if image is None:
            raise FileNotFoundError(f"Cannot read image: {path}")
        return self.set_target(image)

    def clear_target(self):
        """Clear the current target embedding."""
        with self._lock:
            self._reset_target()

    def _reset_target(self) -> None:
        self._target_embedding = None
        self._target_templates = None
        self._tracks.clear()
        self._memo.clear()

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------

    def _similarity(self, emb: np.ndarray) -> float:
        """Similarity of a unit embedding to the enrolled template.

        The reference is enrolled at several resolutions; a low-resolution
        probe should be compared with the matching-resolution row, so the
        best row wins.
        """
        templates = self._target_templates
        if templates is None:
            return float(np.dot(self._target_embedding, emb))
        return float(np.max(templates @ emb))

    def compare(self, crop: np.ndarray) -> float:
        """Compare a detected person crop's face against the target.

        Stateless apart from a one-shot memo that lets the dashboard re-query
        a crop it just passed to :meth:`find_match` for free.

        Returns:
            Cosine similarity (0-1), or 0.0 if no face found in crop.
        """
        with self._lock:
            if self._target_embedding is None:
                return 0.0
            hit = self._memo.get(id(crop))
            if hit is not None and hit[0] is crop:
                return hit[1]
            obs = self._observe(crop)
            if obs is None:
                return 0.0
            return max(0.0, self._similarity(obs.embedding))

    def _track_score(self, tid: int, crop: np.ndarray) -> float:
        """Score a crop through its track's aggregated face template."""
        tr = self._tracks.get(tid)
        if tr is not None and self._call - tr.last_call > TRACK_STALE_CALLS:
            tr = None  # recycled / long-gone id: start fresh
        if tr is None:
            if len(self._tracks) >= self.track_max:
                oldest = min(self._tracks, key=lambda t: self._tracks[t].last_call)
                del self._tracks[oldest]
            tr = self._tracks[tid] = _TrackFace()
        tr.last_call = self._call

        settled = len(tr.obs) >= self.track_settled_obs and tr.obs[0][0] >= 0.5
        due = self._call - tr.last_embed_call >= self.track_refresh_every
        backoff = 1 + min(tr.misses // 3, 3) if not tr.obs else 1
        retry_ok = self._call - tr.last_embed_call >= backoff
        if not (settled and not due) and retry_ok:
            tr.last_embed_call = self._call
            obs = self._observe(crop)
            if obs is None:
                tr.misses += 1
            else:
                tr.misses = 0
                if tr.obs and float(_l2(np.sum([o[1] for o in tr.obs], axis=0)) @ obs.embedding) < TRACK_SWITCH_SIM:
                    tr.obs.clear()  # track id jumped to a different person
                tr.obs.append((obs.quality, obs.embedding))
                tr.obs.sort(key=lambda o: -o[0])
                del tr.obs[self.track_top_k:]
        if not tr.obs:
            return 0.0
        w = np.array([max(o[0], 1e-3) for o in tr.obs], dtype=np.float32)
        agg = _l2(np.sum([wi * o[1] for wi, o in zip(w, tr.obs)], axis=0))
        return max(0.0, self._similarity(agg))

    def _prune_tracks(self) -> None:
        if len(self._tracks) > 32:
            stale = [t for t, tr in self._tracks.items() if self._call - tr.last_call > 300]
            for t in stale:
                del self._tracks[t]

    def find_match(self, detections: list[dict]) -> tuple[int | None, float]:
        """Find the best face match among detected persons.

        Args:
            detections: List of dicts with a 'crop' key from PersonDetector.
                An optional integer 'track_id' enables per-track embedding
                aggregation and reuse across frames.

        Returns:
            (index, score) of best face match, or (None, 0.0) if none.
        """
        with self._lock:
            return self._find_match(detections)

    def _find_match(self, detections: list[dict]) -> tuple[int | None, float]:
        if self._target_embedding is None or not detections:
            return None, 0.0

        self._memo = {}
        self._call += 1  # one tick per frame
        self._prune_tracks()
        best_idx = None
        best_score = 0.0

        for i, det in enumerate(detections):
            crop = det.get("crop")
            if crop is None or crop.size == 0:
                continue
            tid = det.get("track_id")
            if self.track_reuse and tid is not None:
                score = self._track_score(int(tid), crop)
            else:
                score = self.compare(crop)
            self._memo[id(crop)] = (crop, score)
            if score > best_score:
                best_score = score
                best_idx = i

        if best_score >= self.match_threshold:
            return best_idx, best_score

        return None, best_score

    @property
    def has_target(self) -> bool:
        return self._target_embedding is not None
