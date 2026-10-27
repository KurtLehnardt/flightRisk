"""Tests of the two-node harness itself: switching ground, emergency wiring, start-up failure, defaults."""

import inspect
import socket
import threading
import time

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk.drone.safety import DEFAULT_LINK_LOSS_LAND_S
from flightrisk.edge_device import _FLEET_RECONNECT_MAX_DELAY_S
from flightrisk.transport import EdgeTransportSync
from tests.support import fleet_harness as fh
from tests.support.fleet_harness import FAST, GroundNode, TwoNodeHarness, wait_for
from tests.support.leaks import ErrorWatch, snapshot
from tests.support.netsim import BOTH

pytestmark = pytest.mark.filterwarnings("ignore:coroutine .EdgeTransport.send. was never awaited")


def _switch_in_background(fleet, drone_id: str, ground: str) -> tuple[threading.Thread, list]:
    outcome: list = []

    def run() -> None:
        try:
            fleet.switch_ground(drone_id, ground)
            outcome.append("switched")
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            outcome.append(exc)

    thread = threading.Thread(target=run, name="test-switch", daemon=True)
    thread.start()
    return thread, outcome


def _assert_edge_untouched(edge) -> None:
    assert edge.thread_alive, "the edge's frame thread died"
    assert edge.is_flying
    assert edge.landings() == 0, "the drone was landed"
    assert not edge.flight._closed, "the executor was shut down"


def test_a_switch_survives_a_target_that_stays_unreachable_for_over_ten_seconds(two_node_fleet):
    """The first connect attempt only gives up after the library's 10 s open timeout."""
    fleet = two_node_fleet
    assert fleet.takeoff_all()
    edge = fleet.edges["drone-2"]
    target = fleet.link("drone-2", "b")
    target.partition(BOTH, hold=True)  # the handshake of every attempt waits for the heal
    switching, outcome = _switch_in_background(fleet, "drone-2", "b")

    deadline = time.monotonic() + 11.5
    while time.monotonic() < deadline:
        _assert_edge_untouched(edge)
        time.sleep(0.5)
    assert switching.is_alive() and outcome == []

    target.heal()
    switching.join(30)
    assert outcome == ["switched"]
    assert fleet.wait_connected()
    assert fleet.grounds["b"].drone_ids == ["drone-2"]
    _assert_edge_untouched(edge)
    # The drone obeys its new node, and only that landing is recorded.
    assert fleet.grounds["b"].land("drone-2")
    assert wait_for(lambda: not edge.is_flying, 3.0)
    assert edge.landings() == 1


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_switch_to_a_refusing_target_keeps_the_edge_flying_and_retries(two_node_fleet):
    """The same fault as above but with quick failures: every failed attempt used to stop the live transport."""
    fleet = two_node_fleet
    assert fleet.takeoff_all()
    edge = fleet.edges["drone-2"]
    target = fleet.link("drone-2", "b")
    target.retarget(_closed_port())
    switching, outcome = _switch_in_background(fleet, "drone-2", "b")

    for _ in range(8):
        _assert_edge_untouched(edge)
        time.sleep(0.25)
    assert outcome == []
    assert "drone-2" in fleet.grounds["a"].drone_ids  # still attached to its old node

    target.retarget(fleet.grounds["b"].port)
    switching.join(30)
    assert outcome == ["switched"]
    assert wait_for(lambda: fleet.grounds["b"].drone_ids == ["drone-2"], 10.0)
    _assert_edge_untouched(edge)


def test_a_switch_that_gives_up_leaves_the_old_connection_in_place(two_node_fleet):
    fleet = two_node_fleet
    edge = fleet.edges["drone-2"]
    fleet.link("drone-2", "b").retarget(_closed_port())
    with pytest.raises(RuntimeError, match="could not connect"):
        edge.switch_to(fleet.link("drone-2", "b").url, give_up_s=1.0)
    assert edge.current_url == fleet.link("drone-2", "a").url
    assert edge.thread_alive
    assert wait_for(lambda: edge.transport.connected, 2.0)


