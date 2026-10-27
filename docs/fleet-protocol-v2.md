# Fleet protocol v2 reference

This is the reference for the JSON control messages that travel between the
ground stations and the edge devices of a FlightRisk fleet, and between two
ground stations over the peer link. The code that defines and checks them is
`flightrisk/fleet_protocol/` (a package; every name is importable from
`flightrisk.fleet_protocol`), and its tests are `tests/test_fleet_protocol.py`
(v1), `tests/test_fleet_protocol_v2.py` (v2) and
`tests/test_fleet_protocol_v2_hardening.py` (fail-safe decoding, privacy,
plan ordering and the v2-only primitives).

Protocol v2 belongs to the two-laptop plan (two laptops, two Tellos, one leader
dashboard over a Thunderbolt link). This document covers only the protocol
layer: message schemas, validation, version negotiation, compatibility rules
and the meaning of each field. The components that act on the messages (the
edge authority, the lane executor changes and the leader/peer link) arrive in
later pull requests, and the obligations they must meet are listed in
"What PR-4/5 consumers must do" below.

## Contents

- Versions and negotiation
- Message table
- Fencing and the fail-safe exemption
- Command ids and idempotency
- Acks and nacks
- Compatibility with v1 peers
- Caps and strictness
- Privacy
- What PR-4/5 consumers must do
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
- **Refusing v1.** Two primitives let a v2-only deployment refuse v1:
  - `negotiate_proto(peer_proto, local_min=2)` raises `ProtocolError` when
    the peer announces `proto: 1`; the receiver closes the connection.
  - `validate_ground_to_edge(msg, proto=..., require_fence=True)` (also
    accepted by `validate_for_direction` and `decode`) refuses unfenced
    motion and control (`takeoff`, `assign_lanes`, `start_at`, `hold`,
    `resume`, `nudge`, `handover`, `heartbeat`, `clock`) whatever the link
    version, so a ground that never sends `hello`, or whose first message is
    a bare `heartbeat`, cannot move the drone or feed its watchdog. Those
    types are then checked under the full v2 rules, not only for the fence,
    so a fenced `assign_lanes` without `plan_id` or `plan_seq`, or a fenced
    `start_at` without `plan_id`, is refused too. Landing is still accepted
    unfenced.

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
| `assign_lanes` | v1 | `drone_id`, `alt_cm`, `lanes`, `zone`, **`plan_id`** (required in v2), **`plan_seq`** (integer from 1, required in v2; see "Plan ordering"), **`on_complete`** (`hover` or `land`, optional), **`hover_timeout_s`** (only with `hover`, above 0 and at most 120; when absent the edge uses 20), **`epoch`**, **`leader_id`**, **`cmd_id`** | yes | yes | yes, same `cmd_id`, `plan_id` and `plan_seq` |
| `start_at` | v1 | `t_ground_s`, **`plan_id`** (required in v2), **`epoch`**, **`leader_id`**, **`cmd_id`** | yes | yes | no |
| `takeoff` | v1 | **`epoch`**, **`leader_id`**, **`cmd_id`** | yes | yes | no |
| `hold` | v2 | `epoch`, `leader_id`, `cmd_id`; optional `reason` (`alert`, `operator` or `handover`) | yes | yes | yes |
| `resume` | v2 | `plan_id` (required; the edge answers a plan it does not hold with `nack{reason: plan_mismatch}`), `epoch`, `leader_id`, `cmd_id` | yes | yes | **no** |
| `nudge` | v2 | `direction` (`forward`, `back`, `left` or `right`), `distance_cm` (integer from 20 to 500), `epoch`, `leader_id`, `cmd_id` | yes | yes | no |
| `handover` | v2 | `to_node` (not the sender), `epoch_next` (exactly `epoch + 1`, as in `handover_offer`), `epoch`, `leader_id`, `cmd_id` | yes | yes | yes |
| `land` | v1 | optional `cmd_id`, `epoch`, `leader_id` (informational only) | **never** | optional | yes |
| `emergency_land_all` | v1 | unchanged; optional fields are ignored | **never** | optional | yes |

The legacy `set_target` and `stream_video` messages also travel from ground
to edge. They are not fleet control messages: the strict validators let them
through by type, and their existing handlers check them. `decode` and
`encode` allow them up to `LEGACY_MAX_BYTES` (1 MiB, the WebSocket frame
limit), not the 64 KiB fleet limit (see "Caps and strictness").

### Edge to ground

