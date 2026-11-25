"""Drone-side edge-device program.

This is the edge half of the edge/ground compute split: it runs on the
drone (e.g. a Jetson Orin Nano), captures frames from a camera or video
file, runs the local detection/embedding pipeline via ``EdgeRunner``, and
streams the resulting ``DetectionMessage``s to the ground station over an
``EdgeTransportSync`` WebSocket connection. It also pulls ground->edge
control messages off the same connection and applies them:

    * ``{"type": "stream_video", "enabled": bool}`` toggles full-frame
      thumbnail streaming at runtime (``EdgeRunner.set_stream_video``).
    * ``{"type": "set_target", "reid_embedding": ..., "face_embedding": ...}``
      updates the target the ground station is looking for. The edge does
      not score against a target today, so this is simply retained (stashed
      on the runner) and logged; it never raises.
    * ``{"type": "set_detection_profile", "profile": ..., "cmd_id": ...}``
      switches the detection profile (model and detection settings) through
      the runner's ``ProfileSwitcher`` (``flightrisk/edge_profiles.py``) and
      is answered with an ``ack`` or ``nack``. It is refused while the drone
      flies; it never touches the flight. ``--detection-profile`` (or
      ``FLIGHTRISK_DETECTION_PROFILE``) picks the startup profile; without
      one the edge runs the ``default`` profile, its detector from before
      profiles existed.

With ``--source tello`` (a real Tello) or ``--source fake`` (an in-process
simulated Tello) the edge also flies. A ``LaneExecutor`` handles the fleet
control messages (``takeoff``, ``land``, ``emergency_land_all``,
``assign_lanes``, ``start_at``, ``ping``, ``clock``, ``heartbeat``) inside a
``SafetyEnvelope`` and sends ``pong``, ``ack``, ``nack`` and ``telemetry``
back. A refused lane assignment is always answered with a ``nack`` naming
the reason, so the ground can give those lanes to another drone. In this
mode a dedicated thread receives control messages, so a landing never waits
behind frame processing. Safety controls on the edge laptop:

    * typing ``l`` and Enter on stdin lands the drone at once;
    * Ctrl+C (SIGINT) lands the drone, then exits;
    * no ground message for ``FLIGHTRISK_LINK_LOSS_LAND_S`` seconds lands it;
    * any exit from the edge loop lands the drone first.

Several ground stations (the two-laptop fleet): with ``--ground-urls`` or
``FLIGHTRISK_GROUND_URLS`` set to a comma-separated list, a flying edge
connects to every ground through ``flightrisk.edge_uplink.EdgeUplink`` and
obeys one of them, its authority (``flightrisk.fleet_authority``): it speaks
protocol v2, executes landings from any ground, and only the authority's
messages move the drone or feed the link-loss watchdog. ``--node-id`` (or
``FLIGHTRISK_NODE_ID``) names this laptop in the edge ``hello``.
``FLIGHTRISK_FLEET_LEGACY=1`` keeps v1 grounds connected as monitor-only
links instead of closing them. Without a ground list the single
``--ws-url`` path is unchanged.

A flying edge stamps every detection message with its ``drone_id`` and its
dead-reckoned pose (``rel_x_cm``, ``rel_y_cm``, ``heading_deg``) from the
safety envelope, so the ground can place it on the shared fleet map.

Appearance-only: with ``--source tello`` or ``--source fake`` the edge never
loads face recognition and never sends a ``face_embedding``, unless
``--with-face`` is passed or ``FLIGHTRISK_APPEARANCE_ONLY=0`` is set. Person
crops (JPEG) and ReID appearance embeddings are still sent to the ground over
the edge WebSocket, which is authenticated with ``FLIGHTRISK_EDGE_TOKEN``.
For ``--source camera`` and ``--source file`` face recognition stays on by
default; ``--appearance-only`` or ``FLIGHTRISK_APPEARANCE_ONLY=1`` turns it
off there too. An explicit flag wins over the environment variable.

Run it with::

    python -m flightrisk.edge_device --source camera
    python -m flightrisk.edge_device --source file --video path/to/clip.mp4
    FLIGHTRISK_EDGE_TOKEN=secret python -m flightrisk.edge_device --ws-url ws://ground:9000
    python -m flightrisk.edge_device --source tello --tello-host 192.168.10.1 \
        --drone-id drone-1 --alt-slot 100
    python -m flightrisk.edge_device --source fake --drone-id drone-2 --alt-slot 130
    python -m flightrisk.edge_device --source tello --tello-local-ip 192.168.10.50 \
        --drone-id drone-2 --alt-slot 80

``--tello-local-ip`` (or ``FLIGHTRISK_TELLO_LOCAL_IP``) binds every Tello
socket to one Wi-Fi adapter's IP, so one computer can run two edges, one per
adapter and drone (docs/two-tellos-one-pc.md). Without it nothing changes.

Which drone and where the frames come from are separate choices:
``--drone`` picks the drone the edge flies (``tello``, ``fake`` or
``mavlink``) and ``--source`` where frames come from (``camera``, ``file``,
``url``, or the drone's own video for ``tello`` and ``fake``). Without
``--drone`` everything works as before: ``--source tello`` and ``--source
fake`` fly, ``camera`` and ``file`` do not. ``--drone mavlink`` runs on a
Pixhawk-class drone's companion computer: it flies a PX4 or ArduPilot
autopilot over MAVLink (``--mavlink-address`` or
``FLIGHTRISK_MAVLINK_ADDRESS``) through
``flightrisk.drone.mavlink_fleet.MavlinkFleetDrone``, reads frames from the
companion computer's camera, a file or a stream URL, and uses the outdoor
safety envelope (metre-scale slot, ceiling and geofence, ``--alt-slot-m``,
``--ceiling-m``, ``--ceiling-margin-m``, ``--geofence-m``). See
docs/open-drone-support.md::

    python -m flightrisk.edge_device --drone mavlink --source camera \
        --mavlink-address serial:///dev/ttyAMA0:921600 --drone-id drone-1 \
        --alt-slot-m 15 --frame-heading-deg 0 --ws-url ws://ground:9000

The core loop (``run_edge_device``) is dependency-injected -- the frame
source, transport, and runner are all passed in -- so it can be unit-tested
without a real camera, network, or vision model. The heavy real-object
construction (cv2 capture, YOLO/CLIP/InsightFace) lives in ``main()`` and is
imported lazily, keeping ``import flightrisk.edge_device`` cheap.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import math
import os
import re
import signal
import sys
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable, Sequence, TextIO

from flightrisk import edge_query
from flightrisk import fleet_protocol as fp
from flightrisk.drone.flight_guards import (
    DEFAULT_REFUSAL_HOLD_S,
    DEFAULT_REFUSAL_RETRIES,
    DEFAULT_REFUSALS_PER_FLIGHT,
    DEFAULT_SETTLE_SAMPLES,
    DEFAULT_SETTLE_TIMEOUT_S,
    DEFAULT_SETTLE_TOLERANCE_CM,
    DEFAULT_TAKEOFF_LATE_S,
    ENV_HOVER_TEST,
    ENV_REFUSAL_HOLD_S,
    ENV_REFUSAL_RETRIES,
    ENV_REFUSALS_PER_FLIGHT,
    ENV_SETTLE_SAMPLES,
    ENV_SETTLE_TIMEOUT_S,
    ENV_SETTLE_TOLERANCE_CM,
    ENV_TAKEOFF_LATE_S,
    EVENT_GUARD_SETTING_NON_DEFAULT,
    REFUSAL_HOLD_RANGE_S,
    REFUSAL_RETRIES_RANGE,
    REFUSALS_PER_FLIGHT_RANGE,
    SETTLE_SAMPLES_RANGE,
    SETTLE_TIMEOUT_RANGE_S,
    SETTLE_TOLERANCE_RANGE_CM,
    TAKEOFF_LATE_RANGE_S,
    FlightGuards,
)
from flightrisk.drone.lane_executor import (
    CONTROL_TYPES,
    MANUAL_CONTROL_TYPES,
    LaneExecutor,
)
from flightrisk.drone.obstacle_sensor import (
    CHANNEL_ORDER_BGR,
    CHANNEL_ORDER_RGB,
    ENV_FAKE_OBSTACLE_Y_CM,
    ENV_MAX_DECODE_LAG_S,
    ENV_OBSTACLE_AVOID_BUDGET_S,
    ENV_OBSTACLE_CLEAR_VOTES,
    ENV_OBSTACLE_MIN_SAFE_DEPTH,
    ENV_OBSTACLE_PERSON_FRACTION,
    ENV_OBSTACLE_RELOOK,
    ENV_OBSTACLE_SENSING,
    ENV_OBSTACLE_SETTLE_S,
    ENV_OBSTACLE_SIDESTEP_CM,
    ENV_OBSTACLE_SIDESTEPS,
    ENV_OBSTACLE_STEP_CM,
    ENV_OBSTACLE_TURN,
    EVENT_SETTING_NON_DEFAULT,
    MAX_DECODE_LAG_RANGE_S,
    MAX_DECODE_LAG_S,
    OBSTACLE_AVOID_BUDGET_RANGE_S,
    OBSTACLE_AVOID_BUDGET_S,
    OBSTACLE_CLEAR_VOTES,
    OBSTACLE_CLEAR_VOTES_RANGE,
    OBSTACLE_MIN_SAFE_DEPTH,
    OBSTACLE_MIN_SAFE_DEPTH_RANGE,
    OBSTACLE_PERSON_FRACTION,
    OBSTACLE_PERSON_FRACTION_RANGE,
    OBSTACLE_RELOOK,
    OBSTACLE_RELOOK_RANGE,
    OBSTACLE_SENSE_TIMEOUT_S,
    OBSTACLE_SETTLE_RANGE_S,
    OBSTACLE_SETTLE_S,
    OBSTACLE_SIDESTEP_CM,
    OBSTACLE_SIDESTEP_RANGE_CM,
    OBSTACLE_SIDESTEPS,
    OBSTACLE_SIDESTEPS_RANGE,
    OBSTACLE_STEP_CM,
    OBSTACLE_STEP_FIELD_CM,
    OBSTACLE_STEP_MAX_CM,
    OBSTACLE_STEP_MIN_CM,
    SENSING_ENV_OFF,
    SENSING_AUTOPILOT,
    SENSING_ENV_REQUIRED,
    SENSING_FORWARD_ONLY,
    SENSING_OFF,
    SENSING_STEPPED,
    SENSOR_CHANNEL_ORDER,
    STALE_CAPTURE_UNKNOWN,
    BlockedZone,
    FakeObstacleSensor,
    FrameStamp,
    LatestFrame,
    NullSensor,
    to_sensor_frame,
)
from flightrisk.drone.safety import (
    CEILING_CM_ENV,
    CEILING_MARGIN_CM_ENV,
    DEFAULT_FIELD_LINK_LOSS_HOLD_S,
    DEFAULT_LINK_LOSS_LAND_S,
    LINK_LOSS_LAND_S_ENV,
    SafetyEnvelope,
    min_takeoff_battery_from_env,
)
from flightrisk.drone.temp_preflight import (
    DEFAULT_TEMP_REFUSE_C,
    DEFAULT_TEMP_WARN_C,
    ENV_TELLO_TEMP_REFUSE_C,
    ENV_TELLO_TEMP_WARN_C,
    TempLimits,
)
from flightrisk.edge import EdgeRunner
from flightrisk.edge_uplink import (
    ENV_FLEET_LEGACY,
    ENV_GROUND_URLS,
    ENV_INFLIGHT_HANDOVER,
    ENV_NODE_ID,
    EdgeUplink,
    default_node_id,
    env_flag,
    parse_ground_urls,
)
from flightrisk.link_log_limits import AUTH_BACKOFF_MAX_S
from flightrisk.observability import StructuredLogger
from flightrisk.transport import EdgeTransportSync

__all__ = [
    "run_edge_device",
    "CV2FrameSource",
    "DroneFrameSource",
    "EdgeFrames",
    "ResilientCaptureSource",
    "start_local_land_key",
    "feed_sensor_holder",
    "main",
]

# Default WebSocket URL, matching the dashboard's --edge-ws default.
_DEFAULT_WS_URL = "ws://localhost:9000"
# Environment variable carrying the shared-secret auth token.
_TOKEN_ENV_VAR = "FLIGHTRISK_EDGE_TOKEN"
# Environment variable that turns face recognition off (1) or on (0).
_APPEARANCE_ONLY_ENV_VAR = "FLIGHTRISK_APPEARANCE_ONLY"
_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")
# Environment fallbacks for --tello-local-ip and --tello-ifindex.
_TELLO_LOCAL_IP_ENV_VAR = "FLIGHTRISK_TELLO_LOCAL_IP"
_TELLO_IFINDEX_ENV_VAR = "FLIGHTRISK_TELLO_IFINDEX"
# Sources that fly a drone (and therefore run the lane executor).
_FLIGHT_SOURCES = ("tello", "fake")
# Drones an edge can fly (``--drone``). Without ``--drone``, ``--source tello``
# and ``--source fake`` pick the drone as they always did.
_DRONES = ("tello", "fake", "mavlink")
# Frame sources an autopilot drone can use: the companion computer's camera,
# a video file (looped) or a stream URL (RTSP, HTTP; anything OpenCV opens).
_CAPTURE_SOURCES = ("camera", "file", "url")
_MAVLINK_ADDRESS_ENV_VAR = "FLIGHTRISK_MAVLINK_ADDRESS"
# MAVSDK 2 and later listen with ``udpin://``; 14540 is the usual port a
# companion computer's MAVLink router forwards to. Loopback by default:
# MAVLink is unsigned, so listening on every interface would let any host on
# the network command the aircraft.
_DEFAULT_MAVLINK_ADDRESS = "udpin://127.0.0.1:14540"
_VIDEO_URL_ENV_VAR = "FLIGHTRISK_VIDEO_URL"
_LINK_LOSS_HOLD_ENV_VAR = "FLIGHTRISK_FIELD_LINK_LOSS_HOLD_S"
# A failing camera is logged at most this often.
_CAMERA_ERROR_LOG_EVERY_S = 30.0
# Outdoor envelope settings in metres (``--drone mavlink``). Separate from the
# indoor FLIGHTRISK_*_CM variables so a room setting never applies outdoors.
_FIELD_ENV = {
    "alt_slot_m": "FLIGHTRISK_FIELD_ALT_SLOT_M",
    "geofence_m": "FLIGHTRISK_FIELD_GEOFENCE_M",
    "ceiling_m": "FLIGHTRISK_FIELD_CEILING_M",
    "ceiling_margin_m": "FLIGHTRISK_FIELD_CEILING_MARGIN_M",
}
_FAILSAFE_ACTION_ENV_VAR = "FLIGHTRISK_FAILSAFE_ACTION"
_FRAME_STRIDE_ENV_VAR = "FLIGHTRISK_EDGE_FRAME_STRIDE"
# The stride is applied by time (``DroneFrameSource``): a detection every N times the longer of the
# frame time and the time one frame takes to process. A fast detector at stride 30 gets one frame a
# second at 30 fps; the edge laptop's 330 ms detector at stride 30 would get one every 10 s without the
# skip cap below (one every 1.3 s with it). Above this the video tile and the search are nearly blind.
MAX_FRAME_STRIDE = 30
# Longest time frames are dropped after a processed frame, however slow it was. Without a cap a
# slow frame at stride N blinds detection for (N - 1) times its processing time: one 5 s frame at
# stride 10 is 45 s with no detection.
_STRIDE_MAX_SKIP_ENV_VAR = "FLIGHTRISK_EDGE_STRIDE_MAX_SKIP_S"
DEFAULT_STRIDE_MAX_SKIP_S = 1.0
STRIDE_MAX_SKIP_RANGE_S = (0.0, 5.0)
# Torch threads per edge process (intra-op; inter-op is set to 1). Each edge runs YOLO and MiDaS,
# so on the 4-core Windows laptop (Layout B) two edges with torch's default of one thread per core each, plus the
# ground, oversubscribe the CPU and the video decoder falls behind. 2 per edge fits two edges and
# the ground on 4 cores. 0 leaves torch's own default.
_TORCH_THREADS_ENV_VAR = "FLIGHTRISK_EDGE_TORCH_THREADS"
DEFAULT_TORCH_THREADS = 2
MAX_TORCH_THREADS = 64


class _Default(str):
    """A ``--source`` default that can be told apart from the same value given on the command line."""


class _DefaultSlot(float):
    """An ``--alt-slot`` default that can be told apart from the same value given on the command line."""
# Simulated speed of the fake drone, so lane progress is visible on the map.
_FAKE_SPEED_CM_S = 100.0
# How long the control thread waits in each ``recv`` call.
_CONTROL_RECV_TIMEOUT_S = 0.1
# Longest wait between reconnect attempts for a flying (fleet) edge. The
# backoff still starts at 1 s but stops doubling at 2 s, so after a cable or
# LAN blip the edge tries again at least every 2 s instead of backing off to
# 30 s (each attempt can still take up to the connect timeout while the link
# is down). A ground that rejects the token holds the retry up to
# AUTH_BACKOFF_MAX_S (10 s). Other sources keep the transport's 30 s default.
_FLEET_RECONNECT_MAX_DELAY_S = 2.0
# A frame that fails to process is skipped; the error is logged for the
# first failure and then every this many, so a broken detector cannot flood
# the log at the frame rate.
_FRAME_ERROR_LOG_EVERY = 100
# Frames that keep failing for this long in a row end the edge loop with the
# last error, so a broken detector still ends the edge (and lands a flying
# drone on the way out) as it did before frame errors were tolerated.
_FRAME_ERROR_GIVE_UP_S = 10.0


# ---------------------------------------------------------------------------
# Frame sources
# ---------------------------------------------------------------------------


class CV2FrameSource:
    """Iterable adapter over an OpenCV ``VideoCapture``.

    Yields BGR frames until the capture is exhausted (a camera that drops
    off, or the end of a video file), then stops. Exposes ``release()`` so
    the core loop can tear the capture down on exit. Kept deliberately thin
    so the heavy cv2 capture object itself is created in ``main()``.
    """

    def __init__(self, capture: Any):
        self._cap = capture

    def __iter__(self) -> "CV2FrameSource":
        return self

    def __next__(self):
        if self._cap is None:
            raise StopIteration
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise StopIteration
        return frame

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def _check_frame_stride(value: Any) -> int:
    """Return *value* as a frame stride, a whole number from 1 to ``MAX_FRAME_STRIDE``.

    A string must be plain ASCII digits (so ``1_0`` and non-ASCII digits are refused).
    Raises ``ValueError`` otherwise.
    """
    message = f"frame stride must be a whole number from 1 to {MAX_FRAME_STRIDE}, got {value!r}"
    if isinstance(value, bool):
        raise ValueError(message)
    if isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit()):
            raise ValueError(message)
        value = int(text)
    if not isinstance(value, int):
        raise ValueError(message)
    if not 1 <= value <= MAX_FRAME_STRIDE:
        raise ValueError(message)
    return value


def _check_max_skip(value: Any) -> float:
    """Return *value* as the stride's skip cap in seconds, a finite number in ``STRIDE_MAX_SKIP_RANGE_S``."""
    lo, hi = STRIDE_MAX_SKIP_RANGE_S
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not lo <= value <= hi:
        raise ValueError(f"the stride's skip cap must be a number of seconds from {lo:g} to {hi:g}, got {value!r}")
    return float(value)


