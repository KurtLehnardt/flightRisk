"""Parity of the parameter resolver with today's runtime readers (plan step S10).

For every environment name and flag that today's runtime reads, each probe
(today's low and high ends, one past each end, the target bound's ends, a
typical value, a malformed value and an empty value) goes through TODAY's real
reader and through ``resolve`` with the same input. Both must accept with the
same value, or both must refuse. Where today's reader clamps, the resolver
must clamp to the same value (``policy: clamp``, risk R5).

``resolve`` is the layering step and refuses single values only. The
cross-value constraints of ``check_resolved`` and ``resolve_unit`` (K-1, K-2,
K-6, K-8 to K-12) are new safety checks by design and are not part of parity.
``ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT`` lists every input today's code
accepts that ``resolve_unit`` refuses. It is the behaviour change A3 to A5
must record in design section 27 when they adopt ``resolve_unit``, so the
list is pinned and must never grow silently.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from flightrisk import edge_device
from flightrisk.dashboard import __main__ as dashboard_main
from flightrisk.dashboard import manual_control
from flightrisk.drone import plan_edit, safety, temp_preflight
from flightrisk.drone.mavlink_fleet import MavlinkFleetOptions
from flightrisk.params.resolver import ParamRefused, resolve, resolve_unit, today_accepts_from_golden
from flightrisk.params.spec import Source

GOLDEN = json.loads((Path(__file__).parent / "golden" / "flight_params.json").read_text())
TODAY = today_accepts_from_golden(GOLDEN)

#: Section 27 items that C5 turns into refusals add entries here; A1 has none.
PARITY_EXCEPTIONS: dict[str, str] = {}

REFUSED = "refused"
IGNORED = "ignored"


class _Refused(Exception):
    """Today's reader refused the input."""


def _today(read: Callable[[], Any]) -> Any:
    """Run today's reader; any refusal it raises becomes ``REFUSED``."""
    try:
        return read()
    except (ValueError, SystemExit, argparse.ArgumentTypeError, _Refused):
        return REFUSED


def _resolved(model: str, param_id: str, *, env: Mapping[str, str] | None = None,
              cli: Mapping[str, Any] | None = None, simulated: bool = False) -> Any:
    """The resolver's value of ``param_id`` for one input, ``REFUSED`` or ``IGNORED`` (absent)."""
    layers: dict[Source, Mapping[str, Any]] = {}
    if env:
        layers[Source.ENV] = env
    if cli:
        layers[Source.CLI] = cli
    try:
        result = resolve(model, "drone-1", layers, simulated=simulated, today_accepts=TODAY)
    except ParamRefused:
        return REFUSED
    effective = result.get(param_id)
    return IGNORED if effective is None else effective.value


def _same(today: Any, resolved: Any) -> bool:
    if isinstance(today, str) or isinstance(resolved, str):
        return today == resolved
    if isinstance(today, (tuple, list)) or isinstance(resolved, (tuple, list)):
        return tuple(today) == tuple(resolved)
    if isinstance(today, bool) or isinstance(resolved, bool):
        return today is resolved
    return math.isclose(float(today), float(resolved), rel_tol=1e-9, abs_tol=1e-9)


# --- Today's readers, each called with one input -------------------------------------------------

#: Today's spelling of an enum value that the registry spells differently.
TODAY_SPELLING = {"rtl": "return"}
_EDGE_PARSER = edge_device._build_parser()
_FIELD_ARGS = ("alt_slot_m", "geofence_m", "ceiling_m", "ceiling_margin_m", "link_loss_hold_s", "failsafe_action")
#: The field limits with the cross-value slot check disabled, so only the single-value range is checked.
_FIELD_SINGLE = dataclasses.replace(safety.FIELD_LIMITS, min_alt_slot_cm=-math.inf)


def _flag_value(flag: str, raw: str) -> Any:
    """``raw`` converted by the real edge parser's action for ``flag``, with its choices checked."""
    action = _EDGE_PARSER._option_string_actions[flag]
    value = action.type(raw) if action.type else raw
    if action.choices is not None and value not in action.choices:
        raise _Refused(flag)
    return value


