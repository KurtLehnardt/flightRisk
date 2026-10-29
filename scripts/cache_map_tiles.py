#!/usr/bin/env python3
"""Download map tiles for offline use in the dashboard.

Run this on a computer with internet access before going to the field::

    venv/bin/python scripts/cache_map_tiles.py --provider usgs-imagery \\
        --center 40.6894,-111.7310 --radius-m 300 --min-zoom 14 --max-zoom 18

The tiles are stored under ``~/.flightrisk/tiles`` (or ``FLIGHTRISK_TILES_DIR``,
or ``--out``). Use ``--dry-run`` to see how many tiles a job needs without any
network access. The tool is deliberately small and polite; see
``docs/map-and-plan-editing.md`` section 6.4 for its limits. It exits with
status 2 and an explanation when it refuses a job.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flightrisk.dashboard import map_tiles


def _floats(text: str, count: int, what: str) -> tuple[float, ...]:
    parts = [p.strip() for p in text.split(",")]
    try:
        values = tuple(float(p) for p in parts)
    except ValueError:
        raise ValueError(f"{what} must be {count} comma-separated numbers.") from None
    if len(values) != count:
        raise ValueError(f"{what} must be {count} comma-separated numbers.")
    return values


def build_parser() -> argparse.ArgumentParser:
    cacheable = [p.id for p in map_tiles.PROVIDERS.values() if p.cacheable]
    parser = argparse.ArgumentParser(
        description="Download map tiles for offline use in the FlightRisk dashboard."
    )
    parser.add_argument("--provider", required=True, choices=cacheable)
    area = parser.add_mutually_exclusive_group(required=True)
    area.add_argument("--center", metavar="LAT,LON", help="Centre of the area; needs --radius-m.")
    area.add_argument("--bbox", metavar="S,W,N,E", help="South, west, north and east edges.")
    parser.add_argument("--radius-m", type=float, help="Half the side of the square around --center.")
    parser.add_argument("--min-zoom", type=int, required=True)
    parser.add_argument("--max-zoom", type=int, required=True)
    parser.add_argument("--out", type=Path, help="Tile directory (default: FLIGHTRISK_TILES_DIR or ~/.flightrisk/tiles).")
    parser.add_argument("--rate", type=float, default=2.0, help="Maximum requests per second (default 2).")
    parser.add_argument("--max-tiles", type=int, default=map_tiles.DEFAULT_MAX_TILES)
    parser.add_argument("--allow-large", action="store_true", help="Allow a larger area, zoom or tile count.")
    parser.add_argument("--dry-run", action="store_true", help="Print the tile count and directory; make no request.")
    parser.add_argument("--url-template", help="Tile URL of a self-hosted or licensed server, with {z} {x} {y}.")
    parser.add_argument("--key-env", help="Environment variable that holds the key for {key} in --url-template.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.center is not None:
            if args.radius_m is None:
                raise ValueError("--center needs --radius-m.")
            if args.radius_m <= 0:
                raise ValueError("--radius-m must be greater than zero.")
            lat, lon = _floats(args.center, 2, "--center")
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                raise ValueError("--center is not a valid latitude and longitude.")
            bbox = map_tiles.bbox_from_center(lat, lon, args.radius_m)
        else:
            if args.radius_m is not None:
                raise ValueError("--radius-m is used with --center only.")
            bbox = _floats(args.bbox, 4, "--bbox")
        out_dir = args.out if args.out is not None else map_tiles.tiles_dir()
        if args.dry_run:
            if not 0 <= args.min_zoom <= args.max_zoom <= map_tiles.MAX_ZOOM_HARD:
                raise ValueError("The zoom range must satisfy 0 <= min <= max <= 20.")
            total = map_tiles.count_tiles(bbox, args.min_zoom, args.max_zoom)
            print(f"{total} tiles would be downloaded into {Path(out_dir) / args.provider}")
            return 0
        report = map_tiles.download_area(
            args.provider,
            bbox,
            args.min_zoom,
            args.max_zoom,
            out_dir=out_dir,
            rate_per_s=args.rate,
            max_tiles=args.max_tiles,
            allow_large=args.allow_large,
            url_template=args.url_template,
            key_env=args.key_env,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"Summary: {report.requested} tiles requested, {report.downloaded} downloaded, "
        f"{report.skipped_existing} already present, {report.failed} failed, "
        f"{report.bytes} bytes."
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
