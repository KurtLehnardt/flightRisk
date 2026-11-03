"""The shared match score vectors agree with the Python scorer.

``tests/fixtures/mobile_contract/match_score_vectors.json`` is read by the Kotlin
and Swift scorer tests. Python is the reference: every vector here is recomputed
with ``flightrisk.vision.scorer.MatchScorer`` and must match exactly, so a change
to the Python scorer (or a hand edit of the file) fails here before it can leave
the apps and the ground disagreeing about a score.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from flightrisk.vision.scorer import MatchScorer

PATH = Path(__file__).resolve().parent / "fixtures" / "mobile_contract" / "match_score_vectors.json"
DOC = json.loads(PATH.read_text(encoding="utf-8"))
SPECIAL = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def _decode(value):
    return SPECIAL[value] if isinstance(value, str) else value


def _same(expected, actual) -> bool:
    expected, actual = _decode(expected), _decode(actual)
    if isinstance(expected, float) and math.isnan(expected):
        return isinstance(actual, float) and math.isnan(actual)
    return expected == actual


def _scorer() -> MatchScorer:
    weights = DOC["_meta"]["weights"]
    return MatchScorer(
        reid_weight=weights["reid"], face_weight=weights["face"], reasoning_weight=weights["reasoning"],
        match_threshold=DOC["_meta"]["match_threshold"],
    )


def test_file_has_a_meta_block_and_unique_names():
    names = [v["name"] for v in DOC["vectors"]]
    assert len(names) == len(set(names)) > 100
    assert DOC["_meta"]["file"] == "match_score_vectors.json"


def test_vectors_cover_rounding_boundaries_and_non_finite_values():
    by_name = {v["name"]: v for v in DOC["vectors"]}
    # Half to even on the exact binary value, not truncation and not half up.
    assert by_name["reid_only_0.0625"]["combined_score"] == 0.062
    assert by_name["reid_only_0.1875"]["combined_score"] == 0.188
    assert by_name["reid_only_0.0005"]["combined_score"] == 0.001
    assert any(v["reid"] == "NaN" for v in DOC["vectors"])
    assert any(v["reid"] == "Infinity" for v in DOC["vectors"])


@pytest.mark.parametrize("vector", DOC["vectors"], ids=[v["name"] for v in DOC["vectors"]])
def test_vector_matches_the_python_scorer(vector):
    scorer = _scorer()
    result = scorer.score(
        reid_score=_decode(vector["reid"]), face_score=_decode(vector["face"]), reasoning_result=vector["reasoning"],
    )
    assert _same(vector["combined_score"], result["combined_score"])
    assert vector["is_match"] == result["is_match"]
    assert vector["confidence_level"] == result["confidence_level"]
    assert vector["signals_used"] == result.get("signals_used", 0)
    assert vector["alert_level"] == scorer.alert_level(result)
