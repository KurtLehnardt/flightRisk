#!/usr/bin/env python3
"""Export the mobile contract fixtures that the iOS and Android apps test against.

Every payload in ``tests/fixtures/mobile_contract/`` is produced here by the
real ground-station code: the fleet protocol builders, the dashboard's fleet
supervisor, detection profile board, ceiling board, manual control, LAND ALL
path and Socket.IO handlers, and the Engine.IO and Socket.IO packet encoders
of the libraries the dashboard runs on. Nothing in a payload is typed by
hand. The only hand-written data are the inputs (drone ids, telemetry
values, a tiny JPEG) and the client emit allow-list, which is the contract
itself and has no builder (``ALLOWED_CLIENT_EMITS``).

The output is deterministic: the environment is scrubbed of every
``FLIGHTRISK_*`` and ``ANTHROPIC_*`` variable, ``HOME`` points at an empty
temporary directory, clocks are fakes, and the only random identifiers
(alert ids, command ids) come from a seeded ``uuid4`` replacement or are not
part of any payload. ``tests/test_mobile_contract_fixtures.py`` runs this
script into a temporary directory and fails when the result differs from the
committed files, ignoring only ``_meta.generated_at_commit``.

The spec that explains these files is ``docs/mobile-contract.md``.

Usage::

    venv/bin/python scripts/export_mobile_contract_fixtures.py            # rewrite the committed fixtures
    venv/bin/python scripts/export_mobile_contract_fixtures.py --out DIR  # write them somewhere else

No network, drone, edge or ground station is contacted, and no file outside
the output directory is written.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import copy
import json
import logging
import os
import subprocess
import sys
import tempfile
import types
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "mobile_contract"
REGENERATE = "venv/bin/python scripts/export_mobile_contract_fixtures.py"
SPEC_DOC = "docs/mobile-contract.md"

#: Obviously fake identifiers and keys. No real secret ever appears in a fixture.
FAKE_API_KEY = "test-key"
FAKE_EIO_SID = "fake-eio-sid-00000001"
FAKE_SIO_SID = "fake-sio-sid-00000001"
DRONES = ("drone-1", "drone-2")
#: The key every ``unknown_keys.json`` payload gains at each object level.
UNKNOWN_KEY = "x_future_field"
#: Object keys whose value is a map keyed by drone id or provider name, not a record:
#: ``unknown_keys.json`` adds no key to the map itself, only to the records in it.
MAP_KEYS = frozenset({"drones", "unavailable", "to", "slots_cm"})

#: An 8 x 8 grey JPEG (631 bytes), the stand-in for every thumbnail, crop and snapshot.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1x"
    "eXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAAR"
    "CAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEG"
    "E1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
    "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEB"
    "AQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
    "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0"
    "tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCCiiiuk5z/2Q=="
)

#: The Socket.IO events a phone may emit, with an example payload each (None: no payload).
#: This table is the contract; docs/mobile-contract.md explains every guard.
ALLOWED_CLIENT_EMITS: dict[str, dict[str, Any]] = {
    "get_fleet_status": {"payload": None, "guard": "on every connect and on refresh"},
    "get_detection_profiles": {"payload": None, "guard": "on every connect and on refresh"},
    "get_ceiling_settings": {"payload": None, "guard": "on every connect and on refresh"},
    "get_reasoning_provider": {"payload": None, "guard": "on every connect and on refresh"},
    "get_manual_status": {"payload": None, "guard": "on every connect and on refresh"},
    "land_all": {"payload": None, "guard": "always offered while connected; never disabled by any drone state"},
    "drone_command": {
        "payload": {"command": "land", "drone_id": "drone-1"},
        "guard": "command is always \"land\"; offered for every drone in every state",
    },
    "set_detection_profile": {
        "payload": {"drone_id": "drone-1", "profile": "top_down"},
        "guard": "only when detection_profiles.controls_enabled is true; an empty profile follows the fleet default",
    },
    "set_fleet_detection_profile": {
        "payload": {"profile": "top_down"},
        "guard": "only when detection_profiles.controls_enabled is true; an empty profile clears the default",
    },
    "manual_resume_search": {
        "payload": {"drone_id": "drone-2"},
        "guard": "only for a drone whose control_mode is \"held\" while fleet_status.manual_controlled is null",
    },
}

#: Events the dashboard serves that a phone must never emit.
FORBIDDEN_CLIENT_EMITS: dict[str, str] = {
    "plan_fleet": "the laptop is the mission console; Start must carry the plan the browser shows",
    "start_fleet": "starting a mission makes drones take off; laptop only",
    "start_search": "starts a local search pattern that moves a drone; laptop only",
    "stop_search": "mission control; laptop only",
    "manual_take_control": "a phone link roams and sleeps; the controller-gone path would become the normal path",
    "manual_command": "moves, rotations and takeoff by hand; laptop only (Land goes through drone_command)",
    "set_ceiling_settings": "the ceiling and altitude slots are edited on the laptop only",
    "set_reasoning_provider": "the reasoning backend is chosen on the laptop only",
    "set_stream_video": "video streaming changes every edge's bandwidth; laptop only",
    "register_drone": "drone registration; laptop only",
    "deregister_drone": "drone registration; laptop only",
    "remove_all_drones": "drone registration; laptop only",
    "restart_dashboard": "restarts the ground's pipeline; laptop only",
    "set_target": "target photos and embeddings are managed on the laptop",
}


# ---------------------------------------------------------------------------
# Environment and determinism
# ---------------------------------------------------------------------------


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@contextlib.contextmanager
def scrubbed_environment() -> Iterator[None]:
    """Remove every FLIGHTRISK_* and ANTHROPIC_* variable and point HOME at an empty directory."""
    saved = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="flightrisk-mobile-contract-") as home:
        for key in list(os.environ):
            if key.startswith(("FLIGHTRISK_", "ANTHROPIC_")):
                del os.environ[key]
        os.environ["HOME"] = home
        # A ceiling file that does not exist: nothing here ever saves one.
        os.environ["FLIGHTRISK_CEILING_FILE"] = str(Path(home) / "absent" / "ceiling.json")
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(saved)


@contextlib.contextmanager
def env(**values: str | None) -> Iterator[None]:
    """Set (or with None, remove) environment variables, then reload the config."""
    from flightrisk.config import reset_config

    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_config()
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_config()


@contextlib.contextmanager
def seeded_uuid4() -> Iterator[None]:
    """Make ``uuid.uuid4()`` return 00000000-0000-4000-8000-000000000001, ...002, ... in order."""
    counter = iter(range(1, 1_000_000))
    with mock.patch.object(uuid, "uuid4", lambda: uuid.UUID(f"00000000-0000-4000-8000-{next(counter):012d}")):
        yield


@contextlib.contextmanager
def clean_app_state() -> Iterator[Any]:
    """Snapshot the dashboard's ``app_state`` and restore it afterwards; yields the app module."""
    import dataclasses

    from flightrisk.dashboard import app as app_module
    from flightrisk.dashboard.state import AppState, app_state

    saved = {f.name: getattr(app_state, f.name) for f in dataclasses.fields(AppState)}
    try:
        yield app_module
    finally:
        for name, value in saved.items():
            setattr(app_state, name, value)


# ---------------------------------------------------------------------------
# Fakes (the same shape as tests/test_fleet_dashboard.py, kept here so the
# script does not import the test suite)
# ---------------------------------------------------------------------------


class FakeSocketIO:
    """Records ``emit(event, payload)``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def emit(self, event: str, payload: Any = None, *args: Any, **kwargs: Any) -> None:
        self.events.append((event, copy.deepcopy(payload)))

    def named(self, name: str) -> list[Any]:
        return [payload for event, payload in self.events if event == name]

    def last(self, name: str) -> Any:
        found = self.named(name)
        if not found:
            raise AssertionError(f"no {name!r} event was emitted; got {[e for e, _ in self.events]}")
        return found[-1]


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeGroundTransport:
    """Stands in for ``GroundTransportSync``: records sends and answers clock sync in-process."""

    def __init__(self, drone_ids: tuple[str, ...], clock: FakeClock, offset_s: float = 0.25) -> None:
        from flightrisk import fleet_protocol as fp

        self._fp = fp
        self.clock = clock
        self.offset_s = offset_s
        self.connected = {d: True for d in drone_ids}
        self.link_age_s: dict[str, float | None] = {d: 0.1 for d in drone_ids}
        self.sent: list[tuple[str, dict]] = []

    def now(self) -> float:
        return self.clock()

    def send_to_drone(self, drone_id: str, msg: dict, timeout: float | None = None) -> bool:
        self._fp.validate_message(msg)
        self.sent.append((drone_id, dict(msg)))
        return self.connected.get(drone_id, False)

    def sync_clock(self, drone_id: str, rounds: int = 8, round_timeout_s: float = 1.0, timeout: float | None = None):
        if not self.connected.get(drone_id, False):
            raise LookupError(f"no connected edge has reported drone_id {drone_id!r}")
        t1 = self.clock()
        t2 = t1 + self.offset_s
        sample = self._fp.ClockSample(t1=t1, t2=t2, t3=t2, t4=t1)
        return self._fp.estimate_clock([sample], measured_at_s=t1)

    def emergency_land_all(self, timeout: float | None = None) -> int:
        return sum(1 for ok in self.connected.values() if ok)

    def fleet_status(self, timeout: float | None = None) -> dict:
        return {d: {"connected": ok, "link_age_s": self.link_age_s.get(d)} for d, ok in self.connected.items()}

    def sent_of(self, drone_id: str, mtype: str) -> list[dict]:
        return [msg for d, msg in self.sent if d == drone_id and msg.get("type") == mtype]


def _no_spawn(fn: Callable[..., None], *args: Any) -> None:
    """A board's background send that never runs, so its command stays pending."""


