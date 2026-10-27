"""Fleet control protocol shared by the ground station and the edge devices.

This module is the single source of truth for the JSON control messages that
travel over the existing edge/ground WebSocket (see ``flightrisk/transport.py``)
when several drones fly as one coordinated fleet. It is deliberately pure: it
imports no networking code, so the fleet coordinator, the edge device and the
transport can all share it, and it can be unit-tested without sockets.

Protocol v1 (the hackathon flow) is unchanged and still the default. Protocol
v2 adds leader epochs and fencing, command ids, plan ids, a version handshake
(``hello``), new commands (``hold``, ``resume``, ``nudge``, ``handover``),
generic acks and nacks, and the ground-to-ground messages used for leadership
handover. ``docs/fleet-protocol-v2.md`` is the full reference: the message
table, the fencing rules, version negotiation, v1 compatibility, ``cmd_id``
idempotency and the size caps.

Ground -> edge messages (v1)::

    {"type": "ping", "t1": <ground_monotonic_s>}
    {"type": "clock", "offset_s": <median offset, edge_clock - ground_clock>}
    {"type": "assign_lanes", "drone_id": .., "alt_cm": 100,
     "lanes": [{"direction": "forward", "distance_cm": 200}, ...],
     "zone": {"x_cm": 0, "y_cm": 0, "w_cm": 400, "h_cm": 400}}
    {"type": "start_at", "t_ground_s": <ground_monotonic_s>}
    {"type": "takeoff"}
    {"type": "land"}
    {"type": "emergency_land_all"}
    {"type": "heartbeat", "seq": n}

Edge -> ground messages (v1)::

    {"type": "pong", "t1": .., "t2": <edge_recv>, "t3": <edge_send>}
    {"type": "telemetry", "drone_id": .., "battery": .., "height_cm": ..,
     "is_flying": .., "rel_x_cm": .., "rel_y_cm": .., "heading_deg": ..,
     "lane_index": .., "lanes_total": .., "link_age_s": ..,
     "takeoff_at_local_s": ..}
    {"type": "ack", "for": "start_at", "drone_id": ..,
     "executed_at_ground_s": <t_edge_exec - offset_s>}
    {"type": "ack", "for": "assign_lanes", "drone_id": .., "battery": <percent>,
     "lanes_total": <n>}
    {"type": "nack", "for": "assign_lanes", "drone_id": ..,
     "reason": "geofence|altitude|battery|other_drone|invalid",
     "lanes_total": <n received>}

Protocol v2 additions (every v1 field is kept)::

    ground -> edge
    {"type": "hello", "role": "leader|standby", "node_id": "mac", "epoch": 7, "proto": 2}
    {"type": "heartbeat", "seq": 12, "epoch": 7, "leader_id": "mac"}
    {"type": "assign_lanes", ..v1 fields.., "plan_id": "p-1", "plan_seq": 3,
     "on_complete": "hover|land", "hover_timeout_s": 20, "epoch": 7, "leader_id": "mac", "cmd_id": ".."}
    {"type": "start_at", "t_ground_s": .., "plan_id": "p-1", "epoch": 7, "leader_id": "mac", "cmd_id": ".."}
    {"type": "takeoff", "epoch": 7, "leader_id": "mac", "cmd_id": ".."}
    {"type": "hold", "epoch": 7, "leader_id": "mac", "cmd_id": "..", "reason": "alert|operator|handover"}
    {"type": "resume", "plan_id": "p-1", "epoch": 7, "leader_id": "mac", "cmd_id": ".."}
    {"type": "nudge", "direction": "left", "distance_cm": 50, "epoch": 7, "leader_id": "mac", "cmd_id": ".."}
    {"type": "handover", "to_node": "win", "epoch_next": 8, "epoch": 7, "leader_id": "mac", "cmd_id": ".."}
    {"type": "land", "cmd_id": ".."}            (epoch optional, never required)
    {"type": "emergency_land_all"}              (unchanged)

    edge -> ground
    {"type": "hello", "role": "edge", "drone_id": "drone-2", "node_id": "win", "proto": 2,
     "epoch_seen": 7, "plan_id": "p-1", "lane_index": 3, "is_flying": false, "battery": 71}
    {"type": "telemetry", ..v1 fields.., "epoch": 7, "plan_id": "p-1",
     "state": "idle|taking_off|hovering|flying_lanes|holding|landing", "plan_valid": true}
    {"type": "ack", "for": "hold", "cmd_id": "..", "drone_id": "drone-2", "result": "ok"}
    {"type": "nack", "for": "resume", "cmd_id": "..", "drone_id": "drone-2", "reason": "no_valid_plan"}

    ground -> ground (peer link)
    {"type": "node_state", ..}, {"type": "handover_offer", ..}, {"type": "handover_accept", ..},
    {"type": "handover_reject", ..}, {"type": "peer_heartbeat", ..}

Fail-safe rule: ``land`` and ``emergency_land_all`` are never fenced and never
refused for a malformed optional field, under any protocol version. Fencing
applies to anything that can move a drone, never to landing. Concretely:

* ``validate_message``, ``validate_ground_to_edge`` and ``shape_for_peer``
  accept any mapping whose ``type`` is ``land`` or ``emergency_land_all``,
  whatever else it carries.
* ``decode`` in the ground-to-edge direction accepts every input that the
  pre-v2 edge (``json.loads`` in ``transport.py``) accepted as a landing, and
  more. When its strict parser rejects an input, it parses the input again
  leniently (see ``failsafe_type_of``): if that reads a JSON object whose
  top-level ``type``, or any occurrence of a repeated ``type`` key, is a
  landing type, ``decode`` returns ``{"type": <that type>}``. The lenient
  parse is bounded by ``FAILSAFE_MAX_BYTES`` (1 MiB, the frame limit the
  transport's WebSocket connections use), so it covers every message the
  transport can deliver.

An edge sends ``nack`` when it refuses a command, so the ground can hand lanes
to another drone or report them as uncovered instead of counting them as
searched. In v1 only lane assignments are nacked.

Clock convention (standard NTP): for one ping/pong round with ground send time
``t1``, edge receive time ``t2``, edge send time ``t3`` and ground receive time
``t4``, the offset is ``((t2 - t1) + (t3 - t4)) / 2``, which estimates
``edge_clock - ground_clock``, and the round-trip time is
``(t4 - t1) - (t3 - t2)``. The ground keeps the median of eight rounds and
sends that offset to the edge unchanged in a ``clock`` message. The edge then
fires a ``start_at`` when its own monotonic clock reaches
``t_ground_s + offset_s``, and reports the execution time back in ground time
as ``t_edge_exec - offset_s``.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PING",
    "PONG",
    "CLOCK",
    "ASSIGN_LANES",
    "START_AT",
    "TAKEOFF",
    "LAND",
    "EMERGENCY_LAND_ALL",
    "HEARTBEAT",
    "TELEMETRY",
    "ACK",
    "NACK",
    "HELLO",
    "HOLD",
    "RESUME",
    "NUDGE",
    "HANDOVER",
    "NODE_STATE",
    "HANDOVER_OFFER",
    "HANDOVER_ACCEPT",
    "HANDOVER_REJECT",
    "PEER_HEARTBEAT",
    "PROTOCOL_VERSION",
    "MIN_PROTOCOL_VERSION",
    "NACK_REASONS",
    "NACK_REASONS_V2",
    "GROUND_TO_EDGE_TYPES",
    "EDGE_TO_GROUND_TYPES",
    "GROUND_TO_EDGE_TYPES_V2",
    "EDGE_TO_GROUND_TYPES_V2",
    "PEER_TYPES",
    "V2_ONLY_COMMAND_TYPES",
    "LEGACY_GROUND_TO_EDGE_TYPES",
    "LEGACY_EDGE_TO_GROUND_TYPES",
    "FAILSAFE_TYPES",
    "FENCED_COMMAND_TYPES",
    "FENCED_CONTROL_TYPES",
    "COMMAND_TYPES",
    "RETRYABLE_COMMAND_TYPES",
    "V2_FIELDS",
    "GROUND_ROLES",
    "EDGE_ROLE",
    "HOLD_REASONS",
    "ON_COMPLETE_ACTIONS",
    "EDGE_STATES",
    "MISSION_STATES",
    "LATCH_PHASES",
    "PRIVACY_FORBIDDEN_KEYS",
    "PRIVACY_FORBIDDEN_KEY_PARTS",
    "MAX_NUMBERS_PER_LIST",
    "MAX_LISTED_NUMBERS",
    "DIRECTION_GROUND_TO_EDGE",
    "DIRECTION_EDGE_TO_GROUND",
    "DIRECTION_PEER",
    "CLOCK_SYNC_ROUNDS",
    "CLOCK_SYNC_MIN_SAMPLES",
    "HEARTBEAT_INTERVAL_S",
    "TELEMETRY_INTERVAL_S",
    "PONG_BUDGET_S",
    "HELLO_TIMEOUT_S",
    "CMD_ID_CACHE_SIZE",
    "NUDGE_MIN_CM",
    "NUDGE_MAX_CM",
    "DEFAULT_HOVER_TIMEOUT_S",
    "MAX_HOVER_TIMEOUT_S",
    "MAX_EPOCH",
    "MAX_ID_LEN",
    "MAX_TEXT_LEN",
    "MAX_LANES",
    "MAX_LANE_DISTANCE_CM",
    "MAX_FIELDS",
    "MAX_DEPTH",
    "MAX_NODES",
    "MAX_ROSTER",
    "MAX_DETECTIONS",
    "MAX_MESSAGE_BYTES",
    "FAILSAFE_MAX_BYTES",
    "LANE_DIRECTIONS",
    "NUDGE_DIRECTIONS",
    "TELEMETRY_FIELDS",
    "TELEMETRY_FIELDS_V2",
    "ProtocolError",
    "Fence",
    "TelemetryV2",
    "GroundHello",
    "EdgeHello",
    "ClockSample",
    "ClockEstimate",
    "EdgeClock",
    "new_cmd_id",
    "next_epoch",
    "fence_of",
    "cmd_id_of",
    "negotiate_proto",
    "parse_hello",
    "shape_for_peer",
    "make_ping",
    "make_pong",
    "answer_ping",
    "make_clock",
    "make_assign_lanes",
    "make_start_at",
    "make_takeoff",
    "make_land",
    "make_emergency_land_all",
    "make_heartbeat",
    "make_telemetry",
    "make_ack_start_at",
    "make_ack_assign_lanes",
    "make_nack_assign_lanes",
    "make_ground_hello",
    "make_edge_hello",
    "make_hold",
    "make_resume",
    "make_nudge",
    "make_handover",
    "make_ack",
    "make_nack",
    "make_node_state",
    "make_handover_offer",
    "make_handover_accept",
    "make_handover_reject",
    "make_peer_heartbeat",
    "validate_message",
    "validate_ground_to_edge",
    "validate_edge_to_ground",
    "validate_peer_message",
    "validate_for_direction",
    "encode",
    "decode",
    "failsafe_type_of",
    "sample_from_pong",
    "estimate_clock",
    "ground_to_edge_s",
    "edge_to_ground_s",
    "start_skews_s",
]

# ---------------------------------------------------------------------------
# Message types and protocol constants
# ---------------------------------------------------------------------------

# Protocol versions. A link that never exchanged a ``hello`` is v1.
PROTOCOL_VERSION = 2
MIN_PROTOCOL_VERSION = 1
# ``hello.proto`` values above this are rejected as garbage rather than
# negotiated down, so a corrupted field cannot pass for a future version.
_MAX_PROTO_FIELD = 255

# Ground -> edge.
PING = "ping"
CLOCK = "clock"
ASSIGN_LANES = "assign_lanes"
START_AT = "start_at"
TAKEOFF = "takeoff"
LAND = "land"
EMERGENCY_LAND_ALL = "emergency_land_all"
HEARTBEAT = "heartbeat"
# Ground -> edge, protocol v2.
HOLD = "hold"
RESUME = "resume"
NUDGE = "nudge"
HANDOVER = "handover"

# Both directions (ground form and edge form), protocol v2. A ``hello`` is
# accepted on a link of any version, because it is how the version is agreed.
HELLO = "hello"

# Edge -> ground.
PONG = "pong"
TELEMETRY = "telemetry"
ACK = "ack"
NACK = "nack"

# Ground -> ground over the peer link, protocol v2 only.
NODE_STATE = "node_state"
HANDOVER_OFFER = "handover_offer"
HANDOVER_ACCEPT = "handover_accept"
HANDOVER_REJECT = "handover_reject"
PEER_HEARTBEAT = "peer_heartbeat"

# Why an edge refused a lane assignment (design section 3). These are the only
# reasons a v1 nack may carry.
NACK_REASONS = frozenset({"geofence", "altitude", "battery", "other_drone", "invalid"})
# Protocol v2 adds the reasons for refusing any fenced command.
NACK_REASONS_V2 = NACK_REASONS | frozenset({
    "stale_epoch",
    "not_authority",
    "no_valid_plan",
    "busy",
    "landing",
    "not_flying",
    "plan_mismatch",
    "stale_plan",
})

GROUND_TO_EDGE_TYPES = frozenset(
    {PING, CLOCK, ASSIGN_LANES, START_AT, TAKEOFF, LAND, EMERGENCY_LAND_ALL, HEARTBEAT}
)
# Every edge-to-ground fleet message, including ``NACK``, which an edge sends
# when it refuses a lane assignment. The ground transport delivers ``ACK``,
# ``NACK`` and ``TELEMETRY`` to its ``on_control`` callback and consumes
# ``PONG`` itself for clock sync.
EDGE_TO_GROUND_TYPES = frozenset({PONG, TELEMETRY, ACK, NACK})

# Commands that exist only in protocol v2. They are never sent to a v1 peer:
# a v1 edge would ignore them silently, so a ``hold`` would not hold.
V2_ONLY_COMMAND_TYPES = frozenset({HOLD, RESUME, NUDGE, HANDOVER})
GROUND_TO_EDGE_TYPES_V2 = GROUND_TO_EDGE_TYPES | V2_ONLY_COMMAND_TYPES | {HELLO}
EDGE_TO_GROUND_TYPES_V2 = EDGE_TO_GROUND_TYPES | {HELLO}
PEER_TYPES = frozenset({NODE_STATE, HANDOVER_OFFER, HANDOVER_ACCEPT, HANDOVER_REJECT, PEER_HEARTBEAT})

# Non-fleet messages that share the edge/ground socket. The strict
# directional validators let them through by type only; their own handlers
# (``transport.py``, ``edge_device.py``) check them. ``set_target`` carries the
# ReID embedding, and the face embedding only when the ground is not
# appearance-only; nothing in this module adds to either.
LEGACY_GROUND_TO_EDGE_TYPES = frozenset({"set_target", "stream_video"})
LEGACY_EDGE_TO_GROUND_TYPES = frozenset({"detections"})

# Landing is never fenced: an edge executes these from any authenticated
# ground at any epoch, and never refuses one for a malformed optional field.
FAILSAFE_TYPES = frozenset({LAND, EMERGENCY_LAND_ALL})
# Commands that can move a drone or change who commands it. In v2 each must
# carry ``epoch``, ``leader_id`` and ``cmd_id``.
FENCED_COMMAND_TYPES = frozenset({TAKEOFF, ASSIGN_LANES, START_AT, HOLD, RESUME, NUDGE, HANDOVER})
# Leader control traffic that is not a command (no ack, no ``cmd_id``) but
# still has to come from the authority: a heartbeat feeds the link watchdog
# and a clock offset decides when a later ``start_at`` fires. In v2 each must
# carry ``epoch`` and ``leader_id``.
FENCED_CONTROL_TYPES = frozenset({HEARTBEAT, CLOCK})
# Every command an edge may ack or nack.
COMMAND_TYPES = FENCED_COMMAND_TYPES | FAILSAFE_TYPES
# Commands the ground may resend automatically, with the same ``cmd_id``,
# when no ack arrives in time. ``takeoff``, ``start_at`` and ``nudge`` are
# never resent automatically, and neither is ``resume``: a resume retried
# after a newer ``hold`` would set the drone moving again. A newer ``hold``,
# ``assign_lanes`` or ``land`` for a drone cancels the ground's pending
# retries of ``assign_lanes`` for that drone, and an ``assign_lanes`` that
# still arrives late is refused by its ``plan_seq`` (``stale_plan``).
RETRYABLE_COMMAND_TYPES = FAILSAFE_TYPES | frozenset({HOLD, ASSIGN_LANES, HANDOVER})

# Fields protocol v2 adds to v1 message types. ``shape_for_peer`` strips them
# when a message goes to a v1 peer.
V2_FIELDS = frozenset({
    "epoch",
    "leader_id",
    "cmd_id",
    "plan_id",
    "plan_seq",
    "on_complete",
    "hover_timeout_s",
    "epoch_seen",
    "result",
    "state",
    "plan_valid",
    "detail",
})

GROUND_ROLES = frozenset({"leader", "standby"})
EDGE_ROLE = "edge"
HOLD_REASONS = frozenset({"alert", "operator", "handover"})
ON_COMPLETE_ACTIONS = frozenset({"hover", "land"})
EDGE_STATES = frozenset({"idle", "taking_off", "hovering", "flying_lanes", "holding", "landing"})
MISSION_STATES = frozenset({"idle", "planned", "running", "holding", "complete"})
LATCH_PHASES = frozenset({"searching", "awaiting_response", "resolved"})

# Privacy checks. Detection crops, thumbnails and the target embeddings travel
# only in the legacy ``detections`` and ``set_target`` messages, under their
# existing opt-in rules; fleet control messages must not carry them. Two
# layers enforce this:
#
# * Peer (ground-to-ground) messages are checked against an allowlist at
#   every level: a field that is not in the schema is refused, every string
#   is at most ``MAX_TEXT_LEN`` characters and every list is bounded. So the
#   handover snapshot can only carry ids, positions, states and times.
# * Every fleet message (edge links and peer link alike) is screened by a
#   denylist and payload heuristics: a key that names imagery or biometric
#   data, a data URI or a long base64-looking string, a bytes value, or a
#   list of more than ``MAX_NUMBERS_PER_LIST`` numbers (or more than
#   ``MAX_LISTED_NUMBERS`` in all) is refused. Edge-link messages may carry
#   extra fields for forward compatibility, so on those links this is a
#   guard against accidental leakage, not a guarantee against a sender that
#   deliberately encodes data in permitted fields.
#
# Keys are compared after NFKC normalisation and case folding, both as they
# are and with separators removed (so ``Face-Embedding``, ``faceEmbedding``
# and a full-width ``ＦＡＣＥ`` all match). A key is refused when it is in
# ``PRIVACY_FORBIDDEN_KEYS`` or contains any of ``PRIVACY_FORBIDDEN_KEY_PARTS``.
# Replicating the target ReID embedding on handover (decision D4) would need
# an explicit change here and in the peer allowlists.
PRIVACY_FORBIDDEN_KEYS = frozenset({
    "face",
    "faces",
    "face_embedding",
    "face_embeddings",
    "embedding",
    "embeddings",
    "reid",
    "reid_embedding",
    "target_vec",
    "target_vector",
    "vector",
    "vectors",
    "feature",
    "features",
    "crop",
    "crops",
    "crop_jpeg",
    "crop_path",
    "crop_file",
    "image",
    "images",
    "image_b64",
    "img",
    "photo",
    "photos",
    "picture",
    "thumb",
    "thumbnail",
    "thumbnails",
    "jpeg",
    "jpg",
    "png",
    "frame",
    "frames",
    "pixels",
})
PRIVACY_FORBIDDEN_KEY_PARTS = (
    "face",
    "embed",
    "reid",
    "vec",
    "feature",
    "biometric",
    "crop",
    "image",
    "img",
    "photo",
    "picture",
    "thumb",
    "jpeg",
    "jpg",
    "png",
    "frame",
    "pixel",
    "b64",
    "base64",
)
# A list holding more numbers than this is refused anywhere in a fleet
# message: the largest legitimate number list is a placement triple, and an
# embedding is hundreds of floats.
MAX_NUMBERS_PER_LIST = 16
# Numbers held in lists across one message. A peer ``node_state`` holds at
# most 28 (``area_cm``, ``lkp_cm`` and eight placement triples).
MAX_LISTED_NUMBERS = 64

DIRECTION_GROUND_TO_EDGE = "ground_to_edge"
DIRECTION_EDGE_TO_GROUND = "edge_to_ground"
DIRECTION_PEER = "peer"

# The ground runs this many ping/pong rounds per edge and keeps the median.
CLOCK_SYNC_ROUNDS = 8
# A sync with fewer successful rounds than this is rejected rather than
# trusted, because a median of one or two samples is not robust to jitter.
CLOCK_SYNC_MIN_SAMPLES = 4
# The ground sends a heartbeat this often; the edge link watchdog lands the
# drone when ground messages stop for FLIGHTRISK_LINK_LOSS_LAND_S.
HEARTBEAT_INTERVAL_S = 1.0
# The edge sends telemetry this often.
TELEMETRY_INTERVAL_S = 0.5
# The edge must answer a ping within this budget (edge receive to edge send).
PONG_BUDGET_S = 0.020
# A v2 ground that gets no edge ``hello`` this long after sending its own
# treats the edge as v1.
HELLO_TIMEOUT_S = 2.0
# An edge remembers the outcome of at least this many recent ``cmd_id``s.
CMD_ID_CACHE_SIZE = 256

# A nudge is one horizontal Tello SDK move, so it uses the SDK's range.
NUDGE_MIN_CM = 20
NUDGE_MAX_CM = 500
DEFAULT_HOVER_TIMEOUT_S = 20.0
MAX_HOVER_TIMEOUT_S = 120.0

# Size and range caps. They bound memory and log volume for a malformed or
# hostile peer and are far above anything a real fleet sends.
MAX_EPOCH = 2**53 - 1  # the largest integer every JSON implementation keeps exactly
_MAX_SAFE_INT = 2**53 - 1
_MAX_ABS_NUMBER = 1e15
MAX_ID_LEN = 64
MAX_TEXT_LEN = 200
MAX_LANES = 1024
MAX_LANE_DISTANCE_CM = 100_000
MAX_FIELDS = 32
MAX_DEPTH = 8
MAX_NODES = 10_000
MAX_ROSTER = 8
MAX_DETECTIONS = 256
MAX_MESSAGE_BYTES = 64 * 1024
# Upper bound for the lenient fail-safe parse in ``decode`` (see
# ``failsafe_type_of``). It equals the default ``max_size`` of the
# ``websockets`` library, which ``transport.py`` uses on both ends, so every
# message the transport can deliver is within it. Larger input is not parsed.
FAILSAFE_MAX_BYTES = 1024 * 1024

# Lanes are horizontal moves flown at the assigned altitude slot. Vertical
# moves are not lane directions, because they would break the altitude
# separation between drones.
LANE_DIRECTIONS = frozenset({"forward", "back", "left", "right"})
# Nudges are horizontal for the same reason, and never rotate, because a
# rotation breaks the fixed-heading dead-reckoning frame.
NUDGE_DIRECTIONS = LANE_DIRECTIONS

TELEMETRY_FIELDS = (
    "drone_id",
    "battery",
    "height_cm",
    "is_flying",
    "rel_x_cm",
    "rel_y_cm",
    "heading_deg",
    "lane_index",
    "lanes_total",
    "link_age_s",
    "takeoff_at_local_s",
)
TELEMETRY_FIELDS_V2 = TELEMETRY_FIELDS + ("epoch", "plan_id", "state", "plan_valid")

_ZONE_FIELDS = ("x_cm", "y_cm", "w_cm", "h_cm")

# Peer-link allowlists: the only fields each ground-to-ground object may have.
_NODE_STATE_FIELDS = frozenset(
    {"type", "node_id", "role", "epoch", "proto", "roster", "mission", "latch", "detections"}
)
_ROSTER_FIELDS = frozenset({"drone_id", "node_id", "connected", "is_flying", "epoch_seen"})
_MISSION_FIELDS = frozenset({"plan_id", "pattern", "state", "area_cm", "lkp_cm", "placements"})
_LATCH_FIELDS = frozenset({"phase", "alert_id"})
_DETECTION_FIELDS = frozenset({"id", "x_cm", "y_cm", "first_seen_s", "drone_ids"})
_PEER_MESSAGE_FIELDS = {
    "handover_offer": frozenset({"type", "epoch_next", "snapshot"}),
    "handover_accept": frozenset({"type", "epoch_next"}),
    "handover_reject": frozenset({"type", "reason", "epoch_next"}),
    "peer_heartbeat": frozenset({"type", "seq", "epoch", "node_id"}),
}

# Payload heuristics for the privacy screen. A data URI with a media type, or
# any ``;base64,`` marker, anywhere in a string.
_DATA_URI_RE = re.compile(r"data:[\w.+-]+/[\w.+-]+|;base64,", re.IGNORECASE)
# A string longer than ``MAX_ID_LEN`` made only of base64 (or base64url, or
# hex) characters, optionally wrapped in lines. Plain text has spaces and
# punctuation, so it does not match.
_BASE64_RE = re.compile(r"[A-Za-z0-9+/=_\-\r\n]+")
_KEY_SEPARATORS_RE = re.compile(r"[\W_]+")

# Identifiers introduced by v2 (node ids, leader ids, plan ids, command ids):
# printable ASCII tokens, so they are safe in logs and file names.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")

# Every fleet type this module checks; anything else is passed through by
# ``validate_message`` and rejected by the strict directional validators.
_KNOWN_TYPES = GROUND_TO_EDGE_TYPES_V2 | EDGE_TO_GROUND_TYPES_V2 | PEER_TYPES


class ProtocolError(ValueError):
    """A fleet control message does not match the protocol schema."""


# ---------------------------------------------------------------------------
# Field checks
# ---------------------------------------------------------------------------


def _short(value: Any) -> str:
    """``repr`` capped in length, so a huge hostile value cannot flood the log."""
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _is_number(value: Any) -> bool:
    """True for a finite int or float of sane magnitude (bool is rejected even though it is an int)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -_MAX_ABS_NUMBER <= value <= _MAX_ABS_NUMBER
    if isinstance(value, float):
        return math.isfinite(value) and abs(value) <= _MAX_ABS_NUMBER
    return False


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_member(value: Any, choices: frozenset[str]) -> bool:
    """``value in choices`` that is False, not a TypeError, for unhashable values."""
    return isinstance(value, str) and value in choices


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def _require(msg: Mapping[str, Any], key: str) -> Any:
    if key not in msg:
        raise ProtocolError(f"{_short(msg.get('type'))} message is missing {key!r}")
    return msg[key]


