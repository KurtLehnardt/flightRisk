"""OpenTelemetry instrumentation for FlightRisk.

Provides distributed tracing, metrics, and structured logs exportable
to any OTel-compatible backend (Jaeger, Grafana, Datadog, console).
Logs are only exported in the otlp-* modes. For the bundled Grafana stack
(Tempo + Prometheus + Loki) see ``observability/README.md``.

Enable via env var: FLIGHTRISK_OTEL_ENABLED=true (1, true, yes or on)
Configure exporter: OTEL_EXPORTER=console|otlp-grpc|otlp-http
Configure endpoint: OTEL_ENDPOINT=http://localhost:4317
Name this process: OTEL_RESOURCE_ATTRIBUTES=service.instance.id=edge-2
(standard OTel variable; merged into the resource, so each laptop shows up
as its own node in Grafana). A process that does not name itself is the node
``unknown`` (the SDK would otherwise invent a new random id on every start).

Export never blocks the caller: spans, metrics and logs are queued and sent
from SDK background threads. An unreachable collector only drops telemetry,
and the flush at exit gives up after ``_SHUTDOWN_BUDGET_S`` seconds.
"""

import atexit
import os
import logging
import threading
import time
import warnings

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

# Set once the OTel providers exist; init_telemetry() is then a no-op.
_initialized = False
# Providers to flush and stop at exit (see shutdown_telemetry).
_providers: list = []

# One OTLP export attempt, retries included, gives up after this many
# seconds (the SDK default is 10). Keeps the background export threads from
# sitting on a dead collector. OTEL_EXPORTER_OTLP_TIMEOUT overrides it.
_EXPORT_TIMEOUT_S = 3.0
# Total time the exit-time flush may take. With an unreachable collector the
# SDK defaults would hold the process for about 30 s.
_SHUTDOWN_BUDGET_S = 3.0

# Match scores live in [0, 1]; the SDK's default histogram buckets
# (0, 5, 10, ...) would put every observation in the first bucket.
_MATCH_SCORE_BUCKETS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0]

_LOGS_UNAVAILABLE_REASON = "opentelemetry log SDK not installed"
try:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    try:
        # Deprecated in the SDK in favour of opentelemetry-instrumentation-logging;
        # a future SDK may drop it, so fall back to the new home.
        from opentelemetry.sdk._logs import LoggingHandler
    except ImportError:
        from opentelemetry.instrumentation.logging.handler import LoggingHandler

    _HAS_LOGS = True
