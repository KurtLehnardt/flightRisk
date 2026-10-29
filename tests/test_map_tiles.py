"""Tests for the offline map tile cache (``flightrisk/dashboard/map_tiles.py``).

Every download test talks to a local fake HTTP tile server on 127.0.0.1. No
test contacts a real provider or writes to the real ``~/.flightrisk``.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from flask import Flask

from flightrisk.dashboard import map_tiles as mt

PNG = b"\x89PNG\r\n\x1a\n" + b"tile-bytes"
JPG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes"

# A spot near Utah Lake, used by the documentation's examples.
LAT, LON = 40.6894, -111.7310


# ---------------------------------------------------------------------------
# Fake tile server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # silence the test output
        pass

    def do_GET(self):
        server = self.server
        with server.lock:
            server.active += 1
            server.max_active = max(server.max_active, server.active)
            server.requests.append((self.path, dict(self.headers)))
            script = server.script.pop(0) if server.script else None
        try:
            status, ctype, body, extra = script or (200, "image/png", PNG, {})
            self.send_response(status)
            if ctype:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        finally:
            with server.lock:
                server.active -= 1


class FakeTileServer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.requests: list[tuple[str, dict]] = []
        self.script: list[tuple] = []
        self.active = 0
        self.max_active = 0
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def template(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/t/{{z}}/{{y}}/{{x}}.png"


@pytest.fixture
def server():
    srv = FakeTileServer()
    srv.thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


class FakeTime:
    """A clock whose ``sleep`` advances it, so rate limiting runs instantly."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def small_bbox(z: int = 15, nx: int = 1, ny: int = 1):
    """A box that covers exactly ``nx`` by ``ny`` tiles at zoom ``z``, around LAT, LON."""
    x, y = mt.latlon_to_tile(LAT, LON, z)
    s, w, _n, _e = mt.tile_bounds(x, y + ny - 1, z)
    _s, _w, n, e = mt.tile_bounds(x + nx - 1, y, z)
    inset_lat = (n - s) * 0.01
    inset_lon = (e - w) * 0.01
    return (s + inset_lat, w + inset_lon, n - inset_lat, e - inset_lon)


def run(server, bbox, zmin, zmax, out, ft=None, logs=None, **kw):
    ft = ft or FakeTime()
    logs = logs if logs is not None else []
    return mt.download_area(
        "usgs-imagery",
        bbox,
        zmin,
        zmax,
        out_dir=out,
        url_template=kw.pop("url_template", server.template),
        sleep=ft.sleep,
        clock=ft.clock,
        log=logs.append,
        **kw,
    )


# ---------------------------------------------------------------------------
# Tile math
# ---------------------------------------------------------------------------


def test_tile_round_trip_contains_point():
    for z in (3, 10, 15, 18):
        x, y = mt.latlon_to_tile(LAT, LON, z)
        s, w, n, e = mt.tile_bounds(x, y, z)
        assert s <= LAT <= n
        assert w <= LON <= e


def test_known_tile_at_zoom_zero_and_one():
    assert mt.latlon_to_tile(0.0, 0.0, 0) == (0, 0)
    assert mt.latlon_to_tile(10.0, -10.0, 1) == (0, 0)
    assert mt.latlon_to_tile(-10.0, 10.0, 1) == (1, 1)


def test_latitude_and_longitude_are_clamped():
    z = 5
    assert mt.latlon_to_tile(90.0, 180.0, z) == (2**z - 1, 0)
    assert mt.latlon_to_tile(-90.0, -180.0, z) == (0, 2**z - 1)


def test_count_tiles_matches_generator():
    bbox = mt.bbox_from_center(LAT, LON, 500)
    assert mt.count_tiles(bbox, 12, 17) == len(list(mt.tiles_for_bbox(bbox, 12, 17)))


