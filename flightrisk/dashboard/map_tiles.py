"""Offline map tile cache for the dashboard's lat/lon map.

The dashboard map shows imagery from a few providers. This module holds the
provider list, the Web Mercator tile arithmetic, the on-disk cache layout, the
Flask blueprint that serves cached tiles to the browser, and a polite
downloader that fills the cache ahead of a field trip. See
``docs/map-and-plan-editing.md`` section 6 for the terms of each provider.

Cache layout::

    <tiles dir>/<provider>/<z>/<x>/<y>.<png|jpg>
    <tiles dir>/<provider>/manifest.json

``<tiles dir>`` is ``FLIGHTRISK_TILES_DIR`` when set and ``~/.flightrisk/tiles``
otherwise. The manifest records, for each download run, the area, the zoom
range, the number of tiles present afterwards and the time of the run.

Safety properties:

* The serving route never fetches anything. A tile that is not on disk is a
  404, and the browser then loads it straight from the provider.
* Every path the route reads is built from a registered provider id and
  integers in range, resolved, and checked to lie inside the provider's
  directory, so no other file can be read, even through a symbolic link.
* The downloader refuses providers whose terms forbid caching, refuses large
  jobs unless explicitly allowed, sends one request at a time at a limited
  rate, accepts only small image responses, and never prints or stores an API
  key.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, send_file

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TileProvider:
    """A source of map tiles that the browser can show.

    ``url_template`` is the remote Leaflet-style template with ``{z}``,
    ``{x}`` and ``{y}``. ``cacheable`` is true only when the provider's terms
    allow keeping tiles for offline use; the cache tool and the serving route
    refuse every other provider.
    """

    id: str
    name: str
    kind: str
    url_template: str
    attribution: str
    max_zoom: int
    cacheable: bool


#: The registered providers, in the order the browser lists them. The USGS zoom
#: limits were measured against the live service on 2026-10-29 near 40.69 N,
#: 111.73 W: imagery answered down to zoom 16 and returned 404 from zoom 17.
PROVIDERS: dict[str, TileProvider] = {
    p.id: p
    for p in (
        TileProvider(
            id="usgs-imagery",
            name="Satellite (USGS)",
            kind="satellite",
            url_template=(
                "https://basemap.nationalmap.gov/arcgis/rest/services/"
                "USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
            ),
            attribution="Imagery: U.S. Geological Survey, The National Map",
            max_zoom=16,
            cacheable=True,
        ),
        TileProvider(
            id="usgs-topo",
            name="Topo map (USGS)",
            kind="map",
            url_template=(
                "https://basemap.nationalmap.gov/arcgis/rest/services/"
                "USGSTopo/MapServer/tile/{z}/{y}/{x}"
            ),
            attribution="Map: U.S. Geological Survey, The National Map",
            max_zoom=16,
            cacheable=True,
        ),
        TileProvider(
            id="osm",
            name="Street map (OpenStreetMap)",
            kind="map",
            url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            attribution='© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            max_zoom=19,
            cacheable=False,
        ),
    )
}

#: Environment variable that replaces the default tile directory.
TILES_DIR_ENV = "FLIGHTRISK_TILES_DIR"

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")

#: Content types the cache accepts, mapped to the file extension used on disk.
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg"}
_MIMETYPES = {"png": "image/png", "jpg": "image/jpeg"}

#: Highest zoom level that the tile arithmetic and the cache accept.
_MAX_VALID_ZOOM = 22

#: Mean metres per degree of latitude, used for approximate sizes.
_METRES_PER_DEGREE = 111_320.0

#: Latitude limit of the Web Mercator projection.
_MAX_LAT = 85.0511

MANIFEST_NAME = "manifest.json"


def tiles_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The directory that holds the tile cache.

    It is ``FLIGHTRISK_TILES_DIR`` when that variable is set and not empty,
    and ``~/.flightrisk/tiles`` otherwise. ``environ`` replaces
    ``os.environ`` for tests.
    """
    env = os.environ if environ is None else environ
    configured = env.get(TILES_DIR_ENV, "")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".flightrisk" / "tiles"


# ---------------------------------------------------------------------------
# Web Mercator tile arithmetic
# ---------------------------------------------------------------------------


