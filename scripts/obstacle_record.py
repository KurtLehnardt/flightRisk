#!/usr/bin/env python3
"""Record exactly the frames the obstacle sensor sees, with their capture times (hardware item H1).

Connects to one Tello and turns its video on. It never takes off: no flight
command is sent, so the motors do not start. The frames are fed into the same
kind of frame holder the edge's obstacle sensor judges, through the same two
feeds (``flightrisk.edge_device.feed_sensor_holder`` and
``DecodedFramePoller``): every new decoded frame once, converted from the
decoder's RGB to the sensor's BGR, stamped with the decoder's capture time,
and refused when the decoder marks it stale. Every frame the holder stores is
written as a lossless PNG, so ``scripts/obstacle_replay.py --frames <dir>``
gives the depth model exactly the input it had in flight.

Usage:
    venv/bin/python scripts/obstacle_record.py --out rec/plant_150cm --seconds 5 --label "plant 150 cm"
    venv/bin/python scripts/obstacle_record.py --out rec/wall_300cm --tello-local-ip 192.168.10.2

Writes into ``--out`` (which must not hold a recording yet):
    frame_000001.png ...  one BGR PNG per frame the sensor would judge
    frames.csv            index, file, captured_at_s, arrived_at_s, capture_to_arrival_ms, wall_time
    meta.json             label, channel order, settle time, frame and stale counts, Tello host

``captured_at_s`` and ``arrived_at_s`` are ``time.monotonic()`` seconds: the
latest capture time the decoder vouches for (the arrival of the frame's first
datagram with ``--tello-local-ip``; the frame's arrival otherwise) and the
time the frame reached the holder. Hold a clock in view to measure the video
latency for H5 from the same recording.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Run from a checkout without installing the package.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from flightrisk.drone.obstacle_sensor import (  # noqa: E402
    ENV_OBSTACLE_SETTLE_S,
    OBSTACLE_SETTLE_S,
    SENSOR_CHANNEL_ORDER,
    LatestFrame,
    TimedFrame,
)

#: Name of the frame index written next to the PNGs.
INDEX_NAME = "frames.csv"
META_NAME = "meta.json"
INDEX_FIELDS = ["index", "file", "captured_at_s", "arrived_at_s", "capture_to_arrival_ms", "wall_time"]
#: How often the frame loop polls the drone, like the edge's frame loop (30 fps).
_POLL_S = 1 / 30


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record the frames the obstacle sensor sees, with capture times, for calibration (H1)."
    )
    parser.add_argument("--out", required=True, help="Directory to write the recording into (created)")
    parser.add_argument("--seconds", type=float, default=5.0, help="How long to record (default 5)")
    parser.add_argument("--label", default="", help="What is in front of the drone, for meta.json")
    parser.add_argument("--tello-host", default="192.168.10.1", help="The Tello's IP (default 192.168.10.1)")
    parser.add_argument(
        "--tello-local-ip", default=None,
        help="Bind to this Wi-Fi adapter's IP, as the edge does (docs/two-tellos-one-pc.md). With it the "
             "decoder stamps capture times and marks stale frames; without it frames count from arrival",
    )
    parser.add_argument("--tello-ifindex", type=int, default=None, help="Windows interface index, as for the edge")
    args = parser.parse_args(argv)
    if not 0 < args.seconds <= 600:
        parser.error("--seconds must be more than 0 and at most 600")
    return args


class FrameRecorder:
    """Writes every frame a ``LatestFrame`` stores to ``out_dir`` as a PNG plus an index row.

    ``add`` is the holder's listener: it only queues the frame, so the feeds
    are not held up; a writer thread writes the files. ``close`` writes what
    is queued and returns the number of frames written. The frames are
    written as they are held, in ``SENSOR_CHANNEL_ORDER`` (BGR), which is
    what ``cv2.imwrite`` expects, so the PNG shows the true colours and
    ``cv2.imread`` gives the same array back.
    """

    def __init__(self, out_dir: Path, wall_clock: Callable[[], float] = time.time):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if (self.out_dir / INDEX_NAME).exists():
            raise FileExistsError(f"{self.out_dir} already holds a recording ({INDEX_NAME})")
        self._wall_clock = wall_clock
        self._queue: queue.SimpleQueue[tuple[TimedFrame, float] | None] = queue.SimpleQueue()
        self._index = open(self.out_dir / INDEX_NAME, "w", newline="")
        self._writer = csv.DictWriter(self._index, fieldnames=INDEX_FIELDS)
        self._writer.writeheader()
        self.written = 0
        self.errors: list[str] = []
        self._thread = threading.Thread(target=self._run, name="obstacle-recorder", daemon=True)
        self._thread.start()

    def add(self, timed: TimedFrame) -> None:
        self._queue.put((timed, self._wall_clock()))

    def close(self) -> int:
        self._queue.put(None)
        self._thread.join()
        self._index.close()
        return self.written

    def _run(self) -> None:
        import cv2  # lazy: keep the module importable for its tests without touching OpenCV first

        while True:
            item = self._queue.get()
            if item is None:
                return
            timed, wall = item
            number = self.written + 1
            name = f"frame_{number:06d}.png"
            try:
                if not cv2.imwrite(str(self.out_dir / name), timed.frame):
                    raise OSError(f"cv2.imwrite refused {name}")
            except Exception as exc:  # noqa: BLE001 - keep recording; the count tells what is missing
                self.errors.append(f"{name}: {exc}")
                continue
            self._writer.writerow({
                "index": number,
                "file": name,
                "captured_at_s": f"{timed.captured_at:.6f}",
                "arrived_at_s": f"{timed.arrived_at:.6f}",
                "capture_to_arrival_ms": f"{(timed.arrived_at - timed.captured_at) * 1000.0:.1f}",
                "wall_time": f"{wall:.6f}",
            })
            self.written = number


def record(
    drone: Any,
    out_dir: Path,
    seconds: float,
    *,
    label: str = "",
    host: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Any = None,
) -> dict[str, Any]:
    """Record ``seconds`` of the frames the obstacle sensor would see from a connected ``drone``.

    The drone's frame loop (``get_frame``) is polled at 30 fps, as the edge
    does, and the decoder poller runs beside it. Returns the summary also
    written to ``meta.json``. The drone is not disconnected.
    """
    from flightrisk.edge_device import DecodedFramePoller, feed_sensor_holder

    holder = LatestFrame(clock)
    feed_sensor_holder(drone, holder)
    recorder = FrameRecorder(out_dir)
    holder.add_listener(recorder.add)
    poller = DecodedFramePoller.for_drone(drone, holder, log)
    if poller is not None:
        poller.start()
    polls = 0
    try:
        end = clock() + seconds
        while clock() < end:
            drone.get_frame()
            polls += 1
            sleep(_POLL_S)
    finally:
        if poller is not None:
            poller.stop()
        written = recorder.close()
    settle_raw = (os.environ.get(ENV_OBSTACLE_SETTLE_S) or "").strip()
    reader = getattr(getattr(drone, "tello", None), "background_frame_read", None)
    meta = {
        "label": label,
        "channel_order": SENSOR_CHANNEL_ORDER,
        "seconds": seconds,
        "frames_written": written,
        "frames_held": holder.frames_seen,
        "stale_frames_refused": holder.stale_frames,
        "last_stale_cause": holder.last_stale_cause,
        "capture_stamps": callable(getattr(reader, "frame_info", None)),
        "decoder": {
            name: getattr(reader, name)
            for name in ("frames_decoded", "stale_frames", "decode_errors", "datagrams_dropped")
            if isinstance(getattr(reader, name, None), int)
        },
        "frame_loop_polls": polls,
        "settle_s": float(settle_raw) if settle_raw else OBSTACLE_SETTLE_S,
        "tello_host": host,
        "write_errors": recorder.errors[:20],
    }
    with open(Path(out_dir) / META_NAME, "w") as f:
        json.dump(meta, f, indent=2)
    return meta


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from flightrisk.drone.tello import TelloController

    out = Path(args.out)
    drone = TelloController(
        name="recorder", host=args.tello_host, local_ip=args.tello_local_ip, ifindex=args.tello_ifindex,
    )
    if not drone.connect():
        print(f"Error: could not connect to the Tello at {args.tello_host}", file=sys.stderr)
        return 1
    try:
        meta = record(drone, out, args.seconds, label=args.label, host=args.tello_host)
    finally:
        drone.disconnect()
    print(
        f"Wrote {meta['frames_written']} frames to {out} ({meta['stale_frames_refused']} stale frames refused, "
        f"capture stamps {'on' if meta['capture_stamps'] else 'off: no --tello-local-ip'})."
    )
    print(f"Replay: venv/bin/python scripts/obstacle_replay.py --frames {out} --out {out}/replay.csv")
    return 0 if meta["frames_written"] else 1


if __name__ == "__main__":
    sys.exit(main())
