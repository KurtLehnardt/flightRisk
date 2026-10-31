# Mobile contract (iOS and Android)

This is the one contract the FlightRisk iOS and Android apps follow when they talk to a FlightRisk ground station. Both apps test their decoders and encoders against the same JSON fixtures, which the real Python code produces. Every statement here was checked against the Python source at the commit named in the fixtures' `_meta.generated_at_commit`. Where this document and the code disagree, the code is right and this document has a bug.

A phone plays one or both of two roles:

1. **Operator console.** The phone connects to the dashboard over Socket.IO, like the browser does, and shows the fleet, the plan, the detection profiles, the ceiling settings, the ground's reasoning provider and match alerts. It can land drones and resume a held drone's search. It never plans, starts, or flies a mission.
2. **Phone as an edge.** The phone's own camera pipeline sends `detections` messages to the ground's edge WebSocket, so a person walking at street level adds to the ground's view.

## Fixtures

The fixtures live in `tests/fixtures/mobile_contract/`. Each file has a `_meta` block (what it holds, the Python functions that built it, the commit, how to regenerate it) and a `cases` object of named cases. Each case has `direction` (`server_to_client`, `client_to_server`, `ground_to_edge`, `edge_to_ground` or `reference`), `notes`, and either `payload` (the JSON object exactly as it goes over the wire) or `frame` (a raw Engine.IO text frame). Socket.IO cases also have `event`, the event name.

Regenerate them with:

```
venv/bin/python scripts/export_mobile_contract_fixtures.py
```

`tests/test_mobile_contract_fixtures.py` regenerates every file in a temporary directory and fails when the result differs from the committed files, so any change to a payload a phone reads fails the Python suite first. It also checks the case names below, file sizes, and that no fixture contains or resembles a secret. `tests/test_mobile_socketio_live.py` starts the real dashboard on a loopback port and checks the server behaviour this document describes over a real WebSocket. The case names are stable: both apps reference them, so a rename is a breaking change for both.

