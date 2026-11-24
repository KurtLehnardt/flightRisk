"""CAP-4: changing a fake drone's declared capabilities changes its return method and separation.

The rules that read the declarations are not edited here. Only what the fake declares changes.

The return method half holds today: the recovery flow reads ``Capability.RETURN_TO_HOME``. The separation half
does not yet: no production code reads ``DroneCapabilities.flight_scale`` to pick the safety envelope profile,
so a test that called ``limits_for()`` itself would prove nothing. That half is a strict xfail that fails until
a production reader exists; when one lands it passes, the strict marker turns that into a failure, and the
reader gets a real test.
"""

import ast
from pathlib import Path

import pytest

from flightrisk.dashboard.recovery import run_confirm_recovery
from flightrisk.drone.controller import Capability
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig

ROOT = Path(__file__).resolve().parent.parent


def _recover(drone: FakeDrone) -> tuple[str, list[str]]:
    drone.connect()
    drone.takeoff()
    outcome = run_confirm_recovery(drone)
    return outcome, [call[0] for call in drone.calls]


def test_default_fake_declares_todays_capabilities():
    caps = FakeDrone().capabilities
    assert caps.flight_scale == "room"
    assert caps.supports(Capability.RETURN_TO_HOME)
    assert (caps.min_move_cm, caps.max_move_cm) == (20, 500)


def test_declaring_no_return_to_home_changes_the_return_method():
    outcome, calls = _recover(FakeDrone())
    assert outcome == "returned_and_landed"
    assert "return_to_home" in calls
    outcome, calls = _recover(FakeDrone(config=FakeDroneConfig(supports_return_to_home=False)))
    assert outcome == "landed_in_place"
    assert "return_to_home" not in calls


def _flight_scale_readers() -> list[str]:
    """Each place under ``flightrisk/`` that reads a ``flight_scale`` attribute (declaring one is not a read)."""
    readers = []
    for path in sorted((ROOT / "flightrisk").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "flight_scale" and isinstance(node.ctx, ast.Load):
                readers.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return readers


@pytest.mark.xfail(
    strict=True,
    reason="no production code reads declared capabilities for separation yet; CAP-4 separation lands with the reader",
)
def test_declaring_a_field_scale_changes_the_separation():
    assert _flight_scale_readers(), "no production code reads DroneCapabilities.flight_scale"
