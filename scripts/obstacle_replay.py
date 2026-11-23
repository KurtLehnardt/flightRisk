#!/usr/bin/env python3
"""Replay a drone recording and run the obstacle sensor on every nth frame.

Calibrates the depth threshold (FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH) against
known distances in a recording. Prints per-frame cue values, decisions and
latency to stdout; optionally writes a CSV for further analysis.

Usage:
    python scripts/obstacle_replay.py --frames <dir> [--stride n] [--out csv] [--min-safe-depth x]
    python scripts/obstacle_replay.py --video <mp4> [--stride n] [--out csv] [--min-safe-depth x]
    python scripts/obstacle_replay.py --metrics-report rec/yard_clear rec/yard_wall100 \\
        --clear yard_clear --obstacle yard_wall100 [--json report.json]

Arguments:
    --frames <dir>: A recording made by scripts/obstacle_record.py: the PNG frames
        the sensor saw in flight, in its channel order, with their capture times.
        This is the input to calibrate with: MiDaS gets exactly what it got in flight.
    --video <mp4>: A video file read with OpenCV. Lossy compression changes the
        pixels, so its numbers can differ a little from flight.
    --stride n: Process every nth frame (default 1).
    --out csv: Write per-frame data to CSV file (optional).
    --min-safe-depth x: Depth threshold for obstacle detection (default 0.35).

    --metrics-report DIR [DIR ...]: Compare candidate obstacle metrics over one or more
        recordings and print text tables only (never images). Each DIR is a recording folder,
        labelled with its folder name, or ``label=DIR`` to choose the label. Per label it
        reports the current score, ``centre_floor_ratio`` and ``centre_sides_ratio`` (n, min,
        p10, median, p90, max, share of None), a clear-rate threshold sweep, and the N-of-M
        voting rates (probability of N consecutive clears, N=1..3, from the observed frame
        order). ``--clear`` and ``--obstacle`` take comma separated labels and add the best
        separating threshold per metric with its error rates. ``--json FILE`` also writes the
        numbers. None counts as not clear (blocked), the fail-safe reading.

Every frame goes through ``flightrisk.drone.obstacle_sensor.to_sensor_frame``,
the same conversion the edge's frame feeds use, so a frame replayed here and
the same frame in flight reach the depth model as the same tensor. OpenCV
reads PNG and video files as BGR, the sensor's channel order.
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# Run from a checkout without installing the package.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

#: The frame index of a recording made by ``scripts/obstacle_record.py``.
INDEX_NAME = "frames.csv"

CSV_FIELDS = [
    "frame", "processed", "action", "center_depth", "center_p90",
    "left_depth", "right_depth", "confidence", "center_stat",
    "latency_ms", "safe", "captured_at_s", "decode_lag_ms",
]
#: The recording's decode lag column, and the name it had before it was renamed (still read).
DECODE_LAG_FIELDS = ("decode_lag_ms", "capture_to_arrival_ms")


def parse_args(args=None):
    """Parse command-line arguments.

    Args:
        args: List of arguments to parse (for testing). If None, uses sys.argv.
    """
    parser = argparse.ArgumentParser(
        description="Replay a recording and run the obstacle sensor on every nth frame"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--frames", help="A recording directory written by scripts/obstacle_record.py"
    )
    source.add_argument(
        "--video", help="Path to video file (MP4)"
    )
    parser.add_argument(
        "--stride", type=int, default=1,
        help="Process every nth frame (default 1)"
    )
    parser.add_argument(
        "--out", dest="csv_out",
        help="Path to write CSV output (optional)"
    )
    parser.add_argument(
        "--min-safe-depth", type=float, default=0.35,
        help="Depth threshold for obstacle detection (default 0.35)"
    )
    parser.add_argument(
        "--metrics-report", nargs="+", metavar="DIR",
        help="Compare candidate obstacle metrics over these recording folders (label=DIR or DIR)"
    )
    parser.add_argument(
        "--clear", default="",
        help="With --metrics-report: comma separated labels of recordings with nothing in the way"
    )
    parser.add_argument(
        "--obstacle", default="",
        help="With --metrics-report: comma separated labels of recordings with an obstacle in the way"
    )
    parser.add_argument(
        "--ratio-threshold", type=float, default=1.0,
        help="With --metrics-report: the voting threshold of the ratio metrics when no --clear and "
             "--obstacle labels give a best one (default 1.0)"
    )
    parser.add_argument(
        "--json", dest="json_out",
        help="With --metrics-report: also write the report numbers to this JSON file"
    )
    parsed = parser.parse_args(args)
    if parsed.stride < 1:
        parser.error("--stride must be 1 or more")
    if parsed.metrics_report:
        if parsed.frames or parsed.video:
            parser.error("--metrics-report takes its recordings itself; drop --frames and --video")
        try:
            parsed.recordings = parse_recordings(parsed.metrics_report)
        except ValueError as exc:
            parser.error(str(exc))
        parsed.clear_labels = _split_labels(parsed.clear)
        parsed.obstacle_labels = _split_labels(parsed.obstacle)
        known = {label for label, _ in parsed.recordings}
        for name in parsed.clear_labels + parsed.obstacle_labels:
            if name not in known:
                parser.error(f"label {name!r} is not one of the recordings: {sorted(known)}")
        if set(parsed.clear_labels) & set(parsed.obstacle_labels):
            parser.error("a label cannot be both --clear and --obstacle")
    elif parsed.json_out or parsed.clear or parsed.obstacle:
        parser.error("--json, --clear and --obstacle need --metrics-report")
    elif not (parsed.frames or parsed.video):
        parser.error("one of --frames, --video or --metrics-report is required")
    return parsed


def _split_labels(text):
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_recordings(items):
    """Turn ``DIR`` or ``label=DIR`` items into ``[(label, dir)]``; a bare DIR is labelled by its folder name.

    ``=`` only splits when the text before it has no path separator, so a path that happens to hold
    an ``=`` is still a path. Raises ValueError on a duplicate or empty label.
    """
    pairs = []
    for item in items:
        label, sep, rest = item.partition("=")
        if sep and label and "/" not in label and "\\" not in label:
            directory = rest
        else:
            directory = item
            label = item.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
        if not label or not directory:
            raise ValueError(f"cannot read a label and folder from {item!r}")
        pairs.append((label, directory))
    labels = [label for label, _ in pairs]
    if len(set(labels)) != len(labels):
        raise ValueError(f"duplicate recording labels: {labels}")
    return pairs


def build_guard(min_safe_depth):
    """Build an ObstacleGuard with the given threshold, as the edge's camera sensor does."""
    from flightrisk.drone.obstacle import ObstacleGuard

    return ObstacleGuard(
        min_safe_depth=min_safe_depth,
        check_interval=0.0,
        center_stat="p90",
    )


