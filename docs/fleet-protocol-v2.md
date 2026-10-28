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
dashboard over a Thunderbolt link). This document covers mainly the protocol
layer: message schemas, validation, version negotiation, compatibility rules
and the meaning of each field. The components that act on the messages are
the edge authority, the lane executor and the leader/peer link. The
obligations they must meet are listed in "What PR-4/5 consumers must do"
below; the lane executor's side is described in "The edge lane executor
(PR-5)", and the edge authority and the leader/peer link arrive in later pull
requests.

## Contents

- Versions and negotiation
- Message table
- Fencing and the fail-safe exemption
- Command ids and idempotency
- Acks and nacks
- Drone link preflight (takeoff only)
- Compatibility with v1 peers
- Caps and strictness
- Privacy
- What PR-4/5 consumers must do
- Edge authority and multi-ground links (PR-4)
- The edge lane executor (PR-5)
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
  or a landing excepted) treats that connection as v1. A landing that
  arrives first is executed and does not decide the version.
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
| `telemetry` | v1 | all v1 fields, plus **`epoch`** (the edge's `epoch_seen`), **`plan_id`** (or null), **`state`** (`idle`, `taking_off`, `hovering`, `flying_lanes`, `holding` or `landing`) and **`plan_valid`**, all four required in v2. Optional on every version (`TELEMETRY_DRONE_LINK_FIELDS`, see "Drone link preflight"): `drone_link_ok` (boolean), `drone_link_age_s` (seconds since the drone last sent state, non-negative, or null) and `drone_link_detail` (printable text up to 200 characters, or null) |
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
| `node_state` | Only these fields, at every level (see "Privacy"): `node_id`, `role`, `epoch`, `proto`, `roster` (at most 8 entries of `drone_id`, `node_id`, `connected`, `is_flying`, `epoch_seen`), `mission` (or null: `plan_id`, `pattern`, `state` one of `idle`, `planned`, `running`, `holding`, `complete`, `area_cm` [w, h], `lkp_cm` [x, y] or null, `placements` mapping a drone id to [x, y, heading]), `latch` (or null: `phase` one of `searching`, `awaiting_response`, `resolved`, and `alert_id`), `detections` (at most 128 entries of `id`, `x_cm`, `y_cm`, `drone_ids`, `first_seen_s`). Every drone id (roster, placement keys, `drone_ids`) must be an identifier, and the whole message is at most 16 KiB as compact JSON |
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
  `no_valid_plan`, `busy`, `landing`, `not_flying`, `plan_mismatch`,
  `stale_plan` and `drone_unreachable` (the edge cannot talk to its drone,
  so it refuses to take off; see "Drone link preflight").
- **Stale leaders.** A `stale_epoch` or `not_authority` nack should carry
  `epoch_seen`, so that a stale leader learns the epoch it lost to and steps
  down.
- **Landing is never nacked.** A nack whose `for` is `land` or
  `emergency_land_all` is invalid. An edge may ack a landing.
- **v1 rules kept.** On a v1 link a nack must be for `assign_lanes`, with a
  v1 reason and `lanes_total`, exactly as before.
- **Geofence details.** A `geofence` nack for `assign_lanes`, v1 or v2, may
  carry five optional numbers (`NACK_GEOFENCE_FIELDS`), all in cm and
  relative to the drone's takeoff point: `geofence_cm` (the edge's fence
  half-width), `x_cm` and `y_cm` (the first point of the refused path
  outside the fence) and `rel_x_cm` and `rel_y_cm` (where that path starts:
  the drone's dead-reckoned position in the air, or 0, 0 on the ground,
  see "Takeoff point" below). When present each must be a finite number.
  The edge executor always sends them with a geofence refusal of a lane
  plan; the coordinator shows them in the refusal reason, for example
  `refused by drone-1 (geofence: the path would reach (475, 0) cm from
  drone-1's takeoff point, outside its 450 cm geofence; drone-1 is at
  (300, 0) cm from its takeoff point)`. Older grounds ignore them, as they
  ignore any extra field on an edge link.
