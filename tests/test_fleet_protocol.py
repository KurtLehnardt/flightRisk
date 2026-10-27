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
import time

import pytest

from flightrisk import fleet_protocol as fp


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
    assert fp.EDGE_TO_GROUND_TYPES == {"pong", "ack", "telemetry"}


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
    import numpy as np

    from flightrisk.edge import EdgeRunner
    from flightrisk.ground import GroundStation

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
    import numpy as np

    from flightrisk.edge import EdgeRunner

    def broken_pose():
        raise RuntimeError("no pose yet")

    runner = EdgeRunner(pose_provider=broken_pose)
    data = runner.to_dict(runner.process_frame(np.zeros((4, 4, 3), dtype=np.uint8)))
    for key in ("drone_id", "rel_x_cm", "rel_y_cm", "heading_deg", "lat", "lon"):
        assert key not in data
    assert EdgeRunner.from_dict(data).drone_id is None
