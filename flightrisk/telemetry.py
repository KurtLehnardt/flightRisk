"""OpenTelemetry instrumentation for FlightRisk.

Provides distributed tracing, metrics, and structured logs exportable
to any OTel-compatible backend (Jaeger, Grafana, Datadog, console).
Logs are only exported in the otlp-* modes. For the bundled Grafana stack
(Tempo + Prometheus + Loki) see ``observability/README.md``.

Enable via env var: FLIGHTRISK_OTEL_ENABLED=true
Configure exporter: OTEL_EXPORTER=console|otlp-grpc|otlp-http
Configure endpoint: OTEL_ENDPOINT=http://localhost:4317
"""

import os
import logging
import threading

logger = logging.getLogger(__name__)

# Attempt to import OpenTelemetry packages — if not installed, everything
# degrades to no-ops so the rest of the application keeps working.
try:
    from opentelemetry import trace, metrics
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource

    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False

# OTLP exporters are optional — only needed for otlp-grpc / otlp-http modes.
_HAS_OTLP_GRPC = False
_HAS_OTLP_HTTP = False
_HAS_LOGS = False
_log_handler = None

# Match scores live in [0, 1]; the SDK's default histogram buckets
# (0, 5, 10, ...) would put every observation in the first bucket.
_MATCH_SCORE_BUCKETS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0]

try:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    _HAS_LOGS = True
except ImportError:
    pass

if HAS_OTEL:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as OTLPGrpcSpanExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter as OTLPGrpcMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter as OTLPGrpcLogExporter,
        )
        _HAS_OTLP_GRPC = True
    except ImportError:
        pass

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as OTLPHttpSpanExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter as OTLPHttpMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter as OTLPHttpLogExporter,
        )
        _HAS_OTLP_HTTP = True
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_telemetry(
    service_name: str = "flightrisk",
    environment: str = "development",
) -> bool:
    """Initialise OpenTelemetry tracing and metrics.

    Returns True when OTel providers are active, False otherwise.
    """
    if not HAS_OTEL:
        logger.debug("opentelemetry packages not installed — telemetry disabled")
        return False

    enabled = os.environ.get("FLIGHTRISK_OTEL_ENABLED", "false").lower() == "true"
    if not enabled:
        # Explicitly set no-op providers so any stray get_tracer / get_meter
        # calls produce harmless no-op objects.
        trace.set_tracer_provider(trace.NoOpTracerProvider())
        return False

    exporter_type = os.environ.get("OTEL_EXPORTER", "console").lower()
    endpoint = os.environ.get("OTEL_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({
        "service.name": service_name,
        "service.version": "0.1.0",
        "deployment.environment": environment,
    })

    # --- Trace provider ---
    span_exporter = _build_span_exporter(exporter_type, endpoint)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metric provider ---
    metric_exporter = _build_metric_exporter(exporter_type, endpoint)
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=10_000,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
        views=[
            View(
                instrument_name="flightrisk.match.score",
                aggregation=ExplicitBucketHistogramAggregation(_MATCH_SCORE_BUCKETS),
            ),
        ],
    )
    metrics.set_meter_provider(meter_provider)

    # --- Log provider (OTLP only; console mode already prints to stdout) ---
    _init_logs(resource, exporter_type, endpoint)

    logger.info(
        "OpenTelemetry enabled (exporter=%s, endpoint=%s)", exporter_type, endpoint
    )
    return True


def get_tracer(name: str = "flightrisk") -> "trace.Tracer":
    """Return a Tracer (no-op if OTel is disabled or not installed)."""
    if not HAS_OTEL:
        return _NoOpTracer()
    return trace.get_tracer(name)


def get_meter(name: str = "flightrisk") -> "metrics.Meter":
    """Return a Meter (no-op if OTel is disabled or not installed)."""
    if not HAS_OTEL:
        return _NoOpMeter()
    return metrics.get_meter(name)


# ---------------------------------------------------------------------------
# FlightRiskMetrics — pre-defined metric instruments
# ---------------------------------------------------------------------------