def video_frames(video_path):
    """Yield ``(frame, None, None)`` for every frame of a video file, BGR as OpenCV reads it."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: could not open video file: {video_path}", file=sys.stderr)
        sys.exit(1)
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            yield frame, None, None
    finally:
        cap.release()


def _float_or_none(text):
    try:
        return float(text) if text else None
    except ValueError:
        return None


def recorded_frames(frames_dir):
    """Yield ``(frame, captured_at_s, decode_lag_ms)`` for every frame of an ``obstacle_record.py`` recording.

    In order. ``decode_lag_ms`` is read from the ``decode_lag_ms`` column, or from
    ``capture_to_arrival_ms`` in a recording made before it was renamed; None when neither is there.

    The PNGs are read with OpenCV, which gives back the BGR arrays the
    recorder wrote, pixel for pixel. A frame file that cannot be read stops
    the replay with an error, because a gap would shift every frame number.
    """
    directory = Path(frames_dir)
    index = directory / INDEX_NAME
    if not index.is_file():
        print(f"Error: {directory} holds no {INDEX_NAME}; record it with scripts/obstacle_record.py",
              file=sys.stderr)
        sys.exit(1)
    with open(index, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        path = directory / row["file"]
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if frame is None:
            print(f"Error: could not read {path}", file=sys.stderr)
            sys.exit(1)
        captured = row.get("captured_at_s")
        lag = next((row[name] for name in DECODE_LAG_FIELDS if row.get(name)), None)
        yield frame, (float(captured) if captured else None), _float_or_none(lag)


def run_replay(video_path, stride, min_safe_depth, csv_out=None, frames_dir=None, guard=None):
    """Replay a recording and run the obstacle sensor on every nth frame.

    Args:
        video_path: Path to a video file, or None when ``frames_dir`` is given.
        stride: Process every stride-th frame.
        min_safe_depth: Depth threshold for obstacle detection.
        csv_out: Path to write CSV output (optional).
        frames_dir: A recording written by ``scripts/obstacle_record.py`` (optional).
        guard: An already built ``ObstacleGuard`` (for tests); built here by default.

    Returns:
        List of per-frame results (dicts).
    """
    from flightrisk.drone.obstacle_sensor import CHANNEL_ORDER_BGR, to_sensor_frame

    source = recorded_frames(frames_dir) if frames_dir is not None else video_frames(video_path)
    if guard is None:
        guard = build_guard(min_safe_depth)
    results = []
    frame_num = 0
    processed = 0

    print(f"Processing {'recording' if frames_dir is not None else 'video'}: {frames_dir or video_path}")
    print(f"Stride: {stride}, Min safe depth: {min_safe_depth}")
    print(
        "Frame\tAction\tDepth\tCentre_Stat\tLatency_ms\tSafe\tCentre_Depth\t"
        "P90_Depth\tLeft_Depth\tRight_Depth\tConfidence",
        flush=True
    )

    for raw, captured_at, decode_lag_ms in source:
        frame_num += 1

        # Skip frames based on stride
        if (frame_num - 1) % stride != 0:
            continue

        processed += 1
        # OpenCV reads BGR, the sensor's order: the same conversion the edge's feeds apply.
        frame = to_sensor_frame(raw, CHANNEL_ORDER_BGR)

        # Run obstacle check
        start = time.perf_counter()
        result = guard.check_path(frame)
        latency_ms = round((time.perf_counter() - start) * 1000, 1)

        # Extract key fields
        safe = result.get("safe", False)
        action = result.get("action", "unknown")
        center_depth = result.get("center_depth", 0.0)
        center_p90 = result.get("center_p90", 0.0)
        left_depth = result.get("left_depth", 0.0)
        right_depth = result.get("right_depth", 0.0)
        confidence = result.get("confidence", 0.0)
        center_stat = result.get("center_stat", "mean")

        # Print to stdout
        print(
            f"{frame_num}\t{action}\t{center_depth:.3f}\t{center_stat}\t"
            f"{latency_ms:.1f}\t{safe}\t{center_depth:.3f}\t{center_p90:.3f}\t"
            f"{left_depth:.3f}\t{right_depth:.3f}\t{confidence:.3f}",
            flush=True
        )

        # Store result for CSV output
        results.append({
            "frame": frame_num,
            "processed": processed,
            "action": action,
            "center_depth": center_depth,
            "center_p90": center_p90,
            "left_depth": left_depth,
            "right_depth": right_depth,
            "confidence": confidence,
            "center_stat": center_stat,
            "latency_ms": latency_ms,
            "safe": safe,
            "captured_at_s": "" if captured_at is None else captured_at,
            "decode_lag_ms": "" if decode_lag_ms is None else decode_lag_ms,
        })

    # Write CSV if requested
    if csv_out:
        try:
            with open(csv_out, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(results)
            print(f"\nWrote {len(results)} frames to: {csv_out}", file=sys.stderr)
        except Exception as e:
            print(f"Error writing CSV: {e}", file=sys.stderr)
            sys.exit(1)

    print(
        f"\nProcessed {processed} frames (of {frame_num} total) with stride {stride}",
        file=sys.stderr
    )

    return results


# ---------------------------------------------------------------------------
# --metrics-report: compare candidate obstacle metrics on recordings
# ---------------------------------------------------------------------------

#: Metric names in report order. Higher is closer for every one of them.
METRIC_NAMES = ("score", "centre_floor_ratio", "centre_sides_ratio")
#: Fixed threshold grids for the sweep.
SWEEP_GRIDS = {
    "score": [round(0.30 + 0.05 * i, 2) for i in range(15)],
    "centre_floor_ratio": [round(0.30 + 0.10 * i, 2) for i in range(13)],
    "centre_sides_ratio": [round(0.50 + 0.10 * i, 2) for i in range(11)],
}
VOTE_LENGTHS = (1, 2, 3)


def depth_map_of(guard):
    """The raw depth map of the guard's last check, or None."""
    return getattr(guard, "_last_depth_map", None)


