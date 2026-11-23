"""Generate the FlightRisk Android launcher icons and the Play Store icon.

The source artwork is branding/flightRiskIcon.png: a rounded dark-blue square
with a light margin and a soft shadow around it, and a white drone, a light
search beam and a red location pin inside it.

This script writes:

* A full-bleed square crop of the artwork, taken inside the rounded rectangle
  and inset far enough that no light corner or shadow pixel remains.
* The adaptive icon background: a vector drawable with the vertical blue
  gradient of the artwork (res/drawable/ic_launcher_background.xml).
* The adaptive icon foreground: the drone, beam, pin and dot on a transparent
  canvas, with every important part inside the 66dp safe-zone circle of the
  108dp canvas (res/mipmap-<density>/ic_launcher_foreground.png).
* The adaptive icon monochrome layer: a white silhouette of the drone, the pin
  and the dot, without the beam (res/mipmap-<density>/ic_launcher_monochrome.png).
* The legacy square and round launcher icons at every density, rendered from
  the adaptive layers the way a launcher shows them
  (res/mipmap-<density>/ic_launcher.png and ic_launcher_round.png).
* The 512x512 Play Store icon (branding/play_store_icon_512.png).

Run it from anywhere with a Python that has Pillow, for example:

    ~/work/flightRisk/venv/bin/python branding/generate_android_icons.py

It is safe to run again: every output is rewritten from the source artwork.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "branding" / "flightRiskIcon.png"
PLAY_ICON = REPO / "branding" / "play_store_icon_512.png"
RES = REPO / "mobile" / "app" / "src" / "main" / "res"

# Density bucket -> (adaptive layer size in px for 108dp, legacy icon size in px for 48dp).
DENSITIES = {
    "mdpi": (108, 48),
    "hdpi": (162, 72),
    "xhdpi": (216, 96),
    "xxhdpi": (324, 144),
    "xxxhdpi": (432, 192),
}

# The adaptive layers are drawn on a large master canvas and then scaled down.
MASTER = 1024
CANVAS_DP = 108.0
# The safe zone is a circle of 66dp diameter; important content stays 1dp inside it.
CONTENT_RADIUS_DP = 32.0
# The launcher shows the central 72dp of the 108dp layers.
VIEWPORT_DP = 72.0

# Extra inset, in source pixels, beyond the point where the corner arc meets the
# diagonal. It moves the crop corners clear of the anti-aliased rim and shadow.
CORNER_MARGIN = 40


# ---------------------------------------------------------------------------
# Locating the rounded rectangle and cropping it
# ---------------------------------------------------------------------------

def is_blue(pixel: tuple[int, ...]) -> bool:
    """True for the dark-blue card colour, false for the light margin and shadow."""
    r, g, b = pixel[:3]
    return b > r + 25 and (r + g + b) < 450


def find_card(img: Image.Image) -> tuple[int, int, int, int, float]:
    """Find the rounded rectangle from the pixels.

    Returns the card's left, top, right and bottom edges (inclusive) and the
    estimated corner radius. The edges come from scans through the middle row
    and column; the radius comes from where each corner arc meets the diagonal,
    which for a circular corner of radius r lies r * (1 - 1/sqrt(2)) in from
    each edge.
    """
    px = img.load()
    w, h = img.size
    mid_row = [x for x in range(w) if is_blue(px[x, h // 2])]
    mid_col = [y for y in range(h) if is_blue(px[w // 2, y])]
    left, right = mid_row[0], mid_row[-1]
    top, bottom = mid_col[0], mid_col[-1]

    insets = []
    corners = {
        (left, top): (1, 1),
        (right, top): (-1, 1),
        (left, bottom): (1, -1),
        (right, bottom): (-1, -1),
    }
    for (cx, cy), (dx, dy) in corners.items():
        t = 0
        while not is_blue(px[cx + dx * t, cy + dy * t]):
            t += 1
        insets.append(t)
    radius = max(insets) / (1 - 1 / math.sqrt(2))
    return left, top, right, bottom, radius


def full_bleed_crop(img: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Crop the largest centred square whose corners are well inside the card."""
    left, top, right, bottom, radius = find_card(img)
    inset = math.ceil(radius * (1 - 1 / math.sqrt(2))) + CORNER_MARGIN
    side = min(right - left, bottom - top) + 1 - 2 * inset
    cx = (left + right + 1) / 2
    cy = (top + bottom + 1) / 2
    x0 = round(cx - side / 2)
    y0 = round(cy - side / 2)
    box = (x0, y0, x0 + side, y0 + side)
    print(f"card x={left}..{right} y={top}..{bottom}, corner radius ~{radius:.0f}px, "
          f"inset {inset}px, crop box {box} ({side}px square)")
    return img.crop(box), box


