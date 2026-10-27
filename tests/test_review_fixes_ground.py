"""Tests for the ground-station and security fixes from the PR #87 review (R-111).

* N1: Socket.IO accepts only the dashboard's own origins by default, not
  ``*``. ``FLIGHTRISK_CORS_ORIGINS`` still overrides that for development,
  and the dashboard page itself still connects.
* N3: the ground transport refuses a second live connection that claims a
  ``drone_id`` already in use, and still lets a drone reconnect after its
  old connection closed or stopped answering.
* N6: LAND ALL starts the transport broadcast at once, so a coordinator send
  stalled on one link cannot delay the land order to the other drones.

The N4 hotkey is checked statically in ``tests/e2e/test_map_ui.py``.

No real drone is involved. The transport tests run real ``websockets``
connections on 127.0.0.1 with OS-assigned ports, driven with plain
``asyncio.run()`` like ``tests/test_fleet_protocol.py``. Every timing
assertion has a generous margin because many test processes may share the
machine.
"""

import asyncio
import json
import logging
import re
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.app import (
    CORS_ORIGINS_ENV,
    FleetSupervisor,
    SourceConfig,
    _land_all,
    app,
    run_dashboard,
    socketio,
    socketio_origin_allowed,
)
from flightrisk.dashboard.state import app_state
from flightrisk.transport import EdgeTransport, GroundTransport

WAIT_S = 10.0
TOKEN = "review-fixes-token"


# ===========================================================================
# N1: Socket.IO origins
# ===========================================================================

POLLING = "/socket.io/?EIO=4&transport=polling"


@pytest.fixture
def loopback_dashboard(clean_app_state, monkeypatch):
    """The dashboard as configured by default: loopback bind, no key, no CORS override."""
    monkeypatch.delenv(CORS_ORIGINS_ENV, raising=False)
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", None)
    monkeypatch.setattr(app_module, "_dashboard_bind", ("127.0.0.1", 5555))
    app_state.fleet_supervisor = None
    app_state.ground_transport = None
    app_state.fleet = None
    app_state.logger = None
    return app.test_client()


def _handshake(client, origin=None, host=None):
    """Open an Engine.IO polling session; returns the response."""
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    return client.get(POLLING, headers=headers)


def _session_id(response) -> str:
    body = response.get_data(as_text=True)
    assert body.startswith("0"), body
    return json.loads(body[1:])["sid"]


def _close_session(client, sid, origin=None):
    headers = {"Origin": origin} if origin else {}
    client.post(f"{POLLING}&sid={sid}", data="1", headers=headers, content_type="text/plain")


def test_socketio_cors_not_wildcard_by_default(loopback_dashboard):
    client = loopback_dashboard
    assert socketio.server.eio.cors_allowed_origins != "*"

    # A page from another site, open in this machine's browser, is refused.
    for origin in ("http://evil.example", "https://evil.example", "http://127.0.0.1:8080", "null"):
        response = _handshake(client, origin=origin)
        assert response.status_code == 400, origin
        assert "Access-Control-Allow-Origin" not in response.headers
        assert socketio_origin_allowed(origin) is False

    # A DNS-rebinding page reaches 127.0.0.1 under its own name, so its
    # Origin matches its Host header. A loopback bind still refuses it.
    response = _handshake(client, origin="http://evil.example:5555", host="evil.example:5555")
    assert response.status_code == 400

    # The dashboard's own origins are accepted, under every loopback spelling.
    for origin in ("http://127.0.0.1:5555", "http://localhost:5555", "http://[::1]:5555"):
        response = _handshake(client, origin=origin)
        assert response.status_code == 200, origin
        assert response.headers["Access-Control-Allow-Origin"] == origin
        _close_session(client, _session_id(response), origin)

    # Clients that send no Origin (scripts, test clients) are not affected.
    response = _handshake(client)
    assert response.status_code == 200
    _close_session(client, _session_id(response))


@pytest.fixture
def real_socketio_packets():
    """Send Socket.IO packets over Engine.IO again for the duration of a test.

    ``SocketIOTestClient`` (used by other tests in this process) replaces the
    server's packet senders with recorders for its own clients, so packets
    for a real polling session would otherwise be dropped.
    """
    server = socketio.server
    saved = {name: server.__dict__.pop(name) for name in ("_send_packet", "_send_eio_packet")
             if name in server.__dict__}
    yield
    server.__dict__.update(saved)


