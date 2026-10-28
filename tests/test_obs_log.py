"""The shared JSON log pipeline (flightrisk/obs_log.py)."""

import json
import logging
import queue
import re
import time

import pytest

from flightrisk import obs_log
from flightrisk.obs_log import (
    ContextFilter,
    DropQueueHandler,
    JsonFormatter,
    Throttle,
    build_payload,
    error_fingerprint,
    log_context,
    log_event,
    notice,
    set_session_id,
    setup_logging,
)
from flightrisk.observability import StructuredLogger

ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.addFilter(ContextFilter())

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def collect():
    setup_logging()
    handler = _Collect()
    root = logging.getLogger("flightrisk")
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)
    set_session_id(None)


def _payload(record):
    return json.loads(JsonFormatter().format(record))


def test_json_line_has_the_shared_fields(collect, monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=mac-test,foo=bar")
    set_session_id("session-42")
    with log_context(drone_id="drone-7"):
        log_event(logging.getLogger("flightrisk.dashboard.x"), logging.INFO, "user_input", "hello", category="user_input",
                  action="set_threshold", threshold=0.5)
    p = _payload(collect.records[-1])
    assert ISO_UTC.match(p["timestamp"])
    assert p["level"] == "info"
    assert p["logger"] == "flightrisk.dashboard.x"
    assert p["component"] == "dashboard"
    assert p["event"] == "user_input"
    assert p["message"] == "hello"
    assert p["category"] == "user_input"
    assert p["node"] == "mac-test"
    assert p["session_id"] == "session-42"
    assert p["drone_id"] == "drone-7"
    assert p["action"] == "set_threshold" and p["threshold"] == 0.5


def test_trace_and_span_ids_from_the_active_span(collect):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")  # local provider: the global one is untouched
    with tracer.start_as_current_span("unit") as span:
        logging.getLogger("flightrisk.t").info("inside")
        ctx = span.get_span_context()
    p = _payload(collect.records[-1])
    assert p["trace_id"] == f"{ctx.trace_id:032x}"
    assert p["span_id"] == f"{ctx.span_id:016x}"
    logging.getLogger("flightrisk.t").info("outside")
    assert "trace_id" not in _payload(collect.records[-1])
    assert trace.get_current_span().get_span_context().is_valid is False


def test_structured_logger_records_use_the_same_format(collect):
    StructuredLogger(component="obs-format").info("edge_connect", ws_url="ws://127.0.0.1:9000", authenticated=True)
    record = collect.records[-1]
    # The record message is still the legacy JSON entry, for register_log_handler users.
    assert '"event": "edge_connect"' in record.getMessage()
    p = _payload(record)
    assert p["event"] == "edge_connect" and p["component"] == "obs-format" and p["authenticated"] is True


def test_reserved_keys_are_not_overwritten(collect):
    logging.getLogger("flightrisk.t").info(
        "e", extra={"fr_event": "e", "fr_fields": {"level": "spoofed", "message": "also spoofed"}})
    p = _payload(collect.records[-1])
    assert p["level"] == "info"
    assert p["field_level"] == "spoofed"
    assert p["field_message"] == "also spoofed"


def test_fields_are_redacted(collect):
    log_event(logging.getLogger("flightrisk.t"), logging.WARNING, "call_failed",
              error="401 for key sk-ant-api03-FAKEfakeFAKE0123456789", api_key="whatever")
    p = _payload(collect.records[-1])
    assert "FAKEfake" not in json.dumps(p)
    assert p["api_key"] == "[REDACTED]"


def _raise(kind):
    raise kind("boom")


def test_fingerprint_groups_by_type_and_frames():
    infos = []
    for kind in (ValueError, ValueError, KeyError):
        try:
            _raise(kind)
        except Exception as exc:
            infos.append((type(exc), exc, exc.__traceback__))
    a, b, c = (error_fingerprint(i)[0] for i in infos)
    assert a == b
    assert a != c
    assert error_fingerprint(infos[0])[1] == "ValueError"
    assert error_fingerprint(None, logger_name="x", template="t")[1] == "logged_error"


def test_repeated_errors_are_folded_and_stack_only_at_debug(collect, monkeypatch):
    monkeypatch.setattr(obs_log, "_error_throttle", Throttle(60.0))
    lg = logging.getLogger("flightrisk.repeat")
    for _ in range(3):
        try:
            _raise(RuntimeError)
        except RuntimeError:
            lg.exception("worker failed")
    kept = [r for r in collect.records if r.name == "flightrisk.repeat"]
    assert len(kept) == 1  # the two repeats inside the window are dropped by the handler filter
    p = _payload(kept[0])
    assert p["error_type"] == "RuntimeError" and p["fingerprint"] and "stack" not in p

    monkeypatch.setattr(obs_log, "_error_throttle", Throttle(60.0))
    root = logging.getLogger("flightrisk")
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        try:
            _raise(TypeError)
        except TypeError:
            lg.exception("debug failure")
        assert "Traceback" in _payload(collect.records[-1])["stack"]
    finally:
        root.setLevel(old)


def test_error_hook_counts_every_error(collect, monkeypatch):
    seen = []
    monkeypatch.setattr(obs_log, "_error_hook", lambda *a: seen.append(a))
    monkeypatch.setattr(obs_log, "_error_throttle", Throttle(60.0))
    for _ in range(3):
        logging.getLogger("flightrisk.hooked").error("same failure")
    assert len(seen) == 3  # metrics count repeats the log folds
    assert seen[0][0] == "hooked" and seen[0][3] == "error"


def test_drop_queue_handler_never_blocks():
    handler = DropQueueHandler(queue.Queue(maxsize=2))
    lg = logging.getLogger("flightrisk.unit.dropq")
    lg.propagate = False
    lg.addHandler(handler)
    try:
        t0 = time.perf_counter()
        for i in range(500):
            lg.warning("record %d", i)
        elapsed = time.perf_counter() - t0
    finally:
        lg.removeHandler(handler)
        lg.propagate = True
    assert handler.dropped == 498
    assert elapsed < 1.0


def test_queued_record_keeps_structured_fields():
    q = queue.Queue()
    handler = DropQueueHandler(q)
    record = logging.LogRecord("flightrisk.x", logging.INFO, __file__, 1, "n=%d", (3,), None)
    record.fr_event = "e"
    record.fr_fields = {"k": 1}
    handler.handle(record)
    queued = q.get_nowait()
    assert queued.getMessage() == "n=3"
    assert build_payload(queued)["k"] == 1


def test_notice_prints_once_and_logs_structured(collect, capsys, monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_CONSOLE_ECHO", raising=False)
    notice(logging.getLogger("flightrisk.tello"), "tello_connected", "[drone-1] Connected. Battery: 80%", battery_pct=80)
    assert capsys.readouterr().out == "[drone-1] Connected. Battery: 80%\n"
    record = collect.records[-1]
    assert record.fr_event == "tello_connected" and record.fr_console_echo is True
    assert _payload(record)["battery_pct"] == 80
    # The JSON console skips it (already printed); other handlers keep it.
    assert obs_log._NotEchoed().filter(record) is False


def test_notice_echo_can_be_turned_off(collect, capsys, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_CONSOLE_ECHO", "0")
    notice(logging.getLogger("flightrisk.tello"), "e", "quiet line")
    assert capsys.readouterr().out == ""
    assert collect.records[-1].fr_console_echo is False


def test_throttle():
    now = [0.0]
    t = Throttle(5.0, clock=lambda: now[0])
    assert t.allow("k") == (True, 0)
    assert t.allow("k") == (False, 0)
    assert t.allow("k") == (False, 0)
    assert t.allow("other") == (True, 0)
    now[0] = 6.0
    assert t.allow("k") == (True, 2)


def test_per_frame_structured_helpers_are_throttled(collect, monkeypatch):
    from flightrisk import observability

    monkeypatch.setattr(observability, "_per_frame_throttle", Throttle(60.0))
    slog = StructuredLogger(component="obs-perframe")
    for _ in range(10):
        slog.face_result(success=False, score=0.1)
    assert sum(1 for r in collect.records if getattr(r, "fr_event", None) == "face_result"
               and r.name == "flightrisk.obs-perframe") == 1


def test_setup_logging_is_idempotent_and_owns_the_root_handler():
    setup_logging()
    root = logging.getLogger("flightrisk")
    count = len(root.handlers)
    setup_logging()
    assert len(root.handlers) == count
    # A handler on the Python root logger makes a library's basicConfig() a no-op.
    assert any(isinstance(h, DropQueueHandler) for h in logging.getLogger().handlers)
