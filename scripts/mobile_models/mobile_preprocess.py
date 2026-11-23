"""Reference preprocessing and postprocessing for the mobile models.

These functions are the executable form of the contracts in
``docs/mobile-models.md``. ``make_models.py verify`` feeds the converted models
through them, and the tests check them against upstream behaviour: YOLOX's own
``preproc`` for the detector, and open_clip's evaluation transform (the one
``flightrisk/vision/reid.py`` uses on the ground) for the embedder. The Android
and iOS apps must implement the same steps.

Only numpy is needed at import time. The resize helpers import OpenCV (detector)
or Pillow (embedder) when called.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Detector: YOLOX-S (COCO, 80 classes)
# ---------------------------------------------------------------------------

#: Square network input, in pixels.
YOLOX_INPUT_SIZE = 640
#: Value of every padded pixel, in all three channels.
YOLOX_PAD_VALUE = 114
#: Strides of the three output levels, in the order their rows appear in the output.
YOLOX_STRIDES = (8, 16, 32)
YOLOX_NUM_CLASSES = 80
#: Values per output row: cx, cy, w, h, objectness, then 80 class scores.
YOLOX_ROW_LENGTH = 5 + YOLOX_NUM_CLASSES
#: Rows in the output: 80*80 + 40*40 + 20*20 at a 640 input.
YOLOX_NUM_ANCHORS = sum((YOLOX_INPUT_SIZE // s) ** 2 for s in YOLOX_STRIDES)
#: COCO class index of "person".
COCO_PERSON = 0
#: NMS IoU of upstream's ``yolox.utils.postprocess`` (per class). The score threshold is
#: the one ``verify`` compares detections at; the apps choose their own.
YOLOX_NMS_IOU = 0.45
YOLOX_SCORE_THRESHOLD = 0.25


def yolox_scale(height: int, width: int, size: int = YOLOX_INPUT_SIZE) -> tuple[float, int, int]:
    """The resize ratio and the resized height and width of a ``height`` x ``width`` frame.

    As upstream's ``preproc``: ``r = min(size / height, size / width)`` and each
    side is ``int(side * r)``, truncated toward zero.
    """
    if height <= 0 or width <= 0:
        raise ValueError("the image has no pixels")
    r = min(size / height, size / width)
    return r, int(height * r), int(width * r)


def yolox_letterbox(image_bgr: np.ndarray, size: int = YOLOX_INPUT_SIZE) -> tuple[np.ndarray, float]:
    """The ``size`` x ``size`` uint8 BGR canvas the detector expects, and the resize ratio.

    The frame keeps its aspect ratio, is resized bilinearly (OpenCV
    ``INTER_LINEAR``) and is placed at the top-left corner; the rest of the
    canvas is ``YOLOX_PAD_VALUE`` in every channel. This is upstream's
    ``yolox.data.data_augment.preproc`` before its transpose.
    """
    import cv2

    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.dtype != np.uint8:
        raise ValueError("expected an H x W x 3 uint8 BGR image")
    r, new_h, new_w = yolox_scale(image_bgr.shape[0], image_bgr.shape[1], size)
    canvas = np.full((size, size, 3), YOLOX_PAD_VALUE, dtype=np.uint8)
    resized = cv2.resize(image_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas[:new_h, :new_w] = resized
    return canvas, r


def yolox_input_tensor(canvas_bgr: np.ndarray) -> np.ndarray:
    """The float32 ``[1, 3, H, W]`` network input of a letterboxed uint8 BGR canvas.

    Channels stay in BGR order and values stay in 0..255: no division by 255,
    no mean or standard deviation. (Upstream's ``legacy`` mode, which did
    normalise, is for weights older than 0.1.1rc0 and does not apply.)
    """
    if canvas_bgr.ndim != 3 or canvas_bgr.shape[2] != 3:
        raise ValueError("expected an H x W x 3 canvas")
    chw = np.ascontiguousarray(canvas_bgr.transpose(2, 0, 1), dtype=np.float32)
    return chw[None]


def yolox_anchor_grid(size: int = YOLOX_INPUT_SIZE) -> tuple[np.ndarray, np.ndarray]:
    """The grid cell (x, y) and stride of every output row, in output order.

    Rows run over the stride-8 level, then stride 16, then stride 32; within a
    level, row-major (y outer, x inner). The model already decodes its boxes
    with these; the apps only need them to interpret a row by index.
    """
    cells, strides = [], []
    for stride in YOLOX_STRIDES:
        n = size // stride
        ys, xs = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        cells.append(np.stack([xs.ravel(), ys.ravel()], axis=1))
        strides.append(np.full(n * n, stride))
    return np.concatenate(cells), np.concatenate(strides)


def yolox_scores(raw: np.ndarray) -> np.ndarray:
    """Per-row, per-class scores ``objectness * class_score`` of a ``[N, 85]`` output."""
    raw = np.asarray(raw, dtype=np.float32)
    return raw[:, 4:5] * raw[:, 5:]


def box_iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU matrix of ``[N, 4]`` and ``[M, 4]`` boxes given as x1, y1, x2, y2."""
    a = np.asarray(a, dtype=np.float64).reshape(-1, 4)
    b = np.asarray(b, dtype=np.float64).reshape(-1, 4)
    tl = np.maximum(a[:, None, :2], b[None, :, :2])
    br = np.minimum(a[:, None, 2:], b[None, :, 2:])
    inter = np.prod(np.clip(br - tl, 0, None), axis=2)
    area_a = np.prod(np.clip(a[:, 2:] - a[:, :2], 0, None), axis=1)
    area_b = np.prod(np.clip(b[:, 2:] - b[:, :2], 0, None), axis=1)
    union = area_a[:, None] + area_b[None, :] - inter
    return np.where(union > 0, inter / np.where(union > 0, union, 1), 0.0)


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    order = list(np.argsort(-scores, kind="stable"))
    keep: list[int] = []
    while order:
        i = order.pop(0)
        keep.append(i)
        if not order:
            break
        ious = box_iou(boxes[i], boxes[order])[0]
        order = [j for j, iou in zip(order, ious) if iou <= iou_threshold]
    return keep