| File | Event or link | Cases |
|---|---|---|
| `socketio_frames.json` | Engine.IO and Socket.IO frames | `engineio_open`, `socketio_connect_with_auth`, `socketio_connect_without_auth`, `socketio_connect_ack`, `socketio_connect_error`, `socketio_event_client_to_server`, `socketio_event_client_to_server_with_payload`, `socketio_event_server_to_client`, `socketio_event_with_ack_id`, `socketio_ack`, `engineio_ping`, `engineio_pong`, `socketio_disconnect`, `engineio_close` |
| `status.json` | `status` | `fleet_two_drones`, `no_fleet_sim_source` |
| `fleet_status.json` | `fleet_status` | `all_connected`, `one_down`, `one_lost`, `one_held_orphan_countdown`, `one_manual`, `drone_link_down`, `non_fleet_local_drone`, `non_fleet_no_drone` |
| `fleet_event.json` | `fleet_event` | `plan_refused_unconnected`, `plan_refused_manual_control`, `plan_invalidated`, `start`, `start_refused`, `start_refused_drone_unreachable`, `drone_lost`, `retask`, `retask_failed`, `land_all`, `land_all_failed`, `profile_switch_pending` |
| `fleet_plan.json` | `fleet_plan` | `two_drones`, `map_plan_lost_zone_uncovered` |
| `fleet_detection.json` | `fleet_detection` | `new_detection` |
| `frame.json` | `frame` | `edge_video_with_match`, `edge_detections_only` |
| `match_alert.json` | `match_alert`, `match_alert_reminder` | `edge_match`, `reminder_on_connect` |
| `detection_profiles.json` | `detection_profiles` | `pending`, `fallback`, `refused`, `unsupported`, `standby_controls_disabled`, `no_board` |
| `ceiling_settings.json` | `ceiling_settings` | `on_ground_in_sync`, `edge_pending`, `edge_refused`, `slot_above_limit`, `locked_in_flight`, `unavailable` |
| `reasoning_provider.json` | `reasoning_provider` | `local_gemma_cloud_blocked`, `off`, `cloud_allowed_no_key`, `cloud_allowed` |
| `manual_status.json` | `manual_status` | `held_orphan_countdown`, `manual_controlled` |
| `manual_ack.json` | `manual_ack` | `land_ok`, `land_unknown_drone`, `resume_search_ok`, `resume_search_not_manual` |
| `manual_result.json` | `manual_result` | `land_acked`, `resume_search_acked`, `land_no_reply` |
| `land_all_ack.json` | `land_all_ack` | `fleet_reached_all`, `local_fleet`, `reached_no_drone` |
| `command_ack.json` | `command_ack` | `land` |
| `stream_video_state.json` | `stream_video_state` | `off` |
| `error.json` | `error` | `land_all_reached_no_drone`, `manual_needs_fleet_mode`, `profiles_need_edge_source`, `no_drones_connected`, `manual_refusal` |
| `client_emits.json` | events a phone emits | one case per allowed event; `forbidden_<event>` for each forbidden one |
| `edge_ground_to_edge.json` | edge link, ground to edge | `ground_hello`, `heartbeat_v1`, `heartbeat_v2`, `clock`, `ping`, `set_target`, `stream_video`, `land`, `emergency_land_all` |
| `edge_edge_to_ground.json` | edge link, edge to ground | `detections_appearance_only`, `detections_with_face`, `detections_no_video`, and fleet-edge replies for reference (`telemetry_v1`, `telemetry_v2_drone_link_down`, `ack_assign_lanes`, `nack_assign_lanes_geofence`, `nack_v2_drone_unreachable`, `profile_ack_fallback`, `profile_nack_in_flight`, `ceiling_ack`, `ceiling_nack`, `manual_ack`, `manual_nack`, `pong`) |
| `nack_reasons.json` | reason and mode vocabularies | `assign_lanes_v1`, `fenced_command_v2`, `set_detection_profile`, `profile_fallback`, `set_ceiling`, `manual_command`, `control_modes`, `hold_reasons`, `edge_states` |
| `unknown_keys.json` | forward compatibility | one case per event: a real payload with `x_future_field` added to every record object |

Identifiers, keys and images in the fixtures are fake: drone ids are `drone-1` and `drone-2`, the only key is `test-key`, and every image is the same 8 by 8 grey JPEG.

## Endpoints and ports

| Service | Address | Notes |
|---|---|---|
| Dashboard (Socket.IO and REST) | `http://HOST:5555` | Port from `--port` or the config (`DashboardConfig.port`, default 5555). |
| Socket.IO | `ws://HOST:5555/socket.io/?EIO=4&transport=websocket` | Open the WebSocket transport directly; the phone needs no long-polling. |
| Health | `GET /api/health` | Public (no key needed). Returns `status`, `version`, `components`, `reasoning_provider`, `active_signals`. |
| Status | `GET /api/status` | Needs the key. A `401 {"error": "unauthorized"}` means a wrong or missing key. |
| Edge link | `ws://HOST:9000` | Raw WebSocket, not Socket.IO. Port from `--edge-port` or `FLIGHTRISK_EDGE_PORT` (default 9000). |

The dashboard binds 127.0.0.1 by default. To reach it from a phone, the ground must bind a LAN address (`--host` or `FLIGHTRISK_DASHBOARD_HOST`), and the ground refuses to start on a non-loopback address unless `FLIGHTRISK_API_KEY` is set (`check_dashboard_bind`). So a phone always needs the dashboard key. In the same way, the edge link refuses a non-loopback bind without `FLIGHTRISK_EDGE_TOKEN`, so a phone acting as an edge always needs the edge token.

## Authentication

