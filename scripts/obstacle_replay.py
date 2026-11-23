#!/usr/bin/env python3
"""Replay a drone recording and run the obstacle sensor on every nth frame.

Calibrates the depth threshold (FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH) against
known distances in a recording. Prints per-frame cue values, decisions and
latency to stdout; optionally writes a CSV for further analysis.

Usage:
    python scripts/obstacle_replay.py --frames <dir> [--stride n] [--out csv] [--min-safe-depth x]
    python scripts/obstacle_replay.py --video <mp4> [--stride n] [--out csv] [--min-safe-depth x]
    python scripts/obstacle_replay.py --metrics-report rec/yard_clear rec/yard_wall100 \\
        --clear yard_clear --obstacle yard_wall100 [--vote-interval-s 0.35] [--json report.json]

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
        reports the current score, ``centre_floor_ratio``, ``centre_sides_ratio`` and
        ``centre_floor_span`` (n, min, p10, median, p90, max, share of None), a clear-rate
        threshold sweep, and the probability of N consecutive clears (N-of-N voting, N=1..3).
        Votes are resampled on the capture times so that consecutive votes are about
        ``--vote-interval-s`` apart, and a gap of more than 3 median frame intervals (a refused or stale
        stretch) ends a run, so no window spans it. ``--clear`` and
        ``--obstacle`` take comma separated labels and add the best separating threshold per
        metric with its error rates (each recording weighted equally, per-recording false clears,
        the margin, a leave-one-recording-out estimate); that threshold is also the one the voting
        table uses. The threshold is fitted on the recordings it is judged on and is not
        validated. ``--json FILE`` also writes the numbers. None counts as not clear (blocked),
        the fail-safe reading.
    --vote-interval-s s: With --metrics-report: the spacing of consecutive votes in seconds
        (default 0.35, about the settle time plus one inference). 0 votes on every frame.
    --ratio-threshold x: With --metrics-report: the voting threshold of the ratio metrics when
        no --clear and --obstacle labels give a fitted one (default 1.0).

Every frame goes through ``flightrisk.drone.obstacle_sensor.to_sensor_frame``,
the same conversion the edge's frame feeds use, so a frame replayed here and
the same frame in flight reach the depth model as the same tensor. OpenCV
reads PNG and video files as BGR, the sensor's channel order.
"""

import argparse
import csv
import json
import math
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
        "--ratio-threshold", type=float, default=None,
        help="With --metrics-report: the voting threshold of the ratio metrics when no --clear and "
             "--obstacle labels give a fitted one (default 1.0)"
    )
    parser.add_argument(
        "--vote-interval-s", type=float, default=None,
        help="With --metrics-report: the spacing of consecutive votes in seconds, resampled on the capture "
             f"times (default {DEFAULT_VOTE_INTERVAL_S}, about settle plus inference; 0 votes on every frame)"
    )
    parser.add_argument(
        "--json", dest="json_out",
        help="With --metrics-report: also write the report numbers to this JSON file"
    )
    parsed = parser.parse_args(args)
    if parsed.stride < 1:
        parser.error("--stride must be 1 or more")
    if not math.isfinite(parsed.min_safe_depth):
        parser.error("--min-safe-depth must be a finite number")
    if parsed.ratio_threshold is not None and not math.isfinite(parsed.ratio_threshold):
        parser.error("--ratio-threshold must be a finite number")
    if parsed.vote_interval_s is not None and (not math.isfinite(parsed.vote_interval_s) or parsed.vote_interval_s < 0):
        parser.error("--vote-interval-s must be a finite number, 0 or more")
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
        if parsed.csv_out:
            parser.error("--out writes the per-frame CSV of --frames and --video; use --json with --metrics-report")
        if bool(parsed.clear_labels) != bool(parsed.obstacle_labels):
            print("Warning: --clear and --obstacle must both be given to fit a threshold; "
                  "without both there is no separation, no margin and no fitted voting threshold.",
                  file=sys.stderr)
        if parsed.ratio_threshold is None:
            parsed.ratio_threshold = 1.0
        if parsed.vote_interval_s is None:
            parsed.vote_interval_s = DEFAULT_VOTE_INTERVAL_S
    elif (parsed.json_out or parsed.clear or parsed.obstacle or parsed.ratio_threshold is not None
          or parsed.vote_interval_s is not None):
        parser.error("--json, --clear, --obstacle, --ratio-threshold and --vote-interval-s need --metrics-report")
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


