"""The three operator message forms render the design examples byte for byte."""

from __future__ import annotations

import pytest

from flightrisk.params.messages import (
    FACT_IDS, render_fallback_landing, render_refused_input, render_unavailable_capability,
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