def frame_metrics(result, depth_map):
    """The metrics of one frame: ``{name: float or None}``.

    The score is the guard's own centre score (``center_depth``); a degenerate map has none, so
    None, which the report reads as blocked. The candidates come from the raw depth map.
    """
    from flightrisk.drone.obstacle import centre_floor_ratio, centre_sides_ratio

    score = None if result.get("degenerate") else result.get("center_depth")
    if score is not None and not np.isfinite(score):
        score = None
    return {
        "score": None if score is None else float(score),
        "centre_floor_ratio": None if depth_map is None else centre_floor_ratio(depth_map),
        "centre_sides_ratio": None if depth_map is None else centre_sides_ratio(depth_map),
    }


def collect_metrics(directory, stride, guard):
    """Run the guard over a recording and return one metrics dict per processed frame, in order."""
    from flightrisk.drone.obstacle_sensor import CHANNEL_ORDER_BGR, to_sensor_frame

    rows = []
    for number, (raw, _captured, _lag) in enumerate(recorded_frames(directory)):
        if number % stride != 0:
            continue
        result = guard.check_path(to_sensor_frame(raw, CHANNEL_ORDER_BGR))
        rows.append(frame_metrics(result, depth_map_of(guard)))
    return rows


def summarize(values):
    """n, min, p10, median, p90, max over the non-None values, and the share of None."""
    present = [v for v in values if v is not None]
    out = {"n": len(values), "none_share": (len(values) - len(present)) / len(values) if values else 0.0}
    if present:
        arr = np.asarray(present, dtype=float)
        out.update(
            min=float(arr.min()), p10=float(np.percentile(arr, 10)), median=float(np.median(arr)),
            p90=float(np.percentile(arr, 90)), max=float(arr.max()),
        )
    else:
        out.update(min=None, p10=None, median=None, p90=None, max=None)
    return out