def load_index(frames_dir):
    """The rows of a recording's frame index. Exits with a message when the recording has no index."""
    directory = Path(frames_dir)
    index = directory / INDEX_NAME
    if not index.is_file():
        print(f"Error: {directory} holds no {INDEX_NAME}; record it with scripts/obstacle_record.py",
              file=sys.stderr)
        sys.exit(1)
    with open(index, newline="") as f:
        return list(csv.DictReader(f))


def recorded_frames(frames_dir, stride=1):
    """Yield ``(frame, captured_at_s, decode_lag_ms)`` for every ``stride``-th frame of an ``obstacle_record.py`` recording.

    In order, starting with the first frame. ``decode_lag_ms`` is read from the ``decode_lag_ms`` column, or
    from ``capture_to_arrival_ms`` in a recording made before it was renamed; None when neither is there.

    The PNGs are read with OpenCV, which gives back the BGR arrays the
    recorder wrote, pixel for pixel. Frames that stride skips are not decoded. A frame file that cannot be
    read stops the replay with an error, because a gap would shift every frame number.
    """
    directory = Path(frames_dir)
    rows = load_index(directory)
    for row in rows[::stride]:
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

    # A recording decodes only the frames it processes; a video has to decode every frame to reach the next.
    source = recorded_frames(frames_dir, stride) if frames_dir is not None else video_frames(video_path)
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
        if frames_dir is not None:
            frame_num = processed * stride + 1  # the recording already skipped the other frames
        else:
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
        f"\nProcessed {processed} frames "
        f"{'' if frames_dir is not None else f'(of {frame_num} total) '}with stride {stride}",
        file=sys.stderr
    )

    return results


# ---------------------------------------------------------------------------
# --metrics-report: compare candidate obstacle metrics on recordings
# ---------------------------------------------------------------------------

#: Metric names in report order. Higher is closer for every one of them.
METRIC_NAMES = ("score", "centre_floor_ratio", "centre_sides_ratio", "centre_floor_span")
#: Fixed threshold grids for the sweep.
SWEEP_GRIDS = {
    "score": [round(0.30 + 0.05 * i, 2) for i in range(15)],
    "centre_floor_ratio": [round(0.30 + 0.10 * i, 2) for i in range(13)],
    "centre_sides_ratio": [round(0.50 + 0.10 * i, 2) for i in range(11)],
    "centre_floor_span": [round(0.10 * i, 2) for i in range(16)],
}
VOTE_LENGTHS = (1, 2, 3)
#: The spacing of consecutive votes: about the settle time plus one inference.
DEFAULT_VOTE_INTERVAL_S = 0.35
#: Two consecutive frames further apart than this many median frame intervals are a gap (refused or stale frames).
GAP_FACTOR = 3.0
SOURCE_FITTED = "fitted on these recordings"
SOURCE_FIXED = "fixed"


class RecordingError(Exception):
    """A recording that cannot be replayed (missing index, no rows, missing frame file)."""


def depth_map_of(guard):
    """The raw depth map of the guard's last check, or None."""
    return getattr(guard, "_last_depth_map", None)


def frame_metrics(result, depth_map):
    """The metrics of one frame: ``{name: float or None}``.

    The score is the guard's own centre score (``center_depth``); a degenerate map has none, so
    None, which the report reads as blocked. The candidates come from the raw depth map.
    """
    from flightrisk.drone.obstacle import centre_floor_ratio, centre_floor_span, centre_sides_ratio

    score = None if result.get("degenerate") else result.get("center_depth")
    if score is not None and not np.isfinite(score):
        score = None
    return {
        "score": None if score is None else float(score),
        "centre_floor_ratio": None if depth_map is None else centre_floor_ratio(depth_map),
        "centre_sides_ratio": None if depth_map is None else centre_sides_ratio(depth_map),
        "centre_floor_span": None if depth_map is None else centre_floor_span(depth_map),
    }


