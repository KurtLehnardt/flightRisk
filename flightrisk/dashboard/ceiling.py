"""Ground side of the ceiling height and altitude slots: settings, persistence, checks and edge sync.

On 2026-10-28 a Tello climbed to its 160 cm altitude slot and hit the
ceiling. This module gives the dashboard a ceiling height, a safety margin
and per-drone altitude slots (``docs/ceiling-and-altitude.md``):

* **Settings.** ``CeilingSettings`` holds the ceiling (default 200 cm), the
  margin (default 60 cm) and optional per-drone slots. The effective maximum
  slot is the ceiling minus the margin (140 cm by default). A drone without
  its own slot flies the coordinator's default for its zone
  (``DEFAULT_ALT_SLOTS_CM``, 100 and 130 cm in turn). The rules are those of
  the edge's ``SafetyEnvelope`` (``flightrisk.drone.safety``), so both sides
  agree on what is allowed.
* **Persistence.** ``CeilingStore`` keeps the settings in a small JSON file
  outside the repository, ``~/.flightrisk/ceiling.json`` (or
  ``FLIGHTRISK_CEILING_FILE``), written atomically. A file that cannot be
  read is not replaced by the defaults silently: Plan and Start are refused
  until the operator saves valid settings again.
* **Changes only on the ground.** ``CeilingBoard.update`` refuses any change
  while a drone reports flying or a start is scheduled (the fleet
  supervisor's ``flight_activity``), and on a standby ground, whose commands
  the edges would ignore. The server validates every value; the browser's
  checks are a convenience.
* **Plan and Start.** ``plan_slots`` refuses Plan, with a message such as
  ``drone-2 slot 160 cm is above the ceiling limit 140 cm (ceiling 200 cm
  minus margin 60 cm)``, when any drone's slot is above the limit, and while
  an edge that reports its ceiling has not applied the dashboard's.
  ``check_start`` refuses Start when the planned slots are above the limit or
  no longer match the settings.
* **Edges.** Each edge that reports ``ceiling_cm`` in its telemetry (an edge
  that understands ``set_ceiling``) is sent the ceiling, the margin and its
  slot whenever they differ from what it reports and it is on the ground:
  after a change, when it first reports, after it lands and after it
  reconnects. Older edges never report it and are never sent it; they still
  fly only slots the ground checked. The edge enforces the ceiling on its
  own as well and answers with an ``ack`` or a ``nack`` whose reason is shown.

Commands go out on a worker thread (``spawn``), never on a transport
delivery thread or in a Socket.IO handler.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flightrisk import fleet_protocol as fp
from flightrisk.drone.coordinator import DEFAULT_ALT_SLOTS_CM
from flightrisk.drone.safety import (
    DEFAULT_CEILING_CM,
    DEFAULT_CEILING_MARGIN_CM,
    MAX_ALT_SLOT_CM,
    MAX_CEILING_CM,
    MIN_ALT_SLOT_CM,
    MIN_CEILING_CM,
    MIN_CEILING_MARGIN_CM,
    format_cm,
    slot_above_ceiling,
    validate_ceiling,
)
from flightrisk.observability import StructuredLogger

__all__ = [
    "CEILING_FILE_ENV",
    "EVENT",
    "CeilingBoard",
    "CeilingRefusal",
    "CeilingSettings",
    "CeilingStore",
    "default_settings_path",
]

log = logging.getLogger(__name__)

#: Socket.IO event carrying ``CeilingBoard.payload()`` to the browser.
EVENT = "ceiling_settings"
#: Overrides where the settings are kept (tests, or a second ground on one machine).
CEILING_FILE_ENV = "FLIGHTRISK_CEILING_FILE"
#: Drones tracked at most; drone ids come off the network.
MAX_DRONES = 64
#: Seconds the ground waits for an edge's answer to ``set_ceiling``.
REPLY_TIMEOUT_S = 5.0
#: A drone silent for at least this long and then heard again counts as reconnected.
RECONNECT_GAP_S = 5.0
STANDBY_NOTE = (
    "This ground station is the standby. Edges take ceiling changes only from the leader ground; "
    "change them there."
)
_FILE_VERSION = 1


class CeilingRefusal(RuntimeError):
    """Plan, Start or a settings change refused because of the ceiling; the message says why."""


def default_settings_path(environ: Mapping[str, str] | None = None) -> Path:
    """``FLIGHTRISK_CEILING_FILE``, else ``~/.flightrisk/ceiling.json`` (outside the repository)."""
    env = os.environ if environ is None else environ
    raw = (env.get(CEILING_FILE_ENV) or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".flightrisk" / "ceiling.json"


def _whole_cm(name: str, value: Any) -> int:
    """A height entered by the operator: a whole number of cm (``120`` or ``120.0``, never ``True``)."""
    if isinstance(value, str):
        value = value.strip()
        try:
            value = float(value) if value else None
        except ValueError:
            raise ValueError(f"{name} must be a whole number of cm, got {value!r}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
        raise ValueError(f"{name} must be a whole number of cm, got {value!r}")
    return int(value)


@dataclass(frozen=True)
class CeilingSettings:
    """The ceiling, its margin and the per-drone altitude slots, all in cm."""

    ceiling_cm: int = DEFAULT_CEILING_CM
    margin_cm: int = DEFAULT_CEILING_MARGIN_CM
    slots_cm: Mapping[str, int] = field(default_factory=dict)

    @property
    def max_slot_cm(self) -> int:
        """The effective maximum altitude slot: the ceiling minus the margin."""
        return self.ceiling_cm - self.margin_cm

    def slot_problem(self, drone_id: str, slot_cm: float) -> str | None:
        """Why *slot_cm* cannot be *drone_id*'s slot, or None."""
        if not MIN_ALT_SLOT_CM <= slot_cm <= MAX_ALT_SLOT_CM:
            return f"{drone_id} slot {format_cm(slot_cm)} cm is outside {MIN_ALT_SLOT_CM}-{MAX_ALT_SLOT_CM} cm"
        reason = slot_above_ceiling(slot_cm, self.ceiling_cm, self.margin_cm)
        return None if reason is None else f"{drone_id} {reason}"

    def validate(self) -> None:
        """Raise ``ValueError`` naming every problem (the ceiling and margin first, then each slot)."""
        validate_ceiling(self.ceiling_cm, self.margin_cm)
        if len(self.slots_cm) > MAX_DRONES:
            raise ValueError(f"at most {MAX_DRONES} drones can have their own altitude slot")
        problems = []
        for drone_id, slot in sorted(self.slots_cm.items()):
            if not fp.is_identifier(drone_id):
                raise ValueError(f"{drone_id!r} is not a drone id")
            problem = self.slot_problem(drone_id, slot)
            if problem is not None:
                problems.append(problem)
        if problems:
            raise ValueError("; ".join(problems))

    def to_json(self) -> dict[str, Any]:
        return {
            "version": _FILE_VERSION,
            "ceiling_cm": self.ceiling_cm,
            "margin_cm": self.margin_cm,
            "slots_cm": dict(sorted(self.slots_cm.items())),
        }

    @classmethod
    def from_json(cls, data: Any) -> CeilingSettings:
        """Settings from the file's JSON object; raises ``ValueError`` or ``TypeError`` for anything malformed or unsafe."""
        if not isinstance(data, dict) or data.get("version") != _FILE_VERSION:
            raise TypeError(f"not a version {_FILE_VERSION} ceiling settings object")
        slots = data.get("slots_cm", {})
        if not isinstance(slots, dict):
            raise TypeError("'slots_cm' must be an object")
        settings = cls(
            ceiling_cm=_whole_cm("ceiling_cm", data.get("ceiling_cm")),
            margin_cm=_whole_cm("margin_cm", data.get("margin_cm")),
            slots_cm={str(k): _whole_cm(f"slot of {k}", v) for k, v in slots.items()},
        )
        settings.validate()
        return settings