def is_clear(value, threshold):
    """A frame is clear when its metric is at or below the threshold. None is never clear."""
    return value is not None and value <= threshold


def clear_rate(values, threshold):
    """The share of frames that are clear at the threshold (None counts as blocked)."""
    if not values:
        return None
    return sum(1 for v in values if is_clear(v, threshold)) / len(values)


def consecutive_clear_rate(values, threshold, n):
    """The share of windows of ``n`` consecutive frames that are all clear, from the observed order.

    No independence is assumed: the windows are the real runs of the recording. None when the
    recording has fewer than ``n`` frames.
    """
    windows = len(values) - n + 1
    if windows < 1:
        return None
    flags = [is_clear(v, threshold) for v in values]
    return sum(1 for i in range(windows) if all(flags[i:i + n])) / windows


def best_threshold(clear_values, obstacle_values):
    """The threshold that best separates clear frames from obstacle frames, and its error rates.

    A candidate threshold is every observed value; a frame is called clear when its value is at or
    below it, so None is called blocked. ``false_clear`` is the share of obstacle frames called
    clear (the dangerous error), ``false_block`` the share of clear frames called blocked. The best
    minimises the mean of the two, then the false clear rate, then the threshold. Also reported:
    the lowest false block rate with no false clear at all (``safe``), or None if there is none.
    Returns None without values on either side.
    """
    candidates = sorted({v for v in list(clear_values) + list(obstacle_values) if v is not None})
    if not clear_values or not obstacle_values or not candidates:
        return None
    scored = []
    for t in candidates:
        fc = clear_rate(obstacle_values, t)
        fb = 1.0 - clear_rate(clear_values, t)
        scored.append({"threshold": t, "false_clear": fc, "false_block": fb, "balanced_error": round((fc + fb) / 2, 12)})
    best = min(scored, key=lambda r: (r["balanced_error"], r["false_clear"], r["threshold"]))
    safe_rows = [r for r in scored if r["false_clear"] == 0]
    safe = min(safe_rows, key=lambda r: (r["false_block"], r["threshold"])) if safe_rows else None
    return {"best": best, "safe": safe}


def build_report(per_label, clear_labels, obstacle_labels, score_threshold, ratio_threshold):
    """The report numbers: ``per_label`` is ``{label: [metrics dict per frame]}``."""
    report = {"labels": list(per_label), "score_threshold": score_threshold, "metrics": {}}
    for name in METRIC_NAMES:
        series = {label: [row[name] for row in rows] for label, rows in per_label.items()}
        entry = {"summary": {label: summarize(vals) for label, vals in series.items()}}
        entry["sweep"] = {
            "thresholds": SWEEP_GRIDS[name],
            "clear_rate": {
                label: [clear_rate(vals, t) for t in SWEEP_GRIDS[name]] for label, vals in series.items()
            },
        }
        separation = None
        if clear_labels and obstacle_labels:
            separation = best_threshold(
                [v for lb in clear_labels for v in series[lb]],
                [v for lb in obstacle_labels for v in series[lb]],
            )
        entry["separation"] = separation
        if name == "score":
            operating = score_threshold
        elif separation is not None:
            operating = separation["best"]["threshold"]
        else:
            operating = ratio_threshold
        entry["voting"] = {
            "threshold": operating,
            "p_consecutive_clear": {
                label: {str(n): consecutive_clear_rate(vals, operating, n) for n in VOTE_LENGTHS}
                for label, vals in series.items()
            },
        }
        report["metrics"][name] = entry
    return report


