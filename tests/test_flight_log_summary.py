"""scripts/flight_log_summary.py: filtering, the time window, redaction and the output cap."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("flight_log_summary", ROOT / "scripts" / "flight_log_summary.py")
summary = importlib.util.module_from_spec(_spec)
sys.modules["flight_log_summary"] = summary
_spec.loader.exec_module(summary)

T0 = datetime(2026, 10, 28, 19, 0, 0, tzinfo=timezone.utc)


def _rec(seconds: float, event: str, level: str = "info", **fields) -> str:
    stamp = (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    record = {"timestamp": stamp, "level": level, "logger": "flightrisk.x", "component": "ground",
              "event": event, "message": fields.pop("message", event), "session_id": "s", **fields}
    return json.dumps(record)


def _write(tmp_path: Path, lines: list[str], name: str = "ground.log") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_keeps_the_flight_events_and_counts_the_traffic(tmp_path):
    log = _write(tmp_path, [
        _rec(0, "drone_comm", direction="out", type="heartbeat", count=6, message="out heartbeat x6"),
        _rec(1, "drone_comm", direction="in", type="telemetry", count=10, message="in telemetry x10"),
        _rec(2, "drone_comm", direction="out", type="clock_sync", count=4, message="out clock_sync x4"),
        _rec(3, "drone_comm", direction="out", type="start_at", drone_id="drone-1", message="out start_at drone-1 ok"),
        _rec(4, "drone_comm", direction="in", type="nack", drone_id="drone-2", reason="drone_unreachable"),
        _rec(5, "edge_start_refused", level="warning", reason="no_clock_offset"),
        _rec(6, "flight_safety", level="warning", kind="drone_lost", drone_id="drone-2"),
        _rec(7, "drone_lost", drone_id="drone-2"),
        _rec(8, "tello_takeoff_reply", reply="error", reply_age_ms=74, verdict="unconfirmed_error"),
        _rec(9, "edge_alt_slot_assigned", alt_slot_cm=80),
        _rec(10, "edge_drone_link_unhealthy", level="warning", detail="Tello unreachable (no state for 4 s) via 192.168.10.50"),
        _rec(11, "edge_clock_offset", offset_s=0.012),
        _rec(12, "detection_pipeline", fps=11.5),
        _rec(13, "some_error", level="error", error="boom"),
        _rec(14, "drone_telemetry_snapshot", drone_id="drone-1",
             readings={"battery": 81, "height_cm": 80, "is_flying": True, "drone_link_ok": True, "rel_x_cm": 3}),
        "Traceback (most recent call last):",
        "[detector] Loaded yolo11n.pt on cpu",
    ])
    text = summary.summarize([log])
    for needle in ("start_at", "nack", "drone_unreachable", "edge_start_refused", "no_clock_offset", "drone_lost",
                   "tello_takeoff_reply", "unconfirmed_error", "edge_alt_slot_assigned", "alt_slot_cm=80",
                   "via 192.168.10.50", "edge_clock_offset", "some_error", "| Traceback"):
        assert needle in text, needle
    assert "out heartbeat x6" in text and "in telemetry x10" in text and "out clock_sync x4" in text  # counts
    assert "\"out heartbeat x6\"" not in text  # the traffic itself is not listed line by line
    assert "detection_pipeline" not in text
    assert "yolo11n" not in text
    assert 'last telemetry drone-1 at 19:00:14: {"battery":81,"drone_link_ok":true,"height_cm":80,"is_flying":true}' in text
    assert "session_id" not in text


def test_window_defaults_to_the_last_minutes_of_the_log(tmp_path):
    log = _write(tmp_path, [
        _rec(0, "edge_start_refused", level="warning", reason="old_one"),
        "old plain error line",
        _rec(600, "edge_start_refused", level="warning", reason="new_one"),
        "new plain error line",
    ])
    text = summary.summarize([log], minutes=5)
    assert "new_one" in text and "new plain error line" in text
    assert "old_one" not in text and "old plain error line" not in text
    text = summary.summarize([log], minutes=5, until=T0 + timedelta(seconds=60))
    assert "old_one" in text and "new_one" not in text


def test_two_logs_are_merged_by_time_and_labelled(tmp_path):
    ground = _write(tmp_path, [_rec(2, "drone_lost", drone_id="drone-1")], "ground.log")
    edge = _write(tmp_path, [_rec(1, "tello_takeoff_reply", reply="ok")], "edge-1.log")
    lines = summary.summarize([ground, edge]).splitlines()
    events = [line for line in lines if line.startswith("19:")]
    assert events[0].startswith("19:00:01.000 [edge-1.log]") and "tello_takeoff_reply" in events[0]
    assert events[1].startswith("19:00:02.000 [ground.log]") and "drone_lost" in events[1]


def test_secrets_are_redacted(tmp_path):
    hexes = "a3f9" * 10
    log = _write(tmp_path, [
        _rec(0, "edge_connect", level="warning", token="tok-123-secret", authorization="Bearer abc.def",
             headers={"Authorization": "Bearer xyz", "X-Ok": "fine"}, edge_token="zzz", fingerprint=hexes),
        _rec(1, "some_error", level="error",
             message=f"auth failed: Authorization: Bearer qqq-777 token=leak-1 FLIGHTRISK_EDGE_TOKEN=leak-2 id {hexes}"),
        f"plain error: \"token\": \"leak-3\" bearer leak-4 {hexes}",
    ])
    text = summary.summarize([log])
    for secret in ("tok-123-secret", "abc.def", "xyz", "zzz", "qqq-777", "leak-1", "leak-2", "leak-3", "leak-4", hexes):
        assert secret not in text, secret
    assert "[redacted]" in text and "[hex]" in text
    assert "fine" in text  # other fields stay


def test_output_never_exceeds_the_cap_and_keeps_the_newest_lines(tmp_path):
    lines = [_rec(i * 0.01, "edge_start_refused", level="warning", reason=f"r{i:05d}", detail="x" * 150)
             for i in range(5000)]
    log = _write(tmp_path, lines)
    text = summary.summarize([log])
    assert len(text.encode("utf-8")) <= summary.HARD_CAP_BYTES
    assert "r04999" in text  # the newest line survives
    assert "r00000" not in text
    assert "earlier line(s) omitted" in text
    # --max-bytes can lower the cap but never raise it.
    assert len(summary.summarize([log], max_bytes=50_000).encode("utf-8")) <= summary.HARD_CAP_BYTES
    small = summary.summarize([log], max_bytes=2_000)
    assert len(small.encode("utf-8")) <= 2_000 and "r04999" in small


def test_repeated_lines_are_collapsed(tmp_path):
    log = _write(tmp_path, ["socket error: connection refused"] * 50)
    text = summary.summarize([log])
    assert text.count("connection refused") == 1
    assert "repeated 49 more times" in text


def test_cli_prints_the_summary_and_checks_its_arguments(tmp_path, capsys):
    log = _write(tmp_path, [_rec(0, "drone_lost", drone_id="drone-1")])
    assert summary.main([str(log), "--minutes", "2"]) == 0
    assert "drone_lost" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        summary.main([str(tmp_path / "missing.log")])
    with pytest.raises(SystemExit):
        summary.main([str(log), "--until", "yesterday"])