- **Takeoff point.** The dead-reckoned position, and so the geofence, is
  relative to the spot the drone took off from on its current flight. Every
  takeoff from the ground makes the drone's spot the origin again, just
  before the takeoff is sent (logged as `edge_pose_reset` with where the
  previous flight ended), because a Tello lands where it is and the edge
  cannot see whether the operator carried it back to its start spot. A lane
  plan received while the drone is on the ground is therefore checked from
  0, 0 (the next takeoff point), and one received in the air (a re-task)
  from the dead-reckoned position. After a landing, telemetry keeps
  reporting where the drone landed until the next takeoff. The plan's zones
  are only right if the operator puts every drone back on its start spot
  before each start (`docs/fleet-setup.md`, "Every takeoff is a new start
  point"). Before this change the position was never reset, so each run's
  net displacement accumulated across flights and a later plan or re-task
  could be refused for a position the drone never had.

## Drone link preflight (takeoff only)

Incident of 2026-10-28 (Mac edge-1): the Mac was not on the Tello's Wi-Fi.
For minutes the state stream was silent and every keepalive and stream
recovery failed with `OSError` 51 (network unreachable), but the
edge-to-ground link was fine and telemetry kept the last cached battery, so
the ground thought drone-1 was healthy and sent `start_at`. The edge
accepted it, sent `takeoff` into the dead link (the send raised `OSError`
51), logged `edge_executor_error` and then retried `land` three times, all
unreachable. An earlier run the same morning did the same after a keepalive
went unanswered. The edge now refuses to take off when it cannot talk to
its drone, and says so.

- **Link health.** `TelloController.link_status()` returns a
  `DroneLinkStatus` (`flightrisk/drone/controller.py`): `ok`, `age_s` (age
  of the newest state packet), a bounded `reason` code and an operator
  `detail`. It sends nothing and takes no command lock. It is unhealthy
  when the controller is not connected (`not_connected`), when no state
  packet ever arrived (`no_state`), when none arrived for more than
  `_STATE_LOST_S` = 3 s (`state_stale`, the threshold landing already uses
  for a lost state stream: about 30 missed packets at the Tello's 10 per
  second), or when the last command-channel exchange (keepalive, stream
  command or flight command) raised or got no reply and nothing was
  answered since (`command_failed`). The keepalive runs every 10 s, so a
  recovered link clears within that. No new probe is sent: the existing
  keepalive and the state stream are the evidence. State packets keep
  arriving while a command holds the channel (the state poller skips then,
  the state stream does not), so a drone in the middle of a command is not
  counted as unreachable, and a Tello that has just connected has fresh
  state because `connect()` waits for the first state packet.
- **Who is checked.** Only a controller with `link_status()` (the Tello).
  The fake Tello (`--source fake`) and MAVLink report none and are never
  refused for it. A `link_status()` that raises counts as unhealthy
  (`unreadable`), so the takeoff is refused rather than flown blind.
- **When `start_at` arrives.** A start that would take off (the drone is on
  the ground) is refused at once while the link is unhealthy:
  `nack{reason: drone_unreachable, detail: "Tello unreachable (no state for
  12 s)"}` on a v2 link, and the log event `edge_start_refused` with
  `reason=drone_unreachable` on every link (a v1 link has no `start_at`
  nack). The other `start_at` checks come first.
- **Right before the takeoff.** The link is checked again as the first step
  of the takeoff, after the scheduled wait. If it is unhealthy then, the
  sequence ends with nothing sent to the drone: no takeoff and, because the
  drone never left the ground, no landing to retry. The pose, the plan and
  the land state are left as they were, so a new `start_at` flies once the
  link is back. The edge logs `edge_takeoff_refused` with
  `reason=drone_unreachable`, `link_reason` and `detail`, and answers a v2
  command with the same nack. A standalone `takeoff` command is refused the
  same way.
- **Landing is never gated.** `land`, `emergency_land_all`, the local land
  key, Ctrl+C, the link-loss, battery and geofence landings and the land
  retries never look at the drone link, and nothing about landing, the
  geofence or the safety envelope changed. A flight that did take off keeps
  the existing behaviour: a controller error lands it.
- **Telemetry.** When the controller reports its link, telemetry on every
  version carries `drone_link_ok`, `drone_link_age_s` and
  `drone_link_detail` (null while healthy). While `drone_link_ok` is false,
  `battery` and `height_cm` are the controller's last cached values, not
  fresh ones; the flag says so instead of hiding them. Each field is
  checked when present and absent fields change nothing.
- **Ground.** `FleetCoordinator.start()` refuses a drone whose telemetry
  from the last `TELEMETRY_FRESH_S` (2 s) says `drone_link_ok: false`, with
  the edge's text, so the dashboard shows `Start refused: drone-1: Tello
  unreachable (no state for 12 s)` through the existing `start_refused`
  fleet event, and no `start_at` is sent to any drone (the existing
  all-or-nothing start rule). A `drone_unreachable` nack for `start_at` or
  `takeoff` that arrives after the start is shown the same way
  (`start_refused`, `drone-1: <detail>`); the drone stays on the ground and
  loss detection re-tasks its lanes as for any drone that does not take
  off. `coordinator.status()` (and so `fleet_status`) carries
  `drone_link_ok` and `drone_link_detail`; the dashboard does not display
  them yet.
- **Compatibility.** An older ground ignores the telemetry fields. On a v2
  link it logs a `drone_unreachable` nack as malformed and ignores it; it
  never acted on a `start_at` nack anyway, and the link stays up. An older
  ground therefore does not show the refusal, but the edge still never
  takes off into a dead link. A v1 link gets no nack for `start_at`, only
  the log line; the telemetry flag is how the ground learns of it there.
- **Not verified on hardware.** The thresholds and the incident replay are
  covered by tests on the wire-level Tello fake
  (`tests/test_edge_takeoff_preflight.py`), not on a real Tello.

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
  `ProtocolError` and nothing else, for any input, including in-process
  values that `repr` cannot print (an integer of more than 4300 digits, or
  an object whose `__repr__` raises). `fence_of`, `cmd_id_of` and
  `failsafe_type_of` never raise. The tests check this with seeded fuzzing.
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
  | Detections in `node_state` | 128 |
  | One `node_state` (or `handover_offer` snapshot), as compact JSON | 16 KiB (`MAX_NODE_STATE_BYTES`) |
  | Any string, key or value, on the peer link | 200 characters |
  | Numbers held in one list / in all lists of a message | 16 / 64 |
  | Any number | finite, magnitude at most 10^15 |
  | Integers (epochs, sequence numbers, counts) | 0 to 2^53 - 1 |

  On the edge links `drone_id` keeps its v1 rule (any non-empty string)
  apart from the length limit. The v2 identifiers must also match the
  identifier pattern (`is_identifier`), and so must every drone id on the
  peer link (see "Privacy"). The edge checks its `--drone-id` with
  `is_identifier` at startup and exits with a clear error otherwise, because
  a longer id would make the protocol drop its telemetry and acks and the
  ground refuse to command it.
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
  snapshot. Exactly this is enforced:
  - a field that is not in the schema is refused;
  - every string (keys included) is at most 200 characters;
  - every list has a fixed or capped length (roster and placements 8,
    detections 128, a detection's `drone_ids` 8, number pairs and triples
    exact);
  - every id, including every drone id (roster `drone_id`, placement keys
    and a detection's `drone_ids`), must be an identifier: 1 to 64
    characters from letters, digits, `.`, `_`, `:` and `-`, starting with a
    letter or digit;
  - a `node_state`, and so a `handover_offer` snapshot, is at most
    `MAX_NODE_STATE_BYTES` (16 KiB) as compact JSON. A two-drone state with
    128 detections is about 15 KB; a leader with more to share must send
    the most recent detections that fit.

  So a `node_state` has no field for a crop, a thumbnail or an embedding,
  and the number-list limits below refuse an embedding. This does not make
  it impossible for a sender to hide data deliberately: identifiers use a
  base64url-compatible alphabet and numbers carry bits, so a hostile ground
  could still encode up to a few kilobytes of data in one message. The size
  cap bounds that amount; the boundary that excludes it is that the only
  peer senders are this project's ground stations.
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
  has fields only for ids, positions, states and times, enforced by the
  peer allowlist above (with the limits it states on what a deliberate
  sender could hide). Replicating the target ReID embedding on handover is
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
- **Decode off the event loop.** `decode` is CPU-bound and can take tens
  of milliseconds on a hostile frame near the 1 MiB WebSocket limit. On
  the test machine the strict parse alone took up to about 50 ms (frames
  over 64 KiB are parsed to see whether they are legacy messages), and the
  strict parse followed by the lenient fail-safe parse up to about 65 ms
  (80 ms in review); a busy machine is slower. Run it in a worker thread (`asyncio.to_thread` or an
  executor), not directly on the event loop that also runs the heartbeats
  and the link watchdog, or put a cheap rate or size limit in front of it.
  `failsafe_type_of` already skips the lenient parse when a frame cannot
  name a landing (it contains neither `land` nor a backslash escape and,
  for bytes, no NUL byte, which UTF-16 and UTF-32 text always has), so
  accidental junk costs only the strict parse; a hostile sender can still
  include `land` and pay for both parses.
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
- **Pass `proto=1`, never `None`, before `hello`.** A link that has
  exchanged no `hello` is v1, so pass `proto=1` until the version is agreed.
  This version of the module accepts a landing with any `proto`, but an
  invalid one still refuses every other message, and an edge running a
  build from before this rule refuses a landing given `proto=None`. Keep
  passing `proto=1` until every edge runs this version.
- **A connection's `node_id` never changes.** The `node_id` of the first
  `hello` on a connection holds for its life; a later `hello` with a
  different `node_id` is a protocol error and the receiver closes the
  connection, as for a version change.
- **No `hello`, no motion.** In a v2 deployment an edge refuses motion and
  control from a connection that has not sent a ground `hello`, fenced or
  not, because it has no `node_id` to check the message's `leader_id`
  against. Landing is still executed.
- **Two leaders at one epoch.** Two grounds that both claim the same epoch
  with different `leader_id`s have no tie-break in the protocol. Each edge
  obeys the first authority it accepted at that epoch and refuses the other
  with `nack{reason: not_authority}`; the peer link (which elects one
  leader per epoch) is what resolves the conflict, and the leader that
  loses must step down and land or hand over its drones.
- **The same plan with a new `cmd_id`.** An `assign_lanes` with the same
  `(epoch, plan_seq, plan_id)` as the plan the edge holds but a new
  `cmd_id` is the same plan resent as a new command: the edge acks it with
  the new `cmd_id` and does not restart or re-execute the plan.
- **Plans across a landing.** The edge keeps its last plan's
  `(epoch, plan_seq, plan_id)` across a landing (plan section 2.8), so a
  late or retried copy of that plan cannot restart the drone. After a
  landing the leader must issue a new plan with a new `plan_id` (and a
  higher `plan_seq` in the same epoch); the edge refuses the old plan with
  `nack{reason: stale_plan}` or `plan_mismatch`.
- **Let a landing through the first-message rule, and send `hello` first.**
  The ground sends its `hello` before anything else on a connection and
  must not broadcast `heartbeat` or `clock` to a connection that has not
  yet been sent its `hello`: the edge would take that first message as
  "no `hello`", lock the connection to v1, and refuse the later `hello` as
  a version change. A landing is the exception in both directions: the
  ground may send one at any time, and the edge executes it without letting
  it decide the version.
- **Landing acks clear landing retries.** An ack for `land` or
  `emergency_land_all` may carry no `cmd_id` (a v1 edge, or a landing that
  arrived without one). The ground must still clear its pending landing
  retries for that drone on such an ack.
- **The landing fallback drops `cmd_id`.** A landing that `decode` reads
  through the fail-safe fallback is returned as just `{"type": ...}`, so
  the edge's ack carries no `cmd_id`. With the rule above that is enough;
  a ground that matches landing acks only by `cmd_id` keeps retrying the
  landing until its retry budget runs out, which is harmless.
- **Key names and the privacy screen.** The key-part denylist refuses any
  key that contains a listed part, so some harmless names are refused too:
  `surface` and `interface` (they contain `face`), `frame_id` and
  `keyframe` (`frame`) and `embedded` (`embed`). An edge-to-ground message
  with such a key fails validation, and the edge drops it with only a
  warning, so the telemetry would vanish silently. Check every new field
  name against `PRIVACY_FORBIDDEN_KEYS` and `PRIVACY_FORBIDDEN_KEY_PARTS`
  (a test that builds the message and validates it is enough) before using
  it.
- **Guard `MAX_EPOCH`.** Use `next_epoch` to compute every new epoch, and
  surface its error to the operator. An edge should not adopt an epoch
  close to `MAX_EPOCH` from a ground it has not been told to expect,
  because no leader could follow it.

## Edge authority and multi-ground links (PR-4)

The edge side of these rules is `flightrisk/fleet_authority.py`
(`EdgeAuthority`, no I/O) and `flightrisk/edge_uplink.py` (`EdgeUplink`,
one connection per ground station). The ground side of the version
bookkeeping is `flightrisk/fleet_ground_link.py`, used by `GroundTransport`
when it is given a `GroundIdentity`. Tests: `tests/test_fleet_authority.py`
(rule tests and a model-based random walk), `tests/test_edge_uplink.py` (two
real ground stations and one edge over loopback),
`tests/test_fleet_ground_link.py`, `tests/test_edge_device_uplink.py` and
`tests/test_link_stalls.py` (a dead link must not stall the others, through a
black-hole TCP proxy).

### Turning it on

- **Edge.** A flying edge (`--source tello` or `fake`) with
  `FLIGHTRISK_GROUND_URLS` (or `--ground-urls`) set to a comma-separated
  list, for example `ws://127.0.0.1:9000,ws://10.42.0.2:9000`, connects to
  every listed ground and speaks v2. `FLIGHTRISK_NODE_ID` (or `--node-id`)
  names the laptop in its `hello` (default: the host name); an invalid
  value stops the edge at startup with a usage error. `connect` waits at
  most 2 s for the first attempts, so a ground that does not answer (an
  unplugged link waits for the 10 s open timeout) never delays the start;
  it keeps connecting in the background.
  `FLIGHTRISK_FLEET_LEGACY=1` keeps v1 grounds connected as monitor-only
  links instead of closing them. `FLIGHTRISK_INFLIGHT_HANDOVER=1` enables the
  in-flight adoption rule below (off by default; plan rung L6 first).
- **Single ground, unchanged.** Without a ground list the edge uses
  `--ws-url`/`FLIGHTRISK_EDGE_WS` exactly as before: one connection, protocol
  v1, unfenced commands obeyed. That is the single-ground legacy
  configuration.
- **Ground.** `GroundTransport(identity=GroundIdentity(node_id, role, epoch))`
  speaks v2; `local_min_proto=2` closes v1 edges; `set_identity` announces a
  new role or epoch. Without an identity the ground is unchanged (v1). The
  dashboard does not pass an identity yet; the leader logic (PR-6) will.
- **Today's dashboard is a v1 ground.** Until PR-6 gives the dashboard an
  identity, an edge with `FLIGHTRISK_GROUND_URLS` pointing at it sees a v1
  ground. In strict mode (the default) the edge closes that link at the
  ground's first heartbeat and reconnects after 1 s, then 2, 4 and 8 s, at
  most every 10 s, so the link churns and the drone is never obeyed or fed
  from it; only a landing reaches it while a connection is up. With
  `FLIGHTRISK_FLEET_LEGACY=1` the link stays up as monitor-only (telemetry
  out, landings in). For a single dashboard today, use `--ws-url` instead.
- **The token goes to every listed ground.** The edge sends
  `FLIGHTRISK_EDGE_TOKEN` in the auth handshake to every URL in the list,
  and over `ws://` that is plain text on the link. It cannot tell which
  ground it reached: anything that answers on a listed address receives the
  token and can then send landings (always obeyed) or, by claiming to be a
  leader, try to become the authority. List only addresses on the fleet's
  own link and loopback, and prefer `wss://` where the network is shared.

### Edge rules

- **Landing is never fenced.** `land` and `emergency_land_all` are executed
  from any authenticated ground, at any epoch and on any link version, and
  are never nacked. On a v2 link the edge acks them, without a `cmd_id` when
  the landing carried none (including a landing read by the fail-safe
  decoder). A landing feeds the link watchdog only when it comes from the
  authority. Landing bypasses the command-id cache. A landing also ends the
  plan: `start_at` and `resume` naming it are refused (`no_valid_plan`)
  until a new `assign_lanes` is acked. At most one `edge_landing_command`
  is logged per connection and landing type per second, with the number not
  logged since.
- **Hello first.** A connection's first fleet message must be a ground
  `hello`. Until then the connection cannot move the drone, feed the
  watchdog, ping, set the target or toggle video, and receives nothing. A
  connection whose first fleet message is anything else is a v1 link: it is
  closed (code 4010) unless legacy mode keeps it as a monitor-only link
  (v1-shaped telemetry out, landing in, nothing else obeyed). A later
  `hello` on a v1 link, a `hello` that changes the connection's `node_id`,
  one that changes the agreed version, and, without legacy mode, one that
  announces v1, close the connection. A ping or a landing does not decide
  the version.
- **Decoding.** Every frame goes through `decode` in the ground-to-edge
  direction with the connection's agreed version (1 before its `hello`).
  Frames up to 4 KiB are decoded on the event loop; larger ones in a worker
  thread, so a hostile frame near 1 MiB cannot stall the other ground's
  link. A frame is always decoded before the connection's state is looked
  at: a landing read from a connection that another task already saw close,
  or that the edge is closing, still lands (and its emergency callback
  runs); anything else from such a connection is dropped, and a closed
  connection's state is never re-created.
- **Adopting an epoch.** The edge holds `epoch_seen` and the leader that
  holds it. A ground `hello` with role `leader` (or a heartbeat matching
  that `hello`) at a higher epoch is adopted only while the drone is
  inactive: on the ground, no flight sequence pending, and no motion command
  being handed to the executor, including one handed out and finished
  while the executor's state was being read (a dispatch counter is compared
  before and after). With in-flight handover enabled it is also
  adopted in flight when the current authority announced exactly that node
  and epoch in a `handover`. `epoch_seen` never decreases. An epoch within
  1024 of `MAX_EPOCH` is adopted only when a handover announced it. Adoption
  forgets the plan the edge had acked and calls the executor's
  `invalidate_plan` when it has one.
