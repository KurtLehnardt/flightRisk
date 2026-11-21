#!/usr/bin/env python3
"""Replay a drone recording and run the obstacle sensor on every nth frame.

Calibrates the depth threshold (FLIGHTRISK_OBSTACLE_MIN_SAFE_DEPTH) against
known distances in a recorded video. Prints per-frame cue values, decisions and
latency to stdout; optionally writes a CSV for further analysis.

Usage:
    python scripts/obstacle_replay.py --video <mp4> [--stride n] [--out csv] [--min-safe-depth x]

Arguments:
    --video <mp4>: Path to the video file to replay.
    --stride n: Process every nth frame (default 1).
    --out csv: Write per-frame data to CSV file (optional).
    --min-safe-depth x: Depth threshold for obstacle detection (default 0.35).
"""

import argparse
import csv
import sys
import time

import cv2
import numpy as np


def parse_args(args=None):
    """Parse command-line arguments.

    Args:
        args: List of arguments to parse (for testing). If None, uses sys.argv.
    """
    parser = argparse.ArgumentParser(
        description="Replay video and run obstacle sensor on every nth frame"
    )
    parser.add_argument(
        "--video", required=True, help="Path to video file (MP4)"
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
    return parser.parse_args(args)


def build_guard(min_safe_depth):
    """Build an ObstacleGuard with the given threshold."""
    from flightrisk.drone.obstacle import ObstacleGuard

    return ObstacleGuard(
        min_safe_depth=min_safe_depth,
        check_interval=0.0,
        center_stat="p90",
    )


def run_replay(video_path, stride, min_safe_depth, csv_out=None):
    """Replay video and run obstacle sensor on every nth frame.

    Args:
        video_path: Path to the video file.
        stride: Process every stride-th frame.
        min_safe_depth: Depth threshold for obstacle detection.
        csv_out: Path to write CSV output (optional).

    Returns:
        List of per-frame results (dicts).
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: could not open video file: {video_path}", file=sys.stderr)
        sys.exit(1)

    guard = build_guard(min_safe_depth)
    results = []
    frame_num = 0
    processed = 0

    print(f"Processing video: {video_path}")
    print(f"Stride: {stride}, Min safe depth: {min_safe_depth}")
    print(
        "Frame\tAction\tDepth\tCentre_Stat\tLatency_ms\tSafe\tCentre_Depth\t"
        "P90_Depth\tLeft_Depth\tRight_Depth\tConfidence",
        flush=True
    )

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_num += 1

        # Skip frames based on stride
        if (frame_num - 1) % stride != 0:
            continue

        processed += 1

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
        })

    cap.release()

    # Write CSV if requested
    if csv_out:
        try:
            with open(csv_out, "w", newline="") as f:
                fieldnames = [
                    "frame", "processed", "action", "center_depth", "center_p90",
                    "left_depth", "right_depth", "confidence", "center_stat",
                    "latency_ms", "safe",
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
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


def main():
    args = parse_args()
    run_replay(
        args.video,
        args.stride,
        args.min_safe_depth,
        args.csv_out,
    )


if __name__ == "__main__":
    main()
