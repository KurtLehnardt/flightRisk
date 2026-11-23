#!/usr/bin/env python3
"""Build the store-safe on-device models for the FlightRisk Android and iOS apps.

The models are not committed and not hosted by FlightRisk. On the release build
machine this script downloads the pinned upstream weights, checks their
SHA-256, converts them, checks the conversions against PyTorch, and copies the
checked files into each app's (gitignored) model folder:

    python scripts/mobile_models/make_models.py all

Subcommands (each takes ``--work-dir``, default ``build/mobile_models``):

``fetch``
    Download the pinned files into ``<work-dir>/downloads`` over https only
    (a redirect to anything else is refused). A file that is already there
    and matches its pin is kept; anything else is downloaded again, and a
    download whose size or SHA-256 differs from the pin is deleted and the
    command fails.
``convert``
    Re-check every download against its pin (the bytes that are checked are
    the bytes that are loaded), then write ``<work-dir>/out``:
    YOLOX-S as ONNX fp32 and fp16 and as Core ML fp32 and fp16 ML programs,
    and the open_clip ViT-B-16 image tower as ONNX fp32, weight-only int8 and
    dynamic int8, and as Core ML fp16 and 8-bit palettised ML programs.
``verify``
    Run every output and PyTorch on the same inputs (including a pinned
    public-domain photograph of people, their crops at native size and at
    16 to 48 pixels tall, and procedural silhouettes), compare them against
    the tolerances in ``TOLERANCES``, check the ONNX and Core ML interfaces,
    write ``<work-dir>/verify.json`` and the photo-free golden fixtures in
    ``<work-dir>/golden`` for the apps' unit tests. Fails if any gated check
    fails or any output is not finite.
``install --android DIR --ios DIR``
    Copy one detector and one embedder per platform (``DEFAULT_VARIANTS``,
    changeable with ``--android-detector`` and friends) into the app folders,
    with ``mobile-models.json`` (what was installed, its digests and the
    preprocessing contract) and ``model-notices.md`` (the licence texts).
    Refuses any file whose digest is not the one ``verify`` passed, and a
    build made with unpinned tools unless ``--allow-unpinned-build`` is given.
    Each file is staged next to its destination, checked, and moved into
    place; the old manifest is deleted first and the new one written last.
    Then runs ``licence_guard.py`` over both folders.
``all``
    ``fetch``, ``convert``, ``verify`` and ``install``, stopping at the first failure.

``--targets android`` skips the Core ML outputs (Core ML predictions need
macOS); ``--targets ios`` skips the ONNX outputs.

The tools are hash-locked in ``requirements.lock`` next to this file (generated
from ``requirements.txt``); install them with ``pip install --require-hashes``
in a separate virtual environment (never FlightRisk's own venv). ``convert`` and
``verify`` refuse to run unless the installed packages are exactly the locked
set, because the tolerances were measured with them; ``--allow-unpinned-tools``
overrides that, and the build is then recorded as unpinned.

Preprocessing contracts, the licence table and the follow-ups are in
``docs/mobile-models.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import licence_guard
import mobile_preprocess as mp
from yolox_vendor import YOLOX_COMMIT

REPO_ROOT = HERE.parents[1]
DEFAULT_WORK_DIR = REPO_ROOT / "build" / "mobile_models"
DEFAULT_ANDROID_DIR = REPO_ROOT / "mobile" / "app" / "src" / "main" / "assets"
DEFAULT_IOS_DIR = REPO_ROOT / "ios" / "FlightRisk" / "Models"
NOTICES = HERE / "NOTICES.md"
LOCK_FILE = HERE / "requirements.lock"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "aerial_tiny"
FIXTURE_IMAGES = ("img_a.png", "img_b.png", "img_c.png")

MANIFEST_NAME = licence_guard.MANIFEST_NAME
MANIFEST_GENERATOR = licence_guard.MANIFEST_GENERATOR
NOTICES_INSTALLED_NAME = "model-notices.md"
VERIFY_NAME = "verify.json"
BUILD_NAME = "build.json"
GOLDEN_DIR_NAME = "golden"

#: The ONNX opset every Android model uses, and the highest IR version allowed:
#: the app's onnxruntime-android (1.23.2) reads both.
ONNX_OPSET = 17
MAX_ONNX_IR_VERSION = 9
#: Core ML: ML program, iOS 17 or later (the app's deployment target).
COREML_MIN_SPEC_VERSION = 8

_DOWNLOAD_TIMEOUT_S = 120.0
_CHUNK = 1 << 20
#: Sent with every download; Wikimedia refuses requests without a descriptive User-Agent.
USER_AGENT = "FlightRisk-make_models/1.0 (+https://github.com/KurtLehnardt/flightRisk)"


# ---------------------------------------------------------------------------
# Pinned upstream files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Download:
    """A pinned upstream file: where it comes from, its size and its SHA-256."""

    key: str
    url: str
    sha256: str
    size: int
    filename: str
    licence: str
    what: str


#: YOLOX-S COCO weights from the official Megvii release 0.1.1rc0 (Apache-2.0).
YOLOX_S_WEIGHTS = Download(
    key="yolox_s_weights",
    url="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.pth",
    sha256="f55ded7181e1b0c13285c56e7790b8f0e8f8db590fe4edb37f0b7f345c913a30",
    size=72_089_125,
    filename="yolox_s.pth",
    licence="Apache-2.0",
    what="YOLOX-S checkpoint (COCO, 80 classes), Megvii release 0.1.1rc0",
)

#: The open_clip ViT-B-16 laion2b_s34b_b88k weights, from the Hugging Face repository
#: open_clip's pretrained config names, at a pinned commit (MIT).
CLIP_HF_REPO = "laion/CLIP-ViT-B-16-laion2B-s34B-b88K"
CLIP_HF_REVISION = "7288da5a0d6f0b51c4a2b27c624837a9236d0112"
CLIP_WEIGHTS = Download(
    key="clip_vit_b16_laion2b_weights",
    url=f"https://huggingface.co/{CLIP_HF_REPO}/resolve/{CLIP_HF_REVISION}/open_clip_model.safetensors",
    sha256="3f25d29d3cc74e1d25d47e0593b4dd0864ced1e9b4d8e486a247ca4502f227f1",
    size=598_516_980,
    filename="open_clip_model.safetensors",
    licence="MIT",
    what="open_clip ViT-B-16 laion2b_s34b_b88k (image and text towers; only the image tower is used)",
)

#: YOLOX's own demo picture (a dog, a bicycle and a truck; no person), used only by
#: ``verify`` so the detector parity covers real detections. Never installed.
YOLOX_SAMPLE_IMAGE = Download(
    key="yolox_sample_image",
    url=f"https://raw.githubusercontent.com/Megvii-BaseDetection/YOLOX/{YOLOX_COMMIT}/assets/dog.jpg",
    sha256="5a9522051c3cec2bbd2f6323fccba32e8fbf3ddcc2b3e2fd46b04c720bc6f866",
    size=163_759,
    filename="yolox_sample_dog.jpg",
    licence="Apache-2.0",
    what="YOLOX assets/dog.jpg, a parity input for verify only",
)

#: A public-domain photograph of several people walking through grassland, seen from
#: behind (U.S. Fish and Wildlife Service, Mountain-Prairie Region; a work of the U.S.
#: federal government). Used only by ``verify`` for the person and small-crop parity:
#: kept in the gitignored downloads folder, never installed and never committed.
#: Commons page: https://commons.wikimedia.org/wiki/File:Group_Hiking_on_Munch%27s_Coulee_National_Hiking_Trail_(5220995838).jpg
PERSON_PHOTO = Download(
    key="person_parity_photo",
    url=("https://upload.wikimedia.org/wikipedia/commons/4/4d/"
         "Group_Hiking_on_Munch%27s_Coulee_National_Hiking_Trail_%285220995838%29.jpg"),
    sha256="0b7230075f66aeda713b53924e6b78ccb768e7f0875e83cca7bfbfe1939be581",
    size=1_567_122,
    filename="person_parity_hikers.jpg",
    licence="Public domain (work of the U.S. Fish and Wildlife Service)",
    what="Group Hiking on Munch's Coulee National Hiking Trail, 2816 x 1584, a parity input for verify only",
)

DOWNLOADS: tuple[Download, ...] = (YOLOX_S_WEIGHTS, CLIP_WEIGHTS, YOLOX_SAMPLE_IMAGE, PERSON_PHOTO)

#: The top-level conversion tools (distribution name, version), as in requirements.txt.
#: requirements.lock pins these and every dependency with hashes; check_tool_versions
#: compares the whole installed set with the lock.
PINNED_TOOLS: tuple[tuple[str, str], ...] = (
    ("torch", "2.7.0"),
    ("torchvision", "0.22.0"),
    ("open_clip_torch", "3.3.0"),
    ("onnx", "1.18.0"),
    ("onnxruntime", "1.22.1"),
    ("coremltools", "9.0"),
    ("safetensors", "0.8.0"),
    ("numpy", "2.3.5"),
    ("pillow", "13.0.0"),
    ("opencv-python-headless", "5.1.0.95"),
    ("pytest", "9.2.0"),
)
#: Installed distributions the lock does not list and that may be present: the installer itself.
UNLOCKED_ALLOWED = frozenset({"pip"})


class ChecksumMismatch(RuntimeError):
    """A file's size or SHA-256 is not the pinned one."""


class DownloadFailed(RuntimeError):
    """A pinned file could not be downloaded."""


class VerificationFailed(RuntimeError):
    """An output is missing, malformed or outside its parity tolerance."""


class ToolVersionMismatch(RuntimeError):
    """An installed conversion tool is not the pinned version."""


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Output:
    """One converted model file (or Core ML package) in ``<work-dir>/out``."""

    platform: str  # "android" or "ios"
    role: str  # "detector" or "embedder"
    variant: str  # "fp32", "fp16", "int8-weights", "int8-dynamic" or "palettized8"
    filename: str
    installed_name: str

    @property
    def key(self) -> str:
        return f"{self.platform}/{self.filename}"


OUTPUTS: tuple[Output, ...] = (
    Output("android", "detector", "fp32", "yolox_s.onnx", "yolox_s.onnx"),
    Output("android", "detector", "fp16", "yolox_s_fp16.onnx", "yolox_s.onnx"),
    Output("android", "embedder", "fp32", "clip_vit_b16_laion2b.onnx", "clip_vit_b16_laion2b.onnx"),
    Output("android", "embedder", "int8-dynamic", "clip_vit_b16_laion2b_int8.onnx", "clip_vit_b16_laion2b.onnx"),
    Output("android", "embedder", "int8-weights", "clip_vit_b16_laion2b_w8.onnx", "clip_vit_b16_laion2b.onnx"),
    Output("ios", "detector", "fp32", "YOLOXs.mlpackage", "YOLOXs.mlpackage"),
    Output("ios", "detector", "fp16", "YOLOXsFP16.mlpackage", "YOLOXs.mlpackage"),
    Output("ios", "embedder", "fp16", "CLIPViTB16LAION.mlpackage", "CLIPViTB16LAION.mlpackage"),
    Output("ios", "embedder", "palettized8", "CLIPViTB16LAION8bit.mlpackage", "CLIPViTB16LAION.mlpackage"),
)

#: Which variant ``install`` copies for each (platform, role) unless told otherwise.
#: iOS: the fp32 detector matches PyTorch on every Core ML compute unit, so the app
#: needs no compute-unit setting; the palettised embedder is half the size of the fp16
#: one, and an iPhone only compares its own embeddings with each other (phone-as-edge,
#: which sends embeddings to the ground, is Android only).
DEFAULT_VARIANTS: dict[tuple[str, str], str] = {
    ("android", "detector"): "fp32",
    ("android", "embedder"): "int8-weights",
    ("ios", "detector"): "fp32",
    ("ios", "embedder"): "palettized8",
}

#: Model interface names, the same on both platforms.
DETECTOR_INPUT = "images"
DETECTOR_OUTPUT = "detections"
EMBEDDER_INPUT = "input"
EMBEDDER_OUTPUT = "embedding"

