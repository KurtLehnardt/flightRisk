"""The safety envelope limits are read from the parameter registry (A3.3 step S10, PAR-8).

Each public limit of ``flightrisk/drone/safety.py`` must equal the registry value of its profile
column, and a changed registry value must change the derived limit, so the module derives its limits
instead of keeping a copy that only happens to match. The changed values go into a patched copy of
the table; the real table is never edited.
"""

from __future__ import annotations

import dataclasses

import pytest

from flightrisk.drone import safety
from flightrisk.params import table as registry
from flightrisk.params.spec import Bound

ROOM = registry.T
FIELD = registry.G

_BY_ID = {spec.id: spec for spec in registry.TABLE}


def _default(param_id: str, profile: str):
    return _BY_ID[param_id].defaults[profile]


def _bound(param_id: str, profile: str) -> Bound:
    return _BY_ID[param_id].bounds[profile]


# (public constant, registry row, profile column, column part)
MODULE_CONSTANTS = [
    ("DEFAULT_LINK_LOSS_LAND_S", "link.loss_detect_s", ROOM, "default"),
    ("DEFAULT_MIN_TAKEOFF_BATTERY", "battery.min_takeoff_pct", ROOM, "default"),
    ("DEFAULT_ALT_SLOTS_CM", "plan.alt_slots_cm", ROOM, "default"),
    ("DEFAULT_LAND_BATTERY", "battery.critical_pct", ROOM, "default"),
    ("DEFAULT_GEOFENCE_CM", "envelope.geofence_cm", ROOM, "default"),
    ("DEFAULT_ALT_SLOT_CM", "envelope.alt_slot_cm", ROOM, "default"),
    ("DEFAULT_FIELD_LINK_LOSS_HOLD_S", "link.loss_wait_s", FIELD, "default"),
    ("MIN_MOVE_CM", "move.min_cm", ROOM, "default"),
    ("MAX_MOVE_CM", "move.max_cm", ROOM, "default"),
    ("MIN_ALT_SLOT_CM", "envelope.alt_slot_cm", ROOM, "lo"),
    ("DEFAULT_CEILING_CM", "envelope.ceiling_cm", ROOM, "default"),
    ("DEFAULT_CEILING_MARGIN_CM", "envelope.ceiling_margin_cm", ROOM, "default"),
    ("MIN_CEILING_CM", "envelope.ceiling_cm", ROOM, "lo"),
    ("MAX_CEILING_CM", "envelope.ceiling_cm", ROOM, "hi"),
    ("MIN_CEILING_MARGIN_CM", "envelope.ceiling_margin_cm", ROOM, "lo"),
]

# (EnvelopeLimits field, registry row, column part)
LIMIT_FIELDS = [
    ("min_move_cm", "move.min_cm", "default"),
    ("max_move_cm", "move.max_cm", "default"),
    ("min_alt_slot_cm", "envelope.alt_slot_cm", "lo"),
    ("max_alt_slot_cm", "envelope.alt_slot_cm", "hi"),
    ("min_ceiling_cm", "envelope.ceiling_cm", "lo"),
    ("max_ceiling_cm", "envelope.ceiling_cm", "hi"),
    ("min_ceiling_margin_cm", "envelope.ceiling_margin_cm", "lo"),
    ("default_geofence_cm", "envelope.geofence_cm", "default"),
    ("default_alt_slot_cm", "envelope.alt_slot_cm", "default"),
    ("default_ceiling_cm", "envelope.ceiling_cm", "default"),
    ("default_ceiling_margin_cm", "envelope.ceiling_margin_cm", "default"),
]


def _registry_value(param_id: str, profile: str, part: str):
    if part == "default":
        return _default(param_id, profile)
    return getattr(_bound(param_id, profile), part)


def _same(actual, expected) -> bool:
    return actual == expected and type(actual) is type(expected)


@pytest.mark.parametrize("name, param_id, profile, part", MODULE_CONSTANTS)
def test_module_constant_equals_its_registry_value(name, param_id, profile, part):
    assert _same(getattr(safety, name), _registry_value(param_id, profile, part))


def test_slot_and_detail_constants_follow_the_registry():
    assert safety.MAX_ALT_SLOT_CM == safety.MAX_MOVE_CM == _bound("envelope.alt_slot_cm", ROOM).hi
    assert f"at least {_bound('envelope.alt_slot_cm', ROOM).lo} cm" in safety.MIN_ALT_SLOT_DETAIL


