"""The dashboard's Socket.IO server behaves as docs/mobile-contract.md says, over a real WebSocket.

The phone apps talk to the dashboard with their own small Engine.IO v4 /
Socket.IO v5 clients, so these tests do the same: they start the real
dashboard app on 127.0.0.1 with an OS-assigned port and ``FLIGHTRISK_API_KEY``
set, open ``ws://127.0.0.1:<port>/socket.io/?EIO=4&transport=websocket`` with
a plain WebSocket client (``simple_websocket``, no Origin header), speak the
protocol frame by frame and record every frame. They check that:

* a connect without the key or with a wrong key is refused with the
  ``connect_error`` frame of ``socketio_frames.json``;
* the right key is accepted, the open and connect frames have the shapes of
  ``socketio_frames.json``, and ``status`` arrives at once;
* ``get_fleet_status``, ``land_all``, ``drone_command`` (land) and
  ``manual_resume_search`` are answered with the payload shapes of the
  fixtures, in both the simulated (no fleet) and the fleet configuration;
* unknown event names are ignored and the connection stays up;
* acks, pings and pongs follow the frames in ``socketio_frames.json``;
* no Origin header is needed, and a foreign Origin is refused.

Only throwaway loopback ports and temporary files are used: never the live
ground on 5555/9000, no edge, no drone and no packet to 192.168.10.1.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

import pytest
import simple_websocket
from engineio import packet as eio_packet
from socketio import packet as sio_packet
from werkzeug.serving import make_server

from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import FleetSupervisor
from flightrisk.dashboard.state import app_state
from flightrisk.drone.fake import FakeDrone
from flightrisk.drone.fleet import DroneFleet
from tests.test_fleet_dashboard import FakeClock, FakeGroundTransport

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mobile_contract"
KEY = "test-key-live"
WAIT_S = 10.0


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["cases"]


FRAMES = _fixture("socketio_frames.json")


def _payload(name: str, case: str):
    return _fixture(name)[case]["payload"]


# ---------------------------------------------------------------------------
# A minimal Engine.IO v4 / Socket.IO v5 client that records every frame
# ---------------------------------------------------------------------------


class RawClient:
    """The protocol subset a phone implements, over one WebSocket, with every frame recorded."""

    def __init__(self, port: int, *, headers: dict | None = None, answer_pings: bool = True):
        url = f"ws://127.0.0.1:{port}/socket.io/?EIO=4&transport=websocket"
        self.ws = simple_websocket.Client.connect(url, headers=headers)
        self.frames: list[tuple[str, str]] = []
        self.events: list[tuple[str, object]] = []
        self.acks: list[tuple[int, object]] = []
        self.answer_pings = answer_pings
        self.connect_errors: list[object] = []
        self.sid: str | None = None
        self.open = self._read_open()

    # -- low level ------------------------------------------------------

    def send(self, text: str) -> None:
        self.frames.append(("client_to_server", text))
        self.ws.send(text)

    def recv(self, timeout: float = WAIT_S) -> str | None:
        text = self.ws.receive(timeout=timeout)
        if text is None:
            return None
        self.frames.append(("server_to_client", text))
        pkt = eio_packet.Packet(encoded_packet=text)
        if pkt.packet_type == eio_packet.PING and self.answer_pings:
            self.send(eio_packet.Packet(eio_packet.PONG).encode())
        elif pkt.packet_type == eio_packet.MESSAGE:
            sio = sio_packet.Packet(encoded_packet=pkt.data)
            if sio.packet_type == sio_packet.EVENT:
                self.events.append((sio.data[0], sio.data[1] if len(sio.data) > 1 else None))
            elif sio.packet_type == sio_packet.ACK:
                self.acks.append((sio.id, sio.data))
            elif sio.packet_type == sio_packet.CONNECT:
                self.sid = sio.data["sid"]
            elif sio.packet_type == sio_packet.CONNECT_ERROR:
                self.connect_errors.append(sio.data)
        return text

    def _read_open(self) -> dict:
        text = self.recv()
        assert text is not None and text.startswith("0"), text
        return json.loads(text[1:])

    def close(self) -> None:
        with contextlib.suppress(simple_websocket.ConnectionClosed, simple_websocket.ConnectionError, OSError):
            self.ws.close()

    # -- Socket.IO ------------------------------------------------------

    def connect(self, auth: dict | None) -> None:
        self.send(eio_packet.Packet(eio_packet.MESSAGE, data=sio_packet.Packet(sio_packet.CONNECT, data=auth).encode()).encode())

    def emit(self, event: str, payload=None, ack_id: int | None = None) -> None:
        data = [event] if payload is None else [event, payload]
        sio = sio_packet.Packet(sio_packet.EVENT, data=data, id=ack_id)
        self.send(eio_packet.Packet(eio_packet.MESSAGE, data=sio.encode()).encode())

    def wait_for(self, predicate, timeout: float = WAIT_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = predicate()
            if found:
                return found
            self.recv(timeout=max(0.05, deadline - time.monotonic()))
        return predicate()

    def wait_event(self, name: str, after: int = 0, timeout: float = WAIT_S):
        def found():
            matches = [payload for event, payload in self.events[after:] if event == name]
            return matches[-1] if matches else None

        result = self.wait_for(lambda: found() is not None and [found()], timeout)
        assert result, f"no {name!r} event within {timeout} s; got {[e for e, _ in self.events]}"
        return result[0]

    def connect_ok(self) -> None:
        self.connect({"api_key": KEY})
        assert self.wait_for(lambda: self.sid is not None), self.frames
        self.wait_event("status")


# ---------------------------------------------------------------------------
# Server fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch, tmp_path, clean_app_state):
    """The real dashboard app on 127.0.0.1:<free port>, API key set; yields a function that configures app_state."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FLIGHTRISK_CEILING_FILE", str(tmp_path / "ceiling.json"))
    monkeypatch.setenv("FLIGHTRISK_API_KEY", KEY)
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", KEY)
    app_state.logger = None
    app_state.target_photo = None
    app_state.fleet_supervisor = None
    app_state.ground_transport = None
    app_state.fleet = None
    app_state.profile_board = None
    app_state.ceiling_board = None
    httpd = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, name="mobile-contract-dashboard", daemon=True)
    thread.start()
    clients: list[RawClient] = []

    def connect(**kwargs) -> RawClient:
        client = RawClient(httpd.server_port, **kwargs)
        clients.append(client)
        return client

    connect.port = httpd.server_port
    try:
        yield connect
    finally:
        for client in clients:
            client.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def sim_ground(server):
    """A ground without a fleet, with one simulated local drone (FakeDrone, no network)."""
    fleet = DroneFleet(factory=lambda drone_id, host: FakeDrone(name=drone_id, host=host, sleep=lambda s: None))
    assert fleet.register("drone-1", host="127.0.0.1")
    app_state.source = "sim"
    app_state.fleet = fleet
    app_state.appearance_only = False
    return server