def _supervisor(drone_ids: tuple[str, ...] = DRONES, **kwargs: Any):
    """A fleet supervisor over fake links, the way the fleet tests build one."""
    from flightrisk.dashboard.app import FleetSupervisor

    clock = FakeClock()
    transport = FakeGroundTransport(drone_ids, clock)
    sio = FakeSocketIO()
    kwargs.setdefault("retask_wait_s", 0.0)
    supervisor = FleetSupervisor(transport, drone_ids, sio, area_cm=400, strip_cm=75, clock=clock, **kwargs)
    # Manual commands are sent inline rather than on a worker thread, so the
    # recorded events do not depend on thread timing.
    supervisor.manual._background = False
    return supervisor, transport, sio, clock


def _telemetry(drone_id: str, *, battery: float | None = 87, height_cm: float | None = 100, is_flying: bool = False,
               rel_x_cm: float | None = 0.0, rel_y_cm: float | None = 0.0, lane_index: int | None = None,
               lanes_total: int | None = None, **extra: Any) -> dict:
    """An edge ``telemetry`` message from the real builder, plus the optional fields an edge adds."""
    from flightrisk import fleet_protocol as fp

    msg = fp.make_telemetry(
        drone_id=drone_id, battery=battery, height_cm=height_cm, is_flying=is_flying, rel_x_cm=rel_x_cm,
        rel_y_cm=rel_y_cm, heading_deg=0.0, lane_index=lane_index, lanes_total=lanes_total, link_age_s=0.1,
        takeoff_at_local_s=None,
    )
    msg.update(extra)
    fp.validate_edge_to_ground(msg, proto=1)
    return msg


def _ack_lanes(supervisor) -> None:
    from flightrisk import fleet_protocol as fp

    for drone_id in supervisor.links:
        lanes = len(supervisor.coordinator.lanes(drone_id))
        supervisor.handle_control(fp.make_ack_assign_lanes(drone_id, 90, lanes))


def _report_all(supervisor, **extra: Any) -> None:
    """Every drone reports telemetry from an edge that supports manual control and the drone link."""
    for drone_id in supervisor.links:
        supervisor.handle_control(_telemetry(
            drone_id, control_mode="autonomous", drone_link_ok=True, drone_link_age_s=0.2,
            drone_link_detail=None, **extra,
        ))


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def case(event: str | None, direction: str, payload: Any, notes: str, **extra: Any) -> dict:
    out = {"direction": direction, "notes": notes, "payload": copy.deepcopy(payload)}
    if event is not None:
        out["event"] = event
    out.update(extra)
    return out


def document(description: str, sources: list[str], cases: dict[str, dict], **meta: Any) -> dict:
    return {
        "_meta": {
            "description": description,
            "generated_at_commit": None,  # filled in by write_all
            "regenerate": REGENERATE,
            "sources": sorted(sources),
            "spec": SPEC_DOC,
            **meta,
        },
        "cases": cases,
    }


SERVER_TO_CLIENT = "server_to_client"
CLIENT_TO_SERVER = "client_to_server"
GROUND_TO_EDGE = "ground_to_edge"
EDGE_TO_GROUND = "edge_to_ground"


# ---------------------------------------------------------------------------
# Socket.IO and Engine.IO frames
# ---------------------------------------------------------------------------


def build_socketio_frames() -> dict:
    from engineio import packet as eio
    from socketio import exceptions as sio_exceptions
    from socketio import packet as sio

    from flightrisk.dashboard.app import socketio as dashboard_socketio

    server = dashboard_socketio.server.eio
    open_data = {
        "sid": FAKE_EIO_SID,
        # A client that opens the WebSocket transport directly is offered no upgrade.
        "upgrades": [],
        "pingTimeout": int(server.ping_timeout * 1000),
        "pingInterval": int(server.ping_interval + server.ping_interval_grace_period) * 1000,
        "maxPayload": server.max_http_buffer_size,
    }

    def message(pkt) -> str:
        return eio.Packet(eio.MESSAGE, data=pkt.encode()).encode()

    def frame(direction: str, text: str, eio_type: str, sio_type: str | None, notes: str, **extra: Any) -> dict:
        out = {"direction": direction, "frame": text, "engineio_type": eio_type, "notes": notes}
        out["socketio_type"] = sio_type
        out.update(extra)
        return out

    land_ack = {"coordinator": 2, "local": None, "reached_any": True, "transport": 2}
    cases = {
        "engineio_open": frame(
            SERVER_TO_CLIENT, eio.Packet(eio.OPEN, open_data).encode(), "OPEN", None,
            "First frame after the WebSocket upgrade of ws://HOST:5555/socket.io/?EIO=4&transport=websocket. "
            "pingInterval and pingTimeout are milliseconds; the sid differs on every connection.",
            decoded=open_data,
        ),
        "socketio_connect_with_auth": frame(
            CLIENT_TO_SERVER, message(sio.Packet(sio.CONNECT, data={"api_key": FAKE_API_KEY})), "MESSAGE", "CONNECT",
            "The client's namespace connect with the auth object; the key here is the obviously fake "
            "'test-key'. Send it right after engineio_open.",
        ),
        "socketio_connect_without_auth": frame(
            CLIENT_TO_SERVER, message(sio.Packet(sio.CONNECT)), "MESSAGE", "CONNECT",
            "A connect with no auth object. A ground with FLIGHTRISK_API_KEY set answers socketio_connect_error.",
        ),
        "socketio_connect_ack": frame(
            SERVER_TO_CLIENT, message(sio.Packet(sio.CONNECT, data={"sid": FAKE_SIO_SID})), "MESSAGE", "CONNECT",
            "The server accepted the namespace connect; the 'status' event follows at once.",
            decoded={"sid": FAKE_SIO_SID},
        ),
        "socketio_connect_error": frame(
            SERVER_TO_CLIENT,
            message(sio.Packet(sio.CONNECT_ERROR, data=sio_exceptions.ConnectionRefusedError().error_args)),
            "MESSAGE", "CONNECT_ERROR",
            "The server refused the namespace connect (no key or a wrong key). Do not retry in a loop: "
            "report 'wrong key' and wait for the user.",
            decoded=sio_exceptions.ConnectionRefusedError().error_args,
        ),
        "socketio_event_client_to_server": frame(
            CLIENT_TO_SERVER, message(sio.Packet(sio.EVENT, data=["get_fleet_status"])), "MESSAGE", "EVENT",
            "An event with no payload.", decoded=["get_fleet_status"],
        ),
        "socketio_event_client_to_server_with_payload": frame(
            CLIENT_TO_SERVER,
            message(sio.Packet(sio.EVENT, data=["drone_command", {"command": "land", "drone_id": "drone-1"}])),
            "MESSAGE", "EVENT", "An event with one object payload.",
            decoded=["drone_command", {"command": "land", "drone_id": "drone-1"}],
        ),
        "socketio_event_server_to_client": frame(
            SERVER_TO_CLIENT, message(sio.Packet(sio.EVENT, data=["land_all_ack", land_ack])), "MESSAGE", "EVENT",
            "Every server event is a two-element array: the event name and one payload object.",
            decoded=["land_all_ack", land_ack],
        ),
        "socketio_event_with_ack_id": frame(
            CLIENT_TO_SERVER, message(sio.Packet(sio.EVENT, data=["get_fleet_status"], id=1)), "MESSAGE", "EVENT",
            "An event that asks for an acknowledgement (id 1). The dashboard's handlers return nothing, so the "
            "ack carries an empty list; clients need not ask for acks.",
            decoded=["get_fleet_status"], ack_id=1,
        ),
        "socketio_ack": frame(
            SERVER_TO_CLIENT, message(sio.Packet(sio.ACK, data=[], id=1)), "MESSAGE", "ACK",
            "The acknowledgement for socketio_event_with_ack_id.", decoded=[], ack_id=1,
        ),
        "engineio_ping": frame(
            SERVER_TO_CLIENT, eio.Packet(eio.PING).encode(), "PING", None,
            "Engine.IO v4: the server pings every pingInterval ms. Answer with engineio_pong at once; treat no "
            "ping within pingInterval + pingTimeout as a dropped connection.",
        ),
        "engineio_pong": frame(CLIENT_TO_SERVER, eio.Packet(eio.PONG).encode(), "PONG", None, "The answer to a ping."),
        "socketio_disconnect": frame(
            CLIENT_TO_SERVER, message(sio.Packet(sio.DISCONNECT)), "MESSAGE", "DISCONNECT",
            "Leave the namespace before closing the WebSocket.",
        ),
        "engineio_close": frame(CLIENT_TO_SERVER, eio.Packet(eio.CLOSE).encode(), "CLOSE", None, "Close the Engine.IO session."),
    }
    from importlib import metadata

    return document(
        "Engine.IO v4 / Socket.IO v5 text frames as the dashboard's libraries encode them. Phones open "
        "ws://HOST:PORT/socket.io/?EIO=4&transport=websocket directly (no long-polling) and send no Origin header.",
        ["engineio.packet.Packet.encode", "socketio.packet.Packet.encode",
         "flightrisk.dashboard.app.socketio.server.eio (ping settings)"],
        cases,
        library_versions={
            name: metadata.version(name) for name in ("python-engineio", "python-socketio", "flask-socketio")
        },
    )


# ---------------------------------------------------------------------------
# status, fleet_status, fleet_event, fleet_plan, fleet_detection
# ---------------------------------------------------------------------------


