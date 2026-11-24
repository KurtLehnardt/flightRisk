"""The lane knobs from the parameter registry, and the gimbal level gate (ROLE-4).

Part 1: ``HEADING_TOLERANCE_DEG`` and ``OBSTACLE_RETRY_PAUSE_S`` in ``flightrisk/drone/lane_flight.py``
read their registry rows and keep today's values and types; ``LaneExecutor`` takes its
``obstacle_retry_pause_s`` default from the second and its ``obstacle_turn`` default from ``sensing.turn``.

Part 2: on a gimbal mount every sense gate reads the camera's pitch; a missing pitch (including one
not measured, or read before the look started), or one whose absolute value exceeds ``sensing.level_pitch_tolerance_deg`` (5 degrees), makes the gate
``unavailable`` with the cause "camera not level", which cuts the lane. A fixed mount, and an
executor given no camera (every unit today), are gated exactly as before.

Fakes only: the stepped fake drone, the scripted sensor and the fake clock of
``tests.test_lane_executor_obstacle``.
"""
from __future__ import annotations

import dataclasses
import inspect
import math

import pytest

from flightrisk.drone import lane_flight
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.lane_steps import Cut
from flightrisk.drone.obstacle_sensor import (
    CUT_SENSOR_STALE,
    EVENT_OBSTACLE_GATE,
    EVENT_SENSE_STALE,
    OBSTACLE_SENSE_RETRIES,
)
from flightrisk.hardware.catalogue import CameraSpec
from flightrisk.hardware.features import (
    CAMERA_NOT_LEVEL_REASON,
    default_level_tolerance_deg,
)
from flightrisk.params import table as registry
from tests import test_lane_executor_obstacle as obstacle_tests
from tests.test_lane_executor_obstacle import (
    assert_invariants,
    events,
    fly,
    lane_moves,
    lanes_of,
)

INDOOR = registry.T
FIELD = registry.G

#: The stepped-sensing executor rig of ``test_lane_executor_obstacle`` (a fixture).
rigs = obstacle_tests.rigs


def _row(param_id: str):
    return {spec.id: spec for spec in registry.TABLE}[param_id]


# ---------------------------------------------------------------------------
# Part 1: the lane knobs read their registry rows
# ---------------------------------------------------------------------------

#: (module constant, registry row, today's value). Today's values are the indoor reference column.
LANE_CONSTANTS = [
    ("HEADING_TOLERANCE_DEG", "move.heading_tolerance_deg", 10.0),
    ("OBSTACLE_RETRY_PAUSE_S", "sensing.retry_pause_s", 0.5),
]


@pytest.mark.parametrize("name, param_id, today", LANE_CONSTANTS)
def test_lane_constant_equals_its_row_and_todays_value_and_type(name, param_id, today):
    value = getattr(lane_flight, name)
    row = _row(param_id).defaults[INDOOR]
    assert value == row == today
    assert type(value) is type(row) is type(today)


@pytest.mark.parametrize("name, param_id, today", LANE_CONSTANTS)
def test_changing_the_row_changes_the_lookup(monkeypatch, name, param_id, today):
    new = (not today) if isinstance(today, bool) else today + 7
    rows = tuple(
        dataclasses.replace(spec, defaults={**spec.defaults, INDOOR: new}) if spec.id == param_id else spec
        for spec in registry.TABLE
    )
    monkeypatch.setattr(registry, "TABLE", rows)
    assert lane_flight._registry_default(param_id) == new != getattr(lane_flight, name)


def test_unknown_row_is_an_error():
    with pytest.raises(KeyError):
        lane_flight._registry_default("no.such_row")


def test_executor_defaults_are_the_registry_knobs():
    params = inspect.signature(LaneExecutor.__init__).parameters
    turn = params["obstacle_turn"].default
    assert turn is True and turn == _row("sensing.turn").defaults[INDOOR]
    assert params["obstacle_retry_pause_s"].default is lane_flight.OBSTACLE_RETRY_PAUSE_S


def test_turn_default_follows_the_row(monkeypatch):
    rows = tuple(
        dataclasses.replace(spec, defaults={**spec.defaults, INDOOR: False}) if spec.id == "sensing.turn" else spec
        for spec in registry.TABLE
    )
    monkeypatch.setattr(registry, "TABLE", rows)
    assert lane_flight._registry_default("sensing.turn") is False


