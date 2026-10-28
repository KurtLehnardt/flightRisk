"""Ground-side bookkeeping for protocol v2 edge links.

``GroundTransport`` (``flightrisk/transport.py``) uses this module when it is
given a ``GroundIdentity``; without one it keeps the v1 behaviour unchanged.
It covers what the ground must do per ``docs/fleet-protocol-v2.md`` ("What
PR-4/5 consumers must do"), and nothing of the leader's policy:

* **Hello first.** The ground sends its ``hello`` as the first message on
  every connection (right after the auth handshake) and again whenever its
  role or epoch changes. Nothing but a landing goes to a connection before
  its ``hello``, so a heartbeat can never lock the edge to v1.
* **Version per connection.** The edge's ``hello`` agrees the version
  (``negotiate_proto``); with no edge ``hello`` within ``HELLO_TIMEOUT_S``
  the link is v1. A later edge ``hello`` on a v1 link, one that changes the
  agreed version, or one that changes the connection's ``node_id`` closes
  the connection. A ground that requires v2 (``local_min_proto=2``) closes v1
  links. Incoming acks, nacks and telemetry are validated with the agreed
  version, and outgoing messages are shaped for it (``shape_for_peer``).
* **Fenced leader traffic.** As a leader the ground fences its heartbeats
  and clock messages with its epoch and node id.
* **Ack correlation.** ``CommandTracker`` matches acks and nacks to commands
  by ``(drone_id, cmd_id)``; an ack for a landing without a ``cmd_id`` still
  completes the pending landings of that drone. Retries follow
  ``RETRYABLE_COMMAND_TYPES`` and the supersede rules.
* **Stale leader signal.** A ``stale_epoch`` or ``not_authority`` nack, or an
  edge ``hello``, reporting an ``epoch_seen`` above the ground's epoch is
  passed to ``on_stale_epoch`` so the leader logic (PR-6) can step down.

``hold`` (and the other v2-only commands) to a v1 edge cannot be shaped:
``shape`` raises ``PeerVersionError``. The leader must then fall back, for
example by landing that drone or reporting it as unable to hold; that
policy belongs to the leader (PR-6/PR-8), not to the transport.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed

from flightrisk import fleet_protocol as fp

# The mixin logs as the transport always did.
logger = logging.getLogger("flightrisk.transport")

__all__ = [
    "CommandTracker",
    "GroundIdentity",
    "GroundLinks",
    "GroundV2Mixin",
    "LinkVerdict",
    "PeerVersionError",
]

# Close code for a connection refused for a protocol reason (version or
# node_id change, or a v1 link where v2 is required).
CLOSE_PROTOCOL = 4010

_STALE_REASONS = frozenset({"stale_epoch", "not_authority"})


class PeerVersionError(fp.ProtocolError):
    """The message cannot be sent on this link's protocol version (for example ``hold`` to a v1 edge)."""


@dataclass(frozen=True)
class GroundIdentity:
    """Who this ground is: its node id, role (``leader`` or ``standby``) and epoch."""

    node_id: str
    role: str
    epoch: int

    def __post_init__(self) -> None:
        # Validates node id, role and the epoch range (a leader's is at least 1).
        fp.make_ground_hello(self.node_id, self.role, self.epoch)

    @property
    def fence(self) -> fp.Fence | None:
        """The fence a leader stamps on its messages; ``None`` for a standby."""
        return fp.Fence(self.epoch, self.node_id) if self.role == "leader" else None

    def hello(self) -> dict[str, Any]:
        return fp.make_ground_hello(self.node_id, self.role, self.epoch)


@dataclass(frozen=True)
class LinkVerdict:
    close: bool = False
    reason: str = ""
    epoch_seen: int | None = None


@dataclass
class _Link:
    opened_at: float
    hello_sent: bool = False
    proto: int | None = None
    v1: bool = False
    node_id: str | None = None
    drone_id: str | None = None


