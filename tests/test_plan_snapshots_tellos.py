"""Snapshots of today's fleet plans for one to four Tellos, taken before any per-unit limit change.

Each JSON file under ``tests/fixtures/plan_snapshots/`` records what is flown
for one fleet size and one planner: the zone rectangles, altitude slots,
lanes, grid, gap, strip, problems and uncovered areas, the plan's reason, and
the exact ``assign_lanes`` messages the coordinator sends to each link. The
files were generated from the code as it was before the drone-agnostic
changes to the planner modules, so a later change that adds a per-unit limit
parameter must leave every byte unchanged for a Tello.

Three planners are covered for drone ids ``d1`` to ``d4``:

* ``coordinator_plan``: ``FleetCoordinator.plan()`` with its own defaults;
* ``map_<pattern>``: ``plan_edit.build_plan`` for each map pattern, installed
  with ``plan_zones()`` the way the dashboard does it;
* ``patterns_<kind>``: ``fleet_patterns.plan_search`` for each pattern kind
  with the Tello forward camera model, with its ``check_plan`` result.

A combination that is refused today is recorded as a refusal, not skipped.
Every link is an in-process recording link and the ground clock is fixed, so
nothing touches the network, a thread or the wall clock. Every FLIGHTRISK_*
variable is removed while the snapshots are built, so the result does not
depend on the shell.

Regenerate the fixtures, only when a change is meant to alter a plan, from the
repository root with ``python -m tests.test_plan_snapshots_tellos``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import functools
import json
import math
import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from flightrisk.drone import fleet_patterns as fp
from flightrisk.drone import plan_edit
from flightrisk.drone import plan_paths as pp
from flightrisk.drone.coordinator import DEFAULT_ZONE_GAP_CM, FleetCoordinator
from flightrisk.drone.coverage import tello_sweep_model
from flightrisk.drone.safety import DEFAULT_GEOFENCE_CM

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "plan_snapshots"
FLEET_SIZES = (1, 2, 3, 4)
GROUND_NOW_S = 1000.0
# The dashboard's default search area and lane spacing for a fleet plan.
AREA_CM = (400, 400)
STRIP_CM = plan_edit.DEFAULT_STRIP_CM
# The 4 m demo room, as the existing pattern tests model the Tello camera.
ROOM_CAMERA = tello_sweep_model(max_range_m=4.0)
# Two search areas for the pattern builders: the demo room and a larger field.
PATTERN_AREAS = {"room_400x400": (400, 400), "field_1200x800": (1200, 800)}
PATTERN_LKP_FRACTION = (1, 2)


class RecordingLink:
    """An in-process EdgeLink that records every message the coordinator sends and answers nothing."""

    def __init__(self) -> None:
        self.on_message: Callable[[dict], None] | None = None
        self.sent: list[dict] = []

    def send(self, msg: dict) -> None:
        self.sent.append(json.loads(json.dumps(msg)))


@contextlib.contextmanager
def _clean_environment() -> Iterator[None]:
    """Remove every FLIGHTRISK_* variable for the duration, then restore them."""
    saved = {key: value for key, value in os.environ.items() if key.startswith("FLIGHTRISK_")}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


def _plain(value: Any) -> Any:
    """*value* as plain JSON data: dataclasses, enums, tuples and mappings converted, non-finite floats as text."""
    if isinstance(value, enum.Enum):
        return _plain(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _refusal(exc: Exception) -> dict:
    return {"refused": {"type": type(exc).__name__, "message": str(exc)}}


def _drone_ids(n: int) -> list[str]:
    return [f"d{i + 1}" for i in range(n)]


def _coordinator(n: int) -> tuple[FleetCoordinator, dict[str, RecordingLink]]:
    links = {drone_id: RecordingLink() for drone_id in _drone_ids(n)}
    coordinator = FleetCoordinator(links, area_cm=AREA_CM, strip_cm=STRIP_CM, clock=lambda: GROUND_NOW_S)
    return coordinator, links


def _sent(links: Mapping[str, RecordingLink]) -> dict:
    return {drone_id: link.sent for drone_id, link in links.items()}


def _coordinator_snapshot(n: int) -> dict:
    coordinator, links = _coordinator(n)
    out: dict[str, Any] = {
        "drone_ids": _drone_ids(n),
        "area_cm": list(AREA_CM),
        "strip_cm": STRIP_CM,
        "zone_gap_cm": DEFAULT_ZONE_GAP_CM,
    }
    try:
        plan = coordinator.plan()
    except ValueError as exc:
        out.update(_refusal(exc))
    else:
        out["plan"] = _plain(plan.to_dict())
    out["assign_lanes"] = _sent(links)
    return out


def _layout_takeoffs(drone_ids: list[str], area: pp.Rect) -> dict:
    """Each drone's takeoff mark as the dashboard's ``layout_takeoffs`` reads it."""
    try:
        zones = pp.split_area(area, len(drone_ids), DEFAULT_ZONE_GAP_CM)
    except ValueError:
        return {}
    return {drone_id: (zone.x, zone.y) for drone_id, zone in zip(drone_ids, zones)}


def _map_snapshot(n: int, pattern: str) -> dict:
    coordinator, links = _coordinator(n)
    area = pp.Rect(0, 0, *AREA_CM)
    spec = plan_edit.PlanSpec(pattern, area)
    built = plan_edit.build_plan(
        spec,
        coordinator.drone_ids,
        strip_cm=STRIP_CM,
        gap_cm=DEFAULT_ZONE_GAP_CM,
        geofence_cm=float(DEFAULT_GEOFENCE_CM),
        alt_slot_of=coordinator.zone_alt_cm,
        takeoffs=_layout_takeoffs(coordinator.drone_ids, area),
        footprint_cm=plan_edit.camera_footprint_cm(),
        sidestep_margin_of=coordinator.sidestep_margin_of,
    )
    out: dict[str, Any] = {
        "drone_ids": _drone_ids(n),
        "pattern": pattern,
        "ok": built.ok,
        "built": _plain(built.payload()),
    }
    try:
        zones = built.zone_assignments()
    except ValueError as exc:
        out.update(_refusal(exc))
    else:
        out["zone_assignments"] = [_plain(zone.to_dict()) for zone in zones]
        plan = coordinator.plan_zones(
            zones,
            grid_cm=(area.x, area.y, area.w, area.h),
            gap_cm=built.gap_cm,
            strip_cm=built.strip_cm,
            uncovered_areas=built.uncovered,
            details=built.payload(),
        )
        out["plan"] = _plain(plan.to_dict())
    out["assign_lanes"] = _sent(links)
    return out


def _pattern_snapshot(n: int, kind: fp.PatternKind) -> dict:
    out: dict[str, Any] = {"drone_ids": _drone_ids(n), "kind": kind.value, "cases": {}}
    for case, area in PATTERN_AREAS.items():
        lkp = None
        if kind is not fp.PatternKind.PARALLEL_SPLIT:
            lkp = (area[0] * PATTERN_LKP_FRACTION[0] // PATTERN_LKP_FRACTION[1],
                   area[1] * PATTERN_LKP_FRACTION[0] // PATTERN_LKP_FRACTION[1])
        entry: dict[str, Any] = {"area_cm": list(area), "lkp_cm": None if lkp is None else list(lkp)}
        try:
            plan = fp.plan_search(_drone_ids(n), area, None, lkp, footprint=ROOM_CAMERA)
        except ValueError as exc:
            entry.update(_refusal(exc))
        else:
            entry["plan"] = _plain(plan.to_dict())
            entry["check_plan"] = fp.check_plan(plan)
            messages: dict[str, Any] = {}
            for drone in plan.drones:
                try:
                    messages[drone.drone_id] = plan.assign_lanes_message(drone.drone_id)
                except ValueError as exc:
                    messages[drone.drone_id] = _refusal(exc)
            entry["assign_lanes_message"] = _plain(messages)
        out["cases"][case] = entry
    return out


def _text(snapshot: dict) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def render_snapshots() -> dict[str, str]:
    """Every snapshot as file name to exact file text, built from the current code."""
    files: dict[str, str] = {}
    with _clean_environment():
        for n in FLEET_SIZES:
            files[f"tellos_{n}_coordinator_plan.json"] = _text(_coordinator_snapshot(n))
            for pattern in pp.PATTERNS:
                files[f"tellos_{n}_map_{pattern}.json"] = _text(_map_snapshot(n, pattern))
        for n in FLEET_SIZES:
            files[f"tellos_{n}_patterns_{fp.PatternKind.PARALLEL_SPLIT.value}.json"] = _text(
                _pattern_snapshot(n, fp.PatternKind.PARALLEL_SPLIT))
        kind = fp.PatternKind.SPLIT_EXPANDING_SQUARE
        files[f"tellos_2_patterns_{kind.value}.json"] = _text(_pattern_snapshot(2, kind))
        kind = fp.PatternKind.SQUARE_SPIRAL
        files[f"tellos_1_patterns_{kind.value}.json"] = _text(_pattern_snapshot(1, kind))
    return files


@functools.cache
def _rendered() -> dict[str, str]:
    return render_snapshots()


def test_fixture_files_match_the_rendered_set():
    on_disk = {path.name for path in FIXTURE_DIR.glob("*.json")}
    assert on_disk == set(_rendered()), (
        "the snapshot files differ from what is rendered; regenerate with "
        "`python -m tests.test_plan_snapshots_tellos`"
    )


def test_rendering_is_repeatable():
    assert render_snapshots() == _rendered()


@pytest.mark.parametrize("name", sorted(_rendered()))
def test_snapshot_is_unchanged(name: str, monkeypatch):
    for key in [key for key in os.environ if key.startswith("FLIGHTRISK_")]:
        monkeypatch.delenv(key)
    path = FIXTURE_DIR / name
    assert path.is_file(), f"{name} is missing from {FIXTURE_DIR}"
    expected = _rendered()[name]
    actual = path.read_bytes().decode("ascii")
    assert actual == expected, f"{name} differs from the plan rendered today"


def write_snapshots() -> list[str]:
    """Write every snapshot file, removing stale ones; returns the file names."""
    files = render_snapshots()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in FIXTURE_DIR.glob("*.json"):
        if stale.name not in files:
            stale.unlink()
    for name, text in files.items():
        (FIXTURE_DIR / name).write_bytes(text.encode("ascii"))
    return sorted(files)


if __name__ == "__main__":
    for written in write_snapshots():
        print(written)
