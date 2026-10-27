"""Tests for the fleet control protocol (R-090, R-091).

The pure protocol helpers in ``flightrisk.fleet_protocol`` are tested
directly. The transport side runs real ``websockets`` connections between a
``GroundTransport`` server and ``EdgeTransport`` clients on 127.0.0.1 with
OS-assigned ports, driven with plain ``asyncio.run()`` like
``tests/test_transport.py``. No real drone, camera or network is involved.

Timing assertions use generous margins because many test processes may share
the machine.
"""

import asyncio
import json
import logging
import threading
import time
from itertools import pairwise

import numpy as np
import pytest
import websockets

from flightrisk import fleet_protocol as fp
from flightrisk.edge import DetectionMessage, EdgeRunner
from flightrisk.ground import GroundStation
from flightrisk.transport import (
    ClockSyncError,
    EdgeTransport,
    EdgeTransportSync,
    GroundTransport,
    GroundTransportSync,
)

# ===========================================================================
# Pure protocol helpers
# ===========================================================================


def test_builders_produce_design_schemas():
    assert fp.make_ping(1.5) == {"type": "ping", "t1": 1.5}
    assert fp.make_pong(1.5, 2.0, 2.25) == {"type": "pong", "t1": 1.5, "t2": 2.0, "t3": 2.25}
    assert fp.make_clock(-4000.0) == {"type": "clock", "offset_s": -4000.0}
    assert fp.make_start_at(12.5) == {"type": "start_at", "t_ground_s": 12.5}
    assert fp.make_takeoff() == {"type": "takeoff"}
    assert fp.make_land() == {"type": "land"}
    assert fp.make_emergency_land_all() == {"type": "emergency_land_all"}
    assert fp.make_heartbeat(3) == {"type": "heartbeat", "seq": 3}
    assert fp.make_assign_lanes(
        "drone-1",
        100,
        [{"direction": "forward", "distance_cm": 200}],
        {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400},
    ) == {
        "type": "assign_lanes",
        "drone_id": "drone-1",
        "alt_cm": 100,
        "lanes": [{"direction": "forward", "distance_cm": 200}],
        "zone": {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400},
    }
    telemetry = fp.make_telemetry(
        drone_id="drone-2", battery=87, height_cm=160, is_flying=True,
        rel_x_cm=10.0, rel_y_cm=-20.0, heading_deg=90.0, lane_index=1,
        lanes_total=4, link_age_s=0.2, takeoff_at_local_s=None,
    )
    assert telemetry["type"] == "telemetry"
    assert set(telemetry) == {"type", *fp.TELEMETRY_FIELDS}
    assert fp.make_ack_start_at("drone-1", 42.0) == {
        "type": "ack", "for": "start_at", "drone_id": "drone-1", "executed_at_ground_s": 42.0,
    }
    assert fp.make_ack_assign_lanes("drone-1", 80, 4) == {
        "type": "ack", "for": "assign_lanes", "drone_id": "drone-1", "battery": 80, "lanes_total": 4,
    }
    assert fp.GROUND_TO_EDGE_TYPES == {
        "takeoff", "land", "emergency_land_all", "assign_lanes", "start_at", "ping", "clock", "heartbeat",
    }
    # R-108: ``nack`` joined the edge -> ground set (an intended change).
    assert fp.EDGE_TO_GROUND_TYPES == {"pong", "ack", "nack", "telemetry"}


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "ping"},
        {"type": "ping", "t1": "soon"},
        {"type": "pong", "t1": 1.0, "t2": 2.0},
        {"type": "clock", "offset_s": float("nan")},
        {"type": "start_at", "t_ground_s": None},
        {"type": "heartbeat", "seq": -1},
        {"type": "heartbeat", "seq": True},
        {"type": "assign_lanes", "drone_id": "d", "alt_cm": 100,
         "lanes": [{"direction": "up", "distance_cm": 50}],
         "zone": {"x_cm": 0, "y_cm": 0, "w_cm": 1, "h_cm": 1}},
        {"type": "assign_lanes", "drone_id": "d", "alt_cm": 100,
         "lanes": [{"direction": "forward", "distance_cm": 0}],
         "zone": {"x_cm": 0, "y_cm": 0, "w_cm": 1, "h_cm": 1}},
        {"type": "assign_lanes", "drone_id": "d", "alt_cm": 0, "lanes": [],
         "zone": {"x_cm": 0, "y_cm": 0, "w_cm": 1, "h_cm": 1}},
        {"type": "assign_lanes", "drone_id": "d", "alt_cm": 100, "lanes": [], "zone": {"x_cm": 0}},
        {"type": "ack", "for": "start_at", "drone_id": "d"},
        {"type": "ack", "for": "assign_lanes", "drone_id": "d", "battery": 50},
        {"type": "ack", "drone_id": "d"},
        {"type": "telemetry", "drone_id": "d"},
        {"type": "telemetry", "drone_id": "", "battery": 1, "height_cm": 0, "is_flying": False,
         "rel_x_cm": 0, "rel_y_cm": 0, "heading_deg": 0, "lane_index": None, "lanes_total": None,
         "link_age_s": None, "takeoff_at_local_s": None},
        {"no_type": True},
        ["ping"],
    ],
)
def test_validate_message_rejects_schema_violations(msg):
    with pytest.raises(fp.ProtocolError):
        fp.validate_message(msg)


