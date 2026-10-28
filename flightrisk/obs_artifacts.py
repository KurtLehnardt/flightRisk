"""Local artifact store for the target image, and its loopback preview server.

Only used with content capture on (``FLIGHTRISK_TELEMETRY_CONTENT=1``).
The operator's target reference photo is stored once per distinct image as

    <artifact dir>/targets/<sha256>.jpg         the photo as uploaded (or re-encoded)
    <artifact dir>/targets/<sha256>.thumb.jpg   a thumbnail, at most 160 px on its long side

The directory is created with mode 0700 and every file with mode 0600. Files
older than ``FLIGHTRISK_ARTIFACT_RETENTION_DAYS`` (default 7, the Loki
retention) are deleted, and at most ``FLIGHTRISK_ARTIFACT_MAX_FILES``
(default 50) targets are kept. The artifact directory defaults to
``~/.flightrisk/artifacts`` (``FLIGHTRISK_ARTIFACT_DIR``).

Grafana shows the thumbnail through a separate preview server that listens on
127.0.0.1 only (``FLIGHTRISK_PREVIEW_PORT``, default 5560; 0 disables it).
The dashboard's own deny-by-default API-key gate is not touched: a Grafana
panel cannot send an ``Authorization`` header, so instead each
``target_image`` event carries a capability URL, signed with HMAC-SHA256
over the image hash and an expiry (``FLIGHTRISK_PREVIEW_TTL_S``, default 24
hours). The signing key is ``FLIGHTRISK_PREVIEW_SECRET`` or a random key
kept in ``<artifact dir>/preview.key`` (mode 0600). The server serves only
thumbnails, only for a valid, unexpired signature, and answers 404 to
everything else. See ``observability/README.md`` for the trade-off.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_log = logging.getLogger("flightrisk.artifacts")

ARTIFACT_DIR_ENV = "FLIGHTRISK_ARTIFACT_DIR"
RETENTION_ENV = "FLIGHTRISK_ARTIFACT_RETENTION_DAYS"
MAX_FILES_ENV = "FLIGHTRISK_ARTIFACT_MAX_FILES"
PREVIEW_PORT_ENV = "FLIGHTRISK_PREVIEW_PORT"
PREVIEW_TTL_ENV = "FLIGHTRISK_PREVIEW_TTL_S"
PREVIEW_SECRET_ENV = "FLIGHTRISK_PREVIEW_SECRET"

DEFAULT_RETENTION_DAYS = 7.0
DEFAULT_MAX_FILES = 50
DEFAULT_PREVIEW_PORT = 5560
DEFAULT_PREVIEW_TTL_S = 24 * 3600
THUMB_MAX_PX = 160
PREVIEW_HOST = "127.0.0.1"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PATH_RE = re.compile(r"^/target/([0-9a-f]{64})\.jpg$")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` with mode 0600, atomically."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ArtifactStore:
    """Content-addressed, private, size- and age-bounded artifact directory."""

    def __init__(self, root: str | os.PathLike | None = None, *, retention_days: float | None = None,
                 max_files: int | None = None, ttl_s: float | None = None, secret: bytes | None = None,
                 clock=time.time):
        base = root or os.environ.get(ARTIFACT_DIR_ENV) or (Path.home() / ".flightrisk" / "artifacts")
        self.root = Path(base).expanduser()
        self.targets = self.root / "targets"
        self.retention_s = 86400.0 * (retention_days if retention_days is not None else _env_float(RETENTION_ENV, DEFAULT_RETENTION_DAYS))
        self.max_files = int(max_files if max_files is not None else _env_float(MAX_FILES_ENV, DEFAULT_MAX_FILES))
        self.ttl_s = float(ttl_s if ttl_s is not None else _env_float(PREVIEW_TTL_ENV, DEFAULT_PREVIEW_TTL_S))
        self._secret = secret
        self._clock = clock
        self._lock = threading.Lock()
        self._server: PreviewServer | None = None

    # -- directory ---------------------------------------------------------

    def _ensure_dirs(self) -> None:
        for d in (self.root, self.targets):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(d, 0o700)

    def _key(self) -> bytes:
        if self._secret is not None:
            return self._secret
        env = os.environ.get(PREVIEW_SECRET_ENV)
        if env:
            self._secret = env.encode()
            return self._secret
        self._ensure_dirs()
        path = self.root / "preview.key"
        try:
            self._secret = path.read_bytes()
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            _write_private(path, key)
            self._secret = key
        return self._secret

    # -- store and clean up -----------------------------------------------

    def store_target(self, img: Any, raw: bytes | None = None) -> dict[str, Any] | None:
        """Store the target photo and its thumbnail; return the event fields.

        Returns ``sha256``, ``artifact`` (a reference relative to the
        artifact directory), ``thumb_width``/``thumb_height`` and, when the
        preview server is enabled, ``preview_url`` and ``preview_expires``.
        """
        import cv2

        if raw is None:
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                return None
            raw = buf.tobytes()
        sha = hashlib.sha256(raw).hexdigest()
        h, w = img.shape[:2]
        scale = min(1.0, THUMB_MAX_PX / float(max(h, w) or 1))
        thumb = img if scale >= 1.0 else cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        ok, tbuf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return None
        with self._lock:
            self._ensure_dirs()
            original = self.targets / f"{sha}.jpg"
            preview = self.targets / f"{sha}.thumb.jpg"
            if not original.exists():
                _write_private(original, raw)
            _write_private(preview, tbuf.tobytes())
            now = self._clock()
            os.utime(original, (now, now))
            self._cleanup_locked(now)
        fields: dict[str, Any] = {
            "sha256": sha,
            "artifact": f"targets/{sha}.jpg",
            "thumb_width": int(thumb.shape[1]),
            "thumb_height": int(thumb.shape[0]),
        }
        url = self.preview_url(sha)
        if url:
            fields["preview_url"] = url
            fields["preview_expires"] = int(self._clock() + self.ttl_s)
        return fields

    def cleanup(self) -> int:
        """Delete expired and excess artifacts; returns how many targets were removed."""
        with self._lock:
            if not self.targets.exists():
                return 0
            return self._cleanup_locked(self._clock())

    def _cleanup_locked(self, now: float) -> int:
        originals = []
        for p in self.targets.glob("*.jpg"):
            if p.name.endswith(".thumb.jpg"):
                continue
            try:
                originals.append((p.stat().st_mtime, p))
            except FileNotFoundError:
                continue
        originals.sort(reverse=True)
        removed = 0
        for idx, (mtime, p) in enumerate(originals):
            if now - mtime > self.retention_s or idx >= self.max_files:
                for victim in (p, p.with_name(p.name[:-4] + ".thumb.jpg")):
                    try:
                        victim.unlink()
                    except FileNotFoundError:
                        pass
                removed += 1
        # A thumbnail whose original is gone is removed too.
        for t in self.targets.glob("*.thumb.jpg"):
            if not t.with_name(t.name[: -len(".thumb.jpg")] + ".jpg").exists():
                try:
                    t.unlink()
                except FileNotFoundError:
                    pass
        return removed

    # -- signed preview URLs ------------------------------------------------

    def sign(self, sha: str, expires: int) -> str:
        return hmac.new(self._key(), f"target:{sha}:{expires}".encode(), hashlib.sha256).hexdigest()[:40]

    def verify(self, sha: str, expires: Any, sig: Any) -> bool:
        if not _SHA_RE.match(sha or "") or not isinstance(sig, str):
            return False
        try:
            exp = int(expires)
        except (TypeError, ValueError):
            return False
        if exp < self._clock():
            return False
        return hmac.compare_digest(self.sign(sha, exp), sig)

    def preview_url(self, sha: str) -> str | None:
        port = self.ensure_preview_server()
        if not port:
            return None
        exp = int(self._clock() + self.ttl_s)
        return f"http://{PREVIEW_HOST}:{port}/target/{sha}.jpg?exp={exp}&sig={self.sign(sha, exp)}"

    def thumbnail_bytes(self, sha: str) -> bytes | None:
        if not _SHA_RE.match(sha):
            return None
        path = self.targets / f"{sha}.thumb.jpg"
        try:
            return path.read_bytes()
        except OSError:
            return None

    # -- preview server -----------------------------------------------------

    def ensure_preview_server(self) -> int | None:
        """Start the loopback preview server once; return its port, or None when disabled."""
        if self._server is not None:
            return self._server.port
        port = int(_env_float(PREVIEW_PORT_ENV, DEFAULT_PREVIEW_PORT))
        if port <= 0:
            return None
        with self._lock:
            if self._server is None:
                try:
                    self._server = PreviewServer(self, port=port)
                except OSError as exc:
                    _log.warning("preview_server_unavailable port=%s error=%s", port, exc)
                    return None
        return self._server.port

    def stop_preview_server(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()


class _Handler(http.server.BaseHTTPRequestHandler):
    store: ArtifactStore  # set on the subclass
    server_version = "flightrisk-preview"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # silence the default stderr access log
        _log.debug("preview_request %s", fmt % args)

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=60")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        m = _PATH_RE.match(parsed.path)
        query = parse_qs(parsed.query)
        if not m:
            return self._send(404, b"not found")
        sha = m.group(1)
        exp = (query.get("exp") or [None])[0]
        sig = (query.get("sig") or [None])[0]
        if not self.store.verify(sha, exp, sig):
            return self._send(404, b"not found")
        data = self.store.thumbnail_bytes(sha)
        if data is None:
            return self._send(404, b"not found")
        return self._send(200, data, "image/jpeg")

    do_HEAD = do_GET

    def do_POST(self) -> None:
        self._send(405, b"method not allowed")


class PreviewServer:
    """Thumbnail server bound to 127.0.0.1 only, on its own daemon thread."""

    def __init__(self, store: ArtifactStore, port: int = DEFAULT_PREVIEW_PORT, host: str = PREVIEW_HOST):
        if host != PREVIEW_HOST:
            raise ValueError("the preview server binds 127.0.0.1 only")
        handler = type("PreviewHandler", (_Handler,), {"store": store})
        self._httpd = http.server.ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="preview-server", daemon=True)
        self._thread.start()
        _log.info("preview_server_started host=%s port=%s", host, self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


_default: ArtifactStore | None = None
_default_lock = threading.Lock()


def default_store() -> ArtifactStore:
    global _default
    with _default_lock:
        if _default is None:
            _default = ArtifactStore()
        return _default


__all__ = ["ArtifactStore", "PreviewServer", "default_store"]
