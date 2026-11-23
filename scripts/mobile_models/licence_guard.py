#!/usr/bin/env python3
"""Fail a store build that carries a model FlightRisk must not ship.

Usage::

    python scripts/mobile_models/licence_guard.py PATH [PATH ...]

Each PATH is an app asset folder, an iOS models folder, a built ``.app``
bundle or ``.xcarchive`` (folders), a built ``.apk``, ``.aab``, ``.apks`` or
``.ipa``, or a folder holding any of these. Folders are walked. Zip archives
are recognised by their first bytes, not by their name, and are read entry by
entry, including archives inside archives.

A store build may carry only the models ``make_models.py install`` put there.
Each of these checks that fails is a finding:

``allowlist``
    Every model-like file must be listed, with the same SHA-256, in a
    ``mobile-models.json`` written by ``make_models.py install`` in the same
    folder. A file is model-like when its extension is a model or archive
    format's (``MODEL_EXTENSIONS``) or when its first bytes are those of a
    model or an archive (``sniff``: an ONNX or Core ML protobuf, TFLite, ONNX
    Runtime, safetensors, GGUF, HDF5, NumPy, a Python pickle, ncnn, zip, gzip,
    xz, zstd, 7z or bzip2), whatever its name. Every Core ML package or
    compiled model (``.mlpackage``, ``.mlmodelc``) must carry the SHA-256 of
    one of the pinned upstream weights (``PINNED_WEIGHTS_SHA256``) as its
    ``flightrisk.weights_sha256`` metadata, and must have the listed digest
    when a manifest next to it lists it. App packages (``.apk``, ``.aab``,
    ``.apks``, ``.ipa``, and the ``.dm`` files Android builds put next to an
    APK) are containers: they are opened and checked, not listed.
``denylist``
    No file may have the SHA-256 of a known forbidden model file
    (``DENYLIST_SHA256``), whatever its name or place: every file of
    InsightFace's buffalo_l, buffalo_m, buffalo_s, buffalo_sc and antelopev2
    packs, the pack archives themselves, and the model files the apps loaded
    before this pipeline.
``name`` and ``family``
    No path component may match a forbidden model family
    (``FORBIDDEN_FAMILIES``) or a known file name of one (``FORBIDDEN_STEMS``),
    ignoring case and extension. YOLOX (``yolox*``) is allowed; every other
    YOLO name is not.
``content``
    No model file may name Ultralytics, InsightFace or MobileCLIP in its bytes
    (``CONTENT_MARKERS``); an Ultralytics export records its author in its
    metadata, so a renamed one is caught.
``symlink``
    No symbolic link may appear, in a folder or in an archive: a link could
    point at a model outside the checked folder.

``make_models.py verify`` checks its own outputs, which have no manifest yet,
with ``allowlist=False``; every other check still applies.

Exit status: 0 when nothing forbidden is found, 1 when something is (each
finding is printed), 2 when a path is missing or unreadable. A path that cannot
be checked is never treated as clean.

Only the standard library is used, so this runs in any Python 3.9+ (CI, a
Gradle task or an Xcode build phase, whose ``/usr/bin/python3`` is 3.9) without
the conversion tools.
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import json
import lzma
import os
import re
import stat
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

#: The manifest ``make_models.py install`` writes next to the models it installs.
MANIFEST_NAME = "mobile-models.json"
MANIFEST_GENERATOR = "scripts/mobile_models/make_models.py"

#: SHA-256 of the pinned upstream weights (``make_models.YOLOX_S_WEIGHTS`` and
#: ``make_models.CLIP_WEIGHTS``). Every model ``make_models.py`` converts records
#: one of them as its ``flightrisk.weights_sha256`` metadata.
PINNED_WEIGHTS_SHA256: dict[str, str] = {
    "f55ded7181e1b0c13285c56e7790b8f0e8f8db590fe4edb37f0b7f345c913a30": "YOLOX-S, Megvii release 0.1.1rc0",
    "3f25d29d3cc74e1d25d47e0593b4dd0864ced1e9b4d8e486a247ca4502f227f1": "open_clip ViT-B-16 laion2b_s34b_b88k",
}
WEIGHTS_METADATA_KEY = b"flightrisk.weights_sha256"

#: Forbidden model families: a regular expression searched for in every path
#: component (lower case), and why the family must not ship.
FORBIDDEN_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("yolo", r"yolo(?!x)", "YOLO/Ultralytics models (AGPL-3.0 or Ultralytics licence); only YOLOX is allowed"),
    ("ultralytics", r"ultralytics", "Ultralytics models (AGPL-3.0 or Ultralytics licence)"),
    ("scrfd", r"scrfd", "InsightFace SCRFD face detector (non-commercial research weights)"),
    ("arcface", r"arcface", "InsightFace ArcFace face recogniser (non-commercial research weights)"),
    ("insightface", r"insightface", "InsightFace models (non-commercial research weights)"),
    ("buffalo", r"buffalo", "InsightFace model packs buffalo_* (non-commercial research weights)"),
    ("antelope", r"antelope", "InsightFace model pack antelopev2 (non-commercial research weights)"),
    ("mobileclip", r"mobileclip", "Apple MobileCLIP weights (research-only licence)"),
    ("osnet", r"osnet", "OSNet person re-ID (weights trained on research-only re-ID datasets)"),
)

#: Known file names (without extension, lower case) of forbidden models that
#: the family patterns above would not catch, and those FlightRisk has used:
#: every output of scripts/download_matching_models.sh, the model names the
#: apps loaded before this pipeline, and the files inside InsightFace's packs.
FORBIDDEN_STEMS: dict[str, str] = {
    # scripts/download_matching_models.sh outputs
    "osnet_x1_0": "OSNet person re-ID (download_matching_models.sh)",
    "insightface_r18": "InsightFace recogniser (download_matching_models.sh)",
    "osnetreid": "OSNet person re-ID (download_matching_models.sh)",
    "insightfacer18": "InsightFace recogniser (download_matching_models.sh)",
    # model names the apps loaded before the store pipeline
    "yolo11n": "Ultralytics YOLO11n (AGPL-3.0)",
    "yolopersondetector": "Ultralytics YOLO person detector",
    "scrfd_500m": "InsightFace SCRFD face detector",
    "scrfdfacedetector": "InsightFace SCRFD face detector",
    "arcface_mobilefacenet": "InsightFace ArcFace recogniser",
    "arcfacemobile": "InsightFace ArcFace recogniser",
    "clip_visual": "legacy CLIP encoder of unknown provenance (export_clip_onnx.py makes MobileCLIP by default)",
    "clipvisual": "legacy CLIP encoder of unknown provenance (export_clip_onnx.py makes MobileCLIP by default)",
    "mobileclip_s2": "Apple MobileCLIP-S2 (research-only licence)",
    # files inside InsightFace's buffalo_* and antelopev2 packs
    "det_500m": "InsightFace SCRFD detector (buffalo_s, buffalo_sc)",
    "det_2.5g": "InsightFace SCRFD detector (buffalo_m)",
    "det_10g": "InsightFace SCRFD detector (buffalo_l)",
    "w600k_mbf": "InsightFace ArcFace recogniser (buffalo_s, buffalo_sc)",
    "w600k_r50": "InsightFace ArcFace recogniser (buffalo_l, buffalo_m)",
    "glintr100": "InsightFace recogniser (antelopev2)",
    "scrfd_10g_bnkps": "InsightFace SCRFD detector (antelopev2)",
    "1k3d68": "InsightFace landmark model",
    "2d106det": "InsightFace landmark model",
    "genderage": "InsightFace attribute model",
}

#: SHA-256 -> (size in bytes, what it is) of known forbidden model files. Computed
#: from the InsightFace v0.7 release archives (every file inside them, and the
#: archives) and from the models in the app folders of a developer checkout
#: before this pipeline (mobile/app/src/main/assets and ios/FlightRisk/Models).
#: Only files of 64 KiB or more are listed, so a small generic file can never match.
DENYLIST_SHA256: dict[str, tuple[int, str]] = {
    "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f": (288621354, "InsightFace v0.7 buffalo_l.zip"),
    "4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb": (1322532, "InsightFace v0.7 buffalo_l: genderage.onnx; InsightFace v0.7 buffalo_s: genderage.onnx; InsightFace v0.7 antelopev2: antelopev2/genderage.onnx; InsightFace v0.7 buffalo_m: buffalo_m/genderage.onnx"),
    "f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf": (5030888, "InsightFace v0.7 buffalo_l: 2d106det.onnx; InsightFace v0.7 buffalo_s: 2d106det.onnx; InsightFace v0.7 antelopev2: antelopev2/2d106det.onnx; InsightFace v0.7 buffalo_m: buffalo_m/2d106det.onnx"),
    "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91": (16923827, "InsightFace v0.7 buffalo_l: det_10g.onnx; InsightFace v0.7 antelopev2: antelopev2/scrfd_10g_bnkps.onnx"),
    "df5c06b8a0c12e422b2ed8947b8869faa4105387f199c477af038aa01f9a45cc": (143607619, "InsightFace v0.7 buffalo_l: 1k3d68.onnx; InsightFace v0.7 buffalo_s: 1k3d68.onnx; InsightFace v0.7 antelopev2: antelopev2/1k3d68.onnx; InsightFace v0.7 buffalo_m: buffalo_m/1k3d68.onnx"),
    "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43": (174383860, "InsightFace v0.7 buffalo_l: w600k_r50.onnx; InsightFace v0.7 buffalo_m: buffalo_m/w600k_r50.onnx"),
    "d85a87f503f691807cd8bb97128bdf7a0660326cd9cd02657127fa978bab8b5e": (127607557, "InsightFace v0.7 buffalo_s.zip"),
    "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a": (2524817, "InsightFace v0.7 buffalo_s: det_500m.onnx; InsightFace v0.7 buffalo_sc: det_500m.onnx; pre-pipeline app model mobile/app/src/main/assets/scrfd_500m.onnx"),
    "9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f": (13616099, "InsightFace v0.7 buffalo_s: w600k_mbf.onnx; InsightFace v0.7 buffalo_sc: w600k_mbf.onnx; pre-pipeline app model mobile/app/src/main/assets/arcface_mobilefacenet.onnx"),
    "57d31b56b6ffa911c8a73cfc1707c73cab76efe7f13b675a05223bf42de47c72": (14969382, "InsightFace v0.7 buffalo_sc.zip"),
    "8e182f14fc6e80b3bfa375b33eb6cff7ee05d8ef7633e738d1c89021dcf0c5c5": (360662982, "InsightFace v0.7 antelopev2.zip"),
    "4ab1d6435d639628a6f3e5008dd4f929edf4c4124b1a7169e1048f9fef534cdf": (260665334, "InsightFace v0.7 antelopev2: antelopev2/glintr100.onnx"),
    "d98264bd8f2dc75cbc2ddce2a14e636e02bb857b3051c234b737bf3b614edca9": (275951529, "InsightFace v0.7 buffalo_m.zip"),
    "041f73f47371333d1d17a6fee6c8ab4e6aecabefe398ff32cca4e2d5eaee0af9": (3292009, "InsightFace v0.7 buffalo_m: buffalo_m/det_2.5g.onnx"),
    "fd228984d4b912a7bdeca6b49a7a7a2c5f9a004db1b9cb7bab3310c5b5562348": (89283180, "pre-pipeline app model mobile/app/src/main/assets/clip_visual.onnx"),
    "6fc32abf410f790c5fc18947c0e23f2208b81a6bba43802a2c3d6540b84cd2a5": (10741399, "pre-pipeline app model mobile/app/src/main/assets/yolo11n.onnx"),
    "811b8f333c963529d179e3702c6ac491b205f15905739cc9b633939701ea5a88": (101877, "pre-pipeline app model ios/FlightRisk/Models/ArcFaceMobile.mlmodelc/model.mil"),
    "994e849b8fd0d44dbb33b4aac42d1e0b49b8410ede0f9d32b93ae5d827ed6e68": (6825280, "pre-pipeline app model ios/FlightRisk/Models/ArcFaceMobile.mlmodelc/weights/weight.bin"),
    "eedb3939566104070b4fdf014a5b5cca2b7b3fc26806138ae569fdeaaef7d064": (207916, "pre-pipeline app model ios/FlightRisk/Models/CLIPVisual.mlmodelc/model.mil"),
    "9a0f45a725e871f9d94913debc3fc12180171464ebd2b43ccd1b5b7034dd14f8": (175709312, "pre-pipeline app model ios/FlightRisk/Models/CLIPVisual.mlmodelc/weights/weight.bin"),
    "7b79b95a5b2410816a8da454b8882079df46a00ecb43d17b8568f4afc5bfc507": (110487, "pre-pipeline app model ios/FlightRisk/Models/SCRFDFaceDetector.mlmodelc/model.mil"),
    "906c6c13a2a4846615d566e51fe5f398249ca16f3c91075f3c58ed855a6d8e64": (1261608, "pre-pipeline app model ios/FlightRisk/Models/SCRFDFaceDetector.mlmodelc/weights/weight.bin"),
    "f64800c4f4275d9788250baef433d127a5c1e5d64994e6c16c4df4a83f877d9f": (194684, "pre-pipeline app model ios/FlightRisk/Models/YOLOPersonDetector.mlmodelc/model.mil"),
    "9723ff8dc01c3e2b2ab3908487de186ed1a9509926ffa4020697e723cfb8f144": (5294880, "pre-pipeline app model ios/FlightRisk/Models/YOLOPersonDetector.mlmodelc/weights/weight.bin"),
}

#: Byte strings (lower case) that mark a forbidden model inside a model file.
#: Ultralytics exports write "Ultralytics" into the ONNX, TFLite and Core ML metadata.
CONTENT_MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"ultralytics", "metadata names Ultralytics (AGPL-3.0 or Ultralytics licence)"),
    (b"insightface", "metadata names InsightFace (non-commercial research weights)"),
    (b"mobileclip", "metadata names MobileCLIP (research-only licence)"),
)

#: Extensions of model and archive formats. A file with one is model-like whatever its bytes.
MODEL_EXTENSIONS = frozenset({
    ".onnx", ".ort", ".tflite", ".lite", ".pt", ".pth", ".ptl", ".torchscript", ".safetensors",
    ".mlmodel", ".param", ".mnn", ".engine", ".plan", ".h5", ".hdf5", ".keras", ".gguf", ".npz", ".npy",
    ".caffemodel", ".pkl", ".pickle", ".ckpt", ".tnnmodel", ".dlc", ".weights",
    ".zip", ".tar", ".gz", ".tgz", ".xz", ".zst", ".7z", ".bz2",
})
#: Extensions of files whose bytes are also searched for CONTENT_MARKERS (models often hide in them).
CONTENT_SCAN_EXTENSIONS = MODEL_EXTENSIONS | {".bin", ".pb", ".dat"}
#: Folders that are one model each (Core ML packages and compiled models).
MODEL_BUNDLE_EXTENSIONS = frozenset({".mlpackage", ".mlmodelc"})
#: Zip archives that are app packages: opened and checked, but not model-like themselves.
APP_PACKAGE_EXTENSIONS = frozenset({".apk", ".aab", ".apks", ".ipa", ".dm"})

_CHUNK = 8 << 20
_HEAD = 64 << 10
_FAMILY_RES = tuple((name, re.compile(pattern), reason) for name, pattern, reason in FORBIDDEN_FAMILIES)
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_MAGIC: tuple[tuple[int, bytes, str], ...] = (
    (0, b"PK\x03\x04", "zip archive"),
    (0, b"PK\x05\x06", "zip archive"),
    (0, b"PK\x07\x08", "zip archive"),
    (0, b"\x1f\x8b", "gzip archive"),
    (0, b"\xfd7zXZ\x00", "xz archive"),
    (0, b"\x28\xb5\x2f\xfd", "zstd archive"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z archive"),
    (4, b"TFL3", "TFLite model"),
    (4, b"ORTM", "ONNX Runtime model"),
    (0, b"GGUF", "GGUF model"),
    (0, b"\x89HDF\r\n\x1a\n", "HDF5 file"),
    (0, b"\x93NUMPY", "NumPy array"),
    (0, b"7767517", "ncnn model"),
)


@dataclass(frozen=True)
class Finding:
    """One forbidden item: where it is, which rule matched, and why it is forbidden."""

    location: str
    rule: str
    reason: str

    def __str__(self) -> str:
        return f"{self.location}: {self.rule} - {self.reason}"


class GuardError(Exception):
    """A path could not be checked (missing, unreadable or a broken archive)."""


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def _suffix(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def _stem(name: str) -> str:
    """``name`` in lower case without its last extension."""
    lower = name.lower()
    suffix = _suffix(lower)
    return lower[: -len(suffix)] if suffix else lower


def check_name(name: str) -> tuple[str, str] | None:
    """The (rule, reason) a single file or folder name breaks, or None when it is allowed."""
    lower = name.lower()
    stem = _stem(name)
    for candidate in (lower, stem):
        if candidate in FORBIDDEN_STEMS:
            return f"name:{candidate}", FORBIDDEN_STEMS[candidate]
    for family, regex, reason in _FAMILY_RES:
        if regex.search(lower):
            return f"family:{family}", reason
    return None


def check_parts(path: str) -> tuple[str, str] | None:
    """The first rule any component of a ``/``-separated path breaks, or None."""
    for part in PurePosixPath(path.replace("\\", "/")).parts:
        hit = check_name(part)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------------------
# What a file is
# ---------------------------------------------------------------------------


def _varint(buf: bytes, pos: int) -> tuple[int | None, int]:
    result = shift = 0
    while pos < len(buf) and shift < 64:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
    return None, pos


def _model_protobuf(head: bytes, size: int) -> bool:
    """Whether ``head`` (the first bytes of a ``size``-byte file) starts like an ONNX or Core ML model.

    Both are protocol buffers whose first field is a small version number
    (ONNX ``ir_version``, Core ML ``specificationVersion``). The top-level
    fields are walked until the ONNX graph or opset list (fields 7 and 8) or a
    Core ML model type (fields 200 and up) appears. A well-formed walk that
    runs past the bytes read is counted as a model (fail closed).
    """
    if len(head) < 2 or head[0] != 0x08:
        return False
    truncated = len(head) < size
    version, pos = _varint(head, 1)
    if version is None:
        return truncated
    if not 1 <= version <= 64:
        return False
    for _ in range(64):
        if pos >= size:
            return False
        if pos >= len(head):
            return truncated
        tag, pos = _varint(head, pos)
        if tag is None:
            return truncated
        number, wire = tag >> 3, tag & 7
        if number == 0 or wire not in (0, 1, 2, 5):
            return False
        if wire == 2 and (number in (7, 8) or 200 <= number < 1000):
            return True
        if wire == 0:
            value, pos = _varint(head, pos)
            if value is None:
                return truncated
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            length, pos = _varint(head, pos)
            if length is None:
                return truncated
            pos += length
        if pos > size:
            return False
    return False


def sniff(head: bytes, size: int) -> str | None:
    """What a file's first bytes say it is, when that is a model or an archive; otherwise None."""
    for offset, magic, what in _MAGIC:
        if head[offset:offset + len(magic)] == magic:
            return what
    if head[:3] == b"BZh" and head[3:4] in (b"1", b"2", b"3", b"4", b"5", b"6", b"7", b"8", b"9"):
        return "bzip2 archive"
    if len(head) >= 2 and head[0] == 0x80 and 2 <= head[1] <= 5:
        return "Python pickle (a PyTorch checkpoint)"
    if size >= 10 and head[8:9] == b"{":
        header = int.from_bytes(head[:8], "little")
        if 2 <= header <= size - 8:
            return "safetensors file"
    if _model_protobuf(head, size):
        return "ONNX or Core ML model (protobuf)"
    return None


