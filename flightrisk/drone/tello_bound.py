"""A Tello client whose sockets are all bound to one local IP (one Wi-Fi adapter).

Used only when an edge is given ``--tello-local-ip``; without it
``TelloController`` builds the stock djitellopy client exactly as before.
See ``docs/two-tellos-one-pc.md`` for the design.

Two standard Tellos are both 192.168.10.1, each on its own Wi-Fi network.
One laptop can fly both, one per Wi-Fi adapter, when the two adapters have
different local IPs: a socket bound to one adapter's IP then receives only
the datagrams sent to that IP (Windows strong host model). djitellopy cannot
do this. It binds one process-wide command socket to all addresses on UDP
8889 and a state socket on 8890, files replies and state by sender address
in one module-level table (so two drones at 192.168.10.1 overwrite each
other), and reads video from ``udp://@0.0.0.0:11111``.

``BoundTello`` is a subclass of djitellopy's ``Tello`` that overrides every
method that touches those module-level sockets and tables, and nothing else.
Its command, state and video sockets are bound to ``(local_ip, port)`` and
never to the wildcard address, and its replies and state live in a
per-instance table. Every other client method (``connect``, ``streamon``,
the state getters, ``send_rc_control``, ``parse_state``) is djitellopy's own.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import math
import os
import re
import socket
import struct
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from djitellopy import Tello
from djitellopy.tello import TelloException

from flightrisk.drone import udp_endpoints as _udp_endpoints
from flightrisk.drone.obstacle_sensor import (
    ENV_MAX_DECODE_LAG_S,
    MAX_DECODE_LAG_RANGE_S,
    MAX_DECODE_LAG_S,
    STALE_CORRUPT_FRAME,
    STALE_DATA_DROPPED,
    STALE_DECODE_ERROR,
    STALE_DECODER_BACKLOG,
    STALE_UNTIL_KEY_FRAME,
    FrameStamp,
)
from flightrisk.drone.udp_endpoints import (
    UdpEndpoint,
    netstat_command,
    process_name,
    wildcard_holders,
)
from flightrisk.obs_log import Throttle, log_event, notice

_log = logging.getLogger(__name__)

__all__ = [
    "IP_UNICAST_IF",
    "BoundFrameRead",
    "BoundTello",
    "TelloBinding",
    "TelloBindingError",
    "check_binding",
    "check_no_wildcard_holder",
    "max_decode_lag_s",
    "open_bound_socket",
    "parse_ifindex",
    "parse_local_ip",
]

#: The Windows socket option that pins a socket's outgoing unicast traffic to
#: one interface (``IP_UNICAST_IF`` in ws2ipdef.h). Python does not name it,
#: but ``setsockopt`` accepts the number. Its value is the interface index as
#: a 4-byte integer in network byte order.
IP_UNICAST_IF = 31

#: The Tello's UDP ports: commands and replies, state, and video.
TELLO_CMD_PORT = 8889
TELLO_STATE_PORT = 8890
TELLO_VIDEO_PORT = 11111

# Receiver threads wake this often to notice that their socket was closed.
_RECV_POLL_S = 0.2
# The video socket asks for this much receive buffer, so a burst of H.264
# datagrams (one frame is several) is not dropped while a frame decodes.
_VIDEO_RCVBUF_BYTES = 1 << 20
# How long stopping the video reader waits for each of its threads before
# closing the socket anyway.
_VIDEO_STOP_JOIN_S = 1.0
# A frame decoded more than ``MAX_DECODE_LAG_S`` (0.3 s by default,
# ``FLIGHTRISK_MAX_DECODE_LAG_S``; ``max_decode_lag_s``) after the first datagram
# of its access unit arrived is stale: the decoder is behind the video.
# An ASCII decimal number, as the edge's other settings (``edge_device._obstacle_number``).
_ASCII_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
#: When the oldest datagram waiting for the decoder is older than this, the
#: waiting datagrams are dropped and decoding resumes at the present.
MAX_QUEUE_AGE_S = 2.0
# Decoded frames whose stamps are kept for ``BoundFrameRead.frame_info`` (about
# a second of video), and packets whose arrival is kept until they decode.
_STAMPS_KEPT = 32
_PACKETS_TRACKED = 64
# ``pict_type`` of an intra-coded picture (PyAV's ``PictureType.I``).
_PICT_TYPE_I = 1
# Repeated receive problems are logged at most once per this many seconds
# per drone and kind.
_LOG_INTERVAL_S = 10.0
_throttle = Throttle(_LOG_INTERVAL_S)

# Errors that mean "this address and port is taken" and "this address is not
# on any adapter". On Windows the socket error numbers are the WSA codes
# (10048, 10049); 10013 (access denied) is what Windows returns when another
# program holds the port exclusively.
_ADDR_IN_USE = {errno.EADDRINUSE, 10048, 10013}
_ADDR_NOT_AVAILABLE = {errno.EADDRNOTAVAIL, 10049}

SocketFactory = Callable[[], socket.socket]


class TelloBindingError(RuntimeError):
    """The Tello sockets cannot be bound as asked. The message tells the operator what to fix."""


def max_decode_lag_s(environ: Any = None) -> float:
    """The bound decoder's stale limit in seconds: ``FLIGHTRISK_MAX_DECODE_LAG_S``, else ``MAX_DECODE_LAG_S``.

    A plain ASCII decimal number from 0.1 to 2.0, the same rules as the edge's
    obstacle settings. Raises ``ValueError`` with an operator-facing message
    otherwise. The edge checks it at startup (``ObstacleSettings``), so a bad
    value refuses to start the edge before any drone is connected.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_MAX_DECODE_LAG_S) or "").strip()
    if not raw:
        return MAX_DECODE_LAG_S
    lo, hi = MAX_DECODE_LAG_RANGE_S
    value: float | None = None
    if _ASCII_DECIMAL_RE.fullmatch(raw):
        try:
            value = float(raw)
        except (ValueError, OverflowError):
            value = None
    if value is None or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{ENV_MAX_DECODE_LAG_S}={raw!r} must be a number of seconds from {lo:g} to {hi:g}")
    return value


