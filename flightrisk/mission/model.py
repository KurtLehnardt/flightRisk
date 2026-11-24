"""Mission, task, target and sighting records (types only, no behaviour).

The records follow ``docs/design/hardware-profiles/design.md`` section 14.1 and the return
vocabulary of section 10. They are used from phases D3 and D4. This module imports only the
standard library, and no runtime module (not the edge, the coordinator or the dashboard).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class TargetStatus(str, Enum):
    SEARCHING = "searching"
    SIGHTED = "sighted"
    FOUND = "found"
    ESCORTED = "escorted"
    LOST = "lost"


class TaskState(str, Enum):
    UNASSIGNED = "unassigned"
    WAITING_FOR_CONFIRM = "waiting_for_confirm"
    ASSIGNED = "assigned"
    RUNNING = "running"
    DONE = "done"
    UNSEARCHED = "unsearched"
    ABANDONED = "abandoned"


class TaskType(str, Enum):
    WIDE_SWEEP = "wide_sweep"
    THERMAL_SWEEP = "thermal_sweep"
    HEAD_ON_SEARCH = "head_on_search"
    CLOSE_SEARCH = "close_search"
    KEEP_WATCH = "keep_watch"
    CONVERGE_AND_VERIFY = "converge_and_verify"
    LOCAL_SEARCH = "local_search"
    ESCORT = "escort"


class OnFindPolicy(str, Enum):
    KEEP_WATCH = "keep_watch"
    RETURN_ALL = "return_all"
    LAND_ALL = "land_all"


class ReturnState(str, Enum):
    """The return states of design section 10.3."""

    FLYING = "flying"
    HOLDING = "holding"
    RTH_RETURNING = "rth_returning"
    RTH_WAITING = "rth_waiting"
    RTH_ABOVE_HOME = "rth_above_home"
    RTH_LANDING = "rth_landing"
    LANDED = "landed"
    LANDED_IN_PLACE = "landed_in_place"
    EXTERNAL_CONTROL = "external_control"
    CANNOT_BE_RECALLED = "cannot_be_recalled"


class FallbackReason(str, Enum):
    """The closed list of return fallback reasons of design section 10.5."""

    CRITICAL_BATTERY = "critical_battery"
    BATTERY_CANNOT_REACH_HOME = "battery_cannot_reach_home"
    FAULT_IMU = "fault_imu"
    FAULT_MOTOR = "fault_motor"
    FAULT_POSITIONING = "fault_positioning"
    FAULT_UNCLASSIFIED = "fault_unclassified"
    WAY_HOME_BLOCKED_OBSTACLE = "way_home_blocked_obstacle"
    WAY_HOME_BLOCKED_PERSON = "way_home_blocked_person"
    NO_KNOWN_HOME = "no_known_home"
    UNCERTAINTY_ABOVE_BOUND = "uncertainty_above_bound"
    NO_CLEARED_ROUTE = "no_cleared_route"
    CANNOT_BE_COMMANDED = "cannot_be_commanded"
    OPERATOR_LAND = "operator_land"
    LAND_ALL = "land_all"
    ON_FIND_LAND_ALL = "on_find_land_all"
    CEILING_BREACH = "ceiling_breach"
    NO_RETURN_ORDER = "no_return_order"


@dataclass(frozen=True)
class Target:
    id: str
    references: Tuple[str, ...] = ()
    status: TargetStatus = TargetStatus.SEARCHING


@dataclass(frozen=True)
class Task:
    id: str
    type: TaskType
    place: Optional[Tuple[float, float]] = None
    required_capabilities: Tuple[str, ...] = ()
    preferred_capabilities: Tuple[str, ...] = ()
    band: Optional[str] = None
    precision_tolerance_cm: Optional[float] = None
    priority: int = 0
    created_at: float = 0.0
    progress: float = 0.0
    state: TaskState = TaskState.UNASSIGNED
    #: One reason per unit that could not take the task.
    unassigned_reasons: Tuple[Tuple[str, str], ...] = ()
    assignee: Optional[str] = None


@dataclass(frozen=True)
class Sighting:
    finder: str
    point: Tuple[float, float]
    uncertainty_cm: Optional[float] = None
    #: Match score per target id, as ``(target_id, score)`` pairs.
    scores: Tuple[Tuple[str, float], ...] = ()
    attributed_to: Optional[str] = None
    #: ``ambiguous``, ``attributed`` or ``locked`` once the operator confirmed.
    attribution_state: str = "attributed"
    evidence: Tuple[str, ...] = ()
    #: An aid only, never a decision.
    claude_opinion: Optional[str] = None


@dataclass(frozen=True)
class Mission:
    id: str
    environment: str = "indoor"
    areas: Tuple[str, ...] = ()
    targets: Tuple[Target, ...] = ()
    units: Tuple[str, ...] = ()
    on_find_policy: OnFindPolicy = OnFindPolicy.KEEP_WATCH
    tasks: Tuple[Task, ...] = ()
    no_entry_areas: Tuple[str, ...] = ()
    log: Tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "FallbackReason",
    "Mission",
    "OnFindPolicy",
    "ReturnState",
    "Sighting",
    "Target",
    "TargetStatus",
    "Task",
    "TaskState",
    "TaskType",
]
