"""Make square, full-bleed app icons from the FlightRisk artwork.

The artwork in ``branding/flightRiskIcon.png`` is a dark-blue rounded square with a white drone, a
light beam and a red pin, drawn on a light background with a soft shadow. App stores want a
full-bleed square instead: the store and the operating system apply their own corner mask, and the
App Store icon must not have an alpha channel. ``full_bleed_square`` finds the rounded square in the
artwork, moves in far enough that no light corner or shadow is left, and returns the largest square
crop that fits. The drone, its propellers, the beam and the pin stay whole; the script checks that
before it writes anything.

The crop is the same for every platform, so the Android launcher icon can be made from the same
function (``--size`` and ``--out``), or by importing ``full_bleed_square`` from this file.

Pillow is needed. The repository's development virtual environment has it:

    venv/bin/python branding/make_app_icon.py              # writes the iOS App Store icon
    venv/bin/python branding/make_app_icon.py --size 512 --out /tmp/icon-512.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageChops

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SOURCE = HERE / "flightRiskIcon.png"
IOS_APP_ICON = REPO / "ios" / "FlightRisk" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon-1024.png"

#: A pixel is part of the dark-blue square when its red channel is below this and its blue channel
#: is below ``DARK_BLUE_MAX``. The light background and shadow are well above both.
DARK_RED_MAX = 110
DARK_BLUE_MAX = 150
#: Extra pixels, in the artwork's scale, moved in past the computed corner so that the anti-aliased
#: rim and the shadow are cut away as well.
SAFETY_MARGIN = 16
#: A crop edge pixel brighter than this (mean of R, G and B) counts as background or shadow.
EDGE_LIGHT_LIMIT = 150


def _is_dark_blue(pixel: tuple[int, int, int]) -> bool:
    red, _, blue = pixel
    return red < DARK_RED_MAX and blue < DARK_BLUE_MAX


def _square_bounds(image: Image.Image) -> tuple[int, int, int, int]:
    """Left, top, right and bottom (inclusive) of the dark-blue square, measured through its middle."""
    pixels = image.load()
    width, height = image.size
    middle_row = [x for x in range(width) if _is_dark_blue(pixels[x, height // 2])]
    middle_column = [y for y in range(height) if _is_dark_blue(pixels[width // 2, y])]
    if not middle_row or not middle_column:
        raise ValueError("no dark-blue square found in the artwork")
    return middle_row[0], middle_column[0], middle_row[-1], middle_column[-1]


def _corner_inset(image: Image.Image, bounds: tuple[int, int, int, int]) -> int:
    """How far in from the square's edges the rounded corners reach, from the largest of the four corners.

    Walking diagonally in from a corner of the square's bounding box, the first dark-blue pixel is on the
    corner arc at ``r * (1 - 1/sqrt(2))`` from both edges, where ``r`` is the corner radius. A square crop
    inset by that much on every side has its own corners on the arc, so the result is that distance.
    """
    pixels = image.load()
    left, top, right, bottom = bounds
    corners = [(left, top, 1, 1), (right, top, -1, 1), (left, bottom, 1, -1), (right, bottom, -1, -1)]
    deepest = 0
    for x0, y0, dx, dy in corners:
        limit = min(right - left, bottom - top) // 2
        for step in range(limit):
            if _is_dark_blue(pixels[x0 + dx * step, y0 + dy * step]):
                deepest = max(deepest, step)
                break
        else:
            raise ValueError("a corner of the square was not found")
    return deepest


def _content_box(image: Image.Image, bounds: tuple[int, int, int, int], inset: int) -> tuple[int, int, int, int]:
    """Bounding box of the light artwork (drone, beam and pin) inside the square, past its rim."""
    left, top, right, bottom = bounds
    inner = image.crop((left + inset, top + inset, right - inset + 1, bottom - inset + 1))
    # Everything that is clearly not the dark-blue fill: a pixel with any channel above 150 is part
    # of the white drone, the pale beam or the red pin.
    channels = [inner.getchannel(name).point(lambda value: 255 if value > 150 else 0) for name in "RGB"]
    mask = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    box = mask.getbbox()
    if box is None:
        raise ValueError("no artwork found inside the square")
    return (box[0] + left + inset, box[1] + top + inset, box[2] + left + inset, box[3] + top + inset)


def full_bleed_square(source: Image.Image) -> Image.Image:
    """Return the largest square crop of the artwork that lies wholly inside its rounded square.

    The result is RGB (no alpha) at the artwork's own resolution; resize it for the target size.
    Raises ``ValueError`` if the crop would cut the drone or still show a light corner.
    """
    rgb = source.convert("RGB")
    bounds = _square_bounds(rgb)
    left, top, right, bottom = bounds
    inset = _corner_inset(rgb, bounds) + SAFETY_MARGIN
    side = min(right - left + 1, bottom - top + 1) - 2 * inset
    center_x = (left + right + 1) / 2
    center_y = (top + bottom + 1) / 2
    crop_left = round(center_x - side / 2)
    crop_top = round(center_y - side / 2)
    box = (crop_left, crop_top, crop_left + side, crop_top + side)

    content = _content_box(rgb, bounds, inset)
    if content[0] < box[0] or content[1] < box[1] or content[2] > box[2] or content[3] > box[3]:
        raise ValueError(f"the crop {box} would cut the artwork {content}")

    cropped = rgb.crop(box)
    _check_edges(cropped)
    return cropped


def _check_edges(image: Image.Image) -> None:
    """Fail if any pixel on the crop's border is light enough to be the background or the shadow."""
    pixels = image.load()
    width, height = image.size
    border = [(x, 0) for x in range(width)] + [(x, height - 1) for x in range(width)]
    border += [(0, y) for y in range(height)] + [(width - 1, y) for y in range(height)]
    for x, y in border:
        if sum(pixels[x, y]) / 3 > EDGE_LIGHT_LIMIT:
            raise ValueError(f"light pixel {pixels[x, y]} left on the crop edge at {(x, y)}")


def make_icon(source_path: Path, size: int, out_path: Path) -> Image.Image:
    """Crop, resize and save one icon as an RGB PNG. The colour profile of the artwork is kept."""
    with Image.open(source_path) as source:
        profile = source.info.get("icc_profile")
        square = full_bleed_square(source)
    icon = square.resize((size, size), Image.Resampling.LANCZOS)
    if icon.mode != "RGB":
        raise AssertionError("the icon must not have an alpha channel")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    options = {"optimize": True}
    if profile:
        options["icc_profile"] = profile
    icon.save(out_path, format="PNG", **options)
    return icon


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=SOURCE, help="the artwork (default: %(default)s)")
    parser.add_argument("--size", type=int, default=1024, help="edge length in pixels (default: %(default)s)")
    parser.add_argument("--out", type=Path, default=IOS_APP_ICON, help="output PNG (default: the iOS App Store icon)")
    args = parser.parse_args(argv)
    if args.size <= 0:
        parser.error("--size must be a positive number of pixels")
    icon = make_icon(args.source, args.size, args.out)
    print(f"wrote {args.out} ({icon.size[0]}x{icon.size[1]}, {icon.mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