def yolox_postprocess(
    raw: np.ndarray,
    ratio: float,
    score_threshold: float = YOLOX_SCORE_THRESHOLD,
    nms_iou: float = YOLOX_NMS_IOU,
    classes: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    """Detections in original-image pixels from one ``[N, 85]`` (or ``[1, N, 85]``) output.

    As upstream's ``yolox.utils.postprocess``: each row takes its best class,
    its score is ``objectness * class_score``, rows under ``score_threshold``
    are dropped, and non-maximum suppression runs per class. Boxes are
    converted from centre and size to corners and divided by ``ratio`` (from
    ``yolox_letterbox``). ``classes`` keeps only those class indices (``(0,)``
    for persons). The result is sorted by score, highest first.
    """
    raw = np.asarray(raw, dtype=np.float32)
    if raw.ndim == 3:
        raw = raw[0]
    if raw.ndim != 2 or raw.shape[1] != YOLOX_ROW_LENGTH:
        raise ValueError(f"expected rows of {YOLOX_ROW_LENGTH} values, got shape {raw.shape}")
    if ratio <= 0:
        raise ValueError("ratio must be positive")
    cls_scores = raw[:, 5:]
    cls = cls_scores.argmax(axis=1)
    score = raw[:, 4] * cls_scores[np.arange(len(raw)), cls]
    mask = score >= score_threshold
    if classes is not None:
        mask &= np.isin(cls, classes)
    idx = np.nonzero(mask)[0]
    cx, cy, w, h = (raw[idx, k] for k in range(4))
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1) / ratio
    out: list[dict[str, Any]] = []
    for c in np.unique(cls[idx]):
        sel = np.nonzero(cls[idx] == c)[0]
        for k in _nms(boxes[sel], score[idx][sel], nms_iou):
            j = sel[k]
            out.append({"box": boxes[j].tolist(), "score": float(score[idx][j]), "class": int(c)})
    out.sort(key=lambda d: -d["score"])
    return out


# ---------------------------------------------------------------------------
# From a detection to the embedder's crop
# ---------------------------------------------------------------------------

#: How a detection becomes the embedder's crop, as written into mobile-models.json.
CROP_RULE = (
    "cut the full-resolution camera frame (not the 640x640 detector input) at the detection's box in frame "
    "pixels (x1, y1, x2, y2 = (cx -/+ w/2, cy -/+ h/2) / ratio): clip x to [0, width] and y to [0, height], "
    "truncate each corner to an integer toward zero, and take frame[y1:y2, x1:x2] with no margin; "
    "as flightrisk/vision/detector.py does (its detector clips boxes to the frame, then .astype(int))"
)


def crop_pixels(box: Any, height: int, width: int) -> tuple[int, int, int, int]:
    """The integer ``(x1, y1, x2, y2)`` a frame of ``height`` x ``width`` is sliced at for ``box`` (``CROP_RULE``)."""
    x1, y1, x2, y2 = (float(v) for v in box)
    return (
        int(min(max(x1, 0.0), float(width))),
        int(min(max(y1, 0.0), float(height))),
        int(min(max(x2, 0.0), float(width))),
        int(min(max(y2, 0.0), float(height))),
    )