def read_height_cm(directory):
    """The flight height in centimetres a recording's ``meta.json`` states, or None when absent or unreadable."""
    try:
        with open(Path(directory) / "meta.json") as f:
            value = json.load(f).get("height_cm")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def check_recording(label, directory):
    """Raise RecordingError unless the recording has its index, at least one row and every listed frame file."""
    path = Path(directory)
    if not path.is_dir():
        raise RecordingError(f"{label}: {path} is not a folder")
    index = path / INDEX_NAME
    if not index.is_file():
        raise RecordingError(f"{label}: {path} holds no {INDEX_NAME}; record it with scripts/obstacle_record.py")
    try:
        with open(index, newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error) as exc:
        raise RecordingError(f"{label}: cannot read {index}: {exc}") from exc
    if not rows:
        raise RecordingError(f"{label}: {index} lists no frames")
    missing = [row.get("file") for row in rows if not row.get("file") or not (path / row["file"]).is_file()]
    if missing:
        raise RecordingError(
            f"{label}: {len(missing)} of {len(rows)} frame files listed in {index} are missing "
            f"(first: {missing[0]!r}); the recording is partial"
        )


PROTECTED_NAMES = (INDEX_NAME, "meta.json")


def check_json_target(json_out, recordings=()):
    """Raise RecordingError unless the report can be written to ``json_out`` without harming a recording.

    A field recording cannot be made again, so the report is refused inside any of the listed recording
    folders, and under the name of a recording's own files (``frames.csv``, ``meta.json``) anywhere.
    ``recordings`` is the ``(label, directory)`` pairs of the run.
    """
    target = Path(json_out).resolve()
    if not target.parent.is_dir():
        raise RecordingError(f"--json: the folder {target.parent} does not exist")
    if target.is_dir():
        raise RecordingError(f"--json: {json_out} is a folder")
    if target.name.lower() in PROTECTED_NAMES:
        raise RecordingError(
            f"--json: {json_out} is named like a recording file ({', '.join(PROTECTED_NAMES)}) and would "
            "overwrite recorded data; choose another name"
        )
    for label, directory in recordings:
        folder = Path(directory).resolve()
        if target == folder or folder in target.parents:
            raise RecordingError(
                f"--json: {json_out} is inside the recording {label} ({directory}); a recording is never "
                "written to, so choose a file outside it"
            )


def collect_metrics(directory, stride, guard):
    """Run the guard over a recording and return its processed frames in order.

    Returns ``{"rows": [metrics dict per frame], "times": [captured_at_s or None per frame],
    "stride": stride, "height_cm": float or None}``. Only every ``stride``-th PNG is decoded.
    """
    from flightrisk.drone.obstacle_sensor import CHANNEL_ORDER_BGR, to_sensor_frame

    rows, times = [], []
    for raw, captured, _lag in recorded_frames(directory, stride):
        result = guard.check_path(to_sensor_frame(raw, CHANNEL_ORDER_BGR))
        rows.append(frame_metrics(result, depth_map_of(guard)))
        times.append(captured)
    return {"rows": rows, "times": times, "stride": stride, "height_cm": read_height_cm(directory)}


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


def median_interval(times):
    """The median time between consecutive frames, or None without two usable capture times."""
    if len(times) < 2 or any(t is None for t in times):
        return None
    steps = np.diff(np.asarray(times, dtype=float))
    steps = steps[steps > 0]
    return float(np.median(steps)) if len(steps) else None


def vote_segments(values, times, interval_s):
    """Split a recording's per-frame values into runs of votes; see ``_vote_runs`` (values only)."""
    return [[value for value, _t in run] for run in _vote_runs(values, times, interval_s)]


