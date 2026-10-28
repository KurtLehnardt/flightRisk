"""Regression tests for the findings of the review of the observability PR (#111).

Each test names the scenario it guards. Together they cover: logging and
metrics from a signal handler, a stuck console at exit, folding of distinct
errors, credentials in URLs, EXIF in stored photos, the signed-link preview
server, unbounded metric labels, and the alert rules.
"""

import asyncio
import io
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from flightrisk import fleet_protocol as fp
from flightrisk import obs_comm, obs_log
from flightrisk import obs_metrics as M
from flightrisk.obs_artifacts import ArtifactStore
from flightrisk.obs_log import ContextFilter, DropQueueHandler, Throttle, log_event
from flightrisk.obs_redact import redact_text, safe_token
from flightrisk.observability import StructuredLogger

ROOT = Path(__file__).resolve().parent.parent


def _run_with_deadline(fn, seconds=5.0):
    """Run ``fn`` on a daemon thread; return True if it finished in time (False means it hung)."""
    done = threading.Event()
    errors = []

    def target():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=target, daemon=True).start()
    finished = done.wait(seconds)
    if errors:
        raise errors[0]
    return finished


# ---------------------------------------------------------------------------
# Signal handlers: logging and metrics must not wait for a lock the thread holds
# ---------------------------------------------------------------------------

def test_a_log_from_a_signal_handler_does_not_wait_for_a_metric_lock(monkeypatch):
    """Ctrl+C on an edge logs ``edge_sigint_land`` before landing. If the signal arrives while the
    main thread is inside a metric update (the SDK holds a non-reentrant lock there), the handler's
    own metric update used to wait for that lock forever, and the drone was never told to land."""
    lock = threading.Lock()
    logger = StructuredLogger("sigtest")
    entered = []

    class _Instrument:
        def add(self, *args, **kwargs):
            with lock:  # the SDK's lock
                if not entered:
                    entered.append(1)
                    logger.warning("edge_sigint_land", drone_id="d1")  # the signal handler

    fake = _Instrument()
    current = M.flight_safety_events
    if hasattr(current, "_inner"):
        monkeypatch.setattr(current, "_inner", fake)
    else:
        monkeypatch.setattr(M, "flight_safety_events", fake)

    finished = _run_with_deadline(lambda: logger.warning("edge_land", drone_id="d1", reason="sigint"))
    assert finished, "logging from a signal handler waited for a lock held by its own thread"


def test_the_console_queue_takes_no_lock_a_signal_handler_could_hold():
    obs_log.setup_logging()
    handler = obs_log._queue_handler
    assert handler is not None
    # queue.Queue guards put() with a plain Lock; SimpleQueue.put is reentrant.
    assert isinstance(handler.queue, queue.SimpleQueue)


def test_simple_queue_handler_drops_beyond_its_depth_and_never_blocks():
    handler = DropQueueHandler(queue.SimpleQueue(), max_depth=3)
    lg = logging.getLogger("flightrisk.unit.simple")
    lg.propagate = False
    lg.addHandler(handler)
    try:
        for i in range(10):
            lg.warning("record %d", i)
    finally:
        lg.removeHandler(handler)
        lg.propagate = True
    assert handler.dropped == 7 and handler.queue.qsize() == 3


def test_a_telemetry_call_made_while_the_thread_is_inside_telemetry_is_skipped():
    from flightrisk.drone.fake import FakeDrone
    from flightrisk.drone.instrument import instrument_drone

    drone = instrument_drone(FakeDrone(name="sim-sig"), "sim")
    drone.connect()
    drone.takeoff()
    assert obs_log.obs_enter()
    try:
        result = drone.land()  # a signal handler landing the drone while telemetry code is running
    finally:
        obs_log.obs_exit()
    assert result.ok  # the command ran; only its telemetry was skipped


# ---------------------------------------------------------------------------
# Exit with a console that stopped reading
# ---------------------------------------------------------------------------

