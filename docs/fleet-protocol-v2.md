# Fleet protocol v2 reference

This is the reference for the JSON control messages that travel between the
ground stations and the edge devices of a FlightRisk fleet, and between two
ground stations over the peer link. The code that defines and checks them is
`flightrisk/fleet_protocol.py`, and its tests are `tests/test_fleet_protocol.py`
(v1) and `tests/test_fleet_protocol_v2.py` (v2).

Protocol v2 belongs to the two-laptop plan (two laptops, two Tellos, one leader
dashboard over a Thunderbolt link). This document covers only the protocol
layer: message schemas, validation, version negotiation, compatibility rules
and the meaning of each field. The components that act on the messages (the
edge authority, the lane executor changes and the leader/peer link) arrive in
later pull requests, and the obligations they must meet are listed in
"Receiver obligations" below.

## Contents

- Versions and negotiation
- Message table
- Fencing and the fail-safe exemption
- Command ids and idempotency
- Acks and nacks
- Compatibility with v1 peers
- Caps and strictness
- Privacy
- Receiver obligations for later pull requests
- Differences from the plan text

## Versions and negotiation

- **Version numbers.** v1 is the hackathon fleet protocol. v2 is this one
  (`PROTOCOL_VERSION = 2`). A link that never exchanged a `hello` is v1.
- **Ground speaks first.** A v2 ground sends a ground `hello`
  (`role` `leader` or `standby`) as its first message on every edge
  connection, right after the auth handshake, and sends it again whenever its
  role or epoch changes (for example when it becomes leader after a handover).
- **Edge replies.** A v2 edge answers every ground `hello` with an edge
  `hello`, and never sends a `hello` before it has received one. A v1 ground
  therefore never receives a `hello`.
- **Agreed version.** Both sides use `negotiate_proto`: the agreed version is
  the lower of the two announced versions, and anything below 1 or above 255
  is rejected as garbage.
- **No change on a live connection.** The version agreed by the first `hello`
  holds for the life of the connection. A later `hello` that would change it
  is a protocol error, and the receiver should close the connection. This
  stops a downgrade from the fenced v2 rules to the unfenced v1 rules in the
  middle of a mission.
- **Timeouts.** A v2 ground that receives no edge `hello` within
  `HELLO_TIMEOUT_S` (2 s) treats that edge as v1. A v2 edge whose first fleet
  message on a connection is anything other than a ground `hello` (a `ping`
  excepted) treats that connection as v1.
- **Legacy mode.** Per the plan, the leader refuses to plan a mission that
  includes a v1 edge unless `FLIGHTRISK_FLEET_LEGACY=1` is set. That rule is
  implemented with the leader, not here.

## Message table

"Fenced" means the message must carry `epoch` and `leader_id` under v2.
"cmd_id" means it must also carry a command id under v2. All v1 fields are
kept; v2 fields are in addition to them.

### Ground to edge