def _connect_events(app_module) -> list[dict]:
    """Connect a Flask-SocketIO test client and return what ``on_connect`` sent."""
    client = app_module.socketio.test_client(app_module.app)
    try:
        return client.get_received()
    finally:
        client.disconnect()


def build_status() -> dict:
    from flightrisk.dashboard.state import app_state

    cases = {}
    with clean_app_state() as app_module:
        supervisor, *_ = _supervisor()
        app_state.source = "edge"
        app_state.fleet_supervisor = supervisor
        app_state.appearance_only = True
        app_state.target_photo = None
        received = _connect_events(app_module)
        status = [r["args"][0] for r in received if r["name"] == "status"][-1]
        cases["fleet_two_drones"] = case(
            "status", SERVER_TO_CLIENT, status,
            "Sent once on every connect. fleet.drones is a COUNT here (not a map); fleet.drones > 0 means fleet "
            "mode. appearance_only true means the ground never uses face embeddings.",
        )
    with clean_app_state() as app_module:
        app_state.source = "sim"
        app_state.fleet_supervisor = None
        app_state.fleet = None
        app_state.appearance_only = False
        app_state.target_photo = None
        received = _connect_events(app_module)
        status = [r["args"][0] for r in received if r["name"] == "status"][-1]
        cases["no_fleet_sim_source"] = case(
            "status", SERVER_TO_CLIENT, status,
            "A ground without a fleet (simulated, camera or a single local drone): fleet.drones is 0.",
        )
    return document(
        "The 'status' event the server sends on every Socket.IO connect.",
        ["flightrisk.dashboard.app.on_connect", "flightrisk.dashboard.app._privacy_and_fleet_status",
         "FleetSupervisor.summary"],
        cases,
    )


def _fleet_scenarios() -> dict[str, Any]:
    """Run the fleet scenarios once; returns the payloads every fleet_* file needs."""
    from flightrisk import fleet_protocol as fp
    from flightrisk.drone.plan_edit import PlanSpec
    from flightrisk.edge import Detection, DetectionMessage

    out: dict[str, Any] = {"status": {}, "event": {}, "plan": {}, "manual_status": {}, "manual_ack": {},
                           "manual_result": {}, "detection": {}}

    # All connected: planned, every drone reports, the drone link is up.
    supervisor, transport, sio, clock = _supervisor()
    _report_all(supervisor)
    supervisor.plan()
    _report_all(supervisor)
    out["status"]["all_connected"] = supervisor.status_payload()
    out["plan"]["two_drones"] = sio.last("fleet_plan")

    # One DOWN: drone-2's edge dropped its link after the plan.
    transport.connected["drone-2"] = False
    transport.link_age_s["drone-2"] = None
    out["status"]["one_down"] = supervisor.status_payload()

    # Plan refused because a configured drone is not connected (PR #141).
    supervisor, transport, sio, clock = _supervisor()
    transport.connected["drone-2"] = False
    try:
        supervisor.plan()
    except RuntimeError:
        pass
    out["event"]["plan_refused_unconnected"] = sio.last("fleet_event")

    # Start refused: no assign_lanes acks yet.
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    supervisor.start()
    out["event"]["start_refused"] = sio.last("fleet_event")

    # A v2 edge refuses the takeoff because it cannot reach its drone.
    supervisor.handle_control(fp.make_nack(
        "start_at", "drone-2", "drone_unreachable", "cmd-start-0001",
        detail="Tello unreachable (no state for 12 s)",
    ))
    out["event"]["start_refused_drone_unreachable"] = sio.last("fleet_event")

    # Started; drone-2 lands early and is declared lost; drone-1 refuses its lanes.
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    _ack_lanes(supervisor)
    supervisor.start()
    out["event"]["start"] = sio.last("fleet_event")
    lanes = len(supervisor.coordinator.lanes("drone-2"))
    for drone_id in DRONES:
        supervisor.handle_control(_telemetry(drone_id, is_flying=True, lane_index=0, lanes_total=lanes,
                                             control_mode="autonomous", drone_link_ok=True,
                                             drone_link_age_s=0.2, drone_link_detail=None))
    supervisor.handle_control(_telemetry("drone-2", is_flying=False, lane_index=1, lanes_total=lanes,
                                         control_mode="autonomous", drone_link_ok=True, drone_link_age_s=0.2,
                                         drone_link_detail=None))
    supervisor.check_losses()
    events = sio.named("fleet_event")
    out["event"]["drone_lost"] = [e for e in events if e["event"] == "drone_lost"][-1]
    out["event"]["retask"] = [e for e in events if e["event"] == "retask"][-1]
    out["status"]["one_lost"] = supervisor.status_payload()

    # Map plan with an edited short path: part of the area is uncovered (kind "area").
    supervisor, transport, sio, clock = _supervisor()
    spec = PlanSpec.from_payload({
        "pattern": "parallel_track", "area": {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400},
        "paths": {"drone-2": [[250, 0], [250, 200]]},
    })
    supervisor.plan(spec)
    _ack_lanes(supervisor)
    supervisor.start(spec=spec)
    lanes_1 = len(supervisor.coordinator.lanes("drone-1"))
    lanes_2 = len(supervisor.coordinator.lanes("drone-2"))
    supervisor.handle_control(_telemetry("drone-1", is_flying=True, lane_index=0, lanes_total=lanes_1))
    supervisor.handle_control(_telemetry("drone-2", is_flying=True, lane_index=0, lanes_total=lanes_2))
    # drone-1 lands with lanes left: it is lost and its lanes are offered to drone-2 ...
    supervisor.handle_control(_telemetry("drone-1", is_flying=False, lane_index=1, lanes_total=lanes_1))
    supervisor.check_losses()
    offer = transport.sent_of("drone-2", "assign_lanes")[-1]
    # ... which refuses them, so they are reported uncovered.
    supervisor.handle_control(fp.make_nack_assign_lanes("drone-2", "geofence", len(offer["lanes"])))
    events = sio.named("fleet_event")
    out["event"]["retask_failed"] = [e for e in events if e["event"] == "retask_failed"][-1]
    out["plan"]["map_plan_lost_zone_uncovered"] = supervisor.coordinator.current_plan()

    # Plan edited on the map after Plan: Start is refused until Plan again.
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    supervisor.mark_plan_edited()
    out["event"]["plan_invalidated"] = sio.last("fleet_event")

    # LAND ALL through the coordinator.
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    supervisor.land_all()
    out["event"]["land_all"] = sio.last("fleet_event")
    transport.connected["drone-2"] = False
    supervisor.land_all()
    out["event"]["land_all_failed"] = sio.last("fleet_event")

    # Manual takeover: drone-2 under manual control from the laptop.
    supervisor, transport, sio, clock = _supervisor()
    _report_all(supervisor)
    supervisor.plan()
    supervisor.manual.take_control("drone-2", sid="laptop-browser")
    _report_all(supervisor, is_flying=True)
    supervisor.handle_control(_telemetry("drone-2", is_flying=True, control_mode="manual", drone_link_ok=True,
                                         drone_link_age_s=0.2, drone_link_detail=None))
    out["status"]["one_manual"] = supervisor.status_payload()
    out["manual_status"]["manual_controlled"] = supervisor.manual.status()

    # Plan refused while a drone is under manual control.
    try:
        supervisor.plan()
    except RuntimeError:
        pass
    out["event"]["plan_refused_manual_control"] = sio.last("fleet_event")

    # The laptop's browser goes away: drone-2 stays held and the orphan countdown starts.
    supervisor.manual.controller_disconnected("laptop-browser")
    supervisor.handle_control(_telemetry("drone-2", is_flying=True, control_mode="held", drone_link_ok=True,
                                         drone_link_age_s=0.2, drone_link_detail=None))
    out["status"]["one_held_orphan_countdown"] = supervisor.status_payload()
    out["manual_status"]["held_orphan_countdown"] = supervisor.manual.status()

    # A phone resumes the held drone's search (the one manual action a phone may send).
    out["manual_ack"]["resume_search_ok"] = supervisor.manual.resume_search("drone-2", sid="phone")
    resume = transport.sent_of("drone-2", "resume_search")[-1]
    supervisor.handle_control(fp.make_manual_ack("resume_search", "drone-2", resume["cmd_id"],
                                                 control_mode="autonomous"))
    out["manual_result"]["resume_search_acked"] = sio.last("manual_result")
    out["manual_ack"]["resume_search_not_manual"] = supervisor.manual.resume_search("drone-1", sid="phone")

    # Land one drone (drone_command land in fleet mode): never gated.
    out["manual_ack"]["land_ok"] = supervisor.manual.command("drone-1", "land", sid="phone")
    land = transport.sent_of("drone-1", "land")[-1]
    supervisor.handle_control(fp.make_manual_ack("land", "drone-1", land["cmd_id"]))
    out["manual_result"]["land_acked"] = sio.last("manual_result")
    out["manual_ack"]["land_unknown_drone"] = supervisor.manual.command("drone-9", "land", sid="phone")

    # A land that gets no answer in time.
    supervisor.manual.command("drone-2", "land", sid="phone")
    clock.now += 60.0
    supervisor.manual.check_timeouts()
    out["manual_result"]["land_no_reply"] = sio.last("manual_result")

    # Drone link down: the edge cannot reach its drone (Land still works).
    supervisor, transport, sio, clock = _supervisor()
    _report_all(supervisor)
    supervisor.plan()
    _report_all(supervisor)
    supervisor.handle_control(_telemetry(
        "drone-2", control_mode="autonomous", drone_link_ok=False, drone_link_age_s=12.4,
        drone_link_detail="Tello unreachable (no state for 12 s)",
    ))
    out["status"]["drone_link_down"] = supervisor.status_payload()

    # A merged detection on the shared map.
    supervisor, transport, sio, clock = _supervisor()
    supervisor.plan()
    message = DetectionMessage(
        timestamp=1000.0, frame_id=42, frame_width=960, frame_height=720, drone_id="drone-1",
        rel_x_cm=120.0, rel_y_cm=80.0,
        detections=[Detection(bbox=(400, 300, 460, 420), confidence=0.81, crop_jpeg=TINY_JPEG)],
    )
    supervisor.handle_detections(message)
    out["detection"]["new_detection"] = sio.last("fleet_detection")
    return out


