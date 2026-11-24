"""PROTO-7: describe_unit queries and their replies are logged as aggregated traffic."""
from __future__ import annotations

import pytest

from flightrisk import fleet_protocol as fp
from flightrisk import obs_comm


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def logged(monkeypatch):
    calls = []
    clock = _Clock()
    monkeypatch.setattr(obs_comm, "_agg", obs_comm._Aggregator(interval_s=5.0, clock=clock))
    monkeypatch.setattr(obs_comm, "log_event", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls, clock


def _query() -> dict:
    return fp.make_describe_unit("drone-1", cmd_id="q1")


def test_describe_unit_is_a_known_type_label():
    assert obs_comm.type_label("describe_unit") == "describe_unit"


def test_many_queries_log_one_aggregated_event(logged):
    calls, clock = logged
    for _ in range(50):
        obs_comm.edge_received(_query(), 60)
    assert calls == []
    clock.now = 6.0
    obs_comm.edge_received(_query(), 60)
    assert len(calls) == 1
    kwargs = calls[0][1]
    assert kwargs["aggregated"] is True
    assert kwargs["type"] == "describe_unit"
    assert kwargs["count"] == 51


def test_replies_are_aggregated_too(logged):
    calls, clock = logged
    nack = fp.make_describe_unit_nack("drone-1", "q1", "busy")
    for _ in range(20):
        obs_comm.ground_received(nack, 80)
    assert calls == []
    clock.now = 6.0
    obs_comm.ground_received(nack, 80)
    assert [c[1]["type"] for c in calls] == ["nack:describe_unit"]


def test_a_flight_command_is_still_logged_per_message(logged):
    calls, _ = logged
    obs_comm.ground_sent({"type": "land", "drone_id": "drone-1"}, "drone-1")
    obs_comm.ground_sent({"type": "land", "drone_id": "drone-1"}, "drone-1")
    assert len(calls) == 2
    assert all("aggregated" not in c[1] for c in calls)
