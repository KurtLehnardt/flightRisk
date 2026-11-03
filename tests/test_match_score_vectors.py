"""The shared match score vectors must be reproduced by the Python scorer.

``tests/fixtures/mobile_contract/match_score_vectors.json`` is read by the iOS and Android scorer tests as
well. Python is the reference: its ``round(x, 3)`` rounds the exact binary value, ties to even, and the phones
must give the same numbers. If this test fails, the vectors or the Python scorer changed, and both apps need
to be rechecked.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from flightrisk.vision.scorer import MatchScorer

VECTORS = Path(__file__).resolve().parent / "fixtures" / "mobile_contract" / "match_score_vectors.json"
CASES = json.loads(VECTORS.read_text(encoding="utf-8"))["cases"]


def _number(value):
    return float(value) if isinstance(value, str) else value


def _same_float(actual: float, expected) -> bool:
    expected = _number(expected)
    if math.isnan(expected):
        return math.isnan(actual)
    return actual == expected and math.copysign(1.0, actual) == math.copysign(1.0, expected)


def test_vectors_cover_the_hard_cases():
    assert len(CASES) >= 80
    expected = [_number(c["payload"]["expected"]["combined_score"]) for c in CASES.values()]
    assert any(v == 0.0 and math.copysign(1.0, v) < 0 for v in expected), "no negative zero result"
    assert any(math.isinf(v) for v in expected), "no infinite result"
    inputs = [_number(v) for c in CASES.values() for v in c["payload"]["signals"].values()]
    assert any(math.isnan(v) for v in inputs), "no NaN input"
    # Cases that differ between round-half-even on the binary value and truncation or half-up.
    assert CASES["single_face_half_even_tie"]["payload"]["expected"]["combined_score"] == 0.062
    assert CASES["single_face_half_even_tie_odd"]["payload"]["expected"]["combined_score"] == 0.188
    assert CASES["single_reid_0_9995"]["payload"]["expected"]["combined_score"] == 1.0


@pytest.mark.parametrize("name", sorted(CASES))
def test_python_scorer_reproduces_vector(name):
    payload = CASES[name]["payload"]
    scorer = MatchScorer(**payload["scorer"])
    for extra, weight in payload["extra_signal_weights"].items():
        scorer.register_signal(extra, weight)
    signals = {key: _number(value) for key, value in payload["signals"].items()}
    reid = signals.pop("reid", 0.0)
    face = signals.pop("face", 0.0)
    result = scorer.score(reid_score=reid, face_score=face, **signals)
    expected = payload["expected"]
    assert _same_float(result["combined_score"], expected["combined_score"]), (result, expected)
    assert result["is_match"] is expected["is_match"]
    assert result.get("signals_used", 0) == expected["signals_used"]
    assert result["confidence_level"] == expected["confidence_level"]