def build_fleet_files(scenarios: dict[str, Any]) -> dict[str, dict]:
    from flightrisk.drone.fake import FakeDrone
    from flightrisk.drone.fleet import DroneFleet

    status_notes = {
        "all_connected": "Fleet mode, both drones CONNECTED (connected true, lost false).",
        "one_down": "drone-2 is DOWN: connected false. Its other values are the last ones received.",
        "one_lost": "drone-2 is LOST (lost true; LOST wins over connected). Its lanes were re-tasked.",
        "one_held_orphan_countdown": (
            "drone-2 is HELD: control_mode 'held' and the laptop that controlled it went away; the ground lands "
            "it when orphan_land_in_s reaches 0 (120 s by default). manual_controlled is null, so a phone may "
            "send manual_resume_search for it."
        ),
        "one_manual": "drone-2 is MANUAL: manual_controlled names it and its control_mode is 'manual'.",
        "drone_link_down": (
            "drone-2's edge cannot reach its drone: drone_link_ok false with drone_link_detail. Show "
            "'Drone link down: <detail>. Land still works.' and never disable Land."
        ),
    }
    cases = {
        name: case("fleet_status", SERVER_TO_CLIENT, payload, status_notes[name])
        for name, payload in scenarios["status"].items()
    }
    from flightrisk.dashboard.state import app_state

    with clean_app_state() as app_module:
        app_state.fleet_supervisor = None
        fleet = DroneFleet(factory=lambda drone_id, host: FakeDrone(name=drone_id, host=host, clock=lambda: 0.0,
                                                                     sleep=lambda s: None))
        fleet.register("drone-1", host="127.0.0.1")
        app_state.fleet = fleet
        client = app_module.socketio.test_client(app_module.app)
        try:
            client.get_received()
            client.emit("get_fleet_status")
            local = [r["args"][0] for r in client.get_received() if r["name"] == "fleet_status"][-1]
            app_state.fleet = None
            client.emit("get_fleet_status")
            empty = [r["args"][0] for r in client.get_received() if r["name"] == "fleet_status"][-1]
        finally:
            client.disconnect()
    cases["non_fleet_local_drone"] = case(
        "fleet_status", SERVER_TO_CLIENT, local,
        "A ground without a fleet answers with the legacy shape {drones, count}: each drone has is_connected "
        "(not connected), height in cm and no lost key, so the connection state shows as unknown ('--').",
    )
    cases["non_fleet_no_drone"] = case(
        "fleet_status", SERVER_TO_CLIENT, empty, "A ground without a fleet and without a local drone.",
    )
    files = {"fleet_status.json": document(
        "The 'fleet_status' event: the answer to get_fleet_status, and a broadcast whenever the fleet changes.",
        ["FleetSupervisor.status_payload", "FleetCoordinator.status", "ManualControl.drone_status",
         "flightrisk.dashboard.app.on_get_fleet_status"],
        cases,
    )}

    event_notes = {
        "plan_refused_unconnected": "Plan refused because a configured drone is not connected (PR #141).",
        "plan_refused_manual_control": "Plan refused while a drone is under manual control.",
        "plan_invalidated": "The plan was edited on the map after Plan; Start is refused until Plan again.",
        "start": "The common start was scheduled.",
        "start_refused": "Start refused; detail names every drone and why.",
        "start_refused_drone_unreachable": "A v2 edge refused to take off because it cannot reach its drone.",
        "drone_lost": "A drone dropped out of the mission. This event carries drone_id and NO detail.",
        "retask": (
            "The lost drone's lanes were offered to the others. 'to' maps drone id to the number of accepted lanes; "
            "'awaiting' lists offered lanes with no answer yet."
        ),
        "retask_failed": "Lanes nobody can take: every entry has a reason; they also appear in fleet_plan.uncovered.",
        "land_all": "LAND ALL went out to every link.",
        "land_all_failed": "LAND ALL could not be sent to some links; detail names them.",
    }
    cases = {
        name: case("fleet_event", SERVER_TO_CLIENT, payload, event_notes[name])
        for name, payload in scenarios["event"].items()
    }
    cases["profile_switch_pending"] = case(
        "fleet_event", SERVER_TO_CLIENT, build_profile_switch_pending(),
        "Start was pressed while a detection profile switch is still pending (a warning, not a refusal).",
    )
    files["fleet_event.json"] = document(
        "The 'fleet_event' event, one case per kind. Read the kind from 'event'; show 'detail' when present. "
        "Unknown kinds must be shown by name, never dropped.",
        ["FleetSupervisor.plan", "FleetSupervisor.start", "FleetSupervisor.mark_plan_edited",
         "FleetCoordinator.on_drone_lost", "FleetCoordinator._on_nack", "FleetCoordinator.land_all",
         "flightrisk.dashboard.app._warn_pending_profile_switch"],
        cases,
    )

    files["fleet_plan.json"] = document(
        "The 'fleet_plan' event: zones, lanes and uncovered entries on the shared grid (cm, plan frame).",
        ["FleetCoordinator.current_plan", "FleetPlan.to_dict", "flightrisk.drone.plan_edit.build_plan"],
        {
            "two_drones": case(
                "fleet_plan", SERVER_TO_CLIENT, scenarios["plan"]["two_drones"],
                "A plan made with Plan search (no map edit): two zones side by side, no uncovered entries.",
            ),
            "map_plan_lost_zone_uncovered": case(
                "fleet_plan", SERVER_TO_CLIENT, scenarios["plan"]["map_plan_lost_zone_uncovered"],
                "A plan made on the map with an edited short path for drone-2. drone-1 was lost (its zone has "
                "lost true) and drone-2 refused its lanes. 'uncovered' mixes two kinds of entry: re-tasked lanes "
                "nobody took (no 'kind' key; index, direction, distance_cm, x_cm, y_cm, end_x_cm, end_y_cm, "
                "drone_id, reason) and coverage gaps of the plan itself (kind 'area'; x_cm, y_cm, w_cm, h_cm, "
                "reason). 'details' is the map editor's own view and may be ignored.",
            ),
        },
    )
    files["fleet_detection.json"] = document(
        "The 'fleet_detection' event: a new merged detection on the shared grid.",
        ["FleetSupervisor.handle_detections", "FleetCoordinator.on_detection", "FleetDetection.to_dict"],
        {"new_detection": case(
            "fleet_detection", SERVER_TO_CLIENT, scenarios["detection"]["new_detection"],
            "thumbnail is a base64 JPEG (the most confident crop). rel_x_cm/rel_y_cm repeat x_cm/y_cm.",
        )},
    )
    files["manual_status.json"] = document(
        "The 'manual_status' event: the answer to get_manual_status, and a broadcast on every manual change.",
        ["ManualControl.status", "ManualControl.drone_status"],
        {
            "held_orphan_countdown": case(
                "manual_status", SERVER_TO_CLIENT, scenarios["manual_status"]["held_orphan_countdown"],
                "Nobody controls a drone; drone-2 is held and lands in orphan_land_in_s seconds.",
            ),
            "manual_controlled": case(
                "manual_status", SERVER_TO_CLIENT, scenarios["manual_status"]["manual_controlled"],
                "The laptop controls drone-2.",
            ),
        },
    )
    files["manual_ack.json"] = document(
        "The 'manual_ack' event: the immediate answer to drone_command (fleet mode) and manual_resume_search. "
        "ok false is also sent as an 'error' event.",
        ["ManualControl.command", "ManualControl.resume_search", "flightrisk.dashboard.app._manual_reply"],
        {
            "land_ok": case("manual_ack", SERVER_TO_CLIENT, scenarios["manual_ack"]["land_ok"],
                            "drone_command {command: land} was queued for the drone (never gated)."),
            "land_unknown_drone": case("manual_ack", SERVER_TO_CLIENT, scenarios["manual_ack"]["land_unknown_drone"],
                                       "A drone id that is not in the fleet."),
            "resume_search_ok": case("manual_ack", SERVER_TO_CLIENT, scenarios["manual_ack"]["resume_search_ok"],
                                     "manual_resume_search for a held drone nobody controls was accepted."),
            "resume_search_not_manual": case(
                "manual_ack", SERVER_TO_CLIENT, scenarios["manual_ack"]["resume_search_not_manual"],
                "manual_resume_search for a drone that is not paused is refused.",
            ),
        },
    )
    files["manual_result.json"] = document(
        "The 'manual_result' event: the edge's answer to a manual command (or its absence), broadcast.",
        ["ManualControl.on_reply", "ManualControl.check_timeouts", "ManualControl._result"],
        {
            "land_acked": case("manual_result", SERVER_TO_CLIENT, scenarios["manual_result"]["land_acked"],
                               "The edge acknowledged the land."),
            "resume_search_acked": case(
                "manual_result", SERVER_TO_CLIENT, scenarios["manual_result"]["resume_search_acked"],
                "The edge resumed the search; the orphan countdown is cancelled.",
            ),
            "land_no_reply": case("manual_result", SERVER_TO_CLIENT, scenarios["manual_result"]["land_no_reply"],
                                  "No answer from the edge in time."),
        },
    )
    return files