def _field_args(**given: Any) -> argparse.Namespace:
    values = {name: None for name in _FIELD_ARGS}
    values.update(given)
    return argparse.Namespace(**values)


def _room_envelope(name: str, raw: str, companions: Mapping[str, str] | None = None) -> safety.SafetyEnvelope:
    """The Tello edge's envelope; the slot is the lowest so the ceiling's cross-value check cannot bite."""
    return safety.SafetyEnvelope.from_env(alt_slot_cm=safety.MIN_ALT_SLOT_CM, environ={**(companions or {}), name: raw})


_FIELD_DEFAULT = {
    "geofence_m": safety.FIELD_LIMITS.default_geofence_cm, "ceiling_m": safety.FIELD_LIMITS.default_ceiling_cm,
    "ceiling_margin_m": safety.FIELD_LIMITS.default_ceiling_margin_cm,
    "alt_slot_m": safety.FIELD_LIMITS.default_alt_slot_cm,
}


def _field_cm(arg: str, value_m: float | None) -> Any:
    """A metre value as the field edge converts it (``round(m * 100, 1)``), then today's range check."""
    if value_m is None:
        return _FIELD_DEFAULT[arg]
    return _FIELD_CHECK[arg](round(value_m * 100.0, 1))


_FIELD_CHECK: dict[str, Callable[[float], float]] = {
    "geofence_m": lambda cm: safety.SafetyEnvelope.for_field(geofence_cm=cm).geofence_cm,
    "ceiling_m": lambda cm: safety.validate_ceiling(cm, safety.FIELD_LIMITS.min_ceiling_margin_cm, _FIELD_SINGLE)[0],
    "ceiling_margin_m": lambda cm: safety.validate_ceiling(safety.FIELD_LIMITS.max_ceiling_cm, cm, _FIELD_SINGLE)[1],
    "alt_slot_m": lambda cm: safety.SafetyEnvelope.validate_alt_slot(cm, safety.FIELD_LIMITS),
}


def _field_env(arg: str) -> Callable[[str], Any]:
    env_name = edge_device._FIELD_ENV[arg]
    return lambda raw: _field_cm(arg, edge_device._field_value_m(_field_args(), arg, {env_name: raw}))


def _field_flag(flag: str, arg: str) -> Callable[[str], Any]:
    return lambda raw: _field_cm(arg, edge_device._field_value_m(_field_args(**{arg: _flag_value(flag, raw)}), arg, {}))


def _hold(value: Any) -> float:
    """The field envelope stores a 0 s hold as None (land at once)."""
    return 0.0 if value is None else value


def _obstacle(kind: str | None, attr: str) -> Callable[[str, str], Any]:
    def read(name: str, raw: str) -> Any:
        envelope = safety.SafetyEnvelope.for_field() if kind == "mavlink" else safety.SafetyEnvelope()
        return getattr(edge_device.ObstacleSettings(kind, envelope, {name: raw}), attr)
    return read


def _guards(attr: str) -> Callable[[str, str], Any]:
    return lambda name, raw: getattr(edge_device._flight_guards({name: raw}), attr)


def _temp(attr: str, companion: Mapping[str, str]) -> Callable[[str, str], Any]:
    def read(name: str, raw: str) -> Any:
        value = getattr(temp_preflight.TempLimits.from_env({**companion, name: raw}), attr)
        return "off" if value is None else value
    return read


# --- The probe table -----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Probe:
    """One name on one model: today's reader of a raw text and the inputs to try."""

    name: str
    model: str
    param_id: str
    read: Callable[[str], Any]
    inputs: tuple[str, ...]
    companions: Mapping[str, str] = dataclasses.field(default_factory=dict)
    flag: bool = False
    simulated: bool = False


T, F = "tello", "generic_multirotor"
G = "FLIGHTRISK_"
_BAD = ("abc", "", " ", "nan", "inf")


def _env_probe(name, model, param_id, read, inputs, **kw) -> Probe:
    """``read(name, raw)`` style reader bound to ``name``."""
    return Probe(name, model, param_id, lambda raw: read(name, raw), tuple(inputs) + _BAD, **kw)


def _numbers(*values: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in values)