def test_bbox_from_center_spans_radius():
    s, w, n, e = mt.bbox_from_center(LAT, LON, 1000)
    assert s < LAT < n and w < LON < e
    assert (n - s) * 111_320 == pytest.approx(2000, rel=0.01)


def test_tiles_for_bbox_yields_requested_grid():
    tiles = list(mt.tiles_for_bbox(small_bbox(15, 2, 3), 15, 15))
    assert len(tiles) == 6
    assert {z for z, _x, _y in tiles} == {15}


def test_tiles_dir_uses_environment_or_home():
    assert mt.tiles_dir({"FLIGHTRISK_TILES_DIR": "/somewhere/tiles"}) == Path("/somewhere/tiles")
    assert mt.tiles_dir({}) == Path.home() / ".flightrisk" / "tiles"
    assert mt.tiles_dir({"FLIGHTRISK_TILES_DIR": ""}) == Path.home() / ".flightrisk" / "tiles"


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


def test_download_writes_files_and_manifest(server, tmp_path):
    bbox = small_bbox(15, 2, 2)
    report = run(server, bbox, 15, 15, tmp_path)
    assert (report.requested, report.downloaded, report.failed) == (4, 4, 0)
    assert report.bytes == 4 * len(PNG)
    files = sorted((tmp_path / "usgs-imagery" / "15").rglob("*.png"))
    assert len(files) == 4
    assert files[0].read_bytes() == PNG
    manifest = json.loads((tmp_path / "usgs-imagery" / "manifest.json").read_text())
    assert manifest["provider"] == "usgs-imagery"
    assert "Geological Survey" in manifest["attribution"]
    (area,) = manifest["areas"]
    assert area["bbox"] == list(bbox)
    assert (area["min_zoom"], area["max_zoom"], area["tiles"]) == (15, 15, 4)
    assert area["fetched_at"].endswith("Z")
    assert not list(tmp_path.rglob(".tmp-*"))


def test_jpeg_content_type_stored_as_jpg(server, tmp_path):
    server.script = [(200, "image/jpeg; charset=binary", JPG, {})]
    run(server, small_bbox(), 15, 15, tmp_path)
    (tile,) = (tmp_path / "usgs-imagery" / "15").rglob("*.jpg")
    assert tile.read_bytes() == JPG


def test_rerun_skips_existing_and_appends_area(server, tmp_path):
    bbox = small_bbox(15, 2, 1)
    run(server, bbox, 15, 15, tmp_path)
    before = len(server.requests)
    report = run(server, bbox, 15, 15, tmp_path)
    assert (report.downloaded, report.skipped_existing) == (0, 2)
    assert len(server.requests) == before
    manifest = json.loads((tmp_path / "usgs-imagery" / "manifest.json").read_text())
    assert len(manifest["areas"]) == 2


def test_polite_headers_and_one_request_at_a_time(server, tmp_path):
    run(server, small_bbox(15, 3, 3), 15, 15, tmp_path)
    assert len(server.requests) == 9
    for path, headers in server.requests:
        assert path.startswith("/t/15/")
        assert headers["User-Agent"] == (
            "FlightRisk-tile-cache/1.0 (+https://github.com/KurtLehnardt/flightRisk)"
        )
        assert headers["Accept"] == "image/*"
    assert server.max_active == 1


def test_rate_limit_spaces_requests(server, tmp_path):
    ft = FakeTime()
    run(server, small_bbox(15, 2, 2), 15, 15, tmp_path, ft=ft, rate_per_s=2.0)
    assert sum(ft.sleeps) == pytest.approx(3 * 0.5)
    assert all(s == pytest.approx(0.5) for s in ft.sleeps)


def test_retry_after_is_honoured_then_success(server, tmp_path):
    server.script = [(429, "text/plain", b"slow down", {"Retry-After": "7"})]
    ft = FakeTime()
    report = run(server, small_bbox(), 15, 15, tmp_path, ft=ft)
    assert report.downloaded == 1 and report.failed == 0
    assert 7.0 in ft.sleeps
    assert len(server.requests) == 2