def median_vote_spacing(times, interval_s, values=None):
    """The median time in seconds between consecutive votes inside a run, or None without two votes in a run."""
    if times is None:
        return None
    values = [0] * len(times) if values is None else values
    steps = []
    for run in _vote_runs(values, times, interval_s):
        steps.extend(b[1] - a[1] for a, b in zip(run, run[1:]))
    return float(np.median(steps)) if steps else None


def _vote_runs(values, times, interval_s):
    """Split a recording's per-frame values into runs of votes, as ``[[value, ...], ...]``.

    With capture times, the frames are resampled so that consecutive votes are at least
    ``interval_s`` apart (0 keeps every frame), and a gap between two frames of more than
    ``GAP_FACTOR`` median frame intervals, or a time that goes backwards, ends the run, so that
    no window of votes spans frames the recorder refused as stale. Without capture times the
    values are one run in frame order and are not resampled. Each run is ``[(value, time), ...]``
    (the time is None without capture times).
    """
    if not values:
        return []
    interval = median_interval(times) if times is not None and len(times) == len(values) else None
    if interval is None:
        return [[(v, None) for v in values]]
    gap = GAP_FACTOR * interval
    segments, current = [], []
    last_vote = previous = None
    for value, t in zip(values, times):
        if previous is not None and (t - previous > gap or t < previous):
            if current:
                segments.append(current)
            current, last_vote = [], None
        previous = t
        if last_vote is None or interval_s <= 0 or t - last_vote >= interval_s - 1e-9:
            current.append((value, t))
            last_vote = t
    if current:
        segments.append(current)
    return segments


def consecutive_clear_rate_segments(segments, threshold, n):
    """The share of windows of ``n`` consecutive votes, inside one run, that are all clear.

    The windows never join two runs (two recordings, or the two sides of a gap). None when no run
    has ``n`` votes.
    """
    windows = hits = 0
    for values in segments:
        flags = [is_clear(v, threshold) for v in values]
        count = len(flags) - n + 1
        if count < 1:
            continue
        windows += count
        hits += sum(1 for i in range(count) if all(flags[i:i + n]))
    return hits / windows if windows else None


def consecutive_clear_rate(values, threshold, n):
    """The share of windows of ``n`` consecutive frames that are all clear, from the observed order.

    No independence is assumed: the windows are the real runs of the recording. None when the
    recording has fewer than ``n`` frames.
    """
    return consecutive_clear_rate_segments([list(values)], threshold, n)


def _groups(values):
    """``{label: [values]}`` from a dict, or one group named ``all`` from a flat list; empty groups dropped."""
    if isinstance(values, dict):
        return {k: list(v) for k, v in values.items() if len(v)}
    values = list(values)
    return {"all": values} if values else {}


def _mean(items):
    items = list(items)
    return sum(items) / len(items)


def _present(groups):
    return [v for g in groups.values() for v in g if v is not None]


