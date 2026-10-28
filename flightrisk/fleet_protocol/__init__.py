"""Fleet control protocol shared by the ground station and the edge devices.

This module is the single source of truth for the JSON control messages that
travel over the existing edge/ground WebSocket (see ``flightrisk/transport.py``)
when several drones fly as one coordinated fleet. It is deliberately pure: it
imports no networking code, so the fleet coordinator, the edge device and the
transport can all share it, and it can be unit-tested without sockets.

The code lives in submodules (``constants``, ``checks``, ``peer``,
``validate``, ``wire``, ``fencing``, ``builders`` and ``clock``), and every
public name is re-exported here, so ``from flightrisk import fleet_protocol``
and ``from flightrisk.fleet_protocol import <name>`` keep working.

Protocol v1 (the hackathon flow) is still the default, and realistic v1
messages validate unchanged; v1 validation adds size caps and privacy checks
and no longer crashes on malformed input. Protocol v2 adds leader epochs and fencing, command ids, plan ids, a version handshake
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
  whatever else it carries and whatever protocol version they are given
  (even an invalid one, such as ``None`` before a ``hello``): the landing
  check comes before any check of the version.
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

from flightrisk.fleet_protocol.builders import (
    TelemetryV2,
    answer_ping,
    make_ack,
    make_ack_assign_lanes,
    make_ack_start_at,
    make_assign_lanes,
    make_clock,
    make_edge_hello,
    make_emergency_land_all,
    make_ground_hello,
    make_handover,
    make_handover_accept,
    make_handover_offer,
    make_handover_reject,
    make_heartbeat,
    make_hold,
    make_land,
    make_nack,
    make_nack_assign_lanes,
    make_node_state,
    make_nudge,
    make_peer_heartbeat,
    make_ping,
    make_pong,
    make_profile_ack,
    make_profile_nack,
    make_resume,
    make_set_detection_profile,
    make_start_at,
    make_takeoff,
    make_telemetry,
)
from flightrisk.fleet_protocol.checks import (
    ProtocolError,
    is_identifier,
    is_profile_name,
)
from flightrisk.fleet_protocol.clock import (
    ClockEstimate,
    ClockSample,
    EdgeClock,
    edge_to_ground_s,
    estimate_clock,
    ground_to_edge_s,
    sample_from_pong,
    start_skews_s,
)
from flightrisk.fleet_protocol.constants import (
    ACK,
    ASSIGN_LANES,
    CLOCK,
    CLOCK_SYNC_MIN_SAMPLES,
    CLOCK_SYNC_ROUNDS,
    CMD_ID_CACHE_SIZE,
    COMMAND_TYPES,
    CONFIG_COMMAND_TYPES,
    DEFAULT_HOVER_TIMEOUT_S,
    DIRECTION_EDGE_TO_GROUND,
    DIRECTION_GROUND_TO_EDGE,
    DIRECTION_PEER,
    EDGE_ROLE,
    EDGE_STATES,
    EDGE_TO_GROUND_TYPES,
    EDGE_TO_GROUND_TYPES_V2,
    EMERGENCY_LAND_ALL,
    FAILSAFE_MAX_BYTES,
    FAILSAFE_TYPES,
    FENCED_COMMAND_TYPES,
    FENCED_CONTROL_TYPES,
    GROUND_ROLES,
    GROUND_TO_EDGE_TYPES,
    GROUND_TO_EDGE_TYPES_V2,
    HANDOVER,
    HANDOVER_ACCEPT,
    HANDOVER_OFFER,
    HANDOVER_REJECT,
    HEARTBEAT,
    HEARTBEAT_INTERVAL_S,
    HELLO,
    HELLO_TIMEOUT_S,
    HOLD,
    HOLD_REASONS,
    LAND,
    LANE_DIRECTIONS,
    LATCH_PHASES,
    LEGACY_EDGE_TO_GROUND_TYPES,
    LEGACY_GROUND_TO_EDGE_TYPES,
    LEGACY_MAX_BYTES,
    MAX_DEPTH,
    MAX_DETECTIONS,
    MAX_EPOCH,
    MAX_FIELDS,
    MAX_HOVER_TIMEOUT_S,
    MAX_ID_LEN,
    MAX_LANE_DISTANCE_CM,
    MAX_LANES,
    MAX_LISTED_NUMBERS,
    MAX_MESSAGE_BYTES,
    MAX_NODE_STATE_BYTES,
    MAX_NODES,
    MAX_NUMBERS_PER_LIST,
    MAX_PROFILE_NAME_LEN,
    MAX_ROSTER,
    MAX_TEXT_LEN,
    MIN_PROTOCOL_VERSION,
    MISSION_STATES,
    NACK,
    NACK_GEOFENCE_FIELDS,
    NACK_REASONS,
    NACK_REASONS_V2,
    NODE_STATE,
    NUDGE,
    NUDGE_DIRECTIONS,
    NUDGE_MAX_CM,
    NUDGE_MIN_CM,
    ON_COMPLETE_ACTIONS,
    PEER_HEARTBEAT,
    PEER_TYPES,
    PING,
    PONG,
    PONG_BUDGET_S,
    PRIVACY_FORBIDDEN_KEY_PARTS,
    PRIVACY_FORBIDDEN_KEYS,
    PROFILE_FALLBACK_REASONS,
    PROFILE_NACK_REASONS,
    PROTOCOL_VERSION,
    RESUME,
    RETRYABLE_COMMAND_TYPES,
    SET_DETECTION_PROFILE,
    START_AT,
    TAKEOFF,
    TELEMETRY,
    TELEMETRY_DRONE_LINK_FIELDS,
    TELEMETRY_FIELDS,
    TELEMETRY_FIELDS_V2,
    TELEMETRY_INTERVAL_S,
    V2_FIELDS,
    V2_ONLY_COMMAND_TYPES,
)
from flightrisk.fleet_protocol.fencing import (
    EdgeHello,
    Fence,
    GroundHello,
    cmd_id_of,
    fence_of,
    negotiate_proto,
    new_cmd_id,
    next_epoch,
    parse_hello,
    shape_for_peer,
)
from flightrisk.fleet_protocol.validate import (
    validate_edge_to_ground,
    validate_for_direction,
    validate_ground_to_edge,
    validate_message,
    validate_peer_message,
)
from flightrisk.fleet_protocol.wire import (
    _parse_strict as _parse_strict,  # used by the tests
)
from flightrisk.fleet_protocol.wire import (
    decode,
    encode,
    failsafe_type_of,
)

__all__ = [
    "ACK",
    "ASSIGN_LANES",
    "CLOCK",
    "CLOCK_SYNC_MIN_SAMPLES",
    "CLOCK_SYNC_ROUNDS",
    "CMD_ID_CACHE_SIZE",
    "COMMAND_TYPES",
    "CONFIG_COMMAND_TYPES",
    "DEFAULT_HOVER_TIMEOUT_S",
    "DIRECTION_EDGE_TO_GROUND",
    "DIRECTION_GROUND_TO_EDGE",
    "DIRECTION_PEER",
    "EDGE_ROLE",
    "EDGE_STATES",
    "EDGE_TO_GROUND_TYPES",
    "EDGE_TO_GROUND_TYPES_V2",
    "EMERGENCY_LAND_ALL",
    "FAILSAFE_MAX_BYTES",
    "FAILSAFE_TYPES",
    "FENCED_COMMAND_TYPES",
    "FENCED_CONTROL_TYPES",
    "GROUND_ROLES",
    "GROUND_TO_EDGE_TYPES",
    "GROUND_TO_EDGE_TYPES_V2",
    "HANDOVER",
    "HANDOVER_ACCEPT",
    "HANDOVER_OFFER",
    "HANDOVER_REJECT",
    "HEARTBEAT",
    "HEARTBEAT_INTERVAL_S",
    "HELLO",
    "HELLO_TIMEOUT_S",
    "HOLD",
    "HOLD_REASONS",
    "LAND",
    "LANE_DIRECTIONS",
    "LATCH_PHASES",
    "LEGACY_EDGE_TO_GROUND_TYPES",
    "LEGACY_GROUND_TO_EDGE_TYPES",
    "LEGACY_MAX_BYTES",
    "MAX_DEPTH",
    "MAX_DETECTIONS",
    "MAX_EPOCH",
    "MAX_FIELDS",
    "MAX_HOVER_TIMEOUT_S",
    "MAX_ID_LEN",
    "MAX_LANES",
    "MAX_LANE_DISTANCE_CM",
    "MAX_LISTED_NUMBERS",
    "MAX_MESSAGE_BYTES",
    "MAX_NODES",
    "MAX_NODE_STATE_BYTES",
    "MAX_NUMBERS_PER_LIST",
    "MAX_PROFILE_NAME_LEN",
    "MAX_ROSTER",
    "MAX_TEXT_LEN",
    "MIN_PROTOCOL_VERSION",
    "MISSION_STATES",
    "NACK",
    "NACK_GEOFENCE_FIELDS",
    "NACK_REASONS",
    "NACK_REASONS_V2",
    "NODE_STATE",
    "NUDGE",
    "NUDGE_DIRECTIONS",
    "NUDGE_MAX_CM",
    "NUDGE_MIN_CM",
    "ON_COMPLETE_ACTIONS",
    "PEER_HEARTBEAT",
    "PEER_TYPES",
    "PING",
    "PONG",
    "PONG_BUDGET_S",
    "PRIVACY_FORBIDDEN_KEYS",
    "PRIVACY_FORBIDDEN_KEY_PARTS",
    "PROFILE_FALLBACK_REASONS",
    "PROFILE_NACK_REASONS",
    "PROTOCOL_VERSION",
    "RESUME",
    "RETRYABLE_COMMAND_TYPES",
    "SET_DETECTION_PROFILE",
    "START_AT",
    "TAKEOFF",
    "TELEMETRY",
    "TELEMETRY_DRONE_LINK_FIELDS",
    "TELEMETRY_FIELDS",
    "TELEMETRY_FIELDS_V2",
    "TELEMETRY_INTERVAL_S",
    "V2_FIELDS",
    "V2_ONLY_COMMAND_TYPES",
    "ClockEstimate",
    "ClockSample",
    "EdgeClock",
    "EdgeHello",
    "Fence",
    "GroundHello",
    "ProtocolError",
    "TelemetryV2",
    "answer_ping",
    "cmd_id_of",
    "decode",
    "edge_to_ground_s",
    "encode",
    "estimate_clock",
    "failsafe_type_of",
    "fence_of",
    "ground_to_edge_s",
    "is_identifier",
    "is_profile_name",
    "make_ack",
    "make_ack_assign_lanes",
    "make_ack_start_at",
    "make_assign_lanes",
    "make_clock",
    "make_edge_hello",
    "make_emergency_land_all",
    "make_ground_hello",
    "make_handover",
    "make_handover_accept",
    "make_handover_offer",
    "make_handover_reject",
    "make_heartbeat",
    "make_hold",
    "make_land",
    "make_nack",
    "make_nack_assign_lanes",
    "make_node_state",
    "make_nudge",
    "make_peer_heartbeat",
    "make_ping",
    "make_pong",
    "make_profile_ack",
    "make_profile_nack",
    "make_resume",
    "make_set_detection_profile",
    "make_start_at",
    "make_takeoff",
    "make_telemetry",
    "negotiate_proto",
    "new_cmd_id",
    "next_epoch",
    "parse_hello",
    "sample_from_pong",
    "shape_for_peer",
    "start_skews_s",
    "validate_edge_to_ground",
    "validate_for_direction",
    "validate_ground_to_edge",
    "validate_message",
    "validate_peer_message",
]