def _udp_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _os_error_code(exc: OSError) -> int | None:
    winerror = getattr(exc, "winerror", None)
    return winerror if winerror is not None else exc.errno


def parse_local_ip(raw: str) -> str:
    """Check ``--tello-local-ip``: an IPv4 address that a socket can be bound to. Raises ``ValueError``."""
    text = (raw or "").strip()
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError as exc:
        raise ValueError(f"--tello-local-ip must be an IPv4 address such as 192.168.10.2, got {raw!r}") from exc
    if address.is_unspecified:
        raise ValueError(
            "--tello-local-ip must be the IP of one Wi-Fi adapter, not 0.0.0.0: "
            "binding every adapter is exactly what this option avoids"
        )
    if address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        raise ValueError(f"--tello-local-ip must be a unicast address, got {text}")
    return str(address)


def parse_ifindex(raw: Any) -> int:
    """Check ``--tello-ifindex``: a positive interface index. Raises ``ValueError``."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"--tello-ifindex must be a whole number (an InterfaceIndex), got {raw!r}") from exc
    if not 0 < value < 2**32:
        raise ValueError(f"--tello-ifindex must be a positive interface index, got {value}")
    return value


@dataclass(frozen=True)
class TelloBinding:
    """Where one Tello's sockets are bound.

    ``local_ip`` is the IP of the Wi-Fi adapter that joined this drone's
    network. ``ifindex``, Windows only, is that adapter's interface index;
    when set, ``IP_UNICAST_IF`` pins outgoing commands to it. The ports
    default to the Tello's own; tests use other ports so that two fake
    drones can run side by side on one loopback address.
    """

    local_ip: str
    ifindex: int | None = None
    cmd_port: int = TELLO_CMD_PORT
    state_port: int = TELLO_STATE_PORT
    video_port: int = TELLO_VIDEO_PORT
    drone_cmd_port: int = TELLO_CMD_PORT


def _psutil_ipv4_table() -> dict[str, list[str]] | None:
    """Each network adapter's IPv4 addresses, or None when ``psutil`` is not installed."""
    try:
        import psutil
    except ImportError:
        return None
    return {
        name: [a.address for a in addresses if a.family == socket.AF_INET]
        for name, addresses in psutil.net_if_addrs().items()
    }