def best_threshold(clear_values, obstacle_values):
    """The threshold that best separates clear recordings from obstacle recordings, and its error rates.

    Each argument is ``{label: [values]}`` (or one flat list, taken as one recording). A frame is called
    clear when its value is at or below the threshold, so None is called blocked. ``false_clear`` is the
    share of obstacle frames called clear (the dangerous error), ``false_block`` the share of clear frames
    called blocked; each is the mean over the recordings of that class, so a long recording does not outweigh
    a short one (``pooled_*`` give the frame-pooled rates). The candidates are every observed value and
    one value just below the lowest obstacle value. The best minimises the mean of the two errors, then the
    false clear rate, then the threshold. ``safe`` is the candidate with no false clear at all and the lowest
    false block rate: it exists whenever there is a candidate, but it may block every clear frame.
    ``midpoint`` is the best threshold moved halfway to the next observed value. ``margin`` is the lowest
    obstacle value minus the highest clear value (positive means a threshold separates them completely).
    Returns None without values on either side.
    """
    clear, obstacle = _groups(clear_values), _groups(obstacle_values)
    if not clear or not obstacle:
        return None
    observed = sorted({v for v in _present(clear) + _present(obstacle)})
    obstacle_present = _present(obstacle)
    candidates = [(t, False) for t in observed]
    if obstacle_present:
        below = float(np.nextafter(min(obstacle_present), -np.inf))
        if below not in observed:
            candidates.append((below, True))
    if not candidates:
        return None
    pooled_clear = [v for g in clear.values() for v in g]
    pooled_obstacle = [v for g in obstacle.values() for v in g]
    scored = []
    for t, is_below in candidates:
        per_false_clear = {lb: clear_rate(vals, t) for lb, vals in obstacle.items()}
        per_false_block = {lb: 1.0 - clear_rate(vals, t) for lb, vals in clear.items()}
        fc, fb = _mean(per_false_clear.values()), _mean(per_false_block.values())
        scored.append({
            "threshold": t, "false_clear": fc, "false_block": fb, "balanced_error": round((fc + fb) / 2, 12),
            "pooled_false_clear": clear_rate(pooled_obstacle, t), "pooled_false_block": 1.0 - clear_rate(pooled_clear, t),
            "per_label_false_clear": per_false_clear, "per_label_false_block": per_false_block,
            "just_below_lowest_obstacle": is_below,
        })
    best = dict(min(scored, key=lambda r: (r["balanced_error"], r["false_clear"], r["threshold"])))
    higher = [v for v in observed if v > best["threshold"]]
    best["midpoint"] = best["threshold"]
    if higher:
        halfway = (best["threshold"] + min(higher)) / 2
        # Next to the lowest obstacle value the average can round up to the observed value itself, which
        # would call that obstacle clear; the threshold is kept unless the midpoint is strictly below it.
        if halfway < min(higher):
            best["midpoint"] = halfway
    safe_rows = [r for r in scored if r["false_clear"] == 0]
    safe = min(safe_rows, key=lambda r: (r["false_block"], r["threshold"])) if safe_rows else None
    clear_present = _present(clear)
    margin = (min(obstacle_present) - max(clear_present)) if obstacle_present and clear_present else None
    return {
        "best": best, "safe": safe, "margin": margin,
        "n_clear_recordings": len(clear), "n_obstacle_recordings": len(obstacle),
        "none_share": {
            "clear": _mean(sum(v is None for v in g) / len(g) for g in clear.values()),
            "obstacle": _mean(sum(v is None for v in g) / len(g) for g in obstacle.values()),
        },
        "caveat": f"Threshold fitted on {len(clear)} clear and {len(obstacle)} obstacle recordings; not validated.",
    }


def leave_one_out(clear_values, obstacle_values):
    """A leave-one-recording-out estimate of the best threshold's error on a recording it was not fitted on.

    For each recording the threshold is fitted on all the others and applied to it: an obstacle recording
    gives a false clear rate, a clear recording a false block rate. A recording whose removal leaves one class
    empty has no estimate (None). The means are over the recordings that have one.
    """
    clear, obstacle = _groups(clear_values), _groups(obstacle_values)
    per_recording = {}
    for kind, groups in (("obstacle", obstacle), ("clear", clear)):
        for label, values in groups.items():
            fit = best_threshold(
                {k: v for k, v in clear.items() if not (kind == "clear" and k == label)},
                {k: v for k, v in obstacle.items() if not (kind == "obstacle" and k == label)},
            )
            if fit is None:
                per_recording[label] = None
                continue
            t = fit["best"]["threshold"]
            rate = clear_rate(values, t)
            per_recording[label] = (
                {"held_out_as": "obstacle", "threshold": t, "false_clear": rate} if kind == "obstacle"
                else {"held_out_as": "clear", "threshold": t, "false_block": 1.0 - rate}
            )
    fcs = [r["false_clear"] for r in per_recording.values() if r and "false_clear" in r]
    fbs = [r["false_block"] for r in per_recording.values() if r and "false_block" in r]
    return {
        "per_recording": per_recording,
        "false_clear": _mean(fcs) if fcs else None,
        "false_block": _mean(fbs) if fbs else None,
    }


