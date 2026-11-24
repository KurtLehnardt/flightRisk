"""describe_unit queries cannot delay a link-loss landing (PROTO-6, plan step S24).

Two real ground stations and one flying edge over loopback WebSockets, built
like ``tests/test_edge_uplink.py``: real ``GroundTransportSync`` instances, the
real ``EdgeUplink`` and its fleet authority, and the real ``LaneExecutor`` and
link-loss watchdog on a ``FakeTelloController``. The edge answers
``describe_unit`` from a resolved Tello unit through ``edge_query.for_link``,
as the edge device does, so every query takes the real authority path.

The link-loss timeout is shortened to ``LAND_S`` and every scenario is timed
on ``time.monotonic``: from just before the authority's last heartbeat to the
moment the watchdog calls ``land_now("link_loss")``. The scenario with no
queries is the baseline. A standby ground asking every ``QUERY_EVERY_S``, or
the authority itself asking and sending nothing else, must land the drone
within the same window as the baseline. A query that fed the watchdog would
keep the drone up for as long as the queries continue, which here is well past
the window.
"""
from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from flightrisk import edge_query
from flightrisk import fleet_protocol as fp
from flightrisk.edge_device import _make_sender
from flightrisk.hardware.catalogue import BUILTIN_MODELS
from flightrisk.params.report import build_report
from flightrisk.params.resolver import resolve_unit
from tests.test_edge_uplink import DRONE, FlyingEdge, Ground, connected_to_both, leader, standby, wait_for

LAND_S = 1.5  # FLIGHTRISK_LINK_LOSS_LAND_S for every scenario here
EARLY_S = 0.1  # the watchdog never fires before LAND_S; this allows for the edge-side receive time
TOLERANCE_S = 1.0  # one window for every scenario, the baseline included
QUERY_EVERY_S = 0.2
# Queries go on until the drone lands or this long after the authority falls
# silent, so a query that fed the watchdog would hold the drone up past the window.
QUERY_FOR_S = LAND_S + TOLERANCE_S + 1.5


@pytest.fixture(scope="module")
def resolution():
    report = build_report(BUILTIN_MODELS["tello"], resolve_unit("tello", DRONE, {}))
    return SimpleNamespace(report=report)


@contextmanager
def rig(resolution):
    """A leader (the authority), a standby and a flying edge that answers describe_unit."""
    token = secrets.token_urlsafe(12)
    made: list[Ground] = []
    edge = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FLIGHTRISK_LINK_LOSS_LAND_S", str(LAND_S))
        try:
            authority = Ground(leader(), token=token)
            made.append(authority)
            spare = Ground(standby(), token=token)
            made.append(spare)
            edge = FlyingEdge([authority.url, spare.url], token=token)
            edge.runner.edge_query = edge_query.for_link(
                resolution, DRONE, edge.uplink, _make_sender(edge.uplink, edge.log), log=edge.log,
            )
            edge.start()
            assert connected_to_both(authority, spare)
            assert edge.uplink.authority.authority_leader == "mac"
            yield authority, spare, edge
        finally:
            if edge is not None:
                edge.close()
            for ground in made:
                ground.stop()


class Asker:
    """Sends describe_unit from one ground every QUERY_EVERY_S and records each send and reply."""

    def __init__(self, ground: Ground):
        self.ground = ground
        self.sent: list[float] = []
        self.replies: list[dict] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            self.sent.append(started)
            reply = self.ground.transport.send_command(DRONE, fp.make_describe_unit(DRONE), timeout_s=1.0)
            if reply is not None:
                self.replies.append(reply)
            self.stop.wait(max(0.0, QUERY_EVERY_S - (time.monotonic() - started)))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)


def landing_delay(resolution, asking: str | None):
    """Seconds from the authority's last heartbeat to the link-loss landing.

    *asking* names the ground that queries throughout: None, "standby" or
    "authority". Returns the delay and the Asker (or None).
    """
    with rig(resolution) as (authority, spare, edge):
        landed: list[tuple[str, float]] = []
        land_now = edge.flight.land_now
        edge.flight.land_now = lambda reason: (landed.append((reason, time.monotonic())), land_now(reason))[1]

        authority.transport.send_heartbeat()
        authority.transport.send_to_drone(DRONE, fp.make_takeoff(fence=leader().fence))
        assert wait_for(lambda: edge.flying), "the authority could not take off"

        ground = {"standby": spare, "authority": authority}.get(asking)
        asker = Asker(ground) if ground is not None else None
        if asker is not None:
            asker.__enter__()
            # The queries are already flowing when the authority falls silent.
            assert wait_for(lambda: asker.replies, timeout=3.0), "no reply to describe_unit before the silence"
        try:
            silent_from = time.monotonic()
            authority.transport.send_heartbeat()  # the authority's last non-query message
            deadline = silent_from + QUERY_FOR_S
            while not landed and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            if asker is not None:
                asker.__exit__(None, None, None)
        assert landed, f"no landing within {QUERY_FOR_S:.1f} s of the authority falling silent (asking={asking})"
        reason, landed_at = landed[0]
        assert reason == "link_loss"
        assert wait_for(lambda: not edge.flying, timeout=5.0)
        if asker is not None:
            during = [t for t in asker.sent if silent_from < t < landed_at]
            assert len(during) >= int(LAND_S / QUERY_EVERY_S) - 2, f"too few queries in the silence: {len(during)}"
            assert any(r["type"] == "ack" and r["cmd_id"] for r in asker.replies)
            for reply in asker.replies:
                fp.validate_describe_unit_reply(reply, proto=2)
                assert reply["type"] == "ack" or reply["reason"] == "busy", reply
        return landed_at - silent_from, asker


def assert_in_window(delay, label):
    assert LAND_S - EARLY_S <= delay <= LAND_S + TOLERANCE_S, (
        f"{label}: landed {delay:.3f} s after the authority fell silent, "
        f"outside [{LAND_S - EARLY_S:.2f}, {LAND_S + TOLERANCE_S:.2f}] s"
    )


@pytest.fixture(scope="module")
def baseline(resolution):
    """The same scenario with no queries, timed on the same clock."""
    delay, _ = landing_delay(resolution, None)
    assert_in_window(delay, "no queries")
    return delay


def test_standby_queries_every_0_2_s_do_not_delay_the_link_loss_landing(resolution, baseline):
    delay, asker = landing_delay(resolution, "standby")
    assert_in_window(delay, "standby asking every 0.2 s")
    assert abs(delay - baseline) <= TOLERANCE_S, f"queries {delay:.3f} s vs no queries {baseline:.3f} s"
    # The standby's queries were answered by the edge and gained it nothing.
    assert any(r["type"] == "ack" for r in asker.replies)


def test_the_authority_asking_and_sending_nothing_else_does_not_keep_the_link_alive(resolution, baseline):
    delay, asker = landing_delay(resolution, "authority")
    assert_in_window(delay, "authority asking every 0.2 s")
    assert abs(delay - baseline) <= TOLERANCE_S, f"queries {delay:.3f} s vs no queries {baseline:.3f} s"
    assert any(r["type"] == "ack" for r in asker.replies)

