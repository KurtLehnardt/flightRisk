#!/usr/bin/env python3
"""Measure what the telemetry adds to the frame loop and the lane-executor monitor.

Runs each mode in its own process (OpenTelemetry providers are global):

* ``off``          FLIGHTRISK_OTEL_ENABLED unset (logging pipeline only)
* ``reachable``    exporting to a running collector (``--endpoint``)
* ``closed-port``  exporting to 127.0.0.1:1, where nothing listens
* ``blackhole``    exporting to a non-routable address that never answers

For each mode it reports, in microseconds:

* ``frame``: the instrumentation the frame loop now runs per frame (eight
  stage timers, the telemetry snapshot export, signal scores, the throttled
  per-frame logs, one traced frame in five with stage spans, the pipeline
  summary, a Socket.IO emit counter), minus the same loop without it;
* ``tick``: one ``LaneExecutor.tick()`` on a fake Tello, with telemetry sent
  through the edge transport's ``drone_comm`` hook every tick;
* ``log``: one INFO log call while 20 000 records are written as fast as
  possible (the console queue drops what it cannot take; it never blocks).

    python scripts/bench_telemetry_overhead.py --endpoint http://127.0.0.1:4317
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODES = {
    "off": None,
    "reachable": "{endpoint}",
    "closed-port": "http://127.0.0.1:1",
    "blackhole": "http://10.255.255.1:4317",
}


def _pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))]


def _summary(values: list[float]) -> dict:
    return {"p50": round(_pct(values, 0.5), 1), "p99": round(_pct(values, 0.99), 1), "max": round(max(values), 1),
            "mean": round(statistics.fmean(values), 1)}


def worker(frames: int, ticks: int, logs: int) -> dict:
    import logging

    import numpy as np

    os.environ.setdefault("FLIGHTRISK_CONSOLE_ECHO", "0")
    from flightrisk import obs_comm, obs_events
    from flightrisk import obs_metrics as M
    from flightrisk.drone.fake import FakeDrone
    from flightrisk.drone.lane_executor import LaneExecutor
    from flightrisk.drone.safety import SafetyEnvelope
    from flightrisk.drone.tello import FakeTelloController
    from flightrisk.observability import StructuredLogger
    from flightrisk.telemetry import get_tracer, init_telemetry

    enabled = init_telemetry()
    # Keep the console quiet: this measures the caller, not the terminal.
    logging.getLogger("flightrisk").setLevel(logging.INFO)
    tracer = get_tracer() if enabled else None
    drone = FakeDrone(name="bench-1")
    drone.connect()
    drone.takeoff()
    slog = StructuredLogger(component="bench")
    stats = obs_events.PipelineStats()
    work = np.zeros((48, 64, 3), np.uint8)

    def do_work():
        # A small fixed amount of CPU work standing in for a pipeline stage.
        return int(work.sum())

    stages = ("detect", "track", "reid", "face", "score", "annotate", "encode", "emit")

    def instrumented(i: int) -> None:
        frame_span = tracer.start_as_current_span("frame") if (tracer and i % 5 == 0) else None
        if frame_span is not None:
            frame_span.__enter__()
        try:
            for name in stages:
                span = tracer.start_as_current_span(name) if frame_span is not None else None
                if span is not None:
                    span.__enter__()
                with obs_events.StageTimer(stats, name):
                    do_work()
                if span is not None:
                    span.__exit__(None, None, None)
            obs_events.telemetry_snapshot(drone.snapshot())
            stats.signal_scores({"reid": 0.42, "face": 0.0, "combined": 0.3})
            stats.alert_level("no_match")
            slog.face_result(success=False, score=0.1)
            slog.scoring(combined=0.3, reid=0.42, face=0.0)
            M.socket_emits.add(1, {"event": "frame"})
            stats.frame_done(2, source="bench")
        finally:
            if frame_span is not None:
                frame_span.__exit__(None, None, None)

    def baseline(i: int) -> None:
        for _ in stages:
            do_work()
        drone.snapshot()

    for i in range(200):  # warm up
        instrumented(i)
        baseline(i)
    base, inst = [], []
    for i in range(frames):
        t0 = time.perf_counter()
        baseline(i)
        t1 = time.perf_counter()
        instrumented(i)
        t2 = time.perf_counter()
        base.append((t1 - t0) * 1e6)
        inst.append((t2 - t1) * 1e6)
    added = [b - a for a, b in zip(base, inst)]

    tello = FakeTelloController(name="bench-edge", battery=90)
    tello.connect()

    def send(msg):
        obs_comm.edge_sent(msg, True, 200)

    executor = LaneExecutor(tello, SafetyEnvelope(), "bench-edge", send, telemetry_interval_s=0.0,
                            logger=StructuredLogger(component="bench-lane"))
    executor.note_ground_message()
    tick = []
    for _ in range(ticks):
        t0 = time.perf_counter()
        executor.tick()
        tick.append((time.perf_counter() - t0) * 1e6)

    lg = logging.getLogger("flightrisk.bench.flood")
    flood = []
    for i in range(logs):
        t0 = time.perf_counter()
        lg.info("flood %d", i)
        flood.append((time.perf_counter() - t0) * 1e6)

    from flightrisk.obs_log import console_dropped

    return {
        "otel": enabled,
        "frame_added_us": _summary(added),
        "frame_baseline_us": _summary(base),
        "tick_us": _summary(tick),
        "log_call_us": _summary(flood),
        "console_dropped": console_dropped(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", default="http://127.0.0.1:4317", help="collector for the 'reachable' mode")
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--frames", type=int, default=3000)
    ap.add_argument("--ticks", type=int, default=2000)
    ap.add_argument("--logs", type=int, default=20000)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        print(json.dumps(worker(args.frames, args.ticks, args.logs)))
        return 0
    results = {}
    for mode in args.modes.split(","):
        env = dict(os.environ)
        env.pop("FLIGHTRISK_OTEL_ENABLED", None)
        endpoint = MODES[mode]
        if endpoint is not None:
            env.update({"FLIGHTRISK_OTEL_ENABLED": "true", "OTEL_EXPORTER": "otlp-grpc",
                        "OTEL_ENDPOINT": endpoint.format(endpoint=args.endpoint),
                        "OTEL_RESOURCE_ATTRIBUTES": f"service.instance.id=bench-{mode}"})
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, __file__, "--worker", "--frames", str(args.frames), "--ticks",
                               str(args.ticks), "--logs", str(args.logs)], env=env, capture_output=True, text=True, check=False)
        elapsed = time.monotonic() - t0
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        results[mode] = json.loads(line[-1]) if line else {"error": proc.stderr[-2000:]}
        results[mode]["process_s"] = round(elapsed, 1)
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