class GroundLinks:
    """Per-connection v2 state on the ground. Touched only on the transport's event loop."""

    def __init__(self, identity: GroundIdentity, *, local_min_proto: int = 1, hello_timeout_s: float = fp.HELLO_TIMEOUT_S):
        if local_min_proto not in (1, 2):
            raise ValueError(f"local_min_proto must be 1 or 2, got {local_min_proto!r}")
        self.identity = identity
        self.local_min_proto = local_min_proto
        self.hello_timeout_s = hello_timeout_s
        self._links: dict[Hashable, _Link] = {}

    def opened(self, ws: Hashable, now: float) -> None:
        self._links[ws] = _Link(opened_at=now)

    def forget(self, ws: Hashable) -> None:
        self._links.pop(ws, None)

    def mark_hello_sent(self, ws: Hashable) -> None:
        link = self._links.get(ws)
        if link is not None:
            link.hello_sent = True

    def hello_sent(self, ws: Hashable) -> bool:
        link = self._links.get(ws)
        return link is not None and link.hello_sent

    def proto(self, ws: Hashable, now: float) -> int:
        """The version agreed on *ws*: the edge hello's, or 1 (v1 after the hello timeout)."""
        link = self._links.get(ws)
        if link is None:
            return 1
        if link.proto is None and not link.v1 and now - link.opened_at >= self.hello_timeout_s:
            link.v1 = True
        return link.proto if link.proto is not None else 1

    def refuses(self, ws: Hashable, now: float) -> bool:
        """Whether *ws* is a v1 link that this ground (requiring v2) must close."""
        self.proto(ws, now)
        link = self._links.get(ws)
        return link is not None and link.v1 and self.local_min_proto >= 2

    def on_edge_hello(self, ws: Hashable, msg: Mapping[str, Any], now: float) -> LinkVerdict:
        """Process an edge ``hello`` on *ws*."""
        link = self._links.get(ws)
        if link is None:
            return LinkVerdict(close=True, reason="unknown_connection")
        try:
            hello = fp.parse_hello(msg)
        except fp.ProtocolError:
            return LinkVerdict(reason="invalid_hello")
        if not isinstance(hello, fp.EdgeHello):
            return LinkVerdict(reason="ground_hello_from_edge")
        self.proto(ws, now)
        if link.v1:
            return LinkVerdict(close=True, reason="late_hello")
        if link.node_id is not None and hello.node_id != link.node_id:
            return LinkVerdict(close=True, reason="node_id_changed")
        try:
            agreed = fp.negotiate_proto(hello.proto, local_min=self.local_min_proto, current=link.proto)
        except fp.ProtocolError:
            return LinkVerdict(close=True, reason="protocol_refused")
        link.proto, link.node_id, link.drone_id = agreed, hello.node_id, hello.drone_id
        return LinkVerdict(epoch_seen=hello.epoch_seen)

    def may_send(self, ws: Hashable, msg: Mapping[str, Any]) -> bool:
        """Only a landing may go to a connection before its ``hello`` was sent."""
        mtype = msg.get("type")
        return mtype in fp.FAILSAFE_TYPES or self.hello_sent(ws)

    def shape(self, ws: Hashable, msg: Mapping[str, Any], now: float) -> dict[str, Any]:
        """*msg* shaped for the link's version; raises ``PeerVersionError`` when it cannot be.

        While the version is still being agreed (hello sent, no edge hello
        yet, timeout not reached) a message keeps its v2 fields, which a v1
        edge ignores, so a fence or ``cmd_id`` is never stripped from a v2
        edge that has not answered yet; v2-only commands wait for the answer.
        """
        proto = self.proto(ws, now)
        link = self._links.get(ws)
        if link is not None and link.proto is None and not link.v1:
            if msg.get("type") in fp.V2_ONLY_COMMAND_TYPES:
                raise PeerVersionError(f"{msg.get('type')!r} waits until the edge has agreed protocol v2")
            proto = fp.PROTOCOL_VERSION
        try:
            return fp.shape_for_peer(msg, proto)
        except fp.ProtocolError as exc:
            raise PeerVersionError(str(exc)) from exc

    def peer_proto(self, ws: Hashable) -> int | None:
        link = self._links.get(ws)
        return None if link is None else link.proto

    def stale_epoch_in(self, msg: Mapping[str, Any]) -> int | None:
        """The ``epoch_seen`` of a stale-leader nack that is above this ground's epoch, else ``None``."""
        if msg.get("type") != fp.NACK or msg.get("reason") not in _STALE_REASONS:
            return None
        seen = msg.get("epoch_seen")
        if isinstance(seen, int) and not isinstance(seen, bool) and seen > self.identity.epoch:
            return seen
        return None