def _require_number(msg: Mapping[str, Any], key: str) -> float:
    value = _require(msg, key)
    if not _is_number(value):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be a finite number, got {_short(value)}")
    return value


def _require_optional_number(msg: Mapping[str, Any], key: str) -> None:
    value = _require(msg, key)
    if value is not None and not _is_number(value):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a finite number or null, got {_short(value)}"
        )


def _require_optional_int(msg: Mapping[str, Any], key: str) -> None:
    value = _require(msg, key)
    if value is not None and not (_is_int(value) and 0 <= value <= _MAX_SAFE_INT):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a non-negative integer or null, got {_short(value)}"
        )


def _require_int_range(msg: Mapping[str, Any], key: str, low: int, high: int) -> int:
    value = _require(msg, key)
    if not (_is_int(value) and low <= value <= high):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be an integer from {low} to {high}, got {_short(value)}"
        )
    return value


def _require_bool(msg: Mapping[str, Any], key: str) -> bool:
    value = _require(msg, key)
    if not isinstance(value, bool):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be a boolean, got {_short(value)}")
    return value


def _require_drone_id(msg: Mapping[str, Any], key: str = "drone_id") -> str:
    value = _require(msg, key)
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LEN:
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be a non-empty string of at most "
            f"{MAX_ID_LEN} characters, got {_short(value)}"
        )
    return value


