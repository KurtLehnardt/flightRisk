"""The mission and return types import cleanly and pull in no runtime module."""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from flightrisk.mission import model

_RUNTIME = ("edge_device", "coordinator", "dashboard")


@pytest.mark.parametrize("module", ["flightrisk.mission.model", "flightrisk.drone.rth", "flightrisk.hardware.frames"])
def test_module_imports_no_runtime_module(module):
    code = (
        "import sys, importlib; importlib.import_module(%r); "
        "bad = sorted(m for m in sys.modules if any(r in m.split('.')[-1] or m.endswith('.' + r) "
        "for r in %r) and m.startswith('flightrisk')); print(bad)" % (module, _RUNTIME)
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_records_are_frozen():
    task = model.Task(id="t1", type=model.TaskType.KEEP_WATCH)
    assert task.state is model.TaskState.UNASSIGNED
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.state = model.TaskState.RUNNING


def test_vocabularies_match_the_design():
    assert [s.value for s in model.TargetStatus] == ["searching", "sighted", "found", "escorted", "lost"]
    assert len(model.FallbackReason) == 17
    assert model.ReturnState.RTH_ABOVE_HOME.value == "rth_above_home"


def test_mission_defaults():
    mission = model.Mission(id="m1")
    assert mission.on_find_policy is model.OnFindPolicy.KEEP_WATCH
    assert mission.tasks == ()


def test_rth_protocols_import():
    from flightrisk.drone import rth

    assert set(rth.__all__) == {"HomeRecord", "ReturnMethod"}
