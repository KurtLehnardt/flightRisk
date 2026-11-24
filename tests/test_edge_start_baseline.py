"""The live edge still answers what it answered at the A3 base commit (Phase A3, steps S1 and S2).

``tests/fixtures/edge_start_baseline.json`` was recorded once by
``tests/tools/make_edge_baseline.py`` from the real runtime modules at a2c0c89.
A3 rewires today's readers to the resolver, so ``tests/test_param_parity.py``
would end up comparing the resolver with itself. This test keeps an independent
record: every start object built from an empty environment, and every parity
probe sent through the readers, must still equal the fixture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FIXTURE = json.loads((HERE / "fixtures" / "edge_start_baseline.json").read_text())

_spec = importlib.util.spec_from_file_location("make_edge_baseline", HERE / "tools" / "make_edge_baseline.py")
maker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maker)


def _same_json(value):
    """``value`` after the same JSON round trip the fixture went through."""
    return json.loads(json.dumps(value, sort_keys=True))


@pytest.fixture(scope="module")
def live_start_objects():
    return _same_json(maker.start_objects())


@pytest.mark.parametrize("name", sorted(FIXTURE["start_objects"]))
def test_start_object_equals_the_baseline(name, live_start_objects):
    assert live_start_objects[name] == FIXTURE["start_objects"][name]


def test_start_objects_have_no_extra_or_missing_entries(live_start_objects):
    assert set(live_start_objects) == set(FIXTURE["start_objects"])


def test_the_baseline_covers_every_a3_module():
    assert set(FIXTURE["start_objects"]["module_constants"]) == set(maker.A3_MODULES)


@pytest.fixture(scope="module")
def live_probes():
    return _same_json(maker.probe_results())


def test_probes_cover_every_case_of_the_parity_test(live_probes):
    from tests.test_param_parity import CASES

    assert len(FIXTURE["probes"]) == len(CASES) == len(live_probes)
    assert set(live_probes) == set(FIXTURE["probes"])


@pytest.mark.parametrize("key", sorted(FIXTURE["probes"]))
def test_probes_reader_output_equals_the_baseline(key, live_probes):
    assert live_probes[key] == FIXTURE["probes"][key], key
