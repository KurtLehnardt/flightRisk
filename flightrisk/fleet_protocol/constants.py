"""Message types, field sets and protocol constants for the fleet protocol."""
from __future__ import annotations

import re


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
# Size limit for the legacy non-fleet messages in ``decode`` and ``encode``
# (``LEGACY_GROUND_TO_EDGE_TYPES`` and ``LEGACY_EDGE_TO_GROUND_TYPES``, in
# their own direction). ``MAX_MESSAGE_BYTES`` applies to fleet messages only:
# a ``detections`` message with two crops and a thumbnail is over 100 KB, and
# the pre-v2 transport accepted every legacy message up to the WebSocket
# frame limit.
LEGACY_MAX_BYTES = FAILSAFE_MAX_BYTES

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
