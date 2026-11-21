#!/usr/bin/env python3
"""Benchmark MiDaS latency under a running YOLO pipeline.

Measures depth model inference latency (p50, p95) while YOLO runs in a
background thread to simulate realistic fleet edge load. Can read frames from
a video file or generate synthetic frames.

Usage:
    python scripts/bench_obstacle.py [--video <mp4>] [--frames n] [--with-yolo] [--min-safe-depth x]

Arguments:
    --video <mp4>: Path to video file. If not given, generates synthetic frames.
    --frames n: Number of frames to process (default 100).
    --with-yolo: Run YOLO11n on a background thread to simulate load.
    --min-safe-depth x: Depth threshold (default 0.35).
"""

import argparse
import sys
import threading
import time

import cv2
import numpy as np


def parse_args(args=None):
    """Parse command-line arguments.

    Args:
        args: List of arguments to parse (for testing). If None, uses sys.argv.
    """
    parser = argparse.ArgumentParser(
        description="Benchmark MiDaS latency under YOLO load"
    )
    parser.add_argument(
        "--video", help="Path to video file (optional)"
    )
    parser.add_argument(
        "--frames", type=int, default=100,
        help="Number of frames to process (default 100)"
    )
    parser.add_argument(
        "--with-yolo", action="store_true",
        help="Run YOLO11n on background thread to simulate load"
    )
    parser.add_argument(
        "--min-safe-depth", type=float, default=0.35,
        help="Depth threshold (default 0.35)"
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


def load_yolo_model():
    """Load YOLO11n model for background inference."""
    from ultralytics import YOLO
    return YOLO("yolo11n.pt")


def yolo_worker(model, frame_queue, stop_event):
    """Background thread that runs YOLO inference on frames from queue.

    This simulates the realistic load of running the edge's YOLO pipeline.
    """
    while not stop_event.is_set():
        try:
            frame = frame_queue.get(timeout=0.1)
            if frame is None:
                break
            # Run inference on the frame
            _ = model(frame, verbose=False)
        except:
            pass


def get_frames(video_path, num_frames):
    """Get frames from video or generate synthetic ones.

    Args:
        video_path: Path to video file (or None for synthetic).
        num_frames: Number of frames to generate/read.

    Yields:
        numpy array frames (BGR, 720x960x3).
    """
    if video_path:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"Error: could not open video file: {video_path}", file=sys.stderr)
            sys.exit(1)
        count = 0
        while count < num_frames:
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # Loop video
                continue
            yield frame
            count += 1
        cap.release()
    else:
        # Generate synthetic frames: random noise to mimic camera feed
        for _ in range(num_frames):
            frame = np.random.randint(0, 256, (720, 960, 3), dtype=np.uint8)
            yield frame


def run_benchmark(video_path, num_frames, with_yolo, min_safe_depth):
    """Run latency benchmark.

    Args:
        video_path: Path to video (or None for synthetic).
        num_frames: Number of frames to process.
        with_yolo: Whether to run YOLO in background.
        min_safe_depth: Depth threshold.

    Returns:
        Dict with benchmark results (latencies, p50, p95).
    """
    guard = build_guard(min_safe_depth)

    # Warm up the model
    warmup = np.zeros((720, 960, 3), dtype=np.uint8)
    _ = guard.check_path(warmup)

    # Start YOLO background thread if requested
    yolo_thread = None
    frame_queue = None
    stop_event = None
    if with_yolo:
        try:
            model = load_yolo_model()
            frame_queue = __import__("queue").Queue(maxsize=5)
            stop_event = threading.Event()
            yolo_thread = threading.Thread(
                target=yolo_worker, args=(model, frame_queue, stop_event),
                daemon=True
            )
            yolo_thread.start()
            print("YOLO background thread started", file=sys.stderr)
        except Exception as e:
            print(f"Warning: could not start YOLO: {e}", file=sys.stderr)
            with_yolo = False

    latencies = []
    frame_num = 0

    print(
        f"Running benchmark with {num_frames} frames "
        f"(YOLO load: {'on' if with_yolo else 'off'})",
        file=sys.stderr
    )

    try:
        for frame in get_frames(video_path, num_frames):
            frame_num += 1

            # Feed frame to YOLO if running
            if frame_queue is not None:
                try:
                    frame_queue.put_nowait(frame)
                except:
                    pass

            # Run depth inference
            start = time.perf_counter()
            _ = guard.check_path(frame)
            latency_ms = (time.perf_counter() - start) * 1000.0

            latencies.append(latency_ms)

            if frame_num % 10 == 0:
                print(f"  Processed {frame_num} frames...", file=sys.stderr)

    finally:
        if stop_event is not None:
            stop_event.set()
        if yolo_thread is not None:
            yolo_thread.join(timeout=5.0)

    # Compute statistics
    latencies = sorted(latencies)
    p50 = np.percentile(latencies, 50)
    p95 = np.percentile(latencies, 95)
    mean = np.mean(latencies)
    min_lat = np.min(latencies)
    max_lat = np.max(latencies)

    return {
        "frames_processed": frame_num,
        "latencies": latencies,
        "p50_ms": float(p50),
        "p95_ms": float(p95),
        "mean_ms": float(mean),
        "min_ms": float(min_lat),
        "max_ms": float(max_lat),
    }


def main():
    args = parse_args()

    results = run_benchmark(
        args.video,
        args.frames,
        args.with_yolo,
        args.min_safe_depth,
    )

    print("\n=== Benchmark Results ===", file=sys.stderr)
    print(f"Frames processed: {results['frames_processed']}", file=sys.stderr)
    print(f"Mean latency:     {results['mean_ms']:.1f} ms", file=sys.stderr)
    print(f"P50 latency:      {results['p50_ms']:.1f} ms", file=sys.stderr)
    print(f"P95 latency:      {results['p95_ms']:.1f} ms", file=sys.stderr)
    print(f"Min latency:      {results['min_ms']:.1f} ms", file=sys.stderr)
    print(f"Max latency:      {results['max_ms']:.1f} ms", file=sys.stderr)

    # Return success
    return 0


if __name__ == "__main__":
    sys.exit(main())
