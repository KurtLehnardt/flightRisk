"""Scenarios on the two-node harness: real transports, fake drones, a faulty network between.

Two ground nodes (``a`` and ``b``) and two edges (``drone-1``, ``drone-2``) run
on loopback through ``NetSim`` proxies. The edges' watchdog is 1 s so each
scenario takes a few seconds. These tests assert only what the fleet code on
main guarantees today; the leader and handover tests build on the same harness.
"""

import pytest

from tests.support.fleet_harness import TwoNodeHarness, wait_for
from tests.support.leaks import ErrorWatch, snapshot
from tests.support.netsim import BOTH, DOWN, UP

# A telemetry send racing a transport shutdown leaves one un-awaited coroutine
# (also ignored by tests/test_fake_fleet.py).
pytestmark = pytest.mark.filterwarnings("ignore:coroutine .EdgeTransport.send. was never awaited")


def test_edges_report_and_fly_through_the_proxy_links(two_node_fleet):
    fleet = two_node_fleet
    assert sorted(fleet.grounds["a"].drone_ids) == ["drone-1", "drone-2"]
    assert fleet.grounds["b"].drone_ids == []
    assert fleet.takeoff_all()
    for drone_id in fleet.edges:
        link = fleet.link(drone_id, "a")
        assert link.stats(UP).delivered > 0 and link.stats(DOWN).delivered > 0
        assert link.open_connections == 1
    assert fleet.grounds["a"].received("telemetry")


def test_watchdog_lands_only_the_drone_whose_link_went_silent(two_node_fleet):
    fleet = two_node_fleet
    assert fleet.takeoff_all()
    silent, healthy = fleet.edges["drone-1"], fleet.edges["drone-2"]
    fleet.link("drone-1", "a").partition(DOWN, hold=True)  # no heartbeat reaches drone-1; its socket stays open

    assert wait_for(lambda: not silent.is_flying, silent.link_loss_land_s + 2.0)
    assert healthy.is_flying
    assert silent.landings() >= 1

    # Healed, the edge hears the ground again and can be sent up again.
    fleet.link("drone-1", "a").heal()
    assert wait_for(lambda: silent.link_age_s < silent.link_loss_land_s, 3.0)
    assert fleet.grounds["a"].takeoff("drone-1")
    assert wait_for(lambda: silent.is_flying, 5.0)


def test_one_way_loss_of_the_uplink_does_not_land_a_drone_that_still_hears_the_ground(two_node_fleet):
    fleet = two_node_fleet
    assert fleet.takeoff_all()
    fleet.link("drone-1", "a").partition(UP)  # edge to ground silent; heartbeats still arrive
    assert not wait_for(lambda: not fleet.edges["drone-1"].is_flying, 1.3 * fleet.edges["drone-1"].link_loss_land_s)
    assert fleet.edges["drone-1"].landings() == 0


def test_emergency_land_all_reaches_every_edge_despite_a_dead_uplink_and_latency(two_node_fleet):
    fleet = two_node_fleet
    assert fleet.takeoff_all()
    fleet.link("drone-1", "a").set_faults(DOWN, latency_s=0.15)
    fleet.link("drone-2", "a").partition(UP)

    assert fleet.grounds["a"].emergency_land_all() == 2
    assert wait_for(lambda: not any(e.is_flying for e in fleet.edges.values()), 2.0)
    assert all(len(e.emergencies) == 1 for e in fleet.edges.values())


def test_a_severed_edge_reconnects_by_itself_and_the_ground_sees_it_again(two_node_fleet):
    fleet = two_node_fleet
    link = fleet.link("drone-2", "a")
    assert link.sever() == 1
    assert wait_for(lambda: link.accepted >= 2, 10.0)
    assert wait_for(lambda: link.open_connections == 1 and "drone-2" in fleet.grounds["a"].drone_ids, 10.0)
    assert fleet.grounds["a"].client_count() == 2


def test_an_edge_can_move_to_the_other_ground_node(two_node_fleet):
    fleet = two_node_fleet
    assert fleet.takeoff_all()
    fleet.switch_ground("drone-2", "b")
    assert fleet.wait_connected()
    assert fleet.grounds["b"].drone_ids == ["drone-2"]
    assert wait_for(lambda: fleet.grounds["a"].drone_ids == ["drone-1"], 5.0)
    # The new node's heartbeats feed the watchdog, and its orders are obeyed.
    assert fleet.edges["drone-2"].is_flying
    assert fleet.grounds["b"].land("drone-2")
    assert wait_for(lambda: not fleet.edges["drone-2"].is_flying, 3.0)
    assert fleet.edges["drone-1"].is_flying


def test_a_fault_free_run_and_teardown_leave_nothing_behind():
    before = snapshot()
    with ErrorWatch() as watch:
        harness = TwoNodeHarness(seed=5, link_loss_land_s=1.0).start()
        try:
            assert harness.wait_connected() and harness.takeoff_all()
            harness.link("drone-1", "a").set_faults(BOTH, latency_s=0.02, jitter_s=0.02)
            harness.link("drone-2", "a").flap(0.2, 0.2, cycles=2)
            wait_for(lambda: False, 0.6)
        finally:
            harness.stop()
    assert watch.errors == []
    assert snapshot().leaked_since(before) == {}