- **One authority per epoch.** At an epoch already adopted, the first leader
  wins and another `leader_id` is refused (`not_authority`). The same leader
  on a new connection takes the role over only when its previous connection
  is gone (a reconnect, even in flight); a second live connection from the
  same ground never steals it.
- **Fenced commands** (`takeoff`, `assign_lanes`, `start_at`, `hold`,
  `resume`, `nudge`, `handover`) are executed only from the authority's
  connection, at `epoch_seen`, with `leader_id` equal to the authority and
  to the `node_id` the connection announced. Otherwise: `stale_epoch` for an
  older epoch, `not_authority` for anything else, unfenced motion included
  on every link version. Every nack carries `drone_id`, the command's
  `cmd_id` and `epoch_seen`. A command type the executor does not implement
  (for example `hold` before PR-5) is refused with `invalid` and a `detail`.
  `start_at` and `resume` must name the plan whose `assign_lanes` the
  executor acked in the current epoch (`no_valid_plan`, `plan_mismatch`).
- **Fenced control and authority-only messages.** `heartbeat` and `clock`
  must be fenced by the authority at `epoch_seen`; `ping`, `set_target` and
  `stream_video` are taken only from the authority's connection. Only these
  authority messages (and the authority's landings, hellos and commands)
  feed the link watchdog.
- **Re-check at dispatch.** A message keeps the authority it was accepted
  under. When the executor's thread takes it, it is dropped if a new epoch
  was adopted since (`nack{stale_epoch}`), and a motion command other than
  `hold` is dropped while the authority's connection is gone
  (`nack{not_authority}`), so a takeoff is never started into a dead link.
  An `emergency_land_all` also discards queued `takeoff`, `start_at`,
  `resume` and `nudge`.
- **At most once.** A command's `cmd_id` is recorded as pending when it is
  accepted, before it is executed. A duplicate never runs again: it is
  ignored while pending and answered with the cached reply afterwards. A
  `cmd_id` reused for another type is refused with `invalid`. The
  authority's refusals are cached too; a refusal on any other connection is
  not, so a ground that is not obeyed cannot evict the authority's entries,
  and a resend from it is simply refused again.
- **Outbound.** Telemetry, acks and nacks go to every ground that agreed a
  version, shaped for it; detections (crops, embeddings, thumbnails) go to
  the authority only and are dropped while there is none. The authority
  completes executor messages for v2 until the executor does it itself:
  telemetry gains `epoch`, `plan_id`, `state` and `plan_valid` (`false`
  unless the executor reports otherwise), and acks and nacks gain the
  `cmd_id` of the command they answer, `epoch_seen` and `result`.
- **Closing a refused connection.** A connection closed with 4010 is
  marked as closing at once: frames already behind the refusal are dropped
  (a landing still lands). The next reconnect to that ground waits 1 s,
  doubling while it keeps being refused up to 10 s, and is reset by an
  accepted `hello`. `edge_connection_refused` is logged for the first
  refusal of each (ground, reason) and then every 20th.

### Link stalls

A pulled cable closes nothing: packets stop. The WebSocket keepalive
notices 20 to 40 s later, and the connection then spends its close timeout
(10 s by default) closing, during which a `send` on it waits. Neither side
may let that stall anything else.

- **Edge.** Every ground connection has its own outbound queue and writer
  (`flightrisk/link_outbox.py`). `send` (telemetry, acks, nacks),
  `send_detections` and the reader's replies (hello, pong, landing acks)
  only queue and return at once; the executor's control thread and the
  reader never wait for the network. Each writer sends one message at a
  time with a 1 s timeout and stops at a connection that is closing.
  Replies go first; telemetry keeps the latest four and detections the
  latest two per connection. Connections use a 1 s `close_timeout`.
  Measured with a black-holed standby link and a 10 s close timeout:
  the longest telemetry gap at the authority is 0.57 s (cadence 0.5 s) and
  a `land` from it reaches the drone in 2 ms (before: a 9.8 s telemetry
  gap, and the land waited 9.7 s behind an `assign_lanes` ack on the
  control thread).
- **Ground.** `GroundTransport` sends to each edge independently with a
  0.5 s timeout (`send_timeout_s`) and skips a connection that is closing;
  its connections use a 1 s `close_timeout`, and heartbeats are paced by
  deadline. Measured with a black-holed follower edge and a 10 s close
  timeout: the healthy edge's longest heartbeat gap is 1.0 s (interval
  1 s) and `emergency_land_all` reaches it in under 1 ms and returns in
  1 ms (before: an 11 s heartbeat gap, so the leader's own drone landed on
  link loss, against plan section 2.7, and `emergency_land_all` took 7.5 s
  to return to its caller). A send that timed out may still arrive; it is
  reported as not confirmed.