def check_corners(crop: Image.Image, background) -> None:
    """Fail loudly if any crop corner is lighter than the card background."""
    px = crop.load()
    s = crop.size[0]
    for x, y in [(0, 0), (s - 1, 0), (0, s - 1), (s - 1, s - 1), (3, 3), (s - 4, s - 4)]:
        r, g, b = px[x, y][:3]
        br, bg, bb = background(y)
        distance = max(abs(r - br), abs(g - bg), abs(b - bb))
        print(f"corner ({x},{y}) = {(r, g, b)}, distance from card colour {distance}")
        if distance > 12 or not is_blue((r, g, b)):
            raise SystemExit(f"crop corner ({x},{y}) is not card blue: {(r, g, b)}")


# ---------------------------------------------------------------------------
# Separating the content from the blue gradient
# ---------------------------------------------------------------------------

def fit_background(crop: Image.Image):
    """Fit the card's vertical gradient as a straight line per colour channel.

    The colour is sampled in narrow strips at the left and right edges of the
    crop, where there is no content, and the per-row medians are fitted with a
    least-squares line. The returned function gives the colour at any row,
    including rows outside the crop (by extending the line).
    """
    px = crop.load()
    s = crop.size[0]
    strip = 40
    ys, samples = [], []
    for y in range(s):
        row = [px[x, y][:3] for x in list(range(0, strip)) + list(range(s - strip, s))]
        row = [p for p in row if is_blue(p)]
        if not row:
            continue
        ys.append(y)
        samples.append(tuple(sorted(c[i] for c in row)[len(row) // 2] for i in range(3)))

    n = len(ys)
    mean_y = sum(ys) / n
    var_y = sum((y - mean_y) ** 2 for y in ys)
    lines = []
    for ch in range(3):
        values = [p[ch] for p in samples]
        mean_v = sum(values) / n
        slope = sum((y - mean_y) * (v - mean_v) for y, v in zip(ys, values)) / var_y
        lines.append((mean_v - slope * mean_y, slope))

    def colour(y: float) -> tuple[float, float, float]:
        return tuple(a + b * y for a, b in lines)

    residual = max(
        max(abs(p[ch] - colour(y)[ch]) for ch in range(3)) for y, p in zip(ys, samples)
    )
    print(f"background gradient top {tuple(round(c) for c in colour(0))}, "
          f"bottom {tuple(round(c) for c in colour(s - 1))}, max fit residual {residual:.1f}")
    return colour


def unblend(crop: Image.Image, background) -> Image.Image:
    """Turn the crop into content with alpha over a transparent background.

    For each pixel the smallest alpha that explains its colour as content laid
    over the fitted background is used, and the content colour is solved from
    it. Composited over the same background the result matches the artwork.
    A small alpha floor removes compression noise from the plain card areas.
    """
    s = crop.size[0]
    src = crop.load()
    out = Image.new("RGBA", crop.size, (0, 0, 0, 0))
    dst = out.load()
    floor = 0.05
    for y in range(s):
        bgc = background(y)
        for x in range(s):
            c = src[x, y][:3]
            a = 0.0
            for ch in range(3):
                b = bgc[ch]
                if c[ch] > b:
                    a = max(a, (c[ch] - b) / max(255.0 - b, 1.0))
                elif c[ch] < b:
                    a = max(a, (b - c[ch]) / max(b, 1.0))
            a = (a - floor) / (1 - floor)
            if a <= 0:
                continue
            a = min(a, 1.0)
            colour = tuple(
                max(0, min(255, round(bgc[ch] + (c[ch] - bgc[ch]) / a))) for ch in range(3)
            )
            dst[x, y] = colour + (round(a * 255),)
    return out


def masks(crop: Image.Image, content: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Build the important-content mask and the monochrome silhouette.

    White parts (the drone, the propellers and the dot below the pin) are found
    as near-white pixels, grown by a few pixels so their anti-aliased edges are
    kept with the content alpha. The pin is found by its redness, so the white
    dot inside the pin stays a hole in the silhouette. The beam is left out of
    the silhouette.
    """
    s = crop.size[0]
    src = crop.load()
    white_core = Image.new("L", crop.size, 0)
    red = Image.new("L", crop.size, 0)
    wpx, rpx = white_core.load(), red.load()
    for y in range(s):
        for x in range(s):
            r, g, b = src[x, y][:3]
            if min(r, g, b) >= 240:
                wpx[x, y] = 255
            redness = r - (g + b) / 2
            rpx[x, y] = max(0, min(255, round((redness - 20) / 80 * 255)))

    # Drop isolated specks, then grow the white parts over their soft edges.
    white_core = white_core.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    white_zone = white_core.filter(ImageFilter.MaxFilter(7))
    white = ImageChops.multiply(white_zone, content.getchannel("A"))

    # Inside the pin's box the silhouette follows the red only.
    pin_box = red.point(lambda v: 255 if v > 128 else 0).getbbox()
    if pin_box:
        x0, y0, x1, y1 = pin_box
        pad = 6
        hole = Image.new("L", crop.size, 255)
        ImageDraw.Draw(hole).rectangle((x0 - pad, y0 - pad, x1 + pad, y1 + pad), fill=0)
        white = ImageChops.multiply(white, hole)
    silhouette = ImageChops.lighter(white, red)
    important = silhouette.point(lambda v: 255 if v > 128 else 0)
    return important, silhouette


# ---------------------------------------------------------------------------
# Placing the content on the 108dp canvas
# ---------------------------------------------------------------------------

def enclosing_circle(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Smallest circle around a set of points (Welzl's algorithm, iterative)."""
    import random

    pts = points[:]
    random.Random(7).shuffle(pts)

    def circle_two(a, b):
        cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        return cx, cy, math.dist(a, (cx, cy))

    def circle_three(a, b, c):
        ax, ay = a
        bx, by = b
        cx, cy = c
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-12:
            return None
        ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
              + (cx * cx + cy * cy) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
              + (cx * cx + cy * cy) * (bx - ax)) / d
        return ux, uy, math.dist(a, (ux, uy))

    def inside(c, p):
        return c is not None and math.dist((c[0], c[1]), p) <= c[2] + 1e-7

    c = None
    for i, p in enumerate(pts):
        if inside(c, p):
            continue
        c = (p[0], p[1], 0.0)
        for j in range(i):
            q = pts[j]
            if inside(c, q):
                continue
            c = circle_two(p, q)
            for k in range(j):
                r = pts[k]
                if inside(c, r):
                    continue
                c = circle_three(p, q, r) or c
    return c


def hull_points(mask: Image.Image) -> list[tuple[float, float]]:
    """The leftmost and rightmost set pixel of every row: enough for the circle."""
    w, h = mask.size
    px = mask.load()
    pts = []
    for y in range(h):
        xs = [x for x in range(w) if px[x, y]]
        if xs:
            pts.append((xs[0] + 0.5, y + 0.5))
            pts.append((xs[-1] + 0.5, y + 0.5))
    return pts


def place(layer: Image.Image, centre: tuple[float, float], scale: float) -> tuple[Image.Image, int]:
    """Scale a crop-space layer and put the given crop point at the canvas centre."""
    size = round(layer.size[0] * scale)
    scaled = layer.resize((size, size), Image.LANCZOS)
    canvas = Image.new(layer.mode, (MASTER, MASTER), 0 if layer.mode == "L" else (0, 0, 0, 0))
    ox = round(MASTER / 2 - centre[0] * scale)
    oy = round(MASTER / 2 - centre[1] * scale)
    canvas.paste(scaled, (ox, oy))
    return canvas, oy


# ---------------------------------------------------------------------------
# Writing the outputs
# ---------------------------------------------------------------------------

def hex_colour(c) -> str:
    return "#FF%02X%02X%02X" % tuple(max(0, min(255, round(v))) for v in c)


def write_background_vector(top, bottom) -> None:
    """Write the gradient background as a 108dp vector drawable."""
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Generated by branding/generate_android_icons.py from branding/flightRiskIcon.png. -->
<!-- The vertical blue gradient of the artwork, extended over the full 108dp layer. -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:aapt="http://schemas.android.com/aapt"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <path android:pathData="M0,0h108v108H0z">
        <aapt:attr name="android:fillColor">
            <gradient
                android:type="linear"
                android:startX="54"
                android:startY="0"
                android:endX="54"
                android:endY="108"
                android:startColor="{hex_colour(top)}"
                android:endColor="{hex_colour(bottom)}" />
        </aapt:attr>
    </path>
</vector>
"""
    (RES / "drawable" / "ic_launcher_background.xml").write_text(xml)


def gradient_image(size: int, top, bottom) -> Image.Image:
    """The same gradient as the vector background, as a bitmap."""
    img = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        draw.line([(0, y), (size, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img


def rounded_mask(size: int, radius_fraction: float, scale: int = 4) -> Image.Image:
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1), radius=round(size * scale * radius_fraction), fill=255
    )
    return big.resize((size, size), Image.LANCZOS)


def circle_mask(size: int, scale: int = 4) -> Image.Image:
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def main() -> None:
    art = Image.open(SOURCE).convert("RGBA")
    crop, _ = full_bleed_crop(art)
    crop_rgb = crop.convert("RGB")
    background = fit_background(crop_rgb)
    check_corners(crop_rgb, background)

    # Play Store icon: the full-bleed crop itself. Google Play applies its own mask.
    crop_rgb.resize((512, 512), Image.LANCZOS).convert("RGBA").save(PLAY_ICON, optimize=True)

    content = unblend(crop_rgb, background)
    important, silhouette = masks(crop_rgb, content)

    cx, cy, radius = enclosing_circle(hull_points(important))
    px_per_dp = MASTER / CANVAS_DP
    scale = CONTENT_RADIUS_DP * px_per_dp / radius
    print(f"important content circle centre ({cx:.0f},{cy:.0f}) radius {radius:.0f}px in crop; "
          f"crop drawn at {crop.size[0] * scale / px_per_dp:.1f}dp on the 108dp canvas")

    foreground, offset_y = place(content, (cx, cy), scale)
    monochrome_alpha, _ = place(silhouette, (cx, cy), scale)
    monochrome = Image.new("RGBA", (MASTER, MASTER), (255, 255, 255, 0))
    monochrome.putalpha(monochrome_alpha)

    # Extend the fitted gradient over the whole canvas: canvas row y shows crop row (y - offset_y) / scale.
    top = background((0 - offset_y) / scale)
    bottom = background((MASTER - 1 - offset_y) / scale)
    write_background_vector(top, bottom)
    back = gradient_image(MASTER, top, bottom).convert("RGBA")
    composite = Image.alpha_composite(back, foreground)

    # The launcher shows the central 72dp of the 108dp layers.
    view = round(VIEWPORT_DP * px_per_dp)
    edge = (MASTER - view) // 2
    viewport = composite.crop((edge, edge, edge + view, edge + view))

    for density, (layer_px, legacy_px) in DENSITIES.items():
        folder = RES / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        foreground.resize((layer_px, layer_px), Image.LANCZOS).save(folder / "ic_launcher_foreground.png", optimize=True)
        monochrome.resize((layer_px, layer_px), Image.LANCZOS).save(folder / "ic_launcher_monochrome.png", optimize=True)

        legacy = viewport.resize((legacy_px, legacy_px), Image.LANCZOS)
        square = legacy.copy()
        square.putalpha(ImageChops.multiply(legacy.getchannel("A"), rounded_mask(legacy_px, 0.12)))
        square.save(folder / "ic_launcher.png", optimize=True)
        round_icon = legacy.copy()
        round_icon.putalpha(ImageChops.multiply(legacy.getchannel("A"), circle_mask(legacy_px)))
        round_icon.save(folder / "ic_launcher_round.png", optimize=True)

    print("wrote", PLAY_ICON.relative_to(REPO), "and the launcher icons under", RES.relative_to(REPO))


if __name__ == "__main__":
    main()
