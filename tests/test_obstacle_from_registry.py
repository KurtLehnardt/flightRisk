"""The obstacle sensing and avoidance knobs are read from the parameter registry (A3.4 step S13, PAR-8).

Each public knob of ``flightrisk/drone/obstacle_sensor.py`` that duplicates a ``sensing.*`` or ``avoid.*``
row must equal the registry value of its profile column, with the same type, and a changed registry value
must change the lookup, so the module derives its knobs instead of keeping a copy that only happens to
match. ``ObstacleGuard`` takes its default threshold from the same knob. The changed values go into a
patched copy of the table; the real table is never edited.
"""

from __future__ import annotations

import dataclasses
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from flightrisk.drone import obstacle_sensor
from flightrisk.params import table as registry
from flightrisk.params.spec import Bound

ROOT = Path(__file__).resolve().parents[1]
INDOOR = registry.T
FIELD = registry.G

_BY_ID = {spec.id: spec for spec in registry.TABLE}


def _default(param_id: str, profile: str):
    return _BY_ID[param_id].defaults[profile]


def _bound(param_id: str, profile: str) -> Bound:
    return _BY_ID[param_id].bounds[profile]


# (public constant, registry row, profile column, column part)
MODULE_CONSTANTS = [
    ("OBSTACLE_STEP_CM", "sensing.step_cm", INDOOR, "default"),
    ("OBSTACLE_STEP_MIN_CM", "sensing.step_cm", INDOOR, "lo"),
    ("OBSTACLE_STEP_MAX_CM", "sensing.step_cm", INDOOR, "hi"),
    ("OBSTACLE_STEP_FIELD_CM", "sensing.step_cm", FIELD, "default"),
    ("OBSTACLE_SETTLE_S", "sensing.settle_s", INDOOR, "default"),
    ("OBSTACLE_FRAME_WAIT_S", "sensing.view_wait_s", INDOOR, "default"),
    ("OBSTACLE_SENSE_TIMEOUT_S", "sensing.sense_timeout_s", INDOOR, "default"),
    ("OBSTACLE_SENSE_RETRIES", "sensing.sense_retries", INDOOR, "default"),
    ("OBSTACLE_SENSOR_DOWN_S", "sensing.sensor_down_s", INDOOR, "default"),
    ("OBSTACLE_PERSON_FRACTION", "sensing.person_fraction", INDOOR, "default"),
    ("OBSTACLE_MIN_SAFE_DEPTH", "sensing.min_safe_depth", INDOOR, "default"),
    ("OBSTACLE_DETECTION_WAIT_S", "sensing.person_wait_s", INDOOR, "default"),
    ("MAX_DECODE_LAG_S", "sensing.max_decode_lag_s", INDOOR, "default"),
    ("OBSTACLE_CLEAR_VOTES", "avoid.clear_votes", INDOOR, "default"),
    ("OBSTACLE_RELOOK", "avoid.relook", INDOOR, "default"),
    ("OBSTACLE_SIDESTEP_CM", "avoid.sidestep_cm", INDOOR, "default"),
    ("OBSTACLE_SIDESTEPS", "avoid.sidesteps", INDOOR, "default"),
    ("OBSTACLE_SIDESTEP_PASS_CM", "avoid.sidestep_pass_cm", INDOOR, "default"),
]

#: Today's values at the A3 base (a2c0c89), with their types. The derivation must not change any of them.
TODAY = {
    "OBSTACLE_STEP_CM": 50,
    "OBSTACLE_STEP_MIN_CM": 20,
    "OBSTACLE_STEP_MAX_CM": 100,
    "OBSTACLE_STEP_FIELD_CM": 500,
    "OBSTACLE_SETTLE_S": 0.3,
    "OBSTACLE_FRAME_WAIT_S": 0.7,
    "OBSTACLE_SENSE_TIMEOUT_S": 2.0,
    "OBSTACLE_SENSE_RETRIES": 3,
    "OBSTACLE_SENSOR_DOWN_S": 10.0,
    "OBSTACLE_PERSON_FRACTION": 0.55,
    "OBSTACLE_MIN_SAFE_DEPTH": 0.35,
    "OBSTACLE_DETECTION_WAIT_S": 1.0,
    "MAX_DECODE_LAG_S": 0.3,
    "OBSTACLE_CLEAR_VOTES": 2,
    "OBSTACLE_RELOOK": 2,
    "OBSTACLE_SIDESTEP_CM": 0,
    "OBSTACLE_SIDESTEPS": 2,
    "OBSTACLE_SIDESTEP_PASS_CM": 150,
}


