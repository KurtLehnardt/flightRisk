"""Opt-in: the MAVLink fleet edge against a running ArduPilot (or PX4) SITL. Simulation only.

Skipped by default: the ``sitl`` marker is excluded in ``pytest.ini``, and the
tests also skip unless ``FLIGHTRISK_SITL_ADDRESS`` names a running SITL. See
``docs/sitl-demo.md``, "Fleet edge in SITL", for how to start one::

    FLIGHTRISK_SITL_ADDRESS=tcpout://127.0.0.1:5760 \\
        python -m pytest tests/integration/test_sitl_fleet.py -m sitl -s -p no:cacheprovider

``FLIGHTRISK_SITL_ADDRESS_2`` (a second SITL instance) adds the two-drone
test. Run this file on its own: ``tests/test_mavlink.py`` installs a fake
``mavsdk`` package when it is collected, and these tests need the real one.

The tests set the failsafe parameters the edge requires on the simulated
autopilot (geofence, low-battery and RC-loss failsafes, a return altitude
of 0 so a return keeps the drone's slot), exactly as an operator must on a
real one. They talk only to the SITL addresses given.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time

import pytest

pytestmark = [pytest.mark.sitl, pytest.mark.integration]

ADDRESS = os.environ.get("FLIGHTRISK_SITL_ADDRESS", "").strip()
ADDRESS_2 = os.environ.get("FLIGHTRISK_SITL_ADDRESS_2", "").strip()
#: Under amd64 emulation the EKF can take minutes to settle.
READY_TIMEOUT_S = float(os.environ.get("FLIGHTRISK_SITL_READY_TIMEOUT_S", "300"))
FLIGHT_TIMEOUT_S = 600.0

ARDUPILOT_SETUP = {
    "FENCE_ENABLE": 1, "FENCE_ACTION": 1, "FENCE_TYPE": 3, "FENCE_RADIUS": 150, "FENCE_ALT_MAX": 40,
    "BATT_FS_LOW_ACT": 2, "BATT_LOW_VOLT": 10.5, "FS_THR_ENABLE": 1, "RTL_ALT": 0,
}
PX4_SETUP = {
    "GF_ACTION": 2, "GF_MAX_HOR_DIST": 150.0, "GF_MAX_VER_DIST": 40.0, "COM_LOW_BAT_ACT": 3, "NAV_RCL_ACT": 2,
    "NAV_DLL_ACT": 2, "RTL_RETURN_ALT": 0.0,
}


def _need(address: str) -> None:
    if not address:
        pytest.skip("set FLIGHTRISK_SITL_ADDRESS to a running SITL (docs/sitl-demo.md)")
    mavsdk = sys.modules.get("mavsdk")
    if mavsdk is not None and getattr(mavsdk, "__file__", None) is None:
        pytest.skip("a fake mavsdk package is loaded (tests/test_mavlink.py); run this file on its own")
    pytest.importorskip("mavsdk")


def wait_for(predicate, timeout, interval=0.2):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _connect(address: str, drone_id: str, slot_cm: float):
    from flightrisk.drone.autopilot import ARDUPILOT
    from flightrisk.drone.mavlink_fleet import MavlinkFleetDrone, MavlinkFleetOptions
    from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink

    link = MavsdkAutopilotLink(address, connect_timeout_s=120.0)
    drone = MavlinkFleetDrone(drone_id, link, MavlinkFleetOptions(frame_heading_deg=0.0, speed_mps=3.0))
    drone.bind_slot_source(lambda: slot_cm)
    assert drone.connect(), f"cannot connect to SITL at {address}"
    setup = ARDUPILOT_SETUP if link.state().autopilot == ARDUPILOT else PX4_SETUP
    for name, value in setup.items():
        link.set_param(name, value)
    drone.refresh_params()
    ready = wait_for(lambda: drone.link_status().ok, READY_TIMEOUT_S, 1.0)
    assert ready, f"SITL never became ready: {drone.link_status()}"
    return drone


class SitlEdge:
    """In-process EdgeLink: the real LaneExecutor on a MavlinkFleetDrone flying a SITL autopilot."""

    def __init__(self, drone_id: str, address: str, slot_cm: float, geofence_cm: float, link_loss_land_s=30.0):
        from flightrisk import fleet_protocol as fp
        from flightrisk.drone.lane_executor import LaneExecutor
        from flightrisk.drone.safety import SafetyEnvelope
        from flightrisk.observability import StructuredLogger

        self._fp = fp
        self.drone_id = drone_id
        self.on_message = None
        self.replies: list[dict] = []
        self.envelope = SafetyEnvelope.for_field(alt_slot_cm=slot_cm, geofence_cm=geofence_cm,
                                                 link_loss_land_s=link_loss_land_s)
        self.drone = _connect(address, drone_id, slot_cm)
        self.drone.bind_slot_source(lambda: self.envelope.alt_slot_cm)
        self.executor = LaneExecutor(self.drone, self.envelope, drone_id, send=self._reply,
                                     logger=StructuredLogger(component=f"sitl-{drone_id}"))
        self.executor.start()
        # A real ground sends a heartbeat every second; clearing this event
        # stops them, as a lost ground link would.
        self.ground_alive = threading.Event()
        self.ground_alive.set()
        self._closed = threading.Event()
        self._heartbeats = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeats.start()

    def _heartbeat_loop(self) -> None:
        seq = 0
        while not self._closed.wait(1.0):
            if self.ground_alive.is_set():
                seq += 1
                self.send({"type": "heartbeat", "seq": seq})

    def _reply(self, msg: dict) -> None:
        self._fp.validate_message(msg)
        self.replies.append(msg)
        if self.on_message is not None:
            self.on_message(msg)

    def send(self, msg: dict) -> None:
        from flightrisk.drone.lane_executor import CONTROL_TYPES

        self._fp.validate_message(msg)
        self.executor.note_ground_message()
        if msg["type"] in CONTROL_TYPES:
            self.executor.handle_message(msg, recv_t=self.executor.now())

    def close(self) -> None:
        self._closed.set()
        try:
            self.executor.shutdown(timeout=60.0)
            wait_for(lambda: not self.drone.state.is_flying, 120.0, 1.0)
        finally:
            self.drone.disconnect()


def _fly_field_plan(addresses: list[str]) -> None:
    from flightrisk.drone.coordinator import FleetCoordinator
    from flightrisk.drone.fleet_scale import field_scale

    n = len(addresses)
    scale = field_scale(n, area_m=(24.0 * n, 20.0), alt_slots_m=(10.0, 15.0)[:n], overlap=0.2)
    settings = scale.edge_settings(n)
    edges = {}
    try:
        for i, address in enumerate(addresses):
            drone_id = f"sitl-{i + 1}"
            edges[drone_id] = SitlEdge(drone_id, address, settings[i]["alt_slot_m"] * 100,
                                       settings[i]["geofence_m"] * 100)
        coord = FleetCoordinator(edges, retask_wait_s=5.0, **scale.coordinator_kwargs())
        coord.plan()
        for edge in edges.values():
            assert any(m["type"] == "ack" and m.get("for") == "assign_lanes" for m in edge.replies), edge.replies
        coord.sync(rounds=4)
        result = coord.start(lead_s=2.0)
        assert result.ok, result.refusals
        started = time.monotonic()
        for edge in edges.values():
            total = edge.executor.lanes_total
            done = wait_for(lambda e=edge, t=total: e.executor.lane_index == t and not e.executor.is_busy,
                            FLIGHT_TIMEOUT_S, 1.0)
            assert done, (edge.drone_id, edge.executor.lane_index, total)
        flight_s = time.monotonic() - started
        for i, edge in enumerate(edges.values()):
            pose = edge.drone.measured_pose()
            height = edge.drone.read_height_cm()
            print(f"\n{edge.drone_id}: lanes done in {flight_s:.0f} s, end pose x={pose.x_cm:.0f} cm "
                  f"y={pose.y_cm:.0f} cm, height {height} cm (slot {settings[i]['alt_slot_m'] * 100:.0f} cm)")
            # A parallel track of one strip ends one strip to the right of the takeoff point.
            assert math.hypot(pose.x_cm - scale.strip_cm, pose.y_cm) < 300, pose
            assert abs(height - settings[i]["alt_slot_m"] * 100) < 150, height
        coord.land_all()
        for edge in edges.values():
            assert wait_for(lambda e=edge: not e.drone.state.is_flying, 180.0, 1.0), edge.drone_id
    finally:
        for edge in edges.values():
            edge.close()


def test_one_sitl_drone_flies_a_field_plan():
    _need(ADDRESS)
    _fly_field_plan([ADDRESS])


def test_two_sitl_drones_fly_a_field_plan():
    _need(ADDRESS)
    if not ADDRESS_2:
        pytest.skip("set FLIGHTRISK_SITL_ADDRESS_2 to a second SITL instance")
    _fly_field_plan([ADDRESS, ADDRESS_2])


def test_ground_link_loss_returns_to_launch_in_sitl():
    _need(ADDRESS)
    edge = SitlEdge("sitl-1", ADDRESS, 1000.0, 3000.0, link_loss_land_s=3.0)
    try:
        edge.send({"type": "takeoff"})

        assert wait_for(lambda: edge.drone.state.is_flying and not edge.executor.is_busy, 180.0, 0.5)
        edge.ground_alive.clear()  # the ground link goes silent
        # The ground goes silent: after 3 s the edge asks the autopilot to return.
        mode = wait_for(lambda: edge.drone.state.flight_mode == "RETURN_TO_LAUNCH" and "RETURN_TO_LAUNCH",
                        60.0, 0.5)
        assert mode, edge.drone.state
        assert wait_for(lambda: not edge.drone.state.is_flying, 240.0, 1.0)
        pose = edge.drone.measured_pose()
        print(f"\nreturned and landed {math.hypot(pose.x_cm, pose.y_cm):.0f} cm from the takeoff point")
    finally:
        edge.close()
