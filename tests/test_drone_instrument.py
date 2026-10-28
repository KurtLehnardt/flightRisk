"""Command telemetry applied by the backend registry (flightrisk/drone/instrument.py)."""

import inspect
import logging

import pytest

from flightrisk import obs_metrics as M
from flightrisk.drone import registry
from flightrisk.drone.controller import CommandOutcome, CommandResult, DroneController
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig, FakeDroneError
from flightrisk.drone.instrument import instrument_drone


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))

    record = add


@pytest.fixture
def rec(monkeypatch):
    out = {}
    for name in ("drone_commands", "drone_command_duration", "land_duration", "flight_safety_events"):
        out[name] = Recorder()
        monkeypatch.setattr(M, name, out[name])
    return out


def test_results_are_unchanged_and_counted(rec, caplog):
    drone = instrument_drone(FakeDrone(name="sim-1", config=FakeDroneConfig(drop_commands=frozenset({"rotate"}))), "sim")
    drone.connect()
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        assert drone.takeoff().ok
        assert drone.move("up", 50).ok
        dropped = drone.rotate(90)
        assert isinstance(dropped, CommandResult) and dropped.outcome is CommandOutcome.DROPPED_BUSY
        assert drone.land().ok
    outcomes = [(c[1]["command"], c[1]["outcome"]) for c in rec["drone_commands"].calls]
    assert outcomes == [("takeoff", "ok"), ("move", "ok"), ("rotate", "dropped_busy"), ("land", "ok")]
    assert rec["drone_commands"].calls[0][1]["drone_id"] == "sim-1"
    assert len(rec["land_duration"].calls) == 1
    comm = [r.fr_fields for r in caplog.records if getattr(r, "fr_event", None) == "drone_comm"]
    move = [c for c in comm if c.get("command") == "move"][-1]
    assert move["direction"] == "out" and move["distance_cm"] == 50 and move["outcome"] == "ok"
    assert drone.calls[-1] == ("land",)  # the backend still saw every command


def test_exceptions_propagate_and_land_failure_is_a_safety_event(rec):
    drone = instrument_drone(FakeDrone(config=FakeDroneConfig(fail_commands=frozenset({"land"}))), "sim")
    drone.connect()
    with pytest.raises(FakeDroneError):
        drone.land()
    assert rec["drone_commands"].calls[-1][1]["outcome"] == "error"
    assert rec["flight_safety_events"].calls[-1][1]["kind"] == "land_failed"


def test_type_protocols_and_signatures_are_preserved():
    drone = instrument_drone(FakeDrone(), "sim")
    assert type(drone) is FakeDrone
    assert isinstance(drone, DroneController)
    assert "abort" in inspect.signature(drone.takeoff).parameters
    assert instrument_drone(drone, "sim") is drone  # idempotent
    assert drone.takeoff.__wrapped__.__self__ is drone


def test_registry_fleet_factory_instruments_every_drone():
    spec = registry.get("sim")
    drone = spec.fleet_factory(spec.options({}))("sim-7", "sim://drone")
    assert getattr(drone, "_flightrisk_instrumented", False)
    assert not getattr(spec.create("sim-8", "sim://drone", spec.options({})), "_flightrisk_instrumented", False)