def model_like(name: str, head: bytes, size: int) -> str | None:
    """Why a file is model-like (its extension or its first bytes), or None when it is not."""
    suffix = _suffix(name)
    what = sniff(head, size)
    if what is not None:
        return what
    if suffix in MODEL_EXTENSIONS:
        return f"model file extension {suffix}"
    return None


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(items: Iterable[tuple[str, str]]) -> str:
    """SHA-256 over (relative ``/``-separated name, file SHA-256) pairs, in path order."""
    digest = hashlib.sha256()
    for rel, sha in sorted(items, key=lambda item: tuple(item[0].split("/"))):
        name = rel.encode()
        digest.update(len(name).to_bytes(4, "big") + name)
        digest.update(bytes.fromhex(sha))
    return digest.hexdigest()


def tree_sha256(path: Path | str) -> str:
    """SHA-256 of a file, or of a folder's relative file names and contents in path order.

    ``make_models.py`` records this for every output and ``mobile-models.json``
    carries it; the guard computes the same for a listed Core ML package.
    """
    path = Path(path)
    if path.is_file():
        return sha256_file(path)
    if not path.is_dir():
        raise FileNotFoundError(path)
    items = []
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            full = Path(dirpath) / f
            if full.is_file() and not full.is_symlink():
                items.append((full.relative_to(path).as_posix(), sha256_file(full)))
    return tree_digest(items)