_ROOM_ATTR = {"GEOFENCE_CM": "geofence_cm", "CEILING_CM": "ceiling_cm", "CEILING_MARGIN_CM": "ceiling_margin_cm"}
_FIELD_ATTR = {"geofence_m": "envelope.geofence_cm", "ceiling_m": "envelope.ceiling_cm",
               "ceiling_margin_m": "envelope.ceiling_margin_cm", "alt_slot_m": "envelope.alt_slot_cm"}
_FIELD_INPUTS = {
    "geofence_m": _numbers(-1, 0, 4.99, 5, 30, 500, 500.01, 1.234),
    "ceiling_m": _numbers(9.99, 10, 30, 120, 120.01, 12.345),
    "ceiling_margin_m": _numbers(2.99, 3, 5, 30, 30.01),
    "alt_slot_m": _numbers(9.99, 10, 15, 120, 120.01),
}
_FIELD_FLAG = {"geofence_m": "--geofence-m", "ceiling_m": "--ceiling-m", "ceiling_margin_m": "--ceiling-margin-m",
               "alt_slot_m": "--alt-slot-m"}


def _probes() -> list[Probe]:
    out: list[Probe] = []
    # Room envelope (the Tello edge's SafetyEnvelope.from_env); the margin is probed under a 10 m ceiling
    # so that the ceiling-minus-margin cross check (a constraint, not parity) does not bite.
    room = {
        "GEOFENCE_CM": (_numbers(-5, 0, 1, 39, 40, 500, 5000, 5001, 100000), {}),
        "CEILING_CM": (_numbers(149, 150, 200, 1000, 1001, 150.5), {}),
        "CEILING_MARGIN_CM": (_numbers(29, 30, 60, 500, 501, 900), {G + "CEILING_CM": "1000"}),
    }
    for short, (inputs, companions) in room.items():
        attr = _ROOM_ATTR[short]
        out.append(_env_probe(G + short, T, "envelope." + attr,
                              lambda n, raw, a=attr, c=companions: getattr(_room_envelope(n, raw, c), a),
                              inputs, companions=companions))
        # The field edge never reads the indoor centimetre names (decision 3).
        out.append(_env_probe(G + short, F, "envelope." + attr,
                              lambda n, raw, a=attr: getattr(edge_device._field_envelope(_field_args(), {n: raw}), a),
                              _numbers(150, 99999, -1)))
    out.append(Probe("--alt-slot", T, "envelope.alt_slot_cm", edge_device._parse_alt_slot,
                     _numbers(49, 50, 100, 500, 501, 5, 1.6, 0.4, 100.5, 0, -5) + _BAD[:2], flag=True))
    for arg, inputs in _FIELD_INPUTS.items():
        out.append(Probe(edge_device._FIELD_ENV[arg], F, _FIELD_ATTR[arg], _field_env(arg), inputs + _BAD))
        out.append(Probe(_FIELD_FLAG[arg], F, _FIELD_ATTR[arg], _field_flag(_FIELD_FLAG[arg], arg),
                         inputs + _BAD[:1] + _BAD[3:], flag=True))
    # Battery and link loss.
    for model in (T, F):
        out.append(_env_probe(G + "MIN_TAKEOFF_BATTERY", model, "battery.min_takeoff_pct",
                              lambda n, raw: safety.min_takeoff_battery_from_env({n: raw}),
                              _numbers(14, 15, 19, 20, 30, 95, 96, 100, 101, 30.5, -1)))
    out.append(_env_probe(G + "LINK_LOSS_LAND_S", T, "link.loss_detect_s",
                          lambda n, raw: _room_envelope(n, raw).link_watchdog.timeout_s,
                          _numbers(-1, 0, 1.4, 1.5, 3, 10, 10.1, 30, 31)))
    out.append(_env_probe(G + "LINK_LOSS_LAND_S", F, "link.loss_detect_s",
                          lambda n, raw: edge_device._field_envelope(_field_args(), {n: raw}).link_watchdog.timeout_s,
                          _numbers(-1, 0, 1.4, 1.5, 3, 10, 10.1, 30, 31)))
    hold_inputs = _numbers(-1, 0, 1.4, 1.5, 10, 30, 30.1)
    out.append(_env_probe(G + "FIELD_LINK_LOSS_HOLD_S", F, "link.loss_wait_s",
                          lambda n, raw: _hold(edge_device._field_envelope(_field_args(), {n: raw}).link_loss_hold_s),
                          hold_inputs))
    # The Tello edge's envelope never reads the field hold: the Tello lands at once.
    out.append(_env_probe(G + "FIELD_LINK_LOSS_HOLD_S", T, "link.loss_wait_s",
                          lambda n, raw: _hold(_room_envelope(n, raw).link_loss_hold_s), hold_inputs))
    out.append(Probe("--link-loss-hold-s", F, "link.loss_wait_s",
                     lambda raw: _hold(edge_device._field_envelope(
                         _field_args(link_loss_hold_s=_flag_value("--link-loss-hold-s", raw)), {}).link_loss_hold_s),
                     hold_inputs + ("abc", "nan"), flag=True))
    out.append(_env_probe(G + "FAILSAFE_ACTION", F, "link.loss_action",
                          lambda n, raw: TODAY_SPELLING.get(edge_device._failsafe_action(_field_args(), {n: raw}),
                                                            edge_device._failsafe_action(_field_args(), {n: raw})),
                          ("rtl", "land", "RTL", " Land ", "return", "hover")))
    out.append(Probe("--failsafe-action", F, "link.loss_action",
                     lambda raw: TODAY_SPELLING.get(_flag_value("--failsafe-action", raw),
                                                    _flag_value("--failsafe-action", raw)),
                     ("rtl", "land", "RTL", "return", "hover"), flag=True))
    return out