- **Socket.IO.** Send the key in the namespace connect: `40{"api_key":"<key>"}` (`socketio_connect_with_auth`). A missing or wrong key gets `44{"message":"Connection rejected by server"}` (`socketio_connect_error`), and the server sends no events. Report this as "wrong key" and wait for the user; never retry it in a loop.
- **REST.** Send `Authorization: Bearer <key>` on every request. `/` and `/api/health` work without it; every other route returns 401 without it.
- **Edge link.** The first message must be `{"type": "auth", "token": "<edge token>"}`, sent within 10 seconds of opening the WebSocket. A wrong token, a wrong first message, or no message closes the connection with code 4001 and the reason `unauthorized: edge token mismatch` or `unauthorized: no auth message`. Treat 4001 as "fix the token", not as a reason to reconnect in a loop. The fixtures contain no auth message, because no fixture may carry a token field.

## Socket.IO transport

The dashboard runs Flask-SocketIO 5.6.1 on python-socketio 5.16.3 and python-engineio 4.13.3, which speak Socket.IO protocol 5 over Engine.IO protocol 4. `socketio_frames.json` has every frame a phone sends or receives, encoded by those libraries.

- The first frame after the WebSocket opens is the Engine.IO open packet: `0{"sid":...,"upgrades":[],"pingTimeout":20000,"pingInterval":25000,"maxPayload":10485760}`. Times are milliseconds.
- Then send the namespace connect with the key. The server runs its connect handler before it acknowledges the connect, so `status` (and `target_photo` and `face_report` when a target is set) arrive before `40{"sid":...}`. A client must hold events that arrive before the acknowledgement and deliver them, in order, once the connect completes.
- Events are `42["name",payload]` in both directions. Every server event has exactly one payload object.
- The server pings with `2` every `pingInterval` ms; answer `3` at once. If no ping arrives within `pingInterval + pingTimeout` ms, treat the connection as dropped.
- An event may ask for an ack (`421["get_fleet_status"]`); the dashboard answers `431[]` because its handlers return nothing. Phones do not need acks; every answer comes as an event.
- To leave, send `41` and then close the WebSocket (or send the Engine.IO close `1`).
- **Do not send an `Origin` header.** Engine.IO checks the origin only when the request carries one, and it refuses any origin that is not the dashboard's own page with HTTP 400. A native WebSocket client sends no Origin by default; keep it that way.
- Unknown event names sent to the server are ignored; the connection stays up.

**On every connect and every reconnect**, request the full state with `get_fleet_status`, `get_detection_profiles`, `get_ceiling_settings`, `get_reasoning_provider` and `get_manual_status`, and redraw from the answers. Never keep a drone card from before the disconnect without marking it stale. (The dashboard page requests the first four on connect and the ceiling card requests `get_ceiling_settings`; a phone requests all five.) Do not assume any order between events.

## Events a phone may emit

This is an allow-list. The app must make it impossible to emit anything else (`client_emits.json`, cases with `allowed: true`).

| Event | Payload | When |
|---|---|---|
| `get_fleet_status` | none | On every connect and on refresh. The answer is `fleet_status`, then `fleet_plan` when a plan exists. |
| `get_detection_profiles` | none | On every connect and on refresh. |
| `get_ceiling_settings` | none | On every connect and on refresh. |
| `get_reasoning_provider` | none | On every connect and on refresh. |
| `get_manual_status` | none | On every connect and on refresh. A ground without a fleet does not answer. |
| `land_all` | none | Always offered while the app shows the Ground screen (see the safety rules). The answer is `land_all_ack`. |
| `drone_command` | `{"command": "land", "drone_id": "<id>"}` | Land one drone. `command` is always `"land"`. In fleet mode the answer is `manual_ack` (and `manual_result` when the edge answers); without a fleet it is `command_ack`, or `error` when no drone is connected. |
| `set_detection_profile` | `{"drone_id": "<id>", "profile": "<name>"}` | Only while `detection_profiles.controls_enabled` is true and the drone's `supported` is true. An empty `profile` follows the fleet default. |
| `set_fleet_detection_profile` | `{"profile": "<name>"}` | Only while `controls_enabled` is true. An empty `profile` clears the default. |
| `manual_resume_search` | `{"drone_id": "<id>"}` | Only for a drone whose `control_mode` is `"held"` while `fleet_status.manual_controlled` is null, that is, a held drone nobody controls. The answer is `manual_ack`, then `manual_result`. |