def test_validate_message_passes_unknown_types_through():
    assert fp.validate_message({"type": "detections"}) == "detections"
    assert fp.validate_message({"type": "something_new", "x": 1}) == "something_new"


def test_clock_sample_math_uses_ntp_convention():
    # The edge clock runs exactly 4000 s ahead of the ground clock; the link
    # is symmetric with 10 ms each way and the edge takes 1 ms to answer.
    offset = 4000.0
    t1 = 100.0
    t2 = t1 + 0.010 + offset
    t3 = t2 + 0.001
    t4 = (t3 - offset) + 0.010
    sample = fp.ClockSample(t1, t2, t3, t4)
    assert sample.offset_s == pytest.approx(offset, abs=1e-9)
    assert sample.rtt_s == pytest.approx(0.020, abs=1e-9)
    assert sample.turnaround_s == pytest.approx(0.001, abs=1e-9)


def test_estimate_clock_keeps_median_of_rounds():
    offsets = [0.010, 0.011, 0.009, 0.010, 0.500, 0.012, 0.008, 0.010]
    samples = [fp.ClockSample(0.0, off + 0.001, off + 0.001, 0.002) for off in offsets]
    est = fp.estimate_clock(samples, measured_at_s=5.0)
    assert est.rounds == 8
    # The single 500 ms outlier does not move the median.
    assert est.offset_s == pytest.approx(0.010, abs=1e-9)
    assert est.measured_at_s == 5.0
    with pytest.raises(fp.ProtocolError):
        fp.estimate_clock([])


def test_sample_from_pong_rejects_backwards_timestamps():
    with pytest.raises(fp.ProtocolError):
        fp.sample_from_pong({"type": "pong", "t1": 1.0, "t2": 5.0, "t3": 4.0}, t4=2.0)
    with pytest.raises(fp.ProtocolError):
        fp.sample_from_pong({"type": "pong", "t1": 3.0, "t2": 5.0, "t3": 5.0}, t4=2.0)
    sample = fp.sample_from_pong({"type": "pong", "t1": 1.0, "t2": 5.0, "t3": 5.5}, t4=2.0)
    assert sample == fp.ClockSample(1.0, 5.0, 5.5, 2.0)


@pytest.mark.parametrize("offset", [4000.0, -4000.0])
def test_edge_clock_converts_start_at_with_offset(offset):
    edge_now = {"t": 10_000.0}
    clock = fp.EdgeClock(clock=lambda: edge_now["t"])
    assert clock.synced is False
    with pytest.raises(RuntimeError):
        clock.to_local(1.0)

    clock.apply(fp.make_clock(offset))
    assert clock.offset_s == offset
    # The edge fires start_at when its own clock reaches t_ground_s + offset_s.
    assert clock.to_local(6_000.0) == 6_000.0 + offset
    # The ack converts the execution time back with t_edge_exec - offset_s.
    assert clock.to_ground(6_000.0 + offset) == pytest.approx(6_000.0)
    assert fp.ground_to_edge_s(1.0, offset) == 1.0 + offset
    assert fp.edge_to_ground_s(1.0, offset) == 1.0 - offset
    assert clock.seconds_until(edge_now["t"] - offset + 2.0) == pytest.approx(2.0)