def test_dashboard_page_still_connects(loopback_dashboard, real_socketio_packets):
    """A Socket.IO connect from the page's own origin completes and gets the status event."""
    client = loopback_dashboard
    origin = "http://127.0.0.1:5555"
    sid = _session_id(_handshake(client, origin=origin))
    try:
        posted = client.post(f"{POLLING}&sid={sid}", data="40", headers={"Origin": origin},
                             content_type="text/plain")
        assert posted.status_code == 200
        received = ""
        deadline = time.monotonic() + WAIT_S
        while '"status"' not in received and time.monotonic() < deadline:
            polled = client.get(f"{POLLING}&sid={sid}", headers={"Origin": origin})
            assert polled.status_code == 200
            received += polled.get_data(as_text=True)
        assert re.search(r"(^|\x1e)40\{", received), received  # Socket.IO connect accepted
        assert '"status"' in received and '"connected":true' in received
    finally:
        _close_session(client, sid, origin)


def test_allowed_origins_follow_the_dashboard_bind(clean_app_state, monkeypatch):
    monkeypatch.delenv(CORS_ORIGINS_ENV, raising=False)
    monkeypatch.delenv("FLIGHTRISK_DASHBOARD_HOST", raising=False)
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", None)
    monkeypatch.setattr(app_module, "_dashboard_bind", None)
    monkeypatch.setattr(app_module, "_init_pipeline", MagicMock())
    monkeypatch.setattr(app_module, "_frame_loop", lambda socketio: None)
    monkeypatch.setattr(app_module.socketio, "run", MagicMock())

    run_dashboard(SourceConfig(source="webcam"), port=5999)

    assert socketio_origin_allowed("http://127.0.0.1:5999") is True
    assert socketio_origin_allowed("http://localhost:5999") is True
    assert socketio_origin_allowed("http://127.0.0.1:5555") is False

    # A specific non-loopback bind (which requires the API key) adds its own
    # address, and a wildcard bind accepts the page under the Host it was
    # requested from, because every connection must then present the key.
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", "venue-key")
    run_dashboard(SourceConfig(source="webcam"), port=5999, host="192.168.1.20")
    assert socketio_origin_allowed("http://192.168.1.20:5999") is True
    assert socketio_origin_allowed("http://127.0.0.1:5999") is True
    assert socketio_origin_allowed("http://evil.example") is False

    run_dashboard(SourceConfig(source="webcam"), port=5999, host="0.0.0.0")
    environ = {"HTTP_HOST": "mac.local:5999", "wsgi.url_scheme": "http"}
    assert socketio_origin_allowed("http://mac.local:5999", environ) is True
    assert socketio_origin_allowed("http://evil.example", environ) is False


def test_cors_env_override_for_development(loopback_dashboard, monkeypatch):
    client = loopback_dashboard
    monkeypatch.setenv(CORS_ORIGINS_ENV, "http://dev.example:3000, http://other.example")
    assert socketio_origin_allowed("http://dev.example:3000") is True
    assert socketio_origin_allowed("http://other.example") is True
    # The override replaces the default list.
    assert socketio_origin_allowed("http://127.0.0.1:5555") is False
    response = _handshake(client, origin="http://dev.example:3000")
    assert response.status_code == 200
    _close_session(client, _session_id(response), "http://dev.example:3000")
    assert _handshake(client, origin="http://evil.example").status_code == 400

    monkeypatch.setenv(CORS_ORIGINS_ENV, "*")
    assert socketio_origin_allowed("http://evil.example") is True

    # Empty means unset: back to the dashboard's own origins.
    monkeypatch.setenv(CORS_ORIGINS_ENV, " ")
    assert socketio_origin_allowed("http://evil.example") is False
    assert socketio_origin_allowed("http://127.0.0.1:5555") is True


# ===========================================================================
# N3: duplicate drone ids on the ground transport
# ===========================================================================

def _telemetry(drone_id: str, battery: int = 90) -> dict:
    return fp.make_telemetry(
        drone_id=drone_id, battery=battery, height_cm=0, is_flying=False,
        rel_x_cm=0.0, rel_y_cm=0.0, heading_deg=0.0, lane_index=None,
        lanes_total=None, link_age_s=None, takeoff_at_local_s=None,
    )


