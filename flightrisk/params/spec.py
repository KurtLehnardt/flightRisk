"""Types for one flight parameter row, its constraints and its effective value.

All types are frozen. Mappings given to a ``ParamSpec`` are copied into
read-only views, so a registry row cannot be changed after it is built.
Distances are centimetres on every model, as in ``flightrisk/drone/safety.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional


class Unit(str, Enum):
    CM = "cm"
    S = "s"
    PCT = "pct"
    DEG_C = "degC"
    DEG = "deg"
    DEG_PER_S = "deg_per_s"
    CM_PER_S = "cm_per_s"
    COUNT = "count"
    RATIO = "ratio"
    ENUM = "enum"
    BOOL = "bool"
    CM_LIST = "cm_list"
    PORT = "port"
    PX_PAIR = "px_pair"
    DEG_C_PAIR = "degC_pair"


class Kind(str, Enum):
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    ENUM = "enum"
    INT_LIST = "int_list"
    INT_PAIR = "int_pair"


class Scope(str, Enum):
    DRONE = "drone"
    GROUND = "ground"
    MISSION = "mission"


class Safer(str, Enum):
    HIGHER = "higher"
    LOWER = "lower"
    NONE = "none"


class Confirm(str, Enum):
    LESS_SAFE = "less_safe"
    NON_DEFAULT = "non_default"
    NONE = "none"


class Edit(str, Enum):
    UI = "ui"
    FILE_ENV = "file_env"
    READ_ONLY = "read_only"
    #: A test-adapter fact: read-only and never shown as something to change.
    FACT = "fact"


class Applies(str, Enum):
    NEXT_START = "next_start"
    ON_GROUND = "on_ground"
    PLAN = "plan"
    #: A mission setting that may change during the flight.
    IN_FLIGHT = "in_flight"


class Source(str, Enum):
    PROFILE = "profile"
    FILE = "file"
    INVENTORY = "inventory"
    LAUNCHER = "launcher"
    ENV = "env"
    CLI = "cli"
    GROUND = "ground"
    PLAN = "plan"


@dataclass(frozen=True)
class Bound:
    """An inclusive hard bound. ``lo == hi`` makes a value read-only for a model."""

    lo: float
    hi: float


@dataclass(frozen=True)
class EnvAlias:
    """An environment name that also sets a value, with its scale factor.

    ``applies_to_scale`` names the environment scale (for example ``room``)
    the alias is read for, so a room alias is never read for a field model.
    """

    name: str
    scale_factor: float = 1.0
    applies_to_scale: Optional[str] = None


@dataclass(frozen=True)
class Constraint:
    """A check across several values, with the key of its operator message."""

    id: str
    check: str
    message_key: str


def _frozen_map(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class ParamSpec:
    """One registry row.

    ``defaults``, ``bounds`` and ``edit`` map a profile name (``tello`` or
    ``generic_multirotor``) to a value. A ``None`` default or bound means the
    row does not apply to that model. ``edit`` is per profile because some
    rows are read-only for the Tello and file or environment values for the
    generic multirotor.
    """

    id: str
    unit: Unit
    kind: Kind
    description: str
    group: str
    scope: Scope
    defaults: Mapping[str, Any]
    bounds: Mapping[str, Optional[Bound]]
    safer: Safer
    confirm: Confirm
    edit: Mapping[str, Edit]
    applies: Applies
    off_value: Any = None
    safety_switch: bool = False
    choices: tuple[str, ...] = ()
    env: Optional[str] = None
    env_aliases: tuple[EnvAlias, ...] = ()
    cli: tuple[str, ...] = ()
    constraints: tuple[Constraint, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "defaults", _frozen_map(self.defaults))
        object.__setattr__(self, "bounds", _frozen_map(self.bounds))
        object.__setattr__(self, "edit", _frozen_map(self.edit))

    def is_fact(self, profile: str) -> bool:
        """True when the row is a test-adapter fact for ``profile``."""
        return self.edit.get(profile) is Edit.FACT


@dataclass(frozen=True)
class EffectiveValue:
    """A resolved value with where it came from and how it compares to the base."""

    id: str
    value: Any
    unit: Unit
    source: Source
    source_detail: Optional[str]
    default: Any
    base_default: Any
    lo: Optional[float]
    hi: Optional[float]
    model: str
    applies: Applies
    non_default: bool = False
    less_safe: bool = False