def test_answer_ping_echoes_t1_and_stamps_t3_last():
    pong = fp.answer_ping({"type": "ping", "t1": 7.25}, t2=100.0, clock=lambda: 100.004)
    assert pong == {"type": "pong", "t1": 7.25, "t2": 100.0, "t3": 100.004}
    with pytest.raises(fp.ProtocolError):
        fp.answer_ping({"type": "ping"}, t2=1.0)


def test_start_skews_relative_to_earliest_drone():
    assert fp.start_skews_s({}) == {}
    skews = fp.start_skews_s({"drone-1": 10.00, "drone-2": 10.04})
    assert skews["drone-1"] == pytest.approx(0.0)
    assert skews["drone-2"] == pytest.approx(0.04)


# ===========================================================================
# Detections carry the drone id and pose (edge.py / ground.py)
# ===========================================================================


def test_detections_carry_drone_id_and_pose_round_trip():
    class FakeDetector:
        def detect(self, frame):
            return [{"bbox": [1, 1, 5, 5], "confidence": 0.9}]

    pose = {"rel_x_cm": 120.0, "rel_y_cm": -40.0, "heading_deg": 90.0}
    runner = EdgeRunner(detector=FakeDetector(), drone_id="drone-2", pose_provider=lambda: pose)
    msg = runner.process_frame(np.zeros((10, 10, 3), dtype=np.uint8))
    data = runner.to_dict(msg)
    assert data["drone_id"] == "drone-2"
    assert (data["rel_x_cm"], data["rel_y_cm"], data["heading_deg"]) == (120.0, -40.0, 90.0)
    # No GPS on a Tello: lat and lon stay off the wire.
    assert "lat" not in data and "lon" not in data

    restored = EdgeRunner.from_dict(json.loads(json.dumps(data)))
    results = GroundStation().process_message(restored)
    assert results[0]["drone_id"] == "drone-2"
    assert results[0]["rel_x_cm"] == 120.0
    assert results[0]["rel_y_cm"] == -40.0
    assert results[0]["heading_deg"] == 90.0
    assert results[0]["lat"] is None


def test_detections_without_fleet_fields_keep_legacy_wire_format():
    def broken_pose():
        raise RuntimeError("no pose yet")

    runner = EdgeRunner(pose_provider=broken_pose)
    data = runner.to_dict(runner.process_frame(np.zeros((4, 4, 3), dtype=np.uint8)))
    for key in ("drone_id", "rel_x_cm", "rel_y_cm", "heading_deg", "lat", "lon"):
        assert key not in data
    assert EdgeRunner.from_dict(data).drone_id is None


# ===========================================================================
# Transport: in-process websockets server and clients on 127.0.0.1
# ===========================================================================

TOKEN = "fleet-test-token"
LANES = [{"direction": "forward", "distance_cm": 200}, {"direction": "right", "distance_cm": 50}]
ZONE = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}


def _telemetry(drone_id: str, battery: int = 90) -> dict:
    return fp.make_telemetry(
        drone_id=drone_id, battery=battery, height_cm=0, is_flying=False,
        rel_x_cm=0.0, rel_y_cm=0.0, heading_deg=0.0, lane_index=None,
        lanes_total=None, link_age_s=None, takeoff_at_local_s=None,
    )