async def _until(predicate, timeout: float = WAIT_S, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


async def _start_ground(**kwargs) -> GroundTransport:
    kwargs.setdefault("token", TOKEN)
    gt = GroundTransport(host="127.0.0.1", port=0, **kwargs)
    await gt.start()
    return gt


async def _connect_edge(gt: GroundTransport) -> EdgeTransport:
    # A long reconnect delay keeps a refused edge from retrying during the test.
    et = EdgeTransport(ws_url=f"ws://127.0.0.1:{gt.port}", token=TOKEN, base_delay=60.0)
    await et.connect()
    return et


async def _recv_type(et: EdgeTransport, mtype: str, timeout: float = WAIT_S) -> dict | None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        msg = await et.recv(timeout=remaining)
        if msg is None and not et.connected:
            return None
        if msg is not None and msg.get("type") == mtype:
            return msg


def test_duplicate_drone_id_socket_refused(caplog):
    caplog.set_level(logging.WARNING, logger="flightrisk.transport")
    controls = []

    async def scenario():
        # A generous ping timeout: the live first connection must not be
        # mistaken for a dropped one on a loaded machine.
        gt = await _start_ground(on_control=controls.append, duplicate_ping_timeout_s=5.0)
        first = await _connect_edge(gt)
        second = await _connect_edge(gt)
        try:
            assert await first.send(_telemetry("drone-1", battery=81))
            assert await _until(lambda: gt.drone_ids == ["drone-1"])
            first_socket = gt._drone_sockets["drone-1"]

            # The second edge claims the same drone id while the first is live.
            assert await second.send(_telemetry("drone-1", battery=22))
            assert await _until(lambda: not second.connected)
            assert second._ws.close_code == 4009
            assert "Refusing a second connection for drone 'drone-1'" in caplog.text

            # Routing stayed with the first connection, and the refused
            # edge's telemetry never reached the fleet.
            assert gt._drone_sockets["drone-1"] is first_socket
            assert gt.drone_ids == ["drone-1"]
            assert await gt.send_to_drone("drone-1", fp.make_land()) is True
            assert await _recv_type(first, "land") == {"type": "land"}
            assert await _until(lambda: [c["battery"] for c in controls] == [81])

            # The first edge keeps working; nothing swaps on its next message.
            assert await first.send(_telemetry("drone-1", battery=80))
            assert await _until(lambda: [c["battery"] for c in controls] == [81, 80])
            assert gt._drone_sockets["drone-1"] is first_socket
            assert "moved to a new connection" not in caplog.text
        finally:
            await second.disconnect()
            await first.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_reconnect_after_a_closed_connection_takes_the_drone_id():
    async def scenario():
        gt = await _start_ground(duplicate_ping_timeout_s=5.0)
        first = await _connect_edge(gt)
        try:
            assert await first.send(_telemetry("drone-1"))
            assert await _until(lambda: gt.drone_ids == ["drone-1"])
            await first.disconnect()
            assert await _until(lambda: gt.drone_ids == [])

            again = await _connect_edge(gt)
            try:
                assert await again.send(_telemetry("drone-1"))
                assert await _until(lambda: gt.drone_ids == ["drone-1"])
                assert again.connected
                assert await gt.send_to_drone("drone-1", fp.make_land()) is True
                assert await _recv_type(again, "land") == {"type": "land"}
            finally:
                await again.disconnect()
        finally:
            await first.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_reconnect_over_a_dropped_link_takes_the_drone_id(caplog):
    """An old connection that is still open but answers no ping does not block the reconnect."""
    caplog.set_level(logging.WARNING, logger="flightrisk.transport")

    async def scenario():
        gt = await _start_ground(duplicate_ping_timeout_s=0.5)
        first = await _connect_edge(gt)
        again = None
        try:
            assert await first.send(_telemetry("drone-1"))
            assert await _until(lambda: gt.drone_ids == ["drone-1"])
            stale = gt._drone_sockets["drone-1"]

            # The link dropped without a TCP reset: pings go out, no pong returns.
            async def ping_without_pong(data=None):
                return asyncio.get_running_loop().create_future()
            stale.ping = ping_without_pong

            again = await _connect_edge(gt)
            assert await again.send(_telemetry("drone-1"))
            assert await _until(lambda: gt._drone_sockets.get("drone-1") not in (None, stale))
            assert again.connected
            assert "previous connection did not answer a ping" in caplog.text
            assert await gt.send_to_drone("drone-1", fp.make_land()) is True
            assert await _recv_type(again, "land") == {"type": "land"}
            # The replaced connection is closed.
            assert await _until(lambda: not first.connected)
        finally:
            if again is not None:
                await again.disconnect()
            await first.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ===========================================================================
# N6: LAND ALL order
# ===========================================================================

class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class _Emitter:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None, *args, **kwargs):
        self.events.append((event, payload))