### Executor interface

`EdgeAuthority` needs five hooks (`ExecutorHooks`), adapted for
`LaneExecutor` by `edge_uplink.LaneExecutorHooks`, which uses PR-5's methods
by name when they exist: `is_active()` (`LaneExecutor.is_active` if present,
else the drone's `is_flying` or `is_busy`; an error counts as active),
`supports(type)` (`type in LaneExecutor.CONTROL_TYPES`), `snapshot()` (from
`telemetry()`), `fleet_status()` (`LaneExecutor.fleet_status` if present,
returning `plan_id`, `state`, `plan_valid`) and `invalidate_plan(reason)`
(`LaneExecutor.invalidate_plan` if present). Hooks, `supports` included,
are never called while the authority holds its lock, so a hook may take the
executor's lock.

**Known issue (not fixed here).** A `start_at` ack is sent by the
executor's worker thread without a `cmd_id`; the authority stamps it with
the latest `start_at` it accepted. When a second `start_at` is accepted by
the authority and then refused by the executor as `busy` (the v1 executor
refuses it silently, without a nack) before the first one's ack goes out,
that ack carries the second `cmd_id`. The ground then sees the second
command acked and the first one time out. PR-5's executor echoes `cmd_id`
in its own acks, which removes the guess; until then a leader should not
send a second `start_at` while one is pending.

### Ground rules

- The ground sends its `hello` first on every connection and nothing but a
  landing before it, so a heartbeat never locks an edge to v1.
- While the edge's answer is pending, messages keep their v2 fields (a v1
  edge ignores them) and v2-only commands wait; after an edge `hello` they
  are shaped for the agreed version, and after `HELLO_TIMEOUT_S` without one
  for v1. A late edge `hello`, a version change and a `node_id` change close
  the connection.