| Type | Since | Fields (v2 additions in bold) | Fenced | cmd_id | Auto-retry |
|---|---|---|---|---|---|
| `hello` | v2 | `role` (`leader` or `standby`), `node_id`, `epoch` (at least 1 for a leader, 0 or more for a standby), `proto`; optional `leader_id` (must equal `node_id` for a leader) | no | no | not a command |
| `ping` | v1 | `t1` | no | no | not a command |
| `clock` | v1 | `offset_s`, **`epoch`**, **`leader_id`** | yes | no | not a command |
| `heartbeat` | v1 | `seq`, **`epoch`**, **`leader_id`** | yes | no | not a command |
| `assign_lanes` | v1 | `drone_id`, `alt_cm`, `lanes`, `zone`, **`plan_id`** (required in v2), **`on_complete`** (`hover` or `land`, optional), **`hover_timeout_s`** (only with `hover`, above 0 and at most 120, default 20), **`epoch`**, **`leader_id`**, **`cmd_id`** | yes | yes | yes, same `cmd_id` and `plan_id` |
| `start_at` | v1 | `t_ground_s`, **`plan_id`** (required in v2), **`epoch`**, **`leader_id`**, **`cmd_id`** | yes | yes | no |
| `takeoff` | v1 | **`epoch`**, **`leader_id`**, **`cmd_id`** | yes | yes | no |
| `hold` | v2 | `epoch`, `leader_id`, `cmd_id`; optional `reason` (`alert`, `operator` or `handover`) | yes | yes | yes |
| `resume` | v2 | `epoch`, `leader_id`, `cmd_id`; optional `plan_id` (resume only if the edge still holds that plan) | yes | yes | yes |
| `nudge` | v2 | `direction` (`forward`, `back`, `left` or `right`), `distance_cm` (integer from 20 to 500), `epoch`, `leader_id`, `cmd_id` | yes | yes | no |
| `handover` | v2 | `to_node` (not the sender), `epoch_next` (above `epoch`; the handover flow uses `epoch + 1`), `epoch`, `leader_id`, `cmd_id` | yes | yes | yes |
| `land` | v1 | optional `cmd_id`, `epoch`, `leader_id` (informational only) | **never** | optional | yes |
| `emergency_land_all` | v1 | unchanged; optional fields are ignored | **never** | optional | yes |

The legacy `set_target` and `stream_video` messages also travel from ground
to edge. They are not fleet control messages: the strict validators let them
through by type, and their existing handlers check them.

### Edge to ground

