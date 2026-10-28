"""Two Tellos from one computer: ``--tello-local-ip`` and ``flightrisk.drone.tello_bound``.

Every test here runs over real UDP sockets on 127.0.0.1 against fake Tello
drones (``FakeUdpTello``). Two controllers run side by side in one process,
each with its own fake drone, both drones sending from the same IP, which is
exactly the case djitellopy's module-level tables cannot tell apart. The
controllers use distinct free ports instead of the Tello's 8889, 8890 and
11111, so no second adapter or loopback alias is needed. Nothing here sends
a packet anywhere but 127.0.0.1.
"""

from __future__ import annotations

import argparse
import inspect
import socket
import struct
import threading
import time
from collections.abc import Callable
from fractions import Fraction

import djitellopy.tello as djitello
import numpy as np
import pytest

from flightrisk.drone import tello as tello_mod
from flightrisk.drone.tello import TelloController
from flightrisk.drone.tello_bound import (
    IP_UNICAST_IF,
    BoundTello,
    TelloBinding,
    TelloBindingError,
    check_binding,
    open_bound_socket,
    parse_ifindex,
    parse_local_ip,
)

LOOPBACK = "127.0.0.1"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((LOOPBACK, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _binding(fake: FakeUdpTello, **overrides) -> TelloBinding:
    fields = {
        "local_ip": LOOPBACK, "cmd_port": _free_port(), "state_port": _free_port(), "video_port": _free_port(),
        "drone_cmd_port": fake.cmd_port,
    }
    fields.update(overrides)
    return TelloBinding(**fields)


def _wait_for(predicate: Callable[[], bool], timeout_s: float = 5.0, step_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step_s)
    return predicate()


class FakeUdpTello:
    """A fake Tello on real UDP sockets at 127.0.0.1.

    Answers each command datagram on its command socket, back to the source
    address it came from (as a Tello does), with ``ok``, except ``rc``
    commands, which get no reply. ``script(command)`` may return a list of
    ``(reply, delay_s)`` pairs to give instead. Every command is recorded in
    ``received`` with its source address. ``start_state(addr)`` sends state
    packets (``h``, ``tof``, ``bat``, motor ``time``) to ``addr`` about 20
    times a second; ``start_video(addr, rgb)`` sends an H.264 stream of
    solid-colour frames.
    """

    def __init__(self, battery: int):
        self.battery = battery
        self.height_cm = 0
        self.tof_cm = 10
        self.motor_s = 5
        self.script: Callable[[str], list[tuple[str, float]] | None] | None = None
        self.received: list[tuple[str, tuple[str, int]]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd.bind((LOOPBACK, 0))
        self.cmd.settimeout(0.1)
        self.cmd_port = self.cmd.getsockname()[1]
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.out.bind((LOOPBACK, 0))
        self._state_stop: threading.Event | None = None
        self._video_stop: threading.Event | None = None
        self._threads: list[threading.Thread] = []
        self._spawn(self._serve)

    def _spawn(self, target, *args) -> None:
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        self._threads.append(t)

    def commands(self) -> list[str]:
        with self._lock:
            return [c for c, _ in self.received]

    def sources(self) -> set[tuple[str, int]]:
        with self._lock:
            return {src for _, src in self.received}

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, src = self.cmd.recvfrom(2048)
            except (TimeoutError, ConnectionResetError):
                continue
            except OSError:
                return
            command = data.decode("utf-8")
            with self._lock:
                self.received.append((command, src))
            replies = self.script(command) if self.script is not None else None
            if replies is None:
                replies = [] if command.split()[0] == "rc" else [("ok", 0.0)]
            for text, delay_s in replies:
                if delay_s <= 0:
                    self._reply(text, src)
                else:
                    timer = threading.Timer(delay_s, self._reply, args=(text, src))
                    timer.daemon = True
                    timer.start()

    def _reply(self, text: str, dest: tuple[str, int]) -> None:
        if self._stop.is_set():
            return
        try:
            self.cmd.sendto(text.encode("utf-8"), dest)
        except OSError:
            pass

    def state_packet(self) -> bytes:
        return (
            f"mid:-1;x:0;y:0;z:0;mpry:0,0,0;pitch:0;roll:0;yaw:0;vgx:0;vgy:0;vgz:0;templ:40;temph:42;"
            f"tof:{self.tof_cm};h:{self.height_cm};bat:{self.battery};baro:0.00;time:{self.motor_s};"
            "agx:0.00;agy:0.00;agz:-1000.00;\r\n"
        ).encode("ascii")

    def start_state(self, dest: tuple[str, int]) -> None:
        self.stop_state()
        stop = threading.Event()
        self._state_stop = stop

        def stream() -> None:
            while not stop.is_set() and not self._stop.is_set():
                try:
                    self.out.sendto(self.state_packet(), dest)
                except OSError:
                    pass
                stop.wait(0.05)

        self._spawn(stream)

    def stop_state(self) -> None:
        if self._state_stop is not None:
            self._state_stop.set()
            self._state_stop = None

    def start_video(self, dest: tuple[str, int], rgb: tuple[int, int, int]) -> None:
        stream = _h264_stream(rgb)
        stop = threading.Event()
        self._video_stop = stop

        def send() -> None:
            while not stop.is_set() and not self._stop.is_set():
                for i in range(0, len(stream), 1460):
                    try:
                        self.out.sendto(stream[i:i + 1460], dest)
                    except OSError:
                        pass
                stop.wait(0.05)

        self._spawn(send)

    def close(self) -> None:
        self._stop.set()
        self.stop_state()
        if self._video_stop is not None:
            self._video_stop.set()
        for t in self._threads:
            t.join(1.0)
        self.cmd.close()
        self.out.close()


def _h264_stream(rgb: tuple[int, int, int], frames: int = 5) -> bytes:
    """A short H.264 Annex B stream of solid ``rgb`` 64x48 frames."""
    import av

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = 64, 48, "yuv420p"
    enc.time_base = Fraction(1, 30)
    enc.framerate = Fraction(30, 1)
    enc.options = {"tune": "zerolatency", "preset": "ultrafast"}
    image = np.zeros((48, 64, 3), np.uint8)
    image[...] = rgb
    data = b""
    for i in range(frames):
        frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        data += b"".join(bytes(p) for p in enc.encode(frame))
    data += b"".join(bytes(p) for p in enc.encode(None))
    return data


def _have_h264_encoder() -> bool:
    try:
        import av

        return "libx264" in av.codecs_available
    except ImportError:
        return False


def _shut(ctrl: TelloController) -> None:
    """Stop a test controller without flying anything: no land loop, just close the sockets."""
    ctrl._running = False
    ctrl.state.is_flying = False
    ctrl.tello.is_flying = False
    ctrl.tello.stream_on = False
    ctrl.tello.end()


@pytest.fixture
def pair():
    """Make (controller, fake drone) pairs bound to 127.0.0.1 on their own ports; closed afterwards."""
    made: list[tuple[TelloController, FakeUdpTello]] = []
    fakes: list[FakeUdpTello] = []

    def _make(name: str, battery: int, state: bool = True, **binding_overrides) -> tuple[TelloController, FakeUdpTello]:
        fake = FakeUdpTello(battery)
        fakes.append(fake)
        binding = _binding(fake, **binding_overrides)
        ctrl = TelloController(name=name, host=LOOPBACK, binding=binding)
        made.append((ctrl, fake))
        if state:
            fake.start_state((LOOPBACK, binding.state_port))
        return ctrl, fake

    yield _make
    for ctrl, _ in made:
        _shut(ctrl)
    for fake in fakes:
        fake.close()


# ---------------------------------------------------------------------------
# Isolation: two controllers, two fake drones, one process
# ---------------------------------------------------------------------------


def test_two_controllers_keep_state_and_replies_apart(pair):
    a, drone_a = pair("drone-1", battery=81)
    b, drone_b = pair("drone-2", battery=47)
    drone_a.height_cm = 0
    drone_b.height_cm = 0
    assert a.connect() and b.connect()
    assert a.state.battery == 81
    assert b.state.battery == 47

    # Both drones send from 127.0.0.1. Drone 1 accepts its takeoff after a
    # moment; drone 2 refuses at once. Each controller must read its own.
    def script_a(command: str):
        if command == "takeoff":
            drone_a.height_cm, drone_a.tof_cm = 80, 80
            return [("ok", 0.3)]
        return None

    drone_a.script = script_a
    drone_b.script = lambda command: [("error Motor stop", 0.0)] if command == "takeoff" else None
    results: dict[str, object] = {}

    def fly(name: str, ctrl: TelloController) -> None:
        try:
            results[name] = ctrl.takeoff()
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            results[name] = exc

    threads = [threading.Thread(target=fly, args=(n, c)) for n, c in (("a", a), ("b", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results["a"] is True
    assert isinstance(results["b"], RuntimeError) and "error Motor stop" in str(results["b"])
    assert a.state.is_flying and not b.state.is_flying

    # Each drone saw only its own controller's commands (the keepalive adds
    # more "command"s).
    for drone in (drone_a, drone_b):
        assert drone.commands()[:2] == ["command", "streamon"]
        assert [c for c in drone.commands() if c != "command"] == ["streamon", "takeoff"]
    assert drone_a.sources() == {(LOOPBACK, a.tello.binding.cmd_port)}
    assert drone_b.sources() == {(LOOPBACK, b.tello.binding.cmd_port)}

    # State stays per drone.
    assert _wait_for(lambda: a.read_height_cm() == 80)
    assert b.read_height_cm() == 0
    assert a.read_battery() == 81 and b.read_battery() == 47


def test_every_command_leaves_from_the_one_bound_command_port(pair):
    """The Tello answers only the source port of its first ``command``: keepalive, rc and moves use the same one."""
    ctrl, drone = pair("drone-1", battery=90)
    assert ctrl.connect()
    # The keepalive thread sends "command" as soon as connect() starts it.
    assert _wait_for(lambda: drone.commands().count("command") >= 2)
    ctrl.rc_control(0, 0, 0, 0)
    time.sleep(0.01)  # djitellopy drops an rc sent within 1 ms of the last one
    ctrl.hover()
    drone.height_cm, drone.tof_cm = 80, 80
    assert ctrl.takeoff()
    assert ctrl.move("forward", 50)
    assert _wait_for(lambda: drone.commands().count("rc 0 0 0 0") == 2)
    assert [c for c in drone.commands() if c != "command"] == [
        "streamon", "rc 0 0 0 0", "rc 0 0 0 0", "takeoff", "forward 50",
    ]
    assert drone.sources() == {(LOOPBACK, ctrl.tello.binding.cmd_port)}


def test_no_socket_binds_the_wildcard_address_and_djitellopy_globals_stay_untouched(monkeypatch):
    binds: list[tuple[str, int]] = []

    class RecordingSocket(socket.socket):
        def bind(self, address):
            binds.append(address)
            return super().bind(address)

    def factory() -> socket.socket:
        return RecordingSocket(socket.AF_INET, socket.SOCK_DGRAM)

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"djitellopy's shared client socket was used ({name})")

    monkeypatch.setattr(djitello, "threads_initialized", False)
    monkeypatch.setattr(djitello, "client_socket", Forbidden(), raising=False)
    monkeypatch.setattr(djitello, "drones", {})
    fake = FakeUdpTello(battery=70)
    binding = _binding(fake)
    fake.start_state((LOOPBACK, binding.state_port))
    ctrl = TelloController(name="drone-1", host=LOOPBACK, binding=binding, socket_factory=factory)
    try:
        assert ctrl.connect()
        ctrl.tello.get_frame_read()
        assert djitello.threads_initialized is False
        assert djitello.drones == {}
        assert binds, "nothing was bound"
        assert all(ip == LOOPBACK for ip, _ in binds), binds
        bound_ports = {port for _, port in binds}
        assert {binding.cmd_port, binding.state_port, binding.video_port} <= bound_ports
    finally:
        _shut(ctrl)
        fake.close()


@pytest.mark.skipif(not _have_h264_encoder(), reason="PyAV has no libx264 encoder here")
def test_two_video_streams_decode_to_their_own_controllers(pair):
    a, drone_a = pair("drone-1", battery=80)
    b, drone_b = pair("drone-2", battery=80)
    assert a.connect() and b.connect()
    a.get_frame()  # opens the video readers
    b.get_frame()
    drone_a.start_video((LOOPBACK, a.tello.binding.video_port), (220, 20, 20))
    drone_b.start_video((LOOPBACK, b.tello.binding.video_port), (20, 20, 220))

    def colour(ctrl: TelloController) -> tuple[float, float]:
        frame = ctrl.tello.get_frame_read().frame
        return float(frame[..., 0].mean()), float(frame[..., 2].mean())

    assert _wait_for(lambda: colour(a)[0] > 150 and colour(b)[1] > 150, timeout_s=10)
    red_a, blue_a = colour(a)
    red_b, blue_b = colour(b)
    assert red_a > 150 and blue_a < 80
    assert blue_b > 150 and red_b < 80
    frame = a.get_frame()
    assert frame is not None and frame.shape == (48, 64, 3)


def _loopback_alias_available(ip: str = "127.0.0.2") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.mark.skipif(not _loopback_alias_available(), reason="no 127.0.0.2 on this system (macOS has only 127.0.0.1)")
def test_same_ports_on_two_local_ips_coexist_and_stay_apart():
    """The real layout: both edges use the same port numbers, each bound to its own local IP."""
    ports = {"cmd_port": _free_port(), "state_port": _free_port(), "video_port": _free_port()}
    drones, ctrls = [], []
    try:
        for local_ip, battery in (("127.0.0.1", 81), ("127.0.0.2", 47)):
            drone = FakeUdpTello(battery)
            drones.append(drone)
            binding = TelloBinding(local_ip=local_ip, drone_cmd_port=drone.cmd_port, **ports)
            ctrl = TelloController(name=f"drone-{len(ctrls) + 1}", host=LOOPBACK, binding=binding)
            ctrls.append(ctrl)
            drone.start_state((local_ip, ports["state_port"]))
        assert all(c.connect() for c in ctrls)
        assert [c.state.battery for c in ctrls] == [81, 47]
        assert drones[0].sources() == {("127.0.0.1", ports["cmd_port"])}
        assert drones[1].sources() == {("127.0.0.2", ports["cmd_port"])}
        wildcard = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(OSError):
                wildcard.bind(("0.0.0.0", ports["cmd_port"]))
        finally:
            wildcard.close()
    finally:
        for ctrl in ctrls:
            _shut(ctrl)
        for drone in drones:
            drone.close()


def test_video_reader_restart_rebinds_the_same_port(pair):
    ctrl, _ = pair("drone-1", battery=80)
    reader = ctrl.tello.get_frame_read()
    port = ctrl.tello.binding.video_port
    reader.stop()
    ctrl.tello.background_frame_read = None
    again = ctrl.tello.get_frame_read()
    assert again is not reader
    assert again.address == (LOOPBACK, port)


# ---------------------------------------------------------------------------
# Refusing to start
# ---------------------------------------------------------------------------


def test_two_edges_on_the_same_local_ip_and_port_are_refused(pair):
    a, drone = pair("drone-1", battery=80)
    clash = TelloBinding(
        local_ip=LOOPBACK, cmd_port=a.tello.binding.cmd_port, state_port=_free_port(), video_port=_free_port(),
        drone_cmd_port=drone.cmd_port,
    )
    with pytest.raises(TelloBindingError, match=r"already in use.*same\s+--tello-local-ip"):
        TelloController(name="drone-2", host=LOOPBACK, binding=clash)


def test_a_wildcard_bind_on_a_bound_port_fails(pair):
    """With the edge's command socket bound to one IP, a wildcard bind of the same port is refused."""
    ctrl, _ = pair("drone-1", battery=80)
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(OSError):
            other.bind(("0.0.0.0", ctrl.tello.binding.cmd_port))
    finally:
        other.close()


def test_a_local_ip_on_no_adapter_is_refused():
    # 192.0.2.0/24 (TEST-NET-1) is never assigned to a real adapter.
    with pytest.raises(TelloBindingError, match="not assigned to any network adapter"):
        TelloController(name="drone-1", host="192.168.10.1", local_ip="192.0.2.77")


class _FakeSocket:
    """Records setsockopt and bind calls; ``fail_bind`` raises that OSError on bind."""

    def __init__(self, log: list, fail_bind: OSError | None = None):
        self.log = log
        self.fail_bind = fail_bind
        self.closed = False

    def setsockopt(self, level, option, value):
        self.log.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.log.append(("bind", address))
        if self.fail_bind is not None:
            raise self.fail_bind

    def close(self):
        self.closed = True


def test_a_local_ip_on_two_adapters_is_refused():
    log: list = []
    table = {"Wi-Fi": ["192.168.10.2"], "Wi-Fi 2": ["192.168.10.2"], "Thunderbolt": ["10.42.0.2"]}
    with pytest.raises(TelloBindingError, match=r"more than one adapter \(Wi-Fi, Wi-Fi 2\)"):
        check_binding(TelloBinding("192.168.10.2"), "192.168.10.1",
                      socket_factory=lambda: _FakeSocket(log), interface_table=lambda: table)
    # The only bind was the throwaway probe on an ephemeral port.
    assert log == [("bind", ("192.168.10.2", 0))]


def test_one_adapter_per_ip_passes_and_missing_psutil_is_only_logged(capsys):
    log: list = []
    table = {"Wi-Fi": ["192.168.10.2"], "Wi-Fi 2": ["192.168.10.50"]}
    check_binding(TelloBinding("192.168.10.50"), "192.168.10.1",
                  socket_factory=lambda: _FakeSocket(log), interface_table=lambda: table)
    check_binding(TelloBinding("192.168.10.50"), "192.168.10.1",
                  socket_factory=lambda: _FakeSocket(log), interface_table=lambda: None)
    assert "not checked" in capsys.readouterr().out


@pytest.mark.parametrize("raw", ["0.0.0.0", "", "224.0.0.1", "255.255.255.255", "192.168.10", "::1", "wifi"])
def test_unusable_local_ips_are_refused(raw):
    with pytest.raises(ValueError):
        parse_local_ip(raw)


def test_open_bound_socket_never_binds_the_wildcard():
    for ip in ("", "0.0.0.0"):
        with pytest.raises(TelloBindingError, match="all addresses"):
            open_bound_socket(ip, 8889, purpose="command", socket_factory=lambda: pytest.fail("socket made"))


def test_ifindex_is_refused_off_windows():
    log: list = []
    with pytest.raises(TelloBindingError, match="only supported on Windows"):
        check_binding(TelloBinding("192.168.10.2", ifindex=12), "192.168.10.1",
                      socket_factory=lambda: _FakeSocket(log), interface_table=dict,
                      is_windows=lambda: False)


def test_ifindex_must_name_an_interface_on_windows():
    log: list = []
    common = {"socket_factory": lambda: _FakeSocket(log), "interface_table": dict,
              "is_windows": lambda: True, "interface_indexes": lambda: [1, 12, 23]}
    with pytest.raises(TelloBindingError, match="not an interface"):
        check_binding(TelloBinding("192.168.10.2", ifindex=13), "192.168.10.1", **common)
    check_binding(TelloBinding("192.168.10.2", ifindex=12), "192.168.10.1", **common)


def test_ifindex_sets_ip_unicast_if_in_network_byte_order_before_the_bind():
    log: list = []
    open_bound_socket("192.168.10.2", 8889, purpose="command", ifindex=12, socket_factory=lambda: _FakeSocket(log))
    assert log == [
        ("setsockopt", socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", 12)),
        ("bind", ("192.168.10.2", 8889)),
    ]
    assert IP_UNICAST_IF == 31
    assert struct.pack("!I", 12) == b"\x00\x00\x00\x0c"


def test_bind_errors_become_operator_messages():
    import errno

    for exc, pattern in (
        (OSError(errno.EADDRINUSE, "in use"), "already in use"),
        (OSError(errno.EADDRNOTAVAIL, "not available"), "not assigned"),
    ):
        sock = _FakeSocket([], fail_bind=exc)
        with pytest.raises(TelloBindingError, match=pattern):
            open_bound_socket("192.168.10.2", 8890, purpose="state", socket_factory=lambda s=sock: s)
        assert sock.closed


@pytest.mark.parametrize("raw", ["0", "-3", "x", "1.5", str(2**32)])
def test_bad_ifindex_values_are_refused(raw):
    with pytest.raises(ValueError):
        parse_ifindex(raw)


# ---------------------------------------------------------------------------
# Datagrams from the wrong source
# ---------------------------------------------------------------------------


def test_replies_and_state_from_another_address_are_dropped():
    """A datagram whose source is not the Tello host never reaches the reply list or the state."""
    binding = TelloBinding(local_ip=LOOPBACK, cmd_port=_free_port(), state_port=_free_port(),
                           video_port=_free_port(), drone_cmd_port=_free_port())
    # The host is TEST-NET-1, so every datagram from 127.0.0.1 is foreign.
    client = BoundTello("192.0.2.1", binding, name="drone-1")
    intruder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        intruder.sendto(b"ok", (LOOPBACK, binding.cmd_port))
        intruder.sendto(b"h:80;bat:50;", (LOOPBACK, binding.state_port))
        assert _wait_for(lambda: client.foreign_datagrams == 2)
        assert client.get_own_udp_object()["responses"] == []
        assert client.get_own_udp_object()["state"] == {}
    finally:
        intruder.close()
        client.end()


def test_a_receive_error_does_not_stop_the_receiver():
    """djitellopy's receivers stop for good on the first error; these keep going (for example after
    a Windows ConnectionResetError)."""
    binding = TelloBinding(local_ip=LOOPBACK, cmd_port=_free_port(), state_port=_free_port(),
                           video_port=_free_port(), drone_cmd_port=_free_port())
    client = BoundTello(LOOPBACK, binding, name="drone-1")
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.sendto(b"\xff\xfe not ascii", (LOOPBACK, binding.state_port))  # parse fails
        sender.sendto(b"h:42;bat:77;", (LOOPBACK, binding.state_port))
        assert _wait_for(lambda: client.get_own_udp_object()["state"].get("h") == 42)
    finally:
        sender.close()
        client.end()


# ---------------------------------------------------------------------------
# Takeoff replies (#131), link health (#127) and landing, unchanged
# ---------------------------------------------------------------------------


def test_takeoff_skips_a_stray_reply_and_accepts_the_ok(pair):
    ctrl, drone = pair("drone-1", battery=80)
    assert ctrl.connect()
    drone.script = lambda c: [("unknown command", 0.0), ("ok", 0.2)] if c == "takeoff" else None
    assert ctrl.takeoff() is True
    assert drone.commands().count("takeoff") == 1


def test_takeoff_bare_error_then_airborne_state_counts_as_takeoff(pair):
    ctrl, drone = pair("drone-1", battery=80)
    assert ctrl.connect()

    def script(command: str):
        if command == "takeoff":
            def lift():
                drone.height_cm, drone.tof_cm = 80, 80

            threading.Timer(0.3, lift).start()
            return [("error", 0.05)]
        return None

    drone.script = script
    assert ctrl.takeoff() is True
    assert drone.commands().count("takeoff") == 1


def test_takeoff_refusal_with_detail_fails_at_once(pair):
    ctrl, drone = pair("drone-1", battery=80)
    assert ctrl.connect()
    drone.script = lambda c: [("error No valid imu", 0.0)] if c == "takeoff" else None
    with pytest.raises(RuntimeError, match="No valid imu"):
        ctrl.takeoff()
    assert not ctrl.state.is_flying


def test_link_health_names_the_adapter_when_state_stops(pair, monkeypatch):
    monkeypatch.setattr(tello_mod, "_LINK_STATE_MAX_AGE_S", 0.5)
    ctrl, drone = pair("drone-1", battery=80)
    assert ctrl.connect()
    assert ctrl.link_status().ok
    drone.stop_state()
    assert _wait_for(lambda: not ctrl.link_status().ok, timeout_s=3)
    status = ctrl.link_status()
    assert status.reason == "state_stale"
    assert status.detail.startswith("Tello unreachable (no state for")
    assert status.detail.endswith(f"via {LOOPBACK}")


def test_link_health_no_state_and_long_detail(pair):
    ctrl, _ = pair("drone-1", battery=80, state=False)
    ctrl.state.is_connected = True
    status = ctrl.link_status()
    assert (status.ok, status.reason) == (False, "no_state")
    assert status.detail == f"Tello unreachable (no state received) via {LOOPBACK}"
    ctrl._note_command_outcome("x" * 400)
    ctrl._note_state_packet({"h": "0"})
    detail = ctrl.link_status().detail
    assert len(detail) <= tello_mod._LINK_DETAIL_MAX_LEN
    assert detail.endswith(f"... via {LOOPBACK}")


def test_land_is_sent_while_the_link_is_unhealthy_and_confirmed(pair, monkeypatch):
    """Landing is never gated on link health: with the state stream lost, the land still goes out
    and its own fresh ``ok`` confirms it."""
    monkeypatch.setattr(tello_mod, "_LINK_STATE_MAX_AGE_S", 0.3)
    monkeypatch.setattr(tello_mod, "_STATE_LOST_S", 0.3)
    ctrl, drone = pair("drone-1", battery=80)
    assert ctrl.connect()
    drone.height_cm, drone.tof_cm = 80, 80
    assert ctrl.takeoff()
    drone.stop_state()
    assert _wait_for(lambda: not ctrl.link_status().ok, timeout_s=3)
    ctrl.land()
    assert "land" in drone.commands()
    assert not ctrl.state.is_flying
    assert drone.sources() == {(LOOPBACK, ctrl.tello.binding.cmd_port)}


# ---------------------------------------------------------------------------
# Shutdown and the djitellopy surface
# ---------------------------------------------------------------------------


def test_end_closes_every_socket_and_is_idempotent(pair):
    ctrl, _ = pair("drone-1", battery=80)
    ctrl.tello.get_frame_read()
    b = ctrl.tello.binding
    ctrl.tello.end()
    ctrl.tello.end()
    for port in (b.cmd_port, b.state_port, b.video_port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            assert _wait_for(lambda s=s, port=port: _binds(s, port), timeout_s=2)
        finally:
            s.close()


def _binds(sock: socket.socket, port: int) -> bool:
    try:
        sock.bind((LOOPBACK, port))
        return True
    except OSError:
        return False


def test_bound_tello_overrides_every_djitellopy_method_that_uses_shared_sockets_or_tables():
    """If a future djitellopy adds a method that uses its module-level socket or table, this fails."""
    shared = ("client_socket", "drones", "BackgroundFrameRead", "VS_UDP_IP", "threads_initialized")
    uses_shared = []
    for name, member in inspect.getmembers(djitello.Tello):
        if not callable(member):
            continue
        try:
            source = inspect.getsource(member)
        except (OSError, TypeError):
            continue
        if any(word in source for word in shared):
            uses_shared.append(name)
    assert "send_command_with_return" in uses_shared  # the scan works
    not_overridden = [name for name in uses_shared if name not in BoundTello.__dict__]
    assert not_overridden == []


def test_default_controller_still_uses_the_stock_djitellopy_client(tello_on_wire):
    ctrl, _wire = tello_on_wire()
    assert type(ctrl.tello) is djitello.Tello
    assert ctrl.local_ip is None


# ---------------------------------------------------------------------------
# Edge CLI: --tello-local-ip and --tello-ifindex
# ---------------------------------------------------------------------------


def _args(source: str = "tello", **fields) -> argparse.Namespace:
    base = {"source": source, "tello_local_ip": None, "tello_ifindex": None, "drone_id": "drone-1", "tello_host": None}
    base.update(fields)
    return argparse.Namespace(**base)


def test_resolve_binding_flag_env_and_defaults():
    from flightrisk.edge_device import _resolve_tello_binding

    assert _resolve_tello_binding(_args(), environ={}) == (None, None)
    assert _resolve_tello_binding(_args(tello_local_ip="192.168.10.2"), environ={}) == ("192.168.10.2", None)
    env = {"FLIGHTRISK_TELLO_LOCAL_IP": " 192.168.10.50 ", "FLIGHTRISK_TELLO_IFINDEX": "23"}
    assert _resolve_tello_binding(_args(), environ=env) == ("192.168.10.50", 23)
    # The flag wins over the environment.
    assert _resolve_tello_binding(_args(tello_local_ip="192.168.10.2", tello_ifindex="12"), environ=env) == (
        "192.168.10.2", 12,
    )
    # Other sources ignore the environment and refuse the flags.
    assert _resolve_tello_binding(_args("fake"), environ=env) == (None, None)
    with pytest.raises(ValueError, match="only apply to --source tello"):
        _resolve_tello_binding(_args("fake", tello_local_ip="192.168.10.2"), environ={})
    with pytest.raises(ValueError, match="needs --tello-local-ip"):
        _resolve_tello_binding(_args(tello_ifindex="12"), environ={})
    with pytest.raises(ValueError, match="IPv4"):
        _resolve_tello_binding(_args(tello_local_ip="tello"), environ={})
    with pytest.raises(ValueError, match="not 0.0.0.0"):
        _resolve_tello_binding(_args(tello_local_ip="0.0.0.0"), environ={})


def test_main_refuses_a_bad_local_ip_before_anything_starts(monkeypatch, capsys):
    from flightrisk import edge_device

    monkeypatch.setattr(edge_device, "_run_flight_edge", lambda *a, **k: pytest.fail("edge started"))
    with pytest.raises(SystemExit) as exc:
        edge_device.main(["--source", "fake", "--tello-local-ip", "192.168.10.2"])
    assert exc.value.code == 2
    assert "only apply to --source tello" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        edge_device.main(["--source", "tello", "--tello-local-ip", "192.0.2.77"])
    assert "not assigned to any network adapter" in str(exc.value.code)


def test_build_drone_default_path_is_unchanged(monkeypatch):
    from flightrisk import edge_device
    from flightrisk.observability import StructuredLogger

    calls: list = []

    class Recorder:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(tello_mod, "TelloController", Recorder)
    log = StructuredLogger(component="test")
    edge_device._build_drone(argparse.Namespace(source="tello", drone_id="drone-1", tello_host=None), log)
    edge_device._build_drone(_args(), log)
    assert calls == [((), {"name": "drone-1", "host": None})] * 2
    edge_device._build_drone(_args(tello_local_ip="192.168.10.2", tello_ifindex=12, drone_id="drone-2"), log)
    assert calls[-1] == ((), {"name": "drone-2", "host": None, "local_ip": "192.168.10.2", "ifindex": 12})


def test_build_drone_turns_a_binding_error_into_an_exit(monkeypatch):
    from flightrisk import edge_device
    from flightrisk.observability import StructuredLogger

    def refuse(*args, **kwargs):
        raise TelloBindingError("UDP 192.168.10.2:8889 (command) is already in use.")

    monkeypatch.setattr(tello_mod, "TelloController", refuse)
    with pytest.raises(SystemExit, match="already in use.*Refusing to start"):
        edge_device._build_drone(_args(tello_local_ip="192.168.10.2"), StructuredLogger(component="test"))