#: How many recent source frames ``EdgeFrames`` remembers with their conversion. The frame loop, the
#: ``on_frame`` feed and ``DecodedFramePoller`` all ask for the newest frame or one just before it.
_RECENT_FRAMES = 4


class EdgeFrames:
    """Every consumer of a drone's video on this edge gets the frame in BGR, converted once per frame.

    The Tello decoders deliver RGB (``frame_channel_order``); OpenCV, YOLO, the ReID and face
    models, the JPEG encoder and the obstacle sensor (``SENSOR_CHANNEL_ORDER``) all take BGR. A
    frame from an RGB source is converted exactly once, by the first consumer that asks for it, and
    every other consumer gets the same array: the frame loop (``DroneFrameSource``, for detection,
    ReID and the JPEGs), the sensor's ``on_frame`` feed and ``DecodedFramePoller``. Before this the
    frame loop handed the RGB frame to YOLO and ReID unconverted (red and blue swapped), and each
    sensor feed made its own reversed copy.

    The shared array is read-only, so no consumer can change what another one sees; the holder
    keeps it as its own copy, because nothing else holds a writable reference to it. A frame from
    a BGR source (``frame_channel_order`` not declared, as for an OpenCV capture) is handed to the
    frame loop unchanged, and the holder gets a copy of it, as before.

    ``source_of`` maps a converted frame back to the decoder's frame, whose capture stamp the
    decoder looks up by identity (``_decoder_frame_stamp``). Thread-safe: a frame is converted
    under the lock, so two threads asking for the same frame never convert it twice.
    ``conversions`` counts the frames converted.
    """

    def __init__(self, channel_order: str = CHANNEL_ORDER_BGR):
        to_sensor_frame(None, channel_order)  # refuses an unknown order now, not at the first frame
        self.channel_order = channel_order
        self._lock = threading.Lock()
        self._recent: deque[tuple[Any, Any]] = deque(maxlen=_RECENT_FRAMES)
        self.conversions = 0

    @classmethod
    def for_drone(cls, drone: Any) -> "EdgeFrames":
        """For a drone's own video: its ``frame_channel_order``, BGR when it declares none."""
        return cls(getattr(drone, "frame_channel_order", CHANNEL_ORDER_BGR))

    @property
    def converts(self) -> bool:
        """True when the source's frames are not in BGR and are converted."""
        return self.channel_order != SENSOR_CHANNEL_ORDER

    def bgr(self, frame: Any) -> Any:
        """``frame`` in BGR for the frame loop: the shared conversion, or ``frame`` itself from a BGR source."""
        if frame is None or not self.converts:
            return frame
        return self._converted(frame)

    def sensor_copy(self, frame: Any) -> Any:
        """The obstacle sensor holder's frame: the shared conversion, or a copy of a BGR source's frame."""
        if self.converts:
            return self._converted(frame)
        return to_sensor_frame(frame, self.channel_order)

    def source_of(self, frame: Any) -> Any:
        """The source frame that ``frame`` was converted from, or ``frame`` itself if it is no recent conversion."""
        with self._lock:
            for source, converted in self._recent:
                if converted is frame:
                    return source
        return frame

    def _converted(self, frame: Any) -> Any:
        with self._lock:
            for source, converted in self._recent:
                if source is frame:
                    return converted
            converted = to_sensor_frame(frame, self.channel_order)
            flags = getattr(converted, "flags", None)
            if flags is not None:
                flags.writeable = False
            self._recent.append((frame, converted))
            self.conversions += 1
            return converted


class DroneFrameSource:
    """Iterable over a drone controller's video frames.

    Calls ``drone.get_frame()`` at most once per ``min_interval_s`` and skips
    ``None`` (no new frame yet, or the stream is recovering) instead of
    stopping, because losing video must not end the edge loop while the
    drone is flying. Iteration stops only when ``stop_event`` is set. The
    drone itself is disconnected by ``main()``, so ``release()`` does nothing.

    With ``frame_stride`` N greater than 1 the consumer is given a fresh frame
    only once per N frame times, or once per N processing times when handling a
    frame is slower than a frame: when a frame is handed out, the time the
    consumer takes to come back for the next one (``busy``) is measured, and
    frames are then dropped until ``N * max(busy, min_interval_s)`` has passed
    since the previous one was handed out, but for no longer than
    ``max_skip_s`` after the consumer came back. Detection, ReID and encoding
    therefore run at about 1/N of the rate they would at stride 1, even on a
    machine where they are slower than the frame rate, until the cap holds
    them: without it one slow frame (5 s on an overloaded laptop) at stride 10
    would leave 45 s with no detection; with it, at most ``max_skip_s``. A
    plain count of every Nth frame would not do that, because after a slow
    detection the newest frame is already many frames ahead and the next ones
    would be taken at once. The edge refuses a stride above 1 while obstacle
    sensing is required (``_resolve_frame_stride``), because the obstacle
    sensor's person cue needs a detection of a view taken after each stop.

    Dropped frames are still read at the normal polling rate. Polling at the full
    rate keeps the newest frame available and keeps the frozen-stream recovery
    working, which counts identical polls; the video itself is decoded on the
    drone controller's own thread. The default, 1, hands out every frame as
    before. ``clock`` and ``wait`` (``wait(seconds)`` returns true when
    stopped) can be injected so tests do not depend on real time.

    Every frame handed out is BGR (``frames``, an ``EdgeFrames``; by default
    one for the drone's ``frame_channel_order``): a Tello's RGB frame is
    converted, once, and only when it is handed out, so a dropped frame costs
    no conversion.
    """

    def __init__(
        self,
        drone: Any,
        stop_event: threading.Event | None = None,
        min_interval_s: float = 1 / 30,
        frame_stride: int = 1,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        max_skip_s: float = DEFAULT_STRIDE_MAX_SKIP_S,
        frames: EdgeFrames | None = None,
    ):
        self._drone = drone
        self._frames = frames if frames is not None else EdgeFrames.for_drone(drone)
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._min_interval_s = min_interval_s
        self._frame_stride = _check_frame_stride(frame_stride)
        self._max_skip_s = _check_max_skip(max_skip_s)
        self._clock = clock
        self._wait = wait if wait is not None else self._stop.wait
        self._next_at = 0.0
        self._yielded_at: float | None = None

    def __iter__(self) -> "DroneFrameSource":
        return self

    def __next__(self):
        ready_at = None
        if self._frame_stride > 1 and self._yielded_at is not None:
            now = self._clock()
            busy = now - self._yielded_at
            cycle = self._frame_stride * max(busy, self._min_interval_s)
            # Frames are dropped for the rest of the cycle, but never for longer than the cap.
            skip = min(cycle - busy, self._max_skip_s)
            # Half a frame of slack so that clock rounding cannot cost a whole extra frame.
            ready_at = now + skip - self._min_interval_s / 2
        while not self._stop.is_set():
            wait = self._next_at - self._clock()
            if wait > 0 and self._wait(wait):
                break
            self._next_at = self._clock() + self._min_interval_s
            frame = self._drone.get_frame()
            if frame is None:
                continue
            if ready_at is not None and self._clock() < ready_at:
                continue
            self._yielded_at = self._clock()
            return self._frames.bgr(frame)
        raise StopIteration

    def release(self) -> None:
        pass


class _FrameFeedingSource:
    """Wraps a frame source and gives every frame it yields to the obstacle sensor's frame holder.

    Used for the MAVLink companion camera, which has no ``drone.on_frame`` hook. The holder gets its
    own copy of each new frame object (``_copying_frame_feed``); an OpenCV capture is already BGR, the
    sensor's order. Everything else (iteration, ``release``) is passed through.
    """

    def __init__(self, source: Any, holder: LatestFrame):
        self._source = source
        self._feed = _copying_frame_feed(holder, CHANNEL_ORDER_BGR)

    def __iter__(self) -> "_FrameFeedingSource":
        return self

    def __next__(self):
        frame = next(self._source)
        self._feed(frame)
        return frame

    def release(self) -> None:
        release = getattr(self._source, "release", None)
        if callable(release):
            release()


class DecodedFramePoller:
    """Feeds the obstacle sensor's frame holder from the Tello's video decoder, independent of detection.

    The frame loop calls ``drone.get_frame()``, which feeds the holder through
    ``drone.on_frame``, only between two detections, so while a slow detection
    runs the holder gets nothing and a gate can find no fresh frame. This
    daemon thread reads the decoder's newest frame every ``interval_s`` and
    gives the holder its own copy of each frame object the holder has not
    been given before.

    The decoder is djitellopy's ``BackgroundFrameRead`` on the controller's
    client (``drone.tello.background_frame_read``), which stores a new array
    for every frame it decodes. The poller only reads it:

    * It never calls ``get_frame``, so the frozen-frame counter and the stream
      recovery in ``TelloController.get_frame`` see exactly the polls they saw
      before, and the ``on_frame`` callbacks still fire once per poll.
    * It never calls ``get_frame_read``, which would open a decoder. While the
      client has none (before the first ``get_frame``, or while a stream
      restart has dropped it), or the drone is not connected, it feeds nothing.
    * It offers each frame to the holder through ``LatestFrame.offer``, the
      same check the frame loop's ``on_frame`` feed (``_copying_frame_feed``)
      goes through, converted from the decoder's RGB to the sensor's BGR
      (the edge's shared ``EdgeFrames`` conversion, so a frame is converted
      once whichever consumer asks first) and with the decoder's ``FrameStamp`` when the
      decoder gives one (``_decoder_frame_stamp``), so the holder judges the
      frame by its capture time and refuses one the decoder marks stale. The
      holder keeps one record of the last source frame
      object for both feeds and refuses that object, whichever feed brings it
      again. While the decoder is stalled neither feed adds anything, even
      though ``get_frame`` keeps handing the stalled frame to ``on_frame``
      until its frozen-frame recovery starts, so the sensor finds the view
      stale instead of judging a frame from before the last move or turn.
    * A decoder in queue mode is left alone, because reading its ``frame``
      would take frames from its queue.

    The black frame that a restarted decoder starts on is a new object, so it
    is fed; the sensor refuses it as blank (``obstacle_camera.blank_frame``).

    A drone without such a client (the fake controllers) gets no poller; see
    ``for_drone``.
    """

    def __init__(
        self,
        drone: Any,
        holder: LatestFrame,
        interval_s: float = 1 / 30,
        log: StructuredLogger | None = None,
        name: str = "obstacle-frames",
        channel_order: str = CHANNEL_ORDER_RGB,
        frames: EdgeFrames | None = None,
    ):
        self._drone = drone
        self._client = getattr(drone, "tello", None)
        self._holder = holder
        # The edge's shared conversion (``EdgeFrames``), so a frame this poller converts is the one the
        # frame loop and the ``on_frame`` feed get, and none of them converts it again.
        self._copy = (frames if frames is not None else EdgeFrames(channel_order)).sensor_copy
        self._stamp = _decoder_frame_stamp(drone)
        self._interval_s = interval_s
        self._log = log
        self._error_logged = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    @classmethod
    def for_drone(
        cls, drone: Any, holder: LatestFrame | None, log: StructuredLogger | None = None,
        frames: EdgeFrames | None = None,
    ) -> "DecodedFramePoller | None":
        """A poller for a drone with a djitellopy client, or None when there is nothing to poll."""
        if holder is None or getattr(drone, "tello", None) is None:
            return None
        name = f"{getattr(drone, 'name', None) or 'drone'}-obstacle-frames"
        return cls(drone, holder, log=log, name=name, frames=frames)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def poll_once(self) -> bool:
        """Feed the decoder's newest frame if the holder has not had it; True when one was fed. Never raises."""
        try:
            state = getattr(self._drone, "state", None)
            if not getattr(state, "is_connected", False):
                return False
            reader = getattr(self._client, "background_frame_read", None)
            if reader is None or getattr(reader, "with_queue", False):
                return False
            frame = reader.frame
            if frame is None:
                return False
            # The holder refuses a frame object that either feed already gave it.
            return self._holder.offer(frame, self._copy, stamp=self._stamp(frame))
        except Exception as exc:  # noqa: BLE001 - the frame loop still feeds the holder
            if not self._error_logged and self._log is not None:
                self._error_logged = True
                self._log.warning("edge_obstacle_frame_poller_error", error=repr(exc)[:200])
            return False

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            self.poll_once()