# ---------------------------------------------------------------------------
# Reading files once
# ---------------------------------------------------------------------------


class _Search:
    """Case-insensitive search for several byte strings across the chunks of a stream."""

    def __init__(self, patterns: Iterable[bytes]):
        self.patterns = tuple(patterns)
        self.found: set[bytes] = set()
        self.keep = max((len(p) for p in self.patterns), default=1) - 1
        self.tail = b""

    def feed(self, chunk: bytes) -> None:
        window = (self.tail + chunk).lower()
        for p in self.patterns:
            if p not in self.found and p in window:
                self.found.add(p)
        self.tail = window[-self.keep:] if self.keep else b""


_PATTERNS = tuple(m for m, _ in CONTENT_MARKERS) + (WEIGHTS_METADATA_KEY,) + tuple(
    d.encode() for d in PINNED_WEIGHTS_SHA256
)


def scan_stream(stream: IO[bytes]) -> tuple[str, str] | None:
    """The first CONTENT_MARKERS hit in a binary stream, as (rule, reason), or None."""
    search = _Search(m for m, _ in CONTENT_MARKERS)
    for chunk in iter(lambda: stream.read(_CHUNK), b""):
        search.feed(chunk)
    return _marker_hit(search.found)


def _marker_hit(found: set[bytes]) -> tuple[str, str] | None:
    for marker, reason in CONTENT_MARKERS:
        if marker in found:
            return f"content:{marker.decode()}", reason
    return None