def test_level_tolerance_is_the_row_value_of_5_degrees():
    row = _row("sensing.level_pitch_tolerance_deg")
    # The indoor reference camera is fixed, so the row has no value there.
    assert row.defaults[INDOOR] is None
    assert default_level_tolerance_deg() == row.defaults[FIELD] == 5.0


# ---------------------------------------------------------------------------
# Part 2: the level gate
# ---------------------------------------------------------------------------


def _camera(mount_kind: str = "gimbal") -> CameraSpec:
    if mount_kind == "gimbal":
        return CameraSpec(id="cam", mount="gimbal", mount_kind="gimbal", orientation=None, pitch_min_deg=-90,
                          pitch_max_deg=0, pitch_measured=True, stream_w_px=1920, stream_h_px=1080,
                          hfov_deg=82, fps=30)
    return CameraSpec(id="cam", mount="front", mount_kind="fixed", orientation="forward", pitch_min_deg=None,
                      pitch_max_deg=None, pitch_measured=False, stream_w_px=960, stream_h_px=720,
                      hfov_deg=82, fps=30)


class PitchReader:
    """A fake gimbal pitch reader: ``script`` values first, one per read, then ``pitch``.

    Each read returns ``(pitch, stamp)``, stamped ``age_s`` seconds before the rig's clock reads (0: fresh).
    """

    def __init__(self, pitch, script=(), age_s=0.0):
        self.pitch = pitch
        self.script = list(script)
        self.age_s = age_s
        self.clock = None
        self.reads: list = []

    def __call__(self, camera_id):
        self.reads.append(camera_id)
        pitch = self.script.pop(0) if self.script else self.pitch
        return pitch, self.clock() - self.age_s


def level_events(rig):
    return [e for e in events(rig, EVENT_SENSE_STALE) if e.get("cause") == CAMERA_NOT_LEVEL_REASON]


def gimbal_rig(rigs, camera=None, reader=None, **tolerance):
    """The obstacle rig with *camera* (a gimbal by default) named for the level gate."""
    rig = rigs()
    if isinstance(reader, PitchReader):
        reader.clock = rig.clock
    rig.executor.set_obstacle_camera(_camera() if camera is None else camera, reader, **tolerance)
    return rig


@pytest.mark.parametrize("pitch", [0.0, 4.0, -4.0, 5.0])
def test_a_gimbal_within_the_tolerance_is_gated_as_before(rigs, pitch):
    reader = PitchReader(pitch)
    rig = gimbal_rig(rigs, reader=reader)
    fly(rig, lanes_of(("forward", 100),))
    assert rig.executor.obstacle_cuts == []
    assert [(e[1], e[2]) for e in lane_moves(rig)] == [("forward", 50)] * 2
    assert rig.sensor.states() == ["clear", "clear"]
    assert level_events(rig) == []
    # Read before and after each look, for the camera the executor was given.
    assert reader.reads == ["cam"] * 4
    assert_invariants(rig)


@pytest.mark.parametrize("pitch", [6.0, -6.0, None, float("nan")])
def test_a_gimbal_beyond_the_tolerance_or_without_a_pitch_cuts_the_lane(rigs, pitch):
    rig = gimbal_rig(rigs, reader=PitchReader(pitch))
    fly(rig, lanes_of(("forward", 100),))
    # The sensor is never asked to judge a view the camera did not look level for.
    assert rig.sensor.calls == [] and lane_moves(rig) == []
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    gates = events(rig, EVENT_OBSTACLE_GATE)
    assert [(g["state"], g["reason"]) for g in gates] == [("unavailable", "stale")] * OBSTACLE_SENSE_RETRIES
    stale = level_events(rig)
    assert len(stale) == OBSTACLE_SENSE_RETRIES
    expected = pitch if pitch is not None and math.isfinite(pitch) else None
    assert all(e["pitch_deg"] == expected and e["tolerance_deg"] == 5.0 for e in stale)
    assert ("land",) not in rig.timeline
    assert_invariants(rig)


def test_a_gimbal_with_no_pitch_reader_is_not_level(rigs):
    rig = gimbal_rig(rigs)
    fly(rig, lanes_of(("forward", 50),))
    assert rig.sensor.calls == [] and lane_moves(rig) == []
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert len(level_events(rig)) == OBSTACLE_SENSE_RETRIES


def test_a_reader_that_raises_is_a_missing_pitch(rigs):
    def broken(camera_id):
        raise RuntimeError("gimbal offline")

    rig = gimbal_rig(rigs, reader=broken)
    fly(rig, lanes_of(("forward", 50),))
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert all(e["pitch_deg"] is None for e in level_events(rig))