def test_retry_after_is_capped(server, tmp_path):
    server.script = [(503, "text/plain", b"", {"Retry-After": "600"})]
    ft = FakeTime()
    run(server, small_bbox(), 15, 15, tmp_path, ft=ft)
    assert max(ft.sleeps) == 60.0


def test_gives_up_after_three_retries(server, tmp_path):
    server.script = [(500, "text/plain", b"", {})] * 10
    report = run(server, small_bbox(), 15, 15, tmp_path)
    assert report.failed == 1 and report.downloaded == 0
    assert len(server.requests) == 4


def test_404_counts_as_failed_and_run_continues(server, tmp_path):
    server.script = [(404, "text/plain", b"", {})]
    logs: list[str] = []
    report = run(server, small_bbox(15, 2, 1), 15, 15, tmp_path, logs=logs)
    assert (report.downloaded, report.failed) == (1, 1)
    manifest = json.loads((tmp_path / "usgs-imagery" / "manifest.json").read_text())
    assert manifest["areas"][0]["tiles"] == 1
    assert len(server.requests) == 2


def test_non_image_content_type_is_refused(server, tmp_path):
    server.script = [(200, "text/html", b"<html>login</html>", {})]
    report = run(server, small_bbox(), 15, 15, tmp_path)
    assert report.failed == 1
    assert not list(tmp_path.rglob("*.png"))


def test_oversize_tile_is_refused(server, tmp_path):
    server.script = [(200, "image/png", b"x" * (mt.MAX_TILE_BYTES + 1), {})]
    report = run(server, small_bbox(), 15, 15, tmp_path)
    assert report.failed == 1
    assert not list(tmp_path.rglob("*.png"))


def test_osm_provider_is_refused(tmp_path):
    with pytest.raises(ValueError, match="offline"):
        mt.download_area("osm", small_bbox(), 15, 15, out_dir=tmp_path, opener=_no_network())


@pytest.mark.parametrize(
    "url",
    [
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://server.arcgisonline.com/x/{z}/{y}/{x}",
        "https://services.arcgisonline.com/x/{z}/{y}/{x}",
        "https://tiles.arcgis.com/x/{z}/{y}/{x}",
        "https://tile.opentopomap.org/{z}/{x}/{y}.png",
        "https://b.tile.opentopomap.org/{z}/{x}/{y}.png",
        "https://a.opentopomap.org/{z}/{x}/{y}.png",
        "https://A.TILE.OpenStreetMap.org./{z}/{x}/{y}.png",
    ],
)
def test_deny_listed_templates_are_refused(url, tmp_path):
    with pytest.raises(ValueError, match="do not allow"):
        mt.download_area(
            "usgs-imagery", small_bbox(), 15, 15, out_dir=tmp_path, url_template=url,
            opener=_no_network(),
        )


def test_non_http_templates_are_refused(tmp_path):
    for url in ("file:///etc/passwd?{z}{x}{y}", "ftp://example.com/{z}/{x}/{y}"):
        with pytest.raises(ValueError, match="http"):
            mt.download_area(
                "usgs-imagery", small_bbox(), 15, 15, out_dir=tmp_path, url_template=url,
                opener=_no_network(),
            )


def _no_network():
    class Opener:
        def open(self, *a, **k):
            raise AssertionError("the downloader must not make a request here")

    return Opener()


def test_too_many_tiles_refused_unless_allow_large(server, tmp_path):
    bbox = mt.bbox_from_center(LAT, LON, 1900)
    assert mt.count_tiles(bbox, 15, 19) > 2000
    with pytest.raises(ValueError, match="allow-large"):
        mt.download_area("usgs-imagery", bbox, 15, 19, out_dir=tmp_path, opener=_no_network())
    assert not server.requests