def _has_pinned_metadata(found: set[bytes]) -> bool:
    return WEIGHTS_METADATA_KEY in found and any(d.encode() in found for d in PINNED_WEIGHTS_SHA256)


@dataclass
class _File:
    """One regular file in a folder or an archive."""

    rel: str  # "/"-separated path inside the scanned folder or archive
    size: int
    opener: Callable[[], IO[bytes]]
    location: str


#: Compressed formats the standard library can open: the first bytes of what they hold are checked.
_INSPECTABLE: dict[str, Callable[[IO[bytes]], IO[bytes]]] = {
    "gzip archive": lambda f: gzip.GzipFile(fileobj=f, mode="rb"),  # noqa: SIM115 - used in a with block
    "bzip2 archive": lambda f: bz2.BZ2File(f, mode="rb"),  # noqa: SIM115 - used in a with block
    "xz archive": lambda f: lzma.LZMAFile(f, mode="rb"),  # noqa: SIM115 - used in a with block
}


@dataclass
class _Read:
    head: bytes
    sha256: str | None = None
    found: set = field(default_factory=set)
    spool: Any = None  # a copy of a zip, gzip, bzip2 or xz file, to be opened


def _spooled(head: bytes) -> bool:
    return head[:4] in _ZIP_MAGIC or sniff(head, len(head) + 1) in _INSPECTABLE