def test_closing_the_console_handler_does_not_wait_forever_for_a_stuck_writer(monkeypatch):
    monkeypatch.setattr(obs_log, "LISTENER_STOP_TIMEOUT_S", 0.3, raising=False)
    release = threading.Event()

    class Stuck(logging.Handler):
        def emit(self, record):
            release.wait(10)

    q: queue.Queue = queue.Queue()
    handler = DropQueueHandler(q)
    listener = logging.handlers.QueueListener(q, Stuck())
    listener.start()
    handler.listener = listener
    handler.handle(logging.LogRecord("flightrisk.x", logging.INFO, __file__, 1, "m", (), None))
    time.sleep(0.1)
    closer = threading.Thread(target=handler.close, daemon=True)
    closer.start()
    closer.join(3)
    hung = closer.is_alive()
    release.set()
    assert not hung, "process exit would wait for a console that stopped reading"


# ---------------------------------------------------------------------------
# Distinct errors are not folded into one
# ---------------------------------------------------------------------------

class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.addFilter(ContextFilter())

    def emit(self, record):
        self.records.append(record)


def test_error_events_for_different_drones_are_not_folded_together(monkeypatch):
    """A land failure on drone B right after one on drone A is a different failure."""
    monkeypatch.setattr(obs_log, "_error_throttle", Throttle(60.0))
    collect = _Collect()
    lg = logging.getLogger("flightrisk.unit.fold")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.addHandler(collect)
    try:
        for drone in ("a", "b", "a"):
            log_event(lg, logging.ERROR, "drone_comm", command="land", drone_id=drone)
    finally:
        lg.removeHandler(collect)
        lg.propagate = True
    assert [r.fr_fields["drone_id"] for r in collect.records] == ["a", "b"]  # the second "a" is a repeat


def test_the_error_counter_label_is_bounded(monkeypatch):
    seen = []

    class Spy:
        def add(self, value, attributes=None):
            seen.append(attributes)

    monkeypatch.setattr(M, "error_logs", Spy())
    M._error_hook("dash board", "some free text error", "abc123", "error")
    assert seen[0]["error_type"] == "unknown" and seen[0]["component"] == "unknown"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, leaked", [
    ("connect failed ws://edge:s3cr3tpw@ground.example.com:8765/ws", "s3cr3tpw"),
    ("ConnectionRefused wss://tok-9f8e7d6c@ground.example.com/ws", "tok-9f8e7d6c"),
    ("stream rtsp://admin:hunter22@192.168.1.5/live", "hunter22"),
])
def test_credentials_in_a_url_are_redacted(text, leaked):
    out = redact_text(text)
    assert leaked not in out and "[REDACTED]@" in out
    assert "ground.example.com" in out or "192.168.1.5" in out  # the host stays


def test_a_url_without_credentials_and_an_at_sign_in_a_path_are_kept():
    assert redact_text("see http://h/p@q and ws://10.0.0.1:8765") == "see http://h/p@q and ws://10.0.0.1:8765"


def test_a_stream_url_with_credentials_never_becomes_a_metric_label():
    assert safe_token("rtsp://admin:secret@192.168.1.5/live") == "rtsp://192.168.1.5/live"
    assert "secret" not in safe_token("ws://user:secret@host")


# ---------------------------------------------------------------------------
# Artifact store and preview server
# ---------------------------------------------------------------------------

def _img():
    img = np.zeros((300, 400, 3), np.uint8)
    img[:] = (10, 20, 200)
    cv2.putText(img, "T", (50, 200), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 5)
    return img


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", "0")
    monkeypatch.delenv("FLIGHTRISK_PREVIEW_TTL_S", raising=False)
    s = ArtifactStore(tmp_path / "art", secret=b"k" * 32)
    yield s
    s.stop_preview_server()


def _jpeg_with_gps() -> bytes:
    from PIL import Image

    exif = Image.Exif()
    exif[0x010F] = "SecretPhoneMaker"  # Make
    exif[0x8825] = {1: "N", 2: (40.0, 45.0, 1.0), 3: "W", 4: (111.0, 53.0, 2.0)}  # GPSInfo
    out = io.BytesIO()
    Image.fromarray(cv2.cvtColor(_img(), cv2.COLOR_BGR2RGB)).save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_stored_photos_carry_no_exif_or_gps(store):
    raw = _jpeg_with_gps()
    assert b"Exif" in raw and b"SecretPhoneMaker" in raw  # the upload has metadata
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    fields = store.store_target(img, raw=raw)
    stored = (store.targets / f"{fields['sha256']}.jpg").read_bytes()
    assert b"Exif" not in stored and b"SecretPhoneMaker" not in stored
    assert cv2.imdecode(np.frombuffer(stored, np.uint8), cv2.IMREAD_COLOR) is not None


