"""Tests for init_telemetry(): no-op when disabled, idempotent when enabled,
per-node resource attributes, and a bounded exit flush.

No test here touches the global OTel providers: the set_*_provider calls are
patched so the providers stay local to each test.
"""

import io
import socket
import time
from unittest.mock import patch

import pytest

pytest.importorskip("opentelemetry.sdk")

from flightrisk import edge_device, observability
from flightrisk import telemetry as t
from flightrisk.observability import StructuredLogger


@pytest.fixture
def fresh(monkeypatch):
    """Reset telemetry module state and undo any log handler a test adds."""
    monkeypatch.setattr(t, "_initialized", False)
    monkeypatch.setattr(t, "_providers", [])
    monkeypatch.setattr(t, "_log_handler", None)
    for name in ("FLIGHTRISK_OTEL_ENABLED", "OTEL_EXPORTER", "OTEL_ENDPOINT",
                 "OTEL_RESOURCE_ATTRIBUTES", "OTEL_EXPORTER_OTLP_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    handlers_before = list(observability._extra_handlers)
    registered = []
    captured = {}
    with patch.object(t.atexit, "register", side_effect=registered.append), \
            patch.object(t.trace, "set_tracer_provider",
                         side_effect=lambda p: captured.__setitem__("tracer", p)), \
            patch.object(t.metrics, "set_meter_provider",
                         side_effect=lambda p: captured.__setitem__("meter", p)), \
            patch.object(t, "set_logger_provider",
                         side_effect=lambda p: captured.__setitem__("logger", p)):
        yield {"atexit": registered, "providers": captured}
    t.shutdown_telemetry(timeout_s=2.0)
    for h in list(observability._extra_handlers):
        if h not in handlers_before:
            observability._extra_handlers.remove(h)
            for lg in observability._structured_loggers:
                lg.removeHandler(h)


def _in_memory_exporters():
    from opentelemetry.sdk._logs import export as logs_export
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    log_cls = getattr(logs_export, "InMemoryLogRecordExporter", None) or logs_export.InMemoryLogExporter
    return InMemorySpanExporter(), ConsoleMetricExporter(out=io.StringIO()), log_cls()


class TestInitTelemetry:
    def test_disabled_is_a_no_op(self, fresh):
        with patch.object(t, "_build_span_exporter") as build:
            assert t.init_telemetry() is False

        build.assert_not_called()
        assert t._providers == []
        assert fresh["atexit"] == []
        assert t._log_handler is None

    def test_enabled_exports_with_node_id(self, fresh, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
        monkeypatch.setenv("OTEL_EXPORTER", "otlp-grpc")
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=edge-test")
        spans, metric_exp, logs = _in_memory_exporters()

        with patch.object(t, "_build_span_exporter", return_value=spans), \
                patch.object(t, "_build_metric_exporter", return_value=metric_exp), \
                patch.object(t, "_build_log_exporter", return_value=logs):
            assert t.init_telemetry() is True

        tracer = fresh["providers"]["tracer"].get_tracer("test")
        with tracer.start_as_current_span("detect"):
            pass
        StructuredLogger(component="otel-init-test").info("hello", n=1)
        fresh["providers"]["tracer"].force_flush()
        fresh["providers"]["logger"].force_flush()

        [span] = spans.get_finished_spans()
        assert span.name == "detect"
        attrs = span.resource.attributes
        assert attrs["service.instance.id"] == "edge-test"
        assert attrs["service.name"] == "flightrisk"  # explicit value wins over env
        bodies = [r.log_record.body for r in logs.get_finished_logs()]
        assert any('"event": "hello"' in b for b in bodies)
        assert fresh["atexit"] == [t.shutdown_telemetry]
        assert len(t._providers) == 3

    def test_second_call_is_harmless(self, fresh, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
        monkeypatch.setenv("OTEL_EXPORTER", "otlp-grpc")
        spans, metric_exp, logs = _in_memory_exporters()

        with patch.object(t, "_build_span_exporter", return_value=spans) as b_span, \
                patch.object(t, "_build_metric_exporter", return_value=metric_exp), \
                patch.object(t, "_build_log_exporter", return_value=logs):
            assert t.init_telemetry() is True
            providers = list(t._providers)
            assert t.init_telemetry() is True

        assert b_span.call_count == 1
        assert t._providers == providers
        assert fresh["atexit"] == [t.shutdown_telemetry]

        StructuredLogger(component="otel-init-twice").info("once")
        fresh["providers"]["logger"].force_flush()
        bodies = [r.log_record.body for r in logs.get_finished_logs()]
        assert sum('"event": "once"' in b for b in bodies) == 1

    def test_exporters_get_a_short_timeout(self, fresh, monkeypatch):
        exp = t._build_span_exporter("otlp-grpc", "http://127.0.0.1:1")
        assert exp._timeout == t._EXPORT_TIMEOUT_S
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "7")
        assert t._build_span_exporter("otlp-grpc", "http://127.0.0.1:1")._timeout == 7.0


class _StuckProvider:
    def __init__(self):
        self.called = False

    def shutdown(self):
        self.called = True
        time.sleep(5)


class TestShutdownTelemetry:
    def test_gives_up_after_the_budget(self, fresh):
        stuck = _StuckProvider()
        t._providers.append(stuck)

        start = time.monotonic()
        assert t.shutdown_telemetry(timeout_s=0.2) is False
        assert time.monotonic() - start < 1.0
        assert stuck.called
        assert t._providers == []

    def test_unreachable_collector_drops_telemetry_without_blocking(self, fresh, monkeypatch):
        # A socket that listens but never accepts: connects hang like an
        # unplugged cable, so every export runs into its deadline.
        blackhole = socket.socket()
        blackhole.bind(("127.0.0.1", 0))
        blackhole.listen(0)
        port = blackhole.getsockname()[1]
        monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
        monkeypatch.setenv("OTEL_EXPORTER", "otlp-grpc")
        monkeypatch.setenv("OTEL_ENDPOINT", f"http://127.0.0.1:{port}")
        try:
            assert t.init_telemetry() is True
            tracer = fresh["providers"]["tracer"].get_tracer("test")
            log = StructuredLogger(component="otel-blackhole-test")
            otel_m = t.FlightRiskMetrics(fresh["providers"]["meter"].get_meter("test"))

            start = time.monotonic()
            for i in range(300):
                with tracer.start_as_current_span("detect"):
                    otel_m.record_frame(10.0, 1, 30.0)
                    log.info("tick", i=i)
            hot_path_s = time.monotonic() - start

            start = time.monotonic()
            t.shutdown_telemetry(timeout_s=1.0)
            shutdown_s = time.monotonic() - start
        finally:
            blackhole.close()

        assert hot_path_s < 1.0  # 300 frames' worth; ~0.1 s on a laptop
        assert shutdown_s < 1.5


class TestEdgeTelemetry:
    def test_disabled_is_a_no_op(self, fresh):
        assert edge_device._init_edge_telemetry(StructuredLogger(component="edge-otel")) is False
        assert t._providers == []

    def test_init_failure_never_stops_the_edge(self, fresh, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
        with patch.object(t, "_build_span_exporter", side_effect=RuntimeError("no grpc")):
            assert edge_device._init_edge_telemetry(StructuredLogger(component="edge-otel")) is False

    def test_main_initialises_telemetry_before_running(self, monkeypatch):
        calls = []
        monkeypatch.setattr(edge_device, "_init_edge_telemetry", lambda log: calls.append("otel"))
        monkeypatch.setattr(edge_device, "_run_flight_edge", lambda args, log, **kwargs: calls.append("run"))

        edge_device.main(["--source", "fake"])

        assert calls == ["otel", "run"]