def test_a_stopped_transport_reads_as_disconnected_and_never_raises(two_node_fleet):
    edge = two_node_fleet.edges["drone-1"]
    edge.transport.disconnect()  # stops that transport's event loop
    edge.proxy.send_detections(edge.runner.process_frame(edge.drone.get_frame()))
    assert edge.proxy.send({"type": "ack"}) is False
    assert edge.proxy.recv(recv_timeout=0.01) is None


def test_the_edge_lands_on_emergency_after_switching_nodes(two_node_fleet):
    fleet = two_node_fleet
    assert fleet.takeoff_all()
    edge = fleet.edges["drone-2"]
    fleet.switch_ground("drone-2", "b")
    assert wait_for(lambda: fleet.grounds["b"].drone_ids == ["drone-2"], 10.0)
    assert fleet.grounds["b"].emergency_land_all() == 1
    assert wait_for(lambda: len(edge.emergencies) == 1 and not edge.is_flying, 3.0)
    assert edge.landed_after_emergency(0)


def test_a_missing_landing_after_an_emergency_is_noticed(two_node_fleet):
    fleet = two_node_fleet
    assert fleet.takeoff_all()
    edge = fleet.edges["drone-1"]
    real_land_now = edge.flight.land_now
    edge.flight.land_now = lambda reason: None  # the edge receives the message and never lands
    assert fleet.grounds["a"].emergency_land_all() == 2
    assert wait_for(lambda: len(edge.emergencies) == 1, 3.0)
    time.sleep(0.3)
    assert not edge.landed_after_emergency(0)
    assert edge.is_flying
    edge.flight.land_now = real_land_now


def test_a_failed_start_stops_what_already_started():
    before = snapshot()
    real_start = GroundNode.start

    def start(self):
        if self.name == "b":
            raise RuntimeError("ground b cannot start")
        real_start(self)

    GroundNode.start = start
    try:
        with ErrorWatch() as watch, pytest.raises(RuntimeError, match="ground b cannot start"):
            TwoNodeHarness(seed=9, **FAST).start()
    finally:
        GroundNode.start = real_start
    assert watch.errors == []
    assert snapshot().leaked_since(before) == {}


def test_a_failed_edge_start_stops_everything_too():
    before = snapshot()
    real_start = fh.FleetEdge.start

    def start(self, url):
        if self.drone_id == "drone-2":
            raise RuntimeError("drone-2 cannot start")
        real_start(self, url)

    fh.FleetEdge.start = start
    try:
        with ErrorWatch() as watch, pytest.raises(RuntimeError, match="drone-2 cannot start"):
            TwoNodeHarness(seed=9, **FAST).start()
    finally:
        fh.FleetEdge.start = real_start
    assert watch.errors == []
    assert snapshot().leaked_since(before) == {}


def test_harness_defaults_mirror_the_product():
    harness_signature = inspect.signature(TwoNodeHarness).parameters
    assert harness_signature["heartbeat_interval_s"].default == fp.HEARTBEAT_INTERVAL_S == 1.0
    assert harness_signature["link_loss_land_s"].default == DEFAULT_LINK_LOSS_LAND_S == 3.0
    assert harness_signature["reconnect_base_delay_s"].default == inspect.signature(EdgeTransportSync).parameters[
        "base_delay"].default == 1.0
    assert harness_signature["reconnect_max_delay_s"].default == _FLEET_RECONNECT_MAX_DELAY_S == 2.0
    assert inspect.signature(GroundNode).parameters["heartbeat_interval_s"].default == fp.HEARTBEAT_INTERVAL_S
    edge = fh.FleetEdge("drone-9", 100, "t")
    assert (edge._reconnect, edge.link_loss_land_s) == ((1.0, 2.0), 3.0)
    assert edge.flight.telemetry_interval_s == fp.TELEMETRY_INTERVAL_S