- A leader fences its heartbeats and clock messages; a standby sends no
  heartbeats. Incoming acks, nacks and telemetry are validated with the
  agreed version.
- `send_command` matches replies by `(drone_id, cmd_id)`; a landing ack
  without a `cmd_id` completes that drone's pending landings. Only
  `RETRYABLE_COMMAND_TYPES` are resent, with the same `cmd_id`; a newer
  `hold`, `assign_lanes` or `land` ends a drone's pending `assign_lanes`
  retries, and an `emergency_land_all` ends every pending motion retry.
- An edge reporting a higher `epoch_seen` (in its `hello` or in a
  `stale_epoch`/`not_authority` nack) to a leader triggers `on_stale_epoch`.
- **`hold` to a v1 edge.** `send_to_drone` and `send_command` raise
  `PeerVersionError` (a `ProtocolError`) for a v2-only command to a v1 link,
  and `broadcast_control` skips such links with a warning. Choosing the
  fallback (land that drone, or report it as unable to hold) is the
  leader's policy and is left to PR-6/PR-8.

## Integration with the lane executor (PR-5)

PR-5 (the lane executor with hold, resume and nudge) and this change were
reviewed in parallel. PR-5 is now merged, and wiring the two together is
the integration PR that follows this one. The contracts meet already:
`LaneExecutor` provides the public `fleet_status()` and `is_active()` that
`LaneExecutorHooks` calls by name, and it takes an `authority=` object with
`check(msg)` and a callable `epoch_seen()`. `EdgeAuthority.check(msg)`
returns `None`, `stale_epoch` or `not_authority` (`None` for every message
the uplink accepted and handed out, and always for a landing), and
`EdgeAuthority.epoch_seen` is an `int` that can also be called, so
`authority.epoch_seen()` works as the executor expects while existing code
keeps reading the property. `LaneExecutor.ALL_CONTROL_TYPES` is exported,
and the executor keeps its own `CommandCache` for `cmd_id`s. The remaining
mismatches are all handled in the integration PR that follows:

