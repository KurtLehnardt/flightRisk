#!/usr/bin/env python3
"""Generate the FlightRisk Grafana dashboards (dashboards as code).

    python observability/grafana/build_dashboards.py          # write the JSON files
    python observability/grafana/build_dashboards.py --check  # exit 1 if they are stale

Writes five dashboards to ``observability/grafana/dashboards/``; Grafana
provisions them from there (``provisioning/dashboards/dashboards.yml``):

=====================================  ==================  =====================================
Title                                  uid                 Focus
=====================================  ==================  =====================================
FlightRisk / Overview                  fr-overview         golden signals, saturation, SLOs
FlightRisk / Flight Safety & Comms     fr-flight-comm      battery, height, link, commands, comms
FlightRisk / Detection & Matching      fr-detection        stage latency, scores, alerts, models
FlightRisk / Operator Input & Target   fr-operator         user input, target image, responses
FlightRisk / Errors & Logs             fr-errors           error rate, fingerprints, log explorer
=====================================  ==================  =====================================

Edit this file, not the JSON: ``tests/test_grafana_dashboards.py`` fails when
the committed JSON differs from what this script generates. Metric names are
the OTel names from ``flightrisk/obs_metrics.py`` with dots turned into
underscores; log queries parse the JSON line (``| json``) written by
``flightrisk/obs_log.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dashlib import (
    DRONE,
    NODE,
    STREAM,
    TEMPO,
    Dashboard,
    bargauge,
    gauge,
    logql_events,
    logs,
    loki,
    loki_annotation,
    prom,
    stat,
    table,
    text,
    timeseries,
    traces,
    var_component,
    var_drone,
    var_level,
    var_node,
)

OUT_DIR = Path(__file__).resolve().parent / "dashboards"
RI = "$__rate_interval"


def _sel(*parts: str) -> str:
    return "{" + ", ".join(p for p in parts if p) + "}"


def q(metric: str, *filters: str) -> str:
    """A metric with the node filter and any extra label filters."""
    return metric + _sel(NODE, *filters)


def p(quantile: float, hist: str, by: str = "", *filters: str, window: str = RI) -> str:
    group = f"le, {by}" if by else "le"
    return f"histogram_quantile({quantile}, sum by ({group}) (rate({q(hist + '_bucket', *filters)}[{window}])))"


def _annotations(d: Dashboard) -> None:
    d.annotations += [
        loki_annotation("Flight safety", logql_events('category="flight_safety"'), "red", "kind",
                        "{{kind}} {{reason}} {{drone_id}}"),
        loki_annotation("Alerts", logql_events('event="alert" | transition=~"opened|confirmed|dismissed"'), "orange",
                        "transition", "alert {{transition}} ({{awaiting_s}} s)"),
        loki_annotation("Target changes", logql_events('event="target_image"'), "blue", "event", "target from {{source}}"),
    ]


SAFETY_THRESHOLDS = [("red", None), ("orange", 10), ("yellow", 20), ("green", 30)]


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------

def overview() -> Dashboard:
    d = Dashboard("fr-overview", "FlightRisk / Overview",
                  "Golden signals (traffic, errors, latency, saturation), SLOs and recent problems for every FlightRisk process.",
                  ["overview"])
    d.variables = [var_node()]
    _annotations(d)
    total_http = f"(sum(rate({q('flightrisk_http_requests')}[5m])) or vector(0))"
    total_sock = f"(sum(rate({q('flightrisk_socket_events')}[5m])) or vector(0))"
    err_http = f"(sum(rate({q('flightrisk_http_requests', 'outcome=\"error\"')}[5m])) or vector(0))"
    err_sock = f"(sum(rate({q('flightrisk_socket_events', 'outcome=\"error\"')}[5m])) or vector(0))"

    d.row("Golden signals")
    d.add(stat("Uptime", [prom(f"max({q('flightrisk_process_uptime_s')})", instant=True)], "s",
               "Seconds since the newest FlightRisk process on the selected nodes started."), 4, 4)
    d.add(stat("Request rate", [prom(f"sum(rate({q('flightrisk_http_requests')}[{RI}])) + {total_sock}")], "reqps",
               "Dashboard REST requests plus Socket.IO events from the browser, per second."), 4, 4)
    d.add(stat("Handler error ratio (5m)", [prom(f"({err_http} + {err_sock}) / clamp_min({total_http} + {total_sock}, 1e-9)")],
               "percentunit", "Share of REST requests and Socket.IO events whose handler failed (5xx or exception).",
               [("green", None), ("orange", 0.01), ("red", 0.05)]), 4, 4)
    d.add(stat("Frame loop FPS", [prom(f"max({q('flightrisk_fps')})")], "", "Frames per second processed by the dashboard frame loop.",
               [("red", None), ("orange", 2), ("green", 5)], 1), 4, 4)
    d.add(stat("Alert awaiting operator", [prom(f"max({q('flightrisk_alert_phase', 'phase=\"awaiting_response\"')})")], "",
               "1 while a match alert waits for Confirm or Dismiss.", [("green", None), ("red", 1)]), 4, 4)
    d.add(stat("Errors logged (range)", [prom(f"sum(increase({q('flightrisk_error_logs')}[$__range])) or vector(0)", instant=True)],
               "", "ERROR and CRITICAL log records in the selected time range.", [("green", None), ("orange", 1), ("red", 20)], 0), 4, 4)

    d.row("Traffic, errors and latency")
    d.add(timeseries("Traffic", [
        prom(f"sum by (route) (rate({q('flightrisk_http_requests')}[{RI}]))", "REST {{route}}"),
        prom(f"sum by (event) (rate({q('flightrisk_socket_events')}[{RI}]))", "socket {{event}}", "B"),
    ], "reqps", "RED rate: REST requests by route template and Socket.IO events by name."), 8, 9)
    d.add(timeseries("Errors", [
        prom(f"sum by (outcome) (rate({q('flightrisk_socket_events', 'outcome!=\"ok\"')}[{RI}]))", "socket {{outcome}}"),
        prom(f"sum by (outcome) (rate({q('flightrisk_http_requests', 'outcome!=\"ok\"')}[{RI}]))", "REST {{outcome}}", "B"),
        prom(f"sum by (component) (rate({q('flightrisk_error_logs')}[{RI}]))", "logged errors {{component}}", "C"),
    ], "ops", "RED errors: handlers that were rejected, unauthorized or failed, and ERROR log records by component."), 8, 9)
    d.add(timeseries("Latency p50 / p95 / p99", [
        prom(p(0.5, "flightrisk_socket_duration_ms"), "socket p50", exemplar=True),
        prom(p(0.95, "flightrisk_socket_duration_ms"), "socket p95", "B", exemplar=True),
        prom(p(0.99, "flightrisk_socket_duration_ms"), "socket p99", "C"),
        prom(p(0.95, "flightrisk_http_duration_ms"), "REST p95", "D", exemplar=True),
        prom(p(0.95, "flightrisk_frame_duration_ms"), "frame p95", "E", exemplar=True),
    ], "ms", "RED duration. Exemplar dots link to the trace of a sampled request."), 8, 9)

    d.row("Saturation (USE)")
    d.add(timeseries("CPU", [prom(q("flightrisk_process_usage", 'resource="cpu_percent"'), "{{service_instance_id}} {{service_name}}")],
                     "percent", "Process CPU (100 = one core).", 0), 6, 8)
    d.add(timeseries("Memory (RSS)", [prom(q("flightrisk_process_usage", 'resource="memory_rss_bytes"'), "{{service_instance_id}} {{service_name}}")],
                     "bytes", "Resident set size of each process.", 0), 6, 8)
    d.add(timeseries("Threads and open files", [
        prom(q("flightrisk_process_usage", 'resource="threads"'), "threads {{service_instance_id}}"),
        prom(q("flightrisk_process_usage", 'resource="open_fds"'), "fds {{service_instance_id}}", "B"),
    ], "short", "Thread count and open file descriptors.", 0), 6, 8)
    d.add(timeseries("Queues", [
        prom(f"max by (queue) ({q('flightrisk_queue_depth')})", "depth {{queue}}"),
        prom(f"sum by (queue) (increase({q('flightrisk_queue_dropped')}[{RI}]))", "dropped {{queue}}", "B"),
    ], "short", "Depth of the bounded queues (reasoning, log console) and items dropped because one was full.", 0), 6, 8)

    d.row("Service level objectives (1 h)")
    d.add(stat("Frame loop availability", [prom(
        f"avg_over_time((max({q('flightrisk_frame_loop_last_frame_age_s')}) < bool 5)[1h:30s])")], "percentunit",
        "SLI: share of 30 s samples in the last hour where the frame loop processed a frame in the previous 5 s. SLO 99 %.",
        [("red", None), ("orange", 0.95), ("green", 0.99)], 2), 6, 5)
    d.add(stat("Drone command success", [prom(
        f"sum(increase({q('flightrisk_drone_commands', 'outcome=\"ok\"')}[1h])) / clamp_min(sum(increase({q('flightrisk_drone_commands')}[1h])), 1e-9)")],
        "percentunit", "SLI: commands executed (outcome ok) over all commands in the last hour. SLO 99 %.",
        [("red", None), ("orange", 0.95), ("green", 0.99)], 2), 6, 5)
    d.add(stat("Edge link availability", [prom(
        f"avg_over_time((min({q('flightrisk_link_connected')}) >= bool 1)[1h:30s])")], "percentunit",
        "SLI: share of 30 s samples in the last hour where every known edge link was connected. SLO 99 %.",
        [("red", None), ("orange", 0.95), ("green", 0.99)], 2), 6, 5)
    d.add(timeseries("Error-budget burn rate (SLO 99 %)", [
        prom(f"(1 - sum(rate({q('flightrisk_drone_commands', 'outcome=\"ok\"')}[5m])) / clamp_min(sum(rate({q('flightrisk_drone_commands')}[5m])), 1e-9)) / 0.01",
             "commands 5m"),
        prom(f"(1 - avg_over_time((max({q('flightrisk_frame_loop_last_frame_age_s')}) < bool 5)[5m:30s])) / 0.01", "frame loop 5m", "B"),
    ], "short", "1 means the error budget is spent exactly at the SLO pace; above 14 for 5 minutes is a fast burn.", 0,
        thresholds=[("green", None), ("orange", 1), ("red", 14)], threshold_style="line"), 6, 5)

    d.row("Recent problems")
    d.add(logs("Warnings and errors", logql_events('level=~"warning|error|critical"'),
               "Click a line, then the trace_id link, to open its trace in Tempo."), 16, 10)
    d.add(traces("Recent traces", '{resource.service.name=~"flightrisk.*"}', "Most recent traces in Tempo."), 8, 10)
    return d


# ---------------------------------------------------------------------------
# 2. Flight safety and drone communication
# ---------------------------------------------------------------------------

def flight_comm() -> Dashboard:
    d = Dashboard("fr-flight-comm", "FlightRisk / Flight Safety & Comms",
                  "Per-drone battery, height (with its source and age), speed, link health, command outcomes, landings, "
                  "obstacle checks, and every message between the ground station and the edges.", ["safety", "comms"])
    d.variables = [var_node(), var_drone()]
    _annotations(d)

    d.row("Drone state")
    d.add(bargauge("Battery", [prom(f"min by (drone_id) ({q('flightrisk_drone_battery_pct', DRONE)})", "{{drone_id}}", instant=True)],
                   "percent", "Latest battery per drone. Warning at 20 %, critical land at 10 %.", SAFETY_THRESHOLDS), 6, 8)
    d.add(timeseries("Battery over time", [prom(f"min by (drone_id) ({q('flightrisk_drone_battery_pct', DRONE)})", "{{drone_id}}")],
                     "percent", "", 0, 100, thresholds=[("red", None), ("orange", 10), ("yellow", 20), ("transparent", 30)],
                     threshold_style="line+area"), 6, 8)
    d.add(timeseries("Height by source", [prom(f"max by (drone_id, source) ({q('flightrisk_drone_height_m', DRONE)})", "{{drone_id}} {{source}}")],
                     "m", "Height above takeoff and where it came from: tello.h (state field h, polled), tello.tof, sim.height, "
                     "edge.height_cm (edge telemetry), state.height (legacy cache).", 0), 6, 8)
    d.add(timeseries("Telemetry age", [prom(f"max by (drone_id, backend) ({q('flightrisk_drone_telemetry_age_s', DRONE)})", "{{drone_id}} {{backend}}")],
                     "s", "Seconds since the newest measured reading. Grows when a drone stops reporting.", 0,
                     thresholds=[("green", None), ("orange", 2), ("red", 5)], threshold_style="line"), 6, 8)
    d.add(timeseries("Speed", [prom(f"max by (drone_id, axis, source) ({q('flightrisk_drone_speed_mps', DRONE)})", "{{drone_id}} {{axis}} ({{source}})")],
                     "velocityms", "Measured speed per axis and its source (tello.vgx..., sim.vx..., mavlink.velocity).", None), 8, 8)
    d.add(timeseries("Attitude", [prom(f"max by (drone_id, axis) ({q('flightrisk_drone_attitude_deg', DRONE)})", "{{drone_id}} {{axis}}")],
                     "degree", "Pitch, roll and yaw."), 8, 8)
    d.add(stat("Flying", [prom(f"max by (drone_id) ({q('flightrisk_drone_flying', DRONE)})", "{{drone_id}}")], "",
               "1 while the drone reports flying.", [("blue", None), ("green", 1)]), 8, 8)

    d.row("Commands, landings and safety events")
    d.add(timeseries("Command outcomes", [prom(f"sum by (command, outcome) (increase({q('flightrisk_drone_commands', DRONE)}[{RI}]))",
                                               "{{command}} {{outcome}}")],
                     "short", "Every flight command by outcome: ok, dropped_busy, not_executed, rejected, error.", 0, stack=True, bars=True), 8, 8)
    d.add(timeseries("Command latency p95", [prom(p(0.95, "flightrisk_drone_command_duration_ms", "command"), "{{command}}", exemplar=True)],
                     "ms", "Time from command to result (a land includes its confirmation).", 0), 8, 8)
    d.add(timeseries("Land duration", [
        prom(p(0.5, "flightrisk_drone_land_duration_ms", "outcome", window="10m"), "p50 {{outcome}}"),
        prom(p(0.95, "flightrisk_drone_land_duration_ms", "outcome", window="10m"), "p95 {{outcome}}", "B"),
    ], "ms", "Land command until confirmed (or failed), over 10 minute windows.", 0), 8, 8)
    d.add(timeseries("Flight-safety events", [prom(
        f"sum by (kind, reason) (increase({q('flightrisk_flight_safety_events', DRONE)}[{RI}]))", "{{kind}} {{reason}}")],
        "short", "Battery warnings, critical lands, watchdog (link-loss) landings, geofence refusals, obstacle triggers, "
        "land failures, lost drones.", 0, stack=True, bars=True), 12, 8)
    d.add(table("Flight-safety events by kind (range)", [prom(
        f"sum by (kind, reason, drone_id) (increase({q('flightrisk_flight_safety_events', DRONE)}[$__range]))", instant=True)],
        "Counts in the selected time range.",
        [{"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"Value": "events"}}}],
        sort_by=[{"displayName": "events", "desc": True}]), 12, 8)
    d.add(timeseries("Obstacle guard", [
        prom(f"sum by (action) (increase({q('flightrisk_obstacle_checks')}[{RI}]))", "{{action}}"),
    ], "short", "Obstacle checks by decision (clear, go_left, go_right, reverse).", 0, stack=True, bars=True), 12, 7)
    d.add(timeseries("Obstacle depth ratio", [
        prom(p(0.5, "flightrisk_obstacle_depth_ratio", window="5m"), "p50"),
        prom(p(0.9, "flightrisk_obstacle_depth_ratio", window="5m"), "p90", "B"),
    ], "short", "Centre depth over min_safe_depth (relative MiDaS units): above 1 the path is blocked. "
       "The signed margin is in each obstacle log line.", 0,
        thresholds=[("green", None), ("red", 1)], threshold_style="line"), 12, 7)

    d.row("Link health")
    d.add(timeseries("Link age", [prom(f"max by (drone_id, side) ({q('flightrisk_link_age_s', DRONE)})", "{{drone_id}} {{side}}")],
                     "s", "Seconds since the last message on each drone's link. The edge lands its drone after 3 s of ground silence.", 0,
                     thresholds=[("green", None), ("orange", 2), ("red", 3)], threshold_style="line"), 8, 8)
    d.add(timeseries("Clock offset and RTT", [
        prom(f"max by (drone_id) ({q('flightrisk_link_clock_offset_ms', DRONE)})", "offset {{drone_id}}"),
        prom(p(0.95, "flightrisk_comm_rtt_ms", "drone_id", DRONE, window="10m"), "rtt p95 {{drone_id}}", "B"),
    ], "ms", "Clock-sync median offset (edge minus ground) and round-trip time."), 8, 8)
    d.add(timeseries("Connections and reconnects", [
        prom(f"max by (drone_id) ({q('flightrisk_link_connected', DRONE)})", "connected {{drone_id}}"),
        prom(f"sum by (outcome) (increase({q('flightrisk_link_reconnects')}[{RI}]))", "reconnect {{outcome}}", "B"),
    ], "short", "1 while an edge is connected; edge reconnect attempts by outcome.", 0), 8, 8)

    d.row("Ground and edge messages")
    d.add(timeseries("Message rate by type", [prom(
        f"sum by (side, direction, type) (rate({q('flightrisk_comm_messages', DRONE)}[{RI}]))", "{{side}} {{direction}} {{type}}")],
        "ops", "Every fleet message counted on both sides (ground in/out, edge in/out)."), 8, 8)
    d.add(timeseries("Acks and nacks", [prom(
        f"sum by (kind, for, reason) (increase({q('flightrisk_comm_acks', DRONE)}[{RI}]))", "{{kind}} {{for}} {{reason}}")],
        "short", "Edge answers to ground commands; a nack carries the refusal reason.", 0, bars=True), 8, 8)
    d.add(timeseries("Not delivered", [
        prom(f"sum by (side, type, reason) (increase({q('flightrisk_comm_dropped')}[{RI}]))", "{{side}} {{type}} {{reason}}"),
        prom(f"sum by (side, direction) (rate({q('flightrisk_comm_bytes')}[{RI}]))", "bytes/s {{side}} {{direction}}", "B"),
    ], "short", "Messages not delivered (not connected, closed, telemetry queue full) and traffic volume."), 8, 8)
    d.add(logs("Drone communication (live)", logql_events('event="drone_comm" | drone_id=~"$drone_id"'),
               "Commands, acks, nacks, hello, clock, land and emergency messages one per line; telemetry, heartbeats and "
               "pings as one aggregated line per 5 s."), 12, 10)
    d.add(logs("Flight-safety events", logql_events('category="flight_safety"'), ""), 12, 10)
    d.add(logs("Telemetry snapshots (how height and speed were determined)",
               logql_events('event="drone_telemetry_snapshot" | drone_id=~"$drone_id"'),
               "One line per drone every 5 s: each reading's value, unit, source and age."), 24, 9)
    return d


# ---------------------------------------------------------------------------
# 3. Detection and matching
# ---------------------------------------------------------------------------

def detection() -> Dashboard:
    d = Dashboard("fr-detection", "FlightRisk / Detection & Matching",
                  "Frame pipeline stage latency, detections, per-signal match scores, alert levels and latch, model health "
                  "and reasoning metadata.", ["detection"])
    d.variables = [var_node()]
    _annotations(d)

    d.row("Frame pipeline")
    d.add(stat("FPS", [prom(f"max({q('flightrisk_fps')})")], "", "", [("red", None), ("orange", 2), ("green", 5)], 1), 6, 4)
    d.add(stat("Frame p95", [prom(p(0.95, "flightrisk_frame_duration_ms", window="5m"))], "ms", "",
               [("green", None), ("orange", 200), ("red", 500)], 0), 6, 4)
    d.add(stat("Persons per frame p95", [prom(p(0.95, "flightrisk_detections_per_frame", window="5m"))], "", "", None, 1), 6, 4)
    d.add(stat("Frames (range)", [prom(f"sum(increase({q('flightrisk_frames')}[$__range]))", instant=True)], "short", "", None, 0), 6, 4)
    d.add(timeseries("Stage latency p95", [prom(p(0.95, "flightrisk_stage_duration_ms", "stage"), "{{stage}}", exemplar=True)],
                     "ms", "detect, track, reid, face, score, annotate, encode, emit (frame loop) and edge_score (edge detections).", 0), 12, 8)
    d.add(timeseries("Frame latency", [
        prom(p(0.5, "flightrisk_frame_duration_ms"), "p50", exemplar=True),
        prom(p(0.95, "flightrisk_frame_duration_ms"), "p95", "B"),
        prom(p(0.99, "flightrisk_frame_duration_ms"), "p99", "C"),
    ], "ms", "", 0), 12, 8)
    d.add(timeseries("Frames per second by source", [prom(f"sum by (source) (rate({q('flightrisk_frames')}[{RI}]))", "{{source}}")],
                     "fps", "", 0), 12, 7)
    d.add(timeseries("Persons per frame", [
        prom(p(0.5, "flightrisk_detections_per_frame"), "p50"),
        prom(p(0.95, "flightrisk_detections_per_frame"), "p95", "B"),
    ], "short", "", 0), 12, 7)

    d.row("Matching and alerts")
    d.add(timeseries("Signal scores (p50 / p95)", [
        prom(p(0.5, "flightrisk_signal_score", "signal", window="5m"), "p50 {{signal}}"),
        prom(p(0.95, "flightrisk_signal_score", "signal", window="5m"), "p95 {{signal}}", "B"),
    ], "percentunit", "Per-signal score of the best candidate (reid, face, clothing_color, osnet_reid, combined, ...). Numbers only.", 0, 1), 12, 8)
    d.add(timeseries("Alert levels", [prom(f"sum by (level) (increase({q('flightrisk_alert_levels')}[{RI}]))", "{{level}}")],
                     "short", "Scored candidates by alert level (no_match, weak_signal, possible_match, confirmed_match).", 0,
                     stack=True, bars=True), 12, 8)
    d.add(timeseries("Alert latch", [
        prom(f"max by (phase) ({q('flightrisk_alert_phase')})", "{{phase}}"),
        prom(f"sum by (transition) (increase({q('flightrisk_alert_transitions')}[{RI}]))", "{{transition}}", "B"),
    ], "short", "Current phase (1 = active) and transitions: opened, renotified, confirmed, dismissed, aborted.", 0), 12, 8)
    d.add(timeseries("Operator response time", [
        prom(p(0.5, "flightrisk_alert_response_s", "action", window="30m"), "p50 {{action}}"),
        prom(p(0.95, "flightrisk_alert_response_s", "action", window="30m"), "p95 {{action}}", "B"),
    ], "s", "Seconds from alert to Confirm/Dismiss, over 30 minute windows.", 0), 12, 8)

    d.row("Models and reasoning")
    d.add(table("Model loads", [prom(f"sum by (model, outcome) ({q('flightrisk_model_loads')})", instant=True)],
                "Models and signal providers loaded (ok) or not (failed, unavailable) since start.",
                [{"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"Value": "loads"}}}]), 8, 8)
    d.add(timeseries("Reasoning calls", [
        prom(f"sum by (provider, kind, outcome) (increase({q('flightrisk_reasoning_calls')}[{RI}]))", "{{provider}} {{kind}} {{outcome}}"),
    ], "short", "Provider, kind and outcome only; the reasoning text is never logged.", 0, bars=True), 8, 8)
    d.add(timeseries("Reasoning latency and tokens", [
        prom(p(0.95, "flightrisk_reasoning_duration_ms", "provider", window="10m"), "p95 {{provider}}"),
        prom(f"sum by (provider, direction) (increase({q('flightrisk_reasoning_tokens')}[{RI}]))", "tokens {{provider}} {{direction}}", "B"),
    ], "short", "", 0), 8, 8)
    d.add(logs("Pipeline summaries", logql_events('event=~"detection_pipeline|model_load|reasoning"'),
               "One detection_pipeline summary per 5 s (frames, persons, stage means, score summaries); model loads; "
               "reasoning metadata."), 24, 9)
    return d


# ---------------------------------------------------------------------------
# 4. Operator input and target
# ---------------------------------------------------------------------------

TARGET_PATHS = [
    ("source", "source"), ("width", "width"), ("height", "height"), ("bytes", "bytes"), ("mime", "mime"),
    ("quality.overall_score", "quality"), ("quality.grade", "grade"), ("face_detected", "face"),
    ("sha256", "sha256"), ("artifact", "artifact"), ("preview_url", "preview"), ("preview_expires", "expires"),
]


def operator() -> Dashboard:
    d = Dashboard("fr-operator", "FlightRisk / Operator Input & Target",
                  "Every operator action (Socket.IO and REST) with its outcome, the target image in use with its hash and "
                  "quality numbers, and the alert responses.", ["operator"])
    d.variables = [var_node()]
    _annotations(d)

    d.row("Operator actions")
    d.add(timeseries("Actions by event and outcome", [
        prom(f"sum by (event, outcome) (increase({q('flightrisk_socket_events')}[{RI}]))", "{{event}} {{outcome}}"),
        prom(f"sum by (route, outcome) (increase({q('flightrisk_http_requests', 'method!=\"GET\"')}[{RI}]))", "REST {{route}} {{outcome}}", "B"),
    ], "short", "Socket.IO events and state-changing REST requests.", 0, stack=True, bars=True), 12, 8)
    d.add(stat("Target changes (range)", [prom(f"sum(increase({q('flightrisk_target_changes')}[$__range]))", instant=True)],
               "short", "", None, 0), 4, 4)
    d.add(stat("Rejected or denied (range)", [prom(
        f"sum(increase({q('flightrisk_socket_events', 'outcome!=\"ok\"')}[$__range])) or vector(0)", instant=True)],
        "short", "Socket.IO events answered with an error, refused at connect, or failed.", [("green", None), ("orange", 1)], 0), 4, 4)
    d.add(stat("Unauthorized REST (range)", [prom(
        f"sum(increase({q('flightrisk_http_requests', 'outcome=\"unauthorized\"')}[$__range])) or vector(0)", instant=True)],
        "short", "Requests refused by the API-key gate.", [("green", None), ("red", 1)], 0), 4, 4)
    d.add(gauge("Socket p95 latency", [prom(p(0.95, "flightrisk_socket_duration_ms", window="5m"))], "ms", 0, 1000,
                [("green", None), ("orange", 250), ("red", 750)]), 12, 4)
    d.add(logs("Operator input", logql_events('event="user_input"'),
               "Each Socket.IO event and state-changing REST call: action, sanitized input, actor, remote address, outcome. "
               "Free text (description, notes) appears only with FLIGHTRISK_TELEMETRY_CONTENT=1."), 24, 10)

    d.row("Target image")
    d.add(table("Target image (latest first)", [loki(logql_events('event="target_image" | source!="clear"'))],
                "Numbers only, plus (with content capture on) the sha256, artifact reference and a signed thumbnail URL served "
                "by the loopback-only preview server. The URL expires (FLIGHTRISK_PREVIEW_TTL_S, default 24 h).",
                [
                    {"id": "extractFields", "options": {"source": "Line", "format": "json", "keepTime": True, "replace": True,
                                                         "jsonPaths": [{"path": pth, "alias": alias} for pth, alias in TARGET_PATHS]}},
                    {"id": "limit", "options": {"limitField": 5}},
                ],
                [{"matcher": {"id": "byName", "options": "preview"},
                  "properties": [{"id": "custom.cellOptions", "value": {"type": "image"}}, {"id": "custom.width", "value": 170}]},
                 {"matcher": {"id": "byName", "options": "sha256"},
                  "properties": [{"id": "custom.width", "value": 160}]}]), 18, 9)
    d.add(text("About the preview", (
        "The thumbnail is served by FlightRisk's preview server on **127.0.0.1 only**, from the artifact store "
        "(`~/.flightrisk/artifacts`, mode 0700, files 0600, deleted after `FLIGHTRISK_ARTIFACT_RETENTION_DAYS`).\n\n"
        "Each `target_image` event carries a capability URL signed with HMAC-SHA256 over the image hash and an expiry. "
        "Anyone who can read this log and reach this machine's loopback can open it until it expires. "
        "No image is stored, and no URL is logged, unless `FLIGHTRISK_TELEMETRY_CONTENT=1`.")), 6, 9)
    d.add(logs("Target image events", logql_events('event="target_image"'), ""), 24, 7)

    d.row("Alert responses")
    d.add(logs("Alerts and responses", logql_events('event="alert" | transition=~"opened|renotified|confirmed|dismissed"'),
               "Alert opened, re-announced, and the operator's Confirm or Dismiss with the time it took (awaiting_s)."), 24, 8)
    return d


# ---------------------------------------------------------------------------
# 5. Errors and logs
# ---------------------------------------------------------------------------

def errors() -> Dashboard:
    d = Dashboard("fr-errors", "FlightRisk / Errors & Logs",
                  "Error rate, the most frequent error fingerprints, log volume and a log explorer filtered by node, level "
                  "and component, with links from each line's trace_id to Tempo.", ["errors", "logs"])
    d.variables = [var_node(), var_level(), var_component()]
    _annotations(d)
    filt = 'level=~"$level" | component=~"$component"'

    d.row("Errors")
    d.add(timeseries("Error rate by component", [prom(f"sum by (component) (rate({q('flightrisk_error_logs')}[{RI}]))", "{{component}}")],
                     "ops", "ERROR and CRITICAL records per second (every one is counted, including repeats folded in the log).", 0), 12, 8)
    d.add(table("Top error fingerprints (range)", [prom(
        f"topk(10, sum by (fingerprint, error_type, component) (increase({q('flightrisk_error_logs')}[$__range])))", instant=True)],
        "A fingerprint hashes the exception type and its top frames. Search the log for fingerprint=\"...\" to see the lines.",
        [{"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"Value": "count"}}}],
        sort_by=[{"displayName": "count", "desc": True}]), 12, 8)

    d.row("Logs")
    d.add(timeseries("Log volume by level", [loki(f"sum by (level) (count_over_time({STREAM} | json | {filt} [$__interval]))",
                                                  legend="{{level}}")],
                     "short", "", 0, stack=True, bars=True), 12, 7)
    d.add(timeseries("Log volume by component", [loki(f"sum by (component) (count_over_time({STREAM} | json | {filt} [$__interval]))",
                                                      legend="{{component}}")],
                     "short", "", 0, stack=True, bars=True), 8, 7)
    d.add(stat("Console records dropped", [prom(f"max({q('flightrisk_log_dropped')})")], "short",
               "Records the console log queue dropped instead of blocking a caller (should stay 0).",
               [("green", None), ("orange", 1)], 0), 4, 7)
    d.add(logs("Errors (with fingerprint and trace)", f"{STREAM} | json | level=~\"error|critical\" | component=~\"$component\"",
               "Stack traces are included only while FLIGHTRISK_LOG_LEVEL=DEBUG."), 24, 9)
    d.add(logs("Log explorer", f"{STREAM} | json | {filt}",
               "Filtered by the Node, Level and Component variables. Add debug to Level to see DEBUG records."), 24, 12)
    d.add(traces("Traces with errors", '{resource.service.name=~"flightrisk.*" && status=error}', ""), 24, 8)
    return d


DASHBOARDS = {
    "flightrisk-overview.json": overview,
    "flightrisk-flight-comm.json": flight_comm,
    "flightrisk-detection.json": detection,
    "flightrisk-operator.json": operator,
    "flightrisk-errors.json": errors,
}


def dumps(value, depth: int = 0, max_depth: int = 3) -> str:
    """JSON with one line per key down to ``max_depth`` and compact values below it.

    Keeps each dashboard file short (one panel is about a dozen lines) while a
    change to a query or threshold still shows up as a one-line diff.
    """
    pad = "  " * (depth + 1)
    end = "  " * depth
    if depth < max_depth and isinstance(value, dict) and value:
        items = [f"{pad}{json.dumps(k)}: {dumps(v, depth + 1, max_depth)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + end + "}"
    if depth < max_depth and isinstance(value, list) and value:
        items = [f"{pad}{dumps(v, depth + 1, max_depth)}" for v in value]
        return "[\n" + ",\n".join(items) + "\n" + end + "]"
    return json.dumps(value, ensure_ascii=False)


def render() -> dict[str, str]:
    """File name -> JSON text for every generated dashboard."""
    return {name: dumps(build().to_json()) + "\n" for name, build in DASHBOARDS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if a committed dashboard differs from the generated one")
    args = parser.parse_args(argv)
    stale = []
    for name, content in render().items():
        path = OUT_DIR / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(name)
        else:
            path.write_text(content)
            print(f"wrote {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    if stale:
        print("stale dashboards (run observability/grafana/build_dashboards.py): " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["DASHBOARDS", "TEMPO", "render"]