class CeilingStore:
    """Loads and atomically saves ``CeilingSettings`` as JSON at *path* (``default_settings_path()``)."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path).expanduser() if path is not None else default_settings_path()

    def load(self) -> tuple[CeilingSettings, str | None]:
        """The saved settings, or the defaults when there is no file.

        Returns ``(settings, error)``. When the file exists but cannot be
        read or is not valid, *settings* are the defaults and *error* says
        why; the caller must not fly on them silently.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return CeilingSettings(), None
        except OSError as exc:
            return CeilingSettings(), f"cannot read {self.path}: {exc}"
        try:
            return CeilingSettings.from_json(json.loads(text)), None
        except (ValueError, TypeError) as exc:
            return CeilingSettings(), f"{self.path} is not valid ceiling settings: {exc}"

    def save(self, settings: CeilingSettings) -> None:
        """Write *settings* atomically (temporary file, fsync, rename). Raises ``OSError``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        data = json.dumps(settings.to_json(), indent=2, sort_keys=True) + "\n"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


@dataclass
class _EdgeCeiling:
    """What one edge reports about its ceiling, and this ground's last command to it."""

    drone_id: str
    supported: bool = False
    ceiling_cm: float | None = None
    margin_cm: float | None = None
    slot_cm: float | None = None
    flying: bool | None = None
    last_seen: float | None = None
    pending_cmd: str | None = None
    refusal_reason: str | None = None
    refusal_detail: str | None = None
    note: str | None = None