| Topic | This change (PR-4) | Lane executor (PR-5, merged) | What the integration must do |
|---|---|---|---|
| Routing `hold`, `resume`, `nudge` | The authority accepts them and the uplink delivers them, but `edge_device` routes only `CONTROL_TYPES` to the executor, and `LaneExecutorHooks.supports` reads `CONTROL_TYPES`, so they are refused with `invalid` | `LaneExecutor.ALL_CONTROL_TYPES` (`CONTROL_TYPES` plus the three) and `handle_message` handles them | Handled in the integration PR that follows: route `ALL_CONTROL_TYPES` in `edge_device` and make `supports` in `edge_uplink` read `ALL_CONTROL_TYPES` |
| `supports(type)` | Sampled before the authority's lock (never under it) | No such method; the executor itself refuses what it cannot do | Handled in the integration PR that follows (with the routing row): keep the adapter on `ALL_CONTROL_TYPES`; it must stay non-blocking and never call back into the authority |
| `fleet_status()` / `is_active()` | `LaneExecutorHooks` calls them by name when they exist | Both are public on `LaneExecutor`: `fleet_status()` returns `plan_id`, `state` and `plan_valid` (and the epoch seen), `is_active()` says whether a plan is running | Nothing more: the hooks pick them up by name. The fallbacks to `is_flying`/`is_busy` remain only for executors without them |
| Landing ack | `_landing` acks every `land`/`emergency_land_all` on a v2 link, from the reader | The executor acks every landing with a `cmd_id` again, from the control thread | Handled in the integration PR that follows: keep one ack (drop the reader's once the executor acks, or suppress the executor's in uplink mode); the ground already tolerates a duplicate ack, so until then a landing is acked twice |
| Command-id cache | The authority caches accepted and refused `cmd_id`s | The executor has its own `CommandCache` and reply cache | Either is enough; with both, a duplicate is answered by the authority and never reaches the executor |
| Plan named in the edge `hello` | The authority's own record of the plan acked in this epoch, cleared on adoption and on every landing, so it can name a plan the executor has already invalidated | `plan_id` is kept after invalidation; `plan_valid` says whether it can still be started | Handled in the integration PR that follows: take `plan_id` and `plan_valid` from `fleet_status()` and report `plan_valid` in the `hello`, so a new leader does not trust a dead plan |
| Epoch in telemetry | The authority completes v2 telemetry with `epoch` | The executor reports the epoch it saw from its `authority` | Handled in the integration PR that follows: take the epoch from one source so telemetry and the `hello` agree |
| `start_at` ack `cmd_id` | Guessed from the latest accepted `start_at` (see the known issue above) | Echoed by the executor | Prefer the executor's `cmd_id`; the authority only fills it when absent |
| `invalidate_plan` | Called after adopting an epoch | Provided; also called by the executor on every landing | Nothing more |

