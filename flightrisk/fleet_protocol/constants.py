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
    # The edge cannot talk to its drone (no state, failed keepalive or send),
    # so it refuses a takeoff or a start that would take off. ``detail``
    # carries the operator text, for example ``Tello unreachable (no state
    # for 12 s)``. A ground without this reason logs the nack as malformed
    # and ignores it.
    "drone_unreachable",
    # The straight line home from some point of an assignment's zone and
    # corridor leaves them while the drone's link-loss action cannot be
    # ``land`` (MAV-19, B-3). An older ground rejects an unknown reason and
    # drops the nack, so an edge sends this one only to a ground that has
    # sent it ``describe_unit`` and otherwise sends ``invalid`` with the same
    # ``detail`` (``nack_reason_for_peer``).
    "straight_line_home",
})
# Optional numeric fields of a ``geofence`` nack for ``assign_lanes`` (v1 and
# v2), all in cm and relative to the drone's takeoff point of its current
# flight (or its next takeoff point while it is on the ground):
# ``geofence_cm`` is the edge's fence half-width, ``x_cm``/``y_cm`` the first
# point of the refused path outside the fence, and ``rel_x_cm``/``rel_y_cm``
# where that path starts (the drone's dead-reckoned position). ``geofence_cm``
# stays the full width; ``effective_geofence_cm`` is that width narrowed by the
# margin of a refused step on this flight, which the edge really enforces.
# Older grounds ignore the keys they do not know.
NACK_GEOFENCE_FIELDS = ("geofence_cm", "x_cm", "y_cm", "rel_x_cm", "rel_y_cm", "effective_geofence_cm")

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

# Ground -> edge return commands, protocol v2 only (design section 18,
# ``flightrisk/fleet_protocol/returns.py``). Each carries ``drone_id`` and
# ``cmd_id`` and is answered with an ``ack`` or ``nack``. ``set_no_entry``,
# ``set_speed``, ``escort_setpoint`` and a ``return_home`` with ``wait`` are
# fenced and obeyed only from the authority; a ``return_home`` without
# ``wait`` is obeyed from any authenticated ground, like ``land`` (M-5). A
# ground sends them only to an edge whose ``describe_unit`` reply lists them
# in ``accepts``; an older edge refuses the unknown type. They are kept out of
# ``GROUND_TO_EDGE_TYPES_V2``, ``FENCED_COMMAND_TYPES`` and ``COMMAND_TYPES``
# so the routing, retry and authority rules of today's commands are
# unchanged until the edge and ground adopt them.
RETURN_HOME = "return_home"
SET_NO_ENTRY = "set_no_entry"
SET_SPEED = "set_speed"
ESCORT_SETPOINT = "escort_setpoint"
RETURN_COMMAND_TYPES = frozenset({RETURN_HOME, SET_NO_ENTRY, SET_SPEED, ESCORT_SETPOINT})
# Read-only queries, protocol v2 only. ``describe_unit`` is answered with an
# ``ack`` carrying the unit's description; it never feeds the link watchdog
# and is not a configuration command.
DESCRIBE_UNIT = "describe_unit"
QUERY_TYPES = frozenset({DESCRIBE_UNIT})
EDGE_TO_GROUND_TYPES_V2 = EDGE_TO_GROUND_TYPES | {HELLO}
PEER_TYPES = frozenset({NODE_STATE, HANDOVER_OFFER, HANDOVER_ACCEPT, HANDOVER_REJECT, PEER_HEARTBEAT})

# Non-fleet messages that share the edge/ground socket. The strict
# directional validators let them through by type only; their own handlers
# (``transport.py``, ``edge_device.py``) check them. ``set_target`` carries the
# ReID embedding, and the face embedding only when the ground is not
# appearance-only; nothing in this module adds to either.
LEGACY_GROUND_TO_EDGE_TYPES = frozenset({"set_target", "stream_video"})
LEGACY_EDGE_TO_GROUND_TYPES = frozenset({"detections"})

# Ground -> edge configuration commands. They never move a drone and never
# change who commands it, so they carry no fence, but they are commands: each
# carries a ``cmd_id`` and is answered with an ``ack`` or ``nack`` whose
# ``for`` names it, on every link version. An edge in multi-ground mode obeys
# them only from its authority, like ``stream_video``. Older edges reject the
# type as unknown and ignore it, and older grounds never send it, so adding
# one needs no protocol version change. A ground sends one only to an edge
# that has advertised the feature (see ``docs/detection-profiles.md``).
SET_DETECTION_PROFILE = "set_detection_profile"
CONFIG_COMMAND_TYPES = frozenset({SET_DETECTION_PROFILE})
# Why an edge refused a ``set_detection_profile``. ``in_flight`` means the
# drone is flying or a flight sequence (takeoff, lanes, landing) is under
# way; profiles change only on the ground.
PROFILE_NACK_REASONS = frozenset({"in_flight", "unknown_profile", "unavailable", "load_failed", "busy", "invalid"})
# Why an edge loaded another profile than the one requested.
PROFILE_FALLBACK_REASONS = frozenset({"weights_missing"})
# A detection profile name: lowercase letters, digits and underscores,
# starting with a letter, at most 32 characters (``is_profile_name``).
MAX_PROFILE_NAME_LEN = 32
_PROFILE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")

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
    # The edge ``hello``'s hash of its start-time resolution and the
    # telemetry return object, both optional and ignored by older grounds.
    "unit_rev",
    "rth",
})