def _more_probes() -> list[Probe]:
    out: list[Probe] = []
    for model in (T, F):
        # The ground's stranded-drone timers clamp to their minimum (policy: clamp, risk R5).
        out.append(_env_probe(G + "MANUAL_ORPHAN_LAND_S", model, "stranded.countdown_s",
                              lambda n, raw: manual_control.manual_orphan_land_s({n: raw}),
                              _numbers(-5, 0, 29, 30, 120, 600, 601, 45.5)))
        out.append(_env_probe(G + "MANUAL_LOST_IDLE_S", model, "stranded.lost_idle_s",
                              lambda n, raw: manual_control.manual_lost_idle_s({n: raw}),
                              _numbers(-5, 4, 5, 30, 300, 301, 7.5)))
        out.append(_env_probe(G + "CAMERA_FOOTPRINT_CM", model, "plan.camera_footprint_cm",
                              lambda n, raw: plan_edit.camera_footprint_cm({n: raw}) or "unset",
                              _numbers(19, 20, 300, 5000, 5001, 300.5, 300.0)))
    # Test-adapter facts: honoured on the Tello (a test adapter), read by the edge whatever the drone.
    guards = {
        "TELLO_SETTLE_SAMPLES": ("settle_samples", "takeoff.settle_samples", _numbers(1, 2, 5, 50, 51, "5.0", "+5")),
        "TELLO_SETTLE_TOLERANCE_CM": ("settle_tolerance_cm", "takeoff.settle_tolerance_cm",
                                      _numbers(1.9, 2, 10, 50, 50.1, "+10", "1e1")),
        "TELLO_SETTLE_TIMEOUT_S": ("settle_timeout_s", "takeoff.settle_timeout_s", _numbers(0.9, 1, 5, 10, 10.1)),
        "TELLO_TAKEOFF_LATE_S": ("takeoff_late_s", "takeoff.late_s", _numbers(1.9, 2, 10, 20, 20.1)),
        "TELLO_REFUSAL_RETRIES": ("refusal_retries", "takeoff.refusal_retries", _numbers(-1, 0, 2, 3, 4, "2.0")),
        "TELLO_REFUSAL_HOLD_S": ("refusal_hold_s", "takeoff.refusal_hold_s", _numbers(0.4, 0.5, 2.5, 5, 5.1)),
        "TELLO_REFUSALS_PER_FLIGHT": ("refusals_per_flight", "tello.refusals_per_flight", _numbers(-1, 0, 2, 6, 7)),
        "HOVER_TEST": ("hover_test", "mode.hover_test", ("0", "1", "true", "off", "YES", "2", "maybe")),
    }
    for short, (attr, param_id, inputs) in guards.items():
        out.append(_env_probe(G + short, T, param_id, _guards(attr), inputs))
    out.append(_env_probe(G + "TELLO_TEMP_WARN_C", T, "tello.temp_warn_c",
                          _temp("warn_c", {G + "TELLO_TEMP_REFUSE_C": "off"}), _numbers(39, 40, 92, 120, 121, 92.5, "+92")))
    out.append(_env_probe(G + "TELLO_TEMP_REFUSE_C", T, "tello.temp_refuse_c",
                          _temp("refuse_c", {G + "TELLO_TEMP_WARN_C": "40"}),
                          _numbers(39, 40, 95, 120, 121, "off", "OFF", 95.0)))
    obstacle = {
        "OBSTACLE_SENSING": ("mode", "sensing.mode", ("required", "off", "OFF", "on", "none")),
        "OBSTACLE_TURN": ("turn", "sensing.turn", ("1", "0", "true", "off", "maybe")),
        "OBSTACLE_SETTLE_S": ("settle_s", "sensing.settle_s", _numbers(0.09, 0.1, 0.3, 3, 3.01, "+0.3", "3e-1")),
        "OBSTACLE_MIN_SAFE_DEPTH": ("min_safe_depth", "sensing.min_safe_depth", _numbers(0.04, 0.05, 0.35, 0.9, 0.91)),
        "OBSTACLE_PERSON_FRACTION": ("person_fraction", "sensing.person_fraction", _numbers(0.19, 0.2, 0.55, 0.9, 0.91)),
        "OBSTACLE_CLEAR_VOTES": ("clear_votes", "avoid.clear_votes", _numbers(0, 1, 2, 5, 6, "2.0")),
        "OBSTACLE_RELOOK": ("relook", "avoid.relook", _numbers(-1, 0, 2, 5, 6)),
        "OBSTACLE_SIDESTEP_CM": ("sidestep_cm", "avoid.sidestep_cm", _numbers(-1, 0, 19, 20, 50, 100, 101)),
        "OBSTACLE_SIDESTEPS": ("sidesteps", "avoid.sidesteps", _numbers(-1, 0, 2, 4, 5)),
    }
    for model, kind in ((T, None), (F, "mavlink")):
        rows = dict(obstacle)
        rows["OBSTACLE_STEP_CM"] = ("step_cm", "sensing.step_cm", _numbers(19, 20, 50, 100, 101, 500, 5000, 5001))
        if model == T:
            rows["MAX_DECODE_LAG_S"] = ("max_decode_lag_s", "sensing.max_decode_lag_s", _numbers(0.09, 0.1, 0.3, 2, 2.01))
        for short, (attr, param_id, inputs) in rows.items():
            # A MAVLink unit honours a test-adapter fact override only when it is a verified simulated unit.
            out.append(_env_probe(G + short, model, param_id, _obstacle(kind, attr), inputs, simulated=model == F))
    area = ("400", "600x400", "600X400", "600 x 400", "0", "-5", "0x400", "600x400x2", "abc", "400.5")
    out.append(Probe(G + "FLEET_AREA_CM", T, "plan.area_cm", dashboard_main._parse_area, area))
    out.append(Probe("--fleet-area-cm", T, "plan.area_cm", dashboard_main._parse_area, area, flag=True))
    out.append(Probe("--takeoff-alt-m", F, "takeoff.height_cm",
                     lambda raw: MavlinkFleetOptions(takeoff_alt_m=_flag_value("--takeoff-alt-m", raw))
                     .takeoff_alt_m * 100.0, _numbers(-1, 0, 1.99, 2, 5, 20, 20.01, "abc", "nan"), flag=True))
    out.append(Probe("--speed-mps", F, "move.speed_cm_s",
                     lambda raw: MavlinkFleetOptions(speed_mps=_flag_value("--speed-mps", raw))
                     .speed_mps * 100.0, _numbers(-1, 0, 0.49, 0.5, 3, 15, 15.01, "abc", "nan"), flag=True))
    return out


