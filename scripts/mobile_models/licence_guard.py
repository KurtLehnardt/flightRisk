#!/usr/bin/env python3
"""Fail a store build that carries a model FlightRisk must not ship.

Usage::

    python scripts/mobile_models/licence_guard.py PATH [PATH ...]

Each PATH is an app asset folder, an iOS models folder, a built ``.app``
bundle or ``.xcarchive`` (folders), or a built ``.apk``, ``.aab``, ``.ipa`` or
other zip archive. Folders are walked, archives are read entry by entry, and an
archive found inside a folder is read too.

A path is refused when any part of its name matches a forbidden model family
(``FORBIDDEN_FAMILIES``) or a known file name of one (``FORBIDDEN_STEMS``),
ignoring case and extension. YOLOX (``yolox*``) is allowed; every other YOLO
name is not. Model files (``MODEL_EXTENSIONS``, and every file inside an
``.mlpackage`` or ``.mlmodelc``) are also searched for ``CONTENT_MARKERS``, so
a renamed Ultralytics export, which records its author in the model metadata,
is still caught.

Exit status: 0 when nothing forbidden is found, 1 when something is (each
finding is printed), 2 when a path is missing or unreadable. A path that cannot
be checked is never treated as clean.

Only the standard library is used, so this runs in any Python 3.10+ (CI,
Gradle or an Xcode build phase) without the conversion tools.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

#: Forbidden model families: a regular expression searched for in every path
#: component (lower case), and why the family must not ship.
FORBIDDEN_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("yolo", r"yolo(?!x)", "YOLO/Ultralytics models (AGPL-3.0 or Ultralytics licence); only YOLOX is allowed"),
    ("ultralytics", r"ultralytics", "Ultralytics models (AGPL-3.0 or Ultralytics licence)"),
    ("scrfd", r"scrfd", "InsightFace SCRFD face detector (non-commercial research weights)"),
    ("arcface", r"arcface", "InsightFace ArcFace face recogniser (non-commercial research weights)"),
    ("insightface", r"insightface", "InsightFace models (non-commercial research weights)"),
    ("buffalo", r"buffalo", "InsightFace model packs buffalo_* (non-commercial research weights)"),
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
    # files inside InsightFace's buffalo_* and antelope packs
    "det_500m": "InsightFace SCRFD detector (buffalo_sc)",
    "det_2.5g": "InsightFace SCRFD detector (buffalo_s)",
    "det_10g": "InsightFace SCRFD detector (buffalo_l)",
    "w600k_mbf": "InsightFace ArcFace recogniser (buffalo_sc)",
    "w600k_r50": "InsightFace ArcFace recogniser (buffalo_l)",
    "glintr100": "InsightFace recogniser (antelopev2)",
    "scrfd_10g_bnkps": "InsightFace SCRFD detector (antelopev2)",
    "1k3d68": "InsightFace landmark model",
    "2d106det": "InsightFace landmark model",
    "genderage": "InsightFace attribute model",
}

#: Byte strings (lower case) that mark a forbidden model inside a model file.
#: Ultralytics exports write "Ultralytics" into the ONNX, TFLite and Core ML metadata.
CONTENT_MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"ultralytics", "metadata names Ultralytics (AGPL-3.0 or Ultralytics licence)"),
    (b"insightface", "metadata names InsightFace (non-commercial research weights)"),
    (b"mobileclip", "metadata names MobileCLIP (research-only licence)"),
)

#: Extensions of files whose bytes are searched for CONTENT_MARKERS.
MODEL_EXTENSIONS = frozenset({
    ".onnx", ".ort", ".tflite", ".lite", ".pt", ".pth", ".ptl", ".torchscript", ".safetensors",
    ".mlmodel", ".param", ".mnn", ".engine", ".pb", ".h5", ".keras", ".bin", ".gguf", ".npz",
})
#: Folders whose every file is a model file (Core ML packages and compiled models).
MODEL_BUNDLE_EXTENSIONS = frozenset({".mlpackage", ".mlmodelc"})
#: Zip-based containers read entry by entry.
ARCHIVE_EXTENSIONS = frozenset({".apk", ".aab", ".ipa", ".zip", ".jar", ".aar", ".apks"})

_CHUNK = 8 << 20
_FAMILY_RES = tuple((name, re.compile(pattern), reason) for name, pattern, reason in FORBIDDEN_FAMILIES)
_MAX_MARKER = max(len(m) for m, _ in CONTENT_MARKERS)


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


def _is_model_path(parts: Iterable[str]) -> bool:
    parts = list(parts)
    if not parts:
        return False
    if _suffix(parts[-1]) in MODEL_EXTENSIONS:
        return True
    return any(_suffix(p) in MODEL_BUNDLE_EXTENSIONS for p in parts[:-1])


def scan_stream(stream: IO[bytes]) -> tuple[str, str] | None:
    """The first CONTENT_MARKERS hit in a binary stream, as (rule, reason), or None."""
    tail = b""
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            return None
        window = (tail + chunk).lower()
        for marker, reason in CONTENT_MARKERS:
            if marker in window:
                return f"content:{marker.decode()}", reason
        tail = window[-(_MAX_MARKER - 1):]


def _scan_zip(source: str | IO[bytes], label: str) -> Iterator[Finding]:
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise GuardError(f"{label}: not a readable zip archive ({exc})") from exc
    with archive:
        for info in archive.infolist():
            name = info.filename
            location = f"{label}!{name}"
            hit = check_parts(name)
            if hit:
                yield Finding(location, *hit)
                continue
            if info.is_dir():
                continue
            parts = PurePosixPath(name).parts
            try:
                if _is_model_path(parts):
                    with archive.open(info) as member:
                        hit = scan_stream(member)
                    if hit:
                        yield Finding(location, *hit)
                elif _suffix(name) in ARCHIVE_EXTENSIONS:
                    with archive.open(info) as member:
                        nested = member.read()
                    yield from _scan_zip(io.BytesIO(nested), location)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                raise GuardError(f"{location}: cannot be read ({exc})") from exc


def _scan_file(path: Path, rel_parts: tuple[str, ...], label: str) -> Iterator[Finding]:
    suffix = path.suffix.lower()
    if suffix in ARCHIVE_EXTENSIONS:
        yield from _scan_zip(str(path), label)
    elif _is_model_path(rel_parts):
        try:
            with path.open("rb") as fh:
                hit = scan_stream(fh)
        except OSError as exc:
            raise GuardError(f"{label}: cannot be read ({exc})") from exc
        if hit:
            yield Finding(label, *hit)


def scan_path(path: Path | str) -> list[Finding]:
    """Every forbidden item under one folder, archive or file."""
    path = Path(path)
    if not path.exists():
        raise GuardError(f"{path}: does not exist")
    findings: list[Finding] = []
    hit = check_name(path.name)
    if hit:
        findings.append(Finding(str(path), *hit))
    if path.is_file():
        findings.extend(_scan_file(path, (path.name,), str(path)))
        return findings
    if not path.is_dir():
        raise GuardError(f"{path}: neither a file nor a folder")

    def _raise(exc: OSError) -> None:
        raise GuardError(f"{exc.filename}: cannot be read ({exc.strerror})")

    root_parts = (path.name,)
    for dirpath, dirnames, filenames in os.walk(path, onerror=_raise):
        here = Path(dirpath)
        rel = here.relative_to(path).parts
        for d in sorted(dirnames):
            hit = check_name(d)
            if hit:
                findings.append(Finding(str(here / d), *hit))
                dirnames.remove(d)  # reported once; its contents add nothing
        for f in sorted(filenames):
            full = here / f
            hit = check_name(f)
            if hit:
                findings.append(Finding(str(full), *hit))
                continue
            findings.extend(_scan_file(full, root_parts + rel + (f,), str(full)))
    return findings


def scan(paths: Iterable[Path | str]) -> list[Finding]:
    """Every forbidden item under all ``paths``; raises GuardError if one cannot be checked."""
    findings: list[Finding] = []
    for p in paths:
        findings.extend(scan_path(p))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if a forbidden model is in app asset folders or a built APK, AAB, IPA or .app.",
    )
    parser.add_argument("paths", nargs="+", help="asset or model folders, .app/.xcarchive folders, or .apk/.aab/.ipa files")
    args = parser.parse_args(argv)
    try:
        findings = scan(args.paths)
    except GuardError as exc:
        print(f"licence guard: cannot check {exc}", file=sys.stderr)
        return 2
    if findings:
        print(f"licence guard: {len(findings)} forbidden model file(s) found:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print("Remove them before a store build. See docs/mobile-models.md.", file=sys.stderr)
        return 1
    print(f"licence guard: no forbidden model files in {len(args.paths)} path(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
