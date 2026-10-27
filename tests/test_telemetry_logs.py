"""Tests for shipping StructuredLogger output to OpenTelemetry."""

import logging
from unittest.mock import patch

import pytest

from flightrisk import observability
from flightrisk.observability import StructuredLogger, register_log_handler


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def clean_registry():
    """Undo any handlers a test registers so other tests see a clean state."""
    before = list(observability._extra_handlers)
    yield
    for h in list(observability._extra_handlers):
        if h not in before:
            observability._extra_handlers.remove(h)
            for lg in observability._structured_loggers:
                lg.removeHandler(h)


class TestRegisterLogHandler:
    def test_attaches_to_existing_logger(self, clean_registry):
        log = StructuredLogger(component="otel-test-existing")
        handler = _ListHandler()
        register_log_handler(handler)

        log.info("hello", n=1)

        assert len(handler.messages) == 1
        assert '"event": "hello"' in handler.messages[0]

    def test_attaches_to_logger_created_later(self, clean_registry):
        handler = _ListHandler()
        register_log_handler(handler)

        log = StructuredLogger(component="otel-test-later")
        log.warning("late")

        assert len(handler.messages) == 1
        assert '"level": "warning"' in handler.messages[0]

    def test_registering_twice_does_not_duplicate(self, clean_registry):
        log = StructuredLogger(component="otel-test-dupe")
        handler = _ListHandler()
        register_log_handler(handler)
        register_log_handler(handler)
        StructuredLogger(component="otel-test-dupe")  # same logger again

        log.info("once")

        assert len(handler.messages) == 1


class TestInitLogs:
    @pytest.fixture
    def telemetry(self, clean_registry):
        pytest.importorskip("opentelemetry.sdk._logs")
        from flightrisk import telemetry as t

        saved = t._log_handler
        t._log_handler = None
        yield t
        t._log_handler = saved

    def test_exports_log_records_once_even_if_initialised_twice(self, telemetry):
        from opentelemetry.sdk._logs import export as logs_export
        from opentelemetry.sdk.resources import Resource

        # Renamed in newer SDKs; the old name still works on older ones.
        InMemoryLogExporter = getattr(
            logs_export, "InMemoryLogRecordExporter", None
        ) or logs_export.InMemoryLogExporter
        SimpleLogRecordProcessor = logs_export.SimpleLogRecordProcessor

        exporter = InMemoryLogExporter()
        log = StructuredLogger(component="otel-test-export")

        with patch.object(telemetry, "_build_log_exporter", return_value=exporter), \
                patch.object(telemetry, "BatchLogRecordProcessor", SimpleLogRecordProcessor):
            telemetry._init_logs(Resource.create({}), "otlp-grpc", "http://x:4317")
            telemetry._init_logs(Resource.create({}), "otlp-grpc", "http://x:4317")

        log.error("boom", code=7)

        records = exporter.get_finished_logs()
        assert len(records) == 1
        body = records[0].log_record.body
        assert '"event": "boom"' in body
        assert records[0].log_record.severity_text == "ERROR"

    def test_console_mode_does_not_register_a_handler(self, telemetry):
        from opentelemetry.sdk.resources import Resource

        telemetry._init_logs(Resource.create({}), "console", "http://x:4317")

        assert telemetry._log_handler is None
        assert observability._extra_handlers == []