PROBES = _probes() + _more_probes()
CASES = [(p, raw) for p in PROBES for raw in dict.fromkeys(p.inputs)]


@pytest.mark.parametrize("probe,raw", CASES, ids=[f"{p.model}:{p.name}={raw!r}" for p, raw in CASES])
def test_resolver_matches_today(probe: Probe, raw: str) -> None:
    today = _today(lambda: probe.read(raw))
    if probe.flag:
        resolved = _resolved(probe.model, probe.param_id, cli={probe.name: raw}, simulated=probe.simulated)
    else:
        resolved = _resolved(probe.model, probe.param_id, env={**probe.companions, probe.name: raw},
                             simulated=probe.simulated)
    assert (today == REFUSED) == (resolved == REFUSED) and (today == REFUSED or _same(today, resolved)), (
        f"{probe.name}={raw!r} on {probe.model}: today {today!r}, resolver {resolved!r}"
    )


def test_parity_has_no_exceptions() -> None:
    assert PARITY_EXCEPTIONS == {}


#: Probe inputs that today's single-value reader and ``resolve`` both accept but ``resolve_unit`` refuses
#: through a cross-value constraint (K-8: ceiling minus margin below the slot band; K-12: field ceiling
#: under the default bands; the test-adapter temperature pair). Some of these are refused today as well by
#: today's own cross-value check (for example ``--alt-slot 500`` under the default 2 m ceiling); the list
#: is the full set the probes find. A3 to A5 record it in design section 27 when they adopt
#: ``resolve_unit``. It must never grow silently.
ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT = frozenset({
    (T, G + "CEILING_CM", "150"),
    (T, G + "CEILING_MARGIN_CM", "900"),
    (T, "--alt-slot", "500"),
    (F, G + "FIELD_CEILING_M", "10"),
    (F, G + "FIELD_CEILING_M", "12.345"),
    (F, "--ceiling-m", "10"),
    (F, "--ceiling-m", "12.345"),
    (F, G + "FIELD_CEILING_MARGIN_M", "30"),
    (F, G + "FIELD_CEILING_MARGIN_M", "30.01"),
    (F, "--ceiling-margin-m", "30"),
    (F, "--ceiling-margin-m", "30.01"),
    (F, G + "FIELD_ALT_SLOT_M", "120"),
    (F, "--alt-slot-m", "120"),
    (T, G + "TELLO_TEMP_WARN_C", "120"),
    (T, G + "TELLO_TEMP_REFUSE_C", "40"),
})