def _spawn_thread(fn: Callable[..., None], *args: Any) -> None:
    threading.Thread(target=fn, args=args, daemon=True, name="ceiling-send").start()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


class CeilingBoard:
    """The dashboard's ceiling settings, their checks and their sync to the edges; see the module docstring.

    Args:
        store: Where the settings are kept.
        transport: The ground transport (``GroundTransportSync``), or None
            without the edge source. Uses ``send_command`` and, when present,
            ``identity`` (a standby ground refuses changes; a leader fences
            its commands).
        emit: ``emit(event, payload)`` to every browser (``socketio.emit``).
        fleet_drones: Drone ids of a configured fleet, listed before they report.
        default_slots_cm: Slots given to zones in turn when a drone has none of its own.
        spawn: Runs ``fn(*args)`` in the background (tests run it inline).
        clock: Monotonic clock for reconnect detection.
        logger: Structured logger for the ``ceiling_changed``, ``slot_changed``
            and ``slot_refused_above_ceiling`` events.
    """

    def __init__(
        self,
        store: CeilingStore,
        transport: Any = None,
        emit: Callable[[str, dict[str, Any]], Any] | None = None,
        *,
        fleet_drones: Iterable[str] = (),
        default_slots_cm: tuple[int, ...] = DEFAULT_ALT_SLOTS_CM,
        reply_timeout_s: float = REPLY_TIMEOUT_S,
        spawn: Callable[..., None] = _spawn_thread,
        clock: Callable[[], float] = time.monotonic,
        reconnect_gap_s: float = RECONNECT_GAP_S,
        logger: StructuredLogger | None = None,
    ):
        self._store = store
        self._transport = transport
        self._emit_fn = emit
        self._fleet_drones = tuple(fleet_drones)
        self._default_slots = tuple(int(s) for s in default_slots_cm) or (DEFAULT_ALT_SLOTS_CM[0],)
        self._reply_timeout_s = float(reply_timeout_s)
        self._spawn = spawn
        self._clock = clock
        self._reconnect_gap_s = float(reconnect_gap_s)
        self._log = logger if logger is not None else StructuredLogger(component="dashboard")
        self._lock = threading.Lock()
        self._activity: Callable[[], str | None] | None = None
        self.settings, self.load_error = store.load()
        if self.load_error:
            self._log.error("ceiling_settings_unreadable", error=self.load_error)
        self._edges: dict[str, _EdgeCeiling] = {}
        for drone_id in self._fleet_drones:
            self._edge(drone_id)

    def bind_activity(self, activity: Callable[[], str | None]) -> None:
        """Give the board the fleet supervisor's ``flight_activity`` (why drones are not all on the ground)."""
        self._activity = activity

    # -- Slots ----------------------------------------------------------------

    def slot_for(self, drone_id: str, index: int, settings: CeilingSettings | None = None) -> int:
        """The altitude slot for *drone_id*, flying zone *index*: its own, else the default for the zone."""
        settings = settings if settings is not None else self.settings
        own = settings.slots_cm.get(drone_id)
        return own if own is not None else self._default_slots[index % len(self._default_slots)]

    def plan_slots(self, drone_ids: list[str]) -> dict[str, int]:
        """Each drone's slot for a new plan; raises ``CeilingRefusal`` when Plan must be refused.

        Refused when the settings file could not be read, when any slot is
        above the ceiling limit (every such drone is named), or when an edge
        that reports its ceiling has not applied the dashboard's yet (it is
        sent again).
        """
        with self._lock:
            settings, load_error = self.settings, self.load_error
        if load_error:
            raise CeilingRefusal(f"ceiling settings could not be loaded ({load_error}); save them again")
        slots = {drone_id: self.slot_for(drone_id, i, settings) for i, drone_id in enumerate(drone_ids)}
        problems = []
        for drone_id, slot in slots.items():
            problem = settings.slot_problem(drone_id, slot)
            if problem is not None:
                problems.append(problem)
                self._log.error(
                    "slot_refused_above_ceiling",
                    drone_id=drone_id,
                    source="plan",
                    alt_slot_cm=slot,
                    ceiling_cm=settings.ceiling_cm,
                    ceiling_margin_cm=settings.margin_cm,
                    max_alt_slot_cm=settings.max_slot_cm,
                )
        if problems:
            raise CeilingRefusal("; ".join(problems))
        stale = []
        with self._lock:
            for drone_id in drone_ids:
                edge = self._edges.get(drone_id)
                if edge is not None and edge.supported and not self._ceiling_matches_locked(edge, settings):
                    stale.append(
                        f"{drone_id} has not applied the dashboard ceiling yet (it reports ceiling "
                        f"{format_cm(edge.ceiling_cm)} cm, margin {format_cm(edge.margin_cm)} cm; the dashboard "
                        f"has {settings.ceiling_cm} cm, {settings.margin_cm} cm)"
                    )
        if stale:
            for drone_id in drone_ids:
                self.sync(drone_id, reason="plan")
            raise CeilingRefusal("; ".join(stale) + "; it was sent again, press Plan again in a moment")
        return slots

    def check_start(self, plan: Mapping[str, Any] | None) -> dict[str, str]:
        """Refusals for a Start of *plan* (the coordinator's ``current_plan()``): ``{drone_id: reason}``.

        A start is refused when the settings could not be loaded, when a
        planned slot is above the ceiling limit, or when the settings changed
        since Plan so that a planned slot is no longer the drone's slot.
        """
        with self._lock:
            settings, load_error = self.settings, self.load_error
        if load_error:
            return {"fleet": f"ceiling settings could not be loaded ({load_error}); save them again"}
        refusals: dict[str, str] = {}
        zones = (plan or {}).get("zones") or []
        for index, zone in enumerate(zones):
            drone_id = str(zone.get("drone_id"))
            alt = _number(zone.get("alt_cm"))
            if alt is None:
                continue
            problem = settings.slot_problem(drone_id, alt)
            if problem is not None:
                refusals[drone_id] = problem
                self._log.error(
                    "slot_refused_above_ceiling",
                    drone_id=drone_id,
                    source="start",
                    alt_slot_cm=alt,
                    ceiling_cm=settings.ceiling_cm,
                    ceiling_margin_cm=settings.margin_cm,
                    max_alt_slot_cm=settings.max_slot_cm,
                )
            elif alt != self.slot_for(drone_id, index, settings):
                refusals[drone_id] = (
                    f"{drone_id} was planned at {format_cm(alt)} cm but its slot is now "
                    f"{self.slot_for(drone_id, index, settings)} cm; press Plan again"
                )
        return refusals

    # -- Operator changes -------------------------------------------------------

    def standby(self) -> bool:
        """True on a standby ground, whose ceiling commands the edges would ignore."""
        try:
            identity = getattr(self._transport, "identity", None)
        except Exception:  # noqa: BLE001 - an unreadable role does not hide the controls
            return False
        return getattr(identity, "role", None) == "standby"

    def locked_reason(self) -> str | None:
        """Why the settings cannot change now, or None."""
        if self.standby():
            return STANDBY_NOTE
        activity = self._activity
        if activity is None:
            return None
        try:
            reason = activity()
        except Exception as exc:  # noqa: BLE001 - an unknown flight state locks the settings
            return f"the fleet state is unknown ({exc!r})"
        if reason:
            return (
                "the ceiling and the altitude slots change only while every drone is on the ground and no "
                f"start is scheduled ({reason})"
            )
        return None

    def update(self, data: Any) -> str | None:
        """Apply the operator's change; returns why it was refused, or None once saved and applied.

        *data* may hold ``ceiling_cm``, ``margin_cm`` and ``slots_cm``
        (``{drone_id: cm}``; an empty value or None clears that drone's own
        slot). Anything not given keeps its current value. Nothing changes
        unless every value is valid and the file was written.
        """
        if not isinstance(data, Mapping):
            return "Ceiling settings must be an object."
        locked = self.locked_reason()
        if locked:
            return f"Not changed: {locked}."
        raw_slots = data.get("slots_cm") or {}
        if not isinstance(raw_slots, Mapping):
            return "Not changed: slots_cm must be an object."
        with self._lock:
            current = self.settings
        try:
            ceiling = _whole_cm("ceiling height", data["ceiling_cm"]) if "ceiling_cm" in data else current.ceiling_cm
            margin = _whole_cm("margin", data["margin_cm"]) if "margin_cm" in data else current.margin_cm
            slots = dict(current.slots_cm)
            for drone_id, value in raw_slots.items():
                if not fp.is_identifier(drone_id):
                    raise ValueError(f"{drone_id!r} is not a drone id")
                if value is None or (isinstance(value, str) and not value.strip()):
                    slots.pop(drone_id, None)
                else:
                    slots[drone_id] = _whole_cm(f"{drone_id} slot", value)
            new = CeilingSettings(ceiling, margin, slots)
            new.validate()
            # The default slots of the fleet's zones must fit too.
            problems = [
                problem for i, drone_id in enumerate(self._fleet_drones)
                if (problem := new.slot_problem(drone_id, self.slot_for(drone_id, i, new))) is not None
            ]
            if problems:
                raise ValueError("; ".join(problems))
        except ValueError as exc:
            message = str(exc)
            if "above the ceiling limit" in message:
                self._log.warning("slot_refused_above_ceiling", source="settings", detail=message[:300])
            return f"Not changed: {message}."
        try:
            self._store.save(new)
        except OSError as exc:
            self._log.error("ceiling_settings_save_failed", error=repr(exc), path=str(self._store.path))
            return f"Not changed: the settings could not be saved to {self._store.path} ({exc})."
        with self._lock:
            self.settings, self.load_error = new, None
        if (current.ceiling_cm, current.margin_cm) != (new.ceiling_cm, new.margin_cm):
            self._log.warning(
                "ceiling_changed",
                drone_ids=list(self._fleet_drones) or None,
                ceiling_cm=new.ceiling_cm,
                ceiling_margin_cm=new.margin_cm,
                previous_ceiling_cm=current.ceiling_cm,
                previous_ceiling_margin_cm=current.margin_cm,
                source="dashboard",
            )
        for drone_id in sorted(set(current.slots_cm) | set(new.slots_cm)):
            if current.slots_cm.get(drone_id) != new.slots_cm.get(drone_id):
                self._log.warning(
                    "slot_changed",
                    drone_id=drone_id,
                    alt_slot_cm=new.slots_cm.get(drone_id),
                    previous_alt_slot_cm=current.slots_cm.get(drone_id),
                    source="dashboard",
                )
        for drone_id in self._drone_ids():
            self.sync(drone_id, reason="settings")
        self.publish()
        return None

    # -- What the edges report ----------------------------------------------------

    def observe_control(self, msg: Mapping[str, Any]) -> bool:
        """Note an edge's telemetry or its answer to ``set_ceiling``.

        Returns True when *msg* was a ``set_ceiling`` reply (nothing else
        needs it); telemetry is only read and returns False.
        """
        drone_id = msg.get("drone_id")
        if not fp.is_identifier(drone_id):
            return False
        if msg.get("type") == fp.TELEMETRY:
            self._observe_telemetry(drone_id, msg)
            return False
        if not fp.is_ceiling_reply(msg):
            return False
        with self._lock:
            edge = self._edge(drone_id)
            if edge is None:
                return True
            if msg.get("cmd_id") == edge.pending_cmd:
                edge.pending_cmd = None
            edge.supported = True
            edge.note = None
            self._note_reported_locked(edge, msg)
            if msg.get("type") == fp.ACK:
                edge.refusal_reason = edge.refusal_detail = None
            else:
                edge.refusal_reason = str(msg.get("reason"))
                detail = msg.get("detail")
                edge.refusal_detail = detail if isinstance(detail, str) else None
        if msg.get("type") == fp.NACK:
            self._log.warning(
                "ceiling_refused_by_edge", drone_id=drone_id, reason=msg.get("reason"), detail=msg.get("detail")
            )
        self.publish()
        return True

    def _observe_telemetry(self, drone_id: str, msg: Mapping[str, Any]) -> None:
        flying = msg.get("is_flying") if isinstance(msg.get("is_flying"), bool) else None
        with self._lock:
            edge = self._edge(drone_id)
            if edge is None:
                return
            now = self._clock()
            reconnected = edge.last_seen is not None and now - edge.last_seen >= self._reconnect_gap_s
            edge.last_seen = now
            landed = edge.flying is True and flying is False
            before = (edge.supported, edge.ceiling_cm, edge.margin_cm, edge.slot_cm, edge.flying)
            appeared = False
            if _number(msg.get("ceiling_cm")) is not None:
                appeared = not edge.supported
                edge.supported = True
                self._note_reported_locked(edge, msg)
            edge.flying = flying
            changed = before != (edge.supported, edge.ceiling_cm, edge.margin_cm, edge.slot_cm, edge.flying)
        if changed:
            self.publish()
        if appeared or reconnected or landed:
            self.sync(drone_id, reason="appeared" if appeared else "reconnected" if reconnected else "landed")

    @staticmethod
    def _note_reported_locked(edge: _EdgeCeiling, msg: Mapping[str, Any]) -> None:
        for key, attr in (("ceiling_cm", "ceiling_cm"), ("ceiling_margin_cm", "margin_cm"), ("alt_slot_cm", "slot_cm")):
            value = _number(msg.get(key))
            if value is not None:
                setattr(edge, attr, value)

    @staticmethod
    def _ceiling_matches_locked(edge: _EdgeCeiling, settings: CeilingSettings) -> bool:
        return edge.ceiling_cm == settings.ceiling_cm and edge.margin_cm == settings.margin_cm

    # -- Sending ----------------------------------------------------------------

    def _desired(self, drone_id: str, ids: list[str], settings: CeilingSettings) -> tuple[int, int, int]:
        index = ids.index(drone_id) if drone_id in ids else 0
        return settings.ceiling_cm, settings.margin_cm, self.slot_for(drone_id, index, settings)

    def sync(self, drone_id: str, *, reason: str) -> bool:
        """Send *drone_id* the dashboard's ceiling, margin and slot when it reports others. True if sent.

        Only to an edge that reports its ceiling (it understands
        ``set_ceiling``), that is on the ground by its own last report, and
        that has no command of ours pending, and never from a standby ground.
        """
        if self._transport is None or self.standby():
            return False
        ids = self._drone_ids()
        with self._lock:
            settings, load_error = self.settings, self.load_error
            edge = self._edges.get(drone_id)
            if load_error or edge is None or not edge.supported or edge.pending_cmd is not None:
                return False
            if edge.flying is not False:
                return False
            desired = self._desired(drone_id, ids, settings)
            if (edge.ceiling_cm, edge.margin_cm, edge.slot_cm) == desired:
                return False
            fence = self._fence()
            msg = fp.make_set_ceiling(*desired, drone_id=drone_id, fence=fence)
            edge.pending_cmd = msg["cmd_id"]
            edge.note = None
        log.info("ceiling settings sent to %s (%s): %s", drone_id, reason, desired)
        self.publish()
        self._spawn(self._send_and_wait, drone_id, msg)
        return True

    def _fence(self) -> tuple[int, str] | None:
        identity = getattr(self._transport, "identity", None)
        fence = getattr(identity, "fence", None)
        if fence is None:
            return None
        epoch, leader_id = getattr(fence, "epoch", None), getattr(fence, "leader_id", None)
        return (epoch, leader_id) if isinstance(epoch, int) and isinstance(leader_id, str) else None

    def _send_and_wait(self, drone_id: str, msg: dict[str, Any]) -> None:
        note = None
        reply = None
        try:
            reply = self._transport.send_command(
                drone_id, msg, timeout_s=self._reply_timeout_s, timeout=self._reply_timeout_s + 5.0,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the operator, never raised
            log.warning("ceiling command to %s failed: %r", drone_id, exc)
            note = "send_failed"
        if reply is not None:
            self.observe_control(reply)
            return
        with self._lock:
            edge = self._edges.get(drone_id)
            if edge is None or edge.pending_cmd != msg["cmd_id"]:
                return
            edge.pending_cmd = None
            edge.note = note or "no_reply"
        log.warning("ceiling command to %s: %s", drone_id, note or "no reply")
        self.publish()

    # -- Browser state ------------------------------------------------------------

    def _drone_ids(self) -> list[str]:
        with self._lock:
            reported = [d for d in self._edges if d not in self._fleet_drones]
        return list(self._fleet_drones) + sorted(reported)

    def payload(self) -> dict[str, Any]:
        """The ``ceiling_settings`` event: settings, limits, each drone's slot and edge state, and the lock."""
        locked = self.locked_reason()
        ids = self._drone_ids()
        with self._lock:
            settings, load_error = self.settings, self.load_error
            drones = []
            for index, drone_id in enumerate(ids):
                edge = self._edges.get(drone_id) or _EdgeCeiling(drone_id)
                slot = self.slot_for(drone_id, index, settings)
                desired = (settings.ceiling_cm, settings.margin_cm, slot)
                drones.append({
                    "drone_id": drone_id,
                    "slot_cm": slot,
                    "own_slot_cm": settings.slots_cm.get(drone_id),
                    "default_slot_cm": self._default_slots[index % len(self._default_slots)],
                    "problem": settings.slot_problem(drone_id, slot),
                    "edge": {
                        "supported": edge.supported,
                        "ceiling_cm": edge.ceiling_cm,
                        "margin_cm": edge.margin_cm,
                        "slot_cm": edge.slot_cm,
                        "flying": edge.flying,
                        "in_sync": edge.supported and (edge.ceiling_cm, edge.margin_cm, edge.slot_cm) == desired,
                        "pending": edge.pending_cmd is not None,
                        "refusal": (
                            {"reason": edge.refusal_reason, "detail": edge.refusal_detail}
                            if edge.refusal_reason is not None else None
                        ),
                        "note": edge.note,
                    },
                })
        return {
            "ceiling_cm": settings.ceiling_cm,
            "margin_cm": settings.margin_cm,
            "max_slot_cm": settings.max_slot_cm,
            "limits": {
                "min_ceiling_cm": MIN_CEILING_CM,
                "max_ceiling_cm": MAX_CEILING_CM,
                "min_margin_cm": MIN_CEILING_MARGIN_CM,
                "min_slot_cm": MIN_ALT_SLOT_CM,
                "max_slot_cm": MAX_ALT_SLOT_CM,
            },
            "drones": drones,
            "editable": locked is None,
            "locked_reason": locked,
            "load_error": load_error,
            "file": str(self._store.path),
        }

    def publish(self) -> None:
        if self._emit_fn is None:
            return
        try:
            self._emit_fn(EVENT, self.payload())
        except Exception:  # a browser update must never break the caller
            log.warning("ceiling_settings emit failed", exc_info=True)

    def _edge(self, drone_id: str) -> _EdgeCeiling | None:
        """The state for *drone_id*, created when there is room. Caller holds the lock (or is __init__)."""
        edge = self._edges.get(drone_id)
        if edge is None and fp.is_identifier(drone_id) and len(self._edges) < MAX_DRONES:
            edge = self._edges[drone_id] = _EdgeCeiling(drone_id)
        return edge