def _require_id(msg: Mapping[str, Any], key: str, *, nullable: bool = False) -> str | None:
    value = _require(msg, key)
    if value is None and nullable:
        return None
    if not _is_id(value):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be an identifier of 1 to {MAX_ID_LEN} characters "
            f"(letters, digits, '.', '_', ':', '-'){' or null' if nullable else ''}, got {_short(value)}"
        )
    return value


def _require_choice(msg: Mapping[str, Any], key: str, choices: frozenset[str]) -> str:
    value = _require(msg, key)
    if not _is_member(value, choices):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be one of {sorted(choices)}, got {_short(value)}"
        )
    return value


def _require_text(msg: Mapping[str, Any], key: str) -> str:
    value = _require(msg, key)
    if not (isinstance(value, str) and 0 < len(value) <= MAX_TEXT_LEN and value.isprintable()):
        raise ProtocolError(
            f"{msg.get('type')!r} field {key!r} must be printable text of 1 to {MAX_TEXT_LEN} "
            f"characters, got {_short(value)}"
        )
    return value


def _require_list(msg: Mapping[str, Any], key: str, max_len: int) -> list[Any]:
    value = _require(msg, key)
    if not isinstance(value, list):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be a list, got {_short(value)}")
    if len(value) > max_len:
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} has {len(value)} items; the limit is {max_len}")
    return value


def _require_object(msg: Mapping[str, Any], key: str, *, nullable: bool = False) -> Mapping[str, Any] | None:
    value = _require(msg, key)
    if value is None and nullable:
        return None
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{msg.get('type')!r} field {key!r} must be an object, got {_short(value)}")
    return value


def _is_private_key(key: str) -> bool:
    """True when *key* names imagery or biometric data (see ``PRIVACY_FORBIDDEN_KEYS``)."""
    folded = unicodedata.normalize("NFKC", key).casefold()
    compact = _KEY_SEPARATORS_RE.sub("", folded)
    if folded in PRIVACY_FORBIDDEN_KEYS or compact in PRIVACY_FORBIDDEN_KEYS:
        return True
    return any(part in compact for part in PRIVACY_FORBIDDEN_KEY_PARTS)


def _looks_like_encoded_media(text: str) -> bool:
    """True for a data URI or a long base64-looking string (an encoded image or vector)."""
    if _DATA_URI_RE.search(text):
        return True
    return len(text) > MAX_ID_LEN and _BASE64_RE.fullmatch(text) is not None