def _fmt(value, spec=".3f"):
    return "-" if value is None else format(value, spec)


def format_report(report):
    """The report as text tables."""
    lines = []
    labels = report["labels"]
    width = max([len(lb) for lb in labels] + [5])
    for name, entry in report["metrics"].items():
        lines.append("")
        lines.append(f"== {name} (higher is closer; clear means at or below the threshold; None counts as blocked) ==")
        lines.append(f"{'label':<{width}}  {'n':>5} {'min':>7} {'p10':>7} {'median':>7} {'p90':>7} {'max':>7} {'None%':>6}")
        for label in labels:
            s = entry["summary"][label]
            lines.append(
                f"{label:<{width}}  {s['n']:>5} {_fmt(s['min']):>7} {_fmt(s['p10']):>7} {_fmt(s['median']):>7} "
                f"{_fmt(s['p90']):>7} {_fmt(s['max']):>7} {s['none_share'] * 100:>5.1f}%"
            )
        lines.append("")
        lines.append("Clear rate by threshold:")
        thresholds = entry["sweep"]["thresholds"]
        lines.append(f"{'label':<{width}}  " + " ".join(f"{t:>5.2f}" for t in thresholds))
        for label in labels:
            rates = entry["sweep"]["clear_rate"][label]
            lines.append(f"{label:<{width}}  " + " ".join(f"{_fmt(r, '.2f'):>5}" for r in rates))
        sep = entry["separation"]
        if sep is not None:
            b, s = sep["best"], sep["safe"]
            lines.append("")
            lines.append(
                f"Best separating threshold: {b['threshold']:.3f}  false clear (obstacle called clear) "
                f"{b['false_clear'] * 100:.1f}%  false block (clear called blocked) {b['false_block'] * 100:.1f}%"
            )
            if s is None:
                lines.append("No threshold gives zero false clears.")
            else:
                lines.append(
                    f"Lowest false block with zero false clears: threshold {s['threshold']:.3f}  "
                    f"false block {s['false_block'] * 100:.1f}%"
                )
        vote = entry["voting"]
        lines.append("")
        lines.append(f"Probability of N consecutive clear frames at threshold {vote['threshold']:.3f}:")
        lines.append(f"{'label':<{width}}  " + " ".join(f"{'N=' + str(n):>6}" for n in VOTE_LENGTHS))
        for label in labels:
            rates = vote["p_consecutive_clear"][label]
            lines.append(f"{label:<{width}}  " + " ".join(f"{_fmt(rates[str(n)], '.3f'):>6}" for n in VOTE_LENGTHS))
    return "\n".join(lines) + "\n"


def run_metrics_report(recordings, stride, min_safe_depth, clear_labels=(), obstacle_labels=(),
                       ratio_threshold=1.0, json_out=None, guard=None, out=None):
    """Replay each ``(label, dir)`` recording, print the metrics report as text, and return the report dict.

    Text only: no image or crop is written anywhere. ``guard`` is for tests.
    """
    out = out or sys.stdout
    if guard is None:
        guard = build_guard(min_safe_depth)
    per_label = {}
    for label, directory in recordings:
        print(f"Replaying {label}: {directory}", file=sys.stderr, flush=True)
        per_label[label] = collect_metrics(directory, stride, guard)
    report = build_report(per_label, list(clear_labels), list(obstacle_labels), min_safe_depth, ratio_threshold)
    out.write(format_report(report))
    if json_out:
        with open(json_out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Wrote report numbers to: {json_out}", file=sys.stderr)
    return report


def main(argv=None):
    args = parse_args(argv)
    if args.metrics_report:
        run_metrics_report(
            args.recordings, args.stride, args.min_safe_depth, args.clear_labels,
            args.obstacle_labels, args.ratio_threshold, args.json_out,
        )
        return
    run_replay(
        args.video,
        args.stride,
        args.min_safe_depth,
        args.csv_out,
        frames_dir=args.frames,
    )


if __name__ == "__main__":
    main()