@dataclass
class _Pending:
    drone_id: str
    mtype: str
    cmd_id: str | None
    future: asyncio.Future
    superseded: bool = field(default=False)


class CommandTracker:
    """Matches edge replies to commands by ``(drone_id, cmd_id)``. Event-loop only."""

    def __init__(self) -> None:
        self._pending: list[_Pending] = []

    def register(self, drone_id: str, msg: Mapping[str, Any]) -> _Pending:
        loop = asyncio.get_running_loop()
        entry = _Pending(drone_id, str(msg.get("type")), fp.cmd_id_of(msg), loop.create_future())
        self._pending.append(entry)
        return entry

    def unregister(self, entry: _Pending) -> None:
        if entry in self._pending:
            self._pending.remove(entry)

    def resolve(self, reply: Mapping[str, Any]) -> int:
        """Complete the commands *reply* answers; returns how many."""
        if reply.get("type") not in (fp.ACK, fp.NACK):
            return 0
        drone_id, target, cmd_id = reply.get("drone_id"), reply.get("for"), reply.get("cmd_id")
        done = 0
        for entry in list(self._pending):
            if entry.drone_id != drone_id or entry.future.done():
                continue
            if cmd_id is not None:
                match = entry.cmd_id == cmd_id
            else:
                # A landing ack without a cmd_id (a v1 edge, or a landing read
                # by the fail-safe decoder) still completes that drone's
                # pending landings.
                match = (
                    reply.get("type") == fp.ACK
                    and target in fp.FAILSAFE_TYPES
                    and entry.mtype in fp.FAILSAFE_TYPES
                )
            if match:
                entry.future.set_result(dict(reply))
                done += 1
        return done

    def supersede(self, drone_id: str | None, mtype: str) -> int:
        """Stop retries a newer command makes obsolete; returns how many.

        A newer ``hold``, ``assign_lanes`` or ``land`` for a drone ends the
        pending ``assign_lanes`` retries for that drone; an
        ``emergency_land_all`` ends every pending motion retry for every
        drone.
        """
        stopped = 0
        for entry in list(self._pending):
            if entry.future.done():
                continue
            if mtype == fp.EMERGENCY_LAND_ALL:
                hit = entry.mtype in fp.FENCED_COMMAND_TYPES
            elif mtype in (fp.HOLD, fp.ASSIGN_LANES, fp.LAND):
                hit = entry.drone_id == drone_id and entry.mtype == fp.ASSIGN_LANES
            else:
                hit = False
            if hit:
                entry.superseded = True
                entry.future.set_result(None)
                stopped += 1
        return stopped