def check_binding(
    binding: TelloBinding,
    host: str,
    *,
    socket_factory: SocketFactory = _udp_socket,
    interface_table: Callable[[], dict[str, list[str]] | None] = _psutil_ipv4_table,
    is_windows: Callable[[], bool] | None = None,
    interface_indexes: Callable[[], list[int] | None] | None = None,
    udp_endpoints: Callable[[], list[UdpEndpoint] | None] | None = None,
) -> None:
    """Refuse a binding that cannot work, before any socket on a Tello port is opened.

    Raises ``TelloBindingError`` when the local IP is not a usable IPv4
    address, is not assigned to any adapter on this computer, or is
    assigned to two adapters at once; when ``ifindex`` is given off
    Windows or names an interface that does not exist; and, on Windows,
    when another socket holds the wildcard address on a Tello port (see
    ``check_no_wildcard_holder``). Logs a warning when the Tello host is
    outside the local IP's /24 network.
    """
    if is_windows is None:
        is_windows = _is_windows
    try:
        local_ip = parse_local_ip(binding.local_ip)
    except ValueError as exc:
        raise TelloBindingError(str(exc)) from exc

    # Is the address on an adapter at all? Binding a throwaway socket to an
    # ephemeral port answers that the same way on Windows, macOS and Linux.
    probe = socket_factory()
    try:
        probe.bind((local_ip, 0))
    except OSError as exc:
        if _os_error_code(exc) in _ADDR_NOT_AVAILABLE:
            raise TelloBindingError(
                f"--tello-local-ip {local_ip} is not assigned to any network adapter on this computer. "
                "Check that the adapter is plugged in and has joined the drone's Wi-Fi, and compare with "
                "Get-NetIPAddress -AddressFamily IPv4 (Windows) or ifconfig."
            ) from exc
        raise TelloBindingError(f"--tello-local-ip {local_ip} cannot be used: {exc}") from exc
    finally:
        probe.close()

    table = interface_table()
    if table is None:
        notice(
            _log, "tello_local_ip_duplicate_check_skipped",
            f"Could not list network adapters (psutil is not installed), so it is not checked that "
            f"{local_ip} is on only one adapter.",
            level=logging.WARNING, local_ip=local_ip,
        )
    else:
        owners = sorted(name for name, addresses in table.items() if local_ip in addresses)
        if len(owners) > 1:
            raise TelloBindingError(
                f"--tello-local-ip {local_ip} is assigned to more than one adapter ({', '.join(owners)}), so "
                "traffic for the two drones cannot be told apart. Give each Wi-Fi adapter its own IP "
                "(docs/two-tellos-one-pc.md)."
            )

    if binding.ifindex is not None:
        if not is_windows():
            raise TelloBindingError("--tello-ifindex is only supported on Windows (it sets IP_UNICAST_IF).")
        if interface_indexes is None:
            indexes = _interface_indexes()
        else:
            indexes = interface_indexes()
        if indexes is not None and binding.ifindex not in indexes:
            raise TelloBindingError(
                f"--tello-ifindex {binding.ifindex} is not an interface on this computer. Use the "
                "InterfaceIndex that Get-NetIPAddress -AddressFamily IPv4 shows for the adapter with "
                f"{local_ip}."
            )

    check_no_wildcard_holder(binding, is_windows=is_windows, udp_endpoints=udp_endpoints)

    try:
        same_net = ipaddress.IPv4Address(host) in ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
    except ValueError:
        same_net = True  # a host name: nothing to compare
    if not same_net:
        notice(
            _log, "tello_local_ip_subnet_mismatch",
            f"The Tello host {host} is not on the same /24 network as --tello-local-ip {local_ip}; "
            "check that this is the adapter that joined the drone's Wi-Fi.",
            level=logging.WARNING, local_ip=local_ip, tello_host=host,
        )


def check_no_wildcard_holder(
    binding: TelloBinding,
    *,
    is_windows: Callable[[], bool] | None = None,
    udp_endpoints: Callable[[], list[UdpEndpoint] | None] | None = None,
) -> None:
    """Windows only: refuse when a socket holds 0.0.0.0 or [::] on one of this binding's Tello ports.

    Windows lets a wildcard bind and a specific bind share a port, in either
    order, and gives each datagram to the socket bound to the address it was
    sent to. A program that holds 0.0.0.0:8889 and commands a Tello through
    one adapter (a djitellopy script, or an edge started without
    ``--tello-local-ip``) gets its reply sent to that adapter's IP, so
    Windows delivers the reply to the edge bound there, and the edge's
    source filter accepts it because it comes from the drone. The edge's
    bind cannot catch this and ``SO_EXCLUSIVEADDRUSE`` does not either (see
    ``open_bound_socket``), so the UDP endpoint table is read instead.
    Another edge's sockets are bound to its own adapter IP, so they are not
    holders. When the table cannot be read, a warning is logged and the
    check is skipped. Off Windows nothing is read: there a wildcard holder
    already makes the edge's own bind fail.
    """
    if not (is_windows or _is_windows)():
        return
    ports = (binding.cmd_port, binding.state_port, binding.video_port)
    reader = udp_endpoints if udp_endpoints is not None else _udp_endpoints.read_udp_endpoints
    endpoints = reader()
    if endpoints is None:
        notice(
            _log, "tello_wildcard_check_skipped",
            "Could not read the UDP endpoint table (psutil and netstat both failed), so it is not checked "
            f"that no other program holds UDP {', '.join(map(str, ports))} on all addresses. Check by hand: "
            f"{netstat_command(ports)} must show no 0.0.0.0 line.",
            level=logging.WARNING, local_ip=binding.local_ip,
        )
        return
    holders = wildcard_holders(endpoints, ports)
    if not holders:
        return
    found = ", ".join(_describe_holder(h) for h in holders)
    commands = netstat_command(ports)
    if any(h.ipv6 for h in holders):
        commands += f" and {netstat_command(ports, ipv6=True)}"
    raise TelloBindingError(
        f"Another program holds UDP {found} on all addresses. A Tello answers the adapter IP that sent the "
        "command, so Windows would deliver that program's Tello replies to this edge, and two programs would "
        "command one drone. It is usually a djitellopy script, scripts/tello_hw_check.py without --local-ip, "
        f"or an edge started without --tello-local-ip. Find it with: {commands} (a line with 0.0.0.0 or [::]; "
        "the last column is its PID), stop it, and give every Tello program on this computer its own adapter IP."
    )