def latlon_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    """The ``(x, y)`` of the tile at zoom ``z`` that contains ``lat``, ``lon``.

    The latitude is clamped to the Web Mercator limit of 85.0511 degrees and
    the result is clamped into the valid tile range, so a point exactly on the
    antimeridian or a pole still names a real tile.
    """
    n = 2**z
    lat = max(-_MAX_LAT, min(_MAX_LAT, lat))
    lat_rad = math.radians(lat)
    x = math.floor((lon + 180.0) / 360.0 * n)
    y = math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """The ``(south, west, north, east)`` bounds of tile ``x``, ``y`` at zoom ``z``."""
    n = 2**z

    def lat_of(row: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * row / n))))

    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    return lat_of(y + 1), west, lat_of(y), east


def bbox_from_center(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """The ``(south, west, north, east)`` box that spans ``radius_m`` around a point.

    The box is a square of side ``2 * radius_m``. It is approximate (a
    spherical Earth), which is accurate enough to choose tiles. Latitudes are
    clamped to the Web Mercator limit and longitudes to +-180.
    """
    dlat = radius_m / _METRES_PER_DEGREE
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = radius_m / (_METRES_PER_DEGREE * cos_lat)
    return (
        max(-_MAX_LAT, lat - dlat),
        max(-180.0, lon - dlon),
        min(_MAX_LAT, lat + dlat),
        min(180.0, lon + dlon),
    )


def _tile_ranges(
    bbox: tuple[float, float, float, float], z: int
) -> tuple[range, range]:
    south, west, north, east = bbox
    x0, y0 = latlon_to_tile(north, west, z)
    x1, y1 = latlon_to_tile(south, east, z)
    return range(min(x0, x1), max(x0, x1) + 1), range(min(y0, y1), max(y0, y1) + 1)


def tiles_for_bbox(
    bbox: tuple[float, float, float, float], zmin: int, zmax: int
) -> Iterator[tuple[int, int, int]]:
    """Yield ``(z, x, y)`` for every tile that touches ``bbox`` from ``zmin`` to ``zmax``."""
    for z in range(zmin, zmax + 1):
        xs, ys = _tile_ranges(bbox, z)
        for x in xs:
            for y in ys:
                yield z, x, y


def count_tiles(bbox: tuple[float, float, float, float], zmin: int, zmax: int) -> int:
    """The number of tiles ``tiles_for_bbox`` yields, computed without listing them."""
    total = 0
    for z in range(zmin, zmax + 1):
        xs, ys = _tile_ranges(bbox, z)
        total += len(xs) * len(ys)
    return total


# ---------------------------------------------------------------------------
# Reading the cache
# ---------------------------------------------------------------------------


def _cacheable_provider(provider_id: object) -> TileProvider | None:
    """The registered cacheable provider named ``provider_id``, or ``None``."""
    if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.match(provider_id):
        return None
    provider = PROVIDERS.get(provider_id)
    if provider is None or not provider.cacheable:
        return None
    return provider


def find_tile(provider_id: str, z: int, x: int, y: int, base: Path | None = None) -> Path | None:
    """The cached file for a tile, or ``None`` when it is absent or the request is invalid.

    ``provider_id`` must be a registered cacheable provider, ``z`` an integer
    from 0 to 22 and ``x`` and ``y`` integers below ``2**z``. The path is built
    from those validated values only, resolved, and required to lie inside the
    provider's directory, so a symbolic link that points elsewhere is refused.
    """
    provider = _cacheable_provider(provider_id)
    if provider is None:
        return None
    for value in (z, x, y):
        if isinstance(value, bool) or not isinstance(value, int):
            return None
    if not 0 <= z <= _MAX_VALID_ZOOM or not (0 <= x < 2**z and 0 <= y < 2**z):
        return None
    root = (tiles_dir() if base is None else Path(base)) / provider.id
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError):
        return None
    for ext in ("png", "jpg"):
        candidate = root / str(z) / str(x) / f"{y}.{ext}"
        try:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(resolved_root):
                continue
            if resolved.is_file():
                return resolved
        except (OSError, RuntimeError):
            continue
    return None


#: Largest number of tiles for which ``cache_status`` scans the disk.
STATUS_SCAN_LIMIT = 4096


