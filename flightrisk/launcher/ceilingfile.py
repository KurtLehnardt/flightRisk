"""The launcher writes the dashboard's ceiling file before it starts the ground station.

On a dashboard flight the dashboard's plan decides each drone's altitude slot, from
``~/.flightrisk/ceiling.json`` (``FLIGHTRISK_CEILING_FILE`` overrides the path, see
``docs/ceiling-and-altitude.md``). So the launcher writes the ceiling, the margin and the per-drone
slots it shows in its window into that file when Start is pressed, and the dashboard flies the same
slots as the edges were started with.

The format and every limit come from ``flightrisk.dashboard.ceiling.CeilingSettings``; this module
imports it lazily, and that module needs neither Flask nor the vision stack, so an edge-only install
can still import the launcher. Keys in the file that the dashboard does not know are kept.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flightrisk.launcher.settings import LauncherSettings


class CeilingWriteError(RuntimeError):
    """The ceiling file was not written; the message says why."""


def ceiling_file_path(environ: Mapping[str, str] | None = None) -> Path:
    """``FLIGHTRISK_CEILING_FILE``, else ``~/.flightrisk/ceiling.json``, exactly as the dashboard resolves it."""
    from flightrisk.dashboard.ceiling import default_settings_path

    return default_settings_path(environ)


def desired_settings(settings: LauncherSettings) -> Any:
    """The ``CeilingSettings`` the window describes: its ceiling, its margin and one slot per drone."""
    from flightrisk.dashboard.ceiling import CeilingSettings

    return CeilingSettings(
        ceiling_cm=settings.ceiling_cm,
        margin_cm=settings.margin_cm,
        slots_cm={d.drone_id: d.alt_slot_cm for d in settings.drones},
    )


def _values(ceiling: Any) -> dict[str, Any]:
    return {"ceiling_cm": ceiling.ceiling_cm, "margin_cm": ceiling.margin_cm, "slots_cm": dict(sorted(ceiling.slots_cm.items()))}


@dataclass(frozen=True)
class CeilingPlan:
    """What Start will write, where, and how it differs from what is on disk now."""

    path: Path
    will_write: bool
    values: dict[str, Any]
    previous: dict[str, Any] | None
    previous_problem: str | None
    changes: tuple[str, ...]
    error: str | None
    note: str

    @property
    def differs(self) -> bool:
        return bool(self.changes)

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "will_write": self.will_write,
            "values": self.values,
            "previous": self.previous,
            "previous_problem": self.previous_problem,
            "differs": self.differs,
            "changes": list(self.changes),
            "error": self.error,
            "note": self.note,
        }


def _read_previous(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(the saved values, None), (None, None) when there is no file, or (None, why) when it is unusable."""
    from flightrisk.dashboard.ceiling import CeilingSettings

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    try:
        return _values(CeilingSettings.from_json(json.loads(text))), None
    except (ValueError, TypeError) as exc:
        return None, f"{path} is not valid ceiling settings: {exc}"


def _describe_changes(old: dict[str, Any], new: dict[str, Any]) -> tuple[str, ...]:
    changes = []
    if old["ceiling_cm"] != new["ceiling_cm"]:
        changes.append(f"ceiling {old['ceiling_cm']} cm becomes {new['ceiling_cm']} cm")
    if old["margin_cm"] != new["margin_cm"]:
        changes.append(f"margin {old['margin_cm']} cm becomes {new['margin_cm']} cm")
    for drone_id in sorted(set(old["slots_cm"]) | set(new["slots_cm"])):
        before, after = old["slots_cm"].get(drone_id), new["slots_cm"].get(drone_id)
        if before == after:
            continue
        if before is None:
            changes.append(f"{drone_id} slot is set to {after} cm")
        elif after is None:
            changes.append(f"{drone_id} slot of {before} cm is removed")
        else:
            changes.append(f"{drone_id} slot {before} cm becomes {after} cm")
    return tuple(changes)


def plan_ceiling_write(settings: LauncherSettings, path: Path | None = None, environ: Mapping[str, str] | None = None) -> CeilingPlan:
    """What Start would write for *settings*. Reads the file but never writes it.

    Edges only: nothing is written, because no dashboard runs on this computer.
    """
    path = path if path is not None else ceiling_file_path(environ)
    try:
        wanted = desired_settings(settings)
        wanted.validate()
        values = _values(wanted)
        error = None
    except ValueError as exc:
        values, error = {"ceiling_cm": settings.ceiling_cm, "margin_cm": settings.margin_cm, "slots_cm": {}}, str(exc)
    if not settings.runs_ground:
        return CeilingPlan(path, False, values, None, None, (), error,
                           "Edges only: the launcher does not write the ceiling file; the dashboard on the ground computer owns it.")
    previous, problem = _read_previous(path)
    if error is not None:
        return CeilingPlan(path, False, values, previous, problem, (), error, f"The ceiling file will not be written: {error}")
    if previous is None and problem is None:
        changes: tuple[str, ...] = ("there is no ceiling file yet",)
        note = f"Start will create {path} with ceiling {values['ceiling_cm']} cm, margin {values['margin_cm']} cm and the slots shown."
    elif previous is None:
        changes = ("the saved file is unusable",)
        note = f"Start will replace {path}, which is unusable ({problem})."
    else:
        changes = _describe_changes(previous, values)
        note = (f"Start will change the saved ceiling settings in {path}: " + "; ".join(changes) + "."
                if changes else f"{path} already holds these values; Start writes the same values again.")
    return CeilingPlan(path, True, values, previous, problem, changes, None, note)


def write_ceiling(settings: LauncherSettings, path: Path | None = None, *, processes_running: bool = False,
                  environ: Mapping[str, str] | None = None) -> CeilingPlan:
    """Write the ceiling file for *settings* atomically and return what was planned.

    Refused while a ground or edge started by this launcher runs (the dashboard refuses ceiling changes
    in flight), for the edges-only role, and for values the dashboard would refuse. Keys in the file
    that the dashboard does not know are kept. Raises ``CeilingWriteError`` or ``OSError``.
    """
    plan = plan_ceiling_write(settings, path, environ)
    if processes_running:
        raise CeilingWriteError("the ceiling file is not changed while processes started by this launcher are running")
    if not plan.will_write:
        raise CeilingWriteError(plan.error or plan.note)
    wanted = desired_settings(settings)
    existing: dict[str, Any] = {}
    try:
        loaded = json.loads(plan.path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, ValueError):
        pass
    merged = {**existing, **wanted.to_json()}
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan.path.with_name(f".{plan.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(merged, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, plan.path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return plan
