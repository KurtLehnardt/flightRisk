"""pytest fixtures for the network simulator and the two-node fleet harness.

``tests/conftest.py`` imports these names; they add fixtures and change none.
"""

from __future__ import annotations

import pytest

from tests.support.fleet_harness import FAST, TwoNodeHarness
from tests.support.leaks import ErrorWatch
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
def two_node_fleet(error_watch):
    """Two ground nodes and two fake edges (attached to node ``a``) over a ``NetSim``.

    The timings are the quick ``FAST`` set (1 s link-loss watchdog, 0.5 s
    heartbeats, 0.2 to 1 s reconnect backoff) so scenarios finish quickly; the
    product's values are the harness defaults.
    """
    harness = TwoNodeHarness(seed=1234, **FAST)
    try:
        harness.start()
        assert harness.wait_connected(), "the edges never reported to the ground node"
        yield harness
    finally:
        harness.stop()