@pytest.fixture
def fleet_ground(server):
    """A ground with a two-drone fleet over fake edge links (no transport, no edge)."""
    clock = FakeClock()
    transport = FakeGroundTransport(("drone-1", "drone-2"), clock)
    supervisor = FleetSupervisor(transport, ("drone-1", "drone-2"), app_module.socketio, area_cm=400, strip_cm=75,
                                 clock=clock, retask_wait_s=0.0)
    app_state.source = "edge"
    app_state.fleet_supervisor = supervisor
    app_state.ground_transport = transport
    app_state.appearance_only = True
    return server


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_connect_without_a_key_is_refused(sim_ground):
    client = sim_ground()
    client.connect(None)
    assert client.wait_for(lambda: client.connect_errors), client.frames
    assert ("server_to_client", FRAMES["socketio_connect_error"]["frame"]) in client.frames
    assert client.sid is None
    assert not [e for e, _ in client.events]


def test_connect_with_a_wrong_key_is_refused(sim_ground):
    client = sim_ground()
    client.connect({"api_key": "wrong-key"})
    assert client.wait_for(lambda: client.connect_errors), client.frames
    assert ("server_to_client", FRAMES["socketio_connect_error"]["frame"]) in client.frames
    assert client.sid is None


def test_the_right_key_is_accepted_and_status_arrives_at_once(sim_ground):
    client = sim_ground()
    client.connect_ok()
    status = client.wait_event("status")
    assert set(status) == set(_payload("status.json", "no_fleet_sim_source"))
    assert status["connected"] is True and status["source"] == "sim"