def _as_recording(data):
    """A per-label entry as a recording dict: a plain list of metric rows has no times, stride or height."""
    if isinstance(data, dict):
        return data
    return {"rows": list(data), "times": None, "stride": 1, "height_cm": None}


def build_report(per_label, clear_labels, obstacle_labels, score_threshold, ratio_threshold,
                 vote_interval_s=DEFAULT_VOTE_INTERVAL_S):
    """The report numbers: ``per_label`` is ``{label: recording}`` as ``collect_metrics`` returns it.

    A recording may also be a plain list of metrics dicts (no capture times: one run, no resampling).
    With both ``clear_labels`` and ``obstacle_labels`` every metric is voted on at its own fitted best
    threshold; without them, at the fixed ``score_threshold`` (score) or ``ratio_threshold`` (the others).
    """
    recs = {label: _as_recording(data) for label, data in per_label.items()}
    report = {
        "labels": list(recs), "score_threshold": score_threshold, "ratio_threshold": ratio_threshold,
        "vote_interval_s": vote_interval_s, "warnings": [], "metrics": {},
    }
    report["sampling"] = {}
    for label, rec in recs.items():
        times = rec.get("times")
        report["sampling"][label] = {
            "stride": rec.get("stride", 1),
            "median_frame_interval_s": median_interval(times) if times is not None and len(times) == len(rec["rows"]) else None,
            "frames": len(rec["rows"]),
            "height_cm": rec.get("height_cm"),
        }
        usable = times is not None and len(times) == len(rec["rows"]) and median_interval(times) is not None
        spacing = median_vote_spacing(times, vote_interval_s) if usable else None
        report["sampling"][label]["median_vote_spacing_s"] = spacing
        if not rec["rows"]:
            report["warnings"].append(f"{label}: no frames (n=0); its numbers are empty")
        elif len(rec["rows"]) >= 2 and not usable:
            missing = sum(1 for t in times if t is None) if times is not None else len(rec["rows"])
            report["warnings"].append(
                f"{label}: {missing} of {len(rec['rows'])} frames have no usable captured_at_s, so its votes are "
                "in frame order, are not resampled and no gap ends a run; its vote spacing is unknown"
            )
        elif spacing is not None and vote_interval_s > 0 and spacing > 1.2 * vote_interval_s:
            report["warnings"].append(
                f"{label}: the median achieved vote spacing is {spacing:.3f} s, more than 20% over "
                f"--vote-interval-s {vote_interval_s:g} s (the frames are too sparse to vote that fast; "
                "a lower stride or a longer interval matches it better)"
            )
    heights = {rec.get("height_cm") for rec in recs.values() if rec.get("height_cm") is not None}
    if len(heights) > 1:
        report["warnings"].append(
            f"recordings were made at different heights ({', '.join(f'{h:g}' for h in sorted(heights))} cm); "
            "a ratio threshold does not carry over between heights")
    known = [rec.get("height_cm") is not None for rec in recs.values()]
    if any(known) and not all(known):
        unknown = [label for label, rec in recs.items() if rec.get("height_cm") is None]
        report["warnings"].append(
            f"some recordings state their height and others do not (unknown: {', '.join(unknown)}); "
            "they may have been made at different heights, and a ratio threshold does not carry over"
        )
    use_fit = bool(clear_labels and obstacle_labels)
    fitted_caveat = None
    for name in METRIC_NAMES:
        series = {label: [row[name] for row in rec["rows"]] for label, rec in recs.items()}
        entry = {"summary": {label: summarize(vals) for label, vals in series.items()}}
        entry["sweep"] = {
            "thresholds": SWEEP_GRIDS[name],
            "clear_rate": {
                label: [clear_rate(vals, t) for t in SWEEP_GRIDS[name]] for label, vals in series.items()
            },
        }
        separation = None
        if use_fit:
            clear_groups = {lb: series[lb] for lb in clear_labels}
            obstacle_groups = {lb: series[lb] for lb in obstacle_labels}
            separation = best_threshold(clear_groups, obstacle_groups)
            if separation is not None:
                separation["leave_one_out"] = leave_one_out(clear_groups, obstacle_groups)
        entry["separation"] = separation
        if separation is not None:
            operating, source = separation["best"]["threshold"], SOURCE_FITTED
        else:
            operating = score_threshold if name == "score" else ratio_threshold
            source = SOURCE_FIXED
        entry["voting"] = {
            "threshold": operating,
            "threshold_source": source,
            "p_consecutive_clear": {},
            "votes": {},
        }
        if source == SOURCE_FITTED:
            entry["voting"]["validated"] = False
            entry["voting"]["caveat"] = separation["caveat"]
            fitted_caveat = separation["caveat"]
        for label, rec in recs.items():
            segments = vote_segments(series[label], rec.get("times"), vote_interval_s)
            entry["voting"]["votes"][label] = sum(len(s) for s in segments)
            entry["voting"]["p_consecutive_clear"][label] = {
                str(n): consecutive_clear_rate_segments(segments, operating, n) for n in VOTE_LENGTHS
            }
        report["metrics"][name] = entry
    if fitted_caveat is not None:
        report["caveat"] = fitted_caveat
    return report