def build_profile_switch_pending() -> dict:
    """The fleet_event of ``_warn_pending_profile_switch``, which the start_fleet handler sends."""
    from flightrisk.dashboard.detection_profiles import ProfileBoard
    from flightrisk.dashboard.state import app_state

    with clean_app_state() as app_module:
        board = ProfileBoard(None, lambda *a: None, fleet_drones=DRONES, spawn=_no_spawn)
        board.observe_control(_telemetry("drone-2", detection_profile="default", detection_profile_fallback=False,
                                         detection_profile_switchable=True))
        board.select("drone-2", "top_down")
        app_state.profile_board = board
        sio = FakeSocketIO()
        with mock.patch.object(app_module, "emit", sio.emit):
            app_module._warn_pending_profile_switch()
        return sio.last("fleet_event")


# ---------------------------------------------------------------------------
# frame and match_alert
# ---------------------------------------------------------------------------


def build_frame_and_alert() -> dict[str, dict]:
    from flightrisk.dashboard import alert_latch as alert_latch_module
    from flightrisk.dashboard.state import app_state
    from flightrisk.edge import Detection, DetectionMessage
    from flightrisk.vision.scorer import MatchScorer

    frames = {}
    alerts = {}
    scorer = MatchScorer()

    class Station:
        def __init__(self, results):
            self.results = results

        def process_message(self, msg):
            return self.results

    message = DetectionMessage(
        timestamp=1000.0, frame_id=42, thumbnail_jpeg=TINY_JPEG, frame_width=960, frame_height=720,
        drone_id="drone-1",
        detections=[
            Detection(bbox=(400, 300, 460, 420), confidence=0.81, crop_jpeg=TINY_JPEG),
            Detection(bbox=(100, 120, 150, 230), confidence=0.55, crop_jpeg=TINY_JPEG),
        ],
    )
    results = [
        {"bbox": (400, 300, 460, 420), "reid_score": 0.86, "face_score": 0.0,
         "score_result": scorer.score(reid_score=0.86, face_score=0.0)},
        {"bbox": (100, 120, 150, 230), "reid_score": 0.31, "face_score": 0.0,
         "score_result": scorer.score(reid_score=0.31, face_score=0.0)},
    ]
    with clean_app_state() as app_module, seeded_uuid4(), \
            mock.patch.object(app_module.time, "strftime", lambda fmt, *a: "12:34:56"):
        latch = alert_latch_module.MatchAlertLatch()
        sio = FakeSocketIO()
        with mock.patch.object(app_module, "alert_latch", latch):
            app_state.ground_station = Station(results)
            app_state.scorer = scorer
            app_state.db = None
            app_state.session_id = None
            app_state.match_history = []
            app_state.stream_video = True
            app_module._edge_process_and_emit(sio, message)
            frames["edge_video_with_match"] = case(
                "frame", SERVER_TO_CLIENT, sio.last("frame"),
                "One edge frame with two detections, streaming on (image is a base64 JPEG). bbox_norm is "
                "[x1, y1, x2, y2] divided by frame_width/frame_height. Keep one tile per drone_id.",
            )
            alerts["edge_match"] = case(
                "match_alert", SERVER_TO_CLIENT, sio.last("match_alert"),
                "A match from an edge. snapshot is a base64 JPEG crop; alert_id identifies the alert in "
                "every later event about it.",
            )
            pending = latch.pending_alert()
            alerts["reminder_on_connect"] = case(
                "match_alert_reminder", SERVER_TO_CLIENT, pending,
                "Sent on connect (and periodically) while an alert waits for the operator: the same payload "
                "with renotify true and renotify_count.",
            )
            app_state.stream_video = False
            stripped = DetectionMessage(
                timestamp=1000.5, frame_id=43, thumbnail_jpeg=None, frame_width=960, frame_height=720,
                drone_id="drone-2",
                detections=[Detection(bbox=(100, 120, 150, 230), confidence=0.55)],
            )
            app_state.ground_station = Station(results[1:])
            app_module._edge_process_and_emit(sio, stripped)
            frames["edge_detections_only"] = case(
                "frame", SERVER_TO_CLIENT, sio.last("frame"),
                "Streaming off: image is null; draw the boxes on a blank tile ('detections only').",
            )
    return {
        "frame.json": document(
            "The 'frame' event: one video frame (or detections only) per drone.",
            ["flightrisk.dashboard.app._edge_process_and_emit"], frames,
        ),
        "match_alert.json": document(
            "The 'match_alert' and 'match_alert_reminder' events. A phone shows them; the operator answers on the "
            "laptop (match_alert_response is not in the phone's allow-list).",
            ["flightrisk.dashboard.app._emit_edge_alert", "MatchAlertLatch.pending_alert"], alerts,
        ),
    }


# ---------------------------------------------------------------------------
# detection_profiles, ceiling_settings, reasoning_provider
# ---------------------------------------------------------------------------


def build_detection_profiles() -> dict:
    from flightrisk import fleet_protocol as fp
    from flightrisk.dashboard.detection_profiles import ProfileBoard

    def board(**kwargs: Any) -> tuple[ProfileBoard, list]:
        sent: list = []

        def spawn(fn, drone_id, msg):
            sent.append(msg)

        return ProfileBoard(kwargs.pop("transport", None), lambda *a: None, spawn=spawn, **kwargs), sent

    def report(b: ProfileBoard, drone_id: str, **fields: Any) -> None:
        b.observe_control(_telemetry(drone_id, **fields))

    cases = {}
    b, _ = board(fleet_drones=("drone-1",))
    report(b, "drone-1", detection_profile="default", detection_profile_fallback=False,
           detection_profile_switchable=True)
    b.select("drone-1", "top_down")
    cases["pending"] = case(
        "detection_profiles", SERVER_TO_CLIENT, b.payload(),
        "A switch to top_down was sent and waits for the edge's answer: pending true, mismatch true. "
        "Show 'Switching...'.",
    )

    b, _ = board(fleet_drones=("drone-1",))
    report(b, "drone-1", detection_profile="default", detection_profile_fallback=True,
           detection_profile_fallback_reason="weights_missing", detection_profile_requested="top_down",
           detection_profile_switchable=True)
    cases["fallback"] = case(
        "detection_profiles", SERVER_TO_CLIENT, b.payload(),
        "The edge was asked for top_down but runs its fallback (fallback true, fallback_reason "
        "'weights_missing', requested 'top_down').",
    )

    b, sent = board(fleet_drones=("drone-1",))
    report(b, "drone-1", detection_profile="default", detection_profile_fallback=False,
           detection_profile_switchable=True, is_flying=True)
    b.select("drone-1", "top_down")
    b.observe_control(fp.make_profile_nack(
        "drone-1", sent[-1]["cmd_id"], "in_flight", profile="default", requested="top_down",
        detail="detection profiles change only on the ground",
    ))
    cases["refused"] = case(
        "detection_profiles", SERVER_TO_CLIENT, b.payload(),
        "The edge refused the switch (refusal.reason 'in_flight', refusal.detail is operator text) and keeps "
        "its active profile; mismatch stays true.",
    )

    b, _ = board(fleet_drones=("drone-1", "drone-2"))
    report(b, "drone-1", detection_profile="default", detection_profile_fallback=False,
           detection_profile_switchable=True)
    report(b, "drone-2")
    b.set_fleet_default("default")
    cases["unsupported"] = case(
        "detection_profiles", SERVER_TO_CLIENT, b.payload(),
        "drone-2 runs an edge that never names a profile: supported false, active null. Show 'not supported'; "
        "offer no selector for it. The fleet default is 'default'.",
    )

    standby_transport = types.SimpleNamespace(identity=types.SimpleNamespace(role="standby"))
    b, _ = board(fleet_drones=("drone-1",), transport=standby_transport)
    report(b, "drone-1", detection_profile="default", detection_profile_fallback=False,
           detection_profile_switchable=True)
    cases["standby_controls_disabled"] = case(
        "detection_profiles", SERVER_TO_CLIENT, b.payload(),
        "A standby ground: controls_enabled false with controls_note. Hide the selectors and show the note.",
    )

    with clean_app_state() as app_module:
        from flightrisk.dashboard.state import app_state

        app_state.profile_board = None
        client = app_module.socketio.test_client(app_module.app)
        try:
            client.get_received()
            client.emit("get_detection_profiles")
            no_board = [r["args"][0] for r in client.get_received() if r["name"] == "detection_profiles"][-1]
        finally:
            client.disconnect()
    cases["no_board"] = case(
        "detection_profiles", SERVER_TO_CLIENT, no_board,
        "A ground without the edge source: available false and no profiles. Hide the section.",
    )
    return document(
        "The 'detection_profiles' event: the answer to get_detection_profiles, and a broadcast on every change.",
        ["ProfileBoard.payload", "flightrisk.dashboard.app.on_get_detection_profiles",
         "flightrisk.fleet_protocol.make_profile_nack"],
        cases,
    )


class _FixedStore:
    """A ceiling store at a fixed, fake path that holds *settings* (nothing is read from or written to disk)."""

    path = Path("/home/operator/.flightrisk/ceiling.json")

    def __init__(self, settings=None, error: str | None = None):
        from flightrisk.dashboard.ceiling import CeilingSettings

        self._settings = settings if settings is not None else CeilingSettings()
        self._error = error

    def load(self):
        return self._settings, self._error

    def save(self, settings) -> None:
        raise AssertionError("the fixture exporter never saves ceiling settings")


