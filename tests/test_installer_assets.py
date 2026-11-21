"""installer/assets and installer/locks: the generated icon and the pinned dependency lists."""

from __future__ import annotations

import importlib.util
import re
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Loaded from its path: installer/ is a folder of scripts, not a package (and "installer" is also
# the name of a PyPA package that a package of ours would shadow).
_spec = importlib.util.spec_from_file_location("flightrisk_make_icon", ROOT / "installer" / "assets" / "make_icon.py")
make_icon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_icon)
LOCKS = ROOT / "installer" / "locks"


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0].lower().replace("_", "-"))
    return names


def _pins(path: Path) -> dict[str, str]:
    """name -> version for every requirement in a lock; each must be pinned with == and carry hashes."""
    pins = {}
    text = path.read_text().replace("\\\n", " ")
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        spec, *hashes = line.split("--hash=")
        name, _, version = spec.strip().partition("==")
        assert version, f"{path.name}: {spec!r} is not pinned with =="
        assert hashes and all(h.strip().startswith("sha256:") for h in hashes), f"{path.name}: {name} has no sha256"
        pins[name.lower().replace("_", "-")] = version
    return pins


def test_png_is_valid_and_sized():
    data = make_icon.png_bytes(32)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    _length, kind = struct.unpack(">I4s", data[8:16])
    width, height = struct.unpack(">II", data[16:24])
    assert (kind, width, height) == (b"IHDR", 32, 32)
    idat_start = data.index(b"IDAT") + 4
    idat_len = struct.unpack(">I", data[idat_start - 8:idat_start - 4])[0]
    raw = zlib.decompress(data[idat_start:idat_start + idat_len])
    assert len(raw) == 32 * (1 + 32 * 4)


def test_ico_holds_one_png_per_size():
    data = make_icon.ico_bytes([16, 256])
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 2)
    first = struct.unpack("<BBBBHHII", data[6:22])
    assert first[0] == 16 and data[first[7]:first[7] + 8] == b"\x89PNG\r\n\x1a\n"
    second = struct.unpack("<BBBBHHII", data[22:38])
    assert second[0] == 0, "256 pixels is written as 0 in an .ico directory entry"


def test_committed_icons_exist():
    assert (ROOT / "installer" / "assets" / "FlightRisk.icns").read_bytes()[:4] == b"icns"
    ico = (ROOT / "installer" / "assets" / "FlightRisk.ico").read_bytes()
    assert struct.unpack("<HHH", ico[:6]) == (0, 1, len(make_icon.ICO_SIZES))


def test_macos_lock_pins_every_requirement():
    pins = _pins(LOCKS / "macos-arm64.txt")
    missing = _requirement_names(ROOT / "requirements.txt") - set(pins)
    assert not missing, f"requirements.txt names not in the macOS lock: {sorted(missing)}"


def test_windows_edge_lock_pins_every_edge_requirement():
    pins = _pins(LOCKS / "windows-x64-edge.txt")
    missing = _requirement_names(ROOT / "requirements-edge.txt") - set(pins)
    assert not missing, f"requirements-edge.txt names not in the Windows lock: {sorted(missing)}"
    # The ground matches the target on the embedding the edge ships with each detection, so the edge needs it.
    assert "open-clip-torch" in pins, "an edge without open_clip sends reid_embedding null and never matches"
    for heavy in ("insightface", "ollama", "mavsdk", "flask"):
        assert heavy not in pins, f"the edge lock must not install {heavy}"


def test_windows_ground_lock_pins_every_ground_requirement_and_anthropic():
    pins = _pins(LOCKS / "windows-x64-ground.txt")
    for requirements in ("requirements-ground.txt", "requirements-edge.txt"):
        missing = _requirement_names(ROOT / requirements) - {"-r"} - set(pins)
        assert not missing, f"{requirements} names not in the Windows ground lock: {sorted(missing)}"
    for needed in ("anthropic", "flask", "flask-socketio"):
        assert needed in pins
    # PersonReID needs open_clip; without it app_state.reid is None and the target photo cannot be set.
    assert "open-clip-torch" in pins
    for heavy in ("insightface", "ollama", "mavsdk"):
        assert heavy not in pins, f"the ground lock must not install {heavy}"
    edge = set(_pins(LOCKS / "windows-x64-edge.txt"))
    assert edge <= set(pins), "the ground lock must contain everything the edge lock does"


def test_ground_lock_agrees_with_the_macos_lock_on_every_shared_pin():
    """Windows pins must be versions the macOS lock (the tested app) already runs, anthropic included."""
    mac = _pins(LOCKS / "macos-arm64.txt")
    for lock in ("windows-x64-ground.txt", "windows-x64-edge.txt"):
        win = _pins(LOCKS / lock)
        # Platform wheels differ per OS (torch builds, pywin32 and so on), but versions of shared packages must not.
        differ = {n: (mac[n], v) for n, v in win.items() if n in mac and mac[n] != v}
        assert not differ, f"{lock} pins other versions than the macOS lock: {differ}"
    assert "anthropic" in mac and _pins(LOCKS / "windows-x64-ground.txt")["anthropic"] == mac["anthropic"]