def test_the_signed_link_is_short_lived_by_default_and_the_ttl_is_clamped(tmp_path, monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_PREVIEW_TTL_S", raising=False)
    assert ArtifactStore(tmp_path / "a", secret=b"k" * 32).ttl_s == 3600
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_TTL_S", "99999999")
    assert ArtifactStore(tmp_path / "b", secret=b"k" * 32).ttl_s == 7 * 86400
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_TTL_S", "0.001")
    assert ArtifactStore(tmp_path / "c", secret=b"k" * 32).ttl_s == 30


def test_preview_expires_matches_the_expiry_signed_into_the_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", str(_free_port()))
    ticks = iter(float(t) for t in range(1_800_000_000, 1_800_000_100))
    s = ArtifactStore(tmp_path / "art", secret=b"k" * 32, clock=lambda: next(ticks))
    try:
        fields = s.store_target(_img())
    finally:
        s.stop_preview_server()
    exp = int(re.search(r"exp=(\d+)", fields["preview_url"]).group(1))
    assert fields["preview_expires"] == exp


def test_an_empty_or_short_key_file_is_replaced_not_used(tmp_path, monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_PREVIEW_SECRET", raising=False)
    root = tmp_path / "art"
    root.mkdir(mode=0o700)
    (root / "preview.key").write_bytes(b"")  # an empty HMAC key would let anyone sign a link
    s = ArtifactStore(root)
    assert len(s._key()) == 32
    assert (root / "preview.key").stat().st_size == 32


def test_a_symlink_in_the_store_is_not_followed_when_serving(store, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_bytes(b"not a thumbnail")
    sha = "c" * 64
    store._ensure_dirs()
    (store.targets / f"{sha}.thumb.jpg").symlink_to(secret)
    assert store.thumbnail_bytes(sha) is None


def test_a_signature_with_non_ascii_characters_is_a_404_not_a_dropped_connection(store, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", str(port))
    sha = "d" * 64
    url = f"http://127.0.0.1:{port}/target/{sha}.jpg?exp=4102444800&sig=%C3%A9"
    store.ensure_preview_server()
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(url, timeout=5)
    assert err.value.code == 404
    assert not store.verify(sha, "4102444800", "é")


def test_the_preview_server_rate_limits(store, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("FLIGHTRISK_PREVIEW_PORT", str(port))
    store.ensure_preview_server()
    codes = []
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nothing", timeout=5)
        except urllib.error.HTTPError as err:
            codes.append(err.code)
    assert 429 in codes and codes[0] == 404


def test_a_stalled_client_cannot_hold_a_server_thread_forever():
    from flightrisk.obs_artifacts import _Handler

    assert _Handler.timeout and _Handler.timeout <= 30


def test_cleanup_removes_stale_temporary_files_but_only_inside_the_store(tmp_path):
    now = [1_800_000_000.0]
    s = ArtifactStore(tmp_path / "art", secret=b"k" * 32, clock=lambda: now[0])
    s._ensure_dirs()
    old = s.targets / f".{'e' * 64}.jpg.abcd.tmp"
    old.write_bytes(b"x")
    import os

    os.utime(old, (now[0] - 7200, now[0] - 7200))
    outside = tmp_path / "art" / "keep.tmp"
    outside.write_bytes(b"x")
    os.utime(outside, (now[0] - 7200, now[0] - 7200))
    s.cleanup()
    assert not old.exists() and outside.exists()


def test_an_existing_artifact_directory_keeps_its_mode(tmp_path):
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    s = ArtifactStore(root, secret=b"k" * 32)
    s._ensure_dirs()
    assert (root.stat().st_mode & 0o777) == 0o755 and (s.targets.stat().st_mode & 0o777) == 0o700


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# Transport hook: nothing from telemetry may escape into the transport
# ---------------------------------------------------------------------------

def test_broadcast_control_survives_a_client_that_has_no_payload_yet():
    """With protocol v2 a client can be skipped (no payload before the hello). The size handed to
    the telemetry hook was taken from the last loop variable, which could then be None."""
    from flightrisk.transport import GroundTransport

    gt = GroundTransport(host="127.0.0.1", port=0, token="t", on_control=lambda m: None)
    gt._eligible_clients = lambda: ["ws-ready", "ws-no-hello"]  # type: ignore[method-assign]
    gt._payload_for = lambda ws, message: '{"type": "land"}' if ws == "ws-ready" else None  # type: ignore[method-assign]
    sent = []

    async def send_json(ws, payload):
        sent.append(ws)
        return True

    gt._send_json = send_json  # type: ignore[method-assign]
    assert asyncio.run(gt.broadcast_control(fp.make_land())) == 1
    assert sent == ["ws-ready"]


def test_a_hook_that_fails_does_not_raise_into_the_transport(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("metrics are down")

    monkeypatch.setattr(M, "comm_messages", type("B", (), {"add": staticmethod(boom)})())
    assert obs_comm.ground_received({"type": "ack", "drone_id": "d1"}, 10) is None
    assert obs_comm.edge_received({"type": "land"}, 10) is None
    assert obs_comm.ground_broadcast({"type": "land"}, 1, 1, 10) is None


# ---------------------------------------------------------------------------
# Metric label cardinality for network input
# ---------------------------------------------------------------------------

def test_message_types_and_drone_ids_from_the_network_are_bounded_labels(monkeypatch):
    seen = []

    class Spy:
        def add(self, value, attributes=None):
            seen.append(dict(attributes or {}))

    monkeypatch.setattr(M, "comm_messages", Spy())
    monkeypatch.setattr(M, "_drone_labels", set(), raising=False)
    monkeypatch.setattr(logging.getLogger("flightrisk.comm"), "disabled", True)
    for i in range(500):
        obs_comm.ground_received({"type": f"invented-{i}", "drone_id": f"drone-{i}"}, 10)
    assert len({s["type"] for s in seen}) == 1 and seen[0]["type"] == "other"
    assert len({s["drone_id"] for s in seen}) <= getattr(M, "MAX_DRONE_LABELS", 64) + 1
    obs_comm.ground_received({"type": "land", "drone_id": "drone-0"}, 10)
    assert seen[-1]["type"] == "land" and seen[-1]["drone_id"] == "drone-0"


# ---------------------------------------------------------------------------
# Alert rules: names that exist, and event rules that survive a restart
# ---------------------------------------------------------------------------

def _emitted_metric_names() -> set[str]:
    names = set()
    for path in (ROOT / "flightrisk").rglob("*.py"):
        text = path.read_text()
        for m in re.finditer(r'(?:create_\w+|_counter|_hist|_gauge|_observable)\(\s*"(flightrisk\.[a-z_.0-9]+)"', text):
            names.add(m.group(1).replace(".", "_"))
    full = set(names)
    for n in names:
        full |= {f"{n}_bucket", f"{n}_sum", f"{n}_count"}
    return full


def _alert_exprs():
    rules = yaml.safe_load((ROOT / "observability" / "grafana" / "alerting" / "flightrisk-rules.yml").read_text())
    for group in rules["groups"]:
        for rule in group["rules"]:
            for d in rule["data"]:
                if d["model"].get("expr"):
                    yield rule["uid"], d["model"]["expr"]


def test_every_metric_in_an_alert_rule_is_emitted_by_the_code():
    emitted = _emitted_metric_names()
    assert len(emitted) > 50  # the scan found the instruments
    for uid, expr in _alert_exprs():
        for name in re.findall(r"\bflightrisk_[a-z_0-9]+", expr):
            assert name in emitted, f"{uid}: {name} is not emitted"


def test_event_counter_rules_are_decided_per_series_not_as_a_difference_of_sums():
    """``sum(now) - sum(5 minutes ago)`` falls when an old process's series expires, which hid the
    events of the process that replaced it. The rules use increase() and 'new series' per series."""
    event_rules = {"fr-link-loss-landing", "fr-land-failure", "fr-outbox-drops"}
    found = set()
    for uid, expr in _alert_exprs():
        if uid in event_rules:
            found.add(uid)
            assert "increase(" in expr and " unless " in expr and "offset 5m" in expr
            assert not re.search(r"\)\)\s*-\s*\(sum", expr), f"{uid}: difference of sums"
            assert expr.startswith("sum by"), f"{uid}: sum after the per-series decision"
    assert found == event_rules


def test_the_alert_rule_expressions_balance_their_parentheses():
    for uid, expr in _alert_exprs():
        assert expr.count("(") == expr.count(")"), uid
        json.dumps(expr)