def test_allow_large_still_capped_at_20000(tmp_path):
    bbox = mt.bbox_from_center(LAT, LON, 15000)
    assert mt.count_tiles(bbox, 14, 19) > 20_000
    with pytest.raises(ValueError, match="20000"):
        mt.download_area(
            "usgs-imagery", bbox, 14, 19, out_dir=tmp_path, allow_large=True,
            opener=_no_network(),
        )


def test_allow_large_permits_more_than_max_tiles(server, tmp_path):
    bbox = small_bbox(15, 3, 1)
    report = run(server, bbox, 15, 15, tmp_path, max_tiles=2, allow_large=True)
    assert report.downloaded == 3
    with pytest.raises(ValueError, match="more than"):
        run(server, bbox, 15, 15, tmp_path / "other", max_tiles=2)


def test_area_wider_than_4km_refused_unless_allow_large(server, tmp_path):
    bbox = mt.bbox_from_center(LAT, LON, 2500)
    with pytest.raises(ValueError, match="km across"):
        mt.download_area("usgs-imagery", bbox, 10, 10, out_dir=tmp_path, opener=_no_network())
    report = run(server, bbox, 10, 10, tmp_path, allow_large=True)
    assert report.downloaded > 0


def test_radius_2km_is_allowed(server, tmp_path):
    bbox = mt.bbox_from_center(LAT, LON, 2000)
    assert run(server, bbox, 10, 10, tmp_path).downloaded > 0


def test_zoom_above_19_needs_allow_large_and_above_20_never(server, tmp_path):
    bbox = small_bbox(20, 1, 1)
    with pytest.raises(ValueError, match="allow-large"):
        mt.download_area("usgs-imagery", bbox, 20, 20, out_dir=tmp_path, opener=_no_network())
    assert run(server, bbox, 20, 20, tmp_path, allow_large=True).downloaded == 1
    with pytest.raises(ValueError, match="never"):
        mt.download_area(
            "usgs-imagery", bbox, 20, 21, out_dir=tmp_path, allow_large=True,
            opener=_no_network(),
        )


def test_invalid_input_refused(tmp_path):
    good = small_bbox()
    for bbox, zmin, zmax in [
        ((95.0, 0.0, 96.0, 1.0), 10, 10),
        ((0.0, 0.0, 1.0, 190.0), 10, 10),
        ((float("nan"), 0.0, 1.0, 1.0), 10, 10),
        ((1.0, 0.0, 0.0, 1.0), 10, 10),
        (good, 16, 15),
    ]:
        with pytest.raises(ValueError):
            mt.download_area(
                "usgs-imagery", bbox, zmin, zmax, out_dir=tmp_path, opener=_no_network()
            )


def test_key_is_substituted_but_never_logged_or_stored(server, tmp_path, monkeypatch):
    secret = "s3cr3t-KEY-value"
    monkeypatch.setenv("TEST_TILE_KEY", secret)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    template = f"{base}/{{key}}/{{z}}/{{x}}/{{y}}.png?token={{key}}"
    server.script = [(404, "text/plain", b"", {})]
    logs: list[str] = []
    run(
        server, small_bbox(15, 2, 1), 15, 15, tmp_path, logs=logs,
        url_template=template, key_env="TEST_TILE_KEY",
    )
    assert all(secret in path for path, _ in server.requests)
    assert logs
    assert secret not in "\n".join(logs)
    assert "***" in "\n".join(logs)
    assert "token" not in "\n".join(logs)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
        assert secret not in str(path)


def test_key_env_unset_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_TILE_KEY", raising=False)
    with pytest.raises(ValueError, match="TEST_TILE_KEY"):
        mt.download_area(
            "usgs-imagery", small_bbox(), 15, 15, out_dir=tmp_path,
            url_template="http://127.0.0.1:1/{key}/{z}/{x}/{y}", key_env="TEST_TILE_KEY",
            opener=_no_network(),
        )