def test_no_origin_header_is_needed_and_a_foreign_origin_is_refused(sim_ground):
    client = sim_ground()  # simple_websocket sends no Origin header
    client.connect_ok()
    # Engine.IO checks the origin only when the request carries one; a foreign one is rejected with HTTP 400.
    with pytest.raises(simple_websocket.ConnectionError) as refused:
        sim_ground(headers={"Origin": "http://evil.example"})
    assert refused.value.status_code == 400


# ---------------------------------------------------------------------------
# Frame shapes against socketio_frames.json
# ---------------------------------------------------------------------------


def test_open_and_connect_frames_have_the_fixture_shapes(sim_ground):
    client = sim_ground()
    expected = FRAMES["engineio_open"]["decoded"]
    assert set(client.open) == set(expected)
    for key in ("pingInterval", "pingTimeout", "maxPayload", "upgrades"):
        assert client.open[key] == expected[key], key
    assert isinstance(client.open["sid"], str) and client.open["sid"]
    client.connect_ok()
    sent = [text for direction, text in client.frames if direction == "client_to_server"]
    assert sent[0] == FRAMES["socketio_connect_with_auth"]["frame"].replace("test-key", KEY)
    received = [text for direction, text in client.frames if direction == "server_to_client"]
    connect_ack = next(text for text in received if text.startswith("40"))
    assert set(json.loads(connect_ack[2:])) == set(FRAMES["socketio_connect_ack"]["decoded"])
    status_frame = next(text for text in received if text.startswith('42["status"'))
    assert status_frame.startswith(FRAMES["socketio_event_server_to_client"]["frame"][:2])


def test_an_event_with_an_ack_id_gets_the_fixture_ack(sim_ground):
    client = sim_ground()
    client.connect_ok()
    client.send(FRAMES["socketio_event_with_ack_id"]["frame"])
    assert client.wait_for(lambda: client.acks), client.frames
    assert ("server_to_client", FRAMES["socketio_ack"]["frame"]) in client.frames


def test_the_server_pings_and_a_pong_keeps_the_connection(sim_ground, monkeypatch):
    eio = app_module.socketio.server.eio
    monkeypatch.setattr(eio, "ping_interval", 1)
    client = sim_ground()
    assert client.open["pingInterval"] == 1000
    client.connect_ok()
    assert client.wait_for(lambda: ("server_to_client", FRAMES["engineio_ping"]["frame"]) in client.frames, 5)
    assert ("client_to_server", FRAMES["engineio_pong"]["frame"]) in client.frames
    after = len(client.events)
    client.emit("get_fleet_status")
    client.wait_event("fleet_status", after=after)


def test_disconnect_frames_are_accepted(sim_ground):
    client = sim_ground()
    client.connect_ok()
    client.send(FRAMES["socketio_disconnect"]["frame"])
    client.send(FRAMES["engineio_close"]["frame"])
    deadline = time.monotonic() + WAIT_S
    with pytest.raises(simple_websocket.ConnectionClosed):
        while time.monotonic() < deadline:
            client.ws.receive(timeout=0.2)
    assert not client.ws.connected


# ---------------------------------------------------------------------------
# Events a phone emits, simulated ground (no fleet)
# ---------------------------------------------------------------------------


