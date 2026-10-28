"""The ``set_ceiling`` command on the edge: ceiling, margin and altitude slot, on the ground only.

``LaneExecutor`` (``flightrisk.drone.lane_executor``) inherits
``CeilingCommandMixin``. ``handle_set_ceiling`` applies a ground's
``set_ceiling`` (``flightrisk/fleet_protocol/ceiling.py``) to the drone's
``SafetyEnvelope`` and answers with an ``ack`` or ``nack``:

* It never moves the drone, so it is never queued behind a flight sequence
  and never lands anything. It is refused with ``in_flight`` while the drone
  flies, or while a takeoff, a scheduled start, lanes, a climb, a nudge or a
  landing is under way. The check and the change happen under the executor
  lock, so no flight sequence can start in between.
* The envelope checks the three heights together
  (``SafetyEnvelope.configure_ceiling``): a slot above the new ceiling minus
  margin is refused with ``above_ceiling``, any other bad value with
  ``invalid``. On a refusal nothing changes; the nack reports the values
  still in force.
* Like the fenced commands, it is executed at most once per ``cmd_id`` (a
  duplicate gets the first reply again) and only when the authority accepts
  it: the single-ground default accepts everything, the multi-ground edge
  authority only its current leader's fence.
* Landing never passes through here and is never affected by it.

The ``ceiling_changed``, ``slot_changed`` and ``slot_refused_above_ceiling``
events record each change and refusal with the drone id.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flightrisk.drone.command_cache import DONE, MISMATCH, NEW
from flightrisk.drone.safety import CeilingError
from flightrisk.fleet_protocol import (
    SET_CEILING,
    ProtocolError,
    is_identifier,
    make_ceiling_ack,
    make_ceiling_nack,
)
from flightrisk.fleet_protocol.ceiling import validate_set_ceiling

__all__ = ["CeilingCommandMixin"]

# The protocol caps a nack's detail at 200 printable characters.
_MAX_DETAIL = 200


def _detail(text: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text).strip() or "refused"
    return cleaned[:_MAX_DETAIL]


class CeilingCommandMixin:
    """``set_ceiling`` handling for ``LaneExecutor`` (see the module docstring)."""

    def ceiling_status(self) -> dict[str, float]:
        """The active ``ceiling_cm``, ``ceiling_margin_cm`` and ``alt_slot_cm``."""
        return self.envelope.ceiling_status()

    def handle_set_ceiling(self, msg: Mapping[str, Any]) -> bool:
        """Apply a ``set_ceiling`` on the ground; answer it. True when it was applied.

        Never raises for a malformed message: one without a usable
        ``cmd_id`` is logged and dropped (it cannot be answered), any other
        malformed one is refused with ``invalid``.
        """
        cmd_id = msg.get("cmd_id")
        if not is_identifier(cmd_id):
            self._log.warning("edge_set_ceiling_invalid", drone_id=self.drone_id, error="missing or invalid cmd_id")
            return False
        status, cached = self._commands.claim(cmd_id, SET_CEILING)
        if status == MISMATCH:
            self._log.warning("edge_command_id_reused", drone_id=self.drone_id, type=SET_CEILING, cmd_id=cmd_id)
            self._send_ceiling_reply(None, self._ceiling_nack(cmd_id, "invalid", "cmd_id_reused"))
            return False
        if status != NEW:
            if status == DONE and cached is not None:
                self._emit(cached)
            return False
        try:
            validate_set_ceiling(msg)
        except ProtocolError as exc:
            return self._refuse_ceiling(msg, "invalid", str(exc))
        target = msg.get("drone_id")
        if target is not None and target != self.drone_id:
            return self._refuse_ceiling(msg, "invalid", f"addressed to {target}, this is {self.drone_id}")
        verdict = self._authority_verdict(msg)
        if verdict is not None:
            return self._refuse_ceiling(msg, verdict, "not from the ground this edge obeys")

        before = self.envelope.ceiling_status()
        ceiling, margin, slot = msg["ceiling_cm"], msg["ceiling_margin_cm"], msg["alt_slot_cm"]
        refusal: tuple[str, str] | None = None
        with self._lock:
            # Checked and applied under the executor lock: every flight
            # sequence is launched under it, so none can start in between.
            try:
                flying = self._is_flying()
            except Exception:  # noqa: BLE001 - an unreadable flight state counts as flying
                flying = True
            busy = self._worker_alive() or self._land_inflight or self._land_requests > 0
            if flying or busy:
                refusal = ("in_flight", "ceiling and altitude slot change only on the ground")
            else:
                try:
                    self.envelope.configure_ceiling(ceiling, margin, slot)
                except CeilingError as exc:
                    refusal = ("above_ceiling", str(exc))
                except ValueError as exc:
                    refusal = ("invalid", str(exc))
        if refusal is not None:
            if refusal[0] == "above_ceiling":
                self._log.error(
                    "slot_refused_above_ceiling",
                    drone_id=self.drone_id,
                    source="set_ceiling",
                    alt_slot_cm=slot,
                    ceiling_cm=ceiling,
                    ceiling_margin_cm=margin,
                )
            return self._refuse_ceiling(msg, *refusal)

        after = self.envelope.ceiling_status()
        if (before["ceiling_cm"], before["ceiling_margin_cm"]) != (after["ceiling_cm"], after["ceiling_margin_cm"]):
            self._log.warning(
                "ceiling_changed",
                drone_id=self.drone_id,
                ceiling_cm=after["ceiling_cm"],
                ceiling_margin_cm=after["ceiling_margin_cm"],
                previous_ceiling_cm=before["ceiling_cm"],
                previous_ceiling_margin_cm=before["ceiling_margin_cm"],
            )
        if before["alt_slot_cm"] != after["alt_slot_cm"]:
            self._log.warning(
                "slot_changed",
                drone_id=self.drone_id,
                alt_slot_cm=after["alt_slot_cm"],
                previous_alt_slot_cm=before["alt_slot_cm"],
                source="set_ceiling",
            )
        self._send_ceiling_reply(cmd_id, make_ceiling_ack(self.drone_id, cmd_id, after))
        return True

    def _refuse_ceiling(self, msg: Mapping[str, Any], reason: str, detail: str) -> bool:
        self._log.warning("edge_set_ceiling_refused", drone_id=self.drone_id, reason=reason, detail=_detail(detail))
        self._send_ceiling_reply(msg["cmd_id"], self._ceiling_nack(msg["cmd_id"], reason, detail))
        return False

    def _ceiling_nack(self, cmd_id: str, reason: str, detail: str) -> dict[str, Any] | None:
        try:
            return make_ceiling_nack(
                self.drone_id,
                cmd_id,
                reason,
                active=self.envelope.ceiling_status(),
                detail=_detail(detail),
                epoch_seen=self.epoch_seen if reason in ("stale_epoch", "not_authority") else None,
            )
        except ProtocolError as exc:
            self._log.error("edge_reply_invalid", drone_id=self.drone_id, type=SET_CEILING, error=str(exc)[:120])
            return None

    def _send_ceiling_reply(self, cmd_id: str | None, reply: dict[str, Any] | None) -> None:
        """Send *reply*; with *cmd_id*, record it first as that command's final outcome."""
        if cmd_id is not None and not self._commands.finish(cmd_id, SET_CEILING, reply):
            return
        if reply is not None:
            self._emit(reply)