class StalledLinkTransport:
    """A ground transport whose per-drone send to ``stalled`` blocks until released.

    It stands for a coordinator send waiting out its timeout on one stalled
    link. The broadcast records when it was called.
    """

    def __init__(self, drone_ids, stalled, clock):
        self.clock = clock
        self.drone_ids = list(drone_ids)
        self.stalled = stalled
        self.release = threading.Event()
        self.stall_entered = threading.Event()
        self.broadcast_called = threading.Event()
        self.sent = []
        self._lock = threading.Lock()

    def now(self):
        return self.clock()

    def send_to_drone(self, drone_id, msg, timeout=None):
        fp.validate_message(msg)
        if drone_id == self.stalled:
            self.stall_entered.set()
            assert self.release.wait(WAIT_S * 3), "the stalled send was never released"
        with self._lock:
            self.sent.append((drone_id, msg["type"]))
        return True

    def emergency_land_all(self, timeout=None):
        self.broadcast_called.set()
        return len(self.drone_ids)

    def fleet_status(self, timeout=None):
        return {d: {"connected": True, "link_age_s": 0.1} for d in self.drone_ids}


@pytest.fixture
def stalled_fleet(clean_app_state, monkeypatch):
    clock = _Clock()
    drone_ids = ("drone-1", "drone-2")  # the coordinator sends to drone-1 first
    transport = StalledLinkTransport(drone_ids, stalled="drone-1", clock=clock)
    supervisor = FleetSupervisor(transport, drone_ids, _Emitter(), area_cm=400, strip_cm=75, clock=clock)
    app_state.fleet_supervisor = supervisor
    app_state.ground_transport = transport
    app_state.fleet = None
    app_state.logger = None
    monkeypatch.setattr(app_module, "socketio", _Emitter())
    yield types.SimpleNamespace(transport=transport, supervisor=supervisor)
    transport.release.set()


def _land_all_in_background():
    outcome = {}

    def run():
        try:
            outcome["report"] = _land_all()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_land_all_broadcast_not_delayed_by_stalled_link(stalled_fleet):
    f = stalled_fleet
    thread, outcome = _land_all_in_background()

    # The coordinator is stuck sending to drone-1 ...
    assert f.transport.stall_entered.wait(WAIT_S)
    # ... and the broadcast has still gone out to every drone.
    assert f.transport.broadcast_called.wait(WAIT_S), "the broadcast waited for the stalled link"
    assert not f.transport.release.is_set()

    f.transport.release.set()
    thread.join(WAIT_S)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome
    assert outcome["report"] == {"coordinator": 2, "transport": 2, "local": None, "reached_any": True}
    # Every path still ran: the coordinator reached both drones too.
    assert sorted(f.transport.sent) == [("drone-1", "emergency_land_all"), ("drone-2", "emergency_land_all")]


def test_land_all_paths_do_not_wait_for_each_other(clean_app_state, monkeypatch):
    """A stalled broadcast delays neither the coordinator nor the local fleet path."""
    release = threading.Event()
    started = {name: threading.Event() for name in ("transport", "coordinator", "local")}

    def stalled_broadcast(timeout=None):
        started["transport"].set()
        assert release.wait(WAIT_S * 3)
        return 1

    def coordinator_land_all():
        started["coordinator"].set()
        return {"drone-1": None}

    def local_land(command):
        started["local"].set()
        return {"tello-1": None}

    app_state.ground_transport = types.SimpleNamespace(emergency_land_all=stalled_broadcast)
    app_state.fleet_supervisor = types.SimpleNamespace(land_all=coordinator_land_all)
    app_state.fleet = types.SimpleNamespace(broadcast_command=local_land)
    app_state.logger = None
    monkeypatch.setattr(app_module, "socketio", _Emitter())

    thread, outcome = _land_all_in_background()
    try:
        for name in ("transport", "coordinator", "local"):
            assert started[name].wait(WAIT_S), f"the {name} path never started"
    finally:
        release.set()
    thread.join(WAIT_S)
    assert not thread.is_alive()
    assert outcome["report"] == {"coordinator": 1, "transport": 1, "local": 1, "reached_any": True}


def test_land_all_with_every_path_failing_is_still_critical(stalled_fleet, caplog):
    f = stalled_fleet
    f.transport.release.set()

    def broken(*args, **kwargs):
        raise RuntimeError("down")

    f.transport.emergency_land_all = broken
    f.transport.send_to_drone = broken
    with caplog.at_level(logging.CRITICAL):
        report = _land_all()

    assert report == {"coordinator": 0, "transport": None, "local": None, "reached_any": False}
    assert any(r.levelno == logging.CRITICAL and "REACHED NO DRONE" in r.getMessage() for r in caplog.records)
    errors = [payload for event, payload in app_module.socketio.events if event == "error"]
    assert errors and "reached no drone" in errors[-1]["message"]
