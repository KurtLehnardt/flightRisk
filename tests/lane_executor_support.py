"""Shared fakes and helpers for the protocol v2 lane executor tests.

Not a test module (pytest collects only ``test_*.py``). The executors here
run against ``FakeTelloController`` subclasses with an injected fake clock;
nothing touches the network or a drone.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk.drone.lane_executor import LaneExecutor
from flightrisk.drone.safety import SafetyEnvelope
from flightrisk.drone.tello import FakeTelloController
from flightrisk.observability import StructuredLogger

WAIT_S = 10.0
FENCE = fp.Fence(epoch=3, leader_id="mac")
ZONE = {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}
LANES = [
    {"direction": "forward", "distance_cm": 200},
    {"direction": "right", "distance_cm": 100},
    {"direction": "back", "distance_cm": 200},
]


class FakeClock:
    """Monotonic clock that only moves when the test advances it."""

    def __init__(self, start: float = 1000.0):
        self._t = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def advance(self, dt: float) -> None:
        with self._lock:
            self._t += dt

    def set(self, t: float) -> None:
        with self._lock:
            self._t = t


class QuietLogger(StructuredLogger):
    """Records events instead of printing them."""

    def __init__(self) -> None:
        super().__init__(component="test-lane-v2")
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _emit(self, level, event, **kwargs):
        self.events.append((level, event, kwargs))

    def names(self) -> list[str]:
        return [event for _, event, _ in self.events]


class SteppedTello(FakeTelloController):
    """Fake Tello whose every move waits until the test lets it finish.

    ``release(n)`` lets ``n`` moves finish. A landing interrupts a waiting
    move, which then completes like an interrupted ``FakeTelloController``
    move (it returns True). ``busy`` is true while a move is with the drone,
    and ``waiting`` while it is held there with no release pending (so the
    executor cannot make progress until the test acts). ``move_log`` records
    each move as it starts, with ``nudge=True`` for the move that follows
    ``expect_nudge()``. With ``auto`` set, moves finish at once.
    """

    def __init__(self, auto: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.auto = auto
        self.busy = False
        self.completed = 0
        self.move_log: list[dict[str, Any]] = []
        self._permits = 0
        self._next_is_nudge = False
        self._cv = threading.Condition()

    @property
    def waiting(self) -> bool:
        with self._cv:
            return self.busy and self._permits == 0 and not self.auto and not self._interrupt.is_set()

    def expect_nudge(self, expected: bool = True) -> None:
        self._next_is_nudge = expected

    def release(self, n: int = 1) -> None:
        with self._cv:
            self._permits += n
            self._cv.notify_all()

    def move(self, direction: str, distance_cm: int) -> bool:
        if not self.state.is_flying:
            raise RuntimeError(f"[{self.name}] cannot move while landed")
        if direction not in ("forward", "back", "left", "right", "up", "down"):
            return False
        self._record("move", direction, distance_cm)
        deadline = time.monotonic() + WAIT_S
        with self._cv:
            self.busy = True
            self.move_log.append({"direction": direction, "distance_cm": distance_cm, "nudge": self._next_is_nudge})
            self._next_is_nudge = False
            try:
                while not self.auto and not self._interrupt.is_set():
                    if self._permits > 0:
                        self._permits -= 1
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("test never released the move")
                    self._cv.wait(0.005)
            finally:
                self.busy = False
                self.completed += 1
        if direction == "up":
            self.state.height += distance_cm
        elif direction == "down":
            self.state.height = max(0, self.state.height - distance_cm)
        return True


def wait_for(predicate, timeout: float = WAIT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.003)
    return bool(predicate())


def of_type(sent: list[dict], mtype: str, ack_for: str | None = None) -> list[dict]:
    return [m for m in sent if m.get("type") == mtype and (ack_for is None or m.get("for") == ack_for)]


def replies_for(sent: list[dict], cmd_id: str) -> list[dict]:
    return [m for m in sent if m.get("type") in ("ack", "nack") and m.get("cmd_id") == cmd_id]


def reply_for(sent: list[dict], cmd_id: str) -> dict | None:
    replies = replies_for(sent, cmd_id)
    assert len(replies) <= 1, replies
    return replies[0] if replies else None


def horizontal_moves(drone: FakeTelloController) -> list[tuple]:
    return [c for c in drone.calls if c[0] == "move" and c[1] in ("forward", "back", "left", "right")]


def dead_reckoned(drone: FakeTelloController) -> tuple[float, float]:
    """Position from every horizontal move the drone flew (heading 0)."""
    x = y = 0.0
    for _, direction, distance in horizontal_moves(drone):
        x += {"right": distance, "left": -distance}.get(direction, 0)
        y += {"forward": distance, "back": -distance}.get(direction, 0)
    return x, y


def assign(drone_id: str = "drone-1", lanes=LANES, *, plan_id: str = "p-1", plan_seq: int = 1,
           fence: fp.Fence = FENCE, cmd_id: str | None = None, **kwargs) -> dict[str, Any]:
    return fp.make_assign_lanes(drone_id, 100, lanes, ZONE, fence=fence, cmd_id=cmd_id,
                                plan_id=plan_id, plan_seq=plan_seq, **kwargs)


def start_at(t_ground_s: float, *, plan_id: str = "p-1", cmd_id: str | None = None) -> dict[str, Any]:
    return fp.make_start_at(t_ground_s, fence=FENCE, plan_id=plan_id, cmd_id=cmd_id)


def hold(cmd_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
    return fp.make_hold(FENCE, cmd_id=cmd_id, reason=reason)


def resume(plan_id: str = "p-1", cmd_id: str | None = None) -> dict[str, Any]:
    return fp.make_resume(FENCE, plan_id=plan_id, cmd_id=cmd_id)


def nudge(direction: str = "left", distance_cm: int = 50, cmd_id: str | None = None) -> dict[str, Any]:
    return fp.make_nudge(FENCE, direction, distance_cm, cmd_id=cmd_id)


def takeoff(cmd_id: str | None = None) -> dict[str, Any]:
    return fp.make_takeoff(fence=FENCE, cmd_id=cmd_id)


def validate_sent(sent: list[dict]) -> None:
    """Every message the executor sent is valid: v2 when it has v2 fields, else v1."""
    for msg in sent:
        v1_reply = msg.get("type") in ("ack", "nack") and "cmd_id" not in msg and msg.get("for") not in fp.FAILSAFE_TYPES
        v1_telemetry = msg.get("type") == "telemetry" and "state" not in msg
        fp.validate_edge_to_ground(msg, proto=1 if v1_reply or v1_telemetry else 2)


def make_v2_executor(drone=None, clock=None, *, geofence_cm: float = 500, alt_slot_cm: float = 100,
                     fleet_v2: bool = True, **kwargs) -> tuple[LaneExecutor, Any, list[dict], FakeClock, QuietLogger]:
    """An executor in v2 mode on *drone* (an auto-finishing ``SteppedTello`` by default), synced at offset 0."""
    clock = clock if clock is not None else FakeClock()
    drone = drone if drone is not None else SteppedTello(auto=True, name="drone-1", battery=90)
    drone.connect()
    envelope = SafetyEnvelope(alt_slot_cm=alt_slot_cm, geofence_cm=geofence_cm, clock=clock)
    sent: list[dict] = []
    log = QuietLogger()
    executor = LaneExecutor(drone, envelope, "drone-1", send=sent.append, clock=clock, logger=log,
                            fleet_v2=fleet_v2, **kwargs)
    executor.handle_clock({"type": "clock", "offset_s": 0.0})
    return executor, drone, sent, clock, log


def send(executor: LaneExecutor, msg: dict[str, Any]) -> None:
    """Deliver one ground message the way the edge device does."""
    executor.note_ground_message()
    executor.handle_message(msg, recv_t=executor.now())


def fly_to_lane(executor: LaneExecutor, drone: SteppedTello, clock: FakeClock, sent: list[dict],
                lanes=LANES, plan_id: str = "p-1", plan_seq: int = 1, **assign_kwargs) -> None:
    """Assign *lanes*, start, and stop with the climb move in flight (a stepped drone)."""
    send(executor, assign(lanes=lanes, plan_id=plan_id, plan_seq=plan_seq, **assign_kwargs))
    t_ground = clock() + 0.5
    send(executor, start_at(t_ground, plan_id=plan_id))
    clock.set(t_ground)
    assert wait_for(lambda: drone.waiting)