def test_a_fresh_measured_level_pitch_passes(rigs):
    rig = gimbal_rig(rigs, reader=PitchReader(0.0))
    fly(rig, lanes_of(("forward", 50),))
    assert rig.executor.obstacle_cuts == [] and level_events(rig) == []
    assert rig.sensor.states() == ["clear"] and len(lane_moves(rig)) == 1


def test_a_stale_pitch_is_a_missing_pitch(rigs):
    # A level 0 read a minute before the look may hide a gimbal that has tipped since.
    rig = gimbal_rig(rigs, reader=PitchReader(0.0, age_s=60.0))
    fly(rig, lanes_of(("forward", 50),))
    assert rig.sensor.calls == [] and lane_moves(rig) == []
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert len(level_events(rig)) == OBSTACLE_SENSE_RETRIES
    assert all(e["pitch_deg"] is None for e in level_events(rig))


def test_a_pitch_that_is_not_measured_is_a_missing_pitch(rigs):
    # A gimbal that does not measure its pitch never looks level, whatever its reader says.
    reader = PitchReader(0.0)
    rig = gimbal_rig(rigs, dataclasses.replace(_camera(), pitch_measured=False), reader)
    fly(rig, lanes_of(("forward", 50),))
    assert rig.sensor.calls == [] and lane_moves(rig) == []
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert len(level_events(rig)) == OBSTACLE_SENSE_RETRIES and reader.reads == []


def test_a_pitch_without_its_time_is_a_missing_pitch(rigs):
    rig = gimbal_rig(rigs, reader=lambda camera_id: 0.0)
    fly(rig, lanes_of(("forward", 50),))
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert all(e["pitch_deg"] is None for e in level_events(rig))


def test_a_clear_view_taken_while_the_gimbal_tipped_does_not_count(rigs):
    # Reads: before the first look 0, after it 6 (tipped); then level for the second attempt.
    rig = gimbal_rig(rigs, reader=PitchReader(0.0, script=[0.0, 6.0]))
    fly(rig, lanes_of(("forward", 50),))
    assert rig.sensor.states() == ["clear", "clear"]
    assert [g["state"] for g in events(rig, EVENT_OBSTACLE_GATE)] == ["unavailable", "clear"]
    assert level_events(rig)[0]["pitch_deg"] == 6.0
    assert rig.executor.obstacle_cuts == []
    assert len(lane_moves(rig)) == 1
    assert_invariants(rig)


@pytest.mark.parametrize("pitch", [0.0, 4.0, 6.0, None])
def test_a_fixed_mount_is_unaffected(rigs, pitch):
    reader = PitchReader(pitch)
    rig = gimbal_rig(rigs, _camera("fixed"), reader)
    fly(rig, lanes_of(("forward", 100),))
    assert rig.executor.obstacle_cuts == []
    assert rig.sensor.states() == ["clear", "clear"]
    assert len(lane_moves(rig)) == 2
    assert level_events(rig) == [] and reader.reads == []
    assert_invariants(rig)


def test_without_a_camera_the_gate_is_as_before(rigs):
    rig = rigs()
    fly(rig, lanes_of(("forward", 100),))
    assert rig.executor.obstacle_cuts == []
    assert rig.sensor.states() == ["clear", "clear"]
    assert level_events(rig) == []


def test_a_tighter_tolerance_is_honoured(rigs):
    rig = gimbal_rig(rigs, reader=PitchReader(4.0), level_tolerance_deg=3.0)
    fly(rig, lanes_of(("forward", 50),))
    assert rig.executor.obstacle_cuts == [Cut(0, 0, CUT_SENSOR_STALE)]
    assert all(e["tolerance_deg"] == 3.0 for e in level_events(rig))


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True])
def test_a_bad_tolerance_is_refused(rigs, bad):
    rig = rigs()
    with pytest.raises(ValueError):
        rig.executor.set_obstacle_camera(_camera(), PitchReader(0.0), level_tolerance_deg=bad)


def test_removing_the_camera_turns_the_gate_off(rigs):
    rig = gimbal_rig(rigs, reader=PitchReader(30.0))
    rig.executor.set_obstacle_camera(None)
    fly(rig, lanes_of(("forward", 50),))
    assert rig.executor.obstacle_cuts == [] and level_events(rig) == []