A phone must never emit these, although the dashboard serves them: `plan_fleet`, `start_fleet`, `start_search`, `stop_search`, `manual_take_control`, `manual_command`, `set_ceiling_settings`, `set_reasoning_provider`, `set_stream_video`, `set_target`, `register_drone`, `deregister_drone`, `remove_all_drones` and `restart_dashboard` (`client_emits.json`, cases `forbidden_<event>`, with the reason for each). The laptop stays the mission console: Start must carry the plan the browser shows, manual flight needs a controller who cannot roam out of Wi-Fi range, and the ceiling, the reasoning provider and video streaming are fleet-wide settings. `match_alert_response` is not in the allow-list either: the operator answers match alerts on the laptop.

## Events a phone must handle

| Event | Fixture | What it carries |
|---|---|---|
| `status` | `status.json` | Sent once per connect: `connected`, `source`, `appearance_only`, `tiled`, and `fleet` with `drones` (a **count**), `net` and `synced`. `fleet.drones > 0` means fleet mode. |
| `fleet_status` | `fleet_status.json` | Fleet mode: `drones` (an object keyed by drone id), `net`, `synced`, `manual_controlled`. Without a fleet: the legacy shape `{drones, count}`, whose drones have `battery`, `height` (cm, 0 when unknown), `temperature`, `flight_time`, `is_flying` and `is_connected`, and no `connected` or `lost`. |
| `fleet_event` | `fleet_event.json` | `event` (the kind) and usually `detail`. `drone_lost` has `drone_id` and no `detail`. `retask` has `drone_id`, `to`, `lanes`, optional `awaiting`, `detail`. `retask_failed` has `drone_id`, `lanes` (each with `reason`), `detail`. `profile_switch_pending` has `drone_ids`. |
| `fleet_plan` | `fleet_plan.json` | `zones` (each with `drone_id`, `x_cm`, `y_cm`, `w_cm`, `h_cm`, optional `takeoff_x_cm` and `takeoff_y_cm`, `alt_cm`, `lanes`, `lost`), `grid` (`x_cm`, `y_cm`, `w_cm`, `h_cm`), `gap_cm`, `strip_cm`, `uncovered`, and optional `details` (the map editor's own view, which a phone may ignore). |
| `fleet_detection` | `fleet_detection.json` | `id`, `drone_id`, `drone_ids`, `count`, `x_cm`, `y_cm`, `rel_x_cm`, `rel_y_cm`, optional `thumbnail` (base64 JPEG), optional `lat` and `lon`. |
| `frame` | `frame.json` | `image` (base64 JPEG or null), `boxes` (`bbox_norm` as `[x1, y1, x2, y2]` in 0..1, `score`, `matched`, `track_id`), `fps`, `persons`, `match`, `match_score`, `stream_video`, `drone_id`, `frame_width`, `frame_height`, `telemetry`, `recording`. Keep one tile per `drone_id`. |
| `match_alert`, `match_alert_reminder` | `match_alert.json` | `alert_id`, `time`, `score`, `face_score`, `reid_score`, `alert_level`, `snapshot` (base64 JPEG), `track_id`, `type`, `reasoning`. A reminder adds `renotify` and `renotify_count`; it arrives on connect while an alert waits for the operator. |
| `detection_profiles` | `detection_profiles.json` | `profiles`, `fleet_default`, `drones` (keyed by id: `supported`, `active`, `fallback`, `fallback_reason`, `requested`, `override`, `desired`, `mismatch`, `pending`, `refusal` with `reason` and `detail`, `note`, `flying`, `switchable`, `detector_ok`), `controls_enabled`, `controls_note`. Without the edge source: `{available: false, profiles: [], drones: {}, fleet_default: null}`. |
| `ceiling_settings` | `ceiling_settings.json` | `ceiling_cm`, `margin_cm`, `max_slot_cm`, `limits`, `drones` (a list: `drone_id`, `slot_cm`, `own_slot_cm`, `default_slot_cm`, `problem`, `edge` with `supported`, `ceiling_cm`, `margin_cm`, `slot_cm`, `flying`, `in_sync`, `pending`, `refusal`, `note`), `editable`, `locked_reason`, `load_error`, `file`. Without the edge source: `{available: false}`. Read only on a phone. |
| `reasoning_provider` | `reasoning_provider.json` | `provider` (`gemma`, `claude` or `off`), `label`, `model`, `available`, `unavailable` (provider name to reason text), `cloud_allowed`. Read only on a phone. |
| `manual_status` | `manual_status.json` | `controlled` and `drones` (keyed by id: `control_mode`, `edge_control_mode`, `manual_supported`, `manual_last`, `orphan_land_in_s`). |
| `manual_ack` | `manual_ack.json` | `drone_id`, `action`, `ok`, `reason`, `detail`. A refusal is also sent as `error`. |
| `manual_result` | `manual_result.json` | The same keys, sent when the edge answers or does not answer in time (`reason: "no_reply"`). |
| `land_all_ack` | `land_all_ack.json` | `coordinator`, `transport`, `local` (how many drones each path reached, or null) and `reached_any`. |
| `command_ack` | `command_ack.json` | `command`: the answer to `drone_command` on a ground without a fleet. |
| `stream_video_state` | `stream_video_state.json` | `enabled`. |
| `error`, `warning` | `error.json` | `message`. Show it as a toast; it is never a disconnect. |

Any other event name must be ignored without error. The dashboard sends events a phone does not use (for example `fleet_telemetry`, `plan_preview`, `target_photo`, `face_report`).

## Compatibility rules

- **Ignore unknown keys everywhere**, at every depth. `unknown_keys.json` has a real payload of every event with `x_future_field` added to every record object; a decoder must produce exactly what it produces for the original. Keys are never added inside maps keyed by drone id or provider name.
- **Tolerate missing optional keys.** Treat an absent key and a null value as "not reported" and show a dash (`--`), never zero. Fleet telemetry fields stay null until an edge reports (for example `rel_x_cm` and `rel_y_cm` appear only after the first telemetry with a position).
- **Maps are objects keyed by id.** `fleet_status.drones`, `detection_profiles.drones` and `manual_status.drones` are objects keyed by drone id, never lists. `ceiling_settings.drones` is a list. Sort drone ids for display.
- **`status.fleet.drones` is a count**, while `fleet_status.drones` is the map. Do not read `count` in fleet mode; only the non-fleet `fleet_status` has it.
- **Unknown values are text.** A `fleet_event` kind, a refusal reason, a `control_mode` or an `alert_level` the app does not know is shown by name, never dropped and never an error. `nack_reasons.json` lists today's vocabularies from the fleet protocol constants.
- **No version field.** The Socket.IO payloads carry no protocol version. A missing event or field means "this ground does not support it": hide that control rather than fail. `/api/health` returns `version` for display.
- **Coordinates.** All `*_cm` positions are in the plan frame in centimetres, shared by every drone (`fleet_plan.grid`). `uncovered` holds two kinds of entry: re-tasked lanes nobody took (no `kind` key; `index`, `direction`, `distance_cm`, `x_cm`, `y_cm`, `end_x_cm`, `end_y_cm`, `drone_id`, `reason`) and coverage gaps of a map plan (`kind: "area"`; `x_cm`, `y_cm`, `w_cm`, `h_cm`, `reason`). The `fleet_plan` payload carries no geographic anchor.

## Derived state, exactly as the dashboard shows it

These rules copy `flightrisk/dashboard/static/ui/ui.js` (`connState`, `modeText`, `manualDisabledReason`). `tests/test_mobile_contract_fixtures.py` fails when those lines change, so this section stays in step.

**Connection state** of a drone, from its `fleet_status` entry, first match wins:

1. `lost == true`: **LOST**.
2. `connected == false`: **DOWN**.
3. `connected == true`: **CONNECTED**.
4. Otherwise: unknown, shown as `--` (this is the case for every drone of a ground without a fleet).

A DOWN or LOST card shows its values as the last ones received ("No live link: values are the last ones received").

**Control mode** of a drone, first match wins, from the merged `fleet_status` and `manual_status` entries:

1. `orphan_land_in_s` is a number: **HELD: auto-landing in N s, no controller**.
2. The drone is `fleet_status.manual_controlled` (or `manual_status.controlled`): **MANUAL: under your control** on the dashboard. On a phone, which never controls a drone, show **MANUAL** (the laptop controls it).
3. `control_mode == "held"` or `edge_control_mode == "held"`: **HELD: hovering, search paused**.
4. `control_mode == "manual"`: **MANUAL**.
5. `is_flying == true`: Flying. `is_flying == false`: On the ground. Otherwise nothing.

**Drone link.** When `drone_link_ok == false`, show "Drone link down: `drone_link_detail` (or 'the edge cannot reach the drone'). Land still works." Show nothing when it is true or absent. `fleet_status` carries `drone_link_ok` and `drone_link_detail` but not `drone_link_age_s`.

**HELD countdown.** `orphan_land_in_s` is the number of whole seconds until the ground lands the held drone because nobody controls it (120 s by default, set on the ground with `FLIGHTRISK_MANUAL_ORPHAN_LAND_S`, never below 30 s). Count it down locally between updates, and replace it with each new value from `fleet_status` or `manual_status`. Resuming the search, someone taking control, the drone landing, or LAND ALL cancels it.

**LAND ALL result.** `reached_any == false` is a failure: show "LAND ALL reached no drone. Land every drone with its local control now." (the server also broadcasts this as `error`). Otherwise show the counts.

## Safety display rules

1. **LAND ALL is pinned.** It sits in a top bar that is visible in every state of the Ground screen, never inside a collapsible section, and it is the only element in the danger colour. One tap sends `land_all`, as on the dashboard, which asks for no confirmation.
2. **LAND ALL is never disabled** by any drone or fleet state (DOWN, LOST, HELD, MANUAL, link down, pending commands). When the socket is not connected, the button stays visible and tappable; a tap then says plainly that LAND ALL was not sent and starts a reconnect: "LAND ALL was NOT sent: this phone is not connected to the ground. Land the drones now with their own controls, or press LAND ALL on the ground laptop." When the ground refused the phone's key, the reconnect would reuse the refused key, so the text says so instead: "LAND ALL was NOT sent: the ground refused this phone's key. Land the drones now with their own controls, or press LAND ALL on the ground laptop." The phone never queues the command for later delivery.
3. **Per-drone Land is always enabled**, in every state including DOWN, LOST, HELD and drone link down, and sends `drone_command {command: "land"}`. The ground never gates a land.
4. **Show the drone link text** "Drone link down: ... Land still works." whenever `drone_link_ok` is false.
5. **Show the HELD countdown** from `orphan_land_in_s` on the drone's card.
6. **The ground is the authority.** Geofence, ceiling, altitude slots, plan validity and link preflight are decided by the ground. The phone shows its verdicts and never computes a refusal of its own.
7. **No automatic retries.** The phone never resends a command by itself. The ground reports a manual command (a per-drone land, a resume) that its edge did not answer as `manual_result` with `reason: "no_reply"` after 4 seconds (`MANUAL_REPLY_TIMEOUT_S`). `land_all_ack` normally arrives within a few seconds, but the ground waits up to 35 seconds (`LAND_ALL_PATHS_DEADLINE_S`) for a stalled path before it answers; show "sending" until the ack arrives, show "no reply yet" after 5 seconds, and keep LAND ALL available throughout.

## Phone as an edge

A phone may send its own camera detections to the ground's edge link (`ws://HOST:9000`). The rules:

- The first message is the auth message with the edge token (see Authentication).
- The phone sends only the legacy `detections` message, in the shape `EdgeRunner.to_dict` produces (`edge_edge_to_ground.json`, `detections_appearance_only`): `type`, `timestamp`, `frame_id`, `frame_width`, `frame_height`, `thumbnail` (base64 JPEG or null) and `detections`, each with `bbox` (`[x1, y1, x2, y2]` pixels), `confidence`, `reid_embedding` and `crop` (base64 JPEG or null).
- It omits `drone_id` (it flies nothing; the ground accepts detections without one and scores them, but places nothing on the fleet map), `detection_profile` (the phone's detector is not a registered profile), and `face_embedding` unless the user turned face matching on (`detections_with_face`).
- A message is at most 1 MiB (`LEGACY_MAX_BYTES` = 1,048,576 bytes) as compact JSON. When a message would be larger, drop crops first, then the thumbnail.
- After `stream_video` with `enabled: false`, send `thumbnail: null` (`detections_no_video`); with `true`, send it again.
- Ignore `set_target` (the phone keeps its own target), `hello`, `heartbeat`, `clock`, `ping`, flight commands (`land`, `emergency_land_all`, and any other) and every unknown type, without closing the connection. A v2 ground that gets no edge `hello` within 2 seconds treats the edge as protocol v1, which is what a phone edge is.
- The phone never sends `telemetry`, `ack`, `nack`, `hello` or `pong`. The fleet-edge samples in `edge_edge_to_ground.json` are for reference only.

## Secrets

- Names, the same on both platforms: `FLIGHTRISK_DASHBOARD_API_KEY` (the ground's `FLIGHTRISK_API_KEY`, for Socket.IO and REST), `FLIGHTRISK_EDGE_TOKEN` (the edge link) and `FLIGHTRISK_CLAUDE_API_KEY` (the phone's own optional cloud reasoning).
- Storage: iOS Keychain (this device only, accessible when unlocked); Android `EncryptedSharedPreferences`. Never `UserDefaults`, plain `SharedPreferences`, files or logs.
- Use: the dashboard key only in the Socket.IO `auth` object and the `Authorization: Bearer` header; the edge token only in the edge auth message. Never log, toast, crash-report or display a key after entry; log the host and event names only.

## Cleartext on the local network

The dashboard is plain `http://` and `ws://`, and the edge link is plain `ws://`, on the fleet's private network (`docs/privacy-design.md`). Both apps need a narrow cleartext exception: on iOS, App Transport Security's local-networking exception (`NSAllowsLocalNetworking`) and a local network usage string that names the ground station; on Android, a network security config that permits cleartext only to private (RFC 1918), link-local and loopback addresses. Cleartext to public hosts stays blocked. Neither platform relies on the platform setting alone, because it cannot express address ranges and a raw socket does not consult it: both apps check the resolved address with the same ranges (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, IPv6 `::1`, `fc00::/7`, `fe80::/10`, and IPv4-mapped IPv6 judged by the IPv4 part) before they open a connection or send the API key. Every address a host name resolves to must be allowed, and the connection goes to the address that was checked, so a second lookup cannot swap in another one. On iOS, `wss://` and `https://` are encrypted and are not subject to the rule (Android supports only the plain-text link today); neither app pins certificates. The link is authenticated but not encrypted, so it belongs on the fleet access point or a trusted LAN, never on the open internet.

## Cloud reasoning opt-in

Cloud reasoning is off by default and needs an explicit opt-in (`docs/app-store-compliance.md`, "Cloud LLM Opt-In"). On the phone: the default reasoning backend is local or none, never cloud; the user must accept a consent step that names what is sent (the reference photo and person crops, to Anthropic) before a Claude key is used; a visible indicator shows while cloud reasoning is active; and a saved key alone never turns it on. The ground's own provider (`reasoning_provider`) is display only on a phone: the phone cannot switch it and cannot opt the ground into cloud use, which only the ground's `FLIGHTRISK_ALLOW_CLOUD_LLM=1` does.
