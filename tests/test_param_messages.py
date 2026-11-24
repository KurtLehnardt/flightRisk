"""The operator message forms render the design examples byte for byte."""

from __future__ import annotations

import pytest

from flightrisk.params.messages import (
    FACT_IDS, render_fallback_landing, render_launcher_for_other_unit, render_refused_input,
    render_unavailable_capability, render_unconfirmed_switch_off,
)
from flightrisk.params.spec import Edit
from flightrisk.params.table import TABLE


def test_refused_input_example():
    assert render_refused_input(
        "envelope.ceiling_margin_cm", 20, "cm", "FLIGHTRISK_CEILING_MARGIN_CM", "Tello", 30, 500,
    ) == ("`envelope.ceiling_margin_cm` 20 cm from FLIGHTRISK_CEILING_MARGIN_CM "
          "is refused: the Tello allows 30 to 500 cm.")


def test_unavailable_capability_example():
    assert render_unavailable_capability(
        "drone-1", "bird's-eye search", "Tello", "has one fixed forward camera",
    ) == "drone-1 cannot do bird's-eye search: the Tello has one fixed forward camera."


def test_fallback_landing_example():
    assert render_fallback_landing(
        "drone-1", "position uncertainty", 62, "cm", 50,
    ) == "drone-1 landed in place: position uncertainty 62 cm is above the 50 cm bound."


def test_fact_rows_are_never_rendered_as_refused_inputs():
    with pytest.raises(ValueError):
        render_refused_input("sensing.min_safe_depth", 1, "cm", "FLIGHTRISK_X", "Tello", 2, 3, edit=Edit.FACT)


def test_fact_ids_are_the_registry_fact_rows():
    assert len(FACT_IDS) == 32
    assert "tello.temp_warn_c" in FACT_IDS


@pytest.mark.parametrize("param_id", sorted(FACT_IDS))
@pytest.mark.parametrize("edit", list(Edit))
def test_a_registry_fact_is_refused_whatever_the_caller_passes(param_id, edit):
    with pytest.raises(ValueError):
        render_refused_input(param_id, 130, "C", "FLIGHTRISK_X", "Tello", 40, 120, edit=edit)
    with pytest.raises(ValueError):
        render_refused_input(param_id, 130, "C", "FLIGHTRISK_X", "Tello", 40, 120)


def test_a_non_fact_registry_row_still_renders():
    assert not {row.id for row in TABLE if row.id == "envelope.ceiling_margin_cm"} & FACT_IDS


@pytest.mark.parametrize("edit", [e for e in Edit if e is not Edit.FACT])
def test_other_rows_render_without_tuning_words(edit):
    text = render_refused_input("a.b_cm", 1.0, "cm", "FLIGHTRISK_A", "Tello", 2, 3, edit=edit)
    assert text == "`a.b_cm` 1 cm from FLIGHTRISK_A is refused: the Tello allows 2 to 3 cm."
    assert not {"calibrate", "tune", "uncalibrated"} & set(text.lower().split())


def test_launcher_for_other_unit_example():
    assert render_launcher_for_other_unit("FLIGHTRISK_LAUNCHER_PARAMS", "drone-9", "drone-1") == (
        "FLIGHTRISK_LAUNCHER_PARAMS is for unit drone-9, but this edge is --drone-id drone-1. "
        "Start the edge from the launcher again for drone-1, or unset FLIGHTRISK_LAUNCHER_PARAMS."
    )


def test_unconfirmed_switch_off_example():
    assert render_unconfirmed_switch_off(
        "drone-1", "switching forward_obstacle_sensing off needs the operator's confirmation", "the launcher"
    ) == (
        "drone-1: switching forward_obstacle_sensing off needs the operator's confirmation, "
        "which a feature switch from the launcher cannot give."
    )