def cache_status(
    provider_id: str,
    bbox: tuple[float, float, float, float],
    zoom: int,
    base: Path | None = None,
) -> dict[str, Any]:
    """How many tiles of ``bbox`` at ``zoom`` the cache holds for a provider.

    The result has ``total`` (tiles that cover the box), ``present`` (those on
    disk) and ``fraction`` (``present / total``). When the box needs more than
    4096 tiles the disk is not scanned: ``present`` and ``fraction`` are
    ``None`` and ``too_large`` is true.
    """
    total = count_tiles(bbox, zoom, zoom)
    if total > STATUS_SCAN_LIMIT:
        return {
            "provider": provider_id,
            "zoom": zoom,
            "total": total,
            "present": None,
            "fraction": None,
            "too_large": True,
        }
    present = sum(1 for z, x, y in tiles_for_bbox(bbox, zoom, zoom) if find_tile(provider_id, z, x, y, base))
    return {
        "provider": provider_id,
        "zoom": zoom,
        "total": total,
        "present": present,
        "fraction": present / total if total else 0.0,
    }


def _read_manifest(provider_dir: Path) -> dict[str, Any] | None:
    """The parsed manifest of a provider directory, or ``None`` when absent or unreadable."""
    try:
        data = json.loads((provider_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def providers_payload(base: Path | None = None) -> list[dict[str, Any]]:
    """The provider list for the browser, with the areas cached for each provider.

    Each entry has ``id``, ``name``, ``kind``, ``url`` (the remote template),
    ``attribution``, ``max_zoom``, ``cacheable`` and ``cached_areas`` (the
    manifest's areas, empty when there is no manifest or it cannot be read).
    This function never raises because of the contents of the cache.
    """
    root = tiles_dir() if base is None else Path(base)
    payload = []
    for provider in PROVIDERS.values():
        areas: list[Any] = []
        if provider.cacheable:
            manifest = _read_manifest(root / provider.id)
            raw = manifest.get("areas") if manifest else None
            if isinstance(raw, list):
                areas = [a for a in raw if isinstance(a, dict)]
        payload.append(
            {
                "id": provider.id,
                "name": provider.name,
                "kind": provider.kind,
                "url": provider.url_template,
                "attribution": provider.attribution,
                "max_zoom": provider.max_zoom,
                "cacheable": provider.cacheable,
                "cached_areas": areas,
            }
        )
    return payload


# ---------------------------------------------------------------------------
# Serving route
# ---------------------------------------------------------------------------

tiles_bp = Blueprint("tiles", __name__)


@tiles_bp.route("/tiles/<provider>/<int:z>/<int:x>/<int:y>", methods=["GET"])
def serve_tile(provider: str, z: int, x: int, y: int):
    """Serve one cached tile, or answer 404 so the browser loads it from the provider.

    The route reads only the local cache and never fetches anything. It adds
    no CORS headers and is covered by the dashboard's API-key gate like every
    other route.
    """
    path = find_tile(provider, z, x, y)
    if path is None:
        response = jsonify({"error": "not cached"})
        response.status_code = 404
        return response
    response = send_file(
        path,
        mimetype=_MIMETYPES.get(path.suffix.lstrip("."), "application/octet-stream"),
        conditional=True,
    )
    # private: the route may sit behind the API key, so no shared cache keeps it.
    response.headers["Cache-Control"] = "private, max-age=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------

USER_AGENT = "FlightRisk-tile-cache/1.0 (+https://github.com/KurtLehnardt/flightRisk)"

#: Hosts whose terms forbid bulk tile downloads. A leading dot matches the
#: host and every subdomain; a name without one matches only itself.
_DENIED_HOSTS = (
    ".tile.openstreetmap.org",
    "tile.openstreetmap.org",
    "server.arcgisonline.com",
    "services.arcgisonline.com",
    ".arcgis.com",
    "arcgis.com",
    ".opentopomap.org",
    "opentopomap.org",
)

REQUEST_TIMEOUT_S = 15
MAX_TILE_BYTES = 2 * 1024 * 1024
MAX_RETRIES = 3
RETRY_AFTER_CAP_S = 60.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Largest area side, in metres, allowed without ``allow_large``. The side is
#: approximate, so a tolerance keeps a 2 km radius (a 4 km box) allowed.
MAX_SIDE_M = 4000.0
_SIDE_TOLERANCE = 1.005
#: Highest zoom allowed without ``allow_large``, and the highest ever allowed.
MAX_ZOOM_DEFAULT = 19
MAX_ZOOM_HARD = 20
#: Tile count limits: the default, and the limit that ``allow_large`` cannot exceed.
DEFAULT_MAX_TILES = 2000
HARD_MAX_TILES = 20_000


@dataclass
class DownloadReport:
    """What a download run did, in tiles and bytes."""

    requested: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    failed: int = 0
    bytes: int = 0


def host_is_denied(host: str) -> bool:
    """Whether ``host`` belongs to a provider whose terms forbid bulk downloads."""
    host = host.lower().rstrip(".")
    for entry in _DENIED_HOSTS:
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def redact_url(url: str, key: str | None = None) -> str:
    """``url`` without its query string and fragment, and with ``key`` replaced by ``***``."""
    parts = urllib.parse.urlsplit(url)
    text = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if key:
        for variant in (key, urllib.parse.quote(key, safe=""), urllib.parse.quote(key)):
            text = text.replace(variant, "***")
    return text


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to http or https hosts that are not on the deny list."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme not in ("http", "https") or host_is_denied(parts.hostname or ""):
            raise urllib.error.HTTPError(
                req.full_url, code, "redirect to a refused location", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_CheckedRedirects())


def _validate_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    try:
        south, west, north, east = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise ValueError("The area must be four numbers: south, west, north, east.") from exc
    if not all(math.isfinite(v) for v in (south, west, north, east)):
        raise ValueError("The area contains a value that is not a finite number.")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise ValueError("Latitude must be between -90 and 90 degrees.")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise ValueError("Longitude must be between -180 and 180 degrees.")
    if south > north:
        raise ValueError("The south edge of the area is north of its north edge.")
    if west > east:
        raise ValueError(
            "The west edge of the area is east of its east edge. Areas that cross the "
            "antimeridian are not supported."
        )
    return south, west, north, east


def _area_size_m(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    """The approximate ``(width, height)`` of ``bbox`` in metres."""
    south, west, north, east = bbox
    height = (north - south) * _METRES_PER_DEGREE
    width = (east - west) * _METRES_PER_DEGREE * math.cos(math.radians((north + south) / 2.0))
    return abs(width), abs(height)


#: Hosts a ``--url-template`` may name while the tiles are stored and
#: attributed as one of the built-in providers. Tiles from any other server
#: must be stored under their own ``--custom-id``.
BUILT_IN_TEMPLATE_HOSTS: tuple[str, ...] = ("basemap.nationalmap.gov",)
_CUSTOM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_RATE_PER_S = 10.0


def _resolve_template(
    provider: TileProvider, url_template: str | None, key_env: str | None, custom: bool = False
) -> tuple[str, str | None]:
    """The template to download from and the API key that was substituted into it, if any.

    A template for a built-in provider must be on ``BUILT_IN_TEMPLATE_HOSTS``,
    so tiles from another server are never stored or attributed as that
    provider's; *custom* (a ``--custom-id`` job) allows any host that is not
    denied.
    """
    template = url_template if url_template is not None else provider.url_template
    parts = urllib.parse.urlsplit(template)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The URL template must be an http or https URL.")
    if host_is_denied(parts.hostname):
        raise ValueError(
            f"Refused: {parts.hostname} belongs to a provider whose terms do not allow "
            "downloading tiles for offline use (OpenStreetMap, Esri/ArcGIS or OpenTopoMap)."
        )
    if url_template is not None and not custom and parts.hostname.lower() not in BUILT_IN_TEMPLATE_HOSTS:
        raise ValueError(
            f"Refused: tiles from {parts.hostname} cannot be stored as {provider.name}. "
            "Name the server with --custom-id; its tiles are then stored under that id."
        )
    for token in ("{z}", "{x}", "{y}"):
        if token not in template:
            raise ValueError(f"The URL template must contain {token}.")
    key = None
    if key_env:
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(f"The environment variable {key_env} named by --key-env is not set.")
        template = template.replace("{key}", key)
    elif "{key}" in template:
        raise ValueError("The URL template contains {key}, but no key environment variable was named.")
    return template, key


def _content_type(headers: Any) -> str:
    raw = headers.get("Content-Type", "") if headers is not None else ""
    return raw.split(";", 1)[0].strip().lower()


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait before retry number ``attempt`` (from 1), honouring ``Retry-After``."""
    delay = float(2**attempt)
    raw = headers.get("Retry-After") if headers is not None else None
    if raw:
        try:
            delay = float(int(str(raw).strip()))
        except ValueError:
            pass
    return max(0.0, min(delay, RETRY_AFTER_CAP_S))


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_manifest_area(
    provider: TileProvider,
    provider_dir: Path,
    bbox: tuple[float, float, float, float],
    min_zoom: int,
    max_zoom: int,
    tiles: int,
) -> None:
    manifest = _read_manifest(provider_dir) or {}
    areas = manifest.get("areas")
    if not isinstance(areas, list):
        areas = []
    areas.append(
        {
            "bbox": list(bbox),
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "tiles": tiles,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    document = {"provider": provider.id, "attribution": provider.attribution, "areas": areas}
    _atomic_write(provider_dir / MANIFEST_NAME, json.dumps(document, indent=2).encode("utf-8"))


def _existing_tile(provider_dir: Path, z: int, x: int, y: int) -> bool:
    return any((provider_dir / str(z) / str(x) / f"{y}.{ext}").is_file() for ext in ("png", "jpg"))


def download_area(
    provider: str,
    bbox: tuple[float, float, float, float],
    min_zoom: int,
    max_zoom: int,
    *,
    out_dir: Path,
    rate_per_s: float = 2.0,
    max_tiles: int = DEFAULT_MAX_TILES,
    allow_large: bool = False,
    url_template: str | None = None,
    key_env: str | None = None,
    custom_id: str | None = None,
    opener: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> DownloadReport:
    """Download the tiles of ``bbox`` for ``provider`` into the cache under ``out_dir``.

    ``provider`` is the id of a cacheable provider. ``url_template`` may name a
    self-hosted or licensed server instead of the provider's own; it must be an
    http or https URL on a host that is not on the deny list, and it may
    contain ``{key}``, which is filled from the environment variable
    ``key_env``. The key is sent in the request only: the log shows the URL
    without its query string and with the key replaced by ``***``, and nothing
    stores it.

    The function raises ``ValueError`` with a message for the operator before
    making any request when the job is refused: a provider that does not allow
    caching, a denied host, an invalid area or zoom range, an area wider or
    taller than 4 km, a zoom above 19, or more than ``max_tiles`` tiles (the
    last three unless ``allow_large``; a zoom above 20 and more than 20,000
    tiles are never allowed).

    Requests are made one at a time and start at least ``1 / rate_per_s``
    seconds apart. Tiles already on disk are skipped, so an interrupted run can
    be repeated. HTTP 429, 500, 502, 503 and 504 and network errors are retried
    up to three times, waiting for ``Retry-After`` (at most 60 seconds) or an
    exponential delay. Only ``image/png`` and ``image/jpeg`` answers of at most
    2 MB are kept. A tile that cannot be fetched counts as failed and the run
    continues. The area is appended to the manifest at the end, even when some
    tiles failed.
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"Unknown tile provider {provider!r}.")
    if not spec.cacheable:
        raise ValueError(
            f"Refused: {spec.name} does not allow downloading tiles for offline use. "
            "It can be shown online only."
        )
    if custom_id is not None:
        # Tiles of another server go under their own id, never a built-in one.
        if url_template is None:
            raise ValueError("--custom-id needs --url-template.")
        if not isinstance(custom_id, str) or not _CUSTOM_ID_RE.match(custom_id):
            raise ValueError("--custom-id must be 1 to 32 lowercase letters, digits or dashes.")
        host = urllib.parse.urlsplit(url_template).hostname or "custom server"
        spec = TileProvider(
            id=f"custom-{custom_id}", name=f"Custom tiles ({host})", kind="map", url_template=url_template,
            attribution=f"Tiles: {host} (custom server; check its terms)", max_zoom=MAX_ZOOM_HARD, cacheable=True,
        )
    template, key = _resolve_template(spec, url_template, key_env, custom=custom_id is not None)
    box = _validate_bbox(bbox)
    if not (0 <= min_zoom <= max_zoom):
        raise ValueError("The minimum zoom must be between 0 and the maximum zoom.")
    if max_zoom > MAX_ZOOM_HARD:
        raise ValueError(f"Refused: a zoom above {MAX_ZOOM_HARD} is never allowed.")
    if max_zoom > MAX_ZOOM_DEFAULT and not allow_large:
        raise ValueError(
            f"Refused: a zoom above {MAX_ZOOM_DEFAULT} needs --allow-large."
        )
    width_m, height_m = _area_size_m(box)
    if max(width_m, height_m) > MAX_SIDE_M * _SIDE_TOLERANCE and not allow_large:
        raise ValueError(
            f"Refused: the area is {width_m / 1000:.1f} km by {height_m / 1000:.1f} km, "
            f"larger than {MAX_SIDE_M / 1000:.0f} km across. Use a smaller area or --allow-large."
        )
    if (
        isinstance(rate_per_s, bool) or not isinstance(rate_per_s, (int, float)) or not math.isfinite(rate_per_s)
        or not 0 < rate_per_s <= MAX_RATE_PER_S
    ):
        raise ValueError(f"The request rate must be above 0 and at most {MAX_RATE_PER_S:g} per second.")
    if isinstance(max_tiles, bool) or not isinstance(max_tiles, int) or max_tiles < 1:
        raise ValueError("The tile limit must be a whole number of at least 1.")
    if max_tiles > DEFAULT_MAX_TILES and not allow_large:
        raise ValueError(f"Refused: a tile limit above {DEFAULT_MAX_TILES} needs --allow-large.")
    total = count_tiles(box, min_zoom, max_zoom)
    if total > HARD_MAX_TILES:
        raise ValueError(f"Refused: {total} tiles is above the limit of {HARD_MAX_TILES}, even with --allow-large.")
    if total > max_tiles and not allow_large:
        raise ValueError(
            f"Refused: {total} tiles is more than the limit of {max_tiles}. "
            "Use a smaller area or fewer zoom levels, or add --allow-large."
        )

    opener = opener if opener is not None else _default_opener()
    provider_dir = Path(out_dir) / spec.id
    report = DownloadReport()
    min_interval = 1.0 / rate_per_s
    last_start: float | None = None
    log(f"Caching {total} tiles of {spec.name} (zoom {min_zoom}-{max_zoom}) into {provider_dir}")

    def wait_turn() -> None:
        nonlocal last_start
        now = clock()
        if last_start is not None and now - last_start < min_interval:
            sleep(min_interval - (now - last_start))
        last_start = clock()

    for z, x, y in tiles_for_bbox(box, min_zoom, max_zoom):
        report.requested += 1
        if _existing_tile(provider_dir, z, x, y):
            report.skipped_existing += 1
            continue
        url = template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        shown = redact_url(url, key)
        outcome = _fetch_tile(opener, url, shown, wait_turn, sleep, log)
        if isinstance(outcome, str):
            report.failed += 1
            log(f"failed {shown}: {outcome}")
            continue
        body, ext = outcome
        _atomic_write(provider_dir / str(z) / str(x) / f"{y}.{ext}", body)
        report.downloaded += 1
        report.bytes += len(body)

    _append_manifest_area(
        spec, provider_dir, box, min_zoom, max_zoom, report.downloaded + report.skipped_existing
    )
    log(
        f"Done: {report.downloaded} downloaded, {report.skipped_existing} already present, "
        f"{report.failed} failed, {report.bytes} bytes."
    )
    return report


def _fetch_tile(
    opener: Any,
    url: str,
    shown: str,
    wait_turn: Callable[[], None],
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> tuple[bytes, str] | str:
    """Fetch one tile. Returns ``(body, extension)``, or a reason string when it failed."""
    for attempt in range(MAX_RETRIES + 1):
        wait_turn()
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"}
        )
        headers = None
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
                headers = response.headers
                ctype = _content_type(headers)
                if ctype not in _EXTENSIONS:
                    return f"unexpected content type {ctype or 'none'}"
                body = response.read(MAX_TILE_BYTES + 1)
            if len(body) > MAX_TILE_BYTES:
                return "tile is larger than 2 MB"
            if not body:
                return "empty response"
            return body, _EXTENSIONS[ctype]
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUSES:
                return f"HTTP {exc.code}"
            headers = exc.headers
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = f"network error ({type(exc).__name__})"
        if attempt == MAX_RETRIES:
            return f"{reason} after {MAX_RETRIES} retries"
        delay = _retry_delay(headers, attempt + 1)
        log(f"retry {attempt + 1} for {shown} after {reason}, waiting {delay:g} s")
        sleep(delay)
    return "gave up"  # pragma: no cover
