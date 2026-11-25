"""Operator texts for refused input, unavailable capabilities and fallback landings.

Every text is plain words with numbers. A test-adapter fact is never offered
to the operator as something to change, so the refused-input form refuses to
render one. Whether a row is a fact is read from the registry by its id, so a
caller cannot render a fact by leaving out ``edit``.
"""

from __future__ import annotations

from .spec import Edit
from .table import TABLE

#: Ids of the rows that are a test-adapter fact on any profile.
FACT_IDS = frozenset(row.id for row in TABLE if any(e is Edit.FACT for e in row.edit.values()))


def _num(value: float) -> str:
    """Whole numbers without a decimal point, other numbers as written."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render_refused_input(
    param_id: str,
    value: float,
    unit: str,
    source_detail: str,
    model_name: str,
    lo: float,
    hi: float,
    edit: Edit = Edit.FILE_ENV,
) -> str:
    """Name a refused value, where it came from and what the model allows.

    Example: "`envelope.ceiling_margin_cm` 20 cm from FLIGHTRISK_CEILING_MARGIN_CM
    is refused: the Tello allows 30 to 500 cm."

    A row that is a fact in the registry is refused whatever ``edit`` says;
    ``edit`` only adds the refusal for an id the registry does not hold.
    """
    if param_id in FACT_IDS or edit is Edit.FACT:
        raise ValueError(f"{param_id} is a test-adapter fact and is never shown as an input")
    return (
        f"`{param_id}` {_num(value)} {unit} from {source_detail} is refused: "
        f"the {model_name} allows {_num(lo)} to {_num(hi)} {unit}."
    )


def render_unavailable_capability(unit_id: str, capability: str, model_name: str, reason: str) -> str:
    """Say that a unit cannot do something and why.

    Example: "drone-1 cannot do bird's-eye search: the Tello has one fixed forward camera."
    """
    return f"{unit_id} cannot do {capability}: the {model_name} {reason}."


def render_fallback_landing(unit_id: str, quantity: str, value: float, unit: str, bound: float) -> str:
    """Say that a unit landed in place because a value passed its bound.

    Example: "drone-1 landed in place: position uncertainty 62 cm is above the 50 cm bound."
    """
    return (
        f"{unit_id} landed in place: {quantity} {_num(value)} {unit} "
        f"is above the {_num(bound)} {unit} bound."
    )


def render_launcher_for_other_unit(variable: str, launcher_unit_id: str, unit_id: str) -> str:
    """Refuse a launcher layer written for another unit than the one this edge flies.

    Example: "FLIGHTRISK_LAUNCHER_PARAMS is for unit drone-9, but this edge is --drone-id drone-1.
    Start the edge from the launcher again for drone-1, or unset FLIGHTRISK_LAUNCHER_PARAMS."
    """
    return (
        f"{variable} is for unit {launcher_unit_id}, but this edge is --drone-id {unit_id}. "
        f"Start the edge from the launcher again for {unit_id}, or unset {variable}."
    )


def render_unconfirmed_switch_off(unit_id: str, reason: str, source: str) -> str:
    """Refuse a safety feature switched off without the operator's confirmation.

    Example: "drone-1: switching forward_obstacle_sensing off needs the operator's confirmation,
    which a feature switch from the launcher cannot give."
    """
    return f"{unit_id}: {reason}, which a feature switch from {source} cannot give."


def render_shadowed_value(
    unit_id: str, param_id: str, variable: str, value: float, layer: str, layer_value: float, flown: float,
) -> str:
    """Refuse a variable the edge does not read for this unit that would hide a launcher or params-file value.

    Example: "drone-1: FLIGHTRISK_FIELD_ALT_SLOT_M sets `envelope.alt_slot_cm` to 120 over 130 from
    FLIGHTRISK_LAUNCHER_PARAMS, but this edge does not read FLIGHTRISK_FIELD_ALT_SLOT_M for its drone and
    would fly 100. Unset FLIGHTRISK_FIELD_ALT_SLOT_M."
    """
    return (
        f"{unit_id}: {variable} sets `{param_id}` to {_num(value)} over {_num(layer_value)} from {layer}, "
        f"but this edge does not read {variable} for its drone and would fly {_num(flown)}. Unset {variable}."
    )


def render_incomplete_acknowledgement(unit_id: str, param_id: str, problem: str) -> str:
    """Refuse turning the altitude cap off with an acknowledgement record that is missing or malformed.

    Example: "drone-1: `regulatory.altitude_cap_on` false from the launcher is refused: the
    acknowledgement has no `text_version`. Turning the altitude cap off needs the operator's complete
    acknowledgement from the launcher. Refusing to start."
    """
    return (
        f"{unit_id}: `{param_id}` false from the launcher is refused: {problem}. Turning the altitude cap "
        f"off needs the operator's complete acknowledgement from the launcher. Refusing to start."
    )


def render_cap_off_unknown_maximum(unit_id: str, model_name: str) -> str:
    """Refuse turning the altitude cap off on a unit whose airframe maximum altitude is unknown.

    Example: "drone-1 cannot turn the altitude cap off: the Generic multirotor has no known airframe
    maximum altitude. The inventory must declare the airframe maximum. Refusing to start."
    """
    return (
        f"{unit_id} cannot turn the altitude cap off: the {model_name} has no known airframe maximum "
        f"altitude. The inventory must declare the airframe maximum. Refusing to start."
    )


def render_inventory_only(param_id: str, source_detail: str) -> str:
    """Refuse a value that only the inventory may state, from any other layer.

    Example: "`envelope.max_altitude_cm` from params.json is refused: the airframe maximum is a fact
    about the airframe, and only the inventory may state it. Refusing to start."
    """
    return (
        f"`{param_id}` from {source_detail} is refused: the airframe maximum is a fact about the airframe, "
        f"and only the inventory may state it. Refusing to start."
    )


def render_confirm_flag_not_allowed(param_id: str) -> str:
    """Refuse a ``--confirm-less-safe`` flag for a row only the launcher's acknowledgement can confirm.

    Example: "--confirm-less-safe cannot confirm `regulatory.altitude_cap_on`: only the launcher's
    acknowledgement can turn the altitude cap off. Refusing to start."
    """
    return (
        f"--confirm-less-safe cannot confirm `{param_id}`: only the launcher's acknowledgement can turn "
        f"the altitude cap off. Refusing to start."
    )