def test_sim_ground_answers_the_allowed_events(sim_ground):
    client = sim_ground()
    client.connect_ok()

    after = len(client.events)
    client.emit("get_fleet_status")
    fleet_status = client.wait_event("fleet_status", after=after)
    assert set(fleet_status) == set(_payload("fleet_status.json", "non_fleet_local_drone"))
    expected_drone = _payload("fleet_status.json", "non_fleet_local_drone")["drones"]["drone-1"]
    assert set(fleet_status["drones"]["drone-1"]) == set(expected_drone)

    after = len(client.events)
    client.emit("drone_command", {"command": "land", "drone_id": "drone-1"})
    assert client.wait_event("command_ack", after=after) == _payload("command_ack.json", "land")

    after = len(client.events)
    client.emit("land_all")
    ack = client.wait_event("land_all_ack", after=after)
    assert set(ack) == set(_payload("land_all_ack.json", "local_fleet"))
    assert ack["local"] == 1 and ack["reached_any"] is True

    after = len(client.events)
    client.emit("get_ceiling_settings")
    assert client.wait_event("ceiling_settings", after=after) == _payload("ceiling_settings.json", "unavailable")

    after = len(client.events)
    client.emit("get_detection_profiles")
    assert client.wait_event("detection_profiles", after=after) == _payload("detection_profiles.json", "no_board")

    after = len(client.events)
    client.emit("get_reasoning_provider")
    reasoning = client.wait_event("reasoning_provider", after=after)
    assert set(reasoning) == set(_payload("reasoning_provider.json", "local_gemma_cloud_blocked"))


def test_unknown_event_names_are_ignored(sim_ground):
    client = sim_ground()
    client.connect_ok()
    before = len(client.events)
    client.emit("definitely_not_an_event", {"x": 1})
    client.emit("get_fleet_status")
    client.wait_event("fleet_status", after=before)
    names = [event for event, _ in client.events[before:]]
    assert "error" not in names
    assert client.ws.connected


# ---------------------------------------------------------------------------
# Events a phone emits, fleet ground
# ---------------------------------------------------------------------------


def test_fleet_ground_answers_the_allowed_events(fleet_ground):
    client = fleet_ground()
    client.connect_ok()
    status = client.events[0][1]
    assert status["fleet"]["drones"] == 2

    after = len(client.events)
    client.emit("get_fleet_status")
    fleet_status = client.wait_event("fleet_status", after=after)
    assert set(fleet_status) == set(_payload("fleet_status.json", "all_connected"))
    expected_drone = set(_payload("fleet_status.json", "all_connected")["drones"]["drone-1"])
    # rel_x_cm/rel_y_cm and the telemetry values appear once an edge has reported; nothing has here.
    assert set(fleet_status["drones"]["drone-1"]) == expected_drone - {"rel_x_cm", "rel_y_cm"}
    assert fleet_status["drones"]["drone-1"]["connected"] is True

    after = len(client.events)
    client.emit("get_manual_status")
    manual = client.wait_event("manual_status", after=after)
    assert set(manual) == set(_payload("manual_status.json", "held_orphan_countdown"))

    after = len(client.events)
    client.emit("drone_command", {"command": "land", "drone_id": "drone-2"})
    assert client.wait_event("manual_ack", after=after) == {**_payload("manual_ack.json", "land_ok"), "drone_id": "drone-2"}

    after = len(client.events)
    client.emit("manual_resume_search", {"drone_id": "drone-1"})
    refused = client.wait_event("manual_ack", after=after)
    assert refused == _payload("manual_ack.json", "resume_search_not_manual")
    assert client.wait_event("error", after=after) == _payload("error.json", "manual_refusal")

    after = len(client.events)
    client.emit("land_all")
    ack = client.wait_event("land_all_ack", after=after)
    assert ack == _payload("land_all_ack.json", "fleet_reached_all")
    event = client.wait_event("fleet_event", after=after)
    assert event == _payload("fleet_event.json", "land_all")


# ---------------------------------------------------------------------------
# The python-socketio client, when its WebSocket dependency is installed
# ---------------------------------------------------------------------------


def test_python_socketio_client_over_websocket(sim_ground):
    pytest.importorskip("websocket", reason="websocket-client is not installed; the raw client covers the wire")
    import socketio

    port = sim_ground.port
    client = socketio.Client(reconnection=False)
    received = {}
    client.on("status", lambda data: received.setdefault("status", data))
    client.on("fleet_status", lambda data: received.setdefault("fleet_status", data))
    client.connect(f"http://127.0.0.1:{port}", transports=["websocket"], auth={"api_key": KEY}, wait_timeout=WAIT_S)
    try:
        client.emit("get_fleet_status")
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline and "fleet_status" not in received:
            time.sleep(0.05)
        assert "status" in received and "fleet_status" in received
    finally:
        client.disconnect()
