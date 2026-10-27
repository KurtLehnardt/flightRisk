"""pytest fixtures for the network simulator and the two-node fleet harness.

``tests/conftest.py`` imports these names; they add fixtures and change none.
"""

from __future__ import annotations

import pytest

from tests.support.fleet_harness import TwoNodeHarness
from tests.support.leaks import ErrorWatch, snapshot
from tests.support.netsim import NetSim


@pytest.fixture
def netsim():
    """A started ``NetSim`` (seed 1234), stopped when the test ends."""
    sim = NetSim(seed=1234).start()
    try:
        yield sim
    finally:
        sim.stop()


@pytest.fixture
def error_watch():
    """Records read-loop and thread errors; fails the test if any were seen."""
    with ErrorWatch() as watch:
        yield watch
    assert not watch.errors, "unexpected errors:\n" + "\n".join(watch.errors)


@pytest.fixture
def resource_baseline():
    """Snapshot taken before the test; call ``assert_no_leaks()`` at the end."""
    before = snapshot()

    class Baseline:
        snapshot = before

        @staticmethod
        def leaks(settle_s: float = 10.0) -> dict:
            return snapshot().leaked_since(before, settle_s)

    return Baseline


@pytest.fixture
def two_node_fleet(error_watch):
    """Two ground nodes and two fake edges (attached to node ``a``) over a ``NetSim``.

    The edges' link-loss watchdog is 1 s here (the product default is 3 s) so
    that scenarios finish quickly.
    """
    harness = TwoNodeHarness(seed=1234, link_loss_land_s=1.0)
    try:
        harness.start()
        assert harness.wait_connected(), "the edges never reported to the ground node"
        yield harness
    finally:
        harness.stop()