class ResilientCaptureSource:
    """Frames from an OpenCV capture that is reopened whenever it fails, for a flying edge.

    A flying edge must not end its loop (and land) because the camera
    hiccupped or a video file ended: a failed read releases the capture and
    opens it again after ``retry_s`` (a file therefore loops). Iteration
    stops only when ``stop_event`` is set. ``open_capture`` returns a new
    ``cv2.VideoCapture``-like object.
    """

    def __init__(
        self,
        open_capture: Callable[[], Any],
        stop_event: threading.Event | None = None,
        min_interval_s: float = 1 / 30,
        retry_s: float = 1.0,
        log: StructuredLogger | None = None,
        on_status: Callable[[bool], None] | None = None,
    ):
        self._open = open_capture
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._min_interval_s = min_interval_s
        self._retry_s = retry_s
        self._cap: Any = None
        self._next_at = 0.0
        self._log = log
        self._on_status = on_status
        #: False while the camera fails (None before the first read).
        self.ok: bool | None = None
        self._failures = 0
        self._last_logged = -_CAMERA_ERROR_LOG_EVERY_S

    def _status(self, ok: bool, error: str | None = None) -> None:
        """Record the camera's state; log failures (rate limited) and the recovery."""
        was, self.ok = self.ok, ok
        if ok:
            if was is False and self._log is not None:
                self._log.info("edge_camera_recovered", failed_reads=self._failures)
            self._failures = 0
        else:
            self._failures += 1
            now = time.monotonic()
            if self._log is not None and (was is not False or now - self._last_logged >= _CAMERA_ERROR_LOG_EVERY_S):
                self._last_logged = now
                self._log.error("edge_camera_failed", failed_reads=self._failures, error=error)
        if was != ok and self._on_status is not None:
            with contextlib.suppress(Exception):
                self._on_status(ok)

    def __iter__(self) -> "ResilientCaptureSource":
        return self

    def __next__(self):
        while not self._stop.is_set():
            wait = self._next_at - time.monotonic()
            if wait > 0 and self._stop.wait(wait):
                break
            self._next_at = time.monotonic() + self._min_interval_s
            error = None
            if self._cap is None:
                try:
                    self._cap = self._open()
                except Exception as exc:  # noqa: BLE001 - retried below
                    self._cap, error = None, repr(exc)[:200]
            ok, frame = (False, None)
            if self._cap is not None:
                try:
                    ok, frame = self._cap.read()
                except Exception as exc:  # noqa: BLE001 - treated as a failed read
                    ok, frame, error = False, None, repr(exc)[:200]
            if ok and frame is not None:
                self._status(True)
                return frame
            self._status(False, error or "no frame")
            self.release()
            self._stop.wait(self._retry_s)
        raise StopIteration

    def release(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            with contextlib.suppress(Exception):
                cap.release()


# ---------------------------------------------------------------------------
# Control-message dispatch
# ---------------------------------------------------------------------------


def _handle_control(
    msg: dict[str, Any],
    runner: EdgeRunner,
    log: StructuredLogger,
    flight: LaneExecutor | None = None,
    recv_t: float | None = None,
    feed_watchdog: bool = True,
) -> None:
    """Apply a single ground->edge control message.

    Never raises: an unexpected/malformed control message must not take the
    frame loop down. With a ``flight`` executor, every message resets the
    link-loss watchdog (unless ``feed_watchdog`` is False: the multi-ground
    uplink feeds it itself, from the authority only), fleet control types
    (``CONTROL_TYPES``) and manual-takeover commands
    (``MANUAL_CONTROL_TYPES``) go to the executor, and an unexpected exception lands
    the drone (fail safe). ``recv_t`` is the edge clock reading taken when
    the message arrived. A read-only query (``QUERY_TYPES``) goes to
    ``edge_query`` before anything else: it is answered in any state, never
    fenced, never feeds the watchdog and never lands the drone (PROTO-4).
    """
    # The isinstance check keeps an unhashable "type" (a list, say) out of the
    # set lookup here; inside the try below it lands the drone, as on main.
    if isinstance(msg, dict) and isinstance(msg.get("type"), str) and msg["type"] in fp.QUERY_TYPES:
        # Any ground may ask, so a query is no sign of a live authority.
        edge_query.route(runner, msg, log)
        return
    try:
        mtype = msg.get("type")
        if flight is not None and feed_watchdog:
            # Any message from the ground proves the link is alive.
            flight.note_ground_message()
        if mtype in CONTROL_TYPES or mtype in MANUAL_CONTROL_TYPES:
            # Fleet control and manual takeover (docs/manual-takeover.md).
            if flight is None:
                log.debug("edge_control_no_flight", type=mtype)
            else:
                flight.handle_message(msg, recv_t=recv_t)
        elif mtype == fp.SET_CEILING:
            # Ceiling, margin and altitude slot (docs/ceiling-and-altitude.md);
            # the executor applies them only on the ground and always answers.
            if flight is None:
                log.debug("edge_control_no_flight", type=mtype)
            else:
                flight.handle_set_ceiling(msg)
        elif mtype == "stream_video":
            enabled = bool(msg.get("enabled"))
            runner.set_stream_video(enabled)
            log.info("edge_control_stream_video", enabled=enabled)
        elif mtype == fp.SET_DETECTION_PROFILE:
            switcher = getattr(runner, "profile_switcher", None)
            if switcher is None:
                log.warning("edge_control_profile_unsupported", type=mtype)
            else:
                # Never raises, and loads the model on its own thread.
                switcher.handle(msg)
        elif mtype == "set_target":
            # The edge doesn't score against a target today; just retain the
            # embeddings (stashed on the runner) for future use and log it.
            try:
                runner._edge_target = {  # type: ignore[attr-defined]
                    "reid_embedding": msg.get("reid_embedding"),
                    "face_embedding": msg.get("face_embedding"),
                }
            except Exception:
                pass
            log.debug(
                "edge_control_set_target",
                has_reid=msg.get("reid_embedding") is not None,
                has_face=msg.get("face_embedding") is not None,
            )
        else:
            log.debug("edge_control_unknown", type=mtype)
    except Exception as exc:
        log.warning(
            "edge_control_error",
            type=msg.get("type") if isinstance(msg, dict) else None,
            error=repr(exc),
        )
        if flight is not None:
            try:
                flight.fail_safe("control_error")
            except Exception as land_exc:
                log.error("edge_control_fail_safe_error", error=repr(land_exc))


def _control_pump(
    transport: Any,
    runner: EdgeRunner,
    log: StructuredLogger,
    flight: LaneExecutor,
    stop_event: threading.Event,
    recv_timeout: float = _CONTROL_RECV_TIMEOUT_S,
) -> None:
    """Receive and apply control messages on a dedicated thread.

    Used when the edge flies a drone, so ``land``/``emergency_land_all`` and
    ``ping`` are handled as soon as they arrive rather than after the next
    frame is processed. This thread is the only caller of ``transport.recv``
    in that mode.
    """
    log.info("edge_control_thread_start")
    feed_watchdog = not getattr(transport, "feeds_watchdog", False)
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            ctrl = transport.recv(recv_timeout=recv_timeout)
        except Exception as exc:
            log.warning("edge_control_recv_error", error=repr(exc))
            stop_event.wait(recv_timeout)
            continue
        if not ctrl:
            # A transport that is disconnected returns at once; wait a
            # little rather than spinning.
            if time.monotonic() - started < recv_timeout / 2:
                stop_event.wait(recv_timeout)
            continue
        recv_t = flight.now()
        if isinstance(ctrl, dict):
            _handle_control(ctrl, runner, log, flight=flight, recv_t=recv_t, feed_watchdog=feed_watchdog)
        else:
            log.debug("edge_control_non_dict", value=ctrl)
    log.info("edge_control_thread_stop")


def _make_sender(transport: Any, log: StructuredLogger) -> Callable[[dict[str, Any]], Any]:
    """Return a function that sends one JSON control message to the ground.

    Uses the transport's generic JSON send method when it has one
    (``send_json``, ``send_message``, ``send_control`` or ``send``, checked
    in that order). For an ``EdgeTransportSync`` without such a method it
    falls back to writing on the wrapped transport's WebSocket through the
    wrapper's event-loop bridge. If neither exists, messages are dropped
    with a single error log, so the ground never receives acks or nacks and
    will not start this drone.
    """
    for name in ("send_json", "send_message", "send_control", "send"):
        method = getattr(transport, name, None)
        if callable(method):
            return method

    bridge = getattr(transport, "_bridge", None)
    inner = getattr(transport, "_transport", None)
    if bridge is not None and inner is not None:
        async def _send(msg: dict[str, Any]) -> None:
            ws = getattr(inner, "_ws", None)
            if ws is None or not getattr(inner, "connected", False):
                return
            await ws.send(json.dumps(msg))

        def _bridge_send(msg: dict[str, Any]) -> None:
            bridge.run(_send(msg), timeout=5.0)

        return _bridge_send

    reported = threading.Event()

    def _unavailable(msg: dict[str, Any]) -> None:
        if not reported.is_set():
            reported.set()
            log.error("edge_send_unavailable", transport=type(transport).__name__, type=msg.get("type"))

    return _unavailable


def start_local_land_key(
    flight: LaneExecutor,
    stream: TextIO | None = None,
    log: StructuredLogger | None = None,
) -> threading.Thread:
    """Start a thread that lands the drone when ``l`` and Enter are typed.

    Reads lines from ``stream`` (``sys.stdin`` by default). The land call
    goes straight to the executor from this thread and does not queue. The
    thread ends quietly when the stream closes.
    """
    source = sys.stdin if stream is None else stream
    log = log if log is not None else StructuredLogger(component="edge-device")

    def _loop() -> None:
        try:
            for line in source:
                if line.strip().lower() == "l":
                    log.warning("edge_local_land_key")
                    try:
                        flight.land_now("local_key")
                    except Exception as exc:
                        log.error("edge_local_land_error", error=repr(exc))
        except Exception as exc:
            log.warning("edge_land_key_stopped", error=repr(exc))

    thread = threading.Thread(target=_loop, daemon=True, name="edge-land-key")
    thread.start()
    log.info("edge_land_key_ready", hint="type l then Enter to land")
    return thread


def _wire_emergency_callback(transport: Any, flight: LaneExecutor) -> bool:
    """Land straight from the transport's emergency callback when it has one.

    A transport exposing an ``on_emergency_land`` attribute calls it on its
    own thread the moment ``emergency_land_all`` arrives, ahead of
    ``recv()``. The same message still reaches the control thread through
    ``recv()``; the executor ignores the second land while the first is in
    progress. Returns True if the callback was installed.
    """
    if not hasattr(transport, "on_emergency_land"):
        return False
    transport.on_emergency_land = lambda msg: flight.land_now("emergency_land_all")
    return True


def _install_sigint_land(flight: LaneExecutor, log: StructuredLogger, signals: Any = signal) -> Any:
    """Make Ctrl+C land the drone before the process exits.

    On Windows the same handler also takes SIGBREAK (Ctrl+Break in the edge's
    console), so that key lands the drone too instead of ending the process
    at once. Platforms without SIGBREAK are unchanged.
    ``signals`` is the ``signal`` module, replaceable in tests.

    Returns the previous SIGINT handler, or None if handlers cannot be
    installed (not on the main thread).
    """
    def _on_sigint(signum, frame):
        # A signal that arrives mid-write to stderr can make the log call
        # raise (reentrant call); the land must run regardless.
        with contextlib.suppress(Exception):  # logging must never skip the land
            log.warning("edge_sigint_land")
        try:
            flight.land_now("sigint")
        except Exception as exc:
            log.error("edge_sigint_land_error", error=repr(exc))
        raise KeyboardInterrupt

    try:
        previous = signals.signal(signals.SIGINT, _on_sigint)
    except ValueError:
        return None
    sigbreak = getattr(signals, "SIGBREAK", None)
    if sigbreak is not None:
        with contextlib.suppress(ValueError, OSError):
            signals.signal(sigbreak, _on_sigint)
    return previous


def _restore_sigbreak(signals: Any = signal) -> None:
    """Give SIGBREAK back its default action (Windows only; elsewhere there is no SIGBREAK)."""
    sigbreak = getattr(signals, "SIGBREAK", None)
    if sigbreak is not None:
        with contextlib.suppress(ValueError, OSError):
            signals.signal(sigbreak, signals.SIG_DFL)


def _drain_control(
    transport: Any,
    runner: EdgeRunner,
    log: StructuredLogger,
    recv_timeout: float,
) -> None:
    """Pull and dispatch any pending control messages without blocking.

    Calls ``transport.recv(recv_timeout=<small>)`` until it returns a falsy
    value (``None`` -- nothing waiting), so multiple queued control messages
    are all applied within one frame.
    """
    while True:
        ctrl = transport.recv(recv_timeout=recv_timeout)
        if not ctrl:
            break
        if isinstance(ctrl, dict):
            _handle_control(ctrl, runner, log)
        else:
            log.debug("edge_control_non_dict", value=ctrl)


# ---------------------------------------------------------------------------
# Core loop (dependency-injected, unit-testable)
# ---------------------------------------------------------------------------


def run_edge_device(
    source: Iterable,
    transport: Any,
    runner: EdgeRunner,
    poll_interval: float = 0.0,
    recv_timeout: float = 0.01,
    logger: StructuredLogger | None = None,
    stop_event: Any = None,
    flight: LaneExecutor | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the edge capture/detect/stream loop.

    Args:
        source: Iterable yielding BGR ``numpy`` frames. Iteration ending
            (source exhausted) stops the loop. If it exposes ``release()``,
            that is called on exit.
        transport: An ``EdgeTransportSync``-like object exposing
            ``send_detections(msg)``, ``recv(recv_timeout=...)`` and
            ``disconnect()``.
        runner: An ``EdgeRunner`` producing a ``DetectionMessage`` per frame.
        poll_interval: Optional sleep (seconds) between frames.
        recv_timeout: How long ``recv`` may wait for a control message per
            drain, keeping the poll effectively non-blocking.
        logger: Optional ``StructuredLogger``; one is created if omitted.
        stop_event: Optional object with ``is_set()`` to request a graceful
            stop between frames.
        flight: Optional ``LaneExecutor`` for a flying edge. When given, a
            dedicated thread receives and applies control messages (the
            frame loop no longer drains them), and the drone is landed
            before the transport is disconnected on any exit.

    A frame whose processing raises (for example a detector failure) is
    skipped and logged as ``edge_frame_error`` (the first, then every 100th),
    and ``runner.detector_ok`` is False until a frame succeeds again, so
    telemetry can report it. A single bad frame never ends the loop. Frames
    that keep failing for ``_FRAME_ERROR_GIVE_UP_S`` (10 s) in a row end it
    with the last error (``edge_frame_errors_fatal``), so the exit path lands
    a flying drone as it did before. ``clock`` times that streak.

    Returns:
        The number of frames processed and sent.
    """
    log = logger if logger is not None else StructuredLogger(component="edge-device")
    frame_count = 0
    frame_errors = 0
    failing_since: float | None = None

    log.info("edge_device_start", stream_video=getattr(runner, "stream_video", None))
    pump_stop = threading.Event()
    pump: threading.Thread | None = None
    if flight is not None:
        pump = threading.Thread(
            target=_control_pump,
            args=(transport, runner, log, flight, pump_stop),
            daemon=True,
            name="edge-control",
        )
        pump.start()
    try:
        for frame in source:
            if stop_event is not None and stop_event.is_set():
                log.info("edge_device_stop_requested", frames=frame_count)
                break

            try:
                msg = runner.process_frame(frame)
            except Exception as exc:  # one bad frame must not end the loop or land the drone
                frame_errors += 1
                now = clock()
                if failing_since is None:
                    failing_since = now
                _set_detector_ok(runner, False)
                if frame_errors == 1 or frame_errors % _FRAME_ERROR_LOG_EVERY == 0:
                    log.error("edge_frame_error", error=repr(exc)[:200], count=frame_errors)
                if now - failing_since >= _FRAME_ERROR_GIVE_UP_S:
                    log.error("edge_frame_errors_fatal", error=repr(exc)[:200], count=frame_errors,
                              failing_s=round(now - failing_since, 1))
                    raise
                msg = None
            else:
                if failing_since is not None:
                    log.info("edge_frame_recovered", failed_frames=frame_errors)
                failing_since = None
                frame_errors = 0
                _set_detector_ok(runner, True)
            if msg is not None:
                transport.send_detections(msg)
                frame_count += 1

            # Apply any ground->edge control messages queued since last frame.
            # A flying edge receives them on its own control thread instead.
            if flight is None:
                _drain_control(transport, runner, log, recv_timeout)

            if poll_interval:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("edge_device_interrupted", frames=frame_count)
    finally:
        if flight is not None:
            # Stop taking control messages, then land before the ground link
            # goes away. shutdown() lands only if the drone is flying, and
            # refuses any takeoff the control thread might still deliver.
            pump_stop.set()
            if pump is not None:
                pump.join(timeout=2.0)
            try:
                flight.shutdown()
            except Exception as exc:
                log.error("edge_device_flight_shutdown_error", error=repr(exc))
        try:
            transport.disconnect()
        except Exception:
            log.warning("edge_device_disconnect_error")
        release = getattr(source, "release", None)
        if callable(release):
            try:
                release()
            except Exception:
                log.warning("edge_device_source_release_error")
        log.info("edge_device_stop", frames=frame_count)

    return frame_count


# ---------------------------------------------------------------------------
# CLI entry point (real camera / network / models)
# ---------------------------------------------------------------------------


def _build_source(args: argparse.Namespace, parser: argparse.ArgumentParser, log: StructuredLogger) -> CV2FrameSource:
    """Construct a real cv2-backed frame source from parsed args."""
    import cv2  # lazy: keep module import light for tests

    if args.source == "camera":
        index = getattr(args, "camera_index", 0)
        log.info("edge_source_camera", index=index)
        return CV2FrameSource(cv2.VideoCapture(index))
    if args.source == "url":
        log.info("edge_source_url")
        return CV2FrameSource(cv2.VideoCapture(_video_url(args)))

    # args.source == "file"
    if not os.path.exists(args.video):
        parser.error(f"--video path does not exist: {args.video}")
    log.info("edge_source_file", video=args.video)
    return CV2FrameSource(cv2.VideoCapture(args.video))


def _resolve_appearance_only(args: argparse.Namespace, environ: Any = None) -> bool:
    """Decide whether this edge runs appearance-only (no face recognition).

    ``--appearance-only`` or ``--with-face`` wins. Otherwise an explicit
    ``FLIGHTRISK_APPEARANCE_ONLY`` decides. Otherwise a flying source
    (``tello``, ``fake``) is appearance-only and ``camera``/``file`` are not.
    Raises ``ValueError`` when the environment variable is not a boolean.
    """
    explicit = getattr(args, "appearance_only", None)
    if explicit is not None:
        return bool(explicit)
    env = os.environ if environ is None else environ
    raw = env.get(_APPEARANCE_ONLY_ENV_VAR, "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    if raw:
        raise ValueError(
            f"{_APPEARANCE_ONLY_ENV_VAR}={raw!r} is not a boolean (use 1/0, true/false, yes/no, on/off)"
        )
    return _drone_kind(args) is not None


def _resolve_frame_stride(args: argparse.Namespace, environ: Any = None) -> int:
    """The detection frame stride: ``--frame-stride``, else ``FLIGHTRISK_EDGE_FRAME_STRIDE``, else 1.

    Raises ``ValueError`` with an operator-facing message when the
    environment variable is not a whole number from 1 to ``MAX_FRAME_STRIDE``. A stride above
    1 is only supported for the ``tello`` and ``fake`` sources; any other
    source raises ``ValueError`` rather than silently ignoring it. A stride above 1 is also
    refused while obstacle sensing is required (``FLIGHTRISK_OBSTACLE_SENSING=required``, the
    default): the sensor's person cue needs a detection of a view taken after each stop, and a
    stride starves it. Only the ``off`` override, which flies unsensed anyway, allows a stride. The
    mode is the one the obstacle wiring flies (``_effective_sensing_mode``); a launcher layer's mode is
    checked once the unit is resolved (``_check_resolved_sensing``).
    """
    env = os.environ if environ is None else environ
    flag = getattr(args, "frame_stride", None)
    if flag is not None:
        stride = flag
    else:
        raw = (env.get(_FRAME_STRIDE_ENV_VAR) or "").strip()
        if not raw:
            return 1
        try:
            stride = _check_frame_stride(raw)
        except ValueError as exc:
            raise ValueError(f"{_FRAME_STRIDE_ENV_VAR}: {exc}") from None
    if stride > 1 and _drone_kind(args) not in ("tello", "fake"):
        origin = (
            f"{_FRAME_STRIDE_ENV_VAR}={stride} is set, but a frame stride above 1"
            if flag is None
            else "a frame stride above 1"
        )
        remedy = f" Unset {_FRAME_STRIDE_ENV_VAR} to run this source." if flag is None else ""
        raise ValueError(f"{origin} only applies to --source tello and --source fake.{remedy}")
    if stride > 1:
        origin = f"{_FRAME_STRIDE_ENV_VAR}={stride} is set" if flag is None else f"--frame-stride {stride} is given"
        _refuse_stride_while_sensing(args, origin, env)
    return stride


def _refuse_stride_while_sensing(args: argparse.Namespace, origin: str, environ: Any) -> None:
    """Refuse a frame stride above 1 while the sensing mode the edge flies is required. Raises ``ValueError``."""
    if _effective_sensing_mode(args, environ) != SENSING_ENV_REQUIRED:
        return
    said, remedy = _required_sensing_text(args)
    raise ValueError(
        f"{origin}, but a frame stride above 1 is refused while obstacle sensing is required "
        f"({said}). The stride starves the obstacle "
        "sensor's person cue, which needs a detection of a view taken after each stop, so a person in the "
        f"lane could be missed. Use a stride of 1, or {remedy}, which flies with no "
        "obstacle sensing at all, only for bring-up with a safety observer."
    )


def _resolve_stride_max_skip(environ: Any = None) -> float:
    """``FLIGHTRISK_EDGE_STRIDE_MAX_SKIP_S``: the longest a stride drops frames after a processed one.

    Default ``DEFAULT_STRIDE_MAX_SKIP_S`` (1.0 s). Raises ``ValueError`` with an operator-facing
    message for anything but a plain number in ``STRIDE_MAX_SKIP_RANGE_S``.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(_STRIDE_MAX_SKIP_ENV_VAR) or "").strip()
    if not raw:
        return DEFAULT_STRIDE_MAX_SKIP_S
    lo, hi = STRIDE_MAX_SKIP_RANGE_S
    value = float(raw) if _ASCII_FLOAT_RE.fullmatch(raw) else None
    if value is None or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{_STRIDE_MAX_SKIP_ENV_VAR}={raw!r} must be a number of seconds from {lo:g} to {hi:g}")
    return value


def _resolve_torch_threads(environ: Any = None) -> int:
    """``FLIGHTRISK_EDGE_TORCH_THREADS``: torch threads for this edge process; 0 leaves torch's default.

    Default ``DEFAULT_TORCH_THREADS`` (2). Raises ``ValueError`` with an operator-facing message for
    anything but plain digits from 0 to ``MAX_TORCH_THREADS``.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(_TORCH_THREADS_ENV_VAR) or "").strip()
    if not raw:
        return DEFAULT_TORCH_THREADS
    if not _ASCII_INT_RE.fullmatch(raw) or int(raw) > MAX_TORCH_THREADS:
        raise ValueError(
            f"{_TORCH_THREADS_ENV_VAR}={raw!r} must be a whole number from 0 to {MAX_TORCH_THREADS} "
            "(0 leaves torch's own thread count)"
        )
    return int(raw)


def _limit_torch_threads(threads: int, log: StructuredLogger) -> int | None:
    """Limit this process's torch to ``threads`` intra-op threads and one inter-op thread.

    Call it before any model loads. Two edges on one laptop each run YOLO and MiDaS; with torch's
    default of one thread per core in every process they oversubscribe the CPU, and the video decoder
    falls behind. ``threads`` 0 leaves torch alone. Returns the limit set, or None when nothing was set
    (0, or torch cannot be imported, which is logged).
    """
    if threads <= 0:
        return None
    try:
        import torch  # lazy: heavy, and only an edge process should change its thread pools
    except Exception as exc:  # noqa: BLE001 - an edge without torch fails later, with its own error
        log.warning("edge_torch_threads_unavailable", error=repr(exc)[:200])
        return None
    torch.set_num_threads(threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # already set, or inter-op work has started; the intra-op limit still holds
    log.info("edge_torch_threads", threads=torch.get_num_threads(), interop_threads=torch.get_num_interop_threads())
    return threads


def _drone_kind(args: argparse.Namespace) -> str | None:
    """The drone this edge flies (``tello``, ``fake`` or ``mavlink``), or None for a camera or file edge.

    ``--drone`` when given; otherwise ``--source tello`` and ``--source
    fake`` fly as they always did.
    """
    drone = getattr(args, "drone", None)
    if drone:
        return str(drone)
    source = getattr(args, "source", None)
    return source if source in _FLIGHT_SOURCES else None


def _autopilot_pose_provider(envelope: SafetyEnvelope, holder: dict[str, Any]) -> Callable[[], dict[str, float]]:
    """Pose for detection messages from the autopilot's own position (``--drone mavlink``).

    The measured position in the plan frame and the GPS position when the
    drone reports them; the envelope's commanded pose before the first
    takeoff or while the autopilot's telemetry is stale.
    """
    fallback = _envelope_pose_provider(envelope)

    def _pose() -> dict[str, float]:
        drone = holder.get("drone")
        pose = drone.measured_pose() if drone is not None else None
        if pose is None:
            return fallback()
        fields = {
            "rel_x_cm": round(pose.x_cm, 1),
            "rel_y_cm": round(pose.y_cm, 1),
            "heading_deg": round(pose.heading_deg, 1),
        }
        position = drone.geo_position()
        if position is not None:
            fields.update(lat=position[0], lon=position[1])
        return fields

    return _pose


def _envelope_pose_provider(envelope: SafetyEnvelope) -> Callable[[], dict[str, float]]:
    """Return a pose provider for ``EdgeRunner`` that reads the envelope's dead-reckoned pose."""

    def _pose() -> dict[str, float]:
        pose = envelope.pose_snapshot()
        return {
            "rel_x_cm": round(pose.x_cm, 1),
            "rel_y_cm": round(pose.y_cm, 1),
            "heading_deg": round(pose.heading_deg, 1),
        }

    return _pose


def _startup_profile_name(args: argparse.Namespace) -> str:
    """``--detection-profile``, else ``FLIGHTRISK_DETECTION_PROFILE``, else empty (the default profile)."""
    explicit = getattr(args, "detection_profile", None)
    if explicit:
        return str(explicit)
    from flightrisk.config import get_config

    return str(get_config().vision.detection_profile or "")


def _set_detector_ok(runner: Any, ok: bool) -> None:
    """Record on the runner whether the last frame processed; telemetry reports it."""
    # A runner that cannot take the attribute (a test double) is left alone.
    with contextlib.suppress(Exception):
        runner.detector_ok = ok


def _with_profile_status(send: Callable[[dict[str, Any]], Any], runner: EdgeRunner) -> Callable[[dict[str, Any]], Any]:
    """Wrap the executor's sender so telemetry also reports the detection profile and the detector's health.

    Only telemetry changes, and only by added fields, which every ground
    version accepts (unknown fields on an edge link are allowed):
    ``detector_ok`` (False while frames fail to process) and the switcher's
    ``status_fields``.
    """

    def _send(msg: dict[str, Any]) -> Any:
        if isinstance(msg, dict) and msg.get("type") == fp.TELEMETRY:
            fields: dict[str, Any] = {"detector_ok": getattr(runner, "detector_ok", True) is not False}
            camera_ok = getattr(runner, "camera_ok", None)
            if camera_ok is not None:
                # Only a flying edge with its own camera (--drone mavlink) sets it.
                fields["camera_ok"] = bool(camera_ok)
            switcher = getattr(runner, "profile_switcher", None)
            if switcher is not None:
                # Telemetry goes out even when the switcher cannot report.
                with contextlib.suppress(Exception):
                    fields.update(switcher.status_fields())
            msg = {**msg, **fields}
        return send(msg)

    return _send


def _build_runner(
    args: argparse.Namespace,
    log: StructuredLogger,
    pose_provider: Callable[[], Any] | None = None,
) -> EdgeRunner:
    """Construct detector/reid/face + EdgeRunner the way the dashboard does.

    A flying source tags every detection message with ``args.drone_id`` and
    the pose from ``pose_provider``. Face recognition is constructed only
    when the edge is not appearance-only (see ``_resolve_appearance_only``).
    The detector comes from the startup detection profile (see
    ``_startup_profile_name``); the runner's ``profile_switcher`` changes it
    later on the ground's request.
    """
    # Decide first, so a bad FLIGHTRISK_APPEARANCE_ONLY fails before any
    # model is loaded.
    appearance_only = _resolve_appearance_only(args)

    # Lazy imports: these pull in ultralytics / torch / insightface, which are
    # heavy and unnecessary for importing this module or unit-testing the loop.
    from flightrisk.edge_profiles import ProfileSwitcher

    flying = _drone_kind(args) is not None
    # The switcher's replies carry the runner's drone id: None on a camera or
    # file edge, which then logs refusals without replying. A flying source
    # refuses every switch until the lane executor's flight check is bound.
    switcher = ProfileSwitcher(None, drone_id=args.drone_id if flying else None, flies=flying, logger=log)
    detector, profile_name = switcher.startup_detector(_startup_profile_name(args))

    reid = None
    try:
        from flightrisk.vision.reid import PersonReID
        reid = PersonReID(match_threshold=0.55)
    except Exception as e:
        log.warning("reid_unavailable", error=str(e))

    face = None
    if appearance_only:
        log.info("edge_appearance_only", source=args.source, face_embedding=False)
    else:
        try:
            from flightrisk.vision.face import FaceRecognizer
            face = FaceRecognizer(match_threshold=0.35)
        except Exception as e:
            log.warning("insightface_unavailable", error=str(e))

    runner = EdgeRunner(
        detector=detector,
        reid=reid,
        face=face,
        stream_video=args.stream_video,
        drone_id=args.drone_id if flying else None,
        pose_provider=pose_provider if flying else None,
        appearance_only=appearance_only,
    )
    runner.detection_profile = profile_name
    switcher.bind_runner(runner)
    runner.profile_switcher = switcher
    return runner


def _bind_profile_switcher(runner: EdgeRunner, transport: Any, log: StructuredLogger, flight: Any = None) -> None:
    """Give the runner's profile switcher its reply sender and, on a flying edge, the flight state.

    On a flying edge this binds ``LaneExecutor.is_active`` as the switcher's
    flight check. Until it is bound, a flying edge's switcher refuses every
    switch (it fails closed).
    """
    switcher = getattr(runner, "profile_switcher", None)
    if switcher is None:
        return
    switcher.bind_sender(_make_sender(transport, log))
    if flight is not None:
        switcher.bind_safety(flight.is_active)


def _parse_drone_id(raw: str) -> str:
    """Parse ``--drone-id``: it must be a fleet protocol identifier.

    Every fleet message carries the drone id, and the protocol refuses one
    that is longer than 64 characters (and the peer link one that is not an
    identifier), so a bad id would leave the drone unable to report or be
    commanded. Refusing it here gives a clear error at startup instead.
    """
    if not fp.is_identifier(raw):
        raise argparse.ArgumentTypeError(
            f"--drone-id must be 1 to {fp.MAX_ID_LEN} characters of letters, digits, '.', '_', ':' or '-', "
            f"starting with a letter or digit (for example drone-1), got {raw!r}"
        )
    return raw


def _parse_confirm_less_safe(raw: str) -> tuple[str, str]:
    """Parse one ``--confirm-less-safe id=value``: a parameter id and the exact value it confirms."""
    key, sep, value = raw.partition("=")
    key, value = key.strip(), value.strip()
    if not sep or not key or not value:
        raise argparse.ArgumentTypeError("--confirm-less-safe takes id=value: a parameter id and the exact "
                                         "value it confirms, both non-empty")
    return key, value


def _parse_frame_stride(raw: str) -> int:
    """Parse ``--frame-stride``: a whole number from 1 to ``MAX_FRAME_STRIDE``."""
    try:
        return _check_frame_stride(raw)
    except ValueError as exc:
        # argparse already names the flag in front of this message.
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_alt_slot(raw: str) -> float:
    """Parse ``--alt-slot`` as centimetres.

    Values below 10 are read as metres (``1.6`` means 160 cm), because a
    slot under 10 cm cannot be flown and the fleet docs quote slots both
    ways. The result must be a valid slot for ``SafetyEnvelope``.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--alt-slot must be a number, got {raw!r}") from exc
    if 0 < value < 10:
        value *= 100.0
    try:
        return SafetyEnvelope.validate_alt_slot(round(value, 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FlightRisk edge-device program")
    parser.add_argument(
        "--source", choices=("camera", "file", "url", "tello", "fake"), default=_Default("camera"),
        help="Frame source: the drone's onboard camera (--camera-index, default 0), a video file, "
             "a stream URL (--video-url), a Tello drone (flies), or an in-process fake Tello (flies, no "
             "hardware). With --drone mavlink only camera, file or url apply (default camera)",
    )
    parser.add_argument("--video", type=str, help="Video file path (when --source=file)")
    parser.add_argument("--video-url", type=str, default=None,
                        help="Stream URL OpenCV can open, such as rtsp://... (when --source=url; default $"
                        + _VIDEO_URL_ENV_VAR + ", which keeps credentials in the URL out of the process list)")
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index (when --source=camera; default 0)")
    parser.add_argument(
        "--drone", choices=_DRONES, default=None,
        help="The drone this edge flies: tello, fake (simulated Tello) or mavlink (a PX4 or ArduPilot "
             "autopilot; run the edge on the drone's companion computer). Default: --source tello or fake "
             "picks the drone as before; camera, file and url fly nothing",
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="The drone model this edge flies, a built-in model or one defined in the inventory (default: $"
             "FLIGHTRISK_MODEL, else --airframe, else the inventory unit for --drone-id, else the launcher's "
             "model, else the backend's default: tello for --drone tello and fake, generic_multirotor for "
             "--drone mavlink). The edge refuses to start when the model's scale, adapter or frame path "
             "differs from the backend's",
    )
    parser.add_argument(
        "--airframe", type=str, default=None,
        help="The drone model, when neither --model nor $FLIGHTRISK_MODEL names one (default $FLIGHTRISK_AIRFRAME)",
    )
    parser.add_argument(
        "--confirm-less-safe", type=_parse_confirm_less_safe, action="append", default=None, metavar="ID=VALUE",
        help="Confirm one less-safe value, as the parameter id and the exact value; repeat for each value",
    )
    mav = parser.add_argument_group("MAVLink drone (--drone mavlink)", "See docs/open-drone-support.md")
    mav.add_argument(
        "--mavlink-address", type=str, default=None,
        help="MAVSDK connection URL of the autopilot, for example serial:///dev/ttyAMA0:921600, "
             "udpin://127.0.0.1:14540 or tcpout://127.0.0.1:5760 (default: $" + _MAVLINK_ADDRESS_ENV_VAR
             + ", else " + _DEFAULT_MAVLINK_ADDRESS + "). Listen on another interface only on a trusted, "
             "isolated network: MAVLink is not authenticated",
    )
    mav.add_argument("--mavlink-sysid", type=int, default=None,
                     help="The autopilot's MAVLink system id; the edge refuses to take off if it is another "
                     "(default 1, every factory autopilot)")
    mav.add_argument("--link-loss-hold-s", type=float, default=None,
                     help="Seconds the drone holds position after the ground link is lost before the link-loss "
                     "failsafe (default $" + _LINK_LOSS_HOLD_ENV_VAR + ", else 10; the plan resumes if the link "
                     "returns)")
    mav.add_argument("--autopilot", choices=("auto", "px4", "ardupilot"), default="auto",
                     help="Autopilot firmware (default auto: detected from its parameters)")
    mav.add_argument("--alt-slot-m", type=float, default=None,
                     help="Altitude slot in metres above the takeoff point (default $" + _FIELD_ENV["alt_slot_m"]
                     + ", else 15)")
    mav.add_argument("--ceiling-m", type=float, default=None,
                     help="Ceiling in metres above the takeoff point (default $" + _FIELD_ENV["ceiling_m"]
                     + ", else 30; at most 120)")
    mav.add_argument("--ceiling-margin-m", type=float, default=None,
                     help="Clearance kept below the ceiling in metres (default $" + _FIELD_ENV["ceiling_margin_m"]
                     + ", else 5; at least 3)")
    mav.add_argument("--geofence-m", type=float, default=None,
                     help="Geofence half-width in metres around the takeoff point (default $"
                     + _FIELD_ENV["geofence_m"] + ", else 30)")
    mav.add_argument("--failsafe-action", choices=("rtl", "land"), default=None,
                     help="What link loss, low battery and a geofence breach do: rtl (the autopilot's return to "
                     "launch) or land in place (default $" + _FAILSAFE_ACTION_ENV_VAR + ", else rtl)")
    mav.add_argument("--frame-heading-deg", type=float, default=None,
                     help="Required: compass bearing of the plan's forward axis (0 is north). Every drone of a "
                     "fleet must use the same value, or the zones rotate into each other")
    mav.add_argument("--speed-mps", type=float, default=3.0, help="Cruise speed in m/s (default 3, at most 15)")
    mav.add_argument("--takeoff-alt-m", type=float, default=5.0,
                     help="Takeoff height in metres before the climb to the slot (default 5)")
    mav.add_argument(
        "--allow-unverified-failsafes", action="store_true",
        help="UNSAFE, for simulation only: take off without checking the autopilot's geofence, battery and "
             "RC-loss failsafe parameters",
    )
    parser.add_argument(
        "--ws-url", type=str,
        default=os.environ.get("FLIGHTRISK_EDGE_WS", _DEFAULT_WS_URL),
        help=f"Ground-station WebSocket URL (default: {_DEFAULT_WS_URL})",
    )
    parser.add_argument(
        "--ground-urls", type=str, default=os.environ.get(ENV_GROUND_URLS, ""),
        help="Comma-separated WebSocket URLs of every ground station (two-laptop fleet, "
             "--source tello or fake). Overrides --ws-url. Default: $" + ENV_GROUND_URLS,
    )
    parser.add_argument(
        "--node-id", type=str, default=None,
        help="This laptop's node id in the fleet hello (default: $FLIGHTRISK_NODE_ID or the host name)",
    )
    parser.add_argument(
        "--stream-video", action="store_true",
        help="Stream full-frame thumbnails from the start (default: off)",
    )
    parser.add_argument(
        "--tello-host", type=str, default=None,
        help="Tello IP address (when --source=tello; default from config, 192.168.10.1)",
    )
    parser.add_argument(
        "--tello-local-ip", type=str, default=None,
        help="Bind every Tello socket to this local IP, the Wi-Fi adapter that joined this drone "
             "(--source tello only; default: $" + _TELLO_LOCAL_IP_ENV_VAR + "). Lets one computer fly two "
             "Tellos, one per adapter; every edge on the computer must then use it. See "
             "docs/two-tellos-one-pc.md",
    )
    parser.add_argument(
        "--tello-ifindex", type=str, default=None,
        help="Windows only, with --tello-local-ip: also pin outgoing Tello commands to this interface "
             "index (InterfaceIndex in Get-NetIPAddress; default: $" + _TELLO_IFINDEX_ENV_VAR + ")",
    )
    parser.add_argument(
        "--drone-id", type=_parse_drone_id, default="drone-1",
        help="Drone id used in fleet messages (default: drone-1)",
    )
    parser.add_argument(
        "--alt-slot", type=_parse_alt_slot, default=_DefaultSlot(100.0),
        help="Altitude slot in cm (default 100); values below 10 are metres, so 1.6 means 1.6 m. It must not "
             "be above the ceiling limit, FLIGHTRISK_CEILING_CM minus FLIGHTRISK_CEILING_MARGIN_CM "
             "(200 - 60 = 140 cm by default), or the edge refuses to start",
    )
    parser.add_argument(
        "--frame-stride", type=_parse_frame_stride, default=None, metavar="N",
        help="Run detection on only every Nth video frame (1 to " + str(MAX_FRAME_STRIDE) + "; default $"
             + _FRAME_STRIDE_ENV_VAR + ", else 1, every frame). Frames are dropped for N times the longer of "
             "the frame time and the time the last frame took to process, but for at most $"
             + _STRIDE_MAX_SKIP_ENV_VAR + " (default " + f"{DEFAULT_STRIDE_MAX_SKIP_S:g}" + " s) after it, so "
             "detection, ReID and encoding run at about 1/N of the rate even when they are slower than the "
             "video, and a slow frame never leaves detection blind for long. Dropped frames are still polled "
             "at the full rate so the newest frame stays available and frozen-stream recovery keeps working. "
             "Applies to --source tello and fake, and only with FLIGHTRISK_OBSTACLE_SENSING=off: while "
             "obstacle sensing is required (the default) a stride above 1 is refused, because it starves the "
             "sensor's person cue. With --stream-video the thumbnails are sent only for the processed frames",
    )
    face = parser.add_mutually_exclusive_group()
    face.add_argument(
        "--appearance-only", dest="appearance_only", action="store_true",
        help="Never load face recognition or send face embeddings (the default for "
             "--source tello and fake unless FLIGHTRISK_APPEARANCE_ONLY=0)",
    )
    face.add_argument(
        "--with-face", dest="appearance_only", action="store_false",
        help="Also compute and send face embeddings (close-range use only; faces are "
             "not resolvable at search altitude)",
    )
    parser.set_defaults(appearance_only=None)
    from flightrisk.vision.profiles import REGISTRY

    parser.add_argument(
        "--detection-profile", choices=REGISTRY.names(), default=None,
        help="Detection profile to start with (default: $FLIGHTRISK_DETECTION_PROFILE, else 'default', "
             "the detector used before profiles existed). The ground can switch it while the drone is "
             "on the ground; see docs/detection-profiles.md",
    )
    return parser


def _build_transport(args: argparse.Namespace, runner: EdgeRunner, token: str | None) -> Any:
    """Construct the ground link; a flying source reconnects with a 2 s backoff cap.

    A flying source with ``--ground-urls`` gets an ``EdgeUplink`` to every
    listed ground; otherwise the single ``--ws-url`` transport, unchanged.
    """
    flying = _drone_kind(args) is not None
    urls = parse_ground_urls(getattr(args, "ground_urls", "")) if flying else ()
    if urls:
        return EdgeUplink(
            urls,
            drone_id=args.drone_id,
            node_id=getattr(args, "node_id", None) or default_node_id(),
            runner=runner,
            token=token,
            max_delay=_FLEET_RECONNECT_MAX_DELAY_S,
            legacy=env_flag(ENV_FLEET_LEGACY),
            inflight_handover=env_flag(ENV_INFLIGHT_HANDOVER),
        )
    if flying:
        return EdgeTransportSync(
            ws_url=args.ws_url, runner=runner, token=token, max_delay=_FLEET_RECONNECT_MAX_DELAY_S,
        )
    return EdgeTransportSync(ws_url=args.ws_url, runner=runner, token=token)


def _mavlink_address(args: argparse.Namespace, environ: Any = None) -> str:
    """``--mavlink-address``, else ``FLIGHTRISK_MAVLINK_ADDRESS``, else ``udpin://0.0.0.0:14540``."""
    env = os.environ if environ is None else environ
    explicit = getattr(args, "mavlink_address", None)
    if explicit:
        return str(explicit)
    return (env.get(_MAVLINK_ADDRESS_ENV_VAR) or "").strip() or _DEFAULT_MAVLINK_ADDRESS


def _failsafe_action(args: argparse.Namespace, environ: Any = None) -> str:
    """``--failsafe-action``, else ``FLIGHTRISK_FAILSAFE_ACTION``, else ``rtl``. Raises ``ValueError``."""
    env = os.environ if environ is None else environ
    explicit = getattr(args, "failsafe_action", None)
    if explicit:
        return str(explicit)
    raw = (env.get(_FAILSAFE_ACTION_ENV_VAR) or "").strip().lower()
    if raw and raw not in ("rtl", "land"):
        raise ValueError(f"{_FAILSAFE_ACTION_ENV_VAR}={raw!r} must be rtl or land")
    return raw or "rtl"


def _build_mavlink_drone(args: argparse.Namespace, log: StructuredLogger) -> Any:
    """The ``--drone mavlink`` controller: a ``MavlinkFleetDrone`` over a MAVSDK link."""
    from flightrisk.drone.mavlink_fleet import MavlinkFleetDrone, MavlinkFleetOptions
    from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink

    address = _mavlink_address(args)
    autopilot = None if getattr(args, "autopilot", "auto") == "auto" else args.autopilot
    options = MavlinkFleetOptions(
        takeoff_alt_m=args.takeoff_alt_m,
        speed_mps=args.speed_mps,
        frame_heading_deg=args.frame_heading_deg,
        failsafe_action=_failsafe_action(args),
        require_failsafes=not args.allow_unverified_failsafes,
        sysid=args.mavlink_sysid if args.mavlink_sysid is not None else 1,
    )
    if args.allow_unverified_failsafes:
        log.warning("edge_mavlink_failsafes_unverified", drone_id=args.drone_id,
                    detail="autopilot failsafe parameters are NOT checked; simulation only")
    log.info("edge_source_mavlink", drone_id=args.drone_id, mavlink_address=address, autopilot=autopilot or "auto",
             failsafe_action=options.failsafe_action, frame_heading_deg=options.frame_heading_deg,
             speed_mps=options.speed_mps)
    return MavlinkFleetDrone(args.drone_id, MavsdkAutopilotLink(address, autopilot=autopilot), options)


class ObstacleWiring:
    """What the edge hands to ``LaneExecutor`` for obstacle sensing (design section 4).

    ``sensor`` and ``sensor_error`` are mutually exclusive, and both are None for
    a drone whose autopilot avoids obstacles by itself. ``holder`` is the frame
    holder that ``drone.on_frame`` feeds, or None when no camera sensor is used.
    ``avoid`` holds the executor's clear votes, re-look and sidestep settings
    (``ObstacleSettings.avoid_kwargs``), or None for the executor's own defaults.
    """

    def __init__(
        self,
        sensor: Any = None,
        sensor_error: str | None = None,
        step_cm: int = OBSTACLE_STEP_CM,
        turn: bool = True,
        holder: LatestFrame | None = None,
        settle_s: float = OBSTACLE_SETTLE_S,
        avoid: dict[str, Any] | None = None,
    ):
        self.sensor = sensor
        self.sensor_error = sensor_error
        self.step_cm = step_cm
        self.turn = turn
        self.holder = holder
        self.settle_s = settle_s
        self.avoid = dict(avoid) if avoid else None

    def executor_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``LaneExecutor``; empty when the executor should fly as before.

        A settle time longer than the default lengthens each sense call's budget by the difference,
        so a longer settle does not eat the time the model has to judge the view.
        """
        kwargs: dict[str, Any] = {}
        if self.sensor is not None:
            kwargs["obstacle_sensor"] = self.sensor
        if self.sensor_error is not None:
            kwargs["obstacle_sensor_error"] = self.sensor_error
        if self.sensor is not None or self.sensor_error is not None:
            kwargs["obstacle_step_cm"] = self.step_cm
            kwargs["obstacle_turn"] = self.turn
            if self.settle_s > OBSTACLE_SETTLE_S:
                kwargs["obstacle_sense_timeout_s"] = sense_timeout_for_settle(self.settle_s)
            if self.settle_s != OBSTACLE_SETTLE_S:
                # The gates' freshness floor uses the sensor's own settle time.
                kwargs["obstacle_settle_s"] = self.settle_s
            if self.avoid:
                kwargs.update(self.avoid)
        return kwargs


def sense_timeout_for_settle(settle_s: float) -> float:
    """The sense budget for a settle time: the default budget, longer by however much the settle is longer."""
    return round(OBSTACLE_SENSE_TIMEOUT_S + max(0.0, settle_s - OBSTACLE_SETTLE_S), 3)


def _obstacle_sensing_mode(environ: Any = None) -> str:
    """``FLIGHTRISK_OBSTACLE_SENSING``: ``required`` (the default) or ``off``; anything else refuses to start."""
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_OBSTACLE_SENSING) or "").strip().lower()
    if not raw:
        return SENSING_ENV_REQUIRED
    if raw not in (SENSING_ENV_REQUIRED, SENSING_ENV_OFF):
        raise SystemExit(
            f"edge_device: {ENV_OBSTACLE_SENSING}={raw!r} is not valid; use "
            f"{SENSING_ENV_REQUIRED!r} (the default) or {SENSING_ENV_OFF!r}. Refusing to start."
        )
    return raw


def _sensing_awaits_resolution(args: argparse.Namespace, environ: Any) -> bool:
    """True when the sensing mode of a flying edge is decided by its resolution, which ``main`` makes later.

    That is a start with a launcher layer (``FLIGHTRISK_LAUNCHER_PARAMS``) and no
    ``FLIGHTRISK_OBSTACLE_SENSING``: the launcher may switch sensing off, confirmed, and the wiring then
    flies it (``edge_resolution.obstacle_arguments``). Every other start is decided by the environment, as
    before.
    """
    if getattr(args, "edge_resolution", None) is not None or _drone_kind(args) is None:
        return False
    if (environ.get(ENV_OBSTACLE_SENSING) or "").strip():
        return False
    from flightrisk.edge_resolution import LAUNCHER_PARAMS_ENV_VAR

    return bool((environ.get(LAUNCHER_PARAMS_ENV_VAR) or "").strip())


def _effective_sensing_mode(args: argparse.Namespace, environ: Any = None) -> str | None:
    """The obstacle sensing mode this edge flies, the one ``_build_obstacle_wiring`` uses. Raises ``SystemExit``.

    Without a resolution it is ``FLIGHTRISK_OBSTACLE_SENSING``, as before. With ``args.edge_resolution`` it is
    the resolved mode (``edge_resolution.resolved_sensing_mode``), so a confirmed launcher off counts. None
    while the mode awaits the resolution (``_sensing_awaits_resolution``); ``_check_resolved_sensing`` then
    checks again once the unit is resolved.
    """
    env = os.environ if environ is None else environ
    mode = _obstacle_sensing_mode(env)
    if _sensing_awaits_resolution(args, env):
        return None
    resolution = getattr(args, "edge_resolution", None)
    if resolution is None:
        return mode
    from flightrisk.edge_resolution import EdgeStartRefused, resolved_sensing_mode

    try:
        mode = resolved_sensing_mode(resolution, mode, env)
    except EdgeStartRefused as exc:
        raise SystemExit(f"edge_device: {exc}") from None
    if mode not in (SENSING_ENV_REQUIRED, SENSING_ENV_OFF):
        raise SystemExit(f"edge_device: the resolved obstacle sensing mode {mode!r} is not valid. Refusing to start.")
    return mode


def _required_sensing_text(args: argparse.Namespace) -> tuple[str, str]:
    """For a refusal: where the required sensing mode comes from, and how to switch sensing off.

    The environment variable as before, unless the resolved mode came from another layer (a launcher's
    ``sensing.mode`` or the profile's default under a launcher layer).
    """
    resolution = getattr(args, "edge_resolution", None)
    where = None
    if resolution is not None:
        from flightrisk.edge_resolution import resolved_elsewhere

        where = resolved_elsewhere(resolution, "sensing.mode")
    if where is None:
        return f"{ENV_OBSTACLE_SENSING}={SENSING_ENV_REQUIRED}, the default", f"set {ENV_OBSTACLE_SENSING}=off"
    return (
        f"sensing.mode={SENSING_ENV_REQUIRED} from {where}",
        f"set sensing.mode=off in the launcher and confirm it there (or set {ENV_OBSTACLE_SENSING}=off)",
    )


def _check_resolved_sensing(args: argparse.Namespace, log: StructuredLogger, environ: Any = None) -> None:
    """The frame-stride and capture-stamp checks again, on the resolved sensing mode. Raises ``SystemExit``.

    ``main`` sets ``args.sensing_awaits_resolution`` when a launcher layer decides the mode
    (``_sensing_awaits_resolution``); its two checks before the resolution then refused nothing, and
    ``_run_flight_edge`` calls this once the unit is resolved, before the drone is built.
    """
    drone_id = getattr(args, "drone_id", None)
    stride = getattr(args, "frame_stride", None) or 1
    if stride > 1:
        try:
            _refuse_stride_while_sensing(args, f"a frame stride of {stride} is set", environ)
        except ValueError as exc:
            log.error("edge_start_refused", drone_id=drone_id, reason=str(exc))
            raise SystemExit(f"edge_device: {exc} Refusing to start.") from None
    problem = _check_capture_stamps(args, environ)
    if problem is not None:
        log.warning(EVENT_NO_CAPTURE_STAMPS, drone_id=drone_id, detail=problem)


# Plain ASCII numbers only: ``int()`` and ``float()`` would also take ``5_0``,
# non-ASCII digits, ``nan`` and ``inf``.
_ASCII_INT_RE = re.compile(r"[0-9]+")
_ASCII_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _obstacle_number(name: str, default: Any, lo: float, hi: float, environ: Any, integer: bool = False) -> Any:
    """A numeric obstacle knob from the environment, in ``lo..hi``; a bad value refuses to start.

    An integer is ASCII digits only; a decimal is an ASCII decimal number (an
    optional sign and exponent), so ``5_0``, non-ASCII digits, ``nan`` and
    ``inf`` are refused.
    """
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    value: Any = None
    if (_ASCII_INT_RE if integer else _ASCII_FLOAT_RE).fullmatch(raw):
        try:
            value = int(raw) if integer else float(raw)
        except (ValueError, OverflowError):
            value = None
    if value is None or not math.isfinite(value) or not lo <= value <= hi:
        kind = "a whole number" if integer else "a number"
        raise SystemExit(f"edge_device: {name}={raw!r} must be {kind} from {lo:g} to {hi:g}. Refusing to start.")
    return value


class ObstacleSettings:
    """Every obstacle setting of the environment, parsed and validated (design section 3.2).

    ``FLIGHTRISK_OBSTACLE_SENSING``, ``_STEP_CM``, ``_TURN``, ``_MIN_SAFE_DEPTH``,
    ``_PERSON_FRACTION``, ``_SETTLE_S``, ``_CLEAR_VOTES``, ``_RELOOK``, ``_SIDESTEP_CM``,
    ``_SIDESTEPS``, ``_AVOID_BUDGET_S``, ``FLIGHTRISK_MAX_DECODE_LAG_S`` (the bound Tello
    decoder reads it itself, ``tello_bound.max_decode_lag_s``) and
    ``FLIGHTRISK_FAKE_OBSTACLE_Y_CM`` are all read
    whatever the mode, the drone kind or the video source, so a malformed value
    refuses to start even when the edge would not use it (under ``off``, on the
    fake drone, or on a MAVLink edge without a camera). The step's range and
    default depend on the drone kind: the field profile (the envelope's move
    limits, default 500 cm) on a MAVLink edge, the room profile (20 to 100 cm,
    default 50) otherwise. ``non_default`` lists ``(variable, value, default)``
    for every setting that differs from its default.

    The edge's start path builds them with ``ObstacleSettings.resolved``, which rebuilds each setting
    of ``edge_resolution.OBSTACLE_ROWS`` from the start resolution (EDGE-2); a direct call keeps
    exactly today's settings.
    """

    def __init__(self, kind: str | None, envelope: SafetyEnvelope, environ: Any):
        self.mode = _obstacle_sensing_mode(environ)
        self.turn = _obstacle_turn(environ)
        if kind == "mavlink":
            step_default = OBSTACLE_STEP_FIELD_CM
            step_lo, step_hi = envelope.limits.min_move_cm, envelope.limits.max_move_cm
        else:
            step_default = OBSTACLE_STEP_CM
            step_lo, step_hi = OBSTACLE_STEP_MIN_CM, OBSTACLE_STEP_MAX_CM
        self.step_cm = _obstacle_number(ENV_OBSTACLE_STEP_CM, step_default, step_lo, step_hi, environ, integer=True)
        self.min_safe_depth = _obstacle_number(
            ENV_OBSTACLE_MIN_SAFE_DEPTH, OBSTACLE_MIN_SAFE_DEPTH, *OBSTACLE_MIN_SAFE_DEPTH_RANGE, environ,
        )
        self.person_fraction = _obstacle_number(
            ENV_OBSTACLE_PERSON_FRACTION, OBSTACLE_PERSON_FRACTION, *OBSTACLE_PERSON_FRACTION_RANGE, environ,
        )
        self.settle_s = _obstacle_number(ENV_OBSTACLE_SETTLE_S, OBSTACLE_SETTLE_S, *OBSTACLE_SETTLE_RANGE_S, environ)
        self.max_decode_lag_s = _obstacle_number(
            ENV_MAX_DECODE_LAG_S, MAX_DECODE_LAG_S, *MAX_DECODE_LAG_RANGE_S, environ,
        )
        self.fake_obstacle_y_cm = _obstacle_number(ENV_FAKE_OBSTACLE_Y_CM, None, -100000.0, 100000.0, environ)
        self.clear_votes = _obstacle_number(
            ENV_OBSTACLE_CLEAR_VOTES, OBSTACLE_CLEAR_VOTES, *OBSTACLE_CLEAR_VOTES_RANGE, environ, integer=True,
        )
        self.relook = _obstacle_number(ENV_OBSTACLE_RELOOK, OBSTACLE_RELOOK, *OBSTACLE_RELOOK_RANGE, environ, integer=True)
        self.sidestep_cm = _obstacle_sidestep_cm(environ)
        self.sidesteps = _obstacle_number(
            ENV_OBSTACLE_SIDESTEPS, OBSTACLE_SIDESTEPS, *OBSTACLE_SIDESTEPS_RANGE, environ, integer=True,
        )
        self.avoid_budget_s = _obstacle_number(
            ENV_OBSTACLE_AVOID_BUDGET_S, OBSTACLE_AVOID_BUDGET_S, *OBSTACLE_AVOID_BUDGET_RANGE_S, environ,
        )
        self._list_non_default(step_default)

    @classmethod
    def resolved(cls, kind: str | None, envelope: SafetyEnvelope, environ: Any, resolution: Any) -> "ObstacleSettings":
        """The settings of ``environ``, rebuilt from ``resolution`` (``args.edge_resolution``) when there is one.

        Without a resolution they are exactly today's. With one, each setting of
        ``edge_resolution.OBSTACLE_ROWS`` comes from ``edge_resolution.obstacle_arguments``. Raises
        ``SystemExit`` on a refusal.
        """
        settings = cls(kind, envelope, environ)
        if resolution is not None:
            settings._apply_resolution(resolution, environ)
            settings._list_non_default(OBSTACLE_STEP_FIELD_CM if kind == "mavlink" else OBSTACLE_STEP_CM)
        return settings

    def _list_non_default(self, step_default: int) -> None:
        self.non_default: list[tuple[str, Any, Any]] = [
            (name, value, default)
            for name, value, default in (
                (ENV_OBSTACLE_SENSING, self.mode, SENSING_ENV_REQUIRED),
                (ENV_OBSTACLE_TURN, self.turn, True),
                (ENV_OBSTACLE_STEP_CM, self.step_cm, step_default),
                (ENV_OBSTACLE_MIN_SAFE_DEPTH, self.min_safe_depth, OBSTACLE_MIN_SAFE_DEPTH),
                (ENV_OBSTACLE_PERSON_FRACTION, self.person_fraction, OBSTACLE_PERSON_FRACTION),
                (ENV_OBSTACLE_SETTLE_S, self.settle_s, OBSTACLE_SETTLE_S),
                (ENV_MAX_DECODE_LAG_S, self.max_decode_lag_s, MAX_DECODE_LAG_S),
                (ENV_FAKE_OBSTACLE_Y_CM, self.fake_obstacle_y_cm, None),
                (ENV_OBSTACLE_CLEAR_VOTES, self.clear_votes, OBSTACLE_CLEAR_VOTES),
                (ENV_OBSTACLE_RELOOK, self.relook, OBSTACLE_RELOOK),
                (ENV_OBSTACLE_SIDESTEP_CM, self.sidestep_cm, OBSTACLE_SIDESTEP_CM),
                (ENV_OBSTACLE_SIDESTEPS, self.sidesteps, OBSTACLE_SIDESTEPS),
                (ENV_OBSTACLE_AVOID_BUDGET_S, self.avoid_budget_s, OBSTACLE_AVOID_BUDGET_S),
            )
            if value != default
        ]

    def _apply_resolution(self, resolution: Any, environ: Any) -> None:
        """Take each setting of ``OBSTACLE_ROWS`` from the resolved values. Raises ``SystemExit``."""
        from flightrisk.edge_resolution import OBSTACLE_ROWS, EdgeStartRefused, obstacle_arguments

        today = {name: getattr(self, name) for name in OBSTACLE_ROWS}
        try:
            arguments = obstacle_arguments(resolution, today, environ)
        except EdgeStartRefused as exc:
            raise SystemExit(f"edge_device: {exc}") from None
        if arguments.get("mode") not in (SENSING_ENV_REQUIRED, SENSING_ENV_OFF):
            raise SystemExit(
                f"edge_device: the resolved obstacle sensing mode {arguments.get('mode')!r} is not valid. "
                "Refusing to start."
            )
        for name, value in arguments.items():
            setattr(self, name, value)

    def avoid_kwargs(self) -> dict[str, Any]:
        """The ``LaneExecutor`` keyword arguments of the clear votes, the re-look and the sidestep."""
        return {
            "obstacle_clear_votes": self.clear_votes,
            "obstacle_relook": self.relook,
            "obstacle_sidestep_cm": self.sidestep_cm,
            "obstacle_sidesteps": self.sidesteps,
            "obstacle_avoid_budget_s": self.avoid_budget_s,
        }

    def warn_non_default(self, log: StructuredLogger, drone_id: str | None) -> None:
        """Log every setting that is not its default once, at warning level, by name and value."""
        for name, value, default in self.non_default:
            log.warning(
                EVENT_SETTING_NON_DEFAULT, drone_id=drone_id, variable=name, value=value, default=default,
                detail=f"{name}={value!r} is not the default ({default!r})",
            )


def _obstacle_sidestep_cm(environ: Any) -> int:
    """``FLIGHTRISK_OBSTACLE_SIDESTEP_CM``: 0 (no sidestep) or a sensed step's range; anything else refuses to start."""
    lo, hi = OBSTACLE_SIDESTEP_RANGE_CM
    value = _obstacle_number(ENV_OBSTACLE_SIDESTEP_CM, OBSTACLE_SIDESTEP_CM, 0, hi, environ, integer=True)
    if value != 0 and value < lo:
        raw = (environ.get(ENV_OBSTACLE_SIDESTEP_CM) or "").strip()
        raise SystemExit(
            f"edge_device: {ENV_OBSTACLE_SIDESTEP_CM}={raw!r} must be 0 (no sidestep) or a whole number "
            f"from {lo:g} to {hi:g}. Refusing to start."
        )
    return value


def _flight_guards(environ: Any, args: argparse.Namespace | None = None) -> FlightGuards:
    """The flight guard settings of the environment (``flightrisk.drone.flight_guards``); a bad value refuses to start.

    ``FLIGHTRISK_TELLO_SETTLE_SAMPLES``, ``_SETTLE_TOLERANCE_CM``,
    ``_SETTLE_TIMEOUT_S``, ``_TAKEOFF_LATE_S``, ``_REFUSAL_RETRIES``,
    ``_REFUSAL_HOLD_S`` and ``_REFUSALS_PER_FLIGHT``, each in its range, and
    ``FLIGHTRISK_HOVER_TEST`` (``1`` takes off and hovers: no move to the
    slot, no lane, nudge, manual step or way back; ``0``, the default, flies
    normally). Read whatever the drone kind, so a malformed value always
    refuses to start. Once the edge has resolved its unit (``args.edge_resolution``), the settings
    are built from the resolved values (``_resolved_flight_guards``).
    """
    raw = (environ.get(ENV_HOVER_TEST) or "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        hover_test = False
    elif raw in ("1", "true", "yes", "on"):
        hover_test = True
    else:
        raise SystemExit(f"edge_device: {ENV_HOVER_TEST}={raw!r} must be 0 or 1. Refusing to start.")
    guards = FlightGuards(
        settle_samples=_obstacle_number(
            ENV_SETTLE_SAMPLES, DEFAULT_SETTLE_SAMPLES, *SETTLE_SAMPLES_RANGE, environ, integer=True
        ),
        settle_tolerance_cm=_obstacle_number(
            ENV_SETTLE_TOLERANCE_CM, DEFAULT_SETTLE_TOLERANCE_CM, *SETTLE_TOLERANCE_RANGE_CM, environ
        ),
        settle_timeout_s=_obstacle_number(ENV_SETTLE_TIMEOUT_S, DEFAULT_SETTLE_TIMEOUT_S, *SETTLE_TIMEOUT_RANGE_S, environ),
        takeoff_late_s=_obstacle_number(ENV_TAKEOFF_LATE_S, DEFAULT_TAKEOFF_LATE_S, *TAKEOFF_LATE_RANGE_S, environ),
        refusal_retries=_obstacle_number(
            ENV_REFUSAL_RETRIES, DEFAULT_REFUSAL_RETRIES, *REFUSAL_RETRIES_RANGE, environ, integer=True
        ),
        refusal_hold_s=_obstacle_number(ENV_REFUSAL_HOLD_S, DEFAULT_REFUSAL_HOLD_S, *REFUSAL_HOLD_RANGE_S, environ),
        refusals_per_flight=_obstacle_number(
            ENV_REFUSALS_PER_FLIGHT, DEFAULT_REFUSALS_PER_FLIGHT, *REFUSALS_PER_FLIGHT_RANGE, environ, integer=True
        ),
        hover_test=hover_test,
    )
    return guards if args is None else _resolved_flight_guards(args, guards)


def _resolved_flight_guards(args: argparse.Namespace, guards: FlightGuards) -> FlightGuards:
    """``guards`` rebuilt from the resolved values in ``args.edge_resolution`` (EDGE-2). Raises ``SystemExit``.

    Without a resolution, or when every resolved value equals today's, it is ``guards`` itself.
    ``edge_resolution.fact_arguments`` says which value comes from where; the hover-only test keeps
    today's reader.
    """
    resolution = getattr(args, "edge_resolution", None)
    if resolution is None:
        return guards
    from flightrisk.edge_resolution import GUARD_ROWS, fact_arguments

    defaults = FlightGuards()
    today = {name: getattr(guards, name) for name in GUARD_ROWS}
    arguments = fact_arguments(resolution, GUARD_ROWS, today, {name: getattr(defaults, name) for name in GUARD_ROWS})
    if arguments == today:
        return guards
    try:
        return dataclasses.replace(guards, **arguments)
    except ValueError as exc:
        raise SystemExit(f"edge_device: the resolved flight guard settings are invalid: {exc}. Refusing to start.") from None


def _warn_flight_guards(guards: FlightGuards, log: StructuredLogger, drone_id: str | None) -> None:
    """Log every flight guard setting that is not its default once, and the hover-only test loudly."""
    defaults = FlightGuards()
    for name, field in (
        (ENV_SETTLE_SAMPLES, "settle_samples"),
        (ENV_SETTLE_TOLERANCE_CM, "settle_tolerance_cm"),
        (ENV_SETTLE_TIMEOUT_S, "settle_timeout_s"),
        (ENV_TAKEOFF_LATE_S, "takeoff_late_s"),
        (ENV_REFUSAL_RETRIES, "refusal_retries"),
        (ENV_REFUSAL_HOLD_S, "refusal_hold_s"),
        (ENV_REFUSALS_PER_FLIGHT, "refusals_per_flight"),
    ):
        value, default = getattr(guards, field), getattr(defaults, field)
        if value != default:
            log.warning(
                EVENT_GUARD_SETTING_NON_DEFAULT, drone_id=drone_id, variable=name, value=value, default=default,
                detail=f"{name}={value!r} is not the default ({default!r})",
            )
    if guards.hover_test:
        log.warning(
            "edge_hover_test_mode", drone_id=drone_id, variable=ENV_HOVER_TEST,
            detail=(f"{ENV_HOVER_TEST}=1: this drone takes off and hovers. It does not move to its altitude slot "
                    "and flies no lane, nudge, manual step or resume_search way back until the edge is restarted "
                    "without it."),
        )


def _obstacle_turn(environ: Any) -> bool:
    """``FLIGHTRISK_OBSTACLE_TURN``: 1 (the default) turns nose first; 0 is the bring-up kill switch."""
    raw = (environ.get(ENV_OBSTACLE_TURN) or "").strip().lower()
    if raw in ("", "1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise SystemExit(f"edge_device: {ENV_OBSTACLE_TURN}={raw!r} must be 0 or 1. Refusing to start.")


def _sensor_copy(channel_order: str) -> Callable[[Any], Any]:
    """The holder's own copy of a frame from a source of ``channel_order``, in the sensor's channel order.

    ``to_sensor_frame`` makes a new array, so later changes to the source array cannot reach the copy,
    and converts the Tello decoders' RGB to the BGR the sensor judges, so a frame in flight and the
    same frame replayed from a recording reach the depth model as the same input.
    """
    to_sensor_frame(None, channel_order)  # refuses an unknown order now, not at the first frame

    def _copy(frame: Any) -> Any:
        return to_sensor_frame(frame, channel_order)

    return _copy


def _decoder_frame_stamp(drone: Any, frames: EdgeFrames | None = None) -> Callable[[Any], FrameStamp | None]:
    """``stamp(frame)``: the drone's video decoder's ``FrameStamp`` for ``frame``, or None.

    Reads the drone's current decoder (``drone.tello.background_frame_read``) on every call, because
    a stream restart replaces it. A decoder that stamps its frames (``BoundFrameRead.frame_info``,
    the decoder of an edge started with ``--tello-local-ip``) gives the frame's capture time and
    whether it is stale; a frame such a decoder does not know (the black frame before the first
    decode, or one long replaced) is stale with ``capture time unknown``. A decoder that cannot
    stamp (djitellopy's own) and a drone without one give None, and the frame counts from its
    arrival, as before. With ``frames`` (the edge's ``EdgeFrames``), a frame it converted is looked
    up as the decoder's frame it was converted from, so the frame loop's BGR frames keep their
    stamps (``EdgeRunner.frame_stamp``).
    """

    def _stamp(frame: Any) -> FrameStamp | None:
        reader = getattr(getattr(drone, "tello", None), "background_frame_read", None)
        info = getattr(reader, "frame_info", None)
        if not callable(info):
            return None
        if frames is not None:
            frame = frames.source_of(frame)
        stamp = info(frame)
        return stamp if isinstance(stamp, FrameStamp) else FrameStamp(stale_cause=STALE_CAPTURE_UNKNOWN)

    return _stamp


def _copying_frame_feed(
    holder: LatestFrame,
    channel_order: str = CHANNEL_ORDER_BGR,
    stamp: Callable[[Any], FrameStamp | None] | None = None,
    frames: EdgeFrames | None = None,
) -> Callable[[Any], None]:
    """A frame callback that gives the holder its own copy of each frame object it has not had before.

    ``channel_order`` is the order the source delivers (``CHANNEL_ORDER_RGB`` for a Tello's decoder,
    ``CHANNEL_ORDER_BGR`` for an OpenCV capture); the copy is in the sensor's order
    (``_sensor_copy``). ``stamp``, when given, returns the decoder's ``FrameStamp`` for the frame
    (``_decoder_frame_stamp``). The holder compares the source object itself, before copying, with
    the last one that any of its feeds offered (``LatestFrame.offer``). This feed and
    ``DecodedFramePoller`` therefore share one record: when ``TelloController.get_frame`` hands the
    same array to ``on_frame`` on every poll of a stalled decoder, the holder keeps the frame's
    first arrival time and the gate finds the view stale. With ``frames`` (the edge's
    ``EdgeFrames``, which then decides the channel order) the copy is its shared conversion.
    """
    copy = frames.sensor_copy if frames is not None else _sensor_copy(channel_order)

    def _feed(frame: Any) -> None:
        holder.offer(frame, copy, stamp=stamp(frame) if stamp is not None else None)

    return _feed


def _build_obstacle_wiring(
    args: argparse.Namespace,
    log: StructuredLogger,
    drone: Any,
    runner: Any,
    envelope: SafetyEnvelope,
    environ: Any = None,
    frames: EdgeFrames | None = None,
) -> ObstacleWiring:
    """Build the obstacle sensor for this drone kind and environment (design sections 3.2, 4, 5 and 7.1).

    Call it after the drone is built and before ``drone.connect()``.

    * ``FLIGHTRISK_OBSTACLE_SENSING=off`` flies unsensed, loudly: a ``NullSensor``.
    * ``tello``: a ``ForwardCameraSensor`` on a ``LatestFrame`` that ``drone.on_frame`` feeds with every
      new frame object, independent of the detector's frame stride (a stalled decoder's frame is not fed
      again; see ``_copying_frame_feed``), converted from the decoder's RGB to the sensor's BGR and
      stamped with the decoder's capture time when it gives one (``_decoder_frame_stamp``). The settle
      time is ``FLIGHTRISK_OBSTACLE_SETTLE_S``. When the sensor cannot be built (torch, the
      MiDaS weights, the first inference) the error is passed to the executor, which refuses to take off.
      ``frames`` is the edge's shared conversion (``EdgeFrames``), so the sensor's frame is the one the
      frame loop gets and nothing converts it twice.
    * ``fake``: a ``FakeObstacleSensor``, blocked from ``FLIGHTRISK_FAKE_OBSTACLE_Y_CM`` along the
      plan-frame y axis when that is set and never otherwise. It loads no model.
    * ``mavlink``: with ``--source camera`` or ``url``, the same camera sensor in steps of 500 cm (the field
      profile), fed from the capture source; with another source no sensor, and the drone's own preflight
      needs the autopilot's avoidance (see ``_tell_mavlink_drone``). ``off`` is the override.
    """
    env = os.environ if environ is None else environ
    kind = _drone_kind(args)
    drone_id = getattr(args, "drone_id", None)
    resolution = getattr(args, "edge_resolution", None)
    settings = ObstacleSettings.resolved(kind, envelope, env, resolution)
    settings.warn_non_default(log, drone_id)
    mode, turn, step_cm, settle_s = settings.mode, settings.turn, settings.step_cm, settings.settle_s
    if mode == SENSING_ENV_OFF:
        from flightrisk.edge_resolution import resolved_elsewhere

        # A launcher off reaches here only when the launcher confirmed it (resolver.LAUNCHER_NEEDS_CONFIRMATION).
        where = resolved_elsewhere(resolution, "sensing.mode") if resolution is not None else None
        said = f"sensing.mode=off from {where}" if where else f"{ENV_OBSTACLE_SENSING}=off"
        log.warning(
            "edge_obstacle_sensing_off", drone_id=drone_id,
            detail=f"{said}: this drone flies its lanes with NO obstacle sensing",
        )
        return ObstacleWiring(sensor=NullSensor(), turn=turn)
    if kind == "mavlink" and getattr(args, "source", None) not in ("camera", "url"):
        # No companion camera to look through: the autopilot's own avoidance or the override.
        log.info("edge_obstacle_sensing_autopilot", drone_id=drone_id)
        return ObstacleWiring(turn=turn)
    if kind == "fake":
        y_cm = settings.fake_obstacle_y_cm
        zones: list[BlockedZone] = []
        if y_cm is not None:
            zones.append(BlockedZone(heading_deg=0.0, y_min=y_cm))
            log.info("edge_obstacle_fake", drone_id=drone_id, blocked_from_y_cm=y_cm)
        return ObstacleWiring(
            sensor=FakeObstacleSensor(envelope.pose_snapshot, zones), step_cm=step_cm, turn=turn,
            avoid=settings.avoid_kwargs(),
        )

    from flightrisk.edge_resolution import forward_sensing_on

    if resolution is not None and not forward_sensing_on(resolution):
        # SEAM-4: no camera sensor unless forward obstacle sensing is available and on. On a MAVLink
        # unit required sensing then needs the autopilot's avoidance at takeoff (FEA-7): the drone
        # refuses a takeoff until ONBOARD_AVOIDANCE is present (``_tell_mavlink_drone``). The start was
        # refused already when the unit can never declare it (``static_feature_gates``). Any other
        # drone has no such check, so the executor gets the error and refuses to take off.
        if kind == "mavlink":
            log.warning(
                "edge_obstacle_sensing_unavailable", drone_id=drone_id,
                detail="forward obstacle sensing is not available on this unit; a takeoff needs the "
                       "autopilot's own obstacle avoidance",
            )
            return ObstacleWiring(turn=turn)
        error = "forward obstacle sensing is not available on this unit"
        log.error("edge_obstacle_sensor_unavailable", drone_id=drone_id, error=error)
        return ObstacleWiring(sensor_error=error, step_cm=step_cm, turn=turn, settle_s=settle_s,
                              avoid=settings.avoid_kwargs())
    min_safe_depth, person_fraction = settings.min_safe_depth, settings.person_fraction
    holder = LatestFrame(time.monotonic)
    try:
        if kind != "mavlink":
            # A MAVLink edge feeds the holder from its capture source instead (``_flight_frame_source``).
            feed_sensor_holder(drone, holder, frames)
        from flightrisk.drone import obstacle_camera

        sensor = obstacle_camera.ForwardCameraSensor(
            holder, runner, drone_id=drone_id, min_safe_depth=min_safe_depth, person_fraction=person_fraction,
            settle_s=settle_s, clock=time.monotonic,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.error("edge_obstacle_sensor_unavailable", drone_id=drone_id, error=error)
        return ObstacleWiring(sensor_error=error, step_cm=step_cm, turn=turn, holder=holder, settle_s=settle_s,
                              avoid=settings.avoid_kwargs())
    return ObstacleWiring(sensor=sensor, step_cm=step_cm, turn=turn, holder=holder, settle_s=settle_s,
                          avoid=settings.avoid_kwargs())


def feed_sensor_holder(drone: Any, holder: LatestFrame, frames: EdgeFrames | None = None) -> None:
    """Feed ``holder`` from a drone's own video the way a Tello edge feeds its obstacle sensor.

    Registers the copy feed on ``drone.on_frame``: each new frame object, converted from the
    decoder's RGB to the sensor's BGR and stamped with the decoder's ``FrameStamp`` when it gives one.
    ``DecodedFramePoller.for_drone`` adds the second feed. ``frames`` is the conversion shared with
    the other consumers (``EdgeFrames``; by default one for the drone's ``frame_channel_order``).
    The edge and the calibration recorder (``scripts/obstacle_record.py``) both use this, so a
    recording holds exactly the frames the sensor judges. Raises ``RuntimeError`` for a drone
    without an ``on_frame`` hook.
    """
    on_frame = getattr(drone, "on_frame", None)
    if not callable(on_frame):
        raise RuntimeError("the drone provides no on_frame hook for the camera sensor")
    if frames is None:
        frames = EdgeFrames.for_drone(drone)
    on_frame(_copying_frame_feed(holder, stamp=_decoder_frame_stamp(drone), frames=frames))


def _obstacle_frame_poller(
    kind: str | None, drone: Any, wiring: ObstacleWiring, log: StructuredLogger | None = None,
    frames: EdgeFrames | None = None,
) -> DecodedFramePoller | None:
    """The poller that feeds a camera sensor's holder straight from the drone's video decoder, or None.

    Only a drone flown through its own video (not the MAVLink capture source) with a working camera sensor
    and a djitellopy client to read gets one, so a slow detection in the frame loop cannot starve the
    holder. The caller starts and stops it.
    """
    if kind == "mavlink" or wiring.sensor is None or isinstance(wiring.sensor, NullSensor):
        return None
    return DecodedFramePoller.for_drone(drone, wiring.holder, log, frames=frames)


def _tell_mavlink_drone(drone: Any, wiring: ObstacleWiring) -> str | None:
    """Tell a ``MavlinkFleetDrone`` how this edge senses obstacles; call after ``connect()``, before any takeoff.

    ``off`` for the override, ``stepped`` (or ``forward_only``) when the edge has a camera sensor, or
    failed to build one and so refuses to fly. With no sensor the drone needs the autopilot's avoidance:
    ``autopilot`` when ``ONBOARD_AVOIDANCE`` is declared now, otherwise ``none``. Both make the drone's
    preflight refuse a takeoff until ``avoidance_status()`` is empty, so the capability, which can still
    appear after the first parameter read and drop later, is checked by the drone and the executor and not
    decided here for good. Returns the mode, or None when the drone does not take one.
    """
    setter = getattr(drone, "set_obstacle_sensing", None)
    if not callable(setter):
        return None
    from flightrisk.drone.controller import Capability, capabilities_of
    from flightrisk.drone.mavlink_fleet import SENSING_NONE

    if isinstance(wiring.sensor, NullSensor):
        mode = SENSING_OFF
    elif wiring.sensor is not None or wiring.sensor_error is not None:
        mode = SENSING_STEPPED if wiring.turn else SENSING_FORWARD_ONLY
    elif capabilities_of(drone).supports(Capability.ONBOARD_AVOIDANCE):
        mode = SENSING_AUTOPILOT
    else:
        mode = SENSING_NONE
    setter(mode)
    return mode


def _build_drone(args: argparse.Namespace, log: StructuredLogger) -> Any:
    """Construct the drone controller for a flying source."""
    if _drone_kind(args) == "mavlink":
        return _build_mavlink_drone(args, log)
    if args.source == "fake":
        from flightrisk.drone.tello import FakeTelloController

        log.info("edge_source_fake_tello", drone_id=args.drone_id)
        return FakeTelloController(name=args.drone_id, speed_cm_s=_FAKE_SPEED_CM_S)

    from flightrisk.drone.tello import TelloController

    local_ip = getattr(args, "tello_local_ip", None)
    if local_ip is None:
        log.info("edge_source_tello", drone_id=args.drone_id, tello_host=args.tello_host)
        return TelloController(name=args.drone_id, host=args.tello_host)

    from flightrisk.drone.tello_bound import TelloBindingError

    ifindex = getattr(args, "tello_ifindex", None)
    log.info("edge_source_tello", drone_id=args.drone_id, tello_host=args.tello_host,
             tello_local_ip=local_ip, tello_ifindex=ifindex)
    try:
        return TelloController(name=args.drone_id, host=args.tello_host, local_ip=local_ip, ifindex=ifindex)
    except TelloBindingError as exc:
        log.error("edge_tello_binding_refused", drone_id=args.drone_id, tello_local_ip=local_ip, error=str(exc))
        raise SystemExit(f"edge_device: {exc} Refusing to start.") from exc


def _resolve_tello_binding(args: argparse.Namespace, environ: Any = None) -> tuple[str | None, int | None]:
    """The checked ``--tello-local-ip`` and ``--tello-ifindex``, each falling back to its environment variable.

    Both are None unless ``--source tello``; the environment variables are
    then ignored, and giving either flag is an error. The interface index
    needs a local IP. Raises ``ValueError`` with an operator-facing message.
    """
    env = os.environ if environ is None else environ
    flag_ip = getattr(args, "tello_local_ip", None)
    flag_ifindex = getattr(args, "tello_ifindex", None)
    if args.source != "tello":
        if flag_ip is not None or flag_ifindex is not None:
            raise ValueError("--tello-local-ip and --tello-ifindex only apply to --source tello")
        return None, None
    raw_ip = flag_ip if flag_ip is not None else (env.get(_TELLO_LOCAL_IP_ENV_VAR) or "").strip() or None
    raw_ifindex = flag_ifindex if flag_ifindex is not None else (env.get(_TELLO_IFINDEX_ENV_VAR) or "").strip() or None
    if raw_ip is None and raw_ifindex is None:
        return None, None

    from flightrisk.drone.tello_bound import parse_ifindex, parse_local_ip

    if raw_ip is None:
        raise ValueError("--tello-ifindex needs --tello-local-ip (the IP of the same adapter)")
    local_ip = parse_local_ip(raw_ip)
    ifindex = parse_ifindex(raw_ifindex) if raw_ifindex is not None else None
    return local_ip, ifindex


def _check_tello_binding(args: argparse.Namespace) -> None:
    """Refuse a ``--tello-local-ip`` that cannot work before the models load. Raises ``SystemExit``.

    The ports themselves are bound, and a port in use is reported, when the
    controller is built.
    """
    if getattr(args, "tello_local_ip", None) is None:
        return
    from flightrisk.config import get_config
    from flightrisk.drone.tello_bound import (
        TelloBinding,
        TelloBindingError,
        check_binding,
    )

    host = args.tello_host or get_config().drone.tello_default_host
    try:
        check_binding(TelloBinding(local_ip=args.tello_local_ip, ifindex=args.tello_ifindex), host)
    except TelloBindingError as exc:
        raise SystemExit(f"edge_device: {exc} Refusing to start.") from exc


#: Logged at startup by a Tello edge whose video decoder gives no capture stamps (sensing ``off`` only).
EVENT_NO_CAPTURE_STAMPS = "edge_obstacle_no_capture_stamps"


def _no_capture_stamps(args: argparse.Namespace) -> str | None:
    """Why this edge's frames carry no capture stamps, or None when they do or need none.

    Only a Tello edge needs them: its obstacle sensor judges a frame by its capture time. The
    decoder of ``--tello-local-ip`` (``tello_bound.BoundFrameRead``) stamps every frame
    (``frame_info``); without it ``TelloController`` uses djitellopy's own decoder, which cannot,
    so a frame counts from its arrival and a decoder that falls behind the video is not seen
    (``_decoder_frame_stamp``). A fake or MAVLink edge has no such decoder.
    """
    if _drone_kind(args) != "tello" or getattr(args, "tello_local_ip", None) is not None:
        return None
    return (
        "this Tello edge has no --tello-local-ip, so it uses djitellopy's video decoder, which gives its "
        "frames no capture stamps: a decoder that falls behind the video would let the obstacle sensor "
        "judge a view from before the last move as fresh"
    )


def _check_capture_stamps(args: argparse.Namespace, environ: Any = None) -> str | None:
    """Refuse a Tello edge whose required obstacle sensor would get no capture stamps. Raises ``SystemExit``.

    Returns the problem to log as a warning when sensing is ``off``, else None. Called at startup,
    before any model loads or anything connects. The mode is the one the obstacle wiring flies
    (``_effective_sensing_mode``); while it awaits the resolution this returns None, and
    ``_check_resolved_sensing`` checks again once the unit is resolved.
    """
    problem = _no_capture_stamps(args)
    if problem is None:
        return None
    mode = _effective_sensing_mode(args, environ)
    if mode is None:
        return None
    if mode == SENSING_ENV_REQUIRED:
        said, remedy = _required_sensing_text(args)
        required = "obstacle sensing is required" + ("" if said.startswith(ENV_OBSTACLE_SENSING) else f" ({said})")
        raise SystemExit(
            f"edge_device: {required}, but {problem}. Start the edge with "
            f"--tello-local-ip <the IP of the Wi-Fi adapter on this Tello's network> (or {_TELLO_LOCAL_IP_ENV_VAR}; "
            f"one drone works the same way, docs/two-tellos-one-pc.md), or {remedy} to fly "
            "with no obstacle sensing. Refusing to start."
        )
    return problem


def _field_value_m(args: argparse.Namespace, name: str, env: Any) -> float | None:
    """A metre setting of the outdoor envelope: the flag, else its environment variable, else None."""
    value = getattr(args, name, None)
    if value is not None:
        return float(value)
    raw = (env.get(_FIELD_ENV[name]) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{_FIELD_ENV[name]} must be a number of metres, got {raw!r}") from exc


def _field_envelope(args: argparse.Namespace, environ: Any = None) -> SafetyEnvelope:
    """The outdoor envelope of a ``--drone mavlink`` edge (``FIELD_LIMITS``). Raises ``SystemExit``.

    Metres from the ``--*-m`` flags or the ``FLIGHTRISK_FIELD_*_M`` variables;
    the battery and link-loss settings come from the same variables as the
    indoor edge. Anything invalid refuses the start with the reason. Once the edge has resolved its
    unit, the envelope is built from the resolved values (``_resolved_envelope``).
    """
    env = os.environ if environ is None else environ

    def _cm(name: str) -> float | None:
        value = _field_value_m(args, name, env)
        return None if value is None else round(value * 100.0, 1)

    def _read(name: str, default: float) -> float:
        raw = (env.get(name) or "").strip()
        return float(raw) if raw else default

    try:
        envelope = SafetyEnvelope.for_field(
            alt_slot_cm=_cm("alt_slot_m"),
            geofence_cm=_cm("geofence_m"),
            ceiling_cm=_cm("ceiling_m"),
            ceiling_margin_cm=_cm("ceiling_margin_m"),
            min_takeoff_battery=min_takeoff_battery_from_env(env),
            link_loss_land_s=_read(LINK_LOSS_LAND_S_ENV, DEFAULT_LINK_LOSS_LAND_S),
            link_loss_hold_s=(
                float(args.link_loss_hold_s) if getattr(args, "link_loss_hold_s", None) is not None
                else _read(_LINK_LOSS_HOLD_ENV_VAR, DEFAULT_FIELD_LINK_LOSS_HOLD_S)
            ),
        )
    except ValueError as exc:
        raise SystemExit(
            f"edge_device: {exc}. Refusing to start: check --alt-slot-m, --ceiling-m, --ceiling-margin-m and "
            "--geofence-m (or the FLIGHTRISK_FIELD_*_M variables; docs/open-drone-support.md)."
        ) from exc
    return _resolved_envelope(args, envelope)


def _resolved_envelope(args: argparse.Namespace, envelope: SafetyEnvelope) -> SafetyEnvelope:
    """``envelope`` rebuilt from the resolved values in ``args.edge_resolution`` (EDGE-2). Raises ``SystemExit``.

    Without a resolution (an envelope built before or without the edge's start resolution), or when
    every resolved value equals today's, it is ``envelope`` itself, today's. ``envelope_arguments``
    says which value comes from where. The envelope keeps today's limits for its scale, except the
    field ceiling and slot upper bounds, which ``envelope_limits`` takes from the unit's altitude limit
    (item 53): the airframe maximum with the cap off and an accepted acknowledgement, or a known airframe
    maximum below the cap with the cap on.
    """
    resolution = getattr(args, "edge_resolution", None)
    if resolution is None:
        return envelope
    from flightrisk.edge_resolution import (
        EdgeStartRefused,
        envelope_arguments,
        envelope_limits,
        todays_envelope_arguments,
    )

    today = todays_envelope_arguments(envelope)
    try:
        arguments = envelope_arguments(resolution, today)
        limits = envelope_limits(resolution, envelope.limits)
    except EdgeStartRefused as exc:
        raise SystemExit(f"edge_device: {exc}") from None
    if arguments == today and limits is envelope.limits:
        return envelope
    try:
        return SafetyEnvelope(**arguments, limits=limits)
    except ValueError as exc:
        raise SystemExit(f"edge_device: the resolved envelope is invalid: {exc}. Refusing to start.") from None


def _temp_limits(
    log: StructuredLogger | None = None, drone_id: str | None = None, environ: Any = None,
    args: argparse.Namespace | None = None,
) -> TempLimits:
    """The takeoff temperature check's levels (``FLIGHTRISK_TELLO_TEMP_WARN_C``, ``..._REFUSE_C``).

    Read and validated at every flying edge's start, whatever the drone kind (only a Tello's
    temperature is checked), like the obstacle settings: a malformed value, or a refusal level
    below the warn level, refuses to start with the reason. A level that differs from its
    default is logged once as ``edge_temp_setting_non_default``. Once the edge has resolved its unit
    (``args.edge_resolution``), the levels are built from the resolved values (``_resolved_temp_limits``).
    """
    env = os.environ if environ is None else environ
    try:
        limits = TempLimits.from_env(env)
    except ValueError as exc:
        raise SystemExit(f"edge_device: {exc}. Refusing to start (docs/fleet-setup.md, \"Takeoff temperature check\").") from None
    if args is not None:
        limits = _resolved_temp_limits(args, limits)
    if log is not None:
        for field, name, value, default in (
            ("warn_c", ENV_TELLO_TEMP_WARN_C, limits.warn_c, DEFAULT_TEMP_WARN_C),
            ("refuse_c", ENV_TELLO_TEMP_REFUSE_C, limits.refuse_c, DEFAULT_TEMP_REFUSE_C),
        ):
            if value != default:
                name, where = _temp_level_source(args, field, name)
                log.warning(
                    "edge_temp_setting_non_default", drone_id=drone_id, variable=name,
                    value="off" if value is None else value, default=default,
                    detail=f"{name}={'off' if value is None else value!r}{where} is not the default ({default!r})",
                )
    return limits


def _temp_level_source(args: argparse.Namespace | None, field: str, variable: str) -> tuple[str, str]:
    """The name of the temperature level ``field`` and where it came from, for its non-default log line.

    A level from the environment, or one read without a resolution, is named by ``variable`` alone. A
    level the profile, the params file or the launcher set is named by its row and that source, for
    example ``("tello.temp_warn_c", " from launcher")``.
    """
    resolution = getattr(args, "edge_resolution", None) if args is not None else None
    if resolution is None:
        return variable, ""
    from flightrisk.edge_resolution import TEMP_ROWS, resolved_elsewhere

    row = TEMP_ROWS[field]
    where = resolved_elsewhere(resolution, row)
    if where is None:
        return variable, ""
    return row, f" from {where}"


def _resolved_temp_limits(args: argparse.Namespace, limits: TempLimits) -> TempLimits:
    """``limits`` rebuilt from the resolved values in ``args.edge_resolution`` (EDGE-2). Raises ``SystemExit``.

    Without a resolution, or when every resolved value equals today's, it is ``limits`` itself.
    ``edge_resolution.fact_arguments`` says which value comes from where. Both levels are facts of the
    drone, and the refusal switch is only read here: nothing new is confirmed or reported for it.
    """
    resolution = getattr(args, "edge_resolution", None)
    if resolution is None:
        return limits
    from flightrisk.edge_resolution import TEMP_ROWS, fact_arguments

    defaults = TempLimits()
    today = {name: getattr(limits, name) for name in TEMP_ROWS}
    arguments = fact_arguments(resolution, TEMP_ROWS, today, {name: getattr(defaults, name) for name in TEMP_ROWS})
    if arguments == today:
        return limits
    try:
        return TempLimits(**arguments)
    except ValueError as exc:
        raise SystemExit(f"edge_device: the resolved temperature levels are invalid: {exc}. Refusing to start.") from None


def _startup_envelope(args: argparse.Namespace, environ: Any = None) -> SafetyEnvelope:
    """The flying edge's safety envelope from ``--alt-slot`` and the environment.

    Raises ``SystemExit`` with an operator-facing message when the limits
    are invalid, in particular when the altitude slot is above the ceiling
    limit (``FLIGHTRISK_CEILING_CM`` minus ``FLIGHTRISK_CEILING_MARGIN_CM``,
    200 - 60 = 140 cm by default): the edge refuses to start rather than
    clamp the slot.

    Once the edge has resolved its unit (``args.edge_resolution``), the envelope is built from the
    resolved values; before that, and wherever no resolution exists, it is today's.
    """
    if _drone_kind(args) == "mavlink":
        return _field_envelope(args, environ)
    try:
        envelope = SafetyEnvelope.from_env(alt_slot_cm=args.alt_slot, environ=environ)
    except ValueError as exc:
        raise SystemExit(
            f"edge_device: {exc}. Refusing to start: lower --alt-slot (ALT_CM), or set "
            f"{CEILING_CM_ENV} / {CEILING_MARGIN_CM_ENV} for this room (docs/ceiling-and-altitude.md)."
        ) from exc
    return _resolved_envelope(args, envelope)


def _run_flight_edge(
    args: argparse.Namespace,
    log: StructuredLogger,
    stop_event: threading.Event | None = None,
    envelope: SafetyEnvelope | None = None,
    argv: Sequence[str] | None = None,
    parser: argparse.ArgumentParser | None = None,
) -> None:
    """Run a flying edge: drone, lane executor, safety controls and the edge loop.

    Setting ``stop_event`` ends the edge loop; the drone lands first. ``argv`` and ``parser`` are the
    command line and parser ``main`` read, which give the resolution its command-line layer.
    """
    start_envelope = envelope is None
    if start_envelope:
        envelope = _startup_envelope(args)
    _temp_limits(None, getattr(args, "drone_id", None))
    _flight_guards(os.environ)
    # After today's own start checks, so they keep refusing with today's messages, and before the
    # drone is built or contacted (EDGE-1).
    _resolve_edge_start(args, log, argv=argv, parser=parser)
    if getattr(args, "sensing_awaits_resolution", False):
        # A launcher layer decides the sensing mode: the frame-stride and capture-stamp checks of ``main``
        # waited for the resolution.
        _check_resolved_sensing(args, log)
    if start_envelope:
        # Built again from the resolved values (EDGE-2). An envelope the caller passed is never rebuilt
        # from the resolution and wins over it; only tests pass one, main never does.
        envelope = _startup_envelope(args)
    # The temperature levels and the flight guards from the resolved values (EDGE-2); a level that is
    # not its default is logged here, once.
    temp_limits = _temp_limits(log, getattr(args, "drone_id", None), args=args)
    guards = _flight_guards(os.environ, args)
    kind = _drone_kind(args)
    holder: dict[str, Any] = {}
    if kind == "mavlink":
        pose_provider = _autopilot_pose_provider(envelope, holder)
    else:
        pose_provider = _envelope_pose_provider(envelope)
    runner = _build_runner(args, log, pose_provider=pose_provider)
    drone = _build_drone(args, log)
    holder["drone"] = drone
    bind_envelope = getattr(drone, "bind_envelope", None)
    if callable(bind_envelope):
        # The autopilot's fence, altitude fence and return altitude are
        # checked against the live geofence, ceiling and slot.
        bind_envelope(envelope)

    # One BGR conversion per frame of the drone's own video, shared by detection, ReID, the JPEGs and
    # the obstacle sensor (``EdgeFrames``). A MAVLink edge's capture source is BGR already.
    frames = EdgeFrames.for_drone(drone) if kind != "mavlink" else None
    obstacle = _build_obstacle_wiring(args, log, drone, runner, envelope, frames=frames)
    if kind != "mavlink" and hasattr(runner, "frame_stamp"):
        # Detections are stamped with their frame's capture time, and a frame the decoder marks stale
        # never feeds the person cue (``EdgeRunner.frame_stamp``).
        runner.frame_stamp = _decoder_frame_stamp(drone, frames)

    token = os.environ.get(_TOKEN_ENV_VAR)
    transport = _build_transport(args, runner, token)
    flight = LaneExecutor(
        drone, envelope, args.drone_id, send=_with_profile_status(_make_sender(transport, log), runner), logger=log,
        temp_limits=temp_limits, flight_guards=guards, **obstacle.executor_kwargs(),
    )
    _warn_flight_guards(guards, log, args.drone_id)
    bind_executor = getattr(transport, "bind_executor", None)
    if callable(bind_executor):
        bind_executor(flight)
    _bind_profile_switcher(runner, transport, log, flight)
    runner.edge_query = edge_query.for_link(
        args.edge_resolution, args.drone_id, transport, _make_sender(transport, log), log=log,
    )
    if stop_event is None:
        stop_event = threading.Event()

    if not drone.connect():
        log.error("edge_drone_connect_failed", drone_id=args.drone_id)
        raise SystemExit(1)
    if kind == "mavlink":
        mode = _tell_mavlink_drone(drone, obstacle)
        log.info("edge_obstacle_sensing", drone_id=args.drone_id, mode=mode)

    previous_sigint = None
    frame_poller = _obstacle_frame_poller(kind, drone, obstacle, log, frames=frames)
    try:
        if frame_poller is not None:
            frame_poller.start()
            log.info("edge_obstacle_frame_poller", drone_id=args.drone_id)
        flight.start()
        _wire_emergency_callback(transport, flight)
        start_local_land_key(flight, sys.stdin, log)
        previous_sigint = _install_sigint_land(flight, log)
        extra = {}
        if kind == "mavlink":
            extra = {"drone": kind, "envelope_profile": envelope.limits.name,
                     "mavlink_address": getattr(drone, "host", None)}
        log.info(
            "edge_connect",
            **extra,
            ws_url=args.ws_url if not isinstance(transport, EdgeUplink) else None,
            ground_urls=len(transport.urls) if isinstance(transport, EdgeUplink) else None,
            authenticated=token is not None,
            drone_id=args.drone_id,
            alt_slot_cm=envelope.alt_slot_cm,
            ceiling_cm=envelope.ceiling_cm,
            ceiling_margin_cm=envelope.ceiling_margin_cm,
            geofence_cm=envelope.geofence_cm,
            link_loss_land_s=envelope.link_watchdog.timeout_s,
            reconnect_max_delay_s=_FLEET_RECONNECT_MAX_DELAY_S,
            auth_backoff_max_s=AUTH_BACKOFF_MAX_S,
            appearance_only=runner.appearance_only,
            frame_stride=getattr(args, "frame_stride", None) or 1,
            stride_max_skip_s=getattr(args, "stride_max_skip_s", DEFAULT_STRIDE_MAX_SKIP_S),
            obstacle_settle_s=obstacle.settle_s,
        )
        transport.connect()
        run_edge_device(
            _flight_frame_source(args, drone, stop_event, log,
                                 on_camera_status=lambda ok: setattr(runner, "camera_ok", ok),
                                 obstacle_holder=obstacle.holder, frames=frames),
            transport,
            runner,
            logger=log,
            stop_event=stop_event,
            flight=flight,
        )
    finally:
        # Belt and braces: land (if flying) before anything else is torn down.
        try:
            flight.shutdown()
        except Exception as exc:
            log.error("edge_flight_shutdown_error", error=repr(exc))
        if frame_poller is not None:
            frame_poller.stop()
        try:
            transport.disconnect()
        except Exception:
            log.warning("edge_disconnect_error")
        try:
            drone.disconnect()
        except Exception as exc:
            log.error("edge_drone_disconnect_error", error=repr(exc))
        if previous_sigint is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint)
            except ValueError:
                pass
            _restore_sigbreak()


def _video_url(args: argparse.Namespace) -> str | None:
    """``--video-url``, else ``FLIGHTRISK_VIDEO_URL`` (keeps stream credentials out of the process list)."""
    return getattr(args, "video_url", None) or (os.environ.get(_VIDEO_URL_ENV_VAR) or "").strip() or None


def _flight_frame_source(
    args: argparse.Namespace,
    drone: Any,
    stop_event: threading.Event,
    log: StructuredLogger,
    on_camera_status: Callable[[bool], None] | None = None,
    obstacle_holder: LatestFrame | None = None,
    frames: EdgeFrames | None = None,
):
    """Frames for a flying edge: the drone's own video, or the companion computer's camera, file or URL.

    Every frame is BGR. The drone's own video is converted by ``frames`` (the edge's ``EdgeFrames``,
    shared with the obstacle sensor's feeds). For a companion camera, ``obstacle_holder`` (the
    obstacle sensor's frame holder) is fed every frame read.
    """
    if _drone_kind(args) != "mavlink":
        # The fake drone only yields blank frames, so a low rate saves CPU.
        return DroneFrameSource(
            drone, stop_event, min_interval_s=0.2 if args.source == "fake" else 1 / 30,
            frame_stride=getattr(args, "frame_stride", None) or 1,
            max_skip_s=getattr(args, "stride_max_skip_s", DEFAULT_STRIDE_MAX_SKIP_S),
            frames=frames,
        )

    def _open():
        import cv2  # lazy: keep module import light for tests

        if args.source == "camera":
            return cv2.VideoCapture(args.camera_index)
        if args.source == "url":
            return cv2.VideoCapture(_video_url(args))
        return cv2.VideoCapture(args.video)

    log.info("edge_frames_capture", source=args.source, camera_index=args.camera_index if args.source == "camera"
             else None)
    source = ResilientCaptureSource(_open, stop_event, log=log, on_status=on_camera_status)
    return _FrameFeedingSource(source, obstacle_holder) if obstacle_holder is not None else source


def _resolve_drone_and_source(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Settle ``--drone`` and ``--source`` together; ``parser.error`` on a combination that cannot work.

    Without ``--drone`` nothing changes. ``--drone tello`` and ``--drone
    fake`` take their frames from the drone (``--source`` defaults to the
    same name and may not be another). ``--drone mavlink`` needs
    ``--source`` camera, file or url (default camera) and the outdoor
    settings in metres; the indoor ``--alt-slot`` (centimetres) and the Tello
    options do not apply to it. Once they are settled, the edge chooses its
    model and checks it against the backend (``_resolve_model``), still before
    it contacts any drone.
    """
    drone = args.drone
    explicit_source = not isinstance(args.source, _Default)
    mavlink_only = [flag for flag, value in (
        ("--mavlink-address", args.mavlink_address), ("--alt-slot-m", args.alt_slot_m),
        ("--ceiling-m", args.ceiling_m), ("--ceiling-margin-m", args.ceiling_margin_m),
        ("--geofence-m", args.geofence_m), ("--failsafe-action", args.failsafe_action),
        ("--frame-heading-deg", args.frame_heading_deg), ("--mavlink-sysid", args.mavlink_sysid),
        ("--link-loss-hold-s", args.link_loss_hold_s),
    ) if value is not None]
    if args.allow_unverified_failsafes:
        mavlink_only.append("--allow-unverified-failsafes")
    if args.autopilot != "auto":
        mavlink_only.append("--autopilot")
    if drone != "mavlink" and mavlink_only:
        parser.error(f"{', '.join(mavlink_only)} only apply to --drone mavlink")
    if drone is None:
        args.source = str(args.source)
        _resolve_model(args, parser)
        return
    if drone in ("tello", "fake"):
        if explicit_source and args.source != drone:
            parser.error(f"--drone {drone} takes its frames from the drone; use --source {drone} or leave it out")
        args.source = drone
        _resolve_model(args, parser)
        return
    # --drone mavlink
    if explicit_source and args.source not in _CAPTURE_SOURCES:
        parser.error("--drone mavlink takes frames from --source camera, file or url")
    args.source = str(args.source)
    if not isinstance(args.alt_slot, _DefaultSlot):
        parser.error("--alt-slot is in centimetres for the indoor Tello; use --alt-slot-m with --drone mavlink")
    if args.tello_host is not None or args.tello_local_ip is not None or args.tello_ifindex is not None:
        parser.error("--tello-host, --tello-local-ip and --tello-ifindex do not apply to --drone mavlink")
    if args.frame_heading_deg is None:
        parser.error("--drone mavlink needs --frame-heading-deg (the same compass bearing on every drone of the "
                     "fleet, for example 0 for north)")
    if args.mavlink_sysid is not None and not 1 <= args.mavlink_sysid <= 255:
        parser.error(f"--mavlink-sysid must be 1 to 255, got {args.mavlink_sysid}")
    try:
        from flightrisk.drone.mavsdk_link import validate_address

        validate_address(_mavlink_address(args))
    except ValueError as exc:
        parser.error(str(exc))
    try:
        from flightrisk.drone.mavlink_fleet import MavlinkFleetOptions

        MavlinkFleetOptions(
            takeoff_alt_m=args.takeoff_alt_m, speed_mps=args.speed_mps, frame_heading_deg=args.frame_heading_deg,
            failsafe_action=_failsafe_action(args),
        )
    except ValueError as exc:
        parser.error(str(exc))
    _resolve_model(args, parser)


def _resolve_model(args: argparse.Namespace, parser: argparse.ArgumentParser, environ: Any = None) -> None:
    """Choose the model of a flying edge and check it against its backend; ``parser.error`` on a mismatch.

    Runs before the edge contacts any drone. An edge that flies nothing has no
    backend to check, so it chooses no model. Without ``--model``,
    ``FLIGHTRISK_MODEL``, ``--airframe``, an inventory unit or a launcher model,
    the model is the backend's default, which always matches.
    """
    from flightrisk.edge_resolution import ModelRefused, resolve_model

    args.model_choice = None
    drone = _drone_kind(args)
    if drone is None:
        return
    frame_path = "drone_video" if args.source in _FLIGHT_SOURCES else "capture_source"
    try:
        args.model_choice = resolve_model(
            drone=drone, drone_id=args.drone_id, frame_path=frame_path, model_flag=getattr(args, "model", None),
            airframe_flag=getattr(args, "airframe", None), environ=os.environ if environ is None else environ,
        )
    except ModelRefused as exc:
        parser.error(str(exc))


def _resolve_edge_start(
    args: argparse.Namespace,
    log: StructuredLogger,
    *,
    argv: Sequence[str] | None = None,
    parser: argparse.ArgumentParser | None = None,
    environ: Any = None,
) -> None:
    """Resolve a flying edge's unit before it contacts its drone (EDGE-1); ``SystemExit`` on a refusal.

    Keeps the result in ``args.edge_resolution``, which the start log, the start envelope
    (``_startup_envelope``) and the consumers rewired from A3.3 on read. Runs once: a second call
    returns at once. An edge that flies nothing resolves nothing.
    """
    from flightrisk.edge_resolution import (
        EdgeStartRefused,
        ModelRefused,
        log_edge_start,
        resolve_edge,
        resolve_model,
    )

    if getattr(args, "edge_resolution", None) is not None:
        return
    args.edge_resolution = None
    drone = _drone_kind(args)
    if drone is None:
        return
    environ = os.environ if environ is None else environ
    drone_id = getattr(args, "drone_id", None)
    try:
        choice = getattr(args, "model_choice", None)
        if choice is None:
            frame_path = "drone_video" if getattr(args, "source", None) in _FLIGHT_SOURCES else "capture_source"
            choice = resolve_model(
                drone=drone, drone_id=drone_id, frame_path=frame_path, model_flag=getattr(args, "model", None),
                airframe_flag=getattr(args, "airframe", None), environ=environ,
            )
        args.edge_resolution = resolve_edge(
            choice=choice, drone=drone, drone_id=drone_id, source=getattr(args, "source", None),
            environ=environ, argv=() if argv is None else argv, parser=parser,
        )
        log_edge_start(args.edge_resolution, log, drone_id=drone_id)
    except (EdgeStartRefused, ModelRefused) as exc:
        text = str(exc) if isinstance(exc, EdgeStartRefused) else f"{exc} Refusing to start."
        log.error("edge_start_refused", drone_id=drone_id, reason=text)
        raise SystemExit(f"edge_device: {text}") from None


def _init_edge_telemetry(log: StructuredLogger) -> bool:
    """Export this edge's logs and spans over OTLP, like the dashboard does.

    A no-op unless ``FLIGHTRISK_OTEL_ENABLED`` is on (see ``flightrisk.telemetry``,
    which warns when it is on but the packages are missing).
    Telemetry must never stop an edge from flying, so any failure is logged
    and ignored. Imported here to keep ``import flightrisk.edge_device`` cheap.
    """
    try:
        from flightrisk.telemetry import init_telemetry

        enabled = init_telemetry()
    except Exception as exc:  # noqa: BLE001 - telemetry must never stop the edge
        log.warning("opentelemetry_init_failed", error=repr(exc))
        return False
    if enabled:
        log.info("opentelemetry_enabled")
    return enabled


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python -m flightrisk.edge_device``."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _resolve_drone_and_source(args, parser)

    if args.source == "file" and not args.video:
        parser.error("--video is required when --source=file")
    if args.source == "url" and not _video_url(args):
        parser.error("--video-url is required when --source=url")
    try:
        ground_urls = parse_ground_urls(args.ground_urls)
    except ValueError as exc:
        parser.error(str(exc))
    if args.node_id is not None and not fp.is_identifier(args.node_id):
        parser.error(f"--node-id must be an identifier such as mac or win-1, got {args.node_id!r}")
    env_node_id = os.environ.get(ENV_NODE_ID, "").strip()
    uses_uplink = bool(ground_urls) and _drone_kind(args) is not None
    if uses_uplink and args.node_id is None and env_node_id and not fp.is_identifier(env_node_id):
        parser.error(f"{ENV_NODE_ID} must be an identifier such as mac or win-1, got {env_node_id!r}")
    try:
        _resolve_appearance_only(args)
    except ValueError as exc:
        parser.error(str(exc))
    args.sensing_awaits_resolution = _sensing_awaits_resolution(args, os.environ)
    try:
        args.frame_stride = _resolve_frame_stride(args)
        args.stride_max_skip_s = _resolve_stride_max_skip()
        args.torch_threads = _resolve_torch_threads()
    except ValueError as exc:
        parser.error(str(exc))
    try:
        args.tello_local_ip, args.tello_ifindex = _resolve_tello_binding(args)
    except ValueError as exc:
        parser.error(str(exc))
    _check_tello_binding(args)
    no_capture_stamps = _check_capture_stamps(args)

    log = StructuredLogger(component="edge-device")
    if no_capture_stamps is not None:
        log.warning(EVENT_NO_CAPTURE_STAMPS, drone_id=getattr(args, "drone_id", None), detail=no_capture_stamps)
    _init_edge_telemetry(log)
    # Before any model is loaded (``_build_runner`` and the obstacle sensor).
    _limit_torch_threads(args.torch_threads, log)

    if _drone_kind(args) is not None:
        # Checked before anything connects, so a slot above the ceiling
        # limit stops the edge at once with the reason.
        _startup_envelope(args)
        # ``_run_flight_edge`` resolves the unit with this command line before it builds the drone, and
        # builds the envelope from the resolved values.
        _run_flight_edge(args, log, argv=sys.argv[1:] if argv is None else list(argv), parser=parser)
        return

    source = _build_source(args, parser, log)
    runner = _build_runner(args, log)

    token = os.environ.get(_TOKEN_ENV_VAR)
    transport = _build_transport(args, runner, token)
    _bind_profile_switcher(runner, transport, log)

    log.info("edge_connect", ws_url=args.ws_url, authenticated=token is not None)
    transport.connect()
    try:
        run_edge_device(source, transport, runner, logger=log)
    finally:
        # run_edge_device already disconnects + releases in its own finally;
        # this is a belt-and-suspenders teardown in case it raised before
        # reaching that block. Both disconnect() calls are idempotent.
        try:
            transport.disconnect()
        except Exception:
            log.warning("edge_disconnect_error")


if __name__ == "__main__":
    main()