async def _until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll *predicate* on the running loop until it is true or *timeout* passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def _until_sync(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


async def _start_ground(**kwargs) -> GroundTransport:
    kwargs.setdefault("token", TOKEN)
    gt = GroundTransport(host="127.0.0.1", port=0, **kwargs)
    await gt.start()
    return gt


async def _connect_edge(gt: GroundTransport, drone_id: str | None = None, **kwargs) -> EdgeTransport:
    """Connect an authenticated edge and, with *drone_id*, identify it via telemetry."""
    kwargs.setdefault("token", TOKEN)
    et = EdgeTransport(ws_url=f"ws://127.0.0.1:{gt.port}", **kwargs)
    await et.connect()
    if drone_id is not None:
        assert await et.send(_telemetry(drone_id)) is True
        assert await _until(lambda: drone_id in gt.drone_ids)
    return et


async def _recv_type(et: EdgeTransport, mtype: str, timeout: float = 5.0) -> dict:
    """Receive until a message of *mtype* arrives (skipping others)."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"no {mtype!r} message within {timeout} s"
        msg = await et.recv(timeout=remaining)
        if msg is not None and msg.get("type") == mtype:
            return msg


def test_ground_broadcast_control_reaches_authenticated_clients():
    async def scenario():
        gt = await _start_ground()
        e1 = await _connect_edge(gt, "drone-1")
        e2 = await _connect_edge(gt, "drone-2")
        # A client that connects but never authenticates gets no control.
        raw = await websockets.connect(f"ws://127.0.0.1:{gt.port}")
        try:
            assert await _until(lambda: len(gt.clients) == 3)
            for msg in (
                fp.make_takeoff(),
                fp.make_land(),
                fp.make_heartbeat(0),
                fp.make_start_at(gt.now() + 60.0),
                fp.make_clock(0.25),
            ):
                assert await gt.broadcast_control(msg) == 2
                assert await e1.recv(timeout=2.0) == msg
                assert await e2.recv(timeout=2.0) == msg
            with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                await asyncio.wait_for(raw.recv(), timeout=0.3)

            # Addressed control reaches only the named drone.
            assign = fp.make_assign_lanes("drone-2", 160, LANES, ZONE)
            assert await gt.send_to_drone("drone-2", assign) is True
            assert await e2.recv(timeout=2.0) == assign
            assert await e1.recv(timeout=0.2) is None
            assert await gt.send_to_drone("drone-9", fp.make_land()) is False

            # Malformed control fails loudly at the sender.
            with pytest.raises(fp.ProtocolError):
                await gt.broadcast_control({"type": "start_at"})
            with pytest.raises(fp.ProtocolError):
                await gt.send_to_drone("drone-1", {"type": "assign_lanes", "drone_id": "drone-1"})
        finally:
            await raw.close()
            await e1.disconnect()
            await e2.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_non_loopback_bind_without_token_is_still_refused():
    async def scenario():
        gt = GroundTransport(host="0.0.0.0", port=0, heartbeat_interval_s=1.0)
        with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TOKEN"):
            await gt.start()

    asyncio.run(scenario())


def test_edge_handles_each_control_type():
    controls = []
    landed = threading.Event()
    land_threads = []

    def on_emergency_land(msg):
        land_threads.append(threading.current_thread().name)
        landed.set()

    async def scenario():
        gt = await _start_ground(on_control=controls.append)
        et = await _connect_edge(gt, "drone-1", on_emergency_land=on_emergency_land)
        try:
            # ping/pong and clock: pings are answered by the transport and
            # never surface; the clock offset is stored and surfaced.
            estimate = await gt.sync_clock("drone-1")
            clock_msg = await et.recv(timeout=2.0)
            assert clock_msg == {"type": "clock", "offset_s": estimate.offset_s}
            assert et.clock.offset_s == estimate.offset_s
            assert et.link_age_s is not None and et.link_age_s < 2.0

            # Commands surface through recv() unchanged and in order.
            commands = [
                fp.make_assign_lanes("drone-1", 100, LANES, ZONE),
                fp.make_heartbeat(7),
                fp.make_start_at(gt.now() + 30.0),
                fp.make_takeoff(),
                fp.make_land(),
            ]
            for msg in commands:
                assert await gt.send_to_drone("drone-1", msg) is True
            for msg in commands:
                assert await et.recv(timeout=2.0) == msg

            # emergency_land_all: the callback fires on its own thread at
            # once, the message jumps the recv() queue, and a takeoff or
            # start_at queued before it is discarded.
            for msg in (fp.make_takeoff(), fp.make_start_at(gt.now() + 1.0), fp.make_land()):
                assert await gt.send_to_drone("drone-1", msg) is True
            assert await _until(lambda: len(et._inbox) == 3)
            assert await gt.emergency_land_all() == 1
            assert await _until(landed.is_set, timeout=2.0)
            assert land_threads == ["edge-emergency-land"]
            assert await et.recv(timeout=2.0) == {"type": "emergency_land_all"}
            assert await et.recv(timeout=2.0) == {"type": "land"}
            assert await et.recv(timeout=0.2) is None

            # Edge -> ground: acks and telemetry reach on_control.
            ack = fp.make_ack_assign_lanes("drone-1", 88, 2)
            assert await et.send(ack) is True
            assert await et.send(_telemetry("drone-1", battery=87)) is True
            assert await _until(lambda: len(controls) >= 3)
            assert controls[-2] == ack
            assert controls[-1]["battery"] == 87
            status = gt.fleet_status()["drone-1"]
            assert status["connected"] is True
            assert status["offset_s"] == estimate.offset_s
            assert status["telemetry"]["battery"] == 87

            # Malformed edge -> ground messages fail loudly at the sender.
            with pytest.raises(fp.ProtocolError):
                await et.send({"type": "ack", "for": "start_at", "drone_id": "drone-1"})
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_unknown_control_ignored(caplog):
    caplog.set_level(logging.WARNING, logger="flightrisk.transport")
    controls = []
    landed = threading.Event()

    async def scenario():
        gt = await _start_ground(on_control=controls.append)
        et = await _connect_edge(gt, "drone-1", on_emergency_land=lambda msg: landed.set())
        try:
            # Ground side: an unknown type and a malformed ack are logged and
            # ignored, and the link keeps working.
            assert await et.send({"type": "warp_drive", "factor": 9}) is True
            await et._ws.send(json.dumps({"type": "ack", "for": "start_at", "drone_id": "drone-1"}))
            assert await et.send(_telemetry("drone-1", battery=55)) is True
            assert await _until(lambda: any(c.get("battery") == 55 for c in controls))
            assert [c["type"] for c in controls] == ["telemetry", "telemetry"]
            assert "Unknown message type: 'warp_drive'" in caplog.text
            assert "Malformed ack message" in caplog.text

            # Edge side: an unknown type is passed through untouched for the
            # edge device to log and ignore; malformed ping and clock
            # messages change nothing; the transport still answers pings.
            assert await gt.broadcast_control({"type": "warp_drive"}) == 1
            assert await et.recv(timeout=2.0) == {"type": "warp_drive"}
            ws = next(iter(gt.clients))
            await ws.send(json.dumps({"type": "ping"}))
            await ws.send(json.dumps({"type": "clock", "offset_s": "late"}))
            await ws.send("not json {{{")
            assert await et.recv(timeout=0.3) is None
            assert et.clock.offset_s is None
            assert not landed.is_set()
            estimate = await gt.sync_clock("drone-1")
            assert estimate.rounds == fp.CLOCK_SYNC_ROUNDS
            assert et.connected is True
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_nack_reaches_ground_on_control(caplog):
    """R-108: an edge's refusal of a lane assignment reaches the ground.

    A real ``GroundTransport`` and ``EdgeTransport`` talk over 127.0.0.1. The
    nack passes the protocol validation on both ends, identifies the drone,
    and is handed to ``on_control`` unchanged; a malformed nack is dropped at
    the ground and never reaches ``on_control``.
    """
    caplog.set_level(logging.WARNING, logger="flightrisk.transport")
    controls = []

    async def scenario():
        gt = await _start_ground(on_control=controls.append)
        # The edge has sent nothing else yet, so the nack alone must identify it.
        et = await _connect_edge(gt)
        try:
            nack = fp.make_nack_assign_lanes("drone-2", "geofence", 3)
            assert fp.validate_message(nack) == fp.NACK

            # A malformed nack fails loudly at the sender ...
            with pytest.raises(fp.ProtocolError):
                await et.send({"type": "nack", "for": "assign_lanes", "drone_id": "drone-2",
                               "reason": "tired", "lanes_total": 3})
            # ... and, when forced onto the wire, is rejected by the ground's
            # validation instead of reaching on_control.
            await et._ws.send(json.dumps({"type": "nack", "for": "assign_lanes",
                                          "drone_id": "drone-2", "lanes_total": 3}))

            assert await et.send(nack) is True
            assert await _until(lambda: len(controls) >= 1)
            # Delivery is in per-client order, so the malformed nack sent
            # first would already be here had it not been dropped.
            assert controls == [nack]
            assert "drone-2" in gt.drone_ids
            assert "Malformed nack message" in caplog.text
            assert "Unknown message type: 'nack'" not in caplog.text

            # A nack interleaved with ack and telemetry keeps its place.
            ack = fp.make_ack_assign_lanes("drone-2", 88, 1)
            other = fp.make_nack_assign_lanes("drone-2", "battery", 1)
            for msg in (ack, other, _telemetry("drone-2", battery=20)):
                assert await et.send(msg) is True
            assert await _until(lambda: len(controls) >= 4)
            assert [c["type"] for c in controls] == ["nack", "ack", "nack", "telemetry"]
            assert controls[2] == other
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_emergency_land_all_reaches_every_client_and_is_never_silent(caplog):
    caplog.set_level(logging.ERROR, logger="flightrisk.transport")
    landed_at = []

    async def scenario():
        gt = await _start_ground()
        assert await gt.emergency_land_all() == 0
        assert "no edge clients are connected" in caplog.text

        et = await _connect_edge(gt, "drone-1", on_emergency_land=lambda msg: landed_at.append(time.monotonic()))
        # A client still in the auth handshake also gets the land order.
        raw = await websockets.connect(f"ws://127.0.0.1:{gt.port}")
        try:
            assert await _until(lambda: len(gt.clients) == 2)
            # Ordinary traffic is already queued at the edge and unread.
            for seq in range(5):
                await gt.send_heartbeat()
            sent_at = time.monotonic()
            assert await gt.emergency_land_all() == 2
            assert await _until(lambda: landed_at, timeout=2.0)
            assert landed_at[0] - sent_at < 1.0
            assert json.loads(await asyncio.wait_for(raw.recv(), timeout=2.0)) == {"type": "emergency_land_all"}
            assert await et.recv(timeout=2.0) == {"type": "emergency_land_all"}
            assert (await et.recv(timeout=2.0))["type"] == "heartbeat"
        finally:
            await raw.close()
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())

    # The sync wrapper raises rather than dropping the order when not running.
    with pytest.raises(RuntimeError):
        GroundTransportSync(host="127.0.0.1", port=0).emergency_land_all()


def test_heartbeat_broadcast_at_interval():
    async def scenario():
        gt = await _start_ground(heartbeat_interval_s=0.05)
        et = await _connect_edge(gt)
        try:
            seqs = []
            for _ in range(3):
                seqs.append((await _recv_type(et, "heartbeat"))["seq"])
            assert seqs == sorted(seqs) and len(set(seqs)) == 3
            assert et.link_age_s is not None and et.link_age_s < 2.0
        finally:
            await et.disconnect()
            await gt.stop()
        assert gt._heartbeat_task is None

    asyncio.run(scenario())


def test_clock_sync_fails_loudly_without_pongs():
    async def scenario():
        gt = await _start_ground()
        with pytest.raises(LookupError):
            await gt.sync_clock("drone-1")
        # An authenticated client that identifies itself but never answers.
        raw = await websockets.connect(f"ws://127.0.0.1:{gt.port}")
        try:
            await raw.send(json.dumps({"type": "auth", "token": TOKEN}))
            await raw.send(json.dumps(_telemetry("drone-1")))
            assert await _until(lambda: "drone-1" in gt.drone_ids)
            with pytest.raises(ClockSyncError):
                await gt.sync_clock("drone-1", round_timeout_s=0.05)
            assert gt.clock_estimate("drone-1") is None
        finally:
            await raw.close()
            await gt.stop()

    asyncio.run(scenario())


def test_slow_detection_callback_does_not_delay_pongs():
    received = []

    def slow_on_message(msg):
        time.sleep(0.2)
        received.append(msg.frame_id)

    async def scenario():
        gt = await _start_ground(on_message=slow_on_message)
        et = await _connect_edge(gt, "drone-1")
        try:
            for frame_id in (1, 2, 3):
                await et.send_detections(DetectionMessage(timestamp=1.0, frame_id=frame_id))
            estimate = await gt.sync_clock("drone-1")
            # With scoring inline on the read loop the first pong would wait
            # behind 0.6 s of callbacks.
            assert max(s.rtt_s for s in estimate.samples) < 0.3
            assert await _until(lambda: len(received) == 3)
            assert received == [1, 2, 3]
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Clock sync accuracy (R-091). Ground and edge each run on their own event
# loop thread through the sync wrappers, as they do in deployment.
# ---------------------------------------------------------------------------


def _sync_pair(edge_clock_shift_s: float = 0.0):
    ground = GroundTransportSync(host="127.0.0.1", port=0, token=TOKEN)
    ground.start()
    edge = EdgeTransportSync(
        ws_url=f"ws://127.0.0.1:{ground.port}",
        token=TOKEN,
        clock=lambda: time.monotonic() + edge_clock_shift_s,
    )
    edge.connect()
    assert edge.send(_telemetry("drone-1")) is True
    assert _until_sync(lambda: "drone-1" in ground.drone_ids())
    return ground, edge


def test_pong_timestamps_monotonic():
    ground, edge = _sync_pair()
    try:
        estimate = ground.sync_clock("drone-1")
        samples = estimate.samples
        assert estimate.rounds == fp.CLOCK_SYNC_ROUNDS == len(samples) == 8
        for s in samples:
            assert s.t1 <= s.t4  # ground clock
            assert s.t2 <= s.t3  # edge clock
        for prev, cur in pairwise(samples):
            # Rounds are sequential and every clock only moves forward.
            assert cur.t1 >= prev.t4
            assert cur.t2 > prev.t3
        turnarounds = sorted(s.turnaround_s for s in samples)
        assert turnarounds[len(turnarounds) // 2] < fp.PONG_BUDGET_S
        assert turnarounds[-1] < 0.5  # generous: the machine may be loaded
    finally:
        edge.disconnect()
        ground.stop()


@pytest.mark.parametrize("shift_s", [0.0, 123.456])
def test_offset_estimate_within_5ms_on_loopback(shift_s):
    ground, edge = _sync_pair(edge_clock_shift_s=shift_s)
    try:
        estimate = ground.sync_clock("drone-1")
        assert abs(estimate.offset_s - shift_s) < 0.005
        assert 0.0 <= estimate.rtt_s < 1.0
        assert ground.clock_estimate("drone-1") == estimate
        # The edge receives the median offset unchanged.
        assert _until_sync(lambda: edge.clock.offset_s is not None)
        assert edge.clock.offset_s == estimate.offset_s
        status = ground.fleet_status()["drone-1"]
        assert status["offset_s"] == estimate.offset_s
        assert status["rtt_s"] == estimate.rtt_s
    finally:
        edge.disconnect()
        ground.stop()


@pytest.mark.parametrize("edge_clock_offset_s", [4000.0, -4000.0])
def test_start_at_converted_with_offset(edge_clock_offset_s):
    """The edge fires start_at at t_ground_s + offset_s on its own clock.

    The edge's monotonic clock is 4000 s ahead of (or behind) the ground's.
    With the conversion sign reversed, the +4000 s edge would fire at once
    (0.3 s early) and the -4000 s edge would wait 8000 s; both fail here.
    """
    acks = []

    async def edge_executor(et: EdgeTransport, drone_id: str) -> None:
        msg = await _recv_type(et, "start_at")
        t_edge = et.clock.to_local(msg["t_ground_s"])
        while et.clock.now() < t_edge:
            await asyncio.sleep(min(0.005, max(0.0, t_edge - et.clock.now())))
        t_exec = et.clock.now()
        await et.send(fp.make_ack_start_at(drone_id, et.clock.to_ground(t_exec)))

    async def scenario():
        gt = await _start_ground(on_control=acks.append)
        et = await _connect_edge(
            gt, "drone-1", clock=lambda: time.monotonic() + edge_clock_offset_s,
        )
        try:
            estimate = await gt.sync_clock("drone-1")
            assert abs(estimate.offset_s - edge_clock_offset_s) < 0.005
            assert await _until(lambda: et.clock.synced)

            executor = asyncio.ensure_future(
                asyncio.wait_for(edge_executor(et, "drone-1"), timeout=5.0)
            )
            t_ground_s = gt.now() + 0.3
            assert await gt.send_to_drone("drone-1", fp.make_start_at(t_ground_s)) is True
            await executor
            assert await _until(lambda: any(a.get("for") == "start_at" for a in acks))
            ack_seen_at = gt.now()

            ack = next(a for a in acks if a.get("for") == "start_at")
            executed = ack["executed_at_ground_s"]
            # Fired on time in ground time (generous upper margin for load).
            assert -0.01 < executed - t_ground_s < 0.25
            # And not early in real time.
            assert ack_seen_at >= t_ground_s - 0.01
            status = gt.fleet_status()["drone-1"]
            assert status["executed_at_ground_s"] == executed
            assert status["start_skew_s"] == 0.0
            assert gt.start_skew_s() == 0.0
        finally:
            await et.disconnect()
            await gt.stop()

    asyncio.run(scenario())