def _fmt(value, spec=".3f"):
    return "-" if value is None else format(value, spec)


def _pct(value):
    return "-" if value is None else f"{value * 100:.1f}%"


def format_report(report):
    """The report as text tables."""
    lines = []
    labels = report["labels"]
    width = max([len(lb) for lb in labels] + [5])
    for warning in report.get("warnings", []):
        lines.append(f"Warning: {warning}")
    lines.append("")
    lines.append("Recordings (a vote is one frame kept after resampling on capture time):")
    lines.append(f"{'label':<{width}}  {'frames':>6} {'stride':>6} {'frame dt s':>10} {'vote dt s':>9} {'height cm':>10}")
    for label in labels:
        s = report["sampling"][label]
        height = "unknown" if s["height_cm"] is None else f"{s['height_cm']:g}"
        lines.append(
            f"{label:<{width}}  {s['frames']:>6} {s['stride']:>6} {_fmt(s['median_frame_interval_s']):>10} "
            f"{_fmt(s.get('median_vote_spacing_s')):>9} {height:>10}"
        )
    lines.append(f"Vote interval: {report['vote_interval_s']:g} s; a gap of more than {GAP_FACTOR:g} frame "
                 "intervals ends a run of votes.")
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
            lines.extend(_format_separation(sep, width))
        vote = entry["voting"]
        lines.append("")
        lines.append(
            f"Probability of N consecutive clear votes (N-of-N) at threshold {vote['threshold']:.4f} "
            f"({vote['threshold_source']}):"
        )
        lines.append(f"{'label':<{width}}  {'votes':>5} " + " ".join(f"{'N=' + str(n):>6}" for n in VOTE_LENGTHS))
        for label in labels:
            rates = vote["p_consecutive_clear"][label]
            lines.append(
                f"{label:<{width}}  {vote['votes'][label]:>5} "
                + " ".join(f"{_fmt(rates[str(n)], '.3f'):>6}" for n in VOTE_LENGTHS)
            )
    return "\n".join(lines) + "\n"