def test_key_placeholder_without_key_env_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="key"):
        mt.download_area(
            "usgs-imagery", small_bbox(), 15, 15, out_dir=tmp_path,
            url_template="http://127.0.0.1:1/{key}/{z}/{x}/{y}", opener=_no_network(),
        )


def test_redact_url_strips_query_and_key():
    shown = mt.redact_url("https://h.example/KEY123/1/2/3.png?token=KEY123", "KEY123")
    assert shown == "https://h.example/***/1/2/3.png"


# ---------------------------------------------------------------------------
# Command-line tool
# ---------------------------------------------------------------------------


def _cli():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "cache_map_tiles.py"
    spec = importlib.util.spec_from_file_location("cache_map_tiles_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_dry_run_makes_no_request(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(mt, "_default_opener", lambda: _no_network())
    cli = _cli()
    code = cli.main(
        [
            "--provider", "usgs-imagery", "--center", f"{LAT},{LON}", "--radius-m", "60",
            "--min-zoom", "17", "--max-zoom", "18", "--out", str(tmp_path), "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    expected = mt.count_tiles(mt.bbox_from_center(LAT, LON, 60), 17, 18)
    assert code == 0
    assert f"{expected} tiles" in out
    assert str(tmp_path / "usgs-imagery") in out
    assert not list(tmp_path.iterdir())


def test_cli_downloads_from_local_server(server, tmp_path, capsys):
    cli = _cli()
    code = cli.main(
        [
            "--provider", "usgs-topo", "--bbox", ",".join(str(v) for v in small_bbox(15, 1, 2)),
            "--min-zoom", "15", "--max-zoom", "15", "--out", str(tmp_path),
            "--url-template", server.template, "--rate", "1000",
        ]
    )
    assert code == 0
    assert "2 downloaded" in capsys.readouterr().out
    assert len(server.requests) == 2


def test_cli_refusal_exits_2(tmp_path, capsys):
    cli = _cli()
    code = cli.main(
        [
            "--provider", "usgs-imagery", "--center", f"{LAT},{LON}", "--radius-m", "3000",
            "--min-zoom", "10", "--max-zoom", "10", "--out", str(tmp_path),
        ]
    )
    assert code == 2
    assert "km across" in capsys.readouterr().err
    code = cli.main(
        [
            "--provider", "usgs-imagery", "--center", f"{LAT},{LON}", "--radius-m", "60",
            "--min-zoom", "15", "--max-zoom", "15", "--out", str(tmp_path),
            "--url-template", "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
        ]
    )
    assert code == 2


def test_cli_rejects_non_cacheable_provider_choice(capsys):
    cli = _cli()
    with pytest.raises(SystemExit) as info:
        cli.main(["--provider", "osm", "--center", "1,1", "--radius-m", "5",
                  "--min-zoom", "1", "--max-zoom", "1"])
    assert info.value.code == 2


# ---------------------------------------------------------------------------
# Serving route
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("FLIGHTRISK_TILES_DIR", str(cache))
    app = Flask(__name__)
    app.register_blueprint(mt.tiles_bp)
    return app.test_client()


def put_tile(cache: Path, provider="usgs-imagery", z=15, x=5, y=7, ext="png", data=PNG):
    path = cache / provider / str(z) / str(x) / f"{y}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_cached_tile_is_served_with_headers(client, tmp_path):
    put_tile(tmp_path / "cache")
    resp = client.get("/tiles/usgs-imagery/15/5/7")
    assert resp.status_code == 200
    assert resp.data == PNG
    assert resp.mimetype == "image/png"
    assert resp.headers["Cache-Control"] == "public, max-age=86400"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_jpeg_tile_mimetype(client, tmp_path):
    put_tile(tmp_path / "cache", provider="usgs-topo", ext="jpg", data=JPG)
    resp = client.get("/tiles/usgs-topo/15/5/7")
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg"


def test_conditional_requests_return_304(client, tmp_path):
    put_tile(tmp_path / "cache")
    first = client.get("/tiles/usgs-imagery/15/5/7")
    by_etag = client.get("/tiles/usgs-imagery/15/5/7", headers={"If-None-Match": first.headers["ETag"]})
    assert by_etag.status_code == 304
    by_date = client.get(
        "/tiles/usgs-imagery/15/5/7",
        headers={"If-Modified-Since": first.headers["Last-Modified"]},
    )
    assert by_date.status_code == 304


def test_missing_tile_is_404_json(client):
    resp = client.get("/tiles/usgs-imagery/15/5/7")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not cached"}


def test_traversal_attempts_never_return_file_contents(client, tmp_path):
    cache = tmp_path / "cache"
    secret = b"TOP-SECRET-CONTENT"
    (tmp_path / "secret.png").write_bytes(secret)
    (cache / "secret.png").write_bytes(secret)
    put_tile(cache)
    (cache / "usgs-imagery" / "15" / "5" / "evil").mkdir()
    urls = [
        "/tiles/../15/5/7",
        "/tiles/%2e%2e/15/5/7",
        "/tiles/usgs-imagery/../../secret.png",
        "/tiles/usgs-imagery/%2e%2e/%2e%2e/secret.png",
        "/tiles/usgs-imagery%2F..%2F../15/5/7",
        "/tiles/usgs-imagery/15%2F5/7/1",
        "/tiles/usgs-imagery/15/5%2F..%2F5/7",
        "/tiles/usgs-imagery/15/5/7%2F..%2F7",
        "/tiles/usgs-imagery/15/5/..%2F7",
        "/tiles/usgs-imagery/15/5/7.png",
        "/tiles/usgs-imagery/15/32768/7",
        "/tiles/usgs-imagery/15/5/32768",
        "/tiles/usgs-imagery/2/4/0",
        "/tiles/usgs-imagery/23/0/0",
        "/tiles/usgs-imagery/-1/0/0",
        "/tiles/usgs-imagery/15/-5/7",
        "/tiles/usgs-imagery/15/5/-7",
        "/tiles/nonexistent/15/5/7",
        "/tiles/osm/15/5/7",
        "/tiles/USGS-IMAGERY/15/5/7",
        "/tiles/usgs-imagery%00/15/5/7",
        "/tiles/usgs-imagery/15/5/evil",
    ]
    for url in urls:
        resp = client.get(url)
        assert resp.status_code in (400, 404), url
        assert secret not in resp.data, url
        assert PNG not in resp.data or url == "/tiles/usgs-imagery/15/5/07", url
    # Leading zeros are the same integer, so that one URL may serve the tile.
    assert client.get("/tiles/usgs-imagery/15/5/07").status_code in (200, 404)


def test_osm_is_never_served_even_when_files_exist(client, tmp_path):
    put_tile(tmp_path / "cache", provider="osm")
    assert client.get("/tiles/osm/15/5/7").status_code == 404


def test_symlink_escaping_cache_dir_is_404(client, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"OUTSIDE-SECRET")
    link = tmp_path / "cache" / "usgs-imagery" / "15" / "5" / "7.png"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    resp = client.get("/tiles/usgs-imagery/15/5/7")
    assert resp.status_code == 404
    assert b"OUTSIDE-SECRET" not in resp.data
    assert mt.find_tile("usgs-imagery", 15, 5, 7, tmp_path / "cache") is None


def test_symlinked_directory_escaping_cache_dir_is_404(client, tmp_path):
    outside = tmp_path / "outside_dir"
    (outside / "5").mkdir(parents=True)
    (outside / "5" / "7.png").write_bytes(b"OUTSIDE-SECRET")
    provider_dir = tmp_path / "cache" / "usgs-imagery"
    provider_dir.mkdir()
    (provider_dir / "15").symlink_to(outside)
    assert client.get("/tiles/usgs-imagery/15/5/7").status_code == 404


def test_symlink_inside_cache_dir_is_allowed(client, tmp_path):
    cache = tmp_path / "cache"
    real = put_tile(cache, x=1, y=1)
    link = cache / "usgs-imagery" / "15" / "5" / "7.png"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    assert client.get("/tiles/usgs-imagery/15/5/7").status_code == 200


def test_find_tile_rejects_bad_types(tmp_path):
    put_tile(tmp_path)
    assert mt.find_tile("usgs-imagery", 15, 5, 7, tmp_path) is not None
    for args in [("usgs-imagery", "15", 5, 7), ("usgs-imagery", 15, 5.0, 7), ("usgs-imagery", True, 0, 0),
                 ("../x", 15, 5, 7), ("", 15, 5, 7), (None, 15, 5, 7)]:
        assert mt.find_tile(*args, base=tmp_path) is None


# ---------------------------------------------------------------------------
# cache_status and providers_payload
# ---------------------------------------------------------------------------


def test_cache_status_counts_present_tiles(tmp_path):
    bbox = small_bbox(16, 2, 2)
    tiles = list(mt.tiles_for_bbox(bbox, 16, 16))
    for z, x, y in tiles[:3]:
        put_tile(tmp_path, z=z, x=x, y=y)
    status = mt.cache_status("usgs-imagery", bbox, 16, tmp_path)
    assert (status["total"], status["present"]) == (4, 3)
    assert status["fraction"] == pytest.approx(0.75)
    assert status["provider"] == "usgs-imagery" and status["zoom"] == 16


def test_cache_status_empty_and_osm(tmp_path):
    bbox = small_bbox(16, 1, 1)
    assert mt.cache_status("usgs-imagery", bbox, 16, tmp_path)["fraction"] == 0.0
    assert mt.cache_status("osm", bbox, 16, tmp_path)["present"] == 0


def test_cache_status_too_large_does_not_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "find_tile", lambda *a, **k: pytest.fail("scanned"))
    bbox = mt.bbox_from_center(LAT, LON, 10000)
    status = mt.cache_status("usgs-imagery", bbox, 18, tmp_path)
    assert status["too_large"] is True
    assert status["present"] is None and status["fraction"] is None
    assert status["total"] > 4096


def test_providers_payload_lists_providers(tmp_path):
    payload = mt.providers_payload(tmp_path)
    assert [p["id"] for p in payload] == ["usgs-imagery", "usgs-topo", "osm"]
    by_id = {p["id"]: p for p in payload}
    assert by_id["osm"]["cacheable"] is False
    assert by_id["usgs-imagery"]["url"].endswith("/tile/{z}/{y}/{x}")
    assert by_id["usgs-imagery"]["cached_areas"] == []
    assert set(by_id["osm"]) == {
        "id", "name", "kind", "url", "attribution", "max_zoom", "cacheable", "cached_areas",
    }


def test_providers_payload_includes_manifest_areas(server, tmp_path):
    run(server, small_bbox(), 15, 15, tmp_path)
    areas = {p["id"]: p for p in mt.providers_payload(tmp_path)}["usgs-imagery"]["cached_areas"]
    assert len(areas) == 1 and areas[0]["min_zoom"] == 15


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'{"areas": "nope"}', b'{"areas": [1, "x", {"min_zoom": 3}]}', b"\xff\xfe"],
)
def test_providers_payload_tolerates_corrupt_manifest(tmp_path, content):
    manifest = tmp_path / "usgs-imagery" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_bytes(content)
    payload = mt.providers_payload(tmp_path)
    areas = {p["id"]: p for p in payload}["usgs-imagery"]["cached_areas"]
    assert isinstance(areas, list)
    assert all(isinstance(a, dict) for a in areas)


def test_download_recovers_from_corrupt_manifest(server, tmp_path):
    manifest = tmp_path / "usgs-imagery" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{broken")
    run(server, small_bbox(), 15, 15, tmp_path)
    assert len(json.loads(manifest.read_text())["areas"]) == 1

