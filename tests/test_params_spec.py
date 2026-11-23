"""Registry types: construction, immutability and import isolation."""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from flightrisk.params.spec import (
    Applies, Bound, Confirm, Constraint, Edit, EffectiveValue, EnvAlias, Kind,
    ParamSpec, Safer, Scope, Source, Unit,
)


def _spec(**over):
    base = dict(
        id="envelope.ceiling_margin_cm", unit=Unit.CM, kind=Kind.INT,
        description="Margin below the ceiling.", group="envelope", scope=Scope.DRONE,
        defaults={"tello": 30, "generic_multirotor": 100},
        bounds={"tello": Bound(30, 500), "generic_multirotor": None},
        safer=Safer.HIGHER, confirm=Confirm.LESS_SAFE,
        edit={"tello": Edit.READ_ONLY, "generic_multirotor": Edit.FILE_ENV},
        applies=Applies.IN_FLIGHT,
        env="FLIGHTRISK_CEILING_MARGIN_CM",
        env_aliases=(EnvAlias("FLIGHTRISK_CEILING_MARGIN_M", 100.0, "field"),),
        constraints=(Constraint("K-1", "a < b", "k1"),),
    )
    base.update(over)
    return ParamSpec(**base)


def test_param_spec_holds_per_profile_values():
    spec = _spec()
    assert spec.defaults["tello"] == 30
    assert spec.bounds["tello"] == Bound(30, 500)
    assert spec.edit["generic_multirotor"] is Edit.FILE_ENV
    assert spec.applies is Applies.IN_FLIGHT


def test_param_spec_is_immutable():
    spec = _spec()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.id = "other"
    with pytest.raises(TypeError):
        spec.defaults["tello"] = 1


def test_other_types_are_immutable():
    for obj, field in (
        (Bound(1, 2), "lo"), (EnvAlias("X"), "name"), (Constraint("K", "c", "m"), "id"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, 0)


def test_effective_value_construction_and_immutability():
    value = EffectiveValue(
        id="envelope.ceiling_cm", value=300, unit=Unit.CM, source=Source.ENV,
        source_detail="FLIGHTRISK_CEILING_CM", default=250, base_default=250, lo=50, hi=500,
        model="tello", applies=Applies.NEXT_START, non_default=True, less_safe=True,
    )
    assert value.source is Source.ENV
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.value = 1


def test_enum_values():
    assert {s.value for s in Source} == {
        "profile", "file", "inventory", "launcher", "env", "cli", "ground", "plan"}
    assert Edit.FACT.value == "fact"
    assert Applies.IN_FLIGHT.value == "in_flight"


def test_fact_rows_are_reported_per_profile():
    spec = _spec(edit={"tello": Edit.FACT, "generic_multirotor": Edit.FILE_ENV})
    assert spec.is_fact("tello")
    assert not spec.is_fact("generic_multirotor")


def test_packages_import_without_heavy_libraries():
    code = (
        "import sys, flightrisk.params, flightrisk.hardware, "
        "flightrisk.params.spec, flightrisk.params.messages\n"
        "bad = [m for m in ('flask','ultralytics','torch','cv2','mavsdk','djitellopy') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