def test_accepted_today_refused_by_resolve_unit_is_pinned() -> None:
    found = set()
    for probe, raw in CASES:
        layer = {probe.name: raw} if probe.flag else {**probe.companions, probe.name: raw}
        source = Source.CLI if probe.flag else Source.ENV
        if _today(lambda: probe.read(raw)) == REFUSED:
            continue
        if _resolved(probe.model, probe.param_id, **{source.value: layer}, simulated=probe.simulated) == REFUSED:
            continue
        try:
            resolve_unit(probe.model, "drone-1", {source: layer}, simulated=probe.simulated, today_accepts=TODAY)
        except ParamRefused:
            found.add((probe.model, probe.name, raw))
    assert found == ACCEPTED_TODAY_REFUSED_BY_RESOLVE_UNIT


#: Names today's runtime reads that the probes above cannot compare, with the reason.
UNCOMPARED = {
    ("FLIGHTRISK_FLEET_STRIP_CM", "--fleet-strip-cm"):
        "parsed by argparse type=int and checked positive inside dashboard __main__.main(), which starts the server",
    ("FLIGHTRISK_FAILSAFE_ACTION on a Tello",):
        "the Tello edge never reads it (only _build_mavlink_drone calls _failsafe_action); no reader to call",
    ("--geofence-m, --ceiling-m, --ceiling-margin-m, --alt-slot-m, --failsafe-action, --link-loss-hold-s on a Tello",):
        "refused by the mavlink_only check inside edge_device._resolve_drone_and_source, which builds the drone",
}


def test_uncompared_list_is_small_and_explicit() -> None:
    assert len(UNCOMPARED) == 3