GROUND_ROLES = frozenset({"leader", "standby"})
EDGE_ROLE = "edge"
HOLD_REASONS = frozenset({"alert", "operator", "handover"})
ON_COMPLETE_ACTIONS = frozenset({"hover", "land"})
# Protocol v2 adds ``return_home`` after the last lane, with
# ``hover_timeout_s`` as the re-task window (at least
# ``MIN_RETURN_HOVER_TIMEOUT_S``, in ``returns.py``). A v1 link keeps
# ``hover`` and ``land``.
ON_COMPLETE_ACTIONS_V2 = ON_COMPLETE_ACTIONS | frozenset({"return_home"})
# The autopilot's link-loss action, reported in ``describe_unit`` and in the
# ``rth`` telemetry object (B-3). ``unknown`` counts as ``return``.
AP_LINK_LOSS_ACTIONS = frozenset({"return", "land", "unknown"})
# The return states of the ``rth`` telemetry object (design section 10.3).
RTH_STATES = frozenset({
    "flying",
    "holding",
    "rth_returning",
    "rth_waiting",
    "rth_above_home",
    "rth_landing",
    "landed",
    "landed_in_place",
    "external_control",
    "cannot_be_recalled",
})
# Why a drone lands in place instead of returning: the closed list of design
# section 10.5.
RTH_REASONS = frozenset({
    "critical_battery",
    "battery_cannot_reach_home",
    "fault_imu",
    "fault_motor",
    "fault_positioning",
    "fault_unclassified",
    "way_home_blocked_obstacle",
    "way_home_blocked_person",
    "no_known_home",
    "uncertainty_above_bound",
    "no_cleared_route",
    "cannot_be_commanded",
    "operator_land",
    "land_all",
    "on_find_land_all",
    "ceiling_breach",
    "no_return_order",
})
# Kinds of no-entry area (design section 11.7).
NO_ENTRY_KINDS = frozenset({"person_cue", "lost_drone", "stranded_drone", "operator"})
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
#   is at most ``MAX_TEXT_LEN`` characters, every list is bounded, every id
#   (drone ids included) must match ``_ID_RE``, and a ``node_state`` is at
#   most ``MAX_NODE_STATE_BYTES`` as compact JSON. So the handover snapshot
#   has no field for a crop, a thumbnail or an embedding, and the size cap
#   bounds what a sender could deliberately encode in ids and numbers; it
#   does not make such encoding impossible.
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
# Return-command wire sanity limits (v2). Each bounds what a malformed or hostile peer can send;
# none is a flight parameter.
# Shortest re-task window of an assign_lanes with on_complete return_home: wire sanity limit, not a flight parameter.
MIN_RETURN_HOVER_TIMEOUT_S = 1.0
# Most points in a return corridor: wire sanity limit, not a flight parameter.
MAX_CORRIDOR_POINTS = 256
# Most points in one area polygon: wire sanity limit, not a flight parameter.
MAX_AREA_POINTS = 64
# Most no-entry areas in one command: wire sanity limit, not a flight parameter.
MAX_NO_ENTRY_AREAS = 32
# Fastest speed a command may carry, in cm/s: wire sanity limit, not a flight parameter.
MAX_SPEED_CM_S = 2_000
# Longest ordered wait, in seconds: wire sanity limit, not a flight parameter.
MAX_RETURN_WAIT_S = 3_600.0
# Longest landing spacing, in seconds: wire sanity limit, not a flight parameter.
MAX_LANDING_INTERVAL_S = 300.0
# Longest validity of one escort setpoint, in seconds: wire sanity limit, not a flight parameter.
MAX_SETPOINT_VALID_S = 10.0
# Top-level fields of one message. An edge's telemetry is the executor's own
# fields (GPS, the drone link and the obstacle fields, the re-look and
# sidestep group ``obstacle_avoid`` included; a Tello has no GPS but adds its
# temperature as one grouped field, ``temp_preflight``) plus the ones the edge
# adds on the way out (``detector_ok``, ``camera_ok`` and the grouped
# ``detection_profile``). With every optional group the producers can add the
# worst case is 40 fields, and 41 with the v2 ``rth`` group. The limit leaves
# headroom above that; a new optional field belongs in a group.
# ``tests/test_telemetry_field_cap.py`` and
# ``tests/test_fleet_protocol_rth_telemetry.py`` compute the count from the
# producers and guard it. Raising it only loosens what a ground accepts, so an
# older edge's messages are unchanged.
MAX_FIELDS = 48
MAX_DEPTH = 8
MAX_NODES = 10_000
# Fleets have at most eight drones, and every SITL scenario uses at most
# eight vehicles; raising the cap is out of scope (N-9).
MAX_ROSTER = 8
MAX_DETECTIONS = 128
# Compact JSON size of one ``node_state`` (and so of a ``handover_offer``
# snapshot). A two-drone state with 128 detections is about 15 KB; the cap
# bounds how much data a peer message could hide in ids and numbers.
MAX_NODE_STATE_BYTES = 16 * 1024
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
# Optional telemetry fields, v1 and v2, sent by an edge whose drone controller
# reports its link (a Tello): ``drone_link_ok`` (boolean), ``drone_link_age_s``
# (seconds since the drone last sent state, or null when unknown) and
# ``drone_link_detail`` (operator text while the link is down, else null).
# While ``drone_link_ok`` is false, ``battery`` and ``height_cm`` are the last
# values the drone reported, not fresh ones. Checked when present; older
# grounds ignore them.
TELEMETRY_DRONE_LINK_FIELDS = ("drone_link_ok", "drone_link_age_s", "drone_link_detail")

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
_KNOWN_TYPES = (
    GROUND_TO_EDGE_TYPES_V2 | EDGE_TO_GROUND_TYPES_V2 | PEER_TYPES | CONFIG_COMMAND_TYPES | RETURN_COMMAND_TYPES
)