def _is_plain_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_structure(msg: Mapping[str, Any], *, max_text: int = MAX_MESSAGE_BYTES) -> None:
    """Caps on shape (fields, depth, size) plus the privacy screen.

    Walks the message iteratively, so a deeply nested value raises
    ``ProtocolError`` instead of ``RecursionError``. *max_text* caps every
    string, keys included (the peer link passes ``MAX_TEXT_LEN``).
    """
    mtype = _short(msg.get("type"))
    if len(msg) > MAX_FIELDS:
        raise ProtocolError(f"{mtype} message has {len(msg)} fields; the limit is {MAX_FIELDS}")
    stack: list[tuple[Any, int]] = [(msg, 1)]
    nodes = 0
    listed_numbers = 0

    def check_text(text: str, what: str) -> None:
        if len(text) > max_text:
            raise ProtocolError(f"{mtype} message has a {what} longer than {max_text} characters")
        if _looks_like_encoded_media(text):
            raise ProtocolError(
                f"{mtype} message has a {what} that looks like encoded media (a data URI or "
                "base64); fleet control messages never carry imagery or biometric data"
            )

    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise ProtocolError(f"{mtype} message has more than {MAX_NODES} values")
        if isinstance(value, Mapping):
            if depth > MAX_DEPTH:
                raise ProtocolError(f"{mtype} message nests deeper than {MAX_DEPTH} levels")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ProtocolError(f"{mtype} message has a non-string key {_short(key)}")
                check_text(key, "key")
                if _is_private_key(key):
                    raise ProtocolError(
                        f"{mtype} message has the field {_short(key)}; fleet control "
                        "messages never carry imagery or biometric data"
                    )
                stack.append((item, depth + 1))
        elif isinstance(value, (list, tuple)):
            if depth > MAX_DEPTH:
                raise ProtocolError(f"{mtype} message nests deeper than {MAX_DEPTH} levels")
            numbers = sum(1 for item in value if _is_plain_number(item))
            listed_numbers += numbers
            if numbers > MAX_NUMBERS_PER_LIST or listed_numbers > MAX_LISTED_NUMBERS:
                raise ProtocolError(
                    f"{mtype} message has a list of {numbers} numbers ({listed_numbers} in all); the limits "
                    f"are {MAX_NUMBERS_PER_LIST} per list and {MAX_LISTED_NUMBERS} per message, because "
                    "fleet control messages never carry embeddings"
                )
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            check_text(value, "string")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            raise ProtocolError(f"{mtype} message has a binary value; fleet control messages carry only JSON")


