"""Session recorder — saves the full search session as a video file.

Records annotated frames to an MP4 file for backup demos and review.
Also supports playing back recorded sessions as a video source.
"""

import time
import threading
from pathlib import Path

import cv2
import numpy as np

RECORDINGS_DIR = Path(__file__).parent.parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)


class SessionRecorder:
    """Records video frames to an MP4 file."""

    def __init__(self, fps: int = 15, resolution: tuple[int, int] = (960, 720)):
        self.fps = fps
        self.resolution = resolution
        self._writer: cv2.VideoWriter | None = None
        self._recording = False
        self._frame_count = 0
        self._filepath: Path | None = None
        self._lock = threading.Lock()

    def start(self, filename: str | None = None) -> str:
        """Start recording.

        Returns:
            Path to the recording file.
        """
        if self._recording:
            return str(self._filepath)

        if filename is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"session_{ts}.mp4"

        self._filepath = RECORDINGS_DIR / filename
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(
            str(self._filepath), fourcc, self.fps, self.resolution
        )
        self._recording = True
        self._frame_count = 0
        print(f"[recorder] Recording started: {self._filepath}")
        return str(self._filepath)

    def write_frame(self, frame: np.ndarray):
        """Write a frame to the recording."""
        if not self._recording or self._writer is None:
            return

        with self._lock:
            resized = cv2.resize(frame, self.resolution)
            self._writer.write(resized)
            self._frame_count += 1

    def stop(self) -> str | None:
        """Stop recording.

        Returns:
            Path to the completed recording, or None if not recording.
        """
        if not self._recording:
            return None

        with self._lock:
            self._recording = False
            if self._writer:
                self._writer.release()
                self._writer = None

        duration = self._frame_count / self.fps if self.fps > 0 else 0
        print(f"[recorder] Recording saved: {self._filepath} "
              f"({self._frame_count} frames, {duration:.1f}s)")
        return str(self._filepath)

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def frame_count(self) -> int:
        return self._frame_count
