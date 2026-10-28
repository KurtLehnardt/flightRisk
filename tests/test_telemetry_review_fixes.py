"""Regression tests for the retroactive review findings on the observability stack."""

import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("opentelemetry.sdk")

from flightrisk import observability
from flightrisk import telemetry as t

ROOT = Path(__file__).resolve().parent.parent
CLEAN_ENV = ("FLIGHTRISK_OTEL_ENABLED", "OTEL_EXPORTER", "OTEL_ENDPOINT", "OTEL_RESOURCE_ATTRIBUTES",
             "OTEL_EXPORTER_OTLP_TIMEOUT", "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
             "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT", "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT")


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(t, "_initialized", False)
    monkeypatch.setattr(t, "_providers", [])
    monkeypatch.setattr(t, "_log_handler", None)
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    handlers_before = list(observability._extra_handlers)
    captured = {}
    with patch.object(t.atexit, "register"), \
            patch.object(t.trace, "set_tracer_provider", side_effect=lambda p: captured.__setitem__("tracer", p)), \
            patch.object(t.metrics, "set_meter_provider", side_effect=lambda p: captured.__setitem__("meter", p)), \
            patch.object(t, "set_logger_provider", side_effect=lambda p: captured.__setitem__("logger", p)):
        yield captured
    t.shutdown_telemetry(timeout_s=2.0)
    for h in list(observability._extra_handlers):
        if h not in handlers_before:
            observability._extra_handlers.remove(h)


def _exporters():
    import io

    from opentelemetry.sdk._logs import export as logs_export
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    log_cls = getattr(logs_export, "InMemoryLogRecordExporter", None) or logs_export.InMemoryLogExporter
    return InMemorySpanExporter(), ConsoleMetricExporter(out=io.StringIO()), log_cls()


def _init_and_get_resource_attrs(fresh):
    spans, metric_exp, logs = _exporters()
    with patch.object(t, "_build_span_exporter", return_value=spans), \
            patch.object(t, "_build_metric_exporter", return_value=metric_exp), \
            patch.object(t, "_build_log_exporter", return_value=logs):
        assert t.init_telemetry() is True
    with fresh["tracer"].get_tracer("test").start_as_current_span("x"):
        pass
    fresh["tracer"].force_flush()
    [span] = spans.get_finished_spans()
    return dict(span.resource.attributes)


# --- T1: service.instance.id -------------------------------------------------


def test_unnamed_process_is_the_node_unknown_not_a_random_uuid(fresh, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    attrs = _init_and_get_resource_attrs(fresh)
    assert attrs["service.instance.id"] == "unknown"


@pytest.mark.parametrize("raw", ["garbage", "=x,,service.instance.id", "a=b,c", "service.instance.id=", " , "])
def test_malformed_resource_attributes_still_give_a_stable_node(fresh, monkeypatch, raw):
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", raw)
    attrs = _init_and_get_resource_attrs(fresh)
    assert attrs["service.instance.id"] == "unknown"


def test_named_process_keeps_its_name_and_other_attributes(fresh, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "team=sar, service.instance.id=edge-9")
    attrs = _init_and_get_resource_attrs(fresh)
    assert attrs["service.instance.id"] == "edge-9"
    assert attrs["team"] == "sar"


# --- T2: missing packages are not silent --------------------------------------


def _run_blocked(code: str, **env):
    full_env = {k: v for k, v in os.environ.items() if k not in CLEAN_ENV}
    full_env.update(env)
    prelude = "import sys\nfor m in ('opentelemetry',):\n    sys.modules[m] = None\n"
    return subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)], cwd=ROOT, env=full_env,
                          capture_output=True, text=True, timeout=60, check=False)


def test_enabled_without_the_packages_warns():
    result = _run_blocked(
        """
        from flightrisk import telemetry as t
        assert t.HAS_OTEL is False
        print("RESULT", t.init_telemetry())
        """,
        FLIGHTRISK_OTEL_ENABLED="true",
    )
    assert "RESULT False" in result.stdout, result.stderr
    assert "opentelemetry_unavailable" in result.stderr
    assert '"level": "warning"' in result.stderr


def test_disabled_without_the_packages_stays_quiet():
    result = _run_blocked("from flightrisk import telemetry as t\nprint('RESULT', t.init_telemetry())\n")
    assert "RESULT False" in result.stdout
    assert "opentelemetry_unavailable" not in result.stderr