def _require_only(obj: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    """Peer-link allowlist: refuse any field of *obj* that is not in *allowed*."""
    for key in obj:
        if key not in allowed:
            raise ProtocolError(
                f"{what} has the field {_short(key)}, which is not in its schema; peer messages "
                "carry only the fields the protocol defines"
            )


def _validate_lanes(msg: Mapping[str, Any]) -> None:
    lanes = _require(msg, "lanes")
    if not isinstance(lanes, list):
        raise ProtocolError(f"'assign_lanes' field 'lanes' must be a list, got {_short(lanes)}")
    if len(lanes) > MAX_LANES:
        raise ProtocolError(f"'assign_lanes' has {len(lanes)} lanes; the limit is {MAX_LANES}")
    for index, lane in enumerate(lanes):
        if not isinstance(lane, Mapping):
            raise ProtocolError(f"'assign_lanes' lane {index} must be an object, got {_short(lane)}")
        direction = lane.get("direction")
        if not _is_member(direction, LANE_DIRECTIONS):
            raise ProtocolError(
                f"'assign_lanes' lane {index} direction must be one of "
                f"{sorted(LANE_DIRECTIONS)}, got {_short(direction)}"
            )
        distance = lane.get("distance_cm")
        if not _is_number(distance) or not 0 < distance <= MAX_LANE_DISTANCE_CM:
            raise ProtocolError(
                f"'assign_lanes' lane {index} distance_cm must be a positive number of at most "
                f"{MAX_LANE_DISTANCE_CM}, got {_short(distance)}"
            )


def _validate_zone(msg: Mapping[str, Any]) -> None:
    zone = _require(msg, "zone")
    if not isinstance(zone, Mapping):
        raise ProtocolError(f"'assign_lanes' field 'zone' must be an object, got {_short(zone)}")
    for key in _ZONE_FIELDS:
        value = zone.get(key)
        if not _is_number(value):
            raise ProtocolError(f"'assign_lanes' zone field {key!r} must be a finite number, got {_short(value)}")
    if zone["w_cm"] < 0 or zone["h_cm"] < 0:
        raise ProtocolError("'assign_lanes' zone width and height must not be negative")


def _validate_fence(msg: Mapping[str, Any], mtype: str, proto: int) -> None:
    """Fencing fields: required in v2 (and on v2-only commands), checked whenever present.

    ``epoch`` and ``leader_id`` travel as a pair: a message with one and not
    the other is malformed under every protocol version. A fenced epoch is at
    least 1, because a leader's first epoch is ``stored + 1`` with nothing
    stored meaning 0.
    """
    required = proto >= 2 or mtype in V2_ONLY_COMMAND_TYPES
    if required or "epoch" in msg or "leader_id" in msg:
        _require_int_range(msg, "epoch", 1, MAX_EPOCH)
        _require_id(msg, "leader_id")
    if mtype in FENCED_COMMAND_TYPES and (required or "cmd_id" in msg):
        _require_id(msg, "cmd_id")


def _validate_plan_id(msg: Mapping[str, Any], proto: int) -> None:
    if proto >= 2 or "plan_id" in msg:
        _require_id(msg, "plan_id")


def _validate_assign_lanes(msg: Mapping[str, Any], proto: int) -> None:
    _require_drone_id(msg)
    alt = _require_number(msg, "alt_cm")
    if alt <= 0:
        raise ProtocolError(f"'assign_lanes' field 'alt_cm' must be positive, got {alt!r}")
    _validate_lanes(msg)
    _validate_zone(msg)
    _validate_plan_id(msg, proto)
    if proto >= 2 or "plan_seq" in msg:
        # Orders the plans one leader issues within its epoch, so an edge can
        # refuse an older plan that arrives late (nack ``stale_plan``).
        _require_int_range(msg, "plan_seq", 1, _MAX_SAFE_INT)
    on_complete = msg.get("on_complete")
    if "on_complete" in msg:
        _require_choice(msg, "on_complete", ON_COMPLETE_ACTIONS)
    if "hover_timeout_s" in msg:
        if on_complete != "hover":
            raise ProtocolError("'assign_lanes' field 'hover_timeout_s' needs 'on_complete' to be 'hover'")
        timeout = _require_number(msg, "hover_timeout_s")
        if not 0 < timeout <= MAX_HOVER_TIMEOUT_S:
            raise ProtocolError(
                f"'assign_lanes' field 'hover_timeout_s' must be above 0 and at most "
                f"{MAX_HOVER_TIMEOUT_S}, got {timeout!r}"
            )


def _validate_telemetry(msg: Mapping[str, Any], proto: int) -> None:
    _require_drone_id(msg)
    _require_bool(msg, "is_flying")
    for key in ("battery", "height_cm", "rel_x_cm", "rel_y_cm", "heading_deg",
                "link_age_s", "takeoff_at_local_s"):
        _require_optional_number(msg, key)
    for key in ("lane_index", "lanes_total"):
        _require_optional_int(msg, key)
    if proto >= 2 or "epoch" in msg:
        _require_int_range(msg, "epoch", 0, MAX_EPOCH)
    if proto >= 2 or "plan_id" in msg:
        _require_id(msg, "plan_id", nullable=True)
    if proto >= 2 or "state" in msg:
        _require_choice(msg, "state", EDGE_STATES)
    if proto >= 2 or "plan_valid" in msg:
        _require_bool(msg, "plan_valid")


def _validate_reply_common(msg: Mapping[str, Any], target: str, proto: int) -> None:
    """Fields shared by v2 ``ack`` and ``nack``."""
    _require_drone_id(msg)
    if (proto >= 2 and target not in FAILSAFE_TYPES) or "cmd_id" in msg:
        _require_id(msg, "cmd_id")
    if "epoch_seen" in msg:
        _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    if "plan_id" in msg:
        _require_id(msg, "plan_id", nullable=True)
    if "detail" in msg:
        _require_text(msg, "detail")


def _validate_ack(msg: Mapping[str, Any], proto: int) -> None:
    target = _require(msg, "for")
    if not isinstance(target, str) or not target or len(target) > MAX_ID_LEN:
        raise ProtocolError(f"'ack' field 'for' must be a non-empty string, got {_short(target)}")
    if proto >= 2 and target not in COMMAND_TYPES:
        raise ProtocolError(f"'ack' field 'for' must be one of {sorted(COMMAND_TYPES)}, got {_short(target)}")
    _validate_reply_common(msg, target, proto)
    if proto >= 2 or "result" in msg:
        result = _require(msg, "result")
        if result != "ok":
            raise ProtocolError(f"'ack' field 'result' must be 'ok', got {_short(result)}")
    if target == START_AT:
        _require_number(msg, "executed_at_ground_s")
    elif target == ASSIGN_LANES:
        _require_number(msg, "battery")
        lanes_total = _require(msg, "lanes_total")
        if not (_is_int(lanes_total) and 0 <= lanes_total <= _MAX_SAFE_INT):
            raise ProtocolError(
                f"'ack' for 'assign_lanes' field 'lanes_total' must be a non-negative integer, "
                f"got {_short(lanes_total)}"
            )


def _validate_nack(msg: Mapping[str, Any], proto: int) -> None:
    target = _require(msg, "for")
    if proto < 2:
        # v1: only lane assignments are refused, for the v1 reasons.
        if target != ASSIGN_LANES:
            raise ProtocolError(f"'nack' field 'for' must be {ASSIGN_LANES!r}, got {_short(target)}")
        reasons = NACK_REASONS
    else:
        # A landing is never refused, so it is never nacked.
        if not _is_member(target, FENCED_COMMAND_TYPES):
            raise ProtocolError(
                f"'nack' field 'for' must be one of {sorted(FENCED_COMMAND_TYPES)}, got {_short(target)}"
            )
        reasons = NACK_REASONS_V2
    _validate_reply_common(msg, target, proto)
    reason = _require(msg, "reason")
    if not _is_member(reason, reasons):
        raise ProtocolError(f"'nack' field 'reason' must be one of {sorted(reasons)}, got {_short(reason)}")
    if target == ASSIGN_LANES:
        lanes_total = _require(msg, "lanes_total")
        if not (_is_int(lanes_total) and 0 <= lanes_total <= _MAX_SAFE_INT):
            raise ProtocolError(
                f"'nack' field 'lanes_total' must be a non-negative integer, got {_short(lanes_total)}"
            )


def _validate_proto_field(msg: Mapping[str, Any]) -> int:
    return _require_int_range(msg, "proto", MIN_PROTOCOL_VERSION, _MAX_PROTO_FIELD)


def _is_ground_hello(msg: Mapping[str, Any]) -> bool:
    return _is_member(msg.get("role"), GROUND_ROLES)


def _validate_hello(msg: Mapping[str, Any]) -> None:
    _validate_proto_field(msg)
    if _is_ground_hello(msg):
        if "drone_id" in msg:
            raise ProtocolError("a ground 'hello' must not carry 'drone_id'")
        node_id = _require_id(msg, "node_id")
        low = 1 if msg["role"] == "leader" else 0
        _require_int_range(msg, "epoch", low, MAX_EPOCH)
        if "leader_id" in msg:
            leader_id = _require_id(msg, "leader_id")
            if msg["role"] == "leader" and leader_id != node_id:
                raise ProtocolError("a leader 'hello' must name itself as 'leader_id'")
        return
    role = msg.get("role", EDGE_ROLE)
    if role != EDGE_ROLE:
        raise ProtocolError(
            f"'hello' field 'role' must be one of {sorted(GROUND_ROLES | {EDGE_ROLE})}, got {_short(role)}"
        )
    _require_drone_id(msg)
    _require_id(msg, "node_id")
    _require_int_range(msg, "epoch_seen", 0, MAX_EPOCH)
    _require_id(msg, "plan_id", nullable=True)
    _require_optional_int(msg, "lane_index")
    _require_bool(msg, "is_flying")
    battery = _require(msg, "battery")
    if battery is not None and not (_is_number(battery) and 0 <= battery <= 100):
        raise ProtocolError(f"'hello' field 'battery' must be a percentage or null, got {_short(battery)}")


def _validate_number_pair(value: Any, name: str, *, positive: bool = False, size: int = 2) -> None:
    if not (isinstance(value, list) and len(value) == size and all(_is_number(v) for v in value)):
        raise ProtocolError(f"{name} must be a list of {size} finite numbers, got {_short(value)}")
    if positive and any(v <= 0 for v in value):
        raise ProtocolError(f"{name} must be positive, got {_short(value)}")


def _validate_node_state(msg: Mapping[str, Any]) -> None:
    _require_only(msg, _NODE_STATE_FIELDS, "'node_state'")
    _require_id(msg, "node_id")
    _require_choice(msg, "role", GROUND_ROLES)
    _require_int_range(msg, "epoch", 0, MAX_EPOCH)
    _validate_proto_field(msg)
    for index, entry in enumerate(_require_list(msg, "roster", MAX_ROSTER)):
        if not isinstance(entry, Mapping):
            raise ProtocolError(f"'node_state' roster entry {index} must be an object")
        _require_only(entry, _ROSTER_FIELDS, f"'node_state' roster entry {index}")
        _require_drone_id(entry)
        _require_id(entry, "node_id")
        _require_bool(entry, "connected")
        _require_bool(entry, "is_flying")
        _require_int_range(entry, "epoch_seen", 0, MAX_EPOCH)
    mission = _require_object(msg, "mission", nullable=True)
    if mission is not None:
        _require_only(mission, _MISSION_FIELDS, "'node_state' mission")
        _require_id(mission, "plan_id", nullable=True)
        _require_id(mission, "pattern")
        _require_choice(mission, "state", MISSION_STATES)
        _validate_number_pair(_require(mission, "area_cm"), "'node_state' mission 'area_cm'", positive=True)
        lkp = _require(mission, "lkp_cm")
        if lkp is not None:
            _validate_number_pair(lkp, "'node_state' mission 'lkp_cm'")
        placements = _require_object(mission, "placements")
        if len(placements) > MAX_ROSTER:
            raise ProtocolError(f"'node_state' mission 'placements' has more than {MAX_ROSTER} drones")
        for drone_id, placement in placements.items():
            if not isinstance(drone_id, str) or not drone_id or len(drone_id) > MAX_ID_LEN:
                raise ProtocolError(f"'node_state' placement key {_short(drone_id)} is not a drone id")
            _validate_number_pair(placement, f"'node_state' placement {drone_id!r}", size=3)
    latch = _require_object(msg, "latch", nullable=True)
    if latch is not None:
        _require_only(latch, _LATCH_FIELDS, "'node_state' latch")
        _require_choice(latch, "phase", LATCH_PHASES)
        alert_id = _require(latch, "alert_id")
        if alert_id is not None and not (_is_int(alert_id) and 0 <= alert_id <= _MAX_SAFE_INT) \
                and not _is_id(alert_id):
            raise ProtocolError(f"'node_state' latch 'alert_id' must be an id or null, got {_short(alert_id)}")
    for index, det in enumerate(_require_list(msg, "detections", MAX_DETECTIONS)):
        if not isinstance(det, Mapping):
            raise ProtocolError(f"'node_state' detection {index} must be an object")
        _require_only(det, _DETECTION_FIELDS, f"'node_state' detection {index}")
        _require_int_range(det, "id", 0, _MAX_SAFE_INT)
        _require_number(det, "x_cm")
        _require_number(det, "y_cm")
        _require_number(det, "first_seen_s")
        drone_ids = _require(det, "drone_ids")
        if not (isinstance(drone_ids, list) and len(drone_ids) <= MAX_ROSTER):
            raise ProtocolError(f"'node_state' detection {index} 'drone_ids' must be a list of drone ids")
        for drone_id in drone_ids:
            if not isinstance(drone_id, str) or not drone_id or len(drone_id) > MAX_ID_LEN:
                raise ProtocolError(f"'node_state' detection {index} has a bad drone id {_short(drone_id)}")


def validate_message(msg: Any, *, proto: int = 1) -> str:
    """Check a fleet control message against the protocol schema.

    Returns the message type when the message is a valid fleet control
    message. Raises ``ProtocolError`` when the message is not an object, has
    no string ``type``, or is a known fleet type whose fields do not match the
    schema. Message types this protocol does not define (for example the
    legacy ``detections``, ``set_target`` and ``stream_video`` messages) are
    returned unchecked, so callers decide for themselves how to treat them;
    use ``validate_ground_to_edge``/``validate_edge_to_ground`` to reject them.

    *proto* is the protocol version agreed for the link (1 when no ``hello``
    was exchanged). With ``proto >= 2``, fenced types must carry ``epoch``
    and ``leader_id`` (and ``cmd_id`` for commands), lane plans and starts
    must carry ``plan_id``, telemetry must carry the v2 state fields, and
    acks and nacks follow the generic v2 rules. With ``proto == 1``, v2
    fields are optional but checked when present. ``land`` and
    ``emergency_land_all`` are always accepted, whatever else they carry.

    Never raises anything but ``ProtocolError`` for any *msg*.
    """
    if not (_is_int(proto) and proto >= MIN_PROTOCOL_VERSION):
        raise ProtocolError(f"protocol version must be an integer >= {MIN_PROTOCOL_VERSION}, got {_short(proto)}")
    if not isinstance(msg, Mapping):
        raise ProtocolError(f"control message must be an object, got {type(msg).__name__}")
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError(f"control message has no string 'type': {_short(mtype)}")
    if mtype in FAILSAFE_TYPES:
        # Fail-safe: a landing is accepted whatever its optional fields say.
        return mtype
    if mtype not in _KNOWN_TYPES:
        return mtype

    if mtype in PEER_TYPES:
        _check_structure(msg, max_text=MAX_TEXT_LEN)
        if mtype in _PEER_MESSAGE_FIELDS:
            _require_only(msg, _PEER_MESSAGE_FIELDS[mtype], repr(mtype))
    else:
        _check_structure(msg)
    if mtype in FENCED_COMMAND_TYPES or mtype in FENCED_CONTROL_TYPES:
        _validate_fence(msg, mtype, proto)

    if mtype == PING:
        _require_number(msg, "t1")
    elif mtype == PONG:
        for key in ("t1", "t2", "t3"):
            _require_number(msg, key)
    elif mtype == CLOCK:
        _require_number(msg, "offset_s")
    elif mtype == ASSIGN_LANES:
        _validate_assign_lanes(msg, proto)
    elif mtype == START_AT:
        _require_number(msg, "t_ground_s")
        _validate_plan_id(msg, proto)
    elif mtype == HEARTBEAT:
        _require_int_range(msg, "seq", 0, _MAX_SAFE_INT)
    elif mtype == TAKEOFF:
        pass
    elif mtype == HOLD:
        if "reason" in msg:
            _require_choice(msg, "reason", HOLD_REASONS)
    elif mtype == RESUME:
        # A v2-only command, so required on every link version: the edge
        # resumes only the plan the ground means, and answers a mismatch with
        # nack ``plan_mismatch``.
        _require_id(msg, "plan_id")
    elif mtype == NUDGE:
        _require_choice(msg, "direction", NUDGE_DIRECTIONS)
        _require_int_range(msg, "distance_cm", NUDGE_MIN_CM, NUDGE_MAX_CM)
    elif mtype == HANDOVER:
        _require_id(msg, "to_node")
        epoch_next = _require_int_range(msg, "epoch_next", 2, MAX_EPOCH)
        if epoch_next != msg["epoch"] + 1:
            # Same rule as handover_offer: leadership passes to exactly the
            # next epoch, so no epochs are skipped or burned.
            raise ProtocolError("'handover' field 'epoch_next' must be 'epoch' + 1")
        if msg["to_node"] == msg["leader_id"]:
            raise ProtocolError("'handover' must name another node as 'to_node'")
    elif mtype == HELLO:
        _validate_hello(msg)
    elif mtype == TELEMETRY:
        _validate_telemetry(msg, proto)
    elif mtype == ACK:
        _validate_ack(msg, proto)
    elif mtype == NACK:
        _validate_nack(msg, proto)
    elif mtype == NODE_STATE:
        _validate_node_state(msg)
    elif mtype == HANDOVER_OFFER:
        epoch_next = _require_int_range(msg, "epoch_next", 2, MAX_EPOCH)
        snapshot = _require_object(msg, "snapshot")
        if snapshot.get("type", NODE_STATE) != NODE_STATE:
            raise ProtocolError("'handover_offer' field 'snapshot' must be a 'node_state'")
        _validate_node_state(snapshot)
        if snapshot["role"] != "leader" or snapshot["epoch"] + 1 != epoch_next:
            raise ProtocolError("'handover_offer' must come from the leader and offer the next epoch")
    elif mtype == HANDOVER_ACCEPT:
        _require_int_range(msg, "epoch_next", 2, MAX_EPOCH)
    elif mtype == HANDOVER_REJECT:
        _require_text(msg, "reason")
        if "epoch_next" in msg:
            _require_int_range(msg, "epoch_next", 2, MAX_EPOCH)
    elif mtype == PEER_HEARTBEAT:
        _require_int_range(msg, "seq", 0, _MAX_SAFE_INT)
        _require_int_range(msg, "epoch", 0, MAX_EPOCH)
        if "node_id" in msg:
            _require_id(msg, "node_id")
    return mtype


def _message_type(msg: Any) -> str:
    if not isinstance(msg, Mapping):
        raise ProtocolError(f"control message must be an object, got {type(msg).__name__}")
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError(f"control message has no string 'type': {_short(mtype)}")
    return mtype


def validate_ground_to_edge(msg: Any, *, proto: int, require_fence: bool = False) -> str:
    """Strict check for a message an edge received from a ground.

    Like ``validate_message`` with *proto*, but also rejects any type that
    is not a ground-to-edge type of that version: unknown commands, edge or
    peer messages, an edge-form ``hello``, and v2-only commands on a v1 link.
    The legacy ``set_target`` and ``stream_video`` messages pass by type only.

    With *require_fence*, motion and control (``FENCED_COMMAND_TYPES`` and
    ``FENCED_CONTROL_TYPES``) must carry a valid fence, and commands a
    ``cmd_id``, whatever the link version. An edge in a v2-only or
    multi-ground deployment sets it, so a ground that never sends ``hello``,
    starts with a bare ``heartbeat`` or announces ``proto: 1`` cannot move
    the drone or feed its watchdog with unfenced v1 messages. Landing is
    still never fenced.
    """
    mtype = _message_type(msg)
    if mtype in LEGACY_GROUND_TO_EDGE_TYPES:
        return mtype
    allowed = GROUND_TO_EDGE_TYPES_V2 if _is_int(proto) and proto >= 2 else GROUND_TO_EDGE_TYPES | {HELLO}
    if mtype not in allowed:
        raise ProtocolError(f"{_short(mtype)} is not a ground-to-edge message in protocol v{_short(proto)}")
    validate_message(msg, proto=proto)
    if mtype == HELLO and not _is_ground_hello(msg):
        raise ProtocolError("a ground-to-edge 'hello' must have role 'leader' or 'standby'")
    if require_fence and (mtype in FENCED_COMMAND_TYPES or mtype in FENCED_CONTROL_TYPES):
        try:
            _validate_fence(msg, mtype, PROTOCOL_VERSION)
        except ProtocolError as exc:
            raise ProtocolError(
                f"unfenced {mtype!r} refused: this edge requires a fence on every link ({exc})"
            ) from exc
    return mtype


def validate_edge_to_ground(msg: Any, *, proto: int) -> str:
    """Strict check for a message a ground received from an edge.

    Rejects unknown types and ground-only types. The legacy ``detections``
    message passes by type only.
    """
    mtype = _message_type(msg)
    if mtype in LEGACY_EDGE_TO_GROUND_TYPES:
        return mtype
    if mtype not in EDGE_TO_GROUND_TYPES_V2:
        raise ProtocolError(f"{_short(mtype)} is not an edge-to-ground message")
    validate_message(msg, proto=proto)
    if mtype == HELLO and _is_ground_hello(msg):
        raise ProtocolError("an edge-to-ground 'hello' must have role 'edge'")
    return mtype


def validate_peer_message(msg: Any) -> str:
    """Strict check for a ground-to-ground message on the peer link (v2 only)."""
    mtype = _message_type(msg)
    if mtype not in PEER_TYPES:
        raise ProtocolError(f"{_short(mtype)} is not a ground-to-ground message")
    return validate_message(msg, proto=PROTOCOL_VERSION)


def validate_for_direction(msg: Any, direction: str, *, proto: int, require_fence: bool = False) -> str:
    """Dispatch to the strict validator for *direction* (``DIRECTION_*``).

    *require_fence* applies to the ground-to-edge direction only (see
    ``validate_ground_to_edge``).
    """
    if direction == DIRECTION_GROUND_TO_EDGE:
        return validate_ground_to_edge(msg, proto=proto, require_fence=require_fence)
    if direction == DIRECTION_EDGE_TO_GROUND:
        return validate_edge_to_ground(msg, proto=proto)
    if direction == DIRECTION_PEER:
        return validate_peer_message(msg)
    raise ProtocolError(f"unknown message direction {_short(direction)}")


# ---------------------------------------------------------------------------
# Wire encoding
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"control message repeats the key {_short(key)}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ProtocolError(f"control message contains the non-finite number {name}")


def _parse_finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ProtocolError(f"control message contains the out-of-range number {_short(text)}")
    return value


def encode(msg: Mapping[str, Any], *, direction: str, proto: int) -> str:
    """Validate *msg* for *direction* and *proto*, then serialise it as compact JSON.

    Raises ``ProtocolError`` for an invalid message or one larger than
    ``MAX_MESSAGE_BYTES`` encoded.
    """
    validate_for_direction(msg, direction, proto=proto)
    try:
        text = json.dumps(msg, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"control message cannot be encoded as JSON: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"encoded control message exceeds {MAX_MESSAGE_BYTES} bytes")
    return text


def _lenient_int(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        # More digits than Python converts by default. The value is never
        # used by the fail-safe parse, which only reads ``type``.
        return math.inf


def _top_level_types(source: str | bytes) -> list[Any] | None:
    """Every value of the top-level ``type`` key, parsed as leniently as ``json`` allows.

    Repeated keys are kept (each occurrence is returned), ``NaN``,
    ``Infinity``, out-of-range numbers, huge integers and control characters
    in strings are accepted. Returns ``None`` when *source* is not a JSON
    object. Never raises.
    """
    outermost: list[list[tuple[str, Any]]] = []

    def keep_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        # Objects close innermost first, so the last call is the top-level
        # object whenever the top-level value is an object.
        outermost[:] = [pairs]
        return dict(pairs)

    try:
        value = json.loads(source, object_pairs_hook=keep_pairs, parse_int=_lenient_int, strict=False)
    except (ValueError, RecursionError, TypeError, OverflowError):
        return None
    if not isinstance(value, dict) or not outermost:
        return None
    return [item for key, item in outermost[0] if key == "type"]


def failsafe_type_of(raw: Any, *, max_bytes: int = FAILSAFE_MAX_BYTES) -> str | None:
    """The landing a received ground-to-edge message asks for, read leniently.

    This is the fail-safe fallback ``decode`` uses after its strict parser
    rejects an input. Returns ``"emergency_land_all"`` or ``"land"`` when
    *raw* (``str``, ``bytes`` or ``bytearray`` of at most *max_bytes* bytes,
    measured as UTF-8 for ``str``) is a JSON object whose top-level ``type``
    key, or any occurrence of that key when it is repeated, is a landing type
    (``emergency_land_all`` wins over ``land``). Otherwise returns ``None``.
    Never raises.

    The parse accepts everything ``json.loads`` accepts (which is what the
    pre-v2 edge used, including bytes in UTF-16 or UTF-32 and a UTF-8 BOM),
    and additionally: repeated keys, ``NaN``/``Infinity``, numbers out of
    float range, integers with more digits than Python converts, control
    characters inside strings, invalid UTF-8 (replaced with U+FFFD) and a
    leading BOM in text. It does not accept input that is not a single JSON
    object, nesting deeper than the parser's recursion limit, or input over
    *max_bytes*. Treating ``{"type": "heartbeat", "type": "land"}`` as a
    landing is deliberate: landing is the safe reading of an ambiguous
    message.
    """
    if not (_is_int(max_bytes) and max_bytes > 0):
        return None
    sources: list[str | bytes] = []
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > max_bytes:
            return None
        data = bytes(raw)
        # json.loads detects UTF-8/16/32 and a UTF-8 BOM itself; the second
        # attempt replaces bytes that are not valid UTF-8.
        sources = [data, data.decode("utf-8", "replace").removeprefix("﻿")]
    elif isinstance(raw, str):
        if len(raw) > max_bytes or len(raw.encode("utf-8", "surrogatepass")) > max_bytes:
            return None
        sources = [raw.removeprefix("﻿")]
    else:
        return None
    for source in sources:
        types = _top_level_types(source)
        if types is None:
            continue
        found = {value for value in types if isinstance(value, str) and value in FAILSAFE_TYPES}
        if EMERGENCY_LAND_ALL in found:
            return EMERGENCY_LAND_ALL
        if LAND in found:
            return LAND
        return None
    return None


def _parse_strict(raw: Any, max_bytes: int) -> Any:
    """Parse *raw* as strict JSON, or raise ``ProtocolError``."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > max_bytes:
            raise ProtocolError(f"control message exceeds {max_bytes} bytes")
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("control message is not valid UTF-8") from exc
    elif isinstance(raw, str):
        if len(raw) > max_bytes or len(raw.encode("utf-8", "surrogatepass")) > max_bytes:
            raise ProtocolError(f"control message exceeds {max_bytes} bytes")
        text = raw
    else:
        raise ProtocolError(f"control message must be text or bytes, got {type(raw).__name__}")
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except ProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(f"control message is not valid JSON: {_short(str(exc))}") from exc


def decode(
    raw: str | bytes | bytearray,
    *,
    direction: str,
    proto: int,
    max_bytes: int = MAX_MESSAGE_BYTES,
    require_fence: bool = False,
) -> dict[str, Any]:
    """Parse and strictly validate one received control message.

    Rejects, with ``ProtocolError`` and nothing else: input over *max_bytes*,
    invalid UTF-8 or JSON, repeated keys, ``NaN``/``Infinity``, nesting too
    deep for the parser, and anything ``validate_for_direction`` rejects.

    Fail-safe exception (ground-to-edge direction only): when the strict
    parse rejects *raw* but ``failsafe_type_of`` reads it as a landing
    (bounded by ``max(max_bytes, FAILSAFE_MAX_BYTES)``), the result is
    ``{"type": "land"}`` or ``{"type": "emergency_land_all"}`` and nothing
    else, so an edge never refuses a landing that the pre-v2 edge executed.
    """
    if not (_is_int(max_bytes) and max_bytes > 0):
        raise ProtocolError(f"max_bytes must be a positive integer, got {_short(max_bytes)}")
    try:
        msg = _parse_strict(raw, max_bytes)
    except ProtocolError:
        if direction == DIRECTION_GROUND_TO_EDGE:
            landing = failsafe_type_of(raw, max_bytes=max(max_bytes, FAILSAFE_MAX_BYTES))
            if landing is not None:
                return {"type": landing}
        raise
    validate_for_direction(msg, direction, proto=proto, require_fence=require_fence)
    return msg


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


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
#
# Every builder validates what it produces, so a malformed message fails
# loudly at the sender instead of being dropped by the receiver. Builders
# that take a ``fence`` produce the v2 form (with a fresh ``cmd_id`` unless
# one is given for a retry) and validate it under v2 rules; without a fence
# they produce exactly the v1 form.


def _checked(msg: dict[str, Any], proto: int = 1) -> dict[str, Any]:
    validate_message(msg, proto=proto)
    return msg


def _fenced(msg: dict[str, Any], fence: Fence | None, cmd_id: str | None, *, command: bool = True) -> int:
    """Add fence and ``cmd_id`` fields in place; return the protocol version to validate with."""
    if fence is None:
        if cmd_id is not None:
            msg["cmd_id"] = cmd_id
        return 1
    if not isinstance(fence, Fence):
        raise ProtocolError(f"fence must be a Fence, got {type(fence).__name__}")
    msg.update(fence.fields())
    if command:
        msg["cmd_id"] = new_cmd_id() if cmd_id is None else cmd_id
    return 2


def make_ping(t1: float | None = None) -> dict[str, Any]:
    """Ground -> edge clock probe; ``t1`` defaults to ``time.monotonic()``."""
    return _checked({"type": PING, "t1": time.monotonic() if t1 is None else t1})


def make_pong(t1: float, t2: float, t3: float) -> dict[str, Any]:
    """Edge -> ground reply carrying the echoed ``t1`` and the edge's ``t2``/``t3``."""
    return _checked({"type": PONG, "t1": t1, "t2": t2, "t3": t3})


def answer_ping(
    ping: Mapping[str, Any],
    t2: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Build the pong for *ping*, which the edge received at edge time *t2*.

    ``t3`` is read from *clock* (the edge's monotonic clock) as the last step,
    so the caller should send the returned message straight away.
    """
    if ping.get("type") != PING:
        raise ProtocolError(f"answer_ping expects a 'ping' message, got {ping.get('type')!r}")
    t1 = _require_number(ping, "t1")
    return make_pong(t1, t2, clock())


def make_clock(offset_s: float, *, fence: Fence | None = None) -> dict[str, Any]:
    """Ground -> edge: the median offset (edge clock minus ground clock), unchanged."""
    msg: dict[str, Any] = {"type": CLOCK, "offset_s": offset_s}
    return _checked(msg, _fenced(msg, fence, None, command=False))


def make_assign_lanes(
    drone_id: str,
    alt_cm: float,
    lanes: Iterable[Mapping[str, Any]],
    zone: Mapping[str, Any],
    *,
    fence: Fence | None = None,
    cmd_id: str | None = None,
    plan_id: str | None = None,
    plan_seq: int | None = None,
    on_complete: str | None = None,
    hover_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Ground -> edge lane assignment for one drone.

    The v2 form (with *fence*) needs *plan_id* and *plan_seq*. *plan_seq*
    is an integer from 1 that the leader increases with every new plan it
    issues within its epoch; a retry of the same plan reuses it. The edge
    orders plans by ``(epoch, plan_seq)`` and refuses an older one with
    ``stale_plan``. *on_complete* says what the drone does after its last
    lane: ``"hover"`` for *hover_timeout_s* (default
    ``DEFAULT_HOVER_TIMEOUT_S``) and then land, or ``"land"``.
    """
    msg: dict[str, Any] = {
        "type": ASSIGN_LANES,
        "drone_id": drone_id,
        "alt_cm": alt_cm,
        "lanes": [dict(lane) for lane in lanes],
        "zone": dict(zone),
    }
    if plan_id is not None:
        msg["plan_id"] = plan_id
    if plan_seq is not None:
        msg["plan_seq"] = plan_seq
    if on_complete is not None:
        msg["on_complete"] = on_complete
        if on_complete == "hover":
            msg["hover_timeout_s"] = DEFAULT_HOVER_TIMEOUT_S if hover_timeout_s is None else hover_timeout_s
    if hover_timeout_s is not None and "hover_timeout_s" not in msg:
        msg["hover_timeout_s"] = hover_timeout_s
    return _checked(msg, _fenced(msg, fence, cmd_id))


def make_start_at(
    t_ground_s: float,
    *,
    fence: Fence | None = None,
    cmd_id: str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Ground -> edge: start (take off, then fly lanes) at this ground monotonic time.

    The v2 form (with *fence*) needs the *plan_id* of the plan to start; the
    edge refuses a mismatch with ``plan_mismatch``.
    """
    msg: dict[str, Any] = {"type": START_AT, "t_ground_s": t_ground_s}
    if plan_id is not None:
        msg["plan_id"] = plan_id
    return _checked(msg, _fenced(msg, fence, cmd_id))


def make_takeoff(*, fence: Fence | None = None, cmd_id: str | None = None) -> dict[str, Any]:
    """Ground -> edge: take off to the altitude slot, then hover. Never resent automatically."""
    msg: dict[str, Any] = {"type": TAKEOFF}
    return _checked(msg, _fenced(msg, fence, cmd_id))


def make_land(*, cmd_id: str | None = None, fence: Fence | None = None) -> dict[str, Any]:
    """Ground -> edge: land now. Never fenced; *cmd_id* and *fence* are informational."""
    msg: dict[str, Any] = {"type": LAND}
    if cmd_id is not None:
        if not _is_id(cmd_id):
            raise ProtocolError(f"cmd_id must be an identifier, got {_short(cmd_id)}")
        msg["cmd_id"] = cmd_id
    if fence is not None:
        if not isinstance(fence, Fence):
            raise ProtocolError(f"fence must be a Fence, got {type(fence).__name__}")
        msg.update(fence.fields())
    return msg


def make_emergency_land_all() -> dict[str, Any]:
    """Ground -> every edge: land immediately, ahead of anything else."""
    return {"type": EMERGENCY_LAND_ALL}


def make_heartbeat(seq: int, *, fence: Fence | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": HEARTBEAT, "seq": seq}
    return _checked(msg, _fenced(msg, fence, None, command=False))


@dataclass(frozen=True)
class TelemetryV2:
    """The fields protocol v2 adds to ``telemetry``."""

    epoch: int
    plan_id: str | None
    state: str
    plan_valid: bool

    def fields(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "plan_id": self.plan_id, "state": self.state, "plan_valid": self.plan_valid}


def make_telemetry(
    drone_id: str,
    battery: float | None,
    height_cm: float | None,
    is_flying: bool,
    rel_x_cm: float | None,
    rel_y_cm: float | None,
    heading_deg: float | None,
    lane_index: int | None,
    lanes_total: int | None,
    link_age_s: float | None,
    takeoff_at_local_s: float | None,
    *,
    v2: TelemetryV2 | None = None,
) -> dict[str, Any]:
    """Edge -> ground state report, sent every ``TELEMETRY_INTERVAL_S``."""
    msg: dict[str, Any] = {
        "type": TELEMETRY,
        "drone_id": drone_id,
        "battery": battery,
        "height_cm": height_cm,
        "is_flying": is_flying,
        "rel_x_cm": rel_x_cm,
        "rel_y_cm": rel_y_cm,
        "heading_deg": heading_deg,
        "lane_index": lane_index,
        "lanes_total": lanes_total,
        "link_age_s": link_age_s,
        "takeoff_at_local_s": takeoff_at_local_s,
    }
    if v2 is None:
        return _checked(msg)
    msg.update(v2.fields())
    return _checked(msg, 2)


def make_ack_start_at(drone_id: str, executed_at_ground_s: float, *, cmd_id: str | None = None) -> dict[str, Any]:
    """Edge -> ground: the edge executed ``start_at`` at this time, in ground time."""
    if cmd_id is not None:
        return make_ack(START_AT, drone_id, cmd_id, executed_at_ground_s=executed_at_ground_s)
    return _checked({
        "type": ACK,
        "for": START_AT,
        "drone_id": drone_id,
        "executed_at_ground_s": executed_at_ground_s,
    })


def make_ack_assign_lanes(
    drone_id: str, battery: float, lanes_total: int, *, cmd_id: str | None = None
) -> dict[str, Any]:
    """Edge -> ground: the edge accepted its lane assignment."""
    if cmd_id is not None:
        return make_ack(ASSIGN_LANES, drone_id, cmd_id, battery=battery, lanes_total=lanes_total)
    return _checked({
        "type": ACK,
        "for": ASSIGN_LANES,
        "drone_id": drone_id,
        "battery": battery,
        "lanes_total": lanes_total,
    })


def make_nack_assign_lanes(drone_id: str, reason: str, lanes_total: int) -> dict[str, Any]:
    """Edge -> ground: the edge refused its lane assignment for ``reason``.

    ``lanes_total`` is the number of lanes in the refused assignment, so the
    ground can tell which of its assignments was refused.
    """
    return _checked({
        "type": NACK,
        "for": ASSIGN_LANES,
        "drone_id": drone_id,
        "reason": reason,
        "lanes_total": lanes_total,
    })


def make_ground_hello(node_id: str, role: str, epoch: int, *, proto: int = PROTOCOL_VERSION) -> dict[str, Any]:
    """Ground -> edge (first message on every connection, and again on any role or epoch change)."""
    return GroundHello(node_id=node_id, role=role, epoch=epoch, proto=proto).to_message()


def make_edge_hello(
    drone_id: str,
    node_id: str,
    epoch_seen: int,
    *,
    plan_id: str | None,
    lane_index: int | None,
    is_flying: bool,
    battery: float | None,
    proto: int = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Edge -> ground, sent in reply to each ground ``hello`` (never before one)."""
    return EdgeHello(
        drone_id=drone_id,
        node_id=node_id,
        epoch_seen=epoch_seen,
        plan_id=plan_id,
        lane_index=lane_index,
        is_flying=is_flying,
        battery=battery,
        proto=proto,
    ).to_message()


def make_hold(fence: Fence, *, cmd_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
    """Ground -> edge (v2): stop and hover in place, keeping the plan for a later ``resume``."""
    msg: dict[str, Any] = {"type": HOLD}
    if reason is not None:
        msg["reason"] = reason
    return _checked(msg, _fenced(msg, _need_fence(fence), cmd_id))


def make_resume(fence: Fence, *, plan_id: str, cmd_id: str | None = None) -> dict[str, Any]:
    """Ground -> edge (v2): continue the held plan *plan_id*.

    The edge answers a *plan_id* that is not its current plan with nack
    ``plan_mismatch``. A resume is never resent automatically.
    """
    msg: dict[str, Any] = {"type": RESUME, "plan_id": plan_id}
    return _checked(msg, _fenced(msg, _need_fence(fence), cmd_id))


def make_nudge(fence: Fence, direction: str, distance_cm: int, *, cmd_id: str | None = None) -> dict[str, Any]:
    """Ground -> edge (v2): one horizontal move of 20-500 cm while hovering or holding."""
    msg: dict[str, Any] = {"type": NUDGE, "direction": direction, "distance_cm": distance_cm}
    return _checked(msg, _fenced(msg, _need_fence(fence), cmd_id))


def make_handover(
    fence: Fence,
    to_node: str,
    *,
    epoch_next: int | None = None,
    cmd_id: str | None = None,
) -> dict[str, Any]:
    """Ground -> edge (v2): leadership passes to *to_node* at *epoch_next*.

    *epoch_next* defaults to ``next_epoch(fence.epoch)`` and must equal it;
    at ``MAX_EPOCH`` there is none, and this raises ``ProtocolError``.
    """
    fence = _need_fence(fence)
    msg: dict[str, Any] = {
        "type": HANDOVER,
        "to_node": to_node,
        "epoch_next": next_epoch(fence.epoch) if epoch_next is None else epoch_next,
    }
    return _checked(msg, _fenced(msg, fence, cmd_id))


def _need_fence(fence: Any) -> Fence:
    if not isinstance(fence, Fence):
        raise ProtocolError(f"v2 commands need a Fence, got {type(fence).__name__}")
    return fence


def make_ack(
    for_type: str,
    drone_id: str,
    cmd_id: str | None,
    *,
    epoch_seen: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Edge -> ground (v2): generic ack for the command *for_type* with *cmd_id*.

    *cmd_id* may be ``None`` only for a ``land``/``emergency_land_all`` that
    carried none. Type-specific fields go in *fields*: ``executed_at_ground_s``
    for ``start_at``; ``battery`` and ``lanes_total`` for ``assign_lanes``.
    """
    msg: dict[str, Any] = {"type": ACK, "for": for_type, "drone_id": drone_id, "result": "ok"}
    if cmd_id is not None:
        msg["cmd_id"] = cmd_id
    if epoch_seen is not None:
        msg["epoch_seen"] = epoch_seen
    msg.update(fields)
    return _checked(msg, 2)


def make_nack(
    for_type: str,
    drone_id: str,
    reason: str,
    cmd_id: str,
    *,
    epoch_seen: int | None = None,
    lanes_total: int | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Edge -> ground (v2): the edge refused the fenced command *for_type* for *reason*.

    Pass *epoch_seen* with ``stale_epoch``/``not_authority`` so a stale
    leader learns the epoch it lost to. ``lanes_total`` is required for an
    ``assign_lanes`` nack. Landing is never refused, so it is never nacked.
    """
    msg: dict[str, Any] = {"type": NACK, "for": for_type, "drone_id": drone_id, "cmd_id": cmd_id, "reason": reason}
    if epoch_seen is not None:
        msg["epoch_seen"] = epoch_seen
    if lanes_total is not None:
        msg["lanes_total"] = lanes_total
    if detail is not None:
        msg["detail"] = detail
    return _checked(msg, 2)


def make_node_state(
    node_id: str,
    role: str,
    epoch: int,
    *,
    roster: Iterable[Mapping[str, Any]] = (),
    mission: Mapping[str, Any] | None = None,
    latch: Mapping[str, Any] | None = None,
    detections: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Ground -> ground: this node's view, sent at 1 Hz and on change. Never carries imagery."""
    return _checked({
        "type": NODE_STATE,
        "node_id": node_id,
        "role": role,
        "epoch": epoch,
        "proto": PROTOCOL_VERSION,
        "roster": [dict(entry) for entry in roster],
        "mission": None if mission is None else dict(mission),
        "latch": None if latch is None else dict(latch),
        "detections": [dict(det) for det in detections],
    }, 2)


def make_handover_offer(epoch_next: int, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Leader -> standby: offer leadership at *epoch_next* with the leader's ``node_state``.

    *epoch_next* must be ``next_epoch(snapshot["epoch"])``.
    """
    return _checked({"type": HANDOVER_OFFER, "epoch_next": epoch_next, "snapshot": dict(snapshot)}, 2)


def make_handover_accept(epoch_next: int) -> dict[str, Any]:
    """Standby -> leader: accepted; the standby has persisted *epoch_next*."""
    return _checked({"type": HANDOVER_ACCEPT, "epoch_next": epoch_next}, 2)


def make_handover_reject(reason: str, *, epoch_next: int | None = None) -> dict[str, Any]:
    """Standby -> leader: refused, with a short human-readable *reason*."""
    msg: dict[str, Any] = {"type": HANDOVER_REJECT, "reason": reason}
    if epoch_next is not None:
        msg["epoch_next"] = epoch_next
    return _checked(msg, 2)


def make_peer_heartbeat(seq: int, epoch: int, *, node_id: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": PEER_HEARTBEAT, "seq": seq, "epoch": epoch}
    if node_id is not None:
        msg["node_id"] = node_id
    return _checked(msg, 2)


# ---------------------------------------------------------------------------
# Clock synchronisation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockSample:
    """One ping/pong round.

    ``t1`` and ``t4`` are on the ground's monotonic clock; ``t2`` and ``t3``
    are on the edge's monotonic clock.
    """

    t1: float
    t2: float
    t3: float
    t4: float

    @property
    def offset_s(self) -> float:
        """Estimated edge clock minus ground clock for this round."""
        return ((self.t2 - self.t1) + (self.t3 - self.t4)) / 2.0

    @property
    def rtt_s(self) -> float:
        """Network round-trip time, excluding the edge's turnaround."""
        return (self.t4 - self.t1) - (self.t3 - self.t2)

    @property
    def turnaround_s(self) -> float:
        """Time the edge took between receiving the ping and sending the pong."""
        return self.t3 - self.t2


@dataclass(frozen=True)
class ClockEstimate:
    """Median clock offset and round-trip time over several rounds."""

    offset_s: float
    rtt_s: float
    rounds: int
    measured_at_s: float | None = None
    samples: tuple[ClockSample, ...] = ()


def sample_from_pong(pong: Mapping[str, Any], t4: float) -> ClockSample:
    """Turn a received pong (ground receive time *t4*) into a ``ClockSample``.

    Raises ``ProtocolError`` for a malformed pong or for timestamps that run
    backwards on either clock, since such a round cannot be trusted.
    """
    if pong.get("type") != PONG:
        raise ProtocolError(f"expected a 'pong' message, got {pong.get('type')!r}")
    validate_message(pong)
    if not _is_number(t4):
        raise ProtocolError(f"t4 must be a finite number, got {t4!r}")
    sample = ClockSample(t1=pong["t1"], t2=pong["t2"], t3=pong["t3"], t4=t4)
    if sample.t3 < sample.t2:
        raise ProtocolError("pong t3 (edge send) is earlier than t2 (edge receive)")
    if sample.t4 < sample.t1:
        raise ProtocolError("pong t4 (ground receive) is earlier than t1 (ground send)")
    return sample


def estimate_clock(
    samples: Iterable[ClockSample],
    measured_at_s: float | None = None,
) -> ClockEstimate:
    """Median offset and median round-trip time over *samples*.

    Raises ``ProtocolError`` when *samples* is empty.
    """
    samples = list(samples)
    if not samples:
        raise ProtocolError("cannot estimate the clock offset from zero samples")
    return ClockEstimate(
        offset_s=statistics.median(s.offset_s for s in samples),
        rtt_s=statistics.median(s.rtt_s for s in samples),
        rounds=len(samples),
        measured_at_s=measured_at_s,
        samples=tuple(samples),
    )


def ground_to_edge_s(t_ground_s: float, offset_s: float) -> float:
    """Convert a ground monotonic time to the edge's monotonic clock."""
    return t_ground_s + offset_s


def edge_to_ground_s(t_edge_s: float, offset_s: float) -> float:
    """Convert an edge monotonic time to the ground's monotonic clock."""
    return t_edge_s - offset_s


def start_skews_s(executed_at_ground_s: Mapping[str, float]) -> dict[str, float]:
    """Per-drone start skew: each drone's start time minus the earliest one."""
    if not executed_at_ground_s:
        return {}
    earliest = min(executed_at_ground_s.values())
    return {drone_id: t - earliest for drone_id, t in executed_at_ground_s.items()}


class EdgeClock:
    """The edge's view of the ground clock.

    Holds the offset from the latest ``clock`` message and converts between
    ground time and the edge's own monotonic clock. ``offset_s`` is ``None``
    until the first ``clock`` message arrives; conversions raise
    ``RuntimeError`` until then, so an edge can never act on a ``start_at``
    with an unknown offset by accident.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._offset_s: float | None = None

    @property
    def offset_s(self) -> float | None:
        return self._offset_s

    @property
    def synced(self) -> bool:
        return self._offset_s is not None

    def now(self) -> float:
        """The edge's monotonic clock."""
        return self._clock()

    def apply(self, msg: Mapping[str, Any]) -> float:
        """Store the offset from a ``clock`` message and return it."""
        if msg.get("type") != CLOCK:
            raise ProtocolError(f"expected a 'clock' message, got {msg.get('type')!r}")
        validate_message(msg)
        self._offset_s = float(msg["offset_s"])
        return self._offset_s

    def _require_offset(self) -> float:
        offset = self._offset_s
        if offset is None:
            raise RuntimeError("edge clock is not synchronised: no 'clock' message received yet")
        return offset

    def to_local(self, t_ground_s: float) -> float:
        """Edge monotonic time at which the ground clock reads *t_ground_s*."""
        return ground_to_edge_s(t_ground_s, self._require_offset())

    def to_ground(self, t_edge_s: float) -> float:
        """Ground monotonic time corresponding to edge time *t_edge_s*."""
        return edge_to_ground_s(t_edge_s, self._require_offset())

    def seconds_until(self, t_ground_s: float) -> float:
        """Seconds from now (edge clock) until the ground clock reads *t_ground_s*."""
        return self.to_local(t_ground_s) - self.now()
