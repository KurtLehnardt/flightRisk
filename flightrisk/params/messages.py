"""Operator texts for refused input, unavailable capabilities and fallback landings.

Every text is plain words with numbers. A test-adapter fact is never offered
to the operator as something to change, so the refused-input form refuses to
render one.
"""

from __future__ import annotations

from .spec import Edit


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
    """
    if edit is Edit.FACT:
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