@pytest.mark.parametrize("limits, profile", [(safety.ROOM_LIMITS, ROOM), (safety.FIELD_LIMITS, FIELD)])
@pytest.mark.parametrize("field, param_id, part", LIMIT_FIELDS)
def test_profile_limit_equals_its_registry_value(limits, profile, field, param_id, part):
    assert _same(getattr(limits, field), _registry_value(param_id, profile, part))


def test_geofence_upper_limits():
    assert safety.ROOM_LIMITS.max_geofence_cm == float("inf")
    assert _same(safety.FIELD_LIMITS.max_geofence_cm, _default("envelope.max_geofence_cm", FIELD))


def test_limits_for_returns_the_registry_profiles():
    assert safety.limits_for("room") is safety.ROOM_LIMITS
    assert safety.limits_for("field") is safety.FIELD_LIMITS
    assert safety.limits_for("unknown") is safety.ROOM_LIMITS
    assert safety.limits_for(None) is safety.ROOM_LIMITS


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
    real = _patched_table(monkeypatch, "envelope.geofence_cm", ROOM, default=777)
    assert registry.TABLE is not real
    assert {spec.id: spec for spec in real}["envelope.geofence_cm"].defaults[ROOM] == 500


@pytest.mark.parametrize("scale, profile", [("room", ROOM), ("field", FIELD)])
@pytest.mark.parametrize(
    "field, param_id, part, new",
    [
        ("default_geofence_cm", "envelope.geofence_cm", "default", 777),
        ("default_ceiling_cm", "envelope.ceiling_cm", "default", 1111),
        ("default_ceiling_margin_cm", "envelope.ceiling_margin_cm", "default", 333),
        ("default_alt_slot_cm", "envelope.alt_slot_cm", "default", 444),
        ("max_move_cm", "move.max_cm", "default", 4321),
        ("min_move_cm", "move.min_cm", "default", 25),
        ("min_alt_slot_cm", "envelope.alt_slot_cm", "lo", 66),
        ("max_alt_slot_cm", "envelope.alt_slot_cm", "hi", 9999),
        ("min_ceiling_cm", "envelope.ceiling_cm", "lo", 160),
        ("max_ceiling_cm", "envelope.ceiling_cm", "hi", 8888),
        ("min_ceiling_margin_cm", "envelope.ceiling_margin_cm", "lo", 35),
    ],
)
def test_changing_a_registry_value_changes_the_derived_limit(monkeypatch, scale, profile, field, param_id, part, new):
    before = getattr(safety._envelope_limits(scale), field)
    if part == "default":
        _patched_table(monkeypatch, param_id, profile, default=new)
    else:
        old = _bound(param_id, profile)
        changed = Bound(new, old.hi) if part == "lo" else Bound(old.lo, new)
        _patched_table(monkeypatch, param_id, profile, bound=changed)
    after = getattr(safety._envelope_limits(scale), field)
    assert after == new != before


def test_changing_the_field_geofence_cap_changes_the_field_limit(monkeypatch):
    _patched_table(monkeypatch, "envelope.max_geofence_cm", FIELD, default=40000)
    assert safety._envelope_limits("field").max_geofence_cm == 40000
    assert safety._envelope_limits("room", max_geofence_cm=float("inf")).max_geofence_cm == float("inf")


@pytest.mark.parametrize("name, param_id, profile, part", MODULE_CONSTANTS)
def test_changing_a_registry_value_changes_the_constant_lookup(monkeypatch, name, param_id, profile, part):
    scale = "room" if profile == ROOM else "field"
    if part == "default":
        new = (1, 2) if param_id == "plan.alt_slots_cm" else 7
        _patched_table(monkeypatch, param_id, profile, default=new)
        assert safety._registry_default(param_id, scale) == new != getattr(safety, name)
    else:
        old = _bound(param_id, profile)
        changed = Bound(old.lo - 1, old.hi) if part == "lo" else Bound(old.lo, old.hi + 1)
        _patched_table(monkeypatch, param_id, profile, bound=changed)
        looked_up = getattr(safety._registry_bound(param_id, scale), part)
        assert looked_up == getattr(changed, part) != getattr(safety, name)


def test_unknown_row_and_missing_bound_are_errors(monkeypatch):
    with pytest.raises(KeyError):
        safety._registry_row("no.such_row")
    rows = [
        dataclasses.replace(spec, bounds={**spec.bounds, FIELD: None}) if spec.id == "envelope.ceiling_cm" else spec
        for spec in registry.TABLE
    ]
    monkeypatch.setattr(registry, "TABLE", tuple(rows))
    with pytest.raises(LookupError):
        safety._envelope_limits("field")


def test_unknown_scale_is_an_error():
    with pytest.raises(ValueError):
        safety._registry_profile("orbit")
