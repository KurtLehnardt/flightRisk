"""Draw the FlightRisk placeholder icon from code and write it as PNG files and a Windows .ico.

Only the standard library is used, so this runs with any Python 3.9 or newer. The icon is a dark
blue rounded square with a white quadcopter (four rotors joined by two arms) and an orange
search ring in the middle. The macOS .icns is made from the PNG files by ``build_icns`` below,
which calls the ``iconutil`` tool that ships with macOS.

Usage:
    python3 installer/assets/make_icon.py            # writes FlightRisk.icns and FlightRisk.ico here
    python3 installer/assets/make_icon.py --png out.png --size 512
"""

from __future__ import annotations

import argparse
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent

BACKGROUND = (22, 44, 84)
WHITE = (245, 247, 250)
ORANGE = (255, 140, 26)

#: The PNG sizes an .iconset needs (file name, pixel size).
ICONSET = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _rounded_square(x: float, y: float, half: float, radius: float) -> float:
    """Signed distance from (x, y) to a rounded square centred on the origin."""
    qx = abs(x) - (half - radius)
    qy = abs(y) - (half - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def _segment(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from (x, y) to the segment from (ax, ay) to (bx, by)."""
    px, py = x - ax, y - ay
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, (px * dx + py * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - t * dx, py - t * dy)


def _coverage(distance: float, pixel: float) -> float:
    """Anti-aliased coverage of a shape whose signed distance at the pixel centre is *distance*."""
    return max(0.0, min(1.0, 0.5 - distance / pixel))


def render(size: int) -> bytes:
    """The icon as raw RGBA rows (size x size), in a coordinate space from -1 to 1."""
    pixel = 2.0 / size
    rows = bytearray()
    rotor_centres = [(-0.42, -0.42), (0.42, -0.42), (-0.42, 0.42), (0.42, 0.42)]
    for j in range(size):
        rows.append(0)  # PNG filter type "none" for this row
        y = -1.0 + (j + 0.5) * pixel
        for i in range(size):
            x = -1.0 + (i + 0.5) * pixel
            base = _coverage(_rounded_square(x, y, 0.92, 0.2), pixel)
            if base <= 0.0:
                rows += b"\x00\x00\x00\x00"
                continue
            r, g, b = BACKGROUND
            arms = min(_segment(x, y, -0.42, -0.42, 0.42, 0.42), _segment(x, y, -0.42, 0.42, 0.42, -0.42)) - 0.05
            rotors = min(abs(math.hypot(x - cx, y - cy) - 0.17) - 0.035 for cx, cy in rotor_centres)
            body = math.hypot(x, y) - 0.2
            white = _coverage(min(arms, rotors, body), pixel)
            ring = _coverage(abs(math.hypot(x, y) - 0.12) - 0.04, pixel)
            r, g, b = (r + (WHITE[0] - r) * white, g + (WHITE[1] - g) * white, b + (WHITE[2] - b) * white)
            r, g, b = (r + (ORANGE[0] - r) * ring, g + (ORANGE[1] - g) * ring, b + (ORANGE[2] - b) * ring)
            rows += bytes((round(r), round(g), round(b), round(255 * base)))
    return bytes(rows)


def png_bytes(size: int) -> bytes:
    """A PNG file of the icon at *size* pixels."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b""))


def ico_bytes(sizes: list[int] | None = None) -> bytes:
    """A Windows .ico holding one PNG image per size (Windows Vista and later read PNG entries)."""
    sizes = sizes or ICO_SIZES
    images = [png_bytes(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in zip(sizes, images):
        dim = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images)


def build_icns(out: Path) -> None:
    """Write a macOS .icns at *out* with ``iconutil`` (macOS only)."""
    iconutil = shutil.which("iconutil")
    if iconutil is None:
        raise SystemExit("iconutil was not found; the .icns can only be built on macOS")
    cache: dict[int, bytes] = {}
    with tempfile.TemporaryDirectory() as work:
        iconset = Path(work) / "FlightRisk.iconset"
        iconset.mkdir()
        for name, size in ICONSET:
            if size not in cache:
                cache[size] = png_bytes(size)
            (iconset / name).write_bytes(cache[size])
        subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(out)], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--png", type=Path, help="write one PNG here instead of the .icns and .ico")
    parser.add_argument("--size", type=int, default=512, help="size of the --png image in pixels")
    parser.add_argument("--out-dir", type=Path, default=HERE, help="folder for FlightRisk.icns and FlightRisk.ico")
    args = parser.parse_args(argv)
    if args.png:
        args.png.write_bytes(png_bytes(args.size))
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "FlightRisk.ico").write_bytes(ico_bytes())
    if sys.platform == "darwin":
        build_icns(args.out_dir / "FlightRisk.icns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
