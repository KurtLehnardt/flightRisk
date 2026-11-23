"""Cross-value constraints of ``parameters.md`` section 1.1 and the stored safety-switch rule.

The resolver runs ``A1_CONSTRAINTS`` against the resolved values of one unit
and refuses the first constraint that fails, with its operator message
(RES-5). A constraint is skipped when one of its rows does not apply to the
model or holds a value that is not a number (an off value, a placeholder or
"unset"), so the airspace bands are checked only for the generic multirotor
and K-9 is skipped when the temperature refusal is off. Every other
constraint of K-1 to K-18 is listed in ``DEFERRED`` with the PR or check that
owns it, so none is skipped silently (PAR-6, plan risk R10).

Decisions:

1. K-3 (``battery.min_takeoff_pct`` above ``battery.return_floor_pct``) is
   enforced for the Tello profile only in A1, as ``parameters.md`` says;
   the generic multirotor waits for C6 (``DEFERRED``). Today's
   ``FLIGHTRISK_MIN_TAKEOFF_BATTERY`` accepts 15 to 25 % on a Tello, under
   its 25 % floor; those inputs are pinned as refused by ``resolve_unit``,
   as K-6 and K-8 are.
2. A constraint over test-adapter facts (K-9, K-10) never names a fact or its
   environment variable in its operator text (design 7.5). Its refusal is
   ``FACT_CONFLICT_REFUSED``, and the values go to the log for developers.
3. K-16 is a Plan check. It is defined as ``K_16`` so the defaults test can
   check it (PAR-6); the resolver does not run it.
4. K-8 checks every slot of ``plan.alt_slots_cm`` and the default slot
   ``envelope.alt_slot_cm`` against ``envelope.ceiling_cm`` minus
   ``envelope.ceiling_margin_cm``; every slot at or below that height implies
   that the lowest slot is too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from flightrisk.fleet_protocol.constants import HEARTBEAT_INTERVAL_S
from flightrisk.params.spec import Constraint, EffectiveValue, Source, Unit

log = logging.getLogger(__name__)

#: Operator text for a constraint over test-adapter facts; it names neither the values nor the variables.
FACT_CONFLICT_REFUSED = (
    "Two test-adapter developer overrides do not fit together. Refusing to start."
)
#: Operator text for a safety switch stored as off in a file (RES-10, detail 10.2).
SWITCH_STORED_OFF = (
    "`{param_id}` {value} from {detail} is refused: a safety feature cannot be stored as off in a file. "
    "Switch it off for one session in the launcher, where Start confirms it."
)
#: Operator text for a safety feature stored as off in a unit's inventory features.
FEATURE_STORED_OFF = (
    "`{feature}` off from {detail} is refused: a safety feature cannot be stored as off in a file. "
    "Switch it off for one session in the launcher, where Start confirms it."
)
#: Unit features that are safety switches; a file may never store them as off (RES-10).
SAFETY_FEATURES: tuple[str, ...] = ("forward_obstacle_sensing",)
#: Layers that are files; a safety switch is never stored as off in one.
FILE_SOURCES: tuple[Source, ...] = (Source.FILE, Source.INVENTORY)
#: K-6: the link-loss detection time is at least this many heartbeat intervals.
LOSS_DETECT_HEARTBEATS = 1.5
#: K-11: the re-task window is at least this many seconds.
MIN_RETASK_WINDOW_S = 1

_UNIT_TEXT = {Unit.PCT: "%", Unit.CM: "cm", Unit.S: "s", Unit.DEG_C: "°C", Unit.CM_LIST: "cm"}


@dataclass(frozen=True)
class ConstraintCheck:
    """A constraint, the rows it reads, its check and its operator message.

    ``holds`` takes ``{id: value}`` for the rows in ``ids`` and those of
    ``optional`` that apply. ``message`` takes
    the effective values and the model's name and returns the operator text;
    it is ``None`` when the constraint reads a test-adapter fact.
    """

    constraint: Constraint
    ids: tuple[str, ...]
    holds: Callable[[Mapping[str, Any]], bool]
    message: Optional[Callable[[Mapping[str, EffectiveValue], str], str]]
    #: Rows read when they apply to the model and skipped otherwise.
    optional: tuple[str, ...] = ()
    #: Profiles the check applies to; ``None`` applies it to every profile.
    profiles: Optional[frozenset[str]] = None

    @property
    def id(self) -> str:
        return self.constraint.id


@dataclass(frozen=True)
class Violation:
    """A failed constraint and the operator text that refuses it."""

    constraint_id: str
    message: str


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _term(ev: EffectiveValue) -> str:
    """One value in plain words: its id, number, unit and where it came from."""
    where = "default" if ev.source is Source.PROFILE else f"from {ev.source_detail or ev.source.value}"
    return f"`{ev.id}` {_num(ev.value)} {_UNIT_TEXT.get(ev.unit, ev.unit.value)} ({where})"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usable(value: Any) -> bool:
    if isinstance(value, tuple):
        return bool(value) and all(_is_number(v) for v in value)
    return _is_number(value)


def _slots(v: Mapping[str, Any]) -> list:
    return list(v["plan.alt_slots_cm"]) + [v["envelope.alt_slot_cm"]]


def _k8_message(ev: Mapping[str, EffectiveValue], model: str) -> str:
    room = ev["envelope.ceiling_cm"].value - ev["envelope.ceiling_margin_cm"].value
    highest = max(_slots({k: e.value for k, e in ev.items()}))
    return (
        f"{_term(ev['envelope.ceiling_cm'])} minus {_term(ev['envelope.ceiling_margin_cm'])} leaves "
        f"{_num(room)} cm, below the {_num(highest)} cm altitude slot on the {model}. Refusing to start."
    )


def _k12_holds(v: Mapping[str, Any]) -> bool:
    top = v["envelope.ceiling_cm"] - v["envelope.ceiling_margin_cm"]
    return (
        v["airspace.close_band_floor_cm"] < v["airspace.close_band_ceiling_cm"]
        and v["airspace.sweep_band_floor_cm"] >= v["airspace.close_band_ceiling_cm"] + v["airspace.band_gap_cm"]
        and v["airspace.sweep_band_floor_cm"] <= v["airspace.sweep_band_ceiling_cm"] <= top
    )


def _k12_message(ev: Mapping[str, EffectiveValue], model: str) -> str:
    return (
        f"The airspace bands do not fit on the {model}: {_term(ev['airspace.close_band_floor_cm'])} must be below "
        f"{_term(ev['airspace.close_band_ceiling_cm'])}; {_term(ev['airspace.sweep_band_floor_cm'])} must be at "
        f"least the close ceiling plus {_term(ev['airspace.band_gap_cm'])} and at most "
        f"{_term(ev['airspace.sweep_band_ceiling_cm'])}, which must be at most {_term(ev['envelope.ceiling_cm'])} "
        f"minus {_term(ev['envelope.ceiling_margin_cm'])}. Refusing to start."
    )


def _k10_holds(v: Mapping[str, Any]) -> bool:
    lo, hi = v["move.min_cm"], v["move.max_cm"]
    sidestep = v["avoid.sidestep_cm"]
    return lo <= v["sensing.step_cm"] <= hi and (sidestep == 0 or lo <= sidestep <= hi)


K_1 = ConstraintCheck(
    Constraint("K-1", "battery.critical_pct < battery.return_floor_pct", "K-1"),
    ("battery.critical_pct", "battery.return_floor_pct"),
    lambda v: v["battery.critical_pct"] < v["battery.return_floor_pct"],
    lambda ev, model: (
        f"{_term(ev['battery.critical_pct'])} must be below {_term(ev['battery.return_floor_pct'])} "
        f"on the {model}. Refusing to start."
    ),
)
K_2 = ConstraintCheck(
    Constraint("K-2", "battery.return_floor_pct >= battery.critical_pct + battery.landing_reserve_pct", "K-2"),
    ("battery.return_floor_pct", "battery.critical_pct", "battery.landing_reserve_pct"),
    lambda v: v["battery.return_floor_pct"] >= v["battery.critical_pct"] + v["battery.landing_reserve_pct"],
    lambda ev, model: (
        f"{_term(ev['battery.return_floor_pct'])} must be at least {_term(ev['battery.critical_pct'])} plus "
        f"{_term(ev['battery.landing_reserve_pct'])} on the {model}. Refusing to start."
    ),
)
#: Defined for C6 and not enforced in A1 (decision 1).
K_3 = ConstraintCheck(
    Constraint("K-3", "battery.min_takeoff_pct > battery.return_floor_pct", "K-3"),
    ("battery.min_takeoff_pct", "battery.return_floor_pct"),
    lambda v: v["battery.min_takeoff_pct"] > v["battery.return_floor_pct"],
    lambda ev, model: (
        f"{_term(ev['battery.min_takeoff_pct'])} must be above {_term(ev['battery.return_floor_pct'])} "
        f"on the {model}. Refusing to start."
    ),
    profiles=frozenset({"tello"}),
)
K_6 = ConstraintCheck(
    Constraint("K-6", "link.loss_detect_s >= 1.5 * HEARTBEAT_INTERVAL_S", "K-6"),
    ("link.loss_detect_s",),
    lambda v: v["link.loss_detect_s"] >= LOSS_DETECT_HEARTBEATS * HEARTBEAT_INTERVAL_S,
    lambda ev, model: (
        f"{_term(ev['link.loss_detect_s'])} must be at least {_num(LOSS_DETECT_HEARTBEATS)} times the "
        f"{_num(HEARTBEAT_INTERVAL_S)} s heartbeat interval on the {model}. Refusing to start."
    ),
)
K_8 = ConstraintCheck(
    Constraint("K-8", "every slot <= envelope.ceiling_cm - envelope.ceiling_margin_cm", "K-8"),
    ("envelope.ceiling_cm", "envelope.ceiling_margin_cm", "plan.alt_slots_cm", "envelope.alt_slot_cm"),
    lambda v: max(_slots(v)) <= v["envelope.ceiling_cm"] - v["envelope.ceiling_margin_cm"],
    _k8_message,
)
K_9 = ConstraintCheck(
    Constraint("K-9", "tello.temp_warn_c <= tello.temp_refuse_c unless the refusal is off", "K-9"),
    ("tello.temp_warn_c", "tello.temp_refuse_c"),
    lambda v: v["tello.temp_warn_c"] <= v["tello.temp_refuse_c"],
    None,
)
K_10 = ConstraintCheck(
    Constraint("K-10", "sensing.step_cm within the move bound; avoid.sidestep_cm 0 or within it", "K-10"),
    ("sensing.step_cm", "move.min_cm", "move.max_cm", "avoid.sidestep_cm"),
    _k10_holds,
    None,
)
K_11 = ConstraintCheck(
    Constraint("K-11", "retask.window_s >= 1", "K-11"),
    ("retask.window_s",),
    lambda v: v["retask.window_s"] >= MIN_RETASK_WINDOW_S,
    lambda ev, model: (
        f"{_term(ev['retask.window_s'])} must be at least {MIN_RETASK_WINDOW_S} s on the {model}. Refusing to start."
    ),
)
K_12 = ConstraintCheck(
    Constraint("K-12", "close floor < close ceiling; close ceiling + gap <= sweep floor <= sweep ceiling "
                       "<= ceiling - margin", "K-12"),
    ("airspace.close_band_floor_cm", "airspace.close_band_ceiling_cm", "airspace.sweep_band_floor_cm",
     "airspace.band_gap_cm", "airspace.sweep_band_ceiling_cm", "envelope.ceiling_cm", "envelope.ceiling_margin_cm"),
    _k12_holds,
    _k12_message,
)
#: A Plan check, held on the defaults (decision 3).
K_16 = ConstraintCheck(
    Constraint("K-16", "escort.standoff_cm >= find.approach_distance_cm >= separation.base_cm", "K-16"),
    ("find.approach_distance_cm", "separation.base_cm"),
    lambda v: (v.get("escort.standoff_cm", v["find.approach_distance_cm"])
               >= v["find.approach_distance_cm"] >= v["separation.base_cm"]),
    lambda ev, model: (
        f"{_term(ev['find.approach_distance_cm'])} must lie between {_term(ev['separation.base_cm'])} and the "
        f"escort standoff on the {model}."
    ),
    optional=("escort.standoff_cm",),
)

#: The constraints the resolver enforces in A1 (RES-5).
A1_CONSTRAINTS: tuple[ConstraintCheck, ...] = (K_1, K_2, K_3, K_6, K_8, K_9, K_10, K_11, K_12)

#: Every constraint A1 does not enforce, with where it is checked and the PR that brings the check.
DEFERRED: Mapping[str, tuple[str, str]] = {
    # K-3 is enforced for the Tello in A1; only the generic multirotor waits for C6.
    "K-3": ("resolver, generic_multirotor profile", "C6"),
    "K-4": ("preflight", "B4"),
    "K-5": ("edge energy model", "C1"),
    "K-7": ("ground", "D2"),
    "K-13": ("Plan", "D1"),
    "K-14": ("Plan", "D1"),
    "K-15": ("Start", "D1"),
    "K-16": ("Plan", "D1"),
    "K-17": ("edge, ground", "E4"),
    "K-18": ("A2 loader; custom.py refuses it in A1", "A2"),
}


def check_constraints(values: Mapping[str, EffectiveValue], model_name: str,
                      checks: tuple[ConstraintCheck, ...] = A1_CONSTRAINTS,
                      profile: Optional[str] = None) -> Optional[Violation]:
    """Return the first constraint the resolved ``values`` break, or None when all hold.

    With ``profile``, a check limited to other profiles is skipped.
    """
    for check in checks:
        if profile is not None and check.profiles is not None and profile not in check.profiles:
            continue
        if not all(i in values and _usable(values[i].value) for i in check.ids):
            continue
        plain = {i: values[i].value for i in check.ids}
        for extra in check.optional:
            if extra in values and _usable(values[extra].value):
                plain[extra] = values[extra].value
        if check.holds(plain):
            continue
        if check.message is None:
            log.warning("constraint %s fails on %s: %s", check.id, model_name,
                        ", ".join(f"{i}={plain[i]!r}" for i in check.ids))
            return Violation(check.id, FACT_CONFLICT_REFUSED)
        return Violation(check.id, check.message(values, model_name))
    return None


def stored_switch_off(param_id: str, value: Any, detail: str) -> str:
    """Operator text refusing a safety switch stored as off in a file (never used for a fact)."""
    text = "false" if value is False else _num(value)
    return SWITCH_STORED_OFF.format(param_id=param_id, value=text, detail=detail)


def stored_feature_off(features: Mapping[str, Any], detail: str) -> Optional[str]:
    """Operator text refusing a unit's features that store a safety feature as off, or None."""
    for feature in SAFETY_FEATURES:
        if features.get(feature) is False:
            return FEATURE_STORED_OFF.format(feature=feature, detail=detail)
    return None