def _describe_holder(holder: UdpEndpoint) -> str:
    if holder.pid is None:
        return holder.address()
    name = process_name(holder.pid)
    return f"{holder.address()} (PID {holder.pid}{', ' + name if name else ''})"


def _interface_indexes() -> list[int] | None:
    try:
        return [index for index, _name in socket.if_nameindex()]
    except (AttributeError, OSError):
        return None


def open_bound_socket(
    local_ip: str,
    port: int,
    *,
    purpose: str,
    ifindex: int | None = None,
    socket_factory: SocketFactory = _udp_socket,
) -> socket.socket:
    """A UDP socket bound to ``(local_ip, port)``, never to the wildcard address.

    With ``ifindex``, ``IP_UNICAST_IF`` is set before the bind. No
    ``SO_REUSEADDR``: a second socket on the same address and port must
    fail, which is how two edges with the same local IP are caught. Raises
    ``TelloBindingError`` with an operator-facing message.

    No ``SO_EXCLUSIVEADDRUSE`` either. Microsoft's bind table for Windows
    Server 2003 and later ("Using SO_REUSEADDR and SO_EXCLUSIVEADDRUSE")
    gives a specific-address bind with it exactly the same outcomes as one
    without, for the same or a different user. It still succeeds when the
    same user's default wildcard socket holds the port, and a later default
    wildcard bind still succeeds beside it. So it would neither refuse nor
    block a stray wildcard holder; ``check_no_wildcard_holder`` does that.
    """
    if not local_ip or local_ip == "0.0.0.0":
        raise TelloBindingError(f"refusing to bind the {purpose} socket to all addresses")
    sock = socket_factory()
    try:
        if ifindex is not None:
            try:
                sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", ifindex))
            except OSError as exc:
                raise TelloBindingError(
                    f"Could not pin the {purpose} socket to interface index {ifindex} (IP_UNICAST_IF): {exc}"
                ) from exc
        try:
            sock.bind((local_ip, port))
        except OSError as exc:
            code = _os_error_code(exc)
            if code in _ADDR_IN_USE:
                raise TelloBindingError(
                    f"UDP {local_ip}:{port} ({purpose}) is already in use. Another edge may be using the same "
                    f"--tello-local-ip, the hardware check may still be running, or a program started without "
                    f"--tello-local-ip holds port {port} on all addresses. Every edge on this computer needs "
                    "its own --tello-local-ip."
                ) from exc
            if code in _ADDR_NOT_AVAILABLE:
                raise TelloBindingError(
                    f"UDP {local_ip}:{port} ({purpose}) cannot be bound: {local_ip} is not assigned to any "
                    "network adapter on this computer."
                ) from exc
            raise TelloBindingError(f"UDP {local_ip}:{port} ({purpose}) cannot be bound: {exc}") from exc
    except BaseException:
        sock.close()
        raise
    return sock


def _note_foreign(name: str, kind: str, source: tuple, expected: str, count: int) -> None:
    """Log (throttled) a datagram dropped because it did not come from this drone."""
    allowed, suppressed = _throttle.allow((name, "tello_foreign_packet", kind))
    if allowed:
        log_event(
            _log, logging.WARNING, "tello_foreign_packet",
            f"[{name}] dropped a {kind} datagram from {source[0]}:{source[1]}; this edge's drone is {expected}",
            category="drone_comm", component="tello", drone_id=name, kind=kind,
            source=f"{source[0]}:{source[1]}", expected=expected, total=count, suppressed=suppressed or None,
        )