| Type | Since | Fields (v2 additions in bold) |
|---|---|---|
| `hello` | v2 | `role` `edge` (optional, the only allowed value), `drone_id`, `node_id`, `proto`, `epoch_seen` (0 when no leader has been obeyed yet), `plan_id` (or null), `lane_index` (or null), `is_flying`, `battery` (0 to 100, or null) |
| `pong` | v1 | `t1`, `t2`, `t3` |
| `telemetry` | v1 | all v1 fields, plus **`epoch`** (the edge's `epoch_seen`), **`plan_id`** (or null), **`state`** (`idle`, `taking_off`, `hovering`, `flying_lanes`, `holding` or `landing`) and **`plan_valid`**, all four required in v2 |
| `ack` | v1 | `for`, `drone_id`, **`cmd_id`** (required in v2 except for a landing that carried none), **`result`** (`ok`, required in v2), optional **`epoch_seen`**, **`plan_id`**, **`detail`**. An ack for `start_at` still needs `executed_at_ground_s`; an ack for `assign_lanes` still needs `battery` and `lanes_total` |
| `nack` | v1 | `for`, `drone_id`, `reason`, **`cmd_id`** (required in v2), optional **`epoch_seen`**, **`plan_id`**, **`detail`** (printable text up to 200 characters). A nack for `assign_lanes` still needs `lanes_total` |

The legacy `detections` message (crops, embeddings, thumbnails) also travels
from edge to ground and is let through by type only.

### Ground to ground (peer link, v2 only)

| Type | Fields |
|---|---|
| `node_state` | `node_id`, `role`, `epoch`, `proto`, `roster` (at most 8 entries of `drone_id`, `node_id`, `connected`, `is_flying`, `epoch_seen`), `mission` (or null: `plan_id`, `pattern`, `state` one of `idle`, `planned`, `running`, `holding`, `complete`, `area_cm` [w, h], `lkp_cm` [x, y] or null, `placements` mapping a drone id to [x, y, heading]), `latch` (or null: `phase` one of `searching`, `awaiting_response`, `resolved`, and `alert_id`), `detections` (at most 256 entries of `id`, `x_cm`, `y_cm`, `drone_ids`, `first_seen_s`) |
| `handover_offer` | `epoch_next`, `snapshot` (the leader's `node_state`; its role must be `leader` and `epoch_next` must equal its epoch plus 1) |
| `handover_accept` | `epoch_next` |
| `handover_reject` | `reason` (printable text up to 200 characters), optional `epoch_next` |
| `peer_heartbeat` | `seq`, `epoch`, optional `node_id` |

The peer link's auth message (`{"type": "auth", "token": ..., "role": "peer",
"node_id": ...}`) belongs to the transport and is not defined here.

## Fencing and the fail-safe exemption

- **Epoch.** An `epoch` is an integer from 1 to 2^53 - 1 (the largest integer
  every JSON implementation represents exactly). A leader's first epoch is
  1, because a new leader takes `max(stored, highest reported) + 1` and
  nothing stored counts as 0. `epoch_seen` and a standby's `epoch` may be 0.
- **Fence pair.** `epoch` and `leader_id` always travel together. A message
  with one and not the other is malformed under every version.
- **Fenced commands.** `takeoff`, `assign_lanes`, `start_at`, `hold`,
  `resume`, `nudge` and `handover` can move a drone or change who commands
  it. In v2 each must carry `epoch`, `leader_id` and `cmd_id`. The four
  v2-only commands carry them under every version.
- **Fenced control.** `heartbeat` and `clock` must carry `epoch` and
  `leader_id` in v2. A heartbeat feeds the edge's link watchdog, and a clock
  offset decides when a later `start_at` fires, so a stale ground must not be
  able to send either. They carry no `cmd_id` because they are not acked.
- **Unfenced.** `ping` is a read-only probe and is not fenced. `hello` carries
  the epoch it announces but is not itself a command.
- **Fail-safe exemption.** `land` and `emergency_land_all` are never fenced.
  The validators accept them whatever else they carry: a stale epoch, a
  malformed `cmd_id`, extra fields, even fields that would be refused on any
  other message. The rule is that fencing applies to anything that moves a
  drone, never to landing. `fence_of` and `cmd_id_of` read the optional
  fields of a landing without ever raising.
- **Fail-safe on the wire.** `decode` in the ground-to-edge direction never
  refuses a landing that the pre-v2 edge executed. The pre-v2 edge ran
  `json.loads` on each WebSocket frame (at most 1 MiB, the `websockets`
  default) and landed when the result was an object whose `type` was a
  landing type. When `decode`'s strict parser rejects an input (repeated
  keys, `NaN`, numbers out of range, invalid UTF-8, more than 64 KiB, and so
  on), it parses the input again with `failsafe_type_of`, which accepts
  everything `json.loads` accepts and also repeated keys, invalid UTF-8,
  huge integers, control characters in strings and a leading BOM. If the
  result is an object whose top-level `type`, or any occurrence of a
  repeated `type` key, is `land` or `emergency_land_all`, `decode` returns
  just `{"type": "land"}` or `{"type": "emergency_land_all"}`
  (`emergency_land_all` wins if both appear). Reading
  `{"type": "heartbeat", "type": "land"}` as a landing is deliberate: it is
  the safe reading of an ambiguous message. The fallback is bounded by
  `FAILSAFE_MAX_BYTES` (1 MiB, or the caller's `max_bytes` if larger); it
  does not accept input that is not one JSON object or that nests deeper
  than the parser's recursion limit, neither of which the pre-v2 edge
  accepted either. A test compares `decode` with the pre-v2 parser on
  thousands of generated and mutated inputs.
- **What the fields mean to the receiver.** Section 2.3 of the plan gives the
  edge's verdicts: adopt a higher epoch only on the ground, accept the
  authority's current epoch, and reject other motion with
  `nack{reason: stale_epoch | not_authority}`. That logic is not in this
  module.

## Command ids and idempotency

- **Shape.** A `cmd_id` is an identifier of 1 to 64 characters from
  letters, digits, `.`, `_`, `:` and `-`, starting with a letter or digit.
  `new_cmd_id()` returns a uuid4 as 32 hex characters.
- **One command, one id.** The sender creates the id once per command. A
  retry of the same command reuses the same id (pass `cmd_id=` to the
  builder). A new attempt after a refusal is a new command with a new id.
- **At most once.** An edge remembers the outcome of at least the last
  `CMD_ID_CACHE_SIZE` (256) command ids, each with the reply it sent. A
  command whose id is in the cache is never executed again; the edge resends
  the cached `ack` or `nack` unchanged.
- **The first outcome is final.** Refusals are cached too. A retry of a
  command that was refused with `busy` gets the same `busy`; to try again
  after the condition clears, the ground sends a new command with a new id.
- **Mismatched reuse.** A cached id arriving with a different `type` is a
  sender bug. The edge should answer `nack{reason: invalid}` and not execute
  it.
- **Landing bypasses the cache.** A `land` or `emergency_land_all` is executed
  every time it arrives, cached id or not, because landing twice is harmless
  and skipping a landing is not.
- **Automatic retries.** The ground may resend, with the same id, only
  `land`, `emergency_land_all`, `hold`, `resume`, `assign_lanes` and
  `handover` (`RETRYABLE_COMMAND_TYPES`). It never resends `takeoff`,
  `start_at` or `nudge` automatically, because a duplicate that arrives after
  the cache entry has been evicted would act twice.
- **Lifetime.** The cache lives in the edge process and survives reconnects.
  It does not survive an edge restart, but a restarted edge has already
  landed its drone.

## Acks and nacks

- **Generic form.** In v2 every fenced command is answered with
  `{"type": "ack", "for": <type>, "cmd_id": ..., "drone_id": ..., "result": "ok"}`
  or a `nack` with a `reason`, and the ground correlates by `cmd_id`.
- **Nack reasons.** v1 keeps `geofence`, `altitude`, `battery`,
  `other_drone` and `invalid`. v2 adds `stale_epoch`, `not_authority`,
  `no_valid_plan`, `busy`, `landing`, `not_flying` and `plan_mismatch`.
- **Stale leaders.** A `stale_epoch` or `not_authority` nack should carry
  `epoch_seen`, so that a stale leader learns the epoch it lost to and steps
  down.
- **Landing is never nacked.** A nack whose `for` is `land` or
  `emergency_land_all` is invalid. An edge may ack a landing.
- **v1 rules kept.** On a v1 link a nack must be for `assign_lanes`, with a
  v1 reason and `lanes_total`, exactly as before.

## Compatibility with v1 peers

| Ground | Edge | What happens |
|---|---|---|
| v1 | v1 | The hackathon flow, unchanged. |
| v2 | v1 | The edge ignores the ground's `hello` (a v1 edge treats any unknown type as a no-op) and never replies, so the ground treats it as v1 after 2 s. The ground then sends only v1-shaped messages through `shape_for_peer(msg, 1)`, which removes the v2 fields and refuses `hold`, `resume`, `nudge`, `handover` and peer messages. A v1 edge would silently ignore those commands, so a `hold` would not hold. A v1 edge also ignores `on_complete` and hovers after its last lane. |
| v1 | v2 | The edge never receives a `hello`, so the link stays v1. The edge sends no `hello`, and only v1-shaped messages: telemetry and acks are supersets that a v1 ground accepts, but a nack must be a v1 nack (lane assignments, v1 reasons). |
| v2 | v2 | Both send `hello`; fenced v2 rules apply from then on. |

- **Validation entry points.** `validate_message(msg)` with no `proto`
  argument behaves exactly as before for v1 messages, including passing
  unknown types through, so existing callers in `transport.py`, the
  dashboard and the coordinator are unaffected. `validate_message(msg,
  proto=2)` applies the v2 rules. `validate_ground_to_edge`,
  `validate_edge_to_ground` and `validate_peer_message` additionally reject
  unknown and misdirected types. `decode` and `encode` wrap them with JSON
  parsing, serialisation and size limits.
- **Builders.** Every existing builder keeps its signature and output. The v2
  form is produced by passing `fence=Fence(epoch, leader_id)` (and
  `plan_id=` where needed); a fresh `cmd_id` is generated unless one is
  passed for a retry.
- **Downgrade protection.** An edge must not accept unfenced motion commands
  on any connection once it has adopted a v2 epoch, and must accept them on a
  v1 link only in the single-ground legacy configuration. This is an
  edge-authority rule (later pull request), recorded here because the
  protocol depends on it.

## Caps and strictness

- **Never anything but `ProtocolError`.** Every validator, `decode`,
  `shape_for_peer`, `parse_hello` and `negotiate_proto` raises
  `ProtocolError` and nothing else, for any input. `fence_of`, `cmd_id_of`
  and `failsafe_type_of` never raise. The tests check this with seeded
  fuzzing.
- **Two v1 crashes fixed.** Before v2, `validate_message` raised `TypeError`
  for a lane `direction` that was a list, and `OverflowError` for a number too
  large for a float. Both are now `ProtocolError`.
- **Size and shape limits.**

  | Limit | Value |
  |---|---|
  | Encoded message (`decode`/`encode`) | 64 KiB |
  | Top-level fields | 32 |
  | Nesting depth | 8 |
  | Values in one message | 10,000 |
  | Identifiers (`drone_id`, `node_id`, `leader_id`, `cmd_id`, `plan_id`, `to_node`) | 64 characters |
  | Free text (`reason` on `handover_reject`, `detail`) | 200 printable characters |
  | Lanes per plan | 1,024 |
  | One lane | 100,000 cm |
  | Roster entries and placements | 8 |
  | Detections in `node_state` | 256 |
  | Any string, key or value, on the peer link | 200 characters |
  | Numbers held in one list / in all lists of a message | 16 / 64 |
  | Any number | finite, magnitude at most 10^15 |
  | Integers (epochs, sequence numbers, counts) | 0 to 2^53 - 1 |

  `drone_id` keeps its v1 rule (any non-empty string) apart from the length
  limit. The v2 identifiers must also match the identifier pattern.
- **Wire format.** `decode` rejects invalid UTF-8, invalid JSON, repeated
  keys, `NaN` and `Infinity`, numbers that overflow a float, integers with
  more digits than Python parses, and nesting too deep for the parser,
  except that a ground-to-edge landing is accepted through the fail-safe
  fallback described under "Fencing and the fail-safe exemption".
- **Unknown fields.** On the edge links, extra fields on a known type are
  allowed, so a later minor addition does not break older receivers, but
  they count toward the limits above and the privacy screen below. On the
  peer link they are refused (see "Privacy").
- **Exempt types.** Landing messages are exempt from every check except
  being an object with a `type`. Over the wire, a landing that fails the
  strict parse (including the 64 KiB limit) goes through the fail-safe
  fallback, which only needs it to be one JSON object of at most 1 MiB.

## Privacy

What is enforced differs by link, and this section states exactly what the
code checks.

- **Peer link: allowlist.** Every ground-to-ground message is checked
  against its schema at every level: the top-level message, each roster
  entry, `mission`, `latch`, each detection and the `handover_offer`
  snapshot. A field that is not in the schema is refused, every string
  (keys included) is at most 200 characters, and every list has a fixed or
  capped length (roster and placements 8, detections 256, a detection's
  `drone_ids` 8, number pairs and triples exact). So a `node_state` or
  handover snapshot can carry only ids, positions, states and times; it has
  no field that could hold a crop, a thumbnail or an embedding.
- **Every fleet message: denylist and payload screen.** All fleet control
  messages, on the edge links and the peer link, are also screened:
  - a key is refused when, after NFKC normalisation and case folding, it is
    in `PRIVACY_FORBIDDEN_KEYS` or contains one of
    `PRIVACY_FORBIDDEN_KEY_PARTS` (`face`, `embed`, `reid`, `vec`,
    `feature`, `biometric`, `crop`, `image`, `img`, `photo`, `picture`,
    `thumb`, `jpeg`, `jpg`, `png`, `frame`, `pixel`, `b64`, `base64`), with
    separators removed, so `image_b64`, `faceEmbedding`, `Face-Embedding`
    and a full-width `ＦＡＣＥ` all match;
  - a string (key or value) is refused when it contains a data URI with a
    media type or a `;base64,` marker, or is longer than 64 characters and
    made only of base64, base64url or hex characters (with optional line
    breaks);
  - a list holding more than `MAX_NUMBERS_PER_LIST` (16) numbers is
    refused, and so is a message whose lists hold more than
    `MAX_LISTED_NUMBERS` (64) numbers in all;
  - a bytes value is refused.
- **What the edge-link screen does not do.** Edge-link messages may carry
  extra fields, so that a later minor addition does not break older
  receivers. The screen therefore guards against accidental leakage (a
  developer adding an `image_b64` or an embedding field), not against a
  sender that deliberately encodes data in permitted fields, for example as
  many short strings or as numbers spread across objects. On the edge links
  the real boundary is that the only senders are this project's builders,
  which cannot add such fields.
- **Opt-in rules unchanged.** Person crops, thumbnails and target embeddings
  travel only in the legacy `detections` and `set_target` messages, under
  their existing rules: appearance-only by default, and face embeddings only
  with `--with-face` or `FLIGHTRISK_APPEARANCE_ONLY=0`. Protocol v2 changes
  none of that.
- **Handover snapshot.** `node_state` (and so the `handover_offer` snapshot)
  carries only ids, positions, states and times, enforced by the peer
  allowlist above. Replicating the target ReID embedding on handover is
  decision D4 in the plan and is not decided. Until it is, both the
  allowlist and the number-list limit refuse it, and enabling it needs an
  explicit schema change here and in `docs/privacy-design.md`.
- **Exception for landing.** A landing message is accepted even if it carries
  a forbidden key, because refusing a landing is the greater harm. The
  receiver uses only its `type`. Senders never add such keys, because the
  builders cannot.

## Receiver obligations for later pull requests

These rules follow from the protocol and must be met by the code that uses it.

- **Validate with the agreed version.** Pass the connection's agreed `proto`
  to `validate_message` or `decode`. Today `transport.py` validates with the
  default (v1). Until the transport passes the agreed version, a v2 nack for
  anything other than `assign_lanes` is dropped at the ground as malformed.
- **Fence `clock`.** In v2, `GroundTransport.sync_clock` must send a fenced
  `clock` (`make_clock(offset, fence=...)`).
- **Check `leader_id` against the connection.** An edge should check that a
  message's `leader_id` equals the `node_id` that the same connection
  announced in its `hello`.
- **Supersede pending motion on landing.** The transport's
  `_SUPERSEDED_BY_EMERGENCY` set (`takeoff`, `start_at`) should grow to
  include `resume` and `nudge`, so that an `emergency_land_all` also discards
  those if they are still queued.
- **Watchdog.** Only the authority's messages may feed the link watchdog
  (plan section 2.3). A v1 edge feeds it on every message, including a
  `hello` it does not understand; that is the existing v1 behaviour and is
  safe while there is a single ground.

## Differences from the plan text

- **`role: "edge"`.** The edge `hello` carries `"role": "edge"`, so the two
  `hello` forms are told apart by `role` alone. A `hello` with no `role` is
  still read as an edge `hello`, as in the plan.
- **`clock` is fenced.** The plan does not list it, but its offset decides
  when a `start_at` fires.
- **`taking_off` state.** Telemetry `state` adds `taking_off`, so the few
  seconds of a takeoff have a state of their own.
- **Optional extra fields.** These are additions the plan does not list:
  `resume` may carry `plan_id`; acks and nacks may carry `epoch_seen`,
  `plan_id` and `detail`; `peer_heartbeat` may carry `node_id`; `land` may
  carry an informational fence.
- **No nack for landing.** A nack for `land` or `emergency_land_all` is
  invalid, which the plan implies but does not state.
- **Ground speaks first.** The edge sends its `hello` in reply to the
  ground's, not unprompted on connect. A v1 ground therefore never sees a
  message type it does not know.
