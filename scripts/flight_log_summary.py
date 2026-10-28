#!/usr/bin/env python3
"""Print a short, redacted summary of FlightRisk ground or edge logs for one time window.

The output is meant to be pasted into a message (for example a vault note),
so it is filtered, stripped of secrets, and never longer than a hard cap
(20 000 bytes by default, and never more).

    python scripts/flight_log_summary.py edge-2.log
    python scripts/flight_log_summary.py ground.log edge-1.log edge-2.log --minutes 10
    python scripts/flight_log_summary.py ground.log --until 2026-10-28T19:10:00Z --minutes 3

A log is the console output of the ground station or an edge: JSON lines
(``{"timestamp": ..., "level": ..., "event": ...}``) mixed with plain text
lines. The window ends at ``--until`` or, by default, at the newest
timestamp in the given logs (not at the current time, so an old log still
gives its last minutes), and starts ``--minutes`` (default 5) earlier. A
plain text line takes the time of the JSON line before it.

Kept: warnings and errors, ``start_at``, nacks and every refusal with its
reason, ``drone_lost``, takeoff replies, altitude slot events, drone link
health, clock sync, takeoffs and landings, Tello adapter binding, and plain
text lines that look like errors. Message traffic (heartbeats, telemetry,
detections, clock sync) is summarised as counts, and the last telemetry
snapshot of each drone is shown. When the result is over the cap, the oldest
event lines are dropped first.

Redaction: any field whose name looks like a token, secret, password,
authorization header, cookie or API key is replaced, and so are bearer
tokens, ``token=...`` text, authorization headers and hex strings of 24 or
more characters anywhere in the text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: The hard output cap in bytes; ``--max-bytes`` may lower it, never raise it.
HARD_CAP_BYTES = 20_000
DEFAULT_MINUTES = 5.0
#: Each event line is cut to this many characters.
LINE_MAX_CHARS = 300
REDACTED = "[redacted]"

# Events kept by name.
KEEP_EVENTS = frozenset({
    "drone_lost", "edge_alt_slot_assigned", "edge_alt_slot_climb", "slot_changed", "ceiling_changed",
    "edge_start_scheduled", "edge_takeoff", "edge_land", "edge_lanes_complete", "land_all_sent",
    "edge_link_lost", "edge_drone_link_unhealthy", "edge_drone_link_restored",
    "tello_connected", "tello_connect_failed", "tello_takeoff", "tello_takeoff_reply",
    "tello_takeoff_error_checked", "tello_land_confirmed", "tello_land_unconfirmed", "tello_link_lost",
    "tello_crash_suspected", "tello_flying_after_land", "tello_transport_bound", "tello_foreign_packet",
    "edge_source_tello", "edge_tello_binding_refused", "edge_connect", "edge_device_start", "edge_device_stop",
    "edge_drone_connect_failed", "ground_bind_listening", "ground_bind_address_lost",
    "ground_bind_address_restored", "fleet_start_refused",
})
# Events kept when their name contains one of these.
KEEP_EVENT_PARTS = ("refus", "nack", "clock", "start_at", "drone_link", "link_lost", "takeoff_reply")
# Message types (``drone_comm`` events) kept as lines; all of them are also counted.
KEEP_COMM_TYPES = frozenset({
    "start_at", "nack", "link_up", "link_down", "land", "emergency_land_all", "takeoff", "manual_hold",
})
# Plain text lines kept when they match this.
PLAIN_KEEP = re.compile(r"error|warn|traceback|exception|refus|nack|lost|fail|unreachable", re.IGNORECASE)
# Fields left out of event lines: always present, or never useful in a summary.
DROP_FIELDS = frozenset({
    "timestamp", "level", "logger", "event", "message", "category", "node", "session_id", "trace_id", "span_id",
})
# Telemetry readings shown for each drone's last snapshot.
TELEMETRY_KEYS = ("battery", "height_cm", "is_flying", "drone_link_ok", "drone_link_age_s", "drone_link_detail")

_SECRET_KEY = re.compile(r"token|secret|passw|authoriz|cookie|api[_-]?key|credential", re.IGNORECASE)
_SECRET_TEXT = (
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*[^\r\n,;}\]]+"), r"\1: " + REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:token|secret|password|passwd|api[_-]?key))(\"?\s*[:=]\s*\"?)[^\s\"&,;}]+"),
     r"\1\2" + REDACTED),
    (re.compile(r"(?i)\b[0-9a-f]{24,}\b"), "[hex]"),
)


def redact_text(text: str) -> str:
    """``text`` with tokens, authorization headers and long hex strings replaced."""
    for pattern, replacement in _SECRET_TEXT:
        text = pattern.sub(replacement, text)
    return text


def redact_value(key: str, value: Any) -> Any:
    """A field value as it may appear in the summary."""
    if _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(key, v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _parse_time(text: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


class Entry:
    """One log line: a parsed JSON record or a plain text line, with its time and source."""

    __slots__ = ("at", "record", "source", "text")

    def __init__(self, at: datetime | None, record: dict | None, text: str, source: str):
        self.at = at
        self.record = record
        self.text = text
        self.source = source


def read_entries(path: Path, source: str) -> list[Entry]:
    """Every line of one log. A plain line takes the time of the JSON line before it."""
    entries: list[Entry] = []
    last_at: datetime | None = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            record = None
            if line.lstrip().startswith("{"):
                try:
                    parsed = json.loads(line)
                    record = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    record = None
            if record is not None:
                at = _parse_time(str(record.get("timestamp", "")))
                if at is not None:
                    last_at = at
                entries.append(Entry(at or last_at, record, line, source))
            else:
                entries.append(Entry(last_at, None, line, source))
    return entries


def _keep(record: dict) -> bool:
    event = str(record.get("event", ""))
    level = str(record.get("level", "")).lower()
    if level in ("warning", "error", "critical"):
        return True
    if event in KEEP_EVENTS or any(part in event for part in KEEP_EVENT_PARTS):
        return True
    return event == "drone_comm" and str(record.get("type", "")) in KEEP_COMM_TYPES


def _format_fields(record: dict) -> str:
    parts = []
    for key, value in record.items():
        if key in DROP_FIELDS:
            continue
        value = redact_value(key, value)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), sort_keys=True)
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _clip(line: str) -> str:
    return line if len(line) <= LINE_MAX_CHARS else line[: LINE_MAX_CHARS - 3] + "..."


def format_entry(entry: Entry, show_source: bool) -> str:
    stamp = entry.at.strftime("%H:%M:%S.%f")[:-3] if entry.at else "--:--:--"
    prefix = f"{stamp} [{entry.source}] " if show_source else f"{stamp} "
    if entry.record is None:
        return _clip(prefix + "| " + redact_text(entry.text.strip()))
    record = entry.record
    level = str(record.get("level", "")).upper()[:4]
    event = str(record.get("event", "?"))
    message = redact_text(str(record.get("message", "")))
    head = f"{prefix}{level} {event}"
    if message and message != event:
        head += f" \"{message}\""
    fields = _format_fields(record)
    return _clip(f"{head} {fields}".rstrip())


def summarize(
    paths: Iterable[Path],
    minutes: float = DEFAULT_MINUTES,
    until: datetime | None = None,
    max_bytes: int = HARD_CAP_BYTES,
) -> str:
    """The summary text for ``paths`` (see the module docstring); never longer than ``max_bytes`` bytes."""
    cap = max(200, min(int(max_bytes), HARD_CAP_BYTES))
    paths = list(paths)
    entries: list[Entry] = []
    for path in paths:
        entries.extend(read_entries(path, path.name))
    stamps = [e.at for e in entries if e.at is not None]
    end = until or (max(stamps) if stamps else None)
    start = end - timedelta(minutes=minutes) if end is not None else None

    def in_window(entry: Entry) -> bool:
        if start is None or entry.at is None:
            return start is None
        return start <= entry.at <= end

    window = [e for e in entries if in_window(e)]
    window.sort(key=lambda e: e.at or datetime.min.replace(tzinfo=timezone.utc))

    counts: Counter = Counter()
    levels: Counter = Counter()
    last_telemetry: dict[str, tuple[Entry, dict]] = {}
    lines: list[str] = []
    show_source = len(paths) > 1
    previous: str | None = None
    repeats = 0
    for entry in window:
        record = entry.record
        if record is not None:
            levels[str(record.get("level", "?")).lower()] += 1
            if record.get("event") == "drone_comm":
                count = record.get("count", 1)
                counts[(str(record.get("direction", "?")), str(record.get("type", "?")))] += (
                    count if isinstance(count, int) else 1
                )
            if record.get("event") == "drone_telemetry_snapshot" and isinstance(record.get("readings"), dict):
                last_telemetry[str(record.get("drone_id", "?"))] = (entry, record["readings"])
            keep = _keep(record)
        else:
            keep = bool(PLAIN_KEEP.search(entry.text))
        if not keep:
            continue
        line = format_entry(entry, show_source)
        # Collapse a line repeated back to back (same text after the time stamp).
        body = line.split(" ", 1)[1] if " " in line else line
        if body == previous:
            repeats += 1
            continue
        if repeats:
            lines.append(f"    (previous line repeated {repeats} more time{'s' if repeats > 1 else ''})")
        repeats = 0
        previous = body
        lines.append(line)
    if repeats:
        lines.append(f"    (previous line repeated {repeats} more time{'s' if repeats > 1 else ''})")

    header = ["FlightRisk log summary"]
    header.append("logs: " + ", ".join(redact_text(p.name) for p in paths))
    if start is not None:
        header.append(f"window: {start.isoformat(timespec='seconds')} to {end.isoformat(timespec='seconds')} "
                      f"({minutes:g} min)")
    else:
        header.append("window: no timestamps found, whole log")
    header.append("levels: " + (", ".join(f"{k}={v}" for k, v in sorted(levels.items())) or "none"))
    if counts:
        header.append("messages: " + ", ".join(f"{d} {t} x{n}" for (d, t), n in sorted(counts.items())))
    for drone, (entry, readings) in sorted(last_telemetry.items()):
        shown = {k: redact_value(k, readings[k]) for k in TELEMETRY_KEYS if k in readings}
        stamp = entry.at.strftime("%H:%M:%S") if entry.at else "?"
        header.append(_clip(f"last telemetry {drone} at {stamp}: "
                            + json.dumps(shown, separators=(",", ":"), sort_keys=True)))
    header.append(f"events: {len(lines)} line(s)")
    header.append("")
    return _fit(header, lines, cap)


def _fit(header: list[str], lines: list[str], cap: int) -> str:
    """Join header and lines within ``cap`` bytes, dropping the oldest lines first."""
    def size(parts: list[str]) -> int:
        return len("\n".join(parts).encode("utf-8")) + 1

    head = list(header)
    while head and size(head) > cap // 2:
        head.pop()
    kept = list(lines)
    dropped = 0
    while kept and size(head + [f"[{dropped + 1} earlier line(s) omitted to stay under {cap} bytes]"] + kept) > cap:
        kept.pop(0)
        dropped += 1
    body = head + ([f"[{dropped} earlier line(s) omitted to stay under {cap} bytes]"] if dropped else []) + kept
    text = "\n".join(body) + "\n"
    data = text.encode("utf-8")
    if len(data) > cap:  # a last guard; the loop above normally keeps it under
        text = data[: cap - 1].decode("utf-8", errors="ignore") + "\n"
    return text


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Summarise FlightRisk ground or edge logs for one time window.")
    p.add_argument("logs", nargs="+", type=Path, help="log files (ground and/or edge console output)")
    p.add_argument("--minutes", type=float, default=DEFAULT_MINUTES,
                   help=f"window length in minutes (default {DEFAULT_MINUTES:g})")
    p.add_argument("--until", help="end of the window, ISO 8601 (default: the newest timestamp in the logs)")
    p.add_argument("--max-bytes", type=int, default=HARD_CAP_BYTES,
                   help=f"output cap in bytes (default and maximum {HARD_CAP_BYTES})")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.minutes <= 0:
        parser.error("--minutes must be positive")
    until = None
    if args.until:
        until = _parse_time(args.until)
        if until is None:
            parser.error(f"--until must be an ISO 8601 time such as 2026-10-28T19:10:00Z, got {args.until!r}")
    for path in args.logs:
        if not path.is_file():
            parser.error(f"no such log file: {path}")
    text = summarize(args.logs, minutes=args.minutes, until=until, max_bytes=args.max_bytes)
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        out.write(text.encode("utf-8"))
        out.flush()
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