def _note_receive_error(name: str, kind: str, exc: BaseException) -> None:
    allowed, suppressed = _throttle.allow((name, "tello_receive_error", kind))
    if allowed:
        log_event(
            _log, logging.WARNING, "tello_receive_error",
            f"[{name}] {kind} receive error: {type(exc).__name__}: {exc}",
            category="error", component="tello", drone_id=name, kind=kind,
            error_type=type(exc).__name__, error=str(exc), suppressed=suppressed or None,
        )


def _closed(sock: socket.socket) -> bool:
    try:
        return sock.fileno() == -1
    except OSError:
        return True


class BoundFrameRead:
    """Decodes one Tello's H.264 video from a UDP socket bound to ``(local_ip, port)``.

    Has the parts of djitellopy's ``BackgroundFrameRead`` that
    ``TelloController`` uses: ``frame`` (an RGB array, like djitellopy's
    ``np.array(frame.to_image())``; a black 300 x 400 frame until the
    first one decodes), ``start()`` and ``stop()``. Datagrams from any
    address other than the drone's are dropped. ``stop()`` closes the
    socket, so the stream can be restarted on the same port.

    Receiving and decoding run on two threads, so the obstacle sensor can
    tell how old a frame is and whether it can be trusted (``frame_info``):

    * The receiver thread only reads datagrams. It stamps each with its
      arrival on ``clock`` (``time.monotonic``) and queues it, so it keeps
      the socket's receive buffer drained whatever the decoder does.
    * The decoder thread feeds the queued datagrams to PyAV's H.264 parser
      and decoder. A frame's capture stamp is the arrival of the first
      datagram of its access unit. That is after the camera captured it,
      so the stamp makes a frame look newer than it is by the camera's own
      latency (encoding and Wi-Fi), and by nothing the decoder does. That
      latency is what the obstacle settle time must cover
      (``FLIGHTRISK_OBSTACLE_SETTLE_S``, hardware item H5).
    * A frame is stale (``FrameStamp.stale_cause``) when it was decoded more
      than ``max_lag_s`` after its first datagram arrived (the decoder is
      behind; ``decoder backlog``), when the decoder flags it corrupt, and
      from a decode error or dropped video data until the next intra frame,
      because every frame until then is built on a damaged one; such a frame
      keeps the cause that tainted the stream (``decode error``, ``video data
      dropped`` or ``corrupt frame``), and before the first intra frame of a
      stream it is ``damaged reference, waiting for a key frame``. Video data is
      dropped only here: when the oldest queued datagram is older than
      ``max_queue_age_s``, the queue is discarded so decoding resumes at the
      present instead of working through seconds of old video. The decoder
      starts in that state too, so nothing is trusted before the first intra
      frame. A stale frame still becomes ``frame``, for the video tile and
      detection; only the sensor refuses it.

    A lost datagram that the decoder conceals without an error and without
    flagging the frame cannot be seen here: the Tello's video has no
    sequence numbers. ``stale_frames``, ``decode_errors`` and
    ``datagrams_dropped`` count what is seen.
    """

    def __init__(
        self,
        host: str,
        local_ip: str,
        port: int,
        *,
        name: str = "drone",
        socket_factory: SocketFactory = _udp_socket,
        clock: Callable[[], float] = time.monotonic,
        max_lag_s: float = MAX_DECODE_LAG_S,
        max_queue_age_s: float = MAX_QUEUE_AGE_S,
    ):
        import av  # djitellopy imports it too; kept local so this module imports without it

        self.host = host
        self.address = (local_ip, port)
        self.name = name
        self.lock = threading.Lock()
        self._frame = np.zeros([300, 400, 3], dtype=np.uint8)
        self._stamps: deque[tuple[np.ndarray, FrameStamp]] = deque(maxlen=_STAMPS_KEPT)
        self.stopped = False
        self.frames_decoded = 0
        self.stale_frames = 0
        self.decode_errors = 0
        self.foreign_datagrams = 0
        self.datagrams_dropped = 0
        self._clock = clock
        self.max_lag_s = float(max_lag_s)
        self.max_queue_age_s = float(max_queue_age_s)
        # Datagrams waiting for the decoder, as (arrival, data), and whether
        # some were dropped since the decoder last looked.
        self._queue: deque[tuple[float, bytes]] = deque()
        self._queue_cond = threading.Condition()
        self._gap = False
        # Decoder state (decoder thread only): until an intra frame decodes
        # cleanly, every frame is stale for this cause.
        self._taint: str | None = STALE_UNTIL_KEY_FRAME
        self._au_started_at: float | None = None
        self._packet_index = 0
        self._packet_started: dict[int, float] = {}
        self._sock = open_bound_socket(local_ip, port, purpose="video", socket_factory=socket_factory)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _VIDEO_RCVBUF_BYTES)
        except OSError:
            pass  # the default buffer still works, it just drops more under load
        self._sock.settimeout(_RECV_POLL_S)
        self._codec = av.CodecContext.create("h264", "r")
        self.receiver = threading.Thread(target=self.receive_datagrams, daemon=True, name=f"{name}-video-rx")
        self.worker = threading.Thread(target=self.update_frame, daemon=True, name=f"{name}-video")

    @property
    def frame(self) -> np.ndarray:
        with self.lock:
            return self._frame

    @frame.setter
    def frame(self, value: np.ndarray) -> None:
        with self.lock:
            self._frame = value

    def frame_info(self, frame: Any) -> FrameStamp | None:
        """The ``FrameStamp`` of ``frame``, one of the last frames this reader decoded, else None.

        ``frame`` is matched by identity, so only the array this reader
        handed out counts (``frame``, or a callback's argument). A frame that
        is not among the last ``_STAMPS_KEPT`` decoded ones, such as the black
        frame before the first decode, returns None.
        """
        with self.lock:
            for array, stamp in reversed(self._stamps):
                if array is frame:
                    return stamp
        return None

    def start(self) -> None:
        self.receiver.start()
        self.worker.start()

    # --- Receiving ---

    def receive_datagrams(self) -> None:
        """Receive the drone's datagrams, stamp and queue them until ``stop()``. Closes the socket at the end."""
        sock = self._sock
        try:
            while not self.stopped:
                try:
                    data, source = sock.recvfrom(65536)
                except (TimeoutError, ConnectionResetError):
                    continue
                except OSError as exc:
                    if self.stopped or _closed(sock):
                        return
                    _note_receive_error(self.name, "video", exc)
                    time.sleep(_RECV_POLL_S)
                    continue
                if source[0] != self.host:
                    self.foreign_datagrams += 1
                    _note_foreign(self.name, "video", source, self.host, self.foreign_datagrams)
                    continue
                self.queue_datagram(data, self._clock())
        finally:
            try:
                sock.close()
            except OSError:
                pass
            with self._queue_cond:
                self._queue_cond.notify_all()

    def queue_datagram(self, data: bytes, arrived_at: float) -> None:
        """Queue one datagram for the decoder, first dropping the queue if its oldest datagram is too old."""
        with self._queue_cond:
            if self._queue and arrived_at - self._queue[0][0] > self.max_queue_age_s:
                self.datagrams_dropped += len(self._queue)
                self._queue.clear()
                self._gap = True
                _note_receive_error(
                    self.name, "video_backlog",
                    RuntimeError(f"the decoder fell more than {self.max_queue_age_s:g} s behind; old video dropped"),
                )
            self._queue.append((arrived_at, data))
            self._queue_cond.notify()

    @property
    def queue_depth(self) -> int:
        """Datagrams received and not yet decoded."""
        with self._queue_cond:
            return len(self._queue)

    def _next_datagram(self) -> tuple[float, bytes, bool] | None:
        with self._queue_cond:
            if not self._queue and not self.stopped:
                self._queue_cond.wait(_RECV_POLL_S)
            if not self._queue:
                return None
            arrived_at, data = self._queue.popleft()
            gap, self._gap = self._gap, False
            return arrived_at, data, gap

    # --- Decoding ---

    def update_frame(self) -> None:
        """Decode queued datagrams into frames until ``stop()``."""
        while not self.stopped:
            item = self._next_datagram()
            if item is None:
                continue
            arrived_at, data, gap = item
            if gap:
                self._taint = STALE_DATA_DROPPED
            self._decode(data, arrived_at)

    def _decode(self, data: bytes, arrived_at: float | None = None) -> None:
        """Feed one datagram that arrived at ``arrived_at`` to the parser and publish what decodes."""
        if arrived_at is None:
            arrived_at = self._clock()
        if self._au_started_at is None:
            self._au_started_at = arrived_at
        try:
            for packet in self._codec.parse(data):
                # The packet's data began with the datagram that ended the previous packet's access unit.
                started = self._au_started_at
                self._au_started_at = arrived_at
                packet.pts = self._packet_index
                self._packet_started[self._packet_index] = started
                self._packet_index += 1
                while len(self._packet_started) > _PACKETS_TRACKED:
                    del self._packet_started[next(iter(self._packet_started))]
                for decoded in self._codec.decode(packet):
                    self._publish(decoded)
        except Exception as exc:  # noqa: BLE001 - a lost datagram corrupts a frame; keep decoding
            self.decode_errors += 1
            self._taint = STALE_DECODE_ERROR
            _note_receive_error(self.name, "video_decode", exc)

    def _publish(self, decoded: Any) -> None:
        """Make ``decoded`` the newest frame, with its capture stamp and whether it can be trusted."""
        started = self._packet_started.get(decoded.pts) if decoded.pts is not None else None
        if started is None:
            # Not matched to its packet: the oldest packet still outstanding, which is never newer.
            started = min(self._packet_started.values(), default=self._clock())
        if decoded.pts is not None:
            for index in [i for i in self._packet_started if i <= decoded.pts]:
                del self._packet_started[index]
        array = decoded.to_ndarray(format="rgb24")
        cause: str | None = None
        if getattr(decoded, "is_corrupt", False):
            cause = STALE_CORRUPT_FRAME
            self._taint = STALE_CORRUPT_FRAME
        elif self._taint is not None:
            if _is_intra(decoded):
                self._taint = None
            else:
                # The cause that tainted the stream (a decode error, dropped video data, a corrupt
                # frame, or no key frame yet) stays the cause until the next intra frame.
                cause = self._taint
        if cause is None and self._clock() - started > self.max_lag_s:
            cause = STALE_DECODER_BACKLOG
        stamp = FrameStamp(captured_at=started, stale_cause=cause)
        with self.lock:
            self._frame = array
            self._stamps.append((array, stamp))
        self.frames_decoded += 1
        if cause is not None:
            self.stale_frames += 1

    def stop(self) -> None:
        """Stop receiving and decoding and close the socket (waits up to ``_VIDEO_STOP_JOIN_S`` per thread)."""
        self.stopped = True
        with self._queue_cond:
            self._queue_cond.notify_all()
        for thread in (self.receiver, self.worker):
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(_VIDEO_STOP_JOIN_S)
        try:
            self._sock.close()
        except OSError:
            pass