def test_edge_requirements_pin_the_otel_packages_like_the_main_file():
    def specs(name):
        lines = (ROOT / name).read_text().splitlines()
        return {m.group(1): m.group(2) for line in lines if (m := re.match(r"^(opentelemetry-[\w-]+)(.*)$", line.strip()))}

    edge, main = specs("requirements-edge.txt"), specs("requirements.txt")
    for package in ("opentelemetry-api", "opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-grpc"):
        assert edge.get(package) == main[package], package


# --- T3: unusable exporter ----------------------------------------------------


def test_unknown_exporter_warns_with_the_valid_values(fresh, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER", "otlp_grpc")
    _init_and_get_resource_attrs(fresh)
    text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "otlp_grpc" in text and "otlp-grpc" in text and "otlp-http" in text and "console" in text


def test_missing_exporter_package_warns(fresh, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER", "otlp-grpc")
    monkeypatch.setattr(t, "_HAS_OTLP_GRPC", False)
    _init_and_get_resource_attrs(fresh)
    assert any("opentelemetry-exporter-otlp-proto-grpc" in r.getMessage() for r in caplog.records)


def test_valid_exporters_do_not_warn(fresh, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER", "OTLP-GRPC")
    _init_and_get_resource_attrs(fresh)
    assert not [r for r in caplog.records if "OTEL_EXPORTER" in r.getMessage()]


# --- T4: FLIGHTRISK_OTEL_ENABLED parsing ---------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " true ", "\ton\n"])
def test_enabled_accepts_the_usual_spellings(fresh, monkeypatch, caplog, value):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", value)
    assert t._otel_enabled() is True
    assert not caplog.records


@pytest.mark.parametrize("value", ["", "0", "false", "No", "off", "  "])
def test_disabled_spellings_are_silent(monkeypatch, caplog, value):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", value)
    assert t._otel_enabled() is False
    assert not caplog.records


@pytest.mark.parametrize("value", ["enable", "2", "ture", "enabled"])
def test_unrecognised_value_warns_and_stays_off(monkeypatch, caplog, value):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", value)
    assert t._otel_enabled() is False
    assert any(value in r.getMessage() and "FLIGHTRISK_OTEL_ENABLED" in r.getMessage() for r in caplog.records)


# --- shutdown / partial init ---------------------------------------------------


def test_manual_shutdown_then_init_builds_live_providers_again(fresh, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER", "otlp-grpc")
    calls = []

    def build_span(*_a):
        calls.append(1)
        return _exporters()[0]

    with patch.object(t, "_build_span_exporter", side_effect=build_span), \
            patch.object(t, "_build_metric_exporter", return_value=_exporters()[1]), \
            patch.object(t, "_build_log_exporter", return_value=_exporters()[2]):
        assert t.init_telemetry() is True
        assert t.shutdown_telemetry(timeout_s=2.0) is True
        assert t._initialized is False
        assert t.init_telemetry() is True
    assert len(calls) == 2
    assert len(t._providers) == 3


def test_partial_init_failure_shuts_down_the_providers_already_built(fresh, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    stopped = []

    class _Tracer(t.TracerProvider):
        def shutdown(self):
            stopped.append("tracer")
            super().shutdown()

    monkeypatch.setattr(t, "TracerProvider", _Tracer)
    with patch.object(t, "_build_span_exporter", return_value=_exporters()[0]), \
            patch.object(t, "_build_metric_exporter", side_effect=RuntimeError("no metrics")), \
            pytest.raises(RuntimeError):
        t.init_telemetry()
    assert stopped == ["tracer"]
    assert t._providers == [] and t._initialized is False


# --- per-signal timeouts --------------------------------------------------------


@pytest.mark.parametrize("signal", ["traces", "metrics", "logs"])
def test_per_signal_timeout_is_left_to_the_sdk(monkeypatch, signal):
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    assert t._export_timeout(signal) == t._EXPORT_TIMEOUT_S
    monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_TIMEOUT", "9")
    assert t._export_timeout(signal) is None
    other = "logs" if signal != "logs" else "traces"
    assert t._export_timeout(other) == t._EXPORT_TIMEOUT_S  # another signal's variable does not leak


def test_builders_pass_the_signal_to_the_timeout_rule(monkeypatch):
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "9")
    with patch.object(t, "OTLPGrpcSpanExporter") as span_cls, patch.object(t, "OTLPGrpcMetricExporter") as metric_cls:
        t._build_span_exporter("otlp-grpc", "http://127.0.0.1:1")
        t._build_metric_exporter("otlp-grpc", "http://127.0.0.1:1")
    assert span_cls.call_args.kwargs["timeout"] is None
    assert metric_cls.call_args.kwargs["timeout"] == t._EXPORT_TIMEOUT_S


# --- logging handler availability ------------------------------------------------


def test_missing_log_handler_warns_instead_of_silently_dropping_logs(fresh, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("FLIGHTRISK_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER", "otlp-grpc")
    monkeypatch.setattr(t, "_HAS_LOGS", False)
    monkeypatch.setattr(t, "_LOGS_UNAVAILABLE_REASON", "LoggingHandler was removed")
    spans, metric_exp, _logs = _exporters()
    with patch.object(t, "_build_span_exporter", return_value=spans), \
            patch.object(t, "_build_metric_exporter", return_value=metric_exp):
        assert t.init_telemetry() is True
    assert any("opentelemetry_logs_unavailable" in r.getMessage() and "LoggingHandler was removed" in r.getMessage()
              for r in caplog.records)


# --- real process exit with the default budget -------------------------------------


def test_process_exit_with_a_black_hole_collector_is_bounded_by_the_default_budget():
    import socket

    blackhole = socket.socket()
    blackhole.bind(("127.0.0.1", 0))
    blackhole.listen(0)
    port = blackhole.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in CLEAN_ENV}
    env.update(FLIGHTRISK_OTEL_ENABLED="true", OTEL_EXPORTER="otlp-grpc", OTEL_ENDPOINT=f"http://127.0.0.1:{port}")
    code = textwrap.dedent(
        """
        from flightrisk import telemetry as t
        from flightrisk.observability import StructuredLogger
        assert t.init_telemetry() is True
        tracer = t.get_tracer("exit-test")
        log = StructuredLogger(component="exit-test")
        for i in range(50):
            with tracer.start_as_current_span("detect"):
                log.info("tick", i=i)
        print("READY", flush=True)
        """
    )
    try:
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        assert proc.stdout.readline().strip() == "READY"
        start = time.monotonic()
        proc.wait(timeout=60)
        exit_s = time.monotonic() - start
    finally:
        blackhole.close()
    assert proc.returncode == 0
    # The default budget is 3 s; allow for interpreter teardown, but the SDK's
    # own unbounded flush would take about 30 s against this listener.
    assert exit_s < t._SHUTDOWN_BUDGET_S + 4.0, exit_s


# --- Grafana, compose and the pre-flight script -------------------------------------


def test_tempo_queries_format_the_multi_select_node_variable_as_a_regex():
    for path in sorted((ROOT / "observability" / "grafana" / "dashboards").glob("*.json")):
        dash = json.loads(path.read_text())
        for panel in dash.get("panels", []):
            for target in panel.get("targets", []):
                query = target.get("query") or ""
                if target.get("queryType") == "traceql" and "$node" in query:
                    assert "${node:regex}" in query, f"{path.name}: {panel.get('title')}"
                    assert "=~" in query


def _compose(name):
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / name).read_text())


@pytest.mark.parametrize("name", ["docker-compose.otel.yml", "docker-compose.yml", "docker-compose.grafana.yml"])
def test_otlp_and_jaeger_ports_bind_to_loopback_by_default(name):
    for service in _compose(name)["services"].values():
        for port in service.get("ports", []):
            text = str(port)
            if re.search(r"(?<!\d)(4317|4318|16686):", text):
                assert text.startswith("${OTLP_BIND:-127.0.0.1}:"), (name, text)


def test_a_missing_cable_address_cannot_stop_prometheus_or_grafana():
    services = _compose("docker-compose.grafana.yml")["services"]
    assert "otel-collector" not in (services["prometheus"].get("depends_on") or [])
    for dep in ("prometheus", "otel-collector"):
        assert dep not in (services["grafana"].get("depends_on") or [])


@pytest.mark.parametrize(
    ("value", "ok"),
    [("127.0.0.1", True), ("10.42.0.1", True), ("", True), ("0.0.0.0", False), ("::", False),
     ("192.168.1.5", False), ("localhost", False)],
)
def test_check_otlp_bind_script(value, ok):
    env = {k: v for k, v in os.environ.items() if k != "OTLP_BIND"}
    env["OTLP_BIND"] = value
    result = subprocess.run(["bash", str(ROOT / "scripts" / "check_otlp_bind.sh")], env=env,
                            capture_output=True, text=True, timeout=20, check=False)
    assert (result.returncode == 0) is ok, result.stderr
    if not ok:
        assert "no authentication" in result.stderr
    if value == "10.42.0.1":
        assert "no authentication" in result.stdout
