"""Fences, command ids, epochs, ``hello`` messages, version negotiation and ``shape_for_peer``."""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from flightrisk.fleet_protocol.constants import (
    EDGE_ROLE,
    FAILSAFE_TYPES,
    HELLO,
    LEGACY_EDGE_TO_GROUND_TYPES,
    LEGACY_GROUND_TO_EDGE_TYPES,
    MAX_EPOCH,
    MIN_PROTOCOL_VERSION,
    PEER_TYPES,
    PROTOCOL_VERSION,
    V2_FIELDS,
    V2_ONLY_COMMAND_TYPES,
    _KNOWN_TYPES,
    _MAX_PROTO_FIELD,
)
from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    _is_id,
    _is_int,
    _short,
)
from flightrisk.fleet_protocol.validate import (
    _checked,
    _is_ground_hello,
    _message_type,
    validate_message,
)


# ---------------------------------------------------------------------------
# Fencing, command ids and version negotiation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fence:
    """A leader's claim: its ``epoch`` and its node id (``leader_id``)."""

    epoch: int
    leader_id: str

    def __post_init__(self) -> None:
        if not (_is_int(self.epoch) and 1 <= self.epoch <= MAX_EPOCH):
            raise ProtocolError(f"fence epoch must be an integer from 1 to {MAX_EPOCH}, got {_short(self.epoch)}")
        if not _is_id(self.leader_id):
            raise ProtocolError(f"fence leader_id must be an identifier, got {_short(self.leader_id)}")

    def fields(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "leader_id": self.leader_id}


def next_epoch(epoch: Any) -> int:
    """The epoch a new leader takes after *epoch*: ``epoch + 1``.

    A new leader computes ``next_epoch(max(stored, highest reported))``, and
    a handover offers ``next_epoch(current)``. Raises ``ProtocolError`` for
    an *epoch* that is not an integer from 0 to ``MAX_EPOCH``, and, with a
    message meant for the operator, when *epoch* is already ``MAX_EPOCH``
    and no successor epoch exists.
    """
    if not (_is_int(epoch) and 0 <= epoch <= MAX_EPOCH):
        raise ProtocolError(f"epoch must be an integer from 0 to {MAX_EPOCH}, got {_short(epoch)}")
    if epoch == MAX_EPOCH:
        raise ProtocolError(
            f"The fleet epoch has reached its maximum ({MAX_EPOCH}), so no ground station can take "
            "leadership after it. Land every drone, stop both ground stations and every edge, reset "
            "the stored leader epoch on both ground stations, then start again; epochs restart at 1."
        )
    return epoch + 1


def new_cmd_id() -> str:
    """A fresh command id (uuid4, 32 hex characters). Retries reuse the original."""
    return uuid.uuid4().hex


def fence_of(msg: Any) -> Fence | None:
    """The message's fence, or ``None`` when it has none or a malformed one.

    Never raises. An edge uses this on a ``land``, whose fence is optional
    and never a reason to refuse it.
    """
    if not isinstance(msg, Mapping):
        return None
    try:
        return Fence(msg.get("epoch"), msg.get("leader_id"))
    except ProtocolError:
        return None


def cmd_id_of(msg: Any) -> str | None:
    """The message's ``cmd_id`` when it is a well-formed id, else ``None``. Never raises."""
    if not isinstance(msg, Mapping):
        return None
    value = msg.get("cmd_id")
    return value if _is_id(value) else None


def negotiate_proto(
    peer_proto: Any,
    *,
    local_max: int = PROTOCOL_VERSION,
    local_min: int = MIN_PROTOCOL_VERSION,
    current: int | None = None,
) -> int:
    """The protocol version to use with a peer that announced *peer_proto*.

    The agreed version is ``min(local_max, peer_proto)``. *local_min* is the
    lowest version this node accepts: a v2-only deployment (multi-ground
    mode without ``FLIGHTRISK_FLEET_LEGACY=1``) passes 2, and then a peer
    that announces ``proto: 1`` raises ``ProtocolError`` and the receiver
    should close the connection. *current* is the version an earlier
    ``hello`` on the same connection agreed, if any: the version can never
    change on a live connection, so a repeated ``hello`` that would change
    it raises ``ProtocolError`` (the receiver should drop the connection).
    This blocks a downgrade from v2 to the unfenced v1 rules mid-connection.

    A peer that never sends ``hello`` never reaches this function; a node
    that requires v2 must treat such a link as unusable for motion (see
    ``validate_ground_to_edge(..., require_fence=True)``).
    """
    if not (_is_int(peer_proto) and MIN_PROTOCOL_VERSION <= peer_proto <= _MAX_PROTO_FIELD):
        raise ProtocolError(
            f"peer protocol version must be an integer from {MIN_PROTOCOL_VERSION} to "
            f"{_MAX_PROTO_FIELD}, got {_short(peer_proto)}"
        )
    if not (_is_int(local_max) and MIN_PROTOCOL_VERSION <= local_max <= _MAX_PROTO_FIELD):
        raise ProtocolError(f"local_max must be a protocol version, got {_short(local_max)}")
    if not (_is_int(local_min) and MIN_PROTOCOL_VERSION <= local_min <= local_max):
        raise ProtocolError(f"local_min must be a protocol version from 1 to local_max, got {_short(local_min)}")
    agreed = min(local_max, peer_proto)
    if agreed < local_min:
        raise ProtocolError(
            f"peer speaks protocol v{peer_proto}, but this node requires at least v{local_min}; "
            "close the connection (a v1 peer is accepted only in legacy mode, FLIGHTRISK_FLEET_LEGACY=1)"
        )
    if current is not None and agreed != current:
        raise ProtocolError(
            f"protocol version cannot change on a live connection (was v{current}, hello says v{agreed})"
        )
    return agreed