def _is_intra(decoded: Any) -> bool:
    """Whether ``decoded`` is a key frame or an intra-coded picture, which needs no earlier frame."""
    if getattr(decoded, "key_frame", False):
        return True
    try:
        return int(decoded.pict_type) == _PICT_TYPE_I
    except (TypeError, ValueError, AttributeError):
        return False


class BoundTello(Tello):
    """djitellopy's ``Tello`` with every socket bound to one local IP (see the module docstring).

    Overrides only the methods that use djitellopy's module-level sockets
    and tables: the constructor, ``get_own_udp_object``, the two send
    methods, the receivers, the video address and reader, and ``end``.
    """

    def __init__(
        self,
        host: str,
        binding: TelloBinding,
        retry_count: int = 1,
        *,
        name: str = "drone",
        socket_factory: SocketFactory = _udp_socket,
    ):
        # djitellopy's constructor is deliberately not called: it binds the
        # process-wide command socket to all addresses on UDP 8889 and starts
        # the shared receivers. These are the attributes it sets.
        self.address = (host, binding.drone_cmd_port)
        self.stream_on = False
        self.is_flying = False
        self.retry_count = retry_count
        self.last_received_command_timestamp = time.time()
        self.last_rc_control_timestamp = time.time()
        self.vs_udp_port = binding.video_port
        self.background_frame_read = None
        self.binding = binding
        self.name = name
        self.foreign_datagrams = 0
        self._socket_factory = socket_factory
        # Replies and the latest state, the per-drone entry that djitellopy
        # keeps in its module-level table.
        self._udp: dict = {"responses": [], "state": {}}
        self._closed = True  # until both sockets are open
        self._cmd_sock = open_bound_socket(
            binding.local_ip, binding.cmd_port, purpose="command", ifindex=binding.ifindex,
            socket_factory=socket_factory,
        )
        try:
            self._state_sock = open_bound_socket(
                binding.local_ip, binding.state_port, purpose="state", socket_factory=socket_factory,
            )
        except BaseException:
            self._cmd_sock.close()
            raise
        self._closed = False
        for sock, kind in ((self._cmd_sock, "reply"), (self._state_sock, "state")):
            sock.settimeout(_RECV_POLL_S)
            threading.Thread(
                target=self._receive, args=(sock, kind), daemon=True, name=f"{name}-{kind}-rx",
            ).start()
        notice(
            _log, "tello_transport_bound",
            f"[{name}] Tello sockets bound to {binding.local_ip}: command {binding.cmd_port}, "
            f"state {binding.state_port}, video {binding.video_port}"
            + (f", interface index {binding.ifindex}" if binding.ifindex is not None else ""),
            drone_id=name, local_ip=binding.local_ip, ifindex=binding.ifindex, tello_host=host,
            cmd_port=binding.cmd_port, state_port=binding.state_port, video_port=binding.video_port,
        )

    # --- The per-drone table ---

    def get_own_udp_object(self) -> dict:
        return self._udp

    def replace_udp_object(self, udp: dict) -> None:
        """Put ``udp`` (for example a wrapper that stamps state arrivals) in place of the table."""
        self._udp = udp

    # --- Receivers ---

    @staticmethod
    def udp_response_receiver():
        raise NotImplementedError("BoundTello runs its own receivers")

    @staticmethod
    def udp_state_receiver():
        raise NotImplementedError("BoundTello runs its own receivers")

    def _receive(self, sock: socket.socket, kind: str) -> None:
        """Store replies (``kind`` "reply") or parsed state ("state") from this drone until the socket closes.

        A datagram from any address other than the drone's is dropped and
        logged. Unlike djitellopy's receivers, an error on one datagram (for
        example a Windows ``ConnectionResetError`` after an ICMP port
        unreachable) does not stop the thread.
        """
        host = self.address[0]
        while not self._closed:
            try:
                data, source = sock.recvfrom(2048)
            except (TimeoutError, ConnectionResetError):
                continue
            except OSError as exc:
                if self._closed or _closed(sock):
                    return
                _note_receive_error(self.name, kind, exc)
                time.sleep(_RECV_POLL_S)
                continue
            if source[0] != host:
                self.foreign_datagrams += 1
                _note_foreign(self.name, kind, source, host, self.foreign_datagrams)
                continue
            try:
                if kind == "reply":
                    self._udp["responses"].append(data)
                else:
                    self._udp["state"] = Tello.parse_state(data.decode("ASCII"))
            except Exception as exc:  # noqa: BLE001 - one bad datagram must not stop the receiver
                _note_receive_error(self.name, kind, exc)

    # --- Sending ---

    def send_command_with_return(self, command: str, timeout: int = Tello.RESPONSE_TIMEOUT) -> str:
        """djitellopy's ``send_command_with_return``, sent from the bound command socket."""
        # As in djitellopy: wait if the last reply was very recent.
        diff = time.time() - self.last_received_command_timestamp
        if diff < self.TIME_BTW_COMMANDS:
            self.LOGGER.debug(f"Waiting {diff} seconds to execute command: {command}...")
            time.sleep(diff)

        self.LOGGER.info(f"Send command: '{command}'")
        timestamp = time.time()

        self._cmd_sock.sendto(command.encode("utf-8"), self.address)

        responses = self.get_own_udp_object()["responses"]

        while not responses:
            if time.time() - timestamp > timeout:
                message = f"Aborting command '{command}'. Did not receive a response after {timeout} seconds"
                self.LOGGER.warning(message)
                return message
            time.sleep(0.1)

        self.last_received_command_timestamp = time.time()

        first_response = responses.pop(0)
        try:
            response = first_response.decode("utf-8")
        except UnicodeDecodeError as e:
            self.LOGGER.error(e)
            return "response decode error"
        response = response.rstrip("\r\n")

        self.LOGGER.info(f"Response {command}: '{response}'")
        return response

    def send_command_without_return(self, command: str):
        """djitellopy's ``send_command_without_return``, sent from the bound command socket."""
        self.LOGGER.info(f"Send command (no response expected): '{command}'")
        self._cmd_sock.sendto(command.encode("utf-8"), self.address)

    # --- Video ---

    def get_udp_video_address(self) -> str:
        return f"udp://@{self.binding.local_ip}:{self.vs_udp_port}"

    def get_frame_read(self, with_queue=False, max_queue_len=32) -> BoundFrameRead:
        if with_queue:
            raise TelloException("BoundTello does not support a frame queue")
        if self.background_frame_read is None:
            reader = BoundFrameRead(
                self.address[0], self.binding.local_ip, self.vs_udp_port,
                name=self.name, socket_factory=self._socket_factory, max_lag_s=max_decode_lag_s(),
            )
            reader.start()
            self.background_frame_read = reader
        return self.background_frame_read

    # --- Shutdown ---

    def end(self):
        """Land and stop the stream as djitellopy's ``end`` does, then close every socket. Idempotent."""
        if getattr(self, "_closed", True):
            return
        try:
            try:
                if self.is_flying:
                    self.land()
                if self.stream_on:
                    self.streamoff()
            except TelloException:
                pass
        finally:
            self._closed = True
            if self.background_frame_read is not None:
                self.background_frame_read.stop()
            for sock in (self._cmd_sock, self._state_sock):
                try:
                    sock.close()
                except OSError:
                    pass

    def ports(self) -> dict[str, tuple[str, int] | None]:
        """The local addresses the sockets are bound to (for logs and tests)."""
        def name(sock: socket.socket) -> tuple[str, int] | None:
            try:
                return sock.getsockname()
            except OSError:
                return None

        return {"command": name(self._cmd_sock), "state": name(self._state_sock)}