def build_ceiling_settings() -> dict:
    from flightrisk import fleet_protocol as fp
    from flightrisk.dashboard.ceiling import CeilingBoard, CeilingSettings
    from flightrisk.observability import StructuredLogger

    def board(settings=None, *, flying: str | None = None):
        sent: list = []

        def spawn(fn, drone_id, msg):
            sent.append(msg)

        b = CeilingBoard(_FixedStore(settings), object(), lambda *a: None, fleet_drones=DRONES, spawn=spawn,
                         clock=FakeClock(), logger=StructuredLogger(component="mobile-contract"))
        if flying is not None:
            b.bind_activity(lambda: flying)
        return b, sent

    def report(b, drone_id: str, ceiling: float, margin: float, slot: float, **fields: Any) -> None:
        b.observe_control(_telemetry(drone_id, ceiling_cm=ceiling, ceiling_margin_cm=margin, alt_slot_cm=slot,
                                     **fields))

    cases = {}
    b, sent = board()
    report(b, "drone-1", 200, 60, 100)
    report(b, "drone-2", 200, 60, 130)
    cases["on_ground_in_sync"] = case(
        "ceiling_settings", SERVER_TO_CLIENT, b.payload(),
        "Defaults: ceiling 200 cm, margin 60 cm, effective max slot 140 cm. Both edges report the same values "
        "(edge.in_sync true). editable true because every drone is on the ground.",
    )

    b, sent = board(CeilingSettings(ceiling_cm=180, margin_cm=50, slots_cm={"drone-2": 120}))
    report(b, "drone-1", 200, 60, 100)
    report(b, "drone-2", 200, 60, 130)
    cases["edge_pending"] = case(
        "ceiling_settings", SERVER_TO_CLIENT, b.payload(),
        "The ground's settings changed (ceiling 180, margin 50, drone-2 has its own slot 120 cm); the new values "
        "were sent and each edge's answer is pending (edge.pending true, edge.in_sync false).",
    )
    b.observe_control(fp.make_ceiling_nack(
        "drone-2", sent[-1]["cmd_id"], "in_flight",
        active={"ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 130},
        detail="the ceiling changes only on the ground",
    ))
    cases["edge_refused"] = case(
        "ceiling_settings", SERVER_TO_CLIENT, b.payload(),
        "drone-2's edge refused the change (edge.refusal with reason and detail) and keeps its old values.",
    )

    b, _ = board(CeilingSettings(ceiling_cm=200, margin_cm=60, slots_cm={"drone-2": 160}))
    cases["slot_above_limit"] = case(
        "ceiling_settings", SERVER_TO_CLIENT, b.payload(),
        "drone-2's own slot 160 cm is above the 140 cm limit: 'problem' says so, and Plan is refused. The edges "
        "have not reported (edge.supported false).",
    )

    b, _ = board(flying="drone-1 reported flying")
    report(b, "drone-1", 200, 60, 100, is_flying=True)
    report(b, "drone-2", 200, 60, 130, is_flying=True)
    cases["locked_in_flight"] = case(
        "ceiling_settings", SERVER_TO_CLIENT, b.payload(),
        "A drone flies: editable false with locked_reason. A phone never edits these settings anyway.",
    )

    with clean_app_state() as app_module:
        from flightrisk.dashboard.state import app_state

        app_state.ceiling_board = None
        client = app_module.socketio.test_client(app_module.app)
        try:
            client.get_received()
            client.emit("get_ceiling_settings")
            unavailable = [r["args"][0] for r in client.get_received() if r["name"] == "ceiling_settings"][-1]
        finally:
            client.disconnect()
    cases["unavailable"] = case(
        "ceiling_settings", SERVER_TO_CLIENT, unavailable,
        "A ground without the edge source: {available: false}. Hide the section.",
    )
    return document(
        "The 'ceiling_settings' event: the answer to get_ceiling_settings, and a broadcast on every change. "
        "'file' is a path on the ground machine, for display only.",
        ["CeilingBoard.payload", "flightrisk.dashboard.app.on_get_ceiling_settings",
         "flightrisk.fleet_protocol.make_ceiling_nack"],
        cases,
    )


def build_reasoning_provider() -> dict:
    from flightrisk.config import get_config
    from flightrisk.dashboard.state import app_state
    from flightrisk.reasoning.agent import FlightRiskAgent

    def gemma():
        # The attributes the real local agent exposes, without contacting Ollama.
        return types.SimpleNamespace(provider=FlightRiskAgent.provider, label=FlightRiskAgent.label,
                                     model=get_config().reasoning.model)

    cases = {}
    with clean_app_state() as app_module:
        with env(FLIGHTRISK_ALLOW_CLOUD_LLM=None, FLIGHTRISK_CLAUDE_API_KEY=None):
            app_state.reasoning = gemma()
            cases["local_gemma_cloud_blocked"] = case(
                "reasoning_provider", SERVER_TO_CLIENT, app_module._reasoning_state(),
                "The shipped default: local Gemma, cloud not allowed (cloud_allowed false); claude is listed in "
                "'unavailable' with the reason to show.",
            )
            app_state.reasoning = None
            cases["off"] = case(
                "reasoning_provider", SERVER_TO_CLIENT, app_module._reasoning_state(),
                "Reasoning off: provider 'off', label and model null.",
            )
        with env(FLIGHTRISK_ALLOW_CLOUD_LLM="1", FLIGHTRISK_CLAUDE_API_KEY=None):
            app_state.reasoning = gemma()
            cases["cloud_allowed_no_key"] = case(
                "reasoning_provider", SERVER_TO_CLIENT, app_module._reasoning_state(),
                "Cloud allowed by the operator but no Claude key on the ground: claude stays unavailable.",
            )
        with env(FLIGHTRISK_ALLOW_CLOUD_LLM="1", FLIGHTRISK_CLAUDE_API_KEY=FAKE_API_KEY):
            app_state.reasoning = gemma()
            cases["cloud_allowed"] = case(
                "reasoning_provider", SERVER_TO_CLIENT, app_module._reasoning_state(),
                "Cloud allowed and configured on the ground (the fake key is not part of the payload).",
            )
    return document(
        "The 'reasoning_provider' event: which reasoning backend the GROUND uses. Display only on a phone.",
        ["flightrisk.dashboard.app._reasoning_state", "flightrisk.reasoning.provider_unavailable_reason"],
        cases,
    )


# ---------------------------------------------------------------------------
# land_all_ack, command_ack, stream_video_state, error
# ---------------------------------------------------------------------------


def _received_after(app_module, event: str, payload: Any = None) -> list[dict]:
    client = app_module.socketio.test_client(app_module.app)
    try:
        client.get_received()
        if payload is None:
            client.emit(event)
        else:
            client.emit(event, payload)
        return client.get_received()
    finally:
        client.disconnect()


def _first(received: list[dict], name: str) -> Any:
    found = [r["args"][0] for r in received if r["name"] == name]
    if not found:
        raise AssertionError(f"no {name!r} received; got {[r['name'] for r in received]}")
    return found[-1]


def build_handler_replies() -> dict[str, dict]:
    from flightrisk.dashboard.state import app_state
    from flightrisk.drone.fake import FakeDrone
    from flightrisk.drone.fleet import DroneFleet

    land, command, stream, errors = {}, {}, {}, {}

    with clean_app_state() as app_module:
        supervisor, transport, *_ = _supervisor()
        app_state.fleet_supervisor = supervisor
        app_state.ground_transport = transport
        app_state.fleet = None
        app_state.logger = None
        received = _received_after(app_module, "land_all")
        land["fleet_reached_all"] = case(
            "land_all_ack", SERVER_TO_CLIENT, _first(received, "land_all_ack"),
            "Each path's count of drones reached (null: path not configured or failed). Show the counts.",
        )

    with clean_app_state() as app_module:
        app_state.fleet_supervisor = None
        app_state.ground_transport = None
        fleet = DroneFleet(factory=lambda drone_id, host: FakeDrone(name=drone_id, host=host, clock=lambda: 0.0,
                                                                     sleep=lambda s: None))
        fleet.register("drone-1", host="127.0.0.1")
        app_state.fleet = fleet
        app_state.logger = None
        received = _received_after(app_module, "land_all")
        land["local_fleet"] = case(
            "land_all_ack", SERVER_TO_CLIENT, _first(received, "land_all_ack"),
            "A ground with a locally connected drone and no edge fleet.",
        )
        received = _received_after(app_module, "drone_command", {"command": "land", "drone_id": "drone-1"})
        command["land"] = case(
            "command_ack", SERVER_TO_CLIENT, _first(received, "command_ack"),
            "The answer to drone_command {command: land} on a ground WITHOUT a fleet (in fleet mode the answer "
            "is manual_ack).",
        )

    with clean_app_state() as app_module:
        app_state.fleet_supervisor = None
        app_state.ground_transport = None
        app_state.fleet = None
        app_state.logger = None
        received = _received_after(app_module, "land_all")
        land["reached_no_drone"] = case(
            "land_all_ack", SERVER_TO_CLIENT, _first(received, "land_all_ack"),
            "No path reached any drone (reached_any false). Show this as a FAILURE: 'LAND ALL reached no drone'.",
        )
        errors["land_all_reached_no_drone"] = case(
            "error", SERVER_TO_CLIENT, _first(received, "error"),
            "Broadcast with the land_all_ack above when LAND ALL reached no drone.",
        )
        received = _received_after(app_module, "manual_resume_search", {"drone_id": "drone-2"})
        errors["manual_needs_fleet_mode"] = case(
            "error", SERVER_TO_CLIENT, _first(received, "error"),
            "manual_resume_search on a ground without a fleet.",
        )
        received = _received_after(app_module, "set_detection_profile", {"drone_id": "drone-1", "profile": "default"})
        errors["profiles_need_edge_source"] = case(
            "error", SERVER_TO_CLIENT, _first(received, "error"),
            "set_detection_profile on a ground without the edge source.",
        )
        received = _received_after(app_module, "drone_command", {"command": "land", "drone_id": "drone-1"})
        errors["no_drones_connected"] = case(
            "error", SERVER_TO_CLIENT, _first(received, "error"),
            "drone_command on a ground with no fleet and no local drone.",
        )

    with clean_app_state() as app_module:
        supervisor, transport, *_ = _supervisor()
        app_state.fleet_supervisor = supervisor
        app_state.ground_transport = None
        app_state.fleet = None
        app_state.logger = None
        received = _received_after(app_module, "manual_resume_search", {"drone_id": "drone-1"})
        errors["manual_refusal"] = case(
            "error", SERVER_TO_CLIENT, _first(received, "error"),
            "Every refused manual_ack is also sent as an error toast: '<drone_id>: <detail or reason>'.",
        )
        app_state.stream_video = False
        client = app_module.socketio.test_client(app_module.app)
        try:
            client.get_received()
            client.emit("set_stream_video", {"enabled": False})
            stream["off"] = case(
                "stream_video_state", SERVER_TO_CLIENT, _first(client.get_received(), "stream_video_state"),
                "The laptop turned video streaming off (the reply goes to the browser that changed it). Frames then "
                "carry no image.",
            )
        finally:
            client.disconnect()

    return {
        "land_all_ack.json": document(
            "The 'land_all_ack' event: the answer to land_all, with how many drones each path reached.",
            ["flightrisk.dashboard.app._land_all", "flightrisk.dashboard.app.on_land_all"], land,
        ),
        "command_ack.json": document(
            "The 'command_ack' event: the answer to drone_command on a ground without a fleet.",
            ["flightrisk.dashboard.app.on_drone_command"], command,
        ),
        "stream_video_state.json": document(
            "The 'stream_video_state' event (read only on a phone).",
            ["flightrisk.dashboard.app.on_set_stream_video"], stream,
        ),
        "error.json": document(
            "The 'error' event: {message}. Show it as a toast; never treat it as a disconnect.",
            ["flightrisk.dashboard.app._land_all", "flightrisk.dashboard.app._manual_reply",
             "flightrisk.dashboard.app.on_manual_resume_search", "flightrisk.dashboard.app.on_set_detection_profile",
             "flightrisk.dashboard.app.on_drone_command"],
            errors,
        ),
    }


# ---------------------------------------------------------------------------
# Edge link (port 9000): what a phone acting as an edge sends and receives
# ---------------------------------------------------------------------------


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def build_edge_link() -> dict[str, dict]:
    from flightrisk import fleet_protocol as fp
    from flightrisk.edge import Detection, DetectionMessage, EdgeRunner
    from flightrisk.transport import GroundTransport

    ground = GroundTransport(host="127.0.0.1", port=0)
    ws = _RecordingWebSocket()
    asyncio.run(ground.send_target(ws, [0.12, -0.08, 0.33, 0.05], None))
    asyncio.run(ground.send_stream_video(ws, True))
    set_target, stream_video = (json.loads(text) for text in ws.sent)
    fence = fp.Fence(epoch=7, leader_id="ground-a")

    g2e = {
        "ground_hello": case(None, GROUND_TO_EDGE, fp.make_ground_hello("ground-a", "leader", 7),
                             "A v2 ground's first message. A phone edge (v1) ignores it and never answers."),
        "heartbeat_v1": case(None, GROUND_TO_EDGE, fp.make_heartbeat(12), "Ignore."),
        "heartbeat_v2": case(None, GROUND_TO_EDGE, fp.make_heartbeat(12, fence=fence), "Ignore."),
        "clock": case(None, GROUND_TO_EDGE, fp.make_clock(0.25), "Clock offset for fleet starts. Ignore."),
        "ping": case(None, GROUND_TO_EDGE, fp.make_ping(1000.0),
                     "Clock-sync ping. A phone edge flies no lanes and never answers with pong; ignore."),
        "set_target": case(None, GROUND_TO_EDGE, set_target,
                           "The target's ReID embedding (shortened here); face_embedding is null on an "
                           "appearance-only ground. A phone keeps its own target: log and ignore."),
        "stream_video": case(None, GROUND_TO_EDGE, stream_video,
                             "Turn the full-frame thumbnail in 'detections' on (true) or off (false)."),
        "land": case(None, GROUND_TO_EDGE, fp.make_land(cmd_id="cmd-land-0001"),
                     "Flight command; a phone edge has no drone and ignores it."),
        "emergency_land_all": case(None, GROUND_TO_EDGE, fp.make_emergency_land_all(),
                                   "Flight command; a phone edge has no drone and ignores it."),
    }
    for name, entry in g2e.items():
        fp.validate_ground_to_edge(entry["payload"], proto=2 if name in ("ground_hello", "heartbeat_v2") else 1)

    appearance_only = EdgeRunner(appearance_only=True)
    with_face = EdgeRunner(appearance_only=False)
    phone_message = DetectionMessage(
        timestamp=1761735600.25, frame_id=17, thumbnail_jpeg=TINY_JPEG, frame_width=1280, frame_height=720,
        detections=[Detection(bbox=(512, 200, 640, 560), confidence=0.74, reid_embedding=[0.11, -0.04, 0.27, 0.09],
                              face_embedding=[0.02, 0.15, -0.21, 0.08], crop_jpeg=TINY_JPEG)],
    )
    detections_ao = appearance_only.to_dict(phone_message)
    detections_face = with_face.to_dict(phone_message)
    phone_message.thumbnail_jpeg = None
    detections_no_video = appearance_only.to_dict(phone_message)
    for msg in (detections_ao, detections_face, detections_no_video):
        fp.validate_edge_to_ground(msg, proto=1)
        if len(json.dumps(msg).encode()) > fp.LEGACY_MAX_BYTES:
            raise AssertionError("a detections sample exceeds the 1 MiB cap")

    v2 = fp.TelemetryV2(epoch=7, plan_id="p-1", state="flying_lanes", plan_valid=True)
    telemetry_v2 = fp.make_telemetry("drone-2", 71, 130, True, 40.0, 220.5, 0.0, 3, 9, 0.12, 1000.0, v2=v2)
    telemetry_v2.update(drone_link_ok=False, drone_link_age_s=12.4,
                        drone_link_detail="Tello unreachable (no state for 12 s)")
    e2g = {
        "detections_appearance_only": case(
            None, EDGE_TO_GROUND, detections_ao,
            "What a phone edge sends: the legacy 'detections' message, no drone_id, no face_embedding "
            "(appearance-only), no detection_profile, at most 1 MiB (LEGACY_MAX_BYTES) as compact JSON. "
            "Embeddings are shortened here; real lengths depend on the model.",
        ),
        "detections_with_face": case(
            None, EDGE_TO_GROUND, detections_face,
            "Only when the user turned face matching on: each detection also carries face_embedding.",
        ),
        "detections_no_video": case(
            None, EDGE_TO_GROUND, detections_no_video,
            "After stream_video false: thumbnail is null; crops and embeddings still go.",
        ),
        "telemetry_v1": case(None, EDGE_TO_GROUND, _telemetry("drone-1", battery=82, height_cm=100, is_flying=True,
                                                               rel_x_cm=35.5, rel_y_cm=120.0, lane_index=2,
                                                               lanes_total=8),
                             "A fleet edge's state report. A phone edge never sends telemetry."),
        "telemetry_v2_drone_link_down": case(
            None, EDGE_TO_GROUND, telemetry_v2,
            "A v2 edge whose drone link is down. A phone edge never sends telemetry.",
        ),
        "ack_assign_lanes": case(None, EDGE_TO_GROUND, fp.make_ack_assign_lanes("drone-1", 90, 8),
                                 "Fleet edges only."),
        "nack_assign_lanes_geofence": case(
            None, EDGE_TO_GROUND,
            fp.make_nack_assign_lanes("drone-1", "geofence", 8,
                                      geofence={"geofence_cm": 450.0, "x_cm": 470.0, "y_cm": 20.0}),
            "Fleet edges only. A phone edge never nacks.",
        ),
        "nack_v2_drone_unreachable": case(
            None, EDGE_TO_GROUND,
            fp.make_nack("takeoff", "drone-2", "drone_unreachable", "cmd-takeoff-0001",
                         detail="Tello unreachable (no state for 12 s)"),
            "Fleet edges only.",
        ),
        "profile_ack_fallback": case(
            None, EDGE_TO_GROUND,
            fp.make_profile_ack("drone-1", "cmd-profile-0001", "default", requested="top_down", fallback=True,
                                fallback_reason="weights_missing"),
            "Fleet edges only.",
        ),
        "profile_nack_in_flight": case(
            None, EDGE_TO_GROUND,
            fp.make_profile_nack("drone-1", "cmd-profile-0002", "in_flight", profile="default",
                                 requested="top_down", detail="detection profiles change only on the ground"),
            "Fleet edges only.",
        ),
        "ceiling_ack": case(
            None, EDGE_TO_GROUND,
            fp.make_ceiling_ack("drone-1", "cmd-ceiling-0001",
                                {"ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 100}),
            "Fleet edges only.",
        ),
        "ceiling_nack": case(
            None, EDGE_TO_GROUND,
            fp.make_ceiling_nack("drone-1", "cmd-ceiling-0002", "in_flight",
                                 active={"ceiling_cm": 200, "ceiling_margin_cm": 60, "alt_slot_cm": 100},
                                 detail="the ceiling changes only on the ground"),
            "Fleet edges only.",
        ),
        "manual_ack": case(None, EDGE_TO_GROUND,
                           fp.make_manual_ack("manual_hold", "drone-2", "cmd-manual-0001", control_mode="manual"),
                           "Fleet edges only."),
        "manual_nack": case(
            None, EDGE_TO_GROUND,
            fp.make_manual_nack("manual_move", "drone-2", "cmd-manual-0002", "ceiling",
                                detail="the move would climb above the ceiling limit"),
            "Fleet edges only.",
        ),
        "pong": case(None, EDGE_TO_GROUND, fp.make_pong(1000.0, 1000.25, 1000.26),
                     "Fleet edges only. A phone edge never answers a ping."),
    }
    for name, entry in e2g.items():
        fp.validate_edge_to_ground(entry["payload"], proto=2 if "v2" in name else 1)

    common = {"link": "raw WebSocket ws://HOST:9000 (not Socket.IO)", "max_message_bytes": fp.LEGACY_MAX_BYTES}
    return {
        "edge_ground_to_edge.json": document(
            "Edge link, ground to edge: every message a phone acting as an edge may receive. Unknown types are "
            "ignored without closing the connection.",
            ["flightrisk.fleet_protocol builders", "GroundTransport.send_target", "GroundTransport.send_stream_video",
             "flightrisk.fleet_protocol.validate_ground_to_edge"],
            g2e, **common,
        ),
        "edge_edge_to_ground.json": document(
            "Edge link, edge to ground: the 'detections' samples a phone edge sends, and the fleet-edge replies "
            "for reference (a phone never sends those).",
            ["EdgeRunner.to_dict", "flightrisk.fleet_protocol builders",
             "flightrisk.fleet_protocol.validate_edge_to_ground"],
            e2g, **common,
        ),
    }


def build_nack_reasons() -> dict:
    from flightrisk import fleet_protocol as fp

    sets = {
        "assign_lanes_v1": fp.NACK_REASONS,
        "fenced_command_v2": fp.NACK_REASONS_V2,
        "set_detection_profile": fp.PROFILE_NACK_REASONS,
        "profile_fallback": fp.PROFILE_FALLBACK_REASONS,
        "set_ceiling": fp.CEILING_NACK_REASONS,
        "manual_command": fp.MANUAL_NACK_REASONS,
        "control_modes": fp.CONTROL_MODES,
        "hold_reasons": fp.HOLD_REASONS,
        "edge_states": fp.EDGE_STATES,
    }
    cases = {name: case(None, EDGE_TO_GROUND, sorted(values), "") for name, values in sets.items()}
    for name, note in {
        "assign_lanes_v1": "NACK_REASONS: why an edge refused assign_lanes on a v1 link.",
        "fenced_command_v2": "NACK_REASONS_V2: any refused fenced command on a v2 link.",
        "set_detection_profile": "PROFILE_NACK_REASONS: detection_profiles drones[].refusal.reason.",
        "profile_fallback": "PROFILE_FALLBACK_REASONS: detection_profiles drones[].fallback_reason.",
        "set_ceiling": "CEILING_NACK_REASONS: ceiling_settings drones[].edge.refusal.reason.",
        "manual_command": "MANUAL_NACK_REASONS: manual_result.reason when the edge refused.",
        "control_modes": "CONTROL_MODES: fleet_status drones[].control_mode and edge_control_mode values.",
        "hold_reasons": "HOLD_REASONS: the reason of a v2 hold.",
        "edge_states": "EDGE_STATES: the v2 telemetry state field.",
    }.items():
        cases[name]["notes"] = note + " Treat any other value as text to show, never as an error."
        cases[name]["direction"] = "reference"
    return document(
        "Reason and mode vocabularies from the fleet protocol constants. Ground-side manual results add "
        "'not_delivered', 'no_reply', 'no_telemetry' and 'invalid' (flightrisk/dashboard/manual_control.py).",
        ["flightrisk.fleet_protocol.constants", "flightrisk.fleet_protocol.manual", "flightrisk.fleet_protocol.ceiling"],
        cases,
    )


def build_client_emits() -> dict:
    from flightrisk.dashboard.app import socketio as dashboard_socketio

    handlers = set(dashboard_socketio.server.handlers.get("/", {}))
    missing = sorted((set(ALLOWED_CLIENT_EMITS) | set(FORBIDDEN_CLIENT_EMITS)) - handlers)
    if missing:
        raise AssertionError(f"the dashboard has no handler for {missing}")
    cases = {
        name: case(name, CLIENT_TO_SERVER, entry["payload"], entry["guard"], allowed=True)
        for name, entry in ALLOWED_CLIENT_EMITS.items()
    }
    for name, why in FORBIDDEN_CLIENT_EMITS.items():
        cases[f"forbidden_{name}"] = case(name, CLIENT_TO_SERVER, None, why, allowed=False)
    return document(
        "The Socket.IO events a phone may emit (allowed true, with an example payload) and the ones it must never "
        "emit (allowed false). Every listed event has a handler on the dashboard; the table itself is the "
        "contract and has no builder.",
        ["scripts/export_mobile_contract_fixtures.py ALLOWED_CLIENT_EMITS and FORBIDDEN_CLIENT_EMITS",
         "flightrisk.dashboard.app.socketio handlers"],
        cases,
    )


# ---------------------------------------------------------------------------
# Unknown-key variants
# ---------------------------------------------------------------------------


def add_unknown_keys(value: Any, parent_key: str | None = None) -> Any:
    """Copy *value* with ``UNKNOWN_KEY`` added to every record object at every depth."""
    if isinstance(value, list):
        return [add_unknown_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    if parent_key in MAP_KEYS:
        return {key: add_unknown_keys(item) for key, item in value.items()}
    out = {key: add_unknown_keys(item, key) for key, item in value.items()}
    out[UNKNOWN_KEY] = "ignore me"
    return out


UNKNOWN_KEY_SOURCES = {
    "status": ("status.json", "fleet_two_drones"),
    "fleet_status": ("fleet_status.json", "one_held_orphan_countdown"),
    "fleet_status_non_fleet": ("fleet_status.json", "non_fleet_local_drone"),
    "fleet_event": ("fleet_event.json", "retask"),
    "fleet_plan": ("fleet_plan.json", "map_plan_lost_zone_uncovered"),
    "fleet_detection": ("fleet_detection.json", "new_detection"),
    "frame": ("frame.json", "edge_video_with_match"),
    "match_alert": ("match_alert.json", "edge_match"),
    "detection_profiles": ("detection_profiles.json", "refused"),
    "ceiling_settings": ("ceiling_settings.json", "edge_refused"),
    "reasoning_provider": ("reasoning_provider.json", "local_gemma_cloud_blocked"),
    "manual_status": ("manual_status.json", "held_orphan_countdown"),
    "manual_ack": ("manual_ack.json", "land_ok"),
    "manual_result": ("manual_result.json", "land_acked"),
    "land_all_ack": ("land_all_ack.json", "fleet_reached_all"),
    "error": ("error.json", "manual_refusal"),
    "edge_set_target": ("edge_ground_to_edge.json", "set_target"),
    "edge_stream_video": ("edge_ground_to_edge.json", "stream_video"),
}


def build_unknown_keys(files: Mapping[str, dict]) -> dict:
    cases = {}
    for name, (file_name, case_name) in UNKNOWN_KEY_SOURCES.items():
        source = files[file_name]["cases"][case_name]
        cases[name] = case(
            source.get("event"), source["direction"], add_unknown_keys(source["payload"]),
            f"{file_name} case {case_name!r} with {UNKNOWN_KEY!r} added to every record object. A decoder must "
            "produce exactly what it produces for the original.",
            source_file=file_name, source_case=case_name,
        )
    return document(
        "Forward compatibility: real payloads with an unknown key added at every object level (never inside maps "
        "keyed by drone id or provider name). Decoders must ignore unknown keys everywhere.",
        ["scripts/export_mobile_contract_fixtures.py add_unknown_keys"],
        cases, unknown_key=UNKNOWN_KEY, map_keys=sorted(MAP_KEYS),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_all() -> dict[str, dict]:
    """Every fixture file, by file name."""
    files: dict[str, dict] = {}
    with seeded_uuid4():
        files["socketio_frames.json"] = build_socketio_frames()
        files["status.json"] = build_status()
        files.update(build_fleet_files(_fleet_scenarios()))
        files.update(build_frame_and_alert())
        files["detection_profiles.json"] = build_detection_profiles()
        files["ceiling_settings.json"] = build_ceiling_settings()
        files["reasoning_provider.json"] = build_reasoning_provider()
        files.update(build_handler_replies())
        files.update(build_edge_link())
        files["nack_reasons.json"] = build_nack_reasons()
        files["client_emits.json"] = build_client_emits()
    files["unknown_keys.json"] = build_unknown_keys(files)
    return files


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_all(out_dir: Path, commit: str | None = None) -> list[Path]:
    """Build every fixture and write it to *out_dir*; returns the paths written."""
    commit = commit if commit is not None else _git_commit()
    with scrubbed_environment():
        from flightrisk.config import reset_config

        reset_config()
        # The ground code logs every safety decision; none of it belongs in the output.
        logging.disable(logging.CRITICAL)
        try:
            files = build_all()
        finally:
            logging.disable(logging.NOTSET)
            reset_config()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, doc in sorted(files.items()):
        doc["_meta"]["generated_at_commit"] = commit
        doc["_meta"]["file"] = name
        path = out_dir / name
        path.write_text(render(doc), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=FIXTURE_DIR, help=f"output directory (default {FIXTURE_DIR})")
    args = parser.parse_args(argv)
    written = write_all(args.out)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