class FlightRiskMetrics:
    """Holds all Amber-specific OTel metric instruments."""

    def __init__(self, meter):
        self.frame_duration = meter.create_histogram(
            "flightrisk.frame.duration_ms",
            unit="ms",
            description="Frame processing latency",
        )
        self.detections_count = meter.create_up_down_counter(
            "flightrisk.detections.count",
            description="Persons detected",
        )
        self.match_score = meter.create_histogram(
            "flightrisk.match.score",
            description="Match confidence scores",
        )
        self.errors_count = meter.create_counter(
            "flightrisk.errors.count",
            description="Pipeline errors",
        )
        self.reasoning_duration = meter.create_histogram(
            "flightrisk.reasoning.duration_ms",
            unit="ms",
            description="Gemma 4 reasoning latency",
        )
        self.match_count = meter.create_counter(
            "flightrisk.match.count",
            description="Total matches",
        )

        # Gauges — the SDK added synchronous Gauge in 1.26+.  Fall back to
        # a simple counter-based approximation when the create_gauge method
        # is not available.
        self._face_successes = 0
        self._face_total = 0
        self._face_lock = threading.Lock()

        try:
            self.face_detection_rate = meter.create_gauge(
                "flightrisk.face.detection_rate",
                unit="%",
                description="Face detection success rate",
            )
            self.fps_gauge = meter.create_gauge(
                "flightrisk.fps",
                description="Frames per second",
            )
            self.drone_battery = meter.create_gauge(
                "flightrisk.drone.battery",
                unit="%",
                description="Drone battery level",
            )
            self._has_gauge = True
        except AttributeError:
            # Older SDK without synchronous Gauge — use ObservableGauge
            self._latest_fps = 0.0
            self._latest_battery = 0.0
            self._has_gauge = False

            meter.create_observable_gauge(
                "flightrisk.face.detection_rate",
                callbacks=[self._observe_face_rate],
                unit="%",
                description="Face detection success rate",
            )
            meter.create_observable_gauge(
                "flightrisk.fps",
                callbacks=[self._observe_fps],
                description="Frames per second",
            )
            meter.create_observable_gauge(
                "flightrisk.drone.battery",
                callbacks=[self._observe_battery],
                unit="%",
                description="Drone battery level",
            )

    # --- observable callbacks (fallback path) ---

    def _observe_face_rate(self, observer):
        with self._face_lock:
            rate = (self._face_successes / self._face_total * 100) if self._face_total else 0.0
        observer.observe(rate)

    def _observe_fps(self, observer):
        observer.observe(self._latest_fps)

    def _observe_battery(self, observer):
        observer.observe(self._latest_battery)

    # --- convenience recording methods ---

    def record_frame(self, duration_ms: float, persons_detected: int, fps: float):
        """Record frame-level metrics."""
        self.frame_duration.record(duration_ms)
        self.detections_count.add(persons_detected)
        if self._has_gauge:
            self.fps_gauge.set(fps)
        else:
            self._latest_fps = fps

    def record_match(self, score: float, match_type: str = "photo"):
        """Record a match event."""
        self.match_score.record(score, {"match_type": match_type})
        self.match_count.add(1, {"match_type": match_type})

    def record_face_check(self, found: bool):
        """Track face detection success / failure."""
        with self._face_lock:
            self._face_total += 1
            if found:
                self._face_successes += 1
            if self._has_gauge:
                rate = (self._face_successes / self._face_total * 100) if self._face_total else 0.0
                self.face_detection_rate.set(rate)

    def record_reasoning(self, duration_ms: float):
        """Record Gemma 4 reasoning latency."""
        self.reasoning_duration.record(duration_ms)

    def record_error(self, component: str, error_type: str):
        """Increment the error counter."""
        self.errors_count.add(1, {"component": component, "error_type": error_type})

    def record_battery(self, level: int):
        """Record current drone battery level."""
        if self._has_gauge:
            self.drone_battery.set(level)
        else:
            self._latest_battery = float(level)


# ---------------------------------------------------------------------------
# Exporter builders (internal)
# ---------------------------------------------------------------------------

def _build_span_exporter(exporter_type: str, endpoint: str):
    if exporter_type == "otlp-grpc" and _HAS_OTLP_GRPC:
        return OTLPGrpcSpanExporter(endpoint=endpoint, insecure=True)
    if exporter_type == "otlp-http" and _HAS_OTLP_HTTP:
        http_endpoint = endpoint.replace(":4317", ":4318")
        return OTLPHttpSpanExporter(endpoint=f"{http_endpoint}/v1/traces")
    return ConsoleSpanExporter()


def _build_log_exporter(exporter_type: str, endpoint: str):
    """Return an OTLP log exporter, or None for console mode / missing deps."""
    if exporter_type == "otlp-grpc" and _HAS_OTLP_GRPC:
        return OTLPGrpcLogExporter(endpoint=endpoint, insecure=True)
    if exporter_type == "otlp-http" and _HAS_OTLP_HTTP:
        http_endpoint = endpoint.replace(":4317", ":4318")
        return OTLPHttpLogExporter(endpoint=f"{http_endpoint}/v1/logs")
    return None


def _init_logs(resource, exporter_type: str, endpoint: str) -> None:
    """Ship StructuredLogger output to the OTLP backend alongside stdout."""
    global _log_handler
    # init_telemetry() runs again on every dashboard restart; a second handler
    # would ship each record twice.
    if not _HAS_LOGS or _log_handler is not None:
        return
    log_exporter = _build_log_exporter(exporter_type, endpoint)
    if log_exporter is None:
        return

    from flightrisk.observability import register_log_handler

    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(log_provider)
    _log_handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
    register_log_handler(_log_handler)


def _build_metric_exporter(exporter_type: str, endpoint: str):
    if exporter_type == "otlp-grpc" and _HAS_OTLP_GRPC:
        return OTLPGrpcMetricExporter(endpoint=endpoint, insecure=True)
    if exporter_type == "otlp-http" and _HAS_OTLP_HTTP:
        http_endpoint = endpoint.replace(":4317", ":4318")
        return OTLPHttpMetricExporter(endpoint=f"{http_endpoint}/v1/metrics")
    return ConsoleMetricExporter()


# ---------------------------------------------------------------------------
# No-op stubs — used when opentelemetry is not installed at all
# ---------------------------------------------------------------------------

class _NoOpSpan:
    """Minimal stand-in for a Span when OTel is not installed."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key, value):
        pass

    def set_status(self, status):
        pass

    def add_event(self, name, attributes=None):
        pass


class _NoOpTracer:
    """Returns _NoOpSpan for every start_as_current_span call."""

    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()

    def start_span(self, name, **kwargs):
        return _NoOpSpan()


class _NoOpMeter:
    """Returns inert instrument stubs."""

    def create_histogram(self, *a, **kw):
        return _NoOpInstrument()

    def create_counter(self, *a, **kw):
        return _NoOpInstrument()

    def create_up_down_counter(self, *a, **kw):
        return _NoOpInstrument()

    def create_gauge(self, *a, **kw):
        return _NoOpInstrument()

    def create_observable_gauge(self, *a, **kw):
        return _NoOpInstrument()


class _NoOpInstrument:
    def record(self, value, attributes=None):
        pass

    def add(self, value, attributes=None):
        pass

    def set(self, value, attributes=None):
        pass