def _read(f: _File, want_all: Callable[[bytes], bool]) -> _Read:
    """Read the first bytes of ``f``; read the rest (hashing and searching it) when ``want_all(head)``."""
    spool = None
    try:
        with f.opener() as fh:
            head = fh.read(_HEAD)
            if not want_all(head):
                return _Read(head)
            digest = hashlib.sha256()
            search = _Search(_PATTERNS)
            # A real temporary file: before Python 3.11 SpooledTemporaryFile has no seekable(),
            # which zipfile needs, so the guard crashed on /usr/bin/python3 (3.9).
            spool = tempfile.TemporaryFile() if _spooled(head) else None  # noqa: SIM115 - closed by the caller
            chunk = head
            while chunk:
                digest.update(chunk)
                search.feed(chunk)
                if spool is not None:
                    spool.write(chunk)
                chunk = fh.read(_CHUNK)
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error) as exc:
        if spool is not None:
            spool.close()
        raise GuardError(f"{f.location}: cannot be read ({exc})") from exc
    return _Read(head, digest.hexdigest(), search.found, spool)


# ---------------------------------------------------------------------------
# Checking a folder or an archive
# ---------------------------------------------------------------------------


def _bundle_of(rel: str) -> str | None:
    """The Core ML package or compiled model folder (relative path) that ``rel`` is inside, if any."""
    parts = rel.split("/")
    for i, part in enumerate(parts[:-1]):
        if _suffix(part) in MODEL_BUNDLE_EXTENSIONS:
            return "/".join(parts[: i + 1])
    return None


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _base(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def _everywhere(rel: str) -> bool:
    return True


def _asset_area(name: str) -> Callable[[str], bool]:
    """Which entries of an archive named ``name`` are app asset folders.

    In an asset folder, every archive is model-like (and so must be listed,
    which ``make_models.py`` never does). Elsewhere (an APK's Java resources,
    say: OkHttp ships its public suffix list gzipped) a gzip, bzip2 or xz file
    is opened and refused only when what it holds is a model or an archive,
    and a zip is opened and its entries checked. A model format (ONNX, TFLite
    and the rest) must be listed wherever it is.
    """
    suffix = _suffix(name)
    if suffix == ".apk":
        return lambda rel: rel.startswith("assets/")
    if suffix == ".aab":
        return lambda rel: re.match(r"^[^/]+/assets/", rel) is not None
    if suffix == ".ipa":
        return lambda rel: rel.startswith("Payload/")
    if suffix in (".apks", ".dm"):
        return lambda rel: False
    return _everywhere


def _compressed_content(r: _Read, what: str, location: str) -> str | None:
    """What a gzip, bzip2 or xz file holds, when that is a model or an archive; otherwise None."""
    try:
        r.spool.seek(0)
        with _INSPECTABLE[what](r.spool) as inner:
            head = inner.read(_HEAD)
            more = inner.read(1)
    except (OSError, EOFError, lzma.LZMAError, zlib.error) as exc:
        raise GuardError(f"{location}: {what} cannot be read ({exc})") from exc
    if head[257:262] == b"ustar":
        return "tar archive"
    # sniff needs the size only to know whether the head is the whole content.
    return sniff(head, len(head) + (1 << 40) if more else len(head))


def _parse_manifest(data: bytes) -> dict[str, str] | None:
    """{file name: sha256} of a mobile-models.json, or None when it is not one install wrote."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("generated_by") != MANIFEST_GENERATOR:
        return None
    models = doc.get("models")
    if not isinstance(models, list):
        return None
    listed: dict[str, str] = {}
    for m in models:
        if not isinstance(m, dict):
            return None
        name, sha = m.get("file"), m.get("sha256")
        if not isinstance(name, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            return None
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        listed[name] = sha
    return listed


def _check_files(files: list[_File], allowlist: bool, findings: list[Finding], depth: int,
                 in_assets: Callable[[str], bool] = _everywhere) -> None:
    """Every check on the files of one folder or archive (archives inside are checked as they are met).

    ``in_assets(rel)`` says whether a file is in an app asset folder (see ``_asset_area``).
    """
    # Manifests first: they decide which model-like files are allowed.
    manifests: dict[str, dict[str, str]] = {}
    for f in files:
        if _base(f.rel) == MANIFEST_NAME:
            try:
                with f.opener() as fh:
                    data = fh.read(4 << 20)
            except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error) as exc:
                raise GuardError(f"{f.location}: cannot be read ({exc})") from exc
            listed = _parse_manifest(data)
            if listed is None:
                findings.append(Finding(f.location, "allowlist:manifest",
                                        f"not a {MANIFEST_NAME} written by {MANIFEST_GENERATOR}"))
                listed = {}
            manifests[_parent(f.rel)] = listed

    named: set[str] = set()
    bundles: dict[str, list[tuple[str, str, set]]] = {}
    bundle_locations: dict[str, str] = {}
    deny_sizes = {size for size, _ in DENYLIST_SHA256.values()}
    for f in sorted(files, key=lambda x: tuple(x.rel.split("/"))):
        parts = f.rel.split("/")
        hit = None
        for i, part in enumerate(parts):
            hit = check_name(part)
            if hit:
                prefix = "/".join(parts[: i + 1])
                if prefix not in named:
                    named.add(prefix)
                    location = f.location[: len(f.location) - len(f.rel)] + prefix
                    findings.append(Finding(location, *hit))
                break
        if hit:
            continue  # reported once by its forbidden name; nothing under it can be allowed

        bundle = _bundle_of(f.rel)
        suffix = _suffix(f.rel)

        def want_all(head: bytes, _f: _File = f, _bundle: str | None = bundle, _suffix: str = suffix) -> bool:
            return (
                _bundle is not None
                or _f.size in deny_sizes
                or _suffix in CONTENT_SCAN_EXTENSIONS
                or model_like(_f.rel, head, _f.size) is not None
            )

        r = _read(f, want_all)
        try:
            if r.sha256 is not None and r.sha256 in DENYLIST_SHA256:
                findings.append(Finding(f.location, "denylist", f"known forbidden model file: {DENYLIST_SHA256[r.sha256][1]}"))
                continue
            what = model_like(f.rel, r.head, f.size)
            sniffed = sniff(r.head, f.size)
            # An archive's own bytes are not searched (a zip lists its entries' names in plain
            # text, and compressed bytes hide everything); its entries are checked one by one.
            marker = None if sniffed is not None and sniffed.endswith("archive") else _marker_hit(r.found)
            if marker:
                findings.append(Finding(f.location, *marker))
                continue
            if r.spool is not None and r.head[:4] in _ZIP_MAGIC:
                r.spool.seek(0)
                with zipfile.ZipFile(r.spool) as nested:
                    _check_zip(nested, f.location, allowlist, findings, depth + 1, _asset_area(f.rel))
            if bundle is not None:
                bundles.setdefault(bundle, []).append((f.rel[len(bundle) + 1:], r.sha256 or "", r.found))
                bundle_locations[bundle] = f.location[: len(f.location) - len(f.rel)] + bundle
                if allowlist and what is not None and what.endswith("archive"):
                    findings.append(Finding(f.location, "allowlist:unlisted",
                                            f"{what} inside a Core ML model folder, which never holds archives"))
                continue  # the files of a Core ML model are checked together below
            if r.spool is not None and r.head[:4] in _ZIP_MAGIC and suffix in APP_PACKAGE_EXTENSIONS:
                continue  # an app package is a container; its contents were checked
            if what is None or not allowlist:
                continue
            if what.endswith("archive") and sniffed == what and not in_assets(f.rel):
                # Outside an asset folder: a zip's entries were checked above; a compressed
                # file is refused only for what it holds; zstd and 7z cannot be opened here.
                if what == "zip archive":
                    continue
                if what in _INSPECTABLE:
                    inner = _compressed_content(r, what, f.location)
                    if inner is not None:
                        findings.append(Finding(f.location, "allowlist:compressed",
                                                f"{what} holding a {inner}; only models make_models.py installed may ship"))
                    continue
                findings.append(Finding(f.location, "allowlist:uninspectable",
                                        f"{what}, which the guard cannot open; a store build may not carry one"))
                continue
            listed = manifests.get(_parent(f.rel))
            if listed is None:
                findings.append(Finding(f.location, "allowlist:unlisted",
                                        f"{what} with no {MANIFEST_NAME} next to it; only models make_models.py installed may ship"))
            elif _base(f.rel) not in listed:
                findings.append(Finding(f.location, "allowlist:unlisted",
                                        f"{what} not listed in the {MANIFEST_NAME} next to it"))
            elif listed[_base(f.rel)] != r.sha256:
                findings.append(Finding(f.location, "allowlist:digest",
                                        f"SHA-256 {r.sha256} is not the {listed[_base(f.rel)]} {MANIFEST_NAME} lists"))
        except zipfile.BadZipFile as exc:
            raise GuardError(f"{f.location}: not a readable zip archive ({exc})") from exc
        finally:
            if r.spool is not None:
                r.spool.close()

    for bundle, items in sorted(bundles.items()):
        location = bundle_locations[bundle]
        if not allowlist:
            continue
        if not any(_has_pinned_metadata(found) for _, _, found in items):
            findings.append(Finding(location, "allowlist:coreml-metadata",
                                    "Core ML model without a pinned flightrisk.weights_sha256 in its metadata; "
                                    "only models make_models.py converted may ship"))
            continue
        listed = manifests.get(_parent(bundle), {})
        name = _base(bundle)
        if name in listed:
            digest = tree_digest((rel, sha) for rel, sha, _ in items)
            if digest != listed[name]:
                findings.append(Finding(location, "allowlist:digest",
                                        f"folder SHA-256 {digest} is not the {listed[name]} {MANIFEST_NAME} lists"))


def _check_zip(archive: zipfile.ZipFile, label: str, allowlist: bool, findings: list[Finding], depth: int = 0,
               in_assets: Callable[[str], bool] = _everywhere) -> None:
    if depth > 8:
        raise GuardError(f"{label}: archives nested more than 8 deep")
    files: list[_File] = []
    for info in archive.infolist():
        location = f"{label}!{info.filename}"
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            findings.append(Finding(location, "symlink", "symbolic link in an archive; a store build may not carry links"))
            continue
        if info.is_dir():
            continue
        files.append(_File(info.filename.rstrip("/"), info.file_size,
                           (lambda i=info: archive.open(i)), location))
    _check_files(files, allowlist, findings, depth, in_assets)


def _scan_zip_path(path: Path, label: str, allowlist: bool, findings: list[Finding]) -> None:
    try:
        archive = zipfile.ZipFile(str(path))
    except (zipfile.BadZipFile, OSError) as exc:
        raise GuardError(f"{label}: not a readable zip archive ({exc})") from exc
    with archive:
        _check_zip(archive, label, allowlist, findings, 0, _asset_area(path.name))


def _folder_files(root: Path, findings: list[Finding]) -> list[_File]:
    def _raise(exc: OSError) -> None:
        raise GuardError(f"{exc.filename}: cannot be read ({exc.strerror})")

    files: list[_File] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
        here = Path(dirpath)
        for d in list(dirnames):
            if (here / d).is_symlink():
                findings.append(Finding(str(here / d), "symlink",
                                        "symbolic link to a folder; a store build may not carry links"))
                dirnames.remove(d)
        for name in filenames:
            full = here / name
            if full.is_symlink():
                findings.append(Finding(str(full), "symlink", "symbolic link; a store build may not carry links"))
                continue
            try:
                st = full.stat()
            except OSError as exc:
                raise GuardError(f"{full}: cannot be read ({exc.strerror})") from exc
            if not stat.S_ISREG(st.st_mode):
                findings.append(Finding(str(full), "special-file", "not a regular file"))
                continue
            files.append(_File(full.relative_to(root).as_posix(), st.st_size,
                               (lambda p=full: open(p, "rb")), str(full)))  # noqa: SIM115 - closed by the caller
    return files


def scan_path(path: Path | str, allowlist: bool = True) -> list[Finding]:
    """Every forbidden item under one folder, archive or file."""
    path = Path(path)
    findings: list[Finding] = []
    if path.is_symlink():
        findings.append(Finding(str(path), "symlink", "symbolic link; pass the real path"))
        return findings
    if not path.exists():
        raise GuardError(f"{path}: does not exist")
    hit = check_name(path.name)
    if hit:
        findings.append(Finding(str(path), *hit))
    if path.is_file():
        try:
            with path.open("rb") as fh:
                head = fh.read(8)
        except OSError as exc:
            raise GuardError(f"{path}: cannot be read ({exc})") from exc
        if head[:4] in _ZIP_MAGIC:
            _scan_zip_path(path, str(path), allowlist, findings)
        else:
            # A single file is checked against the manifest in its own folder.
            manifest = path.parent / MANIFEST_NAME
            files = [_File(path.name, path.stat().st_size, (lambda p=path: open(p, "rb")), str(path))]  # noqa: SIM115 - closed by the caller
            if manifest.is_file() and path.name != MANIFEST_NAME:
                files.append(_File(MANIFEST_NAME, manifest.stat().st_size,
                                   (lambda p=manifest: open(p, "rb")), str(manifest)))  # noqa: SIM115 - closed by the caller
            if not hit:
                _check_files(files, allowlist, findings, 0)
        return findings
    if not path.is_dir():
        raise GuardError(f"{path}: neither a file nor a folder")
    if hit:
        return findings
    files = _folder_files(path, findings)
    _check_files(files, allowlist, findings, 0)
    return findings


def scan(paths: Iterable[Path | str], allowlist: bool = True) -> list[Finding]:
    """Every forbidden item under all ``paths``; raises GuardError if one cannot be checked."""
    findings: list[Finding] = []
    for p in paths:
        findings.extend(scan_path(p, allowlist=allowlist))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if a model other than the ones make_models.py installed is in app asset folders "
                    "or a built APK, AAB, IPA or .app.",
    )
    parser.add_argument("paths", nargs="+", help="asset or model folders, .app/.xcarchive folders, or .apk/.aab/.ipa files")
    args = parser.parse_args(argv)
    try:
        findings = scan(args.paths)
    except GuardError as exc:
        print(f"licence guard: cannot check {exc}", file=sys.stderr)
        return 2
    if findings:
        print(f"licence guard: {len(findings)} forbidden or unlisted model file(s) found:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print("Remove them before a store build; only the models make_models.py installed may ship. "
              "See docs/mobile-models.md.", file=sys.stderr)
        return 1
    print(f"licence guard: no forbidden or unlisted model files in {len(args.paths)} path(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
