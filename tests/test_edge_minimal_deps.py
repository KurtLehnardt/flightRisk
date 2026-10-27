"""The Windows edge must run without InsightFace, Ollama, open_clip or MediaPipe (R-104)."""

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_CHECK = textwrap.dedent(
    """
    import importlib.abc
    import sys

    BLOCKED = ("insightface", "ollama", "open_clip", "mediapipe")

    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(f"{name} is blocked for this test")
            return None

    sys.meta_path.insert(0, Blocker())

    import argparse
    import flightrisk.edge_device as edge_device
    import flightrisk.transport
    import flightrisk.vision.detector
    import flightrisk.vision.reid
    import flightrisk.vision.face
    import flightrisk.drone.tello
    import flightrisk.drone.lane_executor
    import flightrisk.drone.safety
    from flightrisk.observability import StructuredLogger

    # Replace the detector so that no model weights are loaded, then run the
    # real runner and drone construction used by the tello and fake sources.
    class StubDetector:
        def __init__(self, *args, **kwargs):
            pass

    flightrisk.vision.detector.PersonDetector = StubDetector

    log = StructuredLogger("edge-minimal-deps-test")
    parser = edge_device._build_parser()
    for source in ("fake", "tello"):
        args = parser.parse_args(["--source", source, "--drone-id", "drone-9"])
        runner = edge_device._build_runner(args, log)
        assert runner is not None
        assert runner._reid is None, "reid must degrade to None without open_clip"
        assert runner._face is None, "face must degrade to None without insightface"
    fake_args = parser.parse_args(["--source", "fake", "--drone-id", "drone-9"])
    assert edge_device._build_drone(fake_args, log) is not None

    loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
    assert not loaded, loaded
    print("OK")
    """
)


def test_edge_device_imports_without_heavy_deps():
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "OK" in result.stdout


def test_edge_requirements_file_lists_only_edge_packages():
    lines = [
        line.split(">=")[0].strip().lower()
        for line in (ROOT / "requirements-edge.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines, "requirements-edge.txt must not be empty"
    for heavy in ("insightface", "ollama", "open-clip-torch", "mediapipe", "onnxruntime"):
        assert heavy not in lines
    full = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for name in lines:
        assert name in full.lower(), name


def test_install_script_uses_edge_requirements():
    script = (ROOT / "scripts" / "install_windows.ps1").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "windows-edge-setup.md").read_text(encoding="utf-8")
    assert "-r requirements-edge.txt" in script
    assert "-r requirements.txt" not in script
    assert "requirements-edge.txt" in doc