def _format_separation(sep, width):
    b, s = sep["best"], sep["safe"]
    lines = [""]
    lines.append(
        f"Best separating threshold: {b['threshold']:.4f} (midpoint {b['midpoint']:.4f}); each recording weighted equally: "
        f"false clear (obstacle called clear) {_pct(b['false_clear'])}  false block (clear called blocked) {_pct(b['false_block'])}"
    )
    lines.append(
        f"  pooled over frames: false clear {_pct(b['pooled_false_clear'])}  false block {_pct(b['pooled_false_block'])}"
    )
    lines.append(
        f"  None share (counted as blocked): clear recordings {_pct(sep['none_share']['clear'])}, "
        f"obstacle recordings {_pct(sep['none_share']['obstacle'])}"
    )
    lines.append(
        f"  Margin (lowest obstacle value minus highest clear value): {_fmt(sep['margin'], '.4f')}"
        + ("  (negative: the values overlap)" if sep["margin"] is not None and sep["margin"] <= 0 else "")
    )
    if s is not None:
        where = "just below the lowest obstacle value" if s["just_below_lowest_obstacle"] else "an observed value"
        lines.append(
            f"Zero false clears: threshold {s['threshold']:.4f} ({where}); false block {_pct(s['false_block'])}"
            + ("  (every clear frame is blocked)" if s["false_block"] >= 1.0 else "")
        )
    lines.append("Per recording at the best threshold and at the zero-false-clear threshold:")
    lines.append(f"{'label':<{width}}  {'class':<9} {'at best':>9} {'zero-FC':>9}")
    for label, rate in b["per_label_false_clear"].items():
        lines.append(f"{label:<{width}}  {'obstacle':<9} {_pct(rate):>9} {_pct(s['per_label_false_clear'][label]) if s else '-':>9}   (false clear)")
    for label, rate in b["per_label_false_block"].items():
        lines.append(f"{label:<{width}}  {'clear':<9} {_pct(rate):>9} {_pct(s['per_label_false_block'][label]) if s else '-':>9}   (false block)")
    loo = sep.get("leave_one_out")
    if loo is not None:
        lines.append(
            f"Leave-one-recording-out (threshold fitted without the recording): false clear {_pct(loo['false_clear'])}, "
            f"false block {_pct(loo['false_block'])}"
        )
        for label, r in loo["per_recording"].items():
            if r is None:
                lines.append(f"  {label}: no estimate (the other recordings lack a class)")
            else:
                key = "false_clear" if "false_clear" in r else "false_block"
                lines.append(f"  {label}: threshold {r['threshold']:.4f}, {key.replace('_', ' ')} {_pct(r[key])}")
    lines.append(sep["caveat"])
    return lines


def run_metrics_report(recordings, stride, min_safe_depth, clear_labels=(), obstacle_labels=(),
                       ratio_threshold=1.0, json_out=None, guard=None, out=None,
                       vote_interval_s=DEFAULT_VOTE_INTERVAL_S):
    """Replay each ``(label, dir)`` recording, print the metrics report as text, and return the report dict.

    Every recording and the JSON's folder are checked before the depth model is built; a partial
    recording ends the run with a message. Text only: no image or crop is written anywhere. ``guard`` is for tests.
    """
    out = out or sys.stdout
    problems = []
    for label, directory in recordings:
        try:
            check_recording(label, directory)
        except RecordingError as exc:
            problems.append(str(exc))
    if json_out:
        try:
            check_json_target(json_out, recordings)
        except RecordingError as exc:
            problems.append(str(exc))
    if problems:
        for problem in problems:
            print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)
    if json_out and Path(json_out).exists():
        print(f"Warning: {json_out} exists and is overwritten", file=sys.stderr)
    if guard is None:
        guard = build_guard(min_safe_depth)
    per_label = {}
    for label, directory in recordings:
        print(f"Replaying {label}: {directory}", file=sys.stderr, flush=True)
        per_label[label] = collect_metrics(directory, stride, guard)
    report = build_report(
        per_label, list(clear_labels), list(obstacle_labels), min_safe_depth, ratio_threshold, vote_interval_s
    )
    out.write(format_report(report))
    if json_out:
        text = json.dumps(report, indent=2, allow_nan=False)
        with open(json_out, "w") as f:
            f.write(text)
        print(f"Wrote report numbers to: {json_out}", file=sys.stderr)
    return report


def main(argv=None):
    args = parse_args(argv)
    if args.metrics_report:
        run_metrics_report(
            args.recordings, args.stride, args.min_safe_depth, args.clear_labels,
            args.obstacle_labels, args.ratio_threshold, args.json_out,
            vote_interval_s=args.vote_interval_s,
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