## The edge lane executor (PR-5)

The executor side of protocol v2 is implemented in
`flightrisk/drone/lane_executor.py` and its sibling modules `lane_plan.py`
(plans), `lane_commands.py` (v2 commands, replies and the authority hook),
`lane_flight.py` (flight sequences), `lane_monitor.py` (monitor and
telemetry) and `command_cache.py` (command ids). Tests:
`tests/test_lane_executor_hold.py`, `tests/test_lane_executor_plan.py`,
`tests/test_lane_executor_model.py` (seeded model-based interleavings) and
`tests/test_lane_executor_wire.py` (the real Tello driver on the wire-level
fake).

- **Replies.** A command that carries a `cmd_id` gets the v2 `ack` or
  `nack`, built and validated by `make_ack`/`make_nack`. A command without
  one is a v1 command and gets exactly the v1 replies as before; a v2-only
  nack reason (for example `stale_plan`) is reported to a v1 ground as
  `invalid`, the nearest v1 reason. A landing that carried a `cmd_id` is
  acknowledged only after `drone.land()` returns. On a real Tello that is
  once the land is confirmed (the driver waits for state showing the drone
  on the ground, up to 25 s), which takes seconds, longer than the ground's
  2 s ack timeout. PR-4/6 must account for this: a land ack that arrives
  late does not mean the land failed, and a resent `land` is executed
  again (harmless). Every `assign_lanes` nack carries `lanes_total`
  (the number of lanes received, 0 when `lanes` is not a list), including
  an authority refusal (`stale_epoch`, `not_authority`) and a reused
  `cmd_id`, so a stale leader always gets the nack it needs to step down.
- **Command ids.** Every fenced command with a `cmd_id` is recorded as
  pending when it arrives, so a duplicate is never executed again. A
  duplicate of a finished command gets its first reply resent unchanged
  (refusals included); a duplicate of a command that is still running (a
  scheduled `start_at`, a `nudge` in flight) gets nothing, because the
  original's reply follows. The cache holds the last 256 ids and evicts the
  oldest finished entry first. Landing never goes through it.
- **`hold`.** The drone finishes the move it is flying and then sends
  nothing more until `resume`; a Tello hovers on its own once a move
  completes. The executor never sends the Tello `stop` for a hold, for two
  reasons. Interrupting a relative move leaves the distance actually flown
  unknown, which would corrupt the dead-reckoned position that the geofence
  relies on. And what `stop` does in the middle of a move on Tello EDU
  firmware has not been verified on hardware (plan rung L2). Each move, and
  a takeoff that has not been sent yet, is claimed under the executor's lock
  just before it is sent, and no move is claimed once a hold is recorded;
  the ack carries `detail: finishing_move` when a move was already claimed.
  A hold also cancels a scheduled `start_at` that has not fired (that
  `start_at` is answered `nack{reason: busy, detail: cancelled_by_hold}`),
  stops before the altitude-slot climb if it arrives during a takeoff, holds
  a plan with `on_complete: land` before its landing, and restarts (does
  not pause) the end-of-lanes hover timeout: after the resume the drone
  hovers the full `hover_timeout_s` again. The hover-timeout and
  `on_complete` landings are checked again under the executor lock as
  they are recorded, so a hold that was acknowledged first always stops
  them. A hold has no time limit while the ground's heartbeats keep
  arriving; the battery landing (below 15 %), the link-loss landing and
  every operator or ground landing still end it, and every landing clears
  it. A hold during a takeoff leaves the drone at its takeoff height
  (about 80 cm on a Tello), not at its altitude slot, until the resume. A
  hold on a drone that is on the ground with no start pending is refused
  with `not_flying`.