@dataclass(frozen=True)
class GroundHello:
    """A ground's ``hello``: who it is, its role and the epoch it holds or knows."""

    node_id: str
    role: str
    epoch: int
    proto: int = PROTOCOL_VERSION

    def to_message(self) -> dict[str, Any]:
        return _checked({
            "type": HELLO,
            "role": self.role,
            "node_id": self.node_id,
            "epoch": self.epoch,
            "proto": self.proto,
        })


@dataclass(frozen=True)
class EdgeHello:
    """An edge's ``hello``: its drone, the highest epoch it obeyed and its plan state."""

    drone_id: str
    node_id: str
    epoch_seen: int
    plan_id: str | None
    lane_index: int | None
    is_flying: bool
    battery: float | None
    proto: int = PROTOCOL_VERSION

    def to_message(self) -> dict[str, Any]:
        return _checked({
            "type": HELLO,
            "role": EDGE_ROLE,
            "drone_id": self.drone_id,
            "node_id": self.node_id,
            "proto": self.proto,
            "epoch_seen": self.epoch_seen,
            "plan_id": self.plan_id,
            "lane_index": self.lane_index,
            "is_flying": self.is_flying,
            "battery": self.battery,
        })


def parse_hello(msg: Any) -> GroundHello | EdgeHello:
    """Validate a ``hello`` and return it as a ``GroundHello`` or ``EdgeHello``."""
    if _message_type(msg) != HELLO:
        raise ProtocolError(f"expected a 'hello' message, got {_short(msg.get('type'))}")
    validate_message(msg)
    if _is_ground_hello(msg):
        return GroundHello(node_id=msg["node_id"], role=msg["role"], epoch=msg["epoch"], proto=msg["proto"])
    return EdgeHello(
        drone_id=msg["drone_id"],
        node_id=msg["node_id"],
        epoch_seen=msg["epoch_seen"],
        plan_id=msg["plan_id"],
        lane_index=msg["lane_index"],
        is_flying=msg["is_flying"],
        battery=msg["battery"],
        proto=msg["proto"],
    )


def shape_for_peer(msg: Mapping[str, Any], peer_proto: int) -> dict[str, Any]:
    """The form of *msg* to send to a peer speaking *peer_proto*.

    For a v2 peer the message is checked under v2 rules and returned as a
    copy. For a v1 peer:

    * ``land`` and ``emergency_land_all`` always go through (copied as is);
    * a ``hello`` goes through, since that is how the peer learns we speak v2;
    * v2-only commands and peer messages raise ``ProtocolError``, because a
      v1 edge would silently ignore them;
    * every other message loses the v2 fields (``V2_FIELDS``) and must then
      pass the v1 rules, so for example a nack with a v2-only reason raises.

    A v1 edge ignores ``on_complete`` and hovers at the end of its lanes.
    The legacy ``set_target``, ``stream_video`` and ``detections`` messages
    are copied unchanged; a type this protocol does not define raises.
    """
    mtype = _message_type(msg)
    if not (_is_int(peer_proto) and peer_proto >= MIN_PROTOCOL_VERSION):
        raise ProtocolError(f"peer protocol version must be an integer >= 1, got {_short(peer_proto)}")
    if mtype in LEGACY_GROUND_TO_EDGE_TYPES or mtype in LEGACY_EDGE_TO_GROUND_TYPES:
        return dict(msg)
    if mtype not in _KNOWN_TYPES and mtype not in FAILSAFE_TYPES:
        raise ProtocolError(f"{_short(mtype)} is not a fleet protocol message")
    if mtype in FAILSAFE_TYPES or peer_proto >= 2:
        validate_message(msg, proto=peer_proto)
        return dict(msg)
    if mtype == HELLO:
        validate_message(msg)
        return dict(msg)
    if mtype in V2_ONLY_COMMAND_TYPES or mtype in PEER_TYPES:
        raise ProtocolError(f"{mtype!r} cannot be sent to a protocol v1 peer")
    shaped = {key: value for key, value in msg.items() if key not in V2_FIELDS}
    validate_message(shaped, proto=1)
    return shaped