def crop_box(frame: np.ndarray, box: Any) -> np.ndarray:
    """``frame[y1:y2, x1:x2]`` for a detection box in frame pixels, as ``CROP_RULE`` says.

    This is the ground's crop (``flightrisk/vision/detector.py``: the box is
    clipped to the frame by the detector, then ``astype(int)`` truncates it,
    then the frame is sliced). The crop is a view of ``frame``, in the frame's
    own channel order; convert it to RGB for the embedder.
    """
    x1, y1, x2, y2 = crop_pixels(box, frame.shape[0], frame.shape[1])
    return frame[y1:y2, x1:x2]


# ---------------------------------------------------------------------------
# Appearance embedder: open_clip ViT-B-16, laion2b_s34b_b88k (image tower)
# ---------------------------------------------------------------------------

#: The ground's model, as ``flightrisk/vision/reid_spec.py`` names it.
CLIP_MODEL_ID = "open_clip:ViT-B-16:laion2b_s34b_b88k"
CLIP_INPUT_SIZE = 224
CLIP_EMBEDDING_DIM = 512
#: open_clip's OPENAI_DATASET_MEAN and OPENAI_DATASET_STD, which this pretrained config uses.
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)


def clip_resized_size(height: int, width: int, size: int = CLIP_INPUT_SIZE) -> tuple[int, int]:
    """The (height, width) after resizing the shortest side to ``size``.

    As torchvision's ``Resize(size)``: the short side becomes ``size`` and the
    long side ``int(size * long / short)``, truncated toward zero.
    """
    if height <= 0 or width <= 0:
        raise ValueError("the image has no pixels")
    if width <= height:
        return int(size * height / width), size
    return size, int(size * width / height)


def clip_crop_offsets(height: int, width: int, size: int = CLIP_INPUT_SIZE) -> tuple[int, int]:
    """(top, left) of the centre crop, as torchvision's ``center_crop``.

    Each is ``int(round((side - size) / 2))``; Python's ``round`` sends halves
    to the even neighbour, so an odd surplus of 1 gives 0 and of 3 gives 2.
    """
    return round((height - size) / 2.0), round((width - size) / 2.0)


def clip_resize_crop(image_rgb: Any, size: int = CLIP_INPUT_SIZE) -> np.ndarray:
    """The ``size`` x ``size`` uint8 RGB crop the embedder sees.

    ``image_rgb`` is an H x W x 3 uint8 RGB array or a Pillow image. The
    shortest side is resized to ``size`` with Pillow's bicubic filter (what
    torchvision does for a Pillow image, so what open_clip does on the ground),
    then the centre ``size`` x ``size`` square is cut out.
    """
    from PIL import Image

    if isinstance(image_rgb, Image.Image):
        img = image_rgb.convert("RGB")
    else:
        arr = np.asarray(image_rgb)
        if arr.ndim != 3 or arr.shape[2] != 3 or arr.dtype != np.uint8:
            raise ValueError("expected an H x W x 3 uint8 RGB image")
        img = Image.fromarray(np.ascontiguousarray(arr))
    new_h, new_w = clip_resized_size(img.height, img.width, size)
    if (new_h, new_w) != (img.height, img.width):
        img = img.resize((new_w, new_h), Image.Resampling.BICUBIC)
    top, left = clip_crop_offsets(new_h, new_w, size)
    return np.asarray(img, dtype=np.uint8)[top:top + size, left:left + size].copy()


def clip_input_tensor(crop_rgb: np.ndarray) -> np.ndarray:
    """The float32 ``[1, 3, 224, 224]`` input of a uint8 RGB crop: ``(v / 255 - mean) / std``."""
    if crop_rgb.ndim != 3 or crop_rgb.shape[2] != 3:
        raise ValueError("expected an H x W x 3 crop")
    x = crop_rgb.astype(np.float32) / np.float32(255.0)
    x = (x - np.asarray(CLIP_MEAN, dtype=np.float32)) / np.asarray(CLIP_STD, dtype=np.float32)
    return np.ascontiguousarray(x.transpose(2, 0, 1))[None]


def clip_preprocess(image_rgb: Any) -> np.ndarray:
    """``clip_input_tensor(clip_resize_crop(image_rgb))``."""
    return clip_input_tensor(clip_resize_crop(image_rgb))


def l2_normalize(v: np.ndarray) -> np.ndarray:
    """``v`` divided by its L2 norm (along the last axis); a zero vector stays zero."""
    v = np.asarray(v, dtype=np.float64)
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    return np.where(norm > 0, v / np.where(norm > 0, norm, 1), 0.0)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors."""
    a = l2_normalize(np.ravel(a))
    b = l2_normalize(np.ravel(b))
    return float(np.dot(a, b))