def _registry_value(param_id: str, profile: str, part: str):
    if part == "default":
        return _default(param_id, profile)
    return getattr(_bound(param_id, profile), part)


def _same(actual, expected) -> bool:
    return actual == expected and type(actual) is type(expected)


@pytest.mark.parametrize("name, param_id, profile, part", MODULE_CONSTANTS)
def test_module_constant_equals_its_registry_value(name, param_id, profile, part):
    assert _same(getattr(obstacle_sensor, name), _registry_value(param_id, profile, part))


@pytest.mark.parametrize("name", sorted(TODAY))
def test_module_constant_keeps_todays_value_and_type(name):
    assert _same(getattr(obstacle_sensor, name), TODAY[name])


def test_every_derived_constant_is_checked_against_today():
    assert {name for name, *_ in MODULE_CONSTANTS} == set(TODAY)


def test_sidestep_range_follows_the_step_bound():
    step = _bound("sensing.step_cm", INDOOR)
    assert obstacle_sensor.OBSTACLE_SIDESTEP_RANGE_CM == (step.lo, step.hi)


def test_constants_with_no_row_keep_their_literals():
    assert _same(obstacle_sensor.OBSTACLE_AVOID_BUDGET_S, 45.0)
    assert _same(obstacle_sensor.OBSTACLE_RELOOKS_PER_LANE, 3)
    assert _same(obstacle_sensor.BlockedZone().heading_tol_deg, 1.0)


def test_obstacle_guard_default_threshold_is_the_registry_knob():
    pytest.importorskip("cv2")
    pytest.importorskip("torch")
    from flightrisk.drone.obstacle import ObstacleGuard

    default = inspect.signature(ObstacleGuard.__init__).parameters["min_safe_depth"].default
    assert default is obstacle_sensor.OBSTACLE_MIN_SAFE_DEPTH
    assert _same(default, _default("sensing.min_safe_depth", INDOOR))


def _patched_table(monkeypatch, param_id: str, profile: str, *, default=None, bound=None):
    """Replace ``registry.TABLE`` with a copy whose ``param_id`` row has a new default or bound."""
    real = registry.TABLE
    rows = []
    for spec in real:
        if spec.id == param_id:
            if default is not None:
                spec = dataclasses.replace(spec, defaults={**spec.defaults, profile: default})
            if bound is not None:
                spec = dataclasses.replace(spec, bounds={**spec.bounds, profile: bound})
        rows.append(spec)
    monkeypatch.setattr(registry, "TABLE", tuple(rows))
    return real


def test_patched_table_leaves_the_real_table_alone(monkeypatch):
    real = _patched_table(monkeypatch, "sensing.step_cm", INDOOR, default=77)
    assert registry.TABLE is not real
    assert {spec.id: spec for spec in real}["sensing.step_cm"].defaults[INDOOR] == 50


@pytest.mark.parametrize("name, param_id, profile, part", MODULE_CONSTANTS)
def test_changing_a_registry_value_changes_the_constant_lookup(monkeypatch, name, param_id, profile, part):
    if part == "default":
        new = getattr(obstacle_sensor, name) + 7
        _patched_table(monkeypatch, param_id, profile, default=new)
        assert obstacle_sensor._registry_default(param_id, profile) == new != getattr(obstacle_sensor, name)
        if profile == INDOOR:
            assert obstacle_sensor._registry_default(param_id) == new
    else:
        old = _bound(param_id, profile)
        changed = Bound(old.lo - 1, old.hi) if part == "lo" else Bound(old.lo, old.hi + 1)
        _patched_table(monkeypatch, param_id, profile, bound=changed)
        looked_up = getattr(obstacle_sensor._registry_bound(param_id, profile), part)
        assert looked_up == getattr(changed, part) != getattr(obstacle_sensor, name)


def test_unknown_row_and_missing_bound_are_errors():
    with pytest.raises(KeyError):
        obstacle_sensor._registry_row("no.such_row")
    with pytest.raises(LookupError):
        obstacle_sensor._registry_bound("sensing.max_decode_lag_s", FIELD)


def test_params_package_imports_no_drone_module():
    """The registry stays below the drone layer: importing it in a fresh process loads no ``flightrisk.drone``."""
    code = (
        "import sys; import flightrisk.params.table, flightrisk.params.spec; "
        "print(sorted(m for m in sys.modules if m.startswith('flightrisk.drone')))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