| Type | Since | Fields (v2 additions in bold) |
|---|---|---|
| `hello` | v2 | `role` `edge` (optional, the only allowed value), `drone_id`, `node_id`, `proto`, `epoch_seen` (0 when no leader has been obeyed yet), `plan_id` (or null), `lane_index` (or null), `is_flying`, `battery` (0 to 100, or null) |
| `pong` | v1 | `t1`, `t2`, `t3` |
| `telemetry` | v1 | all v1 fields, plus **`epoch`** (the edge's `epoch_seen`), **`plan_id`** (or null), **`state`** (`idle`, `taking_off`, `hovering`, `flying_lanes`, `holding` or `landing`) and **`plan_valid`**, all four required in v2 |
| `ack` | v1 | `for`, `drone_id`, **`cmd_id`** (required in v2 except for a landing that carried none), **`result`** (`ok`, required in v2), optional **`epoch_seen`**, **`plan_id`**, **`detail`**. An ack for `start_at` still needs `executed_at_ground_s`; an ack for `assign_lanes` still needs `battery` and `lanes_total` |
| `nack` | v1 | `for`, `drone_id`, `reason`, **`cmd_id`** (required in v2), optional **`epoch_seen`**, **`plan_id`**, **`detail`** (printable text up to 200 characters). A nack for `assign_lanes` still needs `lanes_total` |

The legacy `detections` message (crops, embeddings, thumbnails) also travels
from edge to ground and is let through by type only. A real one is often
over 64 KiB (two detections with crops and a thumbnail come to about
115 KB), so `decode` and `encode` allow it up to `LEGACY_MAX_BYTES` (1 MiB,
the WebSocket frame limit), as the pre-v2 transport did.

### Ground to ground (peer link, v2 only)

| Type | Fields |
|---|---|
| `node_state` | Only these fields, at every level (see "Privacy"): `node_id`, `role`, `epoch`, `proto`, `roster` (at most 8 entries of `drone_id`, `node_id`, `connected`, `is_flying`, `epoch_seen`), `mission` (or null: `plan_id`, `pattern`, `state` one of `idle`, `planned`, `running`, `holding`, `complete`, `area_cm` [w, h], `lkp_cm` [x, y] or null, `placements` mapping a drone id to [x, y, heading]), `latch` (or null: `phase` one of `searching`, `awaiting_response`, `resolved`, and `alert_id`), `detections` (at most 256 entries of `id`, `x_cm`, `y_cm`, `drone_ids`, `first_seen_s`) |
| `handover_offer` | `epoch_next`, `snapshot` (the leader's `node_state`; its role must be `leader` and `epoch_next` must equal its epoch plus 1) |
| `handover_accept` | `epoch_next` |
| `handover_reject` | `reason` (printable text up to 200 characters), optional `epoch_next` |
| `peer_heartbeat` | `seq`, `epoch`, optional `node_id` |

The peer link's auth message (`{"type": "auth", "token": ..., "role": "peer",
"node_id": ...}`) belongs to the transport and is not defined here.

## Fencing and the fail-safe exemption

- **Epoch.** An `epoch` is an integer from 1 to 2^53 - 1 (`MAX_EPOCH`, the
  largest integer every JSON implementation represents exactly). A leader's
  first epoch is 1, because a new leader takes
  `next_epoch(max(stored, highest reported))` and nothing stored counts as
  0. `epoch_seen` and a standby's `epoch` may be 0.
- **The last epoch.** A `hello` or fence at `MAX_EPOCH` is valid, but no
  successor exists: `next_epoch(MAX_EPOCH)`, and so `make_handover` and a
  takeover, raise `ProtocolError` with instructions for the operator (land
  every drone, stop both ground stations and every edge, reset the stored
  leader epoch, start again). A real fleet never gets near it, because the
  epoch grows by one per leadership change; reaching it means a bug or a
  hostile ground, and the consumer must guard against acting on it (see
  "What PR-4/5 consumers must do").
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
  other message. They also accept them whatever protocol version they are
  given: `validate_message`, `validate_ground_to_edge`,
  `validate_for_direction`, `shape_for_peer` and `decode` check for a
  landing before they check `proto` (or `peer_proto`), so an invalid value
  such as `None`, `0`, `"2"`, `2.0` or `True` refuses every other message
  but never a landing. As a second guard, `decode` in the ground-to-edge
  direction returns a strictly parsed landing even if validation fails.
  The rule is that fencing applies to anything that moves a
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
  `land`, `emergency_land_all`, `hold`, `assign_lanes` and `handover`
  (`RETRYABLE_COMMAND_TYPES`). It never resends `takeoff`, `start_at` or
  `nudge` automatically, because a duplicate that arrives after the cache
  entry has been evicted would act twice. It never resends `resume`
  either: a resume retried after a newer `hold` would set the drone moving
  again.
- **Newer commands cancel older retries.** A newer `hold`, `assign_lanes`
  or `land` for a drone cancels the ground's pending retries of
  `assign_lanes` (and any queued, not yet sent `resume`) for that drone, and
  an `emergency_land_all` cancels every pending retry of a motion command
  for every drone. A `hold` may still be retried after a later `resume`
  was sent; that is safe, because holding is the stopped state and the
  operator sees the drone as holding.

### Plan ordering

- **`plan_seq`.** Every v2 `assign_lanes` carries `plan_seq`, an integer
  from 1 to 2^53 - 1 that the leader increases with each new plan it issues
  within its epoch. A retry of the same plan reuses its `cmd_id`,
  `plan_id` and `plan_seq`.
- **Edge rule.** The edge orders plans by `(epoch, plan_seq)`. It refuses
  an `assign_lanes` that is older than its current plan with
  `nack{reason: stale_plan}`, and one with the same `(epoch, plan_seq)` but
  a different `plan_id` with `nack{reason: plan_mismatch}`. A new leader
  needs no knowledge of the old leader's `plan_seq`, because its epoch is
  higher.
- **Why.** Without an order, an older `assign_lanes` that is retried or
  delayed (for example across a reconnect) could replace a newer plan.
  `plan_seq` is a v2 field: `shape_for_peer` removes it for a v1 edge, and
  a v1 link checks it only when present.
- **Lifetime.** The cache lives in the edge process and survives reconnects.
  It does not survive an edge restart, but a restarted edge has already
  landed its drone.

## Acks and nacks

- **Generic form.** In v2 every fenced command is answered with
  `{"type": "ack", "for": <type>, "cmd_id": ..., "drone_id": ..., "result": "ok"}`
  or a `nack` with a `reason`, and the ground correlates by `cmd_id`.
- **Nack reasons.** v1 keeps `geofence`, `altitude`, `battery`,
  `other_drone` and `invalid`. v2 adds `stale_epoch`, `not_authority`,
  `no_valid_plan`, `busy`, `landing`, `not_flying`, `plan_mismatch` and
  `stale_plan`.
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
  argument applies the v1 rules and still passes unknown types through.
  Realistic v1 messages (everything the v1 builders, the lane executor and
  the dashboard produce) validate unchanged, so existing callers in
  `transport.py`, the dashboard and the coordinator are unaffected. It is
  not byte-for-byte the old function: it adds the caps under "Caps and
  strictness" and the privacy screen under "Privacy", so a message with,
  for example, an extra `frame` key, a list of 20 numbers or nesting deeper
  than 8 levels is now refused, and it fixes two crashes on malformed input
  (see below). `validate_message(msg, proto=2)` applies the v2 rules. `validate_ground_to_edge`,
  `validate_edge_to_ground` and `validate_peer_message` additionally reject
  unknown and misdirected types. `decode` and `encode` wrap them with JSON
  parsing, serialisation and size limits.
- **Builders.** Every existing v1 builder keeps its signature (new
  parameters are keyword-only and optional) and, without a fence, its exact
  output. The v2 form is produced by passing `fence=Fence(epoch, leader_id)`
  (and `plan_id=` and `plan_seq=` where needed); a fresh `cmd_id` is
  generated unless one is passed for a retry.
- **Downgrade protection.** An edge must not accept unfenced motion commands
  on any connection once it has adopted a v2 epoch, and must accept them on a
  v1 link only in the single-ground legacy configuration. The protocol
  provides the primitives (`local_min`, `require_fence`; see "Versions and
  negotiation"); using them is the edge authority's job (later pull
  request).

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
  | Encoded fleet message (`decode`/`encode`) | 64 KiB (a ground-to-edge landing over it goes through the 1 MiB fail-safe fallback) |
  | Encoded legacy message (`detections` from an edge, `set_target` or `stream_video` from a ground) | 1 MiB (`LEGACY_MAX_BYTES`, the WebSocket frame limit; a larger caller `max_bytes` raises it) |
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

## What PR-4/5 consumers must do

The protocol layer defines and checks messages; it does not act on them.
The edge authority (PR-4), the executor (PR-5) and the leader and peer link
must meet these rules, which follow from the protocol and from the review of
this layer.

- **Multi-ground mode is v2-only.** In multi-ground mode the edge treats
  unfenced motion as `not_authority` regardless of the link version
  (`validate_ground_to_edge(..., require_fence=True)`), and both sides
  refuse v1 links (`negotiate_proto(..., local_min=2)`, and no `hello`
  means v1) unless `FLIGHTRISK_FLEET_LEGACY=1` is set.
- **Validate with the agreed version, on both sides.** Pass the
  connection's agreed `proto` into `transport.py` on the ground and on the
  edge. Today `EdgeTransport.send` validates with the v1 default, so a v2
  nack for `hold` or `resume` raises `ProtocolError` there, and
  `LaneExecutor._emit` swallows the error, so the nack is silently lost.
  The ground likewise drops a v2 nack it validates as v1.
- **Use the fail-safe decoder.** An edge that parses frames itself must
  keep the landing guarantee: use `decode` in the ground-to-edge direction
  (or `failsafe_type_of` when its own parse fails), never a strict parse
  alone. `decode` can take every frame of the shared socket: the legacy
  `detections`, `set_target` and `stream_video` messages pass by type in
  their own direction up to 1 MiB, so routing them through it does not
  refuse a large `detections` frame. They are still parsed strictly, so a
  legacy sender must not put `NaN` or repeated keys in them.
- **Record `cmd_id` on arrival.** The edge records a command's `cmd_id` as
  pending when it arrives, before executing it, so a retry that arrives
  while the first copy is still running is recognised as a duplicate and
  not executed twice.
- **Correlate acks by drone and command.** The ground correlates acks and
  nacks by `(drone_id, cmd_id)`, not by `cmd_id` alone, because a broadcast
  `hold` sends the same `cmd_id` to every drone.
- **Only the authority commands and feeds the watchdog.** Only the
  authority may send `set_target`, `stream_video` and `ping` (they carry no
  fence, so the edge checks the connection), and only the authority's
  messages feed the link watchdog (plan section 2.3). A landing from any
  authenticated ground is executed but does not feed the watchdog. A v1
  edge feeds the watchdog on every message, including a `hello` it does not
  understand; that is the existing v1 behaviour and is safe only while
  there is a single ground.
- **Check `leader_id` against the connection.** An edge checks that a
  message's `leader_id` equals the `node_id` that the same connection
  announced in its `hello`.
- **Fence `clock`.** In v2, `GroundTransport.sync_clock` must send a fenced
  `clock` (`make_clock(offset, fence=...)`).
- **Supersede pending motion on landing.** Add `resume` and `nudge` to
  `_SUPERSEDED_BY_EMERGENCY` in `transport.py` (it holds `takeoff` and
  `start_at` today), so that an `emergency_land_all` also discards those if
  they are still queued.
- **Retries.** Follow "Automatic retries" and "Newer commands cancel older
  retries" above: never retry `resume`, and cancel pending `assign_lanes`
  retries for a drone when a newer `hold`, `assign_lanes` or `land` is sent
  to it. The edge refuses an older plan with `stale_plan`.
- **`hold` to a v1 edge.** `shape_for_peer` raises for `hold` (and the
  other v2-only commands) to a v1 edge, so in legacy mode the leader needs a
  fallback, such as landing that drone or reporting it as unable to hold.
- **v1 edge quirks.** A v1 edge drops `on_complete` silently and hovers
  after its last lane. A v2 edge treats a missing `hover_timeout_s` with
  `on_complete: "hover"` as 20 s (`DEFAULT_HOVER_TIMEOUT_S`).
- **Late edge `hello`.** A ground that has already treated an edge as v1
  after `HELLO_TIMEOUT_S` (2 s) closes the connection when an edge `hello`
  arrives later, because the version cannot change on a live connection.
  The edge reconnects and negotiates afresh.
- **Guard `MAX_EPOCH`.** Use `next_epoch` to compute every new epoch, and
  surface its error to the operator. An edge should not adopt an epoch
  close to `MAX_EPOCH` from a ground it has not been told to expect,
  because no leader could follow it.

## Differences from the plan text

- **`role: "edge"`.** The edge `hello` carries `"role": "edge"`, so the two
  `hello` forms are told apart by `role` alone. A `hello` with no `role` is
  still read as an edge `hello`, as in the plan.
- **`clock` is fenced.** The plan does not list it, but its offset decides
  when a `start_at` fires.
- **`taking_off` state.** Telemetry `state` adds `taking_off`, so the few
  seconds of a takeoff have a state of their own.
- **Extra fields.** These are additions the plan does not list: `resume`
  must carry `plan_id`; `assign_lanes` must carry `plan_seq` in v2, with
  the nack reason `stale_plan`; acks and nacks may carry `epoch_seen`,
  `plan_id` and `detail`; `peer_heartbeat` may carry `node_id`; `land` may
  carry an informational fence.
- **`resume` is not retried.** The plan's routing table marks `hold` and
  `resume` as retried by `cmd_id`. Only `hold` is.
- **No nack for landing.** A nack for `land` or `emergency_land_all` is
  invalid, which the plan implies but does not state.
- **Ground speaks first.** The edge sends its `hello` in reply to the
  ground's, not unprompted on connect. A v1 ground therefore never sees a
  message type it does not know.