#: Parity tolerances against PyTorch fp32 on the same inputs (see docs/mobile-models.md).
#: Detector: largest absolute difference of any score (objectness x class), largest
#: absolute box difference in input pixels over rows PyTorch scores at least
#: ``BOX_ROWS_MIN_SCORE``, and the smallest IoU between matched detections after NMS.
#: Embedder: smallest cosine similarity with the PyTorch embedding, and the largest
#: change in the similarity the ground would compute (PyTorch target vs this model).
#: Core ML outputs are gated on ``COREML_GATE_UNITS``; the other compute units are
#: measured and recorded in verify.json but do not fail the build. Any output with a
#: value that is not finite (NaN or infinite) fails, whatever its tolerance.
TOLERANCES: dict[str, dict[str, float]] = {
    "android/yolox_s.onnx": {"score_max_abs": 1e-3, "box_max_abs_px": 0.05, "match_iou_min": 0.999},
    "android/yolox_s_fp16.onnx": {"score_max_abs": 0.02, "box_max_abs_px": 2.0, "match_iou_min": 0.95},
    "ios/YOLOXs.mlpackage": {"score_max_abs": 1e-3, "box_max_abs_px": 0.05, "match_iou_min": 0.999},
    "ios/YOLOXsFP16.mlpackage": {"score_max_abs": 0.02, "box_max_abs_px": 2.0, "match_iou_min": 0.95},
    "android/clip_vit_b16_laion2b.onnx": {"cosine_min": 0.9999, "similarity_max_abs": 0.001},
    "android/clip_vit_b16_laion2b_int8.onnx": {"cosine_min": 0.97, "similarity_max_abs": 0.06},
    "android/clip_vit_b16_laion2b_w8.onnx": {"cosine_min": 0.995, "similarity_max_abs": 0.01},
    "ios/CLIPViTB16LAION.mlpackage": {"cosine_min": 0.99, "similarity_max_abs": 0.02},
    "ios/CLIPViTB16LAION8bit.mlpackage": {"cosine_min": 0.99, "similarity_max_abs": 0.03},
}
#: Rows PyTorch scores below this are left out of the box comparison (their boxes are noise).
BOX_ROWS_MIN_SCORE = 0.05

#: Outputs whose parity on the person inputs (the pinned photograph's people and their
#: crops at native size and at 16 to 48 pixels tall) also gates verify: the Android
#: default detector and the Android embedders whose embeddings the ground may compare
#: with its own (phone-as-edge). The person metrics of every other output are recorded
#: only, and ``install`` refuses an Android embedder that is not listed here unless
#: ``--allow-ungated-embedder`` is given.
PERSON_GATED: frozenset[str] = frozenset({
    "android/yolox_s.onnx", "android/clip_vit_b16_laion2b_w8.onnx", "android/clip_vit_b16_laion2b.onnx",
})
#: The ground's default re-identification threshold (``config.vision.reid_threshold``),
#: and the band around it where a change in similarity can flip a match decision.
GROUND_MATCH_THRESHOLD = 0.55
NEAR_THRESHOLD_BAND = 0.05
#: Tolerances on the person inputs, where they differ from TOLERANCES. The largest
#: similarity change is a maximum over every pair of inputs, so it grows with their
#: number: over the 35 person crops the weight-only int8 embedder moves a similarity by
#: up to 0.0134 (and the Core ML fp16 embedder by 0.013), against 0.005 over the 9
#: general inputs, while its smallest cosine stays at 0.9987. Its person tolerance is
#: therefore 0.02 over all pairs, but 0.01 over the pairs whose PyTorch similarity is
#: within NEAR_THRESHOLD_BAND of GROUND_MATCH_THRESHOLD, where a change can flip a match.
PERSON_TOLERANCES: dict[str, dict[str, float]] = {
    "android/clip_vit_b16_laion2b_w8.onnx": {
        "cosine_min": 0.995, "similarity_max_abs": 0.02, "similarity_near_threshold_max_abs": 0.01,
    },
}
#: Heights (pixels) the person crops are scaled down to, as a person seen from altitude.
SMALL_CROP_HEIGHTS = (16, 24, 32, 48)
#: PyTorch person detections at least this sure are cut out as embedder crops.
PERSON_CROP_MIN_SCORE = 0.5

#: The Core ML compute units each output is checked with; a gated output must pass on
#: every unit listed. The fp32 detector matches PyTorch on every unit, so it is gated
#: on all four and the app may use any. On the Neural Engine the fp16 detector's
#: scores move by up to about 0.13 against PyTorch on small objects (measured on an
#: M1 Max), so it is gated on the CPU and GPU, and an app that installs it must load
#: it with ``computeUnits = .cpuAndGPU`` (``COREML_COMPUTE_UNITS``). The embedders stay
#: within tolerance on every unit and are gated on ``ALL``.
COREML_ALL_UNITS = ("CPU_AND_GPU", "ALL", "CPU_AND_NE", "CPU_ONLY")
COREML_GATE_UNITS: dict[str, tuple[str, ...]] = {
    "ios/YOLOXs.mlpackage": COREML_ALL_UNITS,
    "ios/YOLOXsFP16.mlpackage": ("CPU_AND_GPU",),
    "ios/CLIPViTB16LAION.mlpackage": ("ALL",),
    "ios/CLIPViTB16LAION8bit.mlpackage": ("ALL",),
}
#: The ``MLModelConfiguration.computeUnits`` each iOS output must be loaded with, as
#: recorded in its metadata and in mobile-models.json: "all" (any unit) or "cpuAndGPU".
COREML_COMPUTE_UNITS: dict[str, str] = {
    "ios/YOLOXs.mlpackage": "all",
    "ios/YOLOXsFP16.mlpackage": "cpuAndGPU",
    "ios/CLIPViTB16LAION.mlpackage": "all",
    "ios/CLIPViTB16LAION8bit.mlpackage": "all",
}

#: Smallest weight (in values) the 8-bit palettisation touches. coremltools clusters
#: weights of 10,000 values or more with its bundled kmeans1d and smaller ones with
#: scikit-learn, which is not a pinned tool. ViT-B-16 has no weight between 2,048
#: (coremltools' default threshold) and 10,000 values, so this changes nothing for it.
PALETTIZE_MIN_WEIGHTS = 10_000

#: The one layer the dynamic int8 copy leaves in fp32: the input of block 6's MLP
#: output projection carries the ViT's outlier activations, and quantising it per
#: tensor at run time drops the cosine with PyTorch to about 0.86.
DYNAMIC_INT8_EXCLUDE = ("/visual/transformer/resblocks.6/mlp/c_proj/MatMul",)


def outputs_for(targets: str) -> tuple[Output, ...]:
    if targets == "all":
        return OUTPUTS
    return tuple(o for o in OUTPUTS if o.platform == targets)


# ---------------------------------------------------------------------------
# Checksums and downloads (fail closed)
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: SHA-256 of a file, or of a folder's relative file names and contents in path order.
#: The licence guard computes the same digest for a listed Core ML package.
tree_sha256 = licence_guard.tree_sha256


def tree_size(path: Path) -> int:
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def check_pinned(data: bytes, download: Download, source: str) -> None:
    """Raise ChecksumMismatch unless ``data`` has the pinned size and SHA-256."""
    if len(data) != download.size:
        raise ChecksumMismatch(
            f"{source}: {len(data)} bytes, expected {download.size} for {download.key}; refused"
        )
    actual = sha256_bytes(data)
    if actual != download.sha256:
        raise ChecksumMismatch(
            f"{source}: SHA-256 {actual} is not the pinned {download.sha256} for {download.key}; refused"
        )