- **`resume`.** Restarts only the plan named by `plan_id`, and only while
  it is valid, from the lane and move where the drone stopped (a plan
  accepted while holding starts where that plan says). It is refused with
  `landing`, `no_valid_plan`, `plan_mismatch`, `not_flying`, `busy` (a nudge
  is flying) or `geofence` (the rest of the path, from the current
  dead-reckoned position, would leave the geofence; the drone keeps
  holding). A resume while not holding is acknowledged with
  `detail: not_holding` and changes nothing.
- **`nudge`.** Accepted only while the drone hovers idle or holds, never
  while it flies lanes or runs another sequence (`busy`), only for a
  horizontal direction and an integer distance from 20 to 500 cm
  (`invalid`), and only inside the geofence (`geofence`). It is recorded in
  the dead-reckoned position like a lane move and acknowledged once flown.
  A nudge the controller did not execute is answered
  `nack{reason: busy, detail: <outcome>}` and not recorded; a controller
  error lands the drone, as for a lane move. A nudge that a landing cut
  short is answered `nack{reason: landing}`, never acknowledged; the
  controller reported the move executed, so it is recorded in the
  dead-reckoned position like an interrupted lane move (the distance
  actually flown is unknown either way). A later v2 `start_at` or a `resume`
  re-checks the rest of the plan against the geofence from the nudged
  position and refuses with `geofence` instead of flying out of it, and a
  re-task that arrives while a nudge flies is flown after it only if it
  passes the same check.
- **Plan ordering and validity.** Plans are ordered by `(epoch, plan_seq)`
  as described under "Plan ordering". A plan becomes valid when accepted
  and stops being valid at every landing the executor requests or sees
  (link loss, battery, local key, emergency, geofence, a ground `land`, the
  hover timeout, the plan's own `on_complete` landing, an executor error,
  and a landing the drone made on its own), and when the edge authority
  calls `invalidate_plan`. `start_at` and `resume` are then refused with
  `no_valid_plan`, and a late or retried copy of the old plan with
  `stale_plan`, until a new plan is accepted. This applies to v1 commands
  too: before this change, a v1 `start_at` after an unplanned landing
  restarted the old lanes.
- **Landing requested.** From the moment the executor is asked to land
  (`land_now`: a ground or local landing, battery, link loss, the
  geofence, an error) until that landing is over, no flight sequence is
  launched: `start_at`, `resume`, `nudge`, `hold` and `takeoff` are refused
  with `landing`, and an `assign_lanes` is refused with `landing` and the
  current plan is kept, whether it would re-task the flying drone or arrives
  while the drone still descends after the landing was recorded (a plan
  accepted then would stay valid after touchdown). The executor
  counts the request before it waits for its lock, so a command that is
  being checked at that moment is refused too.
- **End of lanes.** With `on_complete: land` the drone lands after its
  last lane. With `on_complete: hover` it lands once it has hovered idle
  for `hover_timeout_s` (20 s when absent) after its last lane; a hold
  restarts that countdown, which starts again from zero after the resume.
  A hold or a re-task that arrives after either landing was decided, but
  before it was recorded, stops it (a re-task then flies its lanes). A
  plan without `on_complete` hovers until the ground lands it, as in v1.
- **Telemetry.** In v2 mode (constructed with `fleet_v2=True`, or after
  the first command with a `cmd_id`) telemetry adds `epoch`, `plan_id`,
  `state` and `plan_valid`. `plan_id` is kept after the plan is
  invalidated, so the `hello` and telemetry still name it.

What the edge authority (PR-4) has to wire to the executor:

- Route `ALL_CONTROL_TYPES` (the v1 `CONTROL_TYPES` plus `hold`, `resume`
  and `nudge`) to `LaneExecutor.handle_message`. The edge device still
  routes only the v1 set, so until then a v2 command never reaches the
  executor.
- Pass `authority=` an object with `check(msg)` (None to accept a fenced
  command, or `stale_epoch`/`not_authority`) and `epoch_seen()`. The
  executor asks it about every fenced command and never about a landing,
  sends the refusal as a nack carrying `epoch_seen`, and fails closed
  (`not_authority`) if `check` raises or returns anything else. The default
  accepts everything and reports the highest epoch it has seen.
  `epoch_seen()` is called from the monitor thread (telemetry, the `hello`
  and `fleet_status`, the last under the executor lock) as well as from the
  command thread, so it must be non-blocking: no network, disk or long lock.
- Call `invalidate_plan(reason)` when the edge adopts a new epoch.
- Use `is_active()` for the authority's "inactive" test (true while the
  drone flies, a sequence runs or a `start_at` is pending, or a landing is
  requested or in progress; an unreadable flight state counts as active)
  and `fleet_status()` for the v2 telemetry and `hello` fields (`plan_id`,
  `state`, `plan_valid` and `epoch`).
- Construct the executor with `fleet_v2=True` in a v2 deployment, so
  telemetry carries the v2 fields from the start.
- Build the edge `hello` from `plan_id`, `lane_index`, `is_flying`
  (telemetry), the battery and `epoch_seen`.
- Validate the executor's messages with the agreed `proto` in the
  transport, as required above: under the v1 default a v2 nack for `hold`
  or `resume` is dropped.

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
- **Every landing invalidates the plan.** Plan section 2.8 exempts the
  plan's own `on_complete` landing. The executor invalidates the plan on
  that landing too: by then every lane has been flown, so nothing is lost,
  and one rule ("a landed drone needs a new plan") is simpler to check.
- **Ground speaks first.** The edge sends its `hello` in reply to the
  ground's, not unprompted on connect. A v1 ground therefore never sees a
  message type it does not know.
