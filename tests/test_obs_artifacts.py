"""Target-image artifact store and its loopback preview server (flightrisk/obs_artifacts.py)."""

import os
import stat
import urllib.error
import urllib.request

import cv2
import numpy as np
import pytest

from flightrisk.obs_artifacts import ArtifactStore, PreviewServer


def _img(color=(0, 0, 200), size=(400, 300)):
    img = np.zeros((size[0], size[1], 3), np.uint8)
    img[:] = color
    cv2.putText(img, "T", (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 5)
    return img


@pytest.fixture
def store(tmp_path):
    now = [1_800_000_000.0]
    s = ArtifactStore(tmp_path / "art", retention_days=7, max_files=3, ttl_s=3600, secret=b"k" * 32,
                      clock=lambda: now[0])
    s.now = now
    yield s
    s.stop_preview_server()


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_private_permissions_and_content_hash_names(store, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", "0")
    fields = store.store_target(_img())
    sha = fields["sha256"]
    original = store.targets / f"{sha}.jpg"
    thumb = store.targets / f"{sha}.thumb.jpg"
    assert _mode(store.root) == 0o700 and _mode(store.targets) == 0o700
    assert _mode(original) == 0o600 and _mode(thumb) == 0o600
    assert fields["artifact"] == f"targets/{sha}.jpg"
    assert max(fields["thumb_width"], fields["thumb_height"]) <= 160
    # Same image, same name: stored once.
    assert store.store_target(_img())["sha256"] == sha
    assert len(list(store.targets.glob("*.thumb.jpg"))) == 1


def test_raw_upload_bytes_are_hashed_as_uploaded(store, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", "0")
    import hashlib

    _, buf = cv2.imencode(".png", _img())
    raw = buf.tobytes()
    assert store.store_target(_img(), raw=raw)["sha256"] == hashlib.sha256(raw).hexdigest()


def test_retention_by_age_and_count(store, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", "0")
    shas = []
    for i in range(5):
        shas.append(store.store_target(_img(color=(i * 40, 10, 10)))["sha256"])
        store.now[0] += 10
    remaining = sorted(p.name for p in store.targets.glob("*.jpg") if not p.name.endswith(".thumb.jpg"))
    assert len(remaining) == 3  # max_files
    assert f"{shas[0]}.jpg" not in remaining
    store.now[0] += 8 * 86400
    assert store.cleanup() == 3
    assert list(store.targets.glob("*.jpg")) == []


def test_signed_urls_expire_and_resist_tampering(store):
    sha = "a" * 64
    exp = int(store.now[0] + 60)
    sig = store.sign(sha, exp)
    assert store.verify(sha, exp, sig)
    assert not store.verify(sha, exp + 1, sig)
    assert not store.verify("b" * 64, exp, sig)
    assert not store.verify(sha, exp, sig[:-1] + ("0" if sig[-1] != "0" else "1"))
    assert not store.verify("../../etc/passwd", exp, sig)
    store.now[0] = exp + 1
    assert not store.verify(sha, exp, sig)


def test_preview_server_serves_only_valid_thumbnails(store, monkeypatch):
    sock_port = _free_port()
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", str(sock_port))
    store.now[0] = __import__("time").time()
    fields = store.store_target(_img())
    url = fields["preview_url"]
    assert url.startswith(f"http://127.0.0.1:{sock_port}/target/{fields['sha256']}.jpg?exp=")
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200 and resp.headers["Content-Type"] == "image/jpeg"
        assert resp.read() == (store.targets / f"{fields['sha256']}.thumb.jpg").read_bytes()
    for bad in (url.replace("sig=", "sig=0"), url.split("?")[0], f"http://127.0.0.1:{sock_port}/targets/x"):
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(bad, timeout=5)
        assert err.value.code == 404


def test_preview_server_binds_loopback_only(store):
    with pytest.raises(ValueError):
        PreviewServer(store, port=0, host="0.0.0.0")
    server = PreviewServer(store, port=0)
    try:
        assert server._httpd.server_address[0] == "127.0.0.1"
    finally:
        server.stop()


def test_preview_disabled_with_port_zero(store, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", "0")
    assert store.ensure_preview_server() is None
    assert "preview_url" not in store.store_target(_img())


def test_preview_key_file_is_private(tmp_path, monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_PREVIEW_SECRET", raising=False)
    s = ArtifactStore(tmp_path / "art2")
    s.sign("a" * 64, 1)
    assert _mode(tmp_path / "art2" / "preview.key") == 0o600


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