class GroundV2Mixin:
    """``GroundTransport``'s protocol v2 methods: the ground ``hello``, the edge's answer, identity and commands.

    Mixed into ``GroundTransport`` (``flightrisk/transport.py``), whose state
    these methods use (``_links``, ``_commands``, ``_clients``,
    ``_drone_sockets``, ``_send_json``, ``_claim_drone``, ``send_to_drone``
    and the rest). They live here to keep the transport module smaller.
    """

    @property
    def identity(self) -> GroundIdentity | None:
        return self._links.identity if self._links is not None else None

    async def _open_v2_link(self, websocket) -> bool:
        """Send this ground's ``hello`` as the first message on a new connection."""
        self._links.opened(websocket, self._clock())
        identity = self._links.identity
        if not await self._send_json(websocket, json.dumps(identity.hello())):
            return False
        self._links.mark_hello_sent(websocket)
        if self._links.identity is not identity:
            # set_identity ran while this hello was being written and skipped
            # this connection: announce the current role and epoch too.
            return await self._send_json(websocket, json.dumps(self._links.identity.hello()))
        return True

    async def _on_edge_hello(self, websocket, data: dict[str, Any], received_at: float) -> bool:
        verdict = self._links.on_edge_hello(websocket, data, received_at)
        if verdict.close:
            await self._close_protocol(websocket, verdict.reason)
            return False
        if verdict.reason:
            logger.warning("Ignoring an edge hello: %s", verdict.reason)
            return True
        if not await self._claim_drone(websocket, data.get("drone_id"), received_at):
            return False
        if verdict.epoch_seen is not None and verdict.epoch_seen > self._links.identity.epoch:
            self._note_stale(verdict.epoch_seen)
        return True

    async def _close_protocol(self, websocket, reason: str) -> None:
        logger.warning("Closing %s: %s", self._describe(websocket), reason)
        try:
            await websocket.close(code=CLOSE_PROTOCOL, reason=reason)
        except ConnectionClosed:
            pass

    def _note_stale(self, epoch_seen: int | None) -> None:
        # Only a leader can be stale; a standby is expected to be behind.
        if epoch_seen is None or self._links.identity.role != "leader":
            return
        logger.warning(
            "An edge follows leader epoch %d, above this ground's epoch %d",
            epoch_seen, self._links.identity.epoch,
        )
        if self._on_stale_epoch is not None:
            try:
                self._on_stale_epoch(epoch_seen)
            except Exception:
                logger.exception("on_stale_epoch callback raised")

    async def set_identity(self, identity: GroundIdentity) -> int:
        """Change this ground's role or epoch and announce it with a new ``hello``.

        Only for a ground started with an ``identity``. Returns how many
        connections the new ``hello`` reached.
        """
        if self._links is None:
            raise RuntimeError("set_identity needs a ground started with an identity")
        if identity.node_id != self._links.identity.node_id:
            raise ValueError("a ground's node_id cannot change while it runs")
        self._links.identity = identity
        payload = json.dumps(identity.hello())
        clients = [ws for ws in self._clients if self._links.hello_sent(ws)]
        results = await asyncio.gather(*(self._send_json(ws, payload) for ws in clients), return_exceptions=True)
        return sum(1 for result in results if result is True)

    def _payload_for(self, websocket, message: dict[str, Any]) -> str | None:
        """The JSON to send *message* on *websocket*, or ``None`` when it must not go there yet.

        Raises ``PeerVersionError`` when the link's version cannot carry it.
        """
        if self._links is None:
            return json.dumps(message)
        if not self._links.may_send(websocket, message):
            return None
        return json.dumps(self._links.shape(websocket, message, self._clock()))

    def peer_proto(self, drone_id: str) -> int | None:
        """The protocol version agreed with *drone_id*'s edge (``None`` when not yet agreed or unknown)."""
        websocket = self._drone_sockets.get(drone_id)
        if websocket is None or self._links is None:
            return None
        return self._links.peer_proto(websocket)

    async def send_command(
        self,
        drone_id: str,
        message: dict[str, Any],
        *,
        timeout_s: float = 2.0,
        attempts: int = 1,
    ) -> dict[str, Any] | None:
        """Send a command to *drone_id* and wait for its ack or nack, matched by ``(drone_id, cmd_id)``.

        Only ``RETRYABLE_COMMAND_TYPES`` are sent more than once (up to
        *attempts* times, with the same ``cmd_id``); every other command is
        sent once. A newer command that supersedes this one (see
        ``CommandTracker.supersede``) ends the wait. Returns the reply, or
        ``None`` on timeout, supersession or when the drone is not
        connected. Raises ``PeerVersionError`` when the drone's link cannot
        carry the command (for example ``hold`` to a v1 edge): the caller
        decides the fallback.
        """
        mtype = fp.validate_message(message, proto=self._validation_proto(message))
        self._commands.supersede(drone_id, mtype)
        tries = max(1, attempts) if mtype in fp.RETRYABLE_COMMAND_TYPES else 1
        entry = self._commands.register(drone_id, message)
        try:
            for _ in range(tries):
                if not await self.send_to_drone(drone_id, message):
                    return None
                try:
                    return await asyncio.wait_for(asyncio.shield(entry.future), timeout=timeout_s)
                except (asyncio.TimeoutError, TimeoutError):
                    continue
            return None
        finally:
            self._commands.unregister(entry)