except ImportError as _logs_import_error:
    _LOGS_UNAVAILABLE_REASON = f"opentelemetry log SDK unavailable ({_logs_import_error})"

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

    Returns True when OTel providers are active, False otherwise. Safe to
    call more than once: after the first successful call it returns True
    without building a second set of providers and exporter threads.
    """
    global _initialized
    # The JSON log format applies whether or not OpenTelemetry is on.
    from flightrisk.obs_log import setup_logging

    setup_logging()

    enabled = _otel_enabled()
    if not HAS_OTEL:
        if enabled:
            # A node that was asked to export must not stay silent about it.
            logger.warning(
                "opentelemetry_unavailable: FLIGHTRISK_OTEL_ENABLED is set but the "
                "opentelemetry packages are not installed; telemetry is off "
                "(pip install opentelemetry-api opentelemetry-sdk "
                "opentelemetry-exporter-otlp-proto-grpc)"
            )
        else:
            logger.debug("opentelemetry packages not installed — telemetry disabled")
        return False

    if not enabled:
        # Explicitly set no-op providers so any stray get_tracer / get_meter
        # calls produce harmless no-op objects.
        trace.set_tracer_provider(trace.NoOpTracerProvider())
        return False

    if _initialized:
        return True

    exporter_type = os.environ.get("OTEL_EXPORTER", "console").lower()
    endpoint = os.environ.get("OTEL_ENDPOINT", "http://localhost:4317")
    _warn_if_exporter_unusable(exporter_type)

    attributes = {
        "service.name": service_name,
        "service.version": "0.1.0",
        "deployment.environment": environment,
        # Several processes share one node (the ground station and edge-1 on
        # the Mac, or the fake fleet's edges). Without a per-process
        # attribute their series have identical labels and the collector's
        # Prometheus exporter overwrites one process's counters with
        # another's. Loki keeps it as structured metadata, not a label.
        "process.pid": os.getpid(),
    }
    if not _env_instance_id():
        # The SDK invents a random UUID when none is set, which would make
        # every restart of an unnamed process a new Loki stream and a new
        # Prometheus series. An explicit attribute beats the env variable.
        attributes["service.instance.id"] = _UNNAMED_NODE
    resource = Resource.create(attributes)

    tracer_provider = meter_provider = log_provider = None
    try:
        # --- Trace provider ---
        span_exporter = _build_span_exporter(exporter_type, endpoint)
        # shutdown_on_exit=False everywhere: shutdown_telemetry() replaces the
        # SDK's unbounded atexit flush with one that has a time budget.
        tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
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
            shutdown_on_exit=False,
            views=_histogram_views(),
        )
        metrics.set_meter_provider(meter_provider)

        # --- Log provider (OTLP only; console mode already prints to stdout) ---
        log_provider = _init_logs(resource, exporter_type, endpoint)

    except BaseException:
        # Providers created before the failure would keep their exporter
        # threads running for the life of the process.
        _providers.extend(p for p in (log_provider, tracer_provider, meter_provider) if p is not None)
        shutdown_telemetry()
        raise

    _providers.extend(p for p in (log_provider, tracer_provider, meter_provider) if p is not None)
    atexit.register(shutdown_telemetry)
    _initialized = True

    logger.info(
        "OpenTelemetry enabled (exporter=%s, endpoint=%s)", exporter_type, endpoint
    )
    return True


_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")
_VALID_EXPORTERS = ("console", "otlp-grpc", "otlp-http")
# service.instance.id of a process that did not name itself.
_UNNAMED_NODE = "unknown"


def _otel_enabled() -> bool:
    """FLIGHTRISK_OTEL_ENABLED: 1/true/yes/on (any case, surrounding spaces ignored)."""
    raw = os.environ.get("FLIGHTRISK_OTEL_ENABLED", "")
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value and value not in _FALSE_VALUES:
        logger.warning(
            "FLIGHTRISK_OTEL_ENABLED=%r is not recognised; telemetry stays off "
            "(use one of: %s)", raw, ", ".join(_TRUE_VALUES),
        )
    return False


def _env_instance_id() -> str:
    """service.instance.id from OTEL_RESOURCE_ATTRIBUTES, or '' when unset or malformed."""
    for item in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip() == "service.instance.id" and value.strip():
            return value.strip()
    return ""


def _warn_if_exporter_unusable(exporter_type: str) -> None:
    """Warn when the exporter silently falls back to the console."""
    if exporter_type not in _VALID_EXPORTERS:
        logger.warning(
            "OTEL_EXPORTER=%r is not valid; exporting to the console instead "
            "(valid values: %s)", exporter_type, ", ".join(_VALID_EXPORTERS),
        )
    elif exporter_type == "otlp-grpc" and not _HAS_OTLP_GRPC:
        logger.warning(
            "OTEL_EXPORTER=otlp-grpc but opentelemetry-exporter-otlp-proto-grpc is "
            "not installed; exporting to the console instead"
        )
    elif exporter_type == "otlp-http" and not _HAS_OTLP_HTTP:
        logger.warning(
            "OTEL_EXPORTER=otlp-http but opentelemetry-exporter-otlp-proto-http is "
            "not installed; exporting to the console instead"
        )


def _histogram_views() -> list:
    """Explicit bucket boundaries for every FlightRisk histogram.

    The SDK default buckets (0, 5, 10, 25, ...) suit neither scores in [0, 1]
    nor latencies that span 1 ms to a minute. Exemplars (the trace id of a
    sampled observation) are attached by the SDK's default trace-based
    exemplar filter and exposed by the collector in OpenMetrics format.
    """
    from flightrisk.obs_metrics import HISTOGRAM_BUCKETS

    buckets = dict(HISTOGRAM_BUCKETS)
    buckets.setdefault("flightrisk.match.score", _MATCH_SCORE_BUCKETS)
    return [
        View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(bounds))
        for name, bounds in buckets.items()
    ]


def shutdown_telemetry(timeout_s: float = _SHUTDOWN_BUDGET_S) -> bool:
    """Flush and stop the OTel providers, giving up after ``timeout_s``.

    Registered with ``atexit`` by ``init_telemetry``. Each provider shuts
    down on its own daemon thread; whatever has not been sent when the budget
    runs out is dropped, so a missing collector never holds up the exit.
    Returns True when every provider finished in time.
    """
    global _initialized, _log_handler
    providers = list(_providers)
    _providers.clear()
    # A later init_telemetry() must build live providers, not report True for
    # the dead ones; the log handler is bound to a provider that is now stopped.
    _initialized = False
    _log_handler = None
    threads = []
    for provider in providers:
        t = threading.Thread(
            target=_shutdown_quietly, args=(provider,), name="otel-shutdown", daemon=True
        )
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout_s
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in threads)


def _shutdown_quietly(provider) -> None:
    try:
        provider.shutdown()
    except Exception:  # never let telemetry teardown raise at exit
        logger.debug("telemetry provider shutdown failed", exc_info=True)


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

def _export_timeout(signal: str = ""):
    """Per-export timeout in seconds.

    None lets the SDK use OTEL_EXPORTER_OTLP_TIMEOUT or the per-signal
    OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_TIMEOUT.
    """
    names = ["OTEL_EXPORTER_OTLP_TIMEOUT"]
    if signal:
        names.append(f"OTEL_EXPORTER_OTLP_{signal.upper()}_TIMEOUT")
    if any(os.environ.get(name) for name in names):
        return None
    return _EXPORT_TIMEOUT_S


def _build_span_exporter(exporter_type: str, endpoint: str):
    if exporter_type == "otlp-grpc" and _HAS_OTLP_GRPC:
        return OTLPGrpcSpanExporter(endpoint=endpoint, insecure=True, timeout=_export_timeout("traces"))
    if exporter_type == "otlp-http" and _HAS_OTLP_HTTP:
        http_endpoint = endpoint.replace(":4317", ":4318")
        return OTLPHttpSpanExporter(endpoint=f"{http_endpoint}/v1/traces", timeout=_export_timeout("traces"))
    return ConsoleSpanExporter()


def _build_log_exporter(exporter_type: str, endpoint: str):
    """Return an OTLP log exporter, or None for console mode / missing deps."""
    if exporter_type == "otlp-grpc" and _HAS_OTLP_GRPC:
        return OTLPGrpcLogExporter(endpoint=endpoint, insecure=True, timeout=_export_timeout("logs"))
    if exporter_type == "otlp-http" and _HAS_OTLP_HTTP:
        http_endpoint = endpoint.replace(":4317", ":4318")
        return OTLPHttpLogExporter(endpoint=f"{http_endpoint}/v1/logs", timeout=_export_timeout("logs"))
    return None


def _init_logs(resource, exporter_type: str, endpoint: str):
    """Ship every ``flightrisk.*`` log record to the OTLP backend.

    The handler sits on the ``flightrisk`` logger, which every module logger
    and every ``StructuredLogger`` propagates to, so one handler covers all
    of them. Its body is the same JSON line the console shows (with the
    trace and span ids of the active span), and the ``BatchLogRecordProcessor``
    exports from its own thread: a caller never waits on the collector, and
    when the collector is unreachable records are queued and then dropped,
    never blocked on.

    Returns the new LoggerProvider, or None when nothing was set up.
    """
    global _log_handler
    # init_telemetry() runs again on every dashboard restart; a second handler
    # would ship each record twice.
    if _log_handler is not None:
        return None
    if not _HAS_LOGS:
        if exporter_type in ("otlp-grpc", "otlp-http"):
            logger.warning("opentelemetry_logs_unavailable: logs will not be exported: %s", _LOGS_UNAVAILABLE_REASON)
        return None
    log_exporter = _build_log_exporter(exporter_type, endpoint)
    if log_exporter is None:
        return None

    from flightrisk.obs_log import JsonFormatter, install_otel_handler

    log_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(log_provider)
    with warnings.catch_warnings():
        # The SDK marks its LoggingHandler deprecated in favour of a separate
        # package; it is the same handler and works unchanged.
        warnings.simplefilter("ignore", DeprecationWarning)
        handler = _FlightRiskLogHandler(level=logging.NOTSET, logger_provider=log_provider)
    handler.setFormatter(JsonFormatter())
    install_otel_handler(handler)
    _log_handler = handler
    return log_provider


if _HAS_LOGS:

    class _FlightRiskLogHandler(LoggingHandler):
        """OTel log handler whose attributes are a few bounded fields.

        The SDK default copies every ``LogRecord`` attribute; the FlightRisk
        records carry their fields in the JSON body instead, so only the
        event name, category, component and drone id are attached (as
        ``fr.*``, which Loki stores as structured metadata ``fr_event`` and
        so on, without clashing with the keys of the JSON body).
        """

        @staticmethod
        def _get_attributes(record: logging.LogRecord):
            attrs = {}
            event = getattr(record, "fr_event", None)
            if event:
                attrs["fr.event"] = str(event)
            category = getattr(record, "fr_category", None)
            if category:
                attrs["fr.category"] = str(category)
            component = getattr(record, "fr_component", None)
            if component:
                attrs["fr.component"] = str(component)
            fields = getattr(record, "fr_fields", None) or {}
            drone_id = fields.get("drone_id") or getattr(record, "fr_drone_id", None)
            if isinstance(drone_id, str) and drone_id:
                attrs["fr.drone_id"] = drone_id
            fingerprint = getattr(record, "fr_fingerprint", None)
            if fingerprint:
                attrs["fr.fingerprint"] = fingerprint
            return attrs


def _build_metric_exporter(exporter_type: str, endpoint: str):
    if exporter_type == "otlp-grpc" and _HAS_OTLP_GRPC:
        return OTLPGrpcMetricExporter(endpoint=endpoint, insecure=True, timeout=_export_timeout("metrics"))
    if exporter_type == "otlp-http" and _HAS_OTLP_HTTP:
        http_endpoint = endpoint.replace(":4317", ":4318")
        return OTLPHttpMetricExporter(endpoint=f"{http_endpoint}/v1/metrics", timeout=_export_timeout("metrics"))
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