def read_verified(download: Download, downloads_dir: Path) -> bytes:
    """The bytes of a downloaded file, read once and refused unless they match the pin."""
    path = Path(downloads_dir) / download.filename
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise ChecksumMismatch(f"{path} is missing; run `make_models.py fetch` first") from None
    if size != download.size:
        raise ChecksumMismatch(f"{path}: {size} bytes, expected {download.size} for {download.key}; refused")
    data = path.read_bytes()
    check_pinned(data, download, str(path))
    return data


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https address (GitHub and Hugging Face redirect to their CDNs)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise DownloadFailed(f"{req.full_url}: redirected to {newurl}, which is not https; refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def https_open(url: str, timeout: float):
    """Open ``url`` over https only, refusing a redirect to any other scheme."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise DownloadFailed(f"{url}: not an https address; refused")
    opener = urllib.request.build_opener(_HttpsOnlyRedirects)
    response = opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)
    final = response.geturl()
    if urllib.parse.urlsplit(final).scheme != "https":
        response.close()
        raise DownloadFailed(f"{url}: ended at {final}, which is not https; refused")
    return response


def fetch_one(download: Download, downloads_dir: Path, opener: Callable[..., Any] = https_open) -> Path:
    """Put the pinned file in ``downloads_dir``, downloading it unless a matching copy is there.

    The download goes to a temporary file in the same folder; it is moved into
    place only after its size and SHA-256 match the pin, and is deleted otherwise.
    """
    downloads_dir = Path(downloads_dir)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    dest = downloads_dir / download.filename
    if dest.is_dir() and not dest.is_symlink():
        raise DownloadFailed(f"{dest} is a folder, where the pinned file {download.key} belongs; remove it")
    if dest.is_file() and dest.stat().st_size == download.size and sha256_file(dest) == download.sha256:
        return dest
    if dest.exists() or dest.is_symlink():
        dest.unlink()  # not the pinned file; it is never left where convert could find it
    fd, tmp_name = tempfile.mkstemp(prefix=f".{download.filename}.", suffix=".part", dir=downloads_dir)
    tmp = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        written = 0
        try:
            with os.fdopen(fd, "wb") as out, opener(download.url, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > download.size:
                        raise ChecksumMismatch(
                            f"{download.url}: more than the pinned {download.size} bytes; refused"
                        )
                    digest.update(chunk)
                    out.write(chunk)
        except ChecksumMismatch:
            raise
        except OSError as exc:
            raise DownloadFailed(f"{download.url}: download failed ({exc})") from exc
        if written != download.size:
            raise ChecksumMismatch(f"{download.url}: {written} bytes, expected {download.size}; refused")
        actual = digest.hexdigest()
        if actual != download.sha256:
            raise ChecksumMismatch(
                f"{download.url}: SHA-256 {actual} is not the pinned {download.sha256}; refused"
            )
        os.replace(tmp, dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Tool versions
# ---------------------------------------------------------------------------


def installed_versions(names: Iterable[str]) -> dict[str, str | None]:
    from importlib import metadata

    found: dict[str, str | None] = {}
    for name in names:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = None
    return found


def normalise_name(name: str) -> str:
    """A distribution name as PEP 503 compares them (``open_clip_torch`` is ``open-clip-torch``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(text: str) -> dict[str, tuple[str, str | None]]:
    """{normalised name: (version, environment marker or None)} of a ``uv pip compile`` hash lock."""
    locked: dict[str, tuple[str, str | None]] = {}
    for line in text.splitlines():
        if not line or not line[0].isalpha():
            continue  # comments, blank lines and the indented --hash lines
        spec = line.rstrip("\\").strip()
        requirement, _, marker = spec.partition(";")
        name, sep, version = requirement.strip().partition("==")
        if sep != "==" or not version:
            raise ToolVersionMismatch(f"{LOCK_FILE.name}: {line!r} is not pinned to one version")
        locked[normalise_name(name)] = (version.strip(), marker.strip() or None)
    return locked


def _marker_holds(marker: str | None) -> bool:
    if marker is None:
        return True
    try:
        from packaging.markers import Marker
    except ImportError:  # pragma: no cover - pip always vendors packaging
        from pip._vendor.packaging.markers import Marker
    return Marker(marker).evaluate()


def locked_here(lock_text: str | None = None) -> dict[str, str]:
    """{normalised name: version} of the locked packages whose markers hold on this machine."""
    text = LOCK_FILE.read_text() if lock_text is None else lock_text
    return {name: version for name, (version, marker) in read_lock(text).items() if _marker_holds(marker)}


def installed_distributions() -> dict[str, str]:
    """{normalised name: version} of every distribution installed in this environment."""
    from importlib import metadata

    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            found[normalise_name(name)] = dist.version
    return found


def tool_differences(locked: dict[str, str], installed: dict[str, str]) -> list[str]:
    """How the installed set differs from the locked one (empty when it is exactly the lock)."""
    out = []
    for name in sorted(locked):
        if name not in installed:
            out.append(f"{name} missing (locked {locked[name]})")
        elif installed[name] != locked[name]:
            out.append(f"{name} {installed[name]} (locked {locked[name]})")
    for name in sorted(set(installed) - set(locked) - UNLOCKED_ALLOWED):
        out.append(f"{name} {installed[name]} is not in the lock")
    return out


def check_tool_versions(allow_unpinned: bool) -> dict[str, Any]:
    """The installed tools and whether they are exactly the locked set.

    Raises ToolVersionMismatch when they are not, unless ``allow_unpinned``.
    The result records the versions of the top-level tools, ``pinned`` and
    the differences from ``requirements.lock``.
    """
    installed = installed_distributions()
    differences = tool_differences(locked_here(), installed)
    if differences and not allow_unpinned:
        raise ToolVersionMismatch(
            "the installed packages are not the locked set in scripts/mobile_models/requirements.lock: "
            + "; ".join(differences[:12]) + ("; ..." if len(differences) > 12 else "")
            + ". Install them in a fresh venv with `pip install --require-hashes -r scripts/mobile_models/requirements.lock`, "
            "or pass --allow-unpinned-tools (the parity tolerances were measured with the locked versions; "
            "the build is then recorded as unpinned and install refuses it without --allow-unpinned-build)."
        )
    return {
        "pinned": not differences,
        "differences": differences,
        "versions": {name: installed.get(normalise_name(name)) for name, _ in PINNED_TOOLS},
    }


# ---------------------------------------------------------------------------
# Loading the pinned models (torch is imported here, not at module load)
# ---------------------------------------------------------------------------


def load_yolox_s(downloads_dir: Path):
    """YOLOX-S in eval mode with the pinned, checked weights."""
    import torch
    from yolox_vendor import build_yolox_s

    data = read_verified(YOLOX_S_WEIGHTS, downloads_dir)
    ckpt = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    state = ckpt["model"] if isinstance(ckpt, dict) and "model" in ckpt else ckpt
    model = build_yolox_s()
    model.load_state_dict(state, strict=True)
    return model.eval()


def clip_embedder_module(visual):
    """A module that maps a normalised ``[N, 3, 224, 224]`` batch to L2-normalised ``[N, 512]`` embeddings."""
    import torch
    import torch.nn.functional as F

    class ClipImageEmbedder(torch.nn.Module):
        def __init__(self, visual_tower):
            super().__init__()
            self.visual = visual_tower

        def forward(self, x):
            return F.normalize(self.visual(x), dim=-1)

    return ClipImageEmbedder(visual).eval()


#: Functions that download weights or configs, patched to raise while load_clip builds the
#: model: (module, attribute). open_clip.factory imports its downloaders by name, so both
#: modules are patched.
_DOWNLOADERS = (
    ("open_clip.pretrained", "download_pretrained"),
    ("open_clip.pretrained", "download_pretrained_from_url"),
    ("open_clip.pretrained", "download_pretrained_from_hf"),
    ("open_clip.pretrained", "hf_hub_download"),
    ("open_clip.factory", "download_pretrained"),
    ("open_clip.factory", "download_pretrained_from_hf"),
    ("huggingface_hub", "hf_hub_download"),
    ("huggingface_hub", "snapshot_download"),
    ("torch.hub", "load"),
    ("torch.hub", "load_state_dict_from_url"),
    ("torch.hub", "download_url_to_file"),
)


@contextlib.contextmanager
def network_refused() -> Iterator[None]:
    """Make every weight or config download raise, and put Hugging Face in offline mode.

    The model is built with ``pretrained=None`` and loaded from the SHA-256
    checked safetensors, so nothing should be fetched; this keeps it that way
    (as ``_hub_refused`` does for MiDaS in ``flightrisk/drone/midas_model.py``).
    """
    import importlib

    saved_env = {k: os.environ.get(k) for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    saved: list[tuple[Any, str, Any]] = []

    def refuse(where: str) -> Callable[..., Any]:
        def refused(*args: Any, **kwargs: Any) -> Any:
            raise VerificationFailed(
                f"refused {where}({args[0] if args else ''!r}) while building the embedder: "
                "only the SHA-256 checked weights in the downloads folder are used"
            )

        return refused

    try:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        for module_name, attr in _DOWNLOADERS:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            if hasattr(module, attr):
                saved.append((module, attr, getattr(module, attr)))
                setattr(module, attr, refuse(f"{module_name}.{attr}"))
        yield
    finally:
        for module, attr, fn in reversed(saved):
            setattr(module, attr, fn)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def load_clip(downloads_dir: Path):
    """(open_clip model, its evaluation preprocess) with the pinned, checked weights."""
    import open_clip
    from safetensors.torch import load as load_safetensors

    data = read_verified(CLIP_WEIGHTS, downloads_dir)
    state = load_safetensors(data)
    logging.disable(logging.WARNING)  # open_clip warns that the model starts untrained; it is loaded next
    try:
        with network_refused():
            model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-16", pretrained=None)
    finally:
        logging.disable(logging.NOTSET)
    model.load_state_dict(state, strict=True)
    cfg = open_clip.get_pretrained_cfg("ViT-B-16", "laion2b_s34b_b88k")
    if (tuple(cfg["mean"]), tuple(cfg["std"]), cfg["interpolation"], cfg["resize_mode"]) != (
        mp.CLIP_MEAN, mp.CLIP_STD, "bicubic", "shortest"
    ):
        raise VerificationFailed(f"open_clip's preprocessing for laion2b_s34b_b88k changed: {cfg}")
    return model.eval(), preprocess


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _metadata(download: Download, role: str, variant: str) -> dict[str, str]:
    if role == "detector":
        preprocess = (
            "640x640, aspect kept, bilinear resize, image at top-left, padding 114; "
            "BGR, float 0..255, no normalisation; output [1,8400,85] = cx,cy,w,h,obj,80 class "
            "scores in input pixels; score = obj*class"
        )
        name = "YOLOX-S (COCO) release 0.1.1rc0"
    else:
        preprocess = (
            "shortest side to 224 (bicubic), centre crop 224x224, RGB /255, "
            f"mean {list(mp.CLIP_MEAN)}, std {list(mp.CLIP_STD)}; output [1,512] L2-normalised"
        )
        name = mp.CLIP_MODEL_ID
    return {
        "flightrisk.model": name,
        "flightrisk.role": role,
        "flightrisk.variant": variant,
        "flightrisk.weights_url": download.url,
        "flightrisk.weights_sha256": download.sha256,
        "flightrisk.licence": download.licence,
        "flightrisk.preprocess": preprocess,
        "flightrisk.docs": "docs/mobile-models.md",
    }


@contextlib.contextmanager
def mha_fastpath_disabled():
    """Trace nn.MultiheadAttention's plain path: its fused fast path has no ONNX or Core ML mapping."""
    import torch

    before = torch.backends.mha.get_fastpath_enabled()
    torch.backends.mha.set_fastpath_enabled(False)
    try:
        yield
    finally:
        torch.backends.mha.set_fastpath_enabled(before)


def export_onnx(module, example, path: Path, input_name: str, output_name: str, metadata: dict[str, str]) -> None:
    import onnx
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    with torch.no_grad(), mha_fastpath_disabled():
        torch.onnx.export(
            module,
            (example,),
            str(path),
            opset_version=ONNX_OPSET,
            input_names=[input_name],
            output_names=[output_name],
            dynamic_axes=None,
            do_constant_folding=True,
            dynamo=False,
        )
    model = onnx.load(str(path))
    onnx.helper.set_model_props(model, metadata)
    onnx.checker.check_model(model)
    onnx.save(model, str(path))


def onnx_to_fp16(src: Path, dst: Path, metadata: dict[str, str]) -> None:
    """fp16 weights and activations, fp32 input and output (``keep_io_types``)."""
    import onnx
    from onnxruntime.transformers.float16 import convert_float_to_float16
    from onnxruntime.transformers.onnx_model import OnnxModel

    converted = OnnxModel(convert_float_to_float16(onnx.load(str(src)), keep_io_types=True))
    # The converter appends the input Cast after its consumers; ONNX requires topological order.
    converted.topological_sort()
    model = converted.model
    onnx.helper.set_model_props(model, metadata)
    onnx.checker.check_model(model)
    onnx.save(model, str(dst))


def onnx_to_dynamic_int8(src: Path, dst: Path, metadata: dict[str, str],
                         exclude: tuple[str, ...] = DYNAMIC_INT8_EXCLUDE) -> None:
    """Dynamic int8: MatMul weights int8 per channel, activations quantised at run time.

    Only MatMuls with a constant weight are quantised (``MatMulConstBOnly``),
    never the attention's activation-by-activation products, and ``exclude``
    stays in fp32.
    """
    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic

    names = {n.name for n in onnx.load(str(src), load_external_data=False).graph.node}
    absent = [n for n in exclude if n not in names]
    if absent:
        raise VerificationFailed(f"nodes to keep in fp32 are not in {src}: {absent}")
    quantize_dynamic(
        str(src), str(dst), op_types_to_quantize=["MatMul"], per_channel=True, weight_type=QuantType.QInt8,
        nodes_to_exclude=list(exclude), extra_options={"MatMulConstBOnly": True},
    )
    model = onnx.load(str(dst))
    onnx.helper.set_model_props(model, metadata)
    onnx.checker.check_model(model)
    onnx.save(model, str(dst))


def onnx_to_weight_only_int8(src: Path, dst: Path, metadata: dict[str, str]) -> None:
    """Int8 weights, fp32 arithmetic: each constant MatMul/Gemm weight becomes int8 + DequantizeLinear.

    Weights are quantised symmetrically per output channel (scale = max|w| / 127,
    zero point 0). The file is about a quarter of fp32. The ``DequantizeLinear``
    nodes stay in the graph: ONNX Runtime does not fold them into constants
    (its constant folding leaves ``DequantizeLinear`` alone), so they turn the
    int8 weights back into fp32 on every inference, and the MatMuls run in
    fp32. Activations are never quantised.
    """
    import numpy as np
    import onnx
    from onnx import helper, numpy_helper

    model = onnx.load(str(src))
    inits = {i.name: i for i in model.graph.initializer}
    uses: dict[str, int] = {}
    for node in model.graph.node:
        for name in node.input:
            uses[name] = uses.get(name, 0) + 1
    dq_nodes = []
    for node in model.graph.node:
        if node.op_type not in ("MatMul", "Gemm") or len(node.input) < 2:
            continue
        wname = node.input[1]
        if wname not in inits or uses.get(wname) != 1:
            continue
        w = numpy_helper.to_array(inits[wname]).astype(np.float32)
        if w.ndim != 2:
            continue
        trans_b = node.op_type == "Gemm" and any(a.name == "transB" and a.i == 1 for a in node.attribute)
        axis = 0 if trans_b else 1  # the output-channel axis of the weight
        amax = np.abs(w).max(axis=1 - axis)
        scale = np.where(amax > 0, amax / 127.0, 1.0).astype(np.float32)
        q = np.clip(np.round(w / np.expand_dims(scale, 1 - axis)), -127, 127).astype(np.int8)
        model.graph.initializer.remove(inits[wname])
        model.graph.initializer.extend([
            numpy_helper.from_array(q, f"{wname}_int8"),
            numpy_helper.from_array(scale, f"{wname}_scale"),
            numpy_helper.from_array(np.zeros_like(scale, dtype=np.int8), f"{wname}_zero_point"),
        ])
        dq_nodes.append(helper.make_node(
            "DequantizeLinear", [f"{wname}_int8", f"{wname}_scale", f"{wname}_zero_point"], [wname],
            name=f"{wname}_dequantize", axis=axis,
        ))
    if not dq_nodes:
        raise VerificationFailed(f"{src} has no constant MatMul or Gemm weights to quantise")
    nodes = dq_nodes + list(model.graph.node)  # DequantizeLinear reads only initializers
    del model.graph.node[:]
    model.graph.node.extend(nodes)
    onnx.helper.set_model_props(model, metadata)
    onnx.checker.check_model(model)
    onnx.save(model, str(dst))


def _coreml_finish(mlmodel, download: Download, role: str, variant: str, description: str, output_key: str):
    meta = _metadata(download, role, variant)
    meta["flightrisk.coreml_compute_units"] = COREML_COMPUTE_UNITS[output_key]
    mlmodel.author = (
        "Megvii Inc. (YOLOX); converted by FlightRisk" if role == "detector" else "LAION / open_clip; converted by FlightRisk"
    )
    mlmodel.license = download.licence
    mlmodel.short_description = description
    mlmodel.version = variant
    mlmodel.user_defined_metadata.update(meta)
    return mlmodel


def convert_yolox(downloads_dir: Path, out_dir: Path, targets: str, log: Callable[[str], None]) -> None:
    convert_yolox_model(load_yolox_s(downloads_dir), out_dir, targets, log)


def yolox_export_module(model, size: int = mp.YOLOX_INPUT_SIZE):
    """``model`` with its box decoding done against constant grids, for a fixed ``size`` input.

    Upstream decodes with grids built from the feature map shapes at run time
    (``decode_in_inference``). Traced, those shapes become size and int ops that
    Core ML's converter cannot fold, so this module runs a copy of the network
    without decoding and applies the same decoding with the grid cells and
    strides of ``mobile_preprocess.yolox_anchor_grid``. ``verify`` compares its
    output with upstream's own decoding.
    """
    import copy

    import torch

    class DecodedYOLOX(torch.nn.Module):
        def __init__(self, net):
            super().__init__()
            self.net = copy.deepcopy(net).eval()
            self.net.head.decode_in_inference = False
            cells, strides = mp.yolox_anchor_grid(size)
            self.register_buffer("grid", torch.from_numpy(cells).float().unsqueeze(0))
            self.register_buffer("stride", torch.from_numpy(strides).float().reshape(1, -1, 1))

        def forward(self, x):
            out = self.net(x)
            return torch.cat(
                [(out[..., 0:2] + self.grid) * self.stride, torch.exp(out[..., 2:4]) * self.stride, out[..., 4:]],
                dim=-1,
            )

    return DecodedYOLOX(model).eval()


def convert_yolox_model(model, out_dir: Path, targets: str, log: Callable[[str], None]) -> None:
    """Write the detector outputs of ``model`` (YOLOX-S in eval mode) under ``out_dir``."""
    import numpy as np
    import torch

    model = yolox_export_module(model)
    example = torch.zeros(1, 3, mp.YOLOX_INPUT_SIZE, mp.YOLOX_INPUT_SIZE)
    if targets in ("all", "android"):
        fp32 = out_dir / "android" / "yolox_s.onnx"
        log(f"convert: {fp32}")
        export_onnx(model, example, fp32, DETECTOR_INPUT, DETECTOR_OUTPUT, _metadata(YOLOX_S_WEIGHTS, "detector", "fp32"))
        fp16 = out_dir / "android" / "yolox_s_fp16.onnx"
        log(f"convert: {fp16}")
        onnx_to_fp16(fp32, fp16, _metadata(YOLOX_S_WEIGHTS, "detector", "fp16"))
    if targets in ("all", "ios"):
        import coremltools as ct

        with torch.no_grad():
            traced = torch.jit.trace(model, example)
        for variant, filename, precision in (("fp32", "YOLOXs.mlpackage", ct.precision.FLOAT32),
                                             ("fp16", "YOLOXsFP16.mlpackage", ct.precision.FLOAT16)):
            dst = out_dir / "ios" / filename
            log(f"convert: {dst}")
            mlmodel = ct.convert(
                traced,
                convert_to="mlprogram",
                inputs=[
                    ct.ImageType(
                        name=DETECTOR_INPUT,
                        shape=example.shape,
                        color_layout=ct.colorlayout.BGR,
                        scale=1.0,
                        bias=[0.0, 0.0, 0.0],
                    )
                ],
                outputs=[ct.TensorType(name=DETECTOR_OUTPUT, dtype=np.float32)],
                compute_precision=precision,
                minimum_deployment_target=ct.target.iOS17,
            )
            units = COREML_COMPUTE_UNITS[f"ios/{filename}"]
            mlmodel = _coreml_finish(
                mlmodel, YOLOX_S_WEIGHTS, "detector", variant,
                f"YOLOX-S COCO detector ({variant}). Input: 640x640 BGR image, letterboxed at the top-left with 114. "
                "Output: [1,8400,85] cx,cy,w,h,objectness,80 class scores. "
                + ("Load with computeUnits = .cpuAndGPU." if units == "cpuAndGPU" else "Any compute units."),
                f"ios/{filename}",
            )
            mlmodel.input_description[DETECTOR_INPUT] = "640x640 letterboxed frame (top-left, padding 114), BGR, 0-255"
            mlmodel.output_description[DETECTOR_OUTPUT] = "[1,8400,85]: cx, cy, w, h (input pixels), objectness, 80 class scores"
            _save_mlpackage(mlmodel, dst)


def convert_clip(downloads_dir: Path, out_dir: Path, targets: str, log: Callable[[str], None]) -> None:
    model, _ = load_clip(downloads_dir)
    convert_clip_model(model.visual, out_dir, targets, log)


def convert_clip_model(visual, out_dir: Path, targets: str, log: Callable[[str], None],
                       int8_exclude: tuple[str, ...] = DYNAMIC_INT8_EXCLUDE) -> None:
    """Write the embedder outputs of an open_clip image tower ``visual`` under ``out_dir``."""
    import numpy as np
    import torch

    embedder = clip_embedder_module(visual)
    example = torch.zeros(1, 3, mp.CLIP_INPUT_SIZE, mp.CLIP_INPUT_SIZE)
    if targets in ("all", "android"):
        fp32 = out_dir / "android" / "clip_vit_b16_laion2b.onnx"
        log(f"convert: {fp32}")
        export_onnx(embedder, example, fp32, EMBEDDER_INPUT, EMBEDDER_OUTPUT, _metadata(CLIP_WEIGHTS, "embedder", "fp32"))
        int8 = out_dir / "android" / "clip_vit_b16_laion2b_int8.onnx"
        log(f"convert: {int8}")
        onnx_to_dynamic_int8(fp32, int8, _metadata(CLIP_WEIGHTS, "embedder", "int8-dynamic"), int8_exclude)
        w8 = out_dir / "android" / "clip_vit_b16_laion2b_w8.onnx"
        log(f"convert: {w8}")
        onnx_to_weight_only_int8(fp32, w8, _metadata(CLIP_WEIGHTS, "embedder", "int8-weights"))
    if targets in ("all", "ios"):
        import coremltools as ct
        from coremltools.optimize.coreml import (
            OpPalettizerConfig,
            OptimizationConfig,
            palettize_weights,
        )

        with torch.no_grad(), mha_fastpath_disabled():
            traced = torch.jit.trace(embedder, example)
        mlmodel = ct.convert(
            traced,
            convert_to="mlprogram",
            inputs=[ct.TensorType(name=EMBEDDER_INPUT, shape=example.shape, dtype=np.float32)],
            outputs=[ct.TensorType(name=EMBEDDER_OUTPUT, dtype=np.float32)],
            compute_precision=ct.precision.FLOAT16,
            minimum_deployment_target=ct.target.iOS17,
        )
        description = (
            f"{mp.CLIP_MODEL_ID} image tower. Input: [1,3,224,224] float32, RGB, CLIP-normalised. "
            "Output: [1,512] L2-normalised appearance embedding."
        )
        fp16 = out_dir / "ios" / "CLIPViTB16LAION.mlpackage"
        log(f"convert: {fp16}")
        mlmodel = _coreml_finish(mlmodel, CLIP_WEIGHTS, "embedder", "fp16", description,
                                 "ios/CLIPViTB16LAION.mlpackage")
        mlmodel.input_description[EMBEDDER_INPUT] = "[1,3,224,224] RGB, (v/255 - mean)/std with the CLIP mean and std"
        mlmodel.output_description[EMBEDDER_OUTPUT] = "[1,512] L2-normalised embedding"
        _save_mlpackage(mlmodel, fp16)
        pal = out_dir / "ios" / "CLIPViTB16LAION8bit.mlpackage"
        log(f"convert: {pal}")
        config = OptimizationConfig(
            global_config=OpPalettizerConfig(mode="kmeans", nbits=8, weight_threshold=PALETTIZE_MIN_WEIGHTS)
        )
        palettized = palettize_weights(mlmodel, config)
        palettized = _coreml_finish(palettized, CLIP_WEIGHTS, "embedder", "palettized8", description,
                                    "ios/CLIPViTB16LAION8bit.mlpackage")
        palettized.input_description[EMBEDDER_INPUT] = mlmodel.input_description[EMBEDDER_INPUT]
        palettized.output_description[EMBEDDER_OUTPUT] = mlmodel.output_description[EMBEDDER_OUTPUT]
        _save_mlpackage(palettized, pal)


def _save_mlpackage(mlmodel, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    mlmodel.save(str(dst))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _fixture_images_bgr() -> list[tuple[str, Any]]:
    import cv2

    images = []
    for name in FIXTURE_IMAGES:
        path = FIXTURE_DIR / name
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise VerificationFailed(f"parity fixture {path} is missing or unreadable")
        images.append((f"fixture:{name}", img))
    return images


def _sample_image_bgr(downloads_dir: Path):
    import cv2
    import numpy as np

    data = read_verified(YOLOX_SAMPLE_IMAGE, downloads_dir)
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise VerificationFailed("the YOLOX sample image could not be decoded")
    return img


def _synthetic_images_bgr() -> list[tuple[str, Any]]:
    """Deterministic synthetic pictures: seeded noise and seeded coloured rectangles."""
    import cv2
    import numpy as np

    rng = np.random.default_rng(20261122)
    noise = rng.integers(0, 256, size=(480, 640, 3), dtype=np.uint8)
    blocks = np.full((360, 640, 3), 90, dtype=np.uint8)
    for _ in range(24):
        x0, y0 = int(rng.integers(0, 600)), int(rng.integers(0, 330))
        x1, y1 = x0 + int(rng.integers(8, 120)), y0 + int(rng.integers(8, 160))
        color = tuple(int(c) for c in rng.integers(0, 256, size=3))
        cv2.rectangle(blocks, (x0, y0), (x1, y1), color, thickness=-1)
    gradient = np.zeros((300, 200, 3), dtype=np.uint8)
    gradient[..., 0] = np.linspace(0, 255, 200, dtype=np.uint8)[None, :]
    gradient[..., 1] = np.linspace(0, 255, 300, dtype=np.uint8)[:, None]
    gradient[..., 2] = 128
    return [("synthetic:noise", noise), ("synthetic:blocks", blocks), ("synthetic:gradient", gradient)]


def _match_detections(ref: list[dict], got: list[dict], score_tol: float) -> dict[str, Any]:
    """Greedy same-class IoU matching of two detection lists."""
    import numpy as np

    used: set[int] = set()
    ious: list[float] = []
    missing = 0
    for r in ref:
        best, best_iou = None, 0.0
        for j, g in enumerate(got):
            if j in used or g["class"] != r["class"]:
                continue
            iou = float(mp.box_iou(np.array(r["box"]), np.array(g["box"]))[0, 0])
            if iou > best_iou:
                best, best_iou = j, iou
        if best is not None and best_iou > 0.5:
            used.add(best)
            ious.append(best_iou)
        elif r["score"] >= mp.YOLOX_SCORE_THRESHOLD + score_tol:
            missing += 1
    extra = sum(
        1 for j, g in enumerate(got) if j not in used and g["score"] >= mp.YOLOX_SCORE_THRESHOLD + score_tol
    )
    return {"matched": len(ious), "min_iou": min(ious) if ious else None, "missing": missing, "extra": extra}


class _Runner:
    """Runs one output on preprocessed inputs (ONNX Runtime on the CPU, or Core ML on ``compute_units``)."""

    def __init__(self, path: Path, compute_units: str | None = None):
        self.path = path
        if path.suffix == ".onnx":
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            self.session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
            self.kind = "onnx"
        else:
            import coremltools as ct

            unit = getattr(ct.ComputeUnit, compute_units or "ALL")
            self.model = ct.models.MLModel(str(path), compute_units=unit)
            self.kind = "coreml"

    def detector(self, canvas_bgr) -> Any:
        import numpy as np

        if self.kind == "onnx":
            out = self.session.run([DETECTOR_OUTPUT], {DETECTOR_INPUT: mp.yolox_input_tensor(canvas_bgr)})[0]
        else:
            from PIL import Image

            rgb = np.ascontiguousarray(canvas_bgr[:, :, ::-1])
            out = self.model.predict({DETECTOR_INPUT: Image.fromarray(rgb)})[DETECTOR_OUTPUT]
        return np.asarray(out, dtype=np.float32).reshape(-1, mp.YOLOX_ROW_LENGTH)

    def embedder(self, tensor) -> Any:
        import numpy as np

        if self.kind == "onnx":
            out = self.session.run([EMBEDDER_OUTPUT], {EMBEDDER_INPUT: tensor})[0]
        else:
            out = self.model.predict({EMBEDDER_INPUT: tensor})[EMBEDDER_OUTPUT]
        return np.asarray(out, dtype=np.float64).reshape(-1)


def _runs(output: Output) -> list[tuple[str, str, bool]]:
    """(label, compute units, gates the result) for every way ``output`` is run."""
    if output.platform == "android":
        return [("onnxruntime-cpu", "", True)]
    gates = COREML_GATE_UNITS[output.key]
    return [(f"coreml-{u}", u, u in gates) for u in COREML_ALL_UNITS]


def check_onnx_interface(path: Path, input_name: str, input_shape: list[int], output_name: str, output_shape: list[int]) -> dict:
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    problems = []
    opsets = {imp.domain or "ai.onnx": imp.version for imp in model.opset_import}
    if opsets.get("ai.onnx") != ONNX_OPSET:
        problems.append(f"default opset {opsets.get('ai.onnx')} is not {ONNX_OPSET}")
    if model.ir_version > MAX_ONNX_IR_VERSION:
        problems.append(f"IR version {model.ir_version} is above {MAX_ONNX_IR_VERSION}")

    def _io(values):
        return {
            v.name: ([d.dim_value if d.HasField("dim_value") else d.dim_param for d in v.type.tensor_type.shape.dim],
                     v.type.tensor_type.elem_type)
            for v in values
        }

    inputs = {k: v for k, v in _io(model.graph.input).items() if k not in {i.name for i in model.graph.initializer}}
    outputs = _io(model.graph.output)
    if inputs != {input_name: (input_shape, onnx.TensorProto.FLOAT)}:
        problems.append(f"inputs are {inputs}, expected {input_name} float32 {input_shape}")
    if outputs != {output_name: (output_shape, onnx.TensorProto.FLOAT)}:
        problems.append(f"outputs are {outputs}, expected {output_name} float32 {output_shape}")
    return {"ir_version": model.ir_version, "opsets": opsets, "problems": problems}


def check_coreml_interface(path: Path, expect_image_input: bool, input_name: str, input_shape: list[int],
                           output_name: str) -> dict:
    """Problems with a Core ML package's type, version and interface (the output shape is checked at run time)."""
    import coremltools as ct

    spec = ct.utils.load_spec(str(path))
    problems = []
    if spec.WhichOneof("Type") != "mlProgram":
        problems.append(f"model type is {spec.WhichOneof('Type')}, not an ML program")
    if spec.specificationVersion < COREML_MIN_SPEC_VERSION:
        problems.append(f"specification version {spec.specificationVersion} is below {COREML_MIN_SPEC_VERSION} (iOS 17)")
    ins = {i.name: i for i in spec.description.input}
    outs = {o.name: o for o in spec.description.output}
    if set(ins) != {input_name}:
        problems.append(f"inputs are {sorted(ins)}, expected [{input_name}]")
    else:
        t = ins[input_name].type
        if expect_image_input:
            img = t.imageType
            if t.WhichOneof("Type") != "imageType" or [img.height, img.width] != input_shape[-2:]:
                problems.append(f"input {input_name} is not a {input_shape[-2]}x{input_shape[-1]} image")
            elif ct.proto.FeatureTypes_pb2.ImageFeatureType.ColorSpace.Name(img.colorSpace) != "BGR":
                problems.append(f"input {input_name} colour space is not BGR")
        elif t.WhichOneof("Type") != "multiArrayType" or list(t.multiArrayType.shape) != input_shape:
            problems.append(f"input {input_name} is not a multi-array of shape {input_shape}")
    if set(outs) != {output_name}:
        problems.append(f"outputs are {sorted(outs)}, expected [{output_name}]")
    return {"specification_version": spec.specificationVersion, "problems": problems}


def _detector_inputs(downloads_dir: Path) -> list[tuple[str, Any]]:
    """BGR pictures for the detector parity: fixtures, the sample at four scales, synthetic pictures.

    The smaller copies of the sample put its objects at the sizes a person has
    in drone footage, where precision loss shows first.
    """
    import cv2

    sample = _sample_image_bgr(downloads_dir)
    scaled = [("upstream:yolox_dog.jpg", sample)]
    for factor in (0.5, 0.25, 0.12):
        small = cv2.resize(sample, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
        scaled.append((f"upstream:yolox_dog.jpg@{factor}", small))
    return _fixture_images_bgr() + scaled + _synthetic_images_bgr()


def _person_photo_bgr(downloads_dir: Path):
    import cv2
    import numpy as np

    data = read_verified(PERSON_PHOTO, downloads_dir)
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise VerificationFailed("the pinned person photo could not be decoded")
    return img


def silhouette_scene(seed: int = 20261123, size: tuple[int, int] = (480, 800),
                     heights: tuple[int, ...] = (16, 24, 32, 48, 72, 96, 160)):
    """A synthetic BGR frame of person-like figures on a grass-like texture, and their boxes.

    No photograph is used: each figure is a head, a torso, two arms and two legs
    in seeded colours, standing on seeded, blurred noise. Returns the frame and
    a list of ``(x1, y1, x2, y2)`` boxes (frame pixels), one per figure, in the
    order of ``heights``.
    """
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    h, w = size
    noise = rng.integers(0, 256, size=(h // 4, w // 4, 3), dtype=np.uint8)
    texture = cv2.resize(noise, (w, h), interpolation=cv2.INTER_CUBIC)
    texture = cv2.GaussianBlur(texture, (0, 0), 3)
    base = np.array([60, 120, 90], dtype=np.float32)  # grass green, in BGR
    frame = np.clip(base + (texture.astype(np.float32) - 128) * 0.45, 0, 255).astype(np.uint8)
    for _ in range(12):  # paths and patches of bare ground
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        cv2.ellipse(frame, (x0, y0), (int(rng.integers(20, 90)), int(rng.integers(8, 40))), float(rng.integers(0, 180)),
                    0, 360, (70, 110, 150), -1)
    boxes = []
    x = 20
    for fh in heights:
        fw = max(4, round(fh * 0.4))
        y_top = int(rng.integers(10, h - fh - 10))
        if x + fw >= w - 10:
            x = 20
        cx = x + fw // 2
        skin = tuple(int(c) for c in rng.integers(60, 200, size=3))
        shirt = tuple(int(c) for c in rng.integers(0, 256, size=3))
        trousers = tuple(int(c) for c in rng.integers(0, 140, size=3))
        head_r = max(1, round(fh * 0.065))
        thick = max(1, round(fh * 0.07))
        neck = y_top + 2 * head_r
        hip = y_top + int(fh * 0.52)
        cv2.circle(frame, (cx, y_top + head_r), head_r, skin, -1)
        cv2.rectangle(frame, (cx - int(fh * 0.11), neck), (cx + int(fh * 0.11), hip), shirt, -1)
        cv2.line(frame, (cx - int(fh * 0.11), neck + thick), (cx - int(fh * 0.18), y_top + int(fh * 0.5)), shirt, thick)
        cv2.line(frame, (cx + int(fh * 0.11), neck + thick), (cx + int(fh * 0.18), y_top + int(fh * 0.5)), shirt, thick)
        cv2.line(frame, (cx - int(fh * 0.06), hip), (cx - int(fh * 0.09), y_top + fh - 1), trousers, thick)
        cv2.line(frame, (cx + int(fh * 0.06), hip), (cx + int(fh * 0.09), y_top + fh - 1), trousers, thick)
        boxes.append((cx - fw // 2, y_top, cx - fw // 2 + fw, y_top + fh))
        x += fw + int(rng.integers(20, 60))
    return frame, boxes


def _person_detector_inputs(photo, persons: list[dict]) -> list[tuple[str, Any]]:
    """The photo, its people at native size, and its people scaled to 48, 32, 24 and 16 pixels tall.

    Each scaled copy is a 640 x 640 frame (so the letterbox does not resize it
    again) cut around the people from the photo scaled so that their median
    height is that many pixels, padded with 114 where the photo ends.
    """
    import cv2
    import numpy as np

    out = [("person:photo", photo)]
    if not persons:
        return out
    boxes = np.array([d["box"] for d in persons], dtype=np.float64)
    median_h = float(np.median(boxes[:, 3] - boxes[:, 1]))
    centre = ((boxes[:, 0] + boxes[:, 2]).mean() / 2, (boxes[:, 1] + boxes[:, 3]).mean() / 2)

    def window(img, cx, cy, size=mp.YOLOX_INPUT_SIZE):
        frame = np.full((size, size, 3), mp.YOLOX_PAD_VALUE, dtype=np.uint8)
        x0 = round(min(max(cx - size / 2, 0), max(img.shape[1] - size, 0)))
        y0 = round(min(max(cy - size / 2, 0), max(img.shape[0] - size, 0)))
        part = img[y0:y0 + size, x0:x0 + size]
        frame[:part.shape[0], :part.shape[1]] = part
        return frame

    out.append(("person:native", window(photo, *centre)))
    for target in sorted(SMALL_CROP_HEIGHTS, reverse=True):
        f = target / median_h
        scaled = cv2.resize(photo, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        out.append((f"person:h{target}", window(scaled, centre[0] * f, centre[1] * f)))
    return out


def _small_copies(crop) -> list[tuple[str, Any]]:
    """``crop`` at native size and scaled down to each of SMALL_CROP_HEIGHTS (aspect kept, area filter)."""
    import cv2

    out = [("native", crop)]
    h, w = crop.shape[:2]
    for target in SMALL_CROP_HEIGHTS:
        if target >= h:
            continue
        tw = max(1, round(w * target / h))
        out.append((f"{target}px", cv2.resize(crop, (tw, target), interpolation=cv2.INTER_AREA)))
    return out


def _person_crops_rgb(frame, persons: list[dict]) -> list[tuple[str, Any]]:
    """RGB crops of a BGR frame's people (the contract's crop rule), each at native size and small."""
    import numpy as np

    out: list[tuple[str, Any]] = []
    if frame is None:
        return out
    for i, d in enumerate(persons):
        crop = mp.crop_box(frame, d["box"])
        if crop.shape[0] < 2 or crop.shape[1] < 2:
            continue
        rgb = np.ascontiguousarray(crop[:, :, ::-1])
        out += [(f"person:crop{i}@{label}", img) for label, img in _small_copies(rgb)]
    return out


def _silhouette_crops_rgb() -> list[tuple[str, Any]]:
    import numpy as np

    frame, boxes = silhouette_scene()
    out = []
    for box in boxes:
        crop = np.ascontiguousarray(mp.crop_box(frame, box)[:, :, ::-1])
        out.append((f"silhouette:{box[3] - box[1]}px", crop))
    return out


def _group_tolerance(key: str, group: str) -> dict[str, float]:
    """The tolerance an output is measured against on one group of inputs."""
    if group == "person" and key in PERSON_TOLERANCES:
        return PERSON_TOLERANCES[key]
    return TOLERANCES[key]


def _finite_max(current: float, value: float) -> float:
    """``max`` that keeps a NaN (Python's ``max(0.0, nan)`` would drop it)."""
    import numpy as np

    return float(np.maximum(current, value))


def _finite_min(current: float, value: float) -> float:
    import numpy as np

    return float(np.minimum(current, value))


def _detector_metrics(runner: _Runner, canvases, refs, tol: dict[str, float]) -> dict[str, Any]:
    """Parity of one way of running a detector against PyTorch on ``canvases``.

    Any output (or reference) with a value that is not finite fails, and the
    worst values are combined with ``numpy.maximum`` and ``numpy.minimum`` so a
    NaN that gets through still fails the tolerance.
    """
    import numpy as np

    score_max = box_max = 0.0
    min_iou = 1.0
    missing = extra = matched = 0
    problems: list[str] = []
    per_input = {}
    for name, canvas, ratio in canvases:
        got = runner.detector(canvas)
        ref = refs[name]
        if got.shape != ref.shape:
            problems.append(f"output shape {list(got.shape)} on {name}, expected {list(ref.shape)}")
            continue
        if not np.isfinite(got).all():
            problems.append(f"output on {name} has {int((~np.isfinite(got)).sum())} values that are not finite")
            continue
        if not np.isfinite(ref).all():
            problems.append(f"PyTorch's output on {name} is not finite")
            continue
        s_ref, s_got = mp.yolox_scores(ref), mp.yolox_scores(got)
        s_diff = float(np.max(np.abs(s_ref - s_got)))
        rows = s_ref.max(axis=1) >= BOX_ROWS_MIN_SCORE
        b_diff = float(np.max(np.abs(ref[rows, :4] - got[rows, :4]))) if rows.any() else 0.0
        m = _match_detections(mp.yolox_postprocess(ref, ratio), mp.yolox_postprocess(got, ratio), tol["score_max_abs"])
        per_input[name] = {"score_max_abs": s_diff, "box_max_abs_px": b_diff, "rows_compared": int(rows.sum()), **m}
        score_max, box_max = _finite_max(score_max, s_diff), _finite_max(box_max, b_diff)
        if m["min_iou"] is not None:
            min_iou = _finite_min(min_iou, m["min_iou"])
        missing, extra, matched = missing + m["missing"], extra + m["extra"], matched + m["matched"]
    within = bool(
        not problems
        and score_max <= tol["score_max_abs"]
        and box_max <= tol["box_max_abs_px"]
        and min_iou >= tol["match_iou_min"]
        and missing == 0
        and extra == 0
    )
    return {
        "within_tolerance": within, "problems": problems,
        "score_max_abs": score_max, "box_max_abs_px": box_max, "match_iou_min": min_iou,
        "matched_detections": matched, "missing_detections": missing, "extra_detections": extra,
        "per_input": per_input,
    }


def _sample_sanity_problem(sanity: dict[str, float]) -> str | None:
    """Why PyTorch's scores for the demo picture's dog and bicycle fail the 0.8 check (NaN fails), or None.

    Each score is compared on its own: ``min`` would skip a NaN that is not first.
    """
    if not all(score >= 0.8 for score in sanity.values()):
        return f"PyTorch YOLOX-S does not find the sample's dog and bicycle ({sanity}); preprocessing or weights are wrong"
    return None


def detector_reference(downloads_dir: Path) -> Callable[[Any], Any]:
    """PyTorch YOLOX-S with upstream's own decoding: a letterboxed BGR canvas -> ``[8400, 85]``."""
    import torch

    model = load_yolox_s(downloads_dir)

    def run(canvas_bgr):
        with torch.no_grad():
            return model(torch.from_numpy(mp.yolox_input_tensor(canvas_bgr)))[0].numpy()

    return run


def _worst(runs: dict[str, dict[str, Any]], labels: list[str], keys_max: tuple[str, ...],
           keys_min: tuple[str, ...]) -> dict[str, Any]:
    """The worst of each metric over ``labels`` (the gated runs), keeping NaN."""
    import numpy as np

    out: dict[str, Any] = {}
    for k in keys_max:
        vals = [runs[l][k] for l in labels if k in runs[l]]
        out[k] = float(np.max(vals)) if vals else None
    for k in keys_min:
        vals = [runs[l][k] for l in labels if k in runs[l]]
        out[k] = float(np.min(vals)) if vals else None
    return out


_DET_MAX = ("score_max_abs", "box_max_abs_px", "missing_detections", "extra_detections")
_DET_MIN = ("match_iou_min", "matched_detections")
_EMB_MAX = ("similarity_max_abs", "similarity_near_threshold_max_abs")
_EMB_MIN = ("cosine_min", "cosine_mean", "pairs_near_threshold")


def verify_detector(outputs: list[Output], out_dir: Path, downloads_dir: Path, log: Callable[[str], None],
                    reference: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    reference = reference or detector_reference(downloads_dir)
    sample = _sample_image_bgr(downloads_dir)
    sample_canvas, sample_ratio = mp.yolox_letterbox(sample)
    sample_dets = mp.yolox_postprocess(reference(sample_canvas), sample_ratio)
    # The pinned weights and the 0..255, unnormalised contract find the dog and the bicycle.
    # This does not check the channel order (the picture's objects are found as RGB too);
    # the golden fixtures pin the channel order.
    sanity = {
        "dog": max((d["score"] for d in sample_dets if d["class"] == 16), default=0.0),
        "bicycle": max((d["score"] for d in sample_dets if d["class"] == 1), default=0.0),
    }
    photo = _person_photo_bgr(downloads_dir)
    photo_canvas, photo_ratio = mp.yolox_letterbox(photo)
    persons = [d for d in mp.yolox_postprocess(reference(photo_canvas), photo_ratio, classes=(mp.COCO_PERSON,))
               if d["score"] >= PERSON_CROP_MIN_SCORE]
    scene, _ = silhouette_scene()
    groups = {
        "general": _detector_inputs(downloads_dir),
        "person": _person_detector_inputs(photo, persons),
        "silhouette": [("silhouette:scene", scene)],
    }
    canvases = {g: [(name, *mp.yolox_letterbox(img)) for name, img in imgs] for g, imgs in groups.items()}
    refs = {name: reference(canvas) for cs in canvases.values() for name, canvas, _ in cs}
    person_counts = {
        name: sum(1 for d in mp.yolox_postprocess(refs[name], ratio, classes=(mp.COCO_PERSON,)))
        for name, _, ratio in canvases["person"] + canvases["silhouette"]
    }
    # The embedder's person crops come from the native-resolution window (people at
    # their full size), cut where PyTorch finds them, as the app would cut them.
    native = dict(groups["person"]).get("person:native")
    crop_persons = [] if native is None else [
        d for d in mp.yolox_postprocess(refs["person:native"], 1.0, classes=(mp.COCO_PERSON,))
        if d["score"] >= PERSON_CROP_MIN_SCORE
    ]
    results: dict[str, Any] = {
        "pytorch_sample_scores": sanity,
        "pytorch_photo_persons": [round(d["score"], 4) for d in persons],
        "pytorch_photo_person_heights_px": [round(d["box"][3] - d["box"][1], 1) for d in persons],
        "pytorch_crop_persons": [{"score": round(d["score"], 4), "box": [round(v, 1) for v in d["box"]]}
                                 for d in crop_persons],
        "pytorch_person_detections": person_counts,
        "inputs": {g: [n for n, *_ in cs] for g, cs in canvases.items()},
        "outputs": {},
    }
    problems: list[str] = []
    sample_problem = _sample_sanity_problem(sanity)
    if sample_problem:
        problems.append(sample_problem)
    if len(persons) < 3 or len(crop_persons) < 4:
        problems.append(f"PyTorch YOLOX-S finds {len(persons)} people in the whole person photo and {len(crop_persons)} "
                        f"at native size (score {PERSON_CROP_MIN_SCORE} or more), expected at least 3 and 4; "
                        "preprocessing or weights are wrong")
    for output in outputs:
        path = out_dir / output.platform / output.filename
        tol = TOLERANCES[output.key]
        if output.platform == "android":
            iface = check_onnx_interface(path, DETECTOR_INPUT, [1, 3, 640, 640], DETECTOR_OUTPUT, [1, mp.YOLOX_NUM_ANCHORS, mp.YOLOX_ROW_LENGTH])
        else:
            iface = check_coreml_interface(path, True, DETECTOR_INPUT, [1, 3, 640, 640], DETECTOR_OUTPUT)
        person_gated = output.key in PERSON_GATED
        runs: dict[str, dict[str, Any]] = {}
        gated: list[str] = []
        for label, units, gates in _runs(output):
            runner = _Runner(path, units or None)
            runs[label] = {g: _detector_metrics(runner, cs, refs, _group_tolerance(output.key, g))
                           for g, cs in canvases.items()}
            for g in ("general", "person", "silhouette"):
                m = runs[label][g]
                note = "gate" if gates and (g == "general" or (g == "person" and person_gated)) else "recorded"
                log(f"verify: {output.key} [{label}, {g}, {note}]: "
                    f"{'within' if m['within_tolerance'] else 'OUTSIDE'} tolerance, "
                    f"score_max_abs={m['score_max_abs']:.2e} box_max_abs_px={m['box_max_abs_px']:.3f} "
                    f"match_iou_min={m['match_iou_min']:.4f} matched={m['matched_detections']} "
                    f"missing={m['missing_detections']} extra={m['extra_detections']}"
                    + (f" problems={m['problems']}" if m["problems"] else ""))
            if gates:
                gated.append(label)
        gate_groups = ("general", "person") if person_gated else ("general",)
        gate_problems = [f"[{l}, {g}] {p}" for l in gated for g in gate_groups for p in runs[l][g]["problems"]]
        within = all(runs[l][g]["within_tolerance"] for l in gated for g in gate_groups)
        ok = bool(gated) and not iface["problems"] and not gate_problems and within
        summary = {g: _worst({l: runs[l][g] for l in gated}, gated, _DET_MAX, _DET_MIN)
                   for g in ("general", "person", "silhouette")}
        results["outputs"][output.key] = {
            "passed": ok, "tolerance": tol, "person_tolerance": _group_tolerance(output.key, "person"),
            "interface": iface, "gated_on": gated, "person_gated": person_gated,
            **summary["general"],
            "person": summary["person"], "silhouette": summary["silhouette"],
            "runs": {l: {g: {k: v for k, v in m.items() if k != "per_input"} for g, m in r.items()}
                     for l, r in runs.items()},
            "per_input": {l: {g: m["per_input"] for g, m in r.items()} for l, r in runs.items()},
        }
        if not ok:
            problems.append(f"{output.key} {gated} is outside its tolerance or interface "
                            f"(gated on {', '.join(gate_groups)}): {iface['problems'] or gate_problems or tol}")
    results["problems"] = problems
    results["person_crop_source"] = (native, crop_persons)  # for the embedder; run_verify removes it
    return results


def _clip_inputs_rgb(downloads_dir: Path) -> list[tuple[str, Any]]:
    """RGB pictures for the embedder parity: fixtures, crops of the sample, synthetic pictures."""
    import numpy as np

    rgb = []
    for name, bgr in _fixture_images_bgr():
        rgb.append((name, np.ascontiguousarray(bgr[:, :, ::-1])))
    sample = np.ascontiguousarray(_sample_image_bgr(downloads_dir)[:, :, ::-1])
    rgb.append(("upstream:yolox_dog.jpg", sample))
    # Crops shaped like detections: the dog and the bicycle of the sample (tall and wide boxes).
    rgb.append(("upstream:yolox_dog.jpg[dog]", sample[200:540, 120:320]))
    rgb.append(("upstream:yolox_dog.jpg[bicycle]", sample[110:460, 110:580]))
    for name, bgr in _synthetic_images_bgr():
        rgb.append((name, np.ascontiguousarray(bgr[:, :, ::-1])))
    return rgb


def _embedder_metrics(runner: _Runner, tensors, ref, ref_sims, tol: dict[str, float]) -> dict[str, Any]:
    """Parity of one way of running an embedder against PyTorch's embeddings ``ref`` of ``tensors``.

    An embedding with a value that is not finite fails before anything is
    normalised (``l2_normalize`` would turn a NaN vector into zeros).
    """
    import numpy as np

    raw = [runner.embedder(t) for t in tensors]
    problems = []
    if any(r.shape != (mp.CLIP_EMBEDDING_DIM,) for r in raw):
        problems.append(f"embedding shapes {sorted({r.shape for r in raw})}, expected ({mp.CLIP_EMBEDDING_DIM},)")
        return {"within_tolerance": False, "problems": problems}
    bad = [i for i, r in enumerate(raw) if not np.isfinite(r).all()]
    if bad:
        problems.append(f"{len(bad)} of {len(raw)} embeddings have values that are not finite")
        return {"within_tolerance": False, "problems": problems}
    norms = np.array([np.linalg.norm(r) for r in raw])
    got = np.stack([mp.l2_normalize(r) for r in raw])
    cosines = (ref * got).sum(axis=1)
    sim_change = np.abs(ref @ got.T - ref_sims)
    sim_err = float(np.max(sim_change))
    # Pairs whose PyTorch similarity is near the ground's match threshold: there a change flips a decision.
    near = np.abs(ref_sims - GROUND_MATCH_THRESHOLD) <= NEAR_THRESHOLD_BAND
    near_err = float(np.max(sim_change[near])) if near.any() else 0.0
    cos_min = float(np.min(cosines))
    within = cos_min >= tol["cosine_min"] and sim_err <= tol["similarity_max_abs"]
    if "similarity_near_threshold_max_abs" in tol:
        within = within and near_err <= tol["similarity_near_threshold_max_abs"]
    return {
        "within_tolerance": bool(within),
        "problems": problems,
        "cosine_min": cos_min, "cosine_mean": float(np.mean(cosines)),
        "similarity_max_abs": sim_err,
        "similarity_near_threshold_max_abs": near_err, "pairs_near_threshold": int(near.sum()),
        "norm_max_abs_error": float(np.max(np.abs(norms - 1.0))),
        "per_input_cosine": [float(c) for c in cosines],
    }


@dataclass(frozen=True)
class EmbedderReference:
    """The ground's embedder path: open_clip's own transform and PyTorch's image tower."""

    preprocess: Callable[[Any], Any]  # uint8 RGB array -> [1, 3, 224, 224] float32
    encode: Callable[[Any], Any]  # [1, 3, 224, 224] -> L2-normalised [512] float64


def embedder_reference(downloads_dir: Path) -> EmbedderReference:
    import torch
    from PIL import Image

    model, preprocess = load_clip(downloads_dir)

    def pre(img_rgb):
        return preprocess(Image.fromarray(img_rgb)).unsqueeze(0).numpy()

    def enc(tensor):
        with torch.no_grad():
            return mp.l2_normalize(model.encode_image(torch.from_numpy(tensor)).numpy().reshape(-1))

    return EmbedderReference(pre, enc)


def _bucket(name: str) -> str:
    """The size bucket of an embedder input name (``native``, ``16px`` ...), or ``all``."""
    return name.rsplit("@", 1)[1] if "@" in name else "all"


def verify_embedder(outputs: list[Output], out_dir: Path, groups: dict[str, list[tuple[str, Any]]],
                    reference: EmbedderReference, log: Callable[[str], None]) -> dict[str, Any]:
    """Every embedder output against PyTorch on each group of RGB inputs (``general``, ``person``, ``silhouette``)."""
    import numpy as np

    problems: list[str] = []
    # The reference is the ground's path (flightrisk/vision/reid.py): open_clip's own
    # preprocessing, encode_image, L2 normalisation. The documented preprocessing
    # (mobile_preprocess.clip_preprocess) must give the same tensors.
    prepared = {}
    pre_diff = 0.0
    for g, images in groups.items():
        tensors, refs = [], []
        for _, img in images:
            ground = reference.preprocess(img)
            ours = mp.clip_preprocess(img)
            pre_diff = _finite_max(pre_diff, float(np.max(np.abs(ground - ours))))
            tensors.append(ours)
            refs.append(reference.encode(ground))
        ref = np.stack(refs)
        prepared[g] = ([n for n, _ in images], tensors, ref, ref @ ref.T)
    if not pre_diff <= 1e-6:
        problems.append(f"documented CLIP preprocessing differs from open_clip's by {pre_diff}")
    results: dict[str, Any] = {
        "inputs": {g: names for g, (names, *_rest) in prepared.items()},
        "preprocess_max_abs_vs_open_clip": pre_diff,
        "outputs": {},
    }
    for output in outputs:
        path = out_dir / output.platform / output.filename
        tol = TOLERANCES[output.key]
        if output.platform == "android":
            iface = check_onnx_interface(path, EMBEDDER_INPUT, [1, 3, 224, 224], EMBEDDER_OUTPUT, [1, mp.CLIP_EMBEDDING_DIM])
        else:
            iface = check_coreml_interface(path, False, EMBEDDER_INPUT, [1, 3, 224, 224], EMBEDDER_OUTPUT)
        person_gated = output.key in PERSON_GATED
        runs: dict[str, dict[str, Any]] = {}
        gated: list[str] = []
        for label, units, gates in _runs(output):
            runner = _Runner(path, units or None)
            runs[label] = {}
            for g, (names, tensors, ref, sims) in prepared.items():
                m = _embedder_metrics(runner, tensors, ref, sims, _group_tolerance(output.key, g))
                if "per_input_cosine" in m:
                    buckets: dict[str, float] = {}
                    for n, c in zip(names, m["per_input_cosine"]):
                        buckets[_bucket(n)] = _finite_min(buckets.get(_bucket(n), 1.0), c)
                    m["cosine_min_by_size"] = buckets
                runs[label][g] = m
                note = "gate" if gates and (g == "general" or (g == "person" and person_gated)) else "recorded"
                if m["problems"]:
                    log(f"verify: {output.key} [{label}, {g}, {note}]: {m['problems']}")
                else:
                    log(f"verify: {output.key} [{label}, {g}, {note}]: "
                        f"{'within' if m['within_tolerance'] else 'OUTSIDE'} tolerance, "
                        f"cosine_min={m['cosine_min']:.6f} cosine_mean={m['cosine_mean']:.6f} "
                        f"similarity_max_abs={m['similarity_max_abs']:.2e} "
                        f"near_threshold={m['similarity_near_threshold_max_abs']:.2e} "
                        f"({m['pairs_near_threshold']} pairs)")
            if gates:
                gated.append(label)
        gate_groups = ("general", "person") if person_gated else ("general",)
        gate_problems = [f"[{l}, {g}] {p}" for l in gated for g in gate_groups for p in runs[l][g]["problems"]]
        within = all(runs[l][g]["within_tolerance"] for l in gated for g in gate_groups)
        ok = bool(gated) and not iface["problems"] and not gate_problems and within
        summary = {g: _worst({l: runs[l][g] for l in gated}, gated, _EMB_MAX, _EMB_MIN) for g in prepared}
        for g in prepared:
            by_size: dict[str, float] = {}
            for l in gated:
                for b, c in runs[l][g].get("cosine_min_by_size", {}).items():
                    by_size[b] = _finite_min(by_size.get(b, 1.0), c)
            summary[g]["cosine_min_by_size"] = by_size
        results["outputs"][output.key] = {
            "passed": ok, "tolerance": tol, "person_tolerance": _group_tolerance(output.key, "person"),
            "interface": iface, "gated_on": gated, "person_gated": person_gated,
            **summary.get("general", {}),
            **{g: summary[g] for g in prepared if g != "general"},
            "runs": {l: {g: {k: v for k, v in m.items() if k != "per_input_cosine"} for g, m in r.items()}
                     for l, r in runs.items()},
        }
        if not ok:
            problems.append(f"{output.key} {gated} is outside its tolerance or interface "
                            f"(gated on {', '.join(gate_groups)}): {iface['problems'] or gate_problems or tol}")
    results["problems"] = problems
    return results


def _write_f32(path: Path, array) -> dict[str, Any]:
    import numpy as np

    a = np.ascontiguousarray(array, dtype="<f4")
    path.write_bytes(a.tobytes())
    return {"file": path.name, "dtype": "float32 little-endian", "shape": list(a.shape), "sha256": sha256_file(path)}


def write_golden(golden_dir: Path, detector: Callable[[Any], Any], embedder: EmbedderReference,
                 log: Callable[[str], None]) -> dict[str, Any]:
    """Photo-free fixtures for the apps' unit tests: frame -> detector -> crop -> embedder.

    The frame is ``silhouette_scene()`` (drawn, not photographed). Written are
    the frame as PNG, the detector input and PyTorch's output, the detections,
    the crop of the best person detection cut by the contract's rule (the
    figure's drawn box if no person is found), the embedder input and
    PyTorch's embedding. The frame's colours are lopsided, so a channel swap
    changes the outputs by far more than any tolerance (recorded, and checked).
    """
    import cv2
    import numpy as np

    if golden_dir.exists():
        shutil.rmtree(golden_dir)
    golden_dir.mkdir(parents=True)
    frame, boxes = silhouette_scene()
    cv2.imwrite(str(golden_dir / "frame.png"), frame)
    if not np.array_equal(cv2.imread(str(golden_dir / "frame.png"), cv2.IMREAD_COLOR), frame):
        raise VerificationFailed("the golden frame does not survive a PNG round trip")
    canvas, ratio = mp.yolox_letterbox(frame)
    det_in = mp.yolox_input_tensor(canvas)
    det_out = detector(canvas)
    dets = mp.yolox_postprocess(det_out, ratio)
    persons = [d for d in dets if d["class"] == mp.COCO_PERSON
               and min(np.diff(np.array(mp.crop_pixels(d["box"], *frame.shape[:2])).reshape(2, 2), axis=0)[0]) >= 2]
    if persons:
        box, source = persons[0]["box"], "the highest-scoring person detection"
    else:
        box, source = list(max(boxes, key=lambda b: b[3] - b[1])), "the largest drawn figure (no person detected)"
    crop_bgr = mp.crop_box(frame, box)
    crop_rgb = np.ascontiguousarray(crop_bgr[:, :, ::-1])
    cv2.imwrite(str(golden_dir / "crop.png"), crop_bgr)
    emb_in = mp.clip_preprocess(crop_rgb)
    emb = embedder.encode(emb_in)
    # Channel order: the same frame fed as RGB, and the crop fed as BGR.
    swapped = detector(np.ascontiguousarray(canvas[:, :, ::-1]))
    det_swap = float(np.max(np.abs(mp.yolox_scores(swapped) - mp.yolox_scores(det_out))))
    emb_swap = mp.cosine(embedder.encode(mp.clip_preprocess(np.ascontiguousarray(crop_bgr))), emb)
    files = {
        "frame": {"file": "frame.png", "format": "PNG, 8-bit RGB as stored by PNG (decode it as the app decodes a camera frame)",
                  "shape": list(frame.shape), "sha256": sha256_file(golden_dir / "frame.png")},
        "detector_input": _write_f32(golden_dir / "detector_input.f32", det_in),
        "detector_output": _write_f32(golden_dir / "detector_output.f32", det_out[None]),
        "crop": {"file": "crop.png", "format": "PNG", "shape": list(crop_bgr.shape), "sha256": sha256_file(golden_dir / "crop.png")},
        "embedder_input": _write_f32(golden_dir / "embedder_input.f32", emb_in),
        "embedding": _write_f32(golden_dir / "embedding.f32", np.asarray(emb, dtype=np.float32)[None]),
    }
    doc = {
        "what": "Golden fixtures for the apps' unit tests, made by make_models.py verify from a drawn frame "
                "(no photograph). Run the app's own steps on frame.png and compare each stage.",
        "files": files,
        "letterbox_ratio": ratio,
        "detections": dets,
        "crop_box": {"box": [float(v) for v in box], "source": source,
                     "pixels": list(mp.crop_pixels(box, frame.shape[0], frame.shape[1])),
                     "rule": contract()["embedder"]["crop_from_detection"]},
        "embedding": [float(v) for v in emb],
        "channel_order_check": {
            "detector_score_max_abs_if_fed_rgb": det_swap,
            "embedding_cosine_if_fed_bgr": emb_swap,
        },
        "compare_with": {k: TOLERANCES[k] for k in TOLERANCES},
        "contract": contract(),
    }
    (golden_dir / "golden.json").write_text(json.dumps(doc, indent=2) + "\n")
    log(f"verify: golden fixtures in {golden_dir} (channel swap moves detector scores by {det_swap:.3f}, "
        f"embedding cosine {emb_swap:.4f})")
    problems = []
    if not (det_swap > 0.1 and emb_swap < 0.98):  # NaN fails too
        problems.append(f"the golden frame does not show a channel swap clearly (detector {det_swap}, embedder {emb_swap})")
    return {"dir": str(golden_dir), "files": {k: v["sha256"] for k, v in files.items()},
            "channel_order_check": doc["channel_order_check"], "problems": problems}


def _build_record(work_dir: Path) -> dict[str, Any]:
    path = work_dir / BUILD_NAME
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def run_verify(work_dir: Path, targets: str, log: Callable[[str], None], allow_unpinned_tools: bool = False) -> dict[str, Any]:
    tools = check_tool_versions(allow_unpinned_tools)
    downloads_dir, out_dir = work_dir / "downloads", work_dir / "out"
    wanted = outputs_for(targets)
    missing = [o.key for o in wanted if not (out_dir / o.platform / o.filename).exists()]
    if missing:
        raise VerificationFailed(f"outputs missing (run convert first): {', '.join(missing)}")
    if any(o.platform == "ios" for o in wanted) and platform.system() != "Darwin":
        raise VerificationFailed("Core ML outputs can only be checked on macOS; use --targets android elsewhere")
    build = _build_record(work_dir)
    build_pinned = build.get("tools_pinned") is True
    det_ref = detector_reference(downloads_dir)
    detector = verify_detector([o for o in wanted if o.role == "detector"], out_dir, downloads_dir, log, det_ref)
    groups = {
        "general": _clip_inputs_rgb(downloads_dir),
        "person": _person_crops_rgb(*detector.pop("person_crop_source")),
        "silhouette": _silhouette_crops_rgb(),
    }
    emb_ref = embedder_reference(downloads_dir)
    embedder = verify_embedder([o for o in wanted if o.role == "embedder"], out_dir, groups, emb_ref, log)
    golden = write_golden(work_dir / GOLDEN_DIR_NAME, det_ref, emb_ref, log)
    guard_dirs = [out_dir / p for p in sorted({o.platform for o in wanted})]
    guard = [str(f) for f in licence_guard.scan(guard_dirs, allowlist=False)]
    problems = (detector["problems"] + embedder["problems"] + golden["problems"]
                + [f"licence guard: {g}" for g in guard])
    report = {
        "passed": not problems,
        "targets": targets,
        "problems": problems,
        "tools_pinned": bool(build_pinned and tools["pinned"]),
        "tools": {"convert": build.get("tools"), "verify": tools},
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()}",
        "downloads": {d.key: {"url": d.url, "sha256": d.sha256, "bytes": d.size} for d in DOWNLOADS},
        "outputs": {
            o.key: {
                "sha256": tree_sha256(out_dir / o.platform / o.filename),
                "bytes": tree_size(out_dir / o.platform / o.filename),
            }
            for o in wanted
        },
        "detector": detector,
        "embedder": embedder,
        "golden": golden,
        "licence_guard": guard,
    }
    (work_dir / VERIFY_NAME).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def select_outputs(targets: str, overrides: dict[tuple[str, str], str | None]) -> list[Output]:
    chosen = []
    for (plat, role), default in DEFAULT_VARIANTS.items():
        if targets not in ("all", plat):
            continue
        variant = overrides.get((plat, role)) or default
        match = [o for o in OUTPUTS if o.platform == plat and o.role == role and o.variant == variant]
        if not match:
            raise ValueError(f"no {variant} {role} for {plat}")
        chosen.append(match[0])
    return chosen


def contract(installed: Iterable[Output] = ()) -> dict[str, Any]:
    """The preprocessing contract, as written into mobile-models.json.

    For iOS, each role also says which ``MLModelConfiguration.computeUnits``
    the installed variant must be loaded with (``coreml_compute_units``).
    """
    doc: dict[str, Any] = {
        "detector": {
            "model": "YOLOX-S, COCO 80 classes, Megvii release 0.1.1rc0",
            "input": {"name": DETECTOR_INPUT, "shape": [1, 3, 640, 640], "layout": "NCHW",
                      "channels": "BGR", "range": "0..255 float, no normalisation"},
            "letterbox": {"ratio": "min(640/h, 640/w)", "resized": "int(w*ratio) x int(h*ratio), bilinear",
                          "placement": "top-left", "pad_value": mp.YOLOX_PAD_VALUE},
            "output": {"name": DETECTOR_OUTPUT, "shape": [1, mp.YOLOX_NUM_ANCHORS, mp.YOLOX_ROW_LENGTH],
                       "row": "cx, cy, w, h (640-input pixels), objectness, 80 class scores (sigmoid applied)",
                       "score": "objectness * class_score", "to_frame": "divide box values by ratio",
                       "person_class": mp.COCO_PERSON, "row_order": "stride 8 (80x80), 16 (40x40), 32 (20x20), row-major"},
            "postprocess": {"nms": "per class", "nms_iou": mp.YOLOX_NMS_IOU,
                            "score_threshold": "chosen by the app; verify compares at "
                                               f"{mp.YOLOX_SCORE_THRESHOLD}"},
        },
        "embedder": {
            "model": mp.CLIP_MODEL_ID,
            "input": {"name": EMBEDDER_INPUT, "shape": [1, 3, 224, 224], "layout": "NCHW", "channels": "RGB"},
            "crop_from_detection": mp.CROP_RULE,
            "resize": "shortest side to 224 (bicubic); long side int(224*long/short)",
            "crop": "centre 224x224; offsets int(round((side-224)/2)), halves to even",
            "normalise": {"scale": "v/255", "mean": list(mp.CLIP_MEAN), "std": list(mp.CLIP_STD)},
            "output": {"name": EMBEDDER_OUTPUT, "shape": [1, mp.CLIP_EMBEDDING_DIM], "l2_normalised": True},
        },
    }
    for o in installed:
        if o.platform == "ios":
            doc[o.role]["coreml_compute_units"] = COREML_COMPUTE_UNITS[o.key]
    return doc


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _sibling(dst: Path, kind: str) -> Path:
    """A hidden name next to ``dst`` for staging or for the copy being replaced."""
    return dst.parent / f".{dst.name}.{kind}-{os.getpid()}-{os.urandom(4).hex()}"


def install_atomically(src: Path, dst: Path, expected_sha256: str) -> None:
    """Copy ``src`` (file or folder) to ``dst`` so that ``dst`` is either the old copy or the checked new one.

    The copy is staged under a hidden name next to ``dst``, its digest is
    checked, and it is moved into place with ``os.replace``. A folder that is
    already at ``dst`` is first moved aside under a hidden name and deleted
    once the new one is in place (or moved back if that fails). The staged
    copy is removed on any failure.
    """
    stage = _sibling(dst, "staging")
    try:
        if src.is_dir():
            shutil.copytree(src, stage)
        else:
            shutil.copy2(src, stage)
        digest = tree_sha256(stage)
        if digest != expected_sha256:
            raise VerificationFailed(f"{dst}: the staged copy has SHA-256 {digest}, not the verified {expected_sha256}")
        if dst.is_dir() and not dst.is_symlink():
            old = _sibling(dst, "old")
            os.replace(dst, old)
            try:
                os.replace(stage, dst)
            except OSError:
                os.replace(old, dst)
                raise
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(stage, dst)
    finally:
        _remove(stage)


def _write_atomically(dst: Path, data: bytes) -> None:
    tmp = _sibling(dst, "tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dst)
    finally:
        _remove(tmp)


def _clear_leftovers(dest_dir: Path, names: Iterable[str]) -> None:
    """Remove staging and replaced copies a crashed install left next to ``names``."""
    for name in names:
        for kind in ("staging", "old", "tmp"):
            for left in dest_dir.glob(f".{name}.{kind}-*"):
                _remove(left)


def run_install(work_dir: Path, android_dir: Path | None, ios_dir: Path | None, targets: str,
                overrides: dict[tuple[str, str], str | None], log: Callable[[str], None],
                allow_unpinned_build: bool = False, allow_ungated_embedder: bool = False) -> dict[str, Any]:
    out_dir = work_dir / "out"
    verify_path = work_dir / VERIFY_NAME
    if not verify_path.is_file():
        raise VerificationFailed(f"{verify_path} is missing; run verify first")
    report = json.loads(verify_path.read_text())
    if not report.get("passed"):
        raise VerificationFailed(f"{verify_path} records a failed verification; nothing installed")
    tools_pinned = report.get("tools_pinned") is True
    if not tools_pinned and not allow_unpinned_build:
        raise VerificationFailed(
            f"{verify_path} records a build converted or verified with tools other than the locked set "
            "(requirements.lock); nothing installed. Rebuild in a venv installed with "
            "`pip install --require-hashes -r scripts/mobile_models/requirements.lock`, "
            "or pass --allow-unpinned-build to install it anyway (recorded in mobile-models.json)."
        )
    chosen = select_outputs(targets, overrides)
    for o in chosen:
        # The ground compares Android embeddings with its own (phone-as-edge), so only an
        # embedder whose person parity gates verify is installed without being asked twice.
        if o.platform == "android" and o.role == "embedder" and o.key not in PERSON_GATED and not allow_ungated_embedder:
            raise VerificationFailed(
                f"the {o.variant} Android embedder ({o.filename}) is not gated on the person parity, and the "
                "ground compares Android embeddings with its own; nothing installed. Use the default "
                "(int8-weights) or fp32, or pass --allow-ungated-embedder to install it anyway."
            )
    for o in chosen:
        recorded = report.get("outputs", {}).get(o.key)
        src = out_dir / o.platform / o.filename
        if recorded is None:
            raise VerificationFailed(f"{o.key} was not verified; run verify --targets {targets}")
        if not src.exists() or tree_sha256(src) != recorded["sha256"]:
            raise VerificationFailed(f"{src} is not the file verify passed (digest differs); run verify again")
    dests = {"android": android_dir, "ios": ios_dir}
    installed: dict[str, list[dict[str, Any]]] = {}
    for plat in sorted({o.platform for o in chosen}):
        dest_dir = dests[plat]
        if dest_dir is None:
            raise ValueError(f"no {plat} folder given")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        mine = [o for o in chosen if o.platform == plat]
        # The old manifest goes first: until the new one is written last, the folder lists
        # nothing, so the licence guard (and a store build) fails on a half-finished install.
        _remove(dest_dir / MANIFEST_NAME)
        _clear_leftovers(dest_dir, [o.installed_name for o in mine] + [MANIFEST_NAME, NOTICES_INSTALLED_NAME])
        entries = []
        for o in mine:
            src = out_dir / o.platform / o.filename
            dst = dest_dir / o.installed_name
            install_atomically(src, dst, report["outputs"][o.key]["sha256"])
            entry = {
                "file": o.installed_name, "role": o.role, "variant": o.variant, "built_as": o.filename,
                "sha256": report["outputs"][o.key]["sha256"], "bytes": tree_size(dst),
                "parity": _parity_summary(report, o),
            }
            if plat == "ios":
                entry["coreml_compute_units"] = COREML_COMPUTE_UNITS[o.key]
            entries.append(entry)
            log(f"install: {dst} ({o.variant}, {tree_size(dst) / 1e6:.1f} MB)")
        _write_atomically(dest_dir / NOTICES_INSTALLED_NAME, NOTICES.read_bytes())
        manifest = {
            "generated_by": MANIFEST_GENERATOR,
            "docs": "docs/mobile-models.md",
            "platform": plat,
            "tools_pinned": tools_pinned,
            "models": entries,
            "upstream": {d.key: {"url": d.url, "sha256": d.sha256, "licence": d.licence}
                         for d in (YOLOX_S_WEIGHTS, CLIP_WEIGHTS)},
            "contract": contract(mine),
            "notices": NOTICES_INSTALLED_NAME,
        }
        _write_atomically(dest_dir / MANIFEST_NAME, (json.dumps(manifest, indent=2) + "\n").encode())
        installed[plat] = entries
    guard_targets = [Path(dests[p]) for p in installed]
    findings = licence_guard.scan(guard_targets)
    if findings:
        raise VerificationFailed(
            "forbidden or unlisted model files are in the app folders; remove them before a store build "
            "(install deletes only the files it installs):\n  "
            + "\n  ".join(str(f) for f in findings)
        )
    log(f"install: licence guard passed for {', '.join(str(p) for p in guard_targets)}")
    return installed


def _parity_summary(report: dict[str, Any], o: Output) -> dict[str, Any]:
    section = report["detector" if o.role == "detector" else "embedder"]["outputs"].get(o.key, {})
    keys = ("score_max_abs", "box_max_abs_px", "match_iou_min") if o.role == "detector" else (
        "cosine_min", "cosine_mean", "similarity_max_abs")
    out: dict[str, Any] = {k: section.get(k) for k in keys}
    out["gated_on"] = section.get("gated_on")
    person = section.get("person")
    if isinstance(person, dict):
        extra = ("cosine_min_by_size", "similarity_near_threshold_max_abs", "pairs_near_threshold")
        out["person"] = {k: person.get(k) for k in keys + extra if k in person}
        out["person_gated"] = section.get("person_gated")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(msg, flush=True)


def cmd_fetch(args) -> int:
    downloads_dir = Path(args.work_dir) / "downloads"
    for d in DOWNLOADS:
        _log(f"fetch: {d.key} <- {d.url}")
        path = fetch_one(d, downloads_dir)
        _log(f"fetch: {path} sha256={d.sha256} ({d.size} bytes) OK")
    return 0


def cmd_convert(args) -> int:
    work_dir = Path(args.work_dir)
    tools = check_tool_versions(args.allow_unpinned_tools)
    for d in DOWNLOADS:  # fail closed before anything is loaded
        read_verified(d, work_dir / "downloads")
    out_dir = work_dir / "out"
    for o in outputs_for(args.targets):
        _remove(out_dir / o.platform / o.filename)
    (work_dir / VERIFY_NAME).unlink(missing_ok=True)  # a new conversion is unverified
    (work_dir / BUILD_NAME).unlink(missing_ok=True)
    convert_yolox(work_dir / "downloads", out_dir, args.targets, _log)
    convert_clip(work_dir / "downloads", out_dir, args.targets, _log)
    record = {
        "targets": args.targets,
        "tools_pinned": tools["pinned"],
        "tools": tools,
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()}",
        "downloads": {d.key: d.sha256 for d in DOWNLOADS},
        "outputs": {o.key: {"sha256": tree_sha256(out_dir / o.platform / o.filename),
                            "bytes": tree_size(out_dir / o.platform / o.filename)}
                    for o in outputs_for(args.targets)},
    }
    (work_dir / BUILD_NAME).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    for key, info in record["outputs"].items():
        _log(f"convert: {key} {info['bytes'] / 1e6:.1f} MB sha256={info['sha256']}")
    return 0


def cmd_verify(args) -> int:
    report = run_verify(Path(args.work_dir), args.targets, _log, args.allow_unpinned_tools)
    _log(f"verify: wrote {Path(args.work_dir) / VERIFY_NAME}")
    if not report["passed"]:
        for p in report["problems"]:
            _log(f"verify: FAILED: {p}")
        return 1
    _log("verify: all gated outputs within tolerance"
         + ("" if report["tools_pinned"] else " (UNPINNED tools: install refuses this build without --allow-unpinned-build)"))
    return 0


def _overrides(args) -> dict[tuple[str, str], str | None]:
    return {
        ("android", "detector"): args.android_detector,
        ("android", "embedder"): args.android_embedder,
        ("ios", "detector"): args.ios_detector,
        ("ios", "embedder"): args.ios_embedder,
    }


def cmd_install(args) -> int:
    run_install(Path(args.work_dir), args.android, args.ios, args.targets, _overrides(args), _log,
                args.allow_unpinned_build, args.allow_ungated_embedder)
    return 0


def cmd_all(args) -> int:
    for step in (cmd_fetch, cmd_convert, cmd_verify):
        rc = step(args)
        if rc:
            return rc
    return cmd_install(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_models.py",
        description="Download, convert, verify and install the store-safe FlightRisk mobile models.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR), help="downloads and outputs (default: %(default)s)")
        p.add_argument("--targets", choices=("all", "android", "ios"), default="all",
                       help="which platform's models to build, check or install (default: all)")

    def install_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--android", type=Path, default=DEFAULT_ANDROID_DIR, help="Android assets folder (default: %(default)s)")
        p.add_argument("--ios", type=Path, default=DEFAULT_IOS_DIR, help="iOS models folder (default: %(default)s)")
        p.add_argument("--android-detector", choices=("fp32", "fp16"), help="detector variant for Android (default: fp32)")
        p.add_argument("--android-embedder", choices=("fp32", "int8-weights", "int8-dynamic"),
                       help="embedder variant for Android (default: int8-weights; int8-dynamic needs "
                            "--allow-ungated-embedder)")
        p.add_argument("--allow-ungated-embedder", action="store_true",
                       help="install an Android embedder whose person parity does not gate verify (int8-dynamic)")
        p.add_argument("--ios-detector", choices=("fp32", "fp16"),
                       help="detector variant for iOS (default: fp32; fp16 must be loaded with computeUnits = .cpuAndGPU)")
        p.add_argument("--ios-embedder", choices=("fp16", "palettized8"), help="embedder variant for iOS (default: palettized8)")
        p.add_argument("--allow-unpinned-build", action="store_true",
                       help="install a build converted or verified with unpinned tools (recorded in mobile-models.json)")

    def tools_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--allow-unpinned-tools", action="store_true",
                       help="run even if the installed packages are not requirements.lock (recorded as unpinned)")

    p = sub.add_parser("fetch", help="download the pinned weights and check their SHA-256")
    common(p)
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("convert", help="convert the checked weights to ONNX and Core ML")
    common(p)
    tools_opts(p)
    p.set_defaults(func=cmd_convert)
    p = sub.add_parser("verify", help="check every output against PyTorch, write verify.json and the golden fixtures")
    common(p)
    tools_opts(p)
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("install", help="copy the verified models into the app folders")
    common(p)
    install_opts(p)
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("all", help="fetch, convert, verify and install")
    common(p)
    tools_opts(p)
    install_opts(p)
    p.set_defaults(func=cmd_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ChecksumMismatch, DownloadFailed, VerificationFailed, ToolVersionMismatch, ValueError,
            licence_guard.GuardError) as exc:
        print(f"make_models: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
