# Observability event catalog

Every FlightRisk process writes one JSON object per log line (`flightrisk/obs_log.py`). The
same line goes to the console (stderr) and, when `FLIGHTRISK_OTEL_ENABLED=true` with an
`otlp-*` exporter, to Loki through the OpenTelemetry collector. This page lists the event
names, their fields and their levels, and the metrics that go with them. The Grafana
dashboards (`observability/grafana/`) are built on these names; treat them as a stable
interface and change a dashboard query when you rename one.

## Fields on every line

| Field | Meaning |
| --- | --- |
| `timestamp` | ISO-8601 UTC with microseconds, for example `2026-10-28T01:58:54.669649Z` |
| `level` | `debug`, `info`, `warning`, `error` or `critical` |
| `logger` | Python logger name, for example `flightrisk.dashboard.app` |
| `component` | Part of the system: `dashboard`, `ground`, `edge`, `lane-executor`, `tello`, `drone`, ... |
| `event` | Stable event name (below); `log` for a plain message without one |
| `message` | Human-readable text, redacted |
| `category` | Event family: `user_input`, `target_image`, `error`, `drone_comm`, `drone_telemetry`, `alert`, `flight_safety`, `detection_pipeline`, `reasoning` |
| `trace_id`, `span_id` | The span active when the line was written (absent outside a span). Click it in Grafana to open the trace |
| `node` | `service.instance.id` from `OTEL_RESOURCE_ATTRIBUTES` (or `FLIGHTRISK_NODE`), for example `mac-ground` |
| `session_id` | Current search session |
| `drone_id` | When the event is about one drone |
| `fingerprint`, `error_type`, `error_message`, `where` | On ERROR and CRITICAL lines: a hash of the exception type and its innermost frames that groups repeats, the exception type and redacted message, and the innermost frame. A repeat within 10 s is not written again; the next one carries `suppressed` (the count). A repeat means the same fingerprint for the same drone, kind, command, action, type, reason, peer, model and queue: a failure on another drone is written at once The stack trace (`stack`) is included only while `FLIGHTRISK_LOG_LEVEL=DEBUG` |

Every field value passes through `flightrisk/obs_redact.py`: API keys (`sk-ant-...`, `sk-...`,
`ghp_...`, `AKIA...`, `AIza...`, JWTs), bearer and basic credentials, `Authorization` headers,
`token=...` style pairs, the user-info of a URL (`ws://user:pass@host` becomes
`ws://[REDACTED]@host`), mapping keys that name a credential, and the values of
`FLIGHTRISK_EDGE_TOKEN`, `FLIGHTRISK_API_KEY`, `FLIGHTRISK_CLAUDE_API_KEY` and similar
variables become `[REDACTED]`. Strings are capped at 500 characters.

## Events

| Event (`category`) | Level | Written by | Fields |
| --- | --- | --- | --- |
| `user_input` (`user_input`) | INFO; DEBUG for read-only queries; WARNING when rejected, denied or unauthorized; ERROR when the handler failed | `flightrisk/dashboard/obs_hooks.py`: every Socket.IO event from the browser and every POST/PUT/PATCH/DELETE (and every refused) REST request | `action` (event name, or `METHOD /route/<template>`), `channel` (`socket`/`rest`), `input` (sanitized payload, see below), `actor` (Socket.IO session prefix), `remote_addr`, `outcome` (`ok`, `rejected`, `denied`, `unauthorized`, `error`), `status` (REST), `duration_ms`, `error`, `content_capture` |
| `target_image` (`target_image`) | INFO | dashboard target upload (REST and Socket.IO), revert, startup load, clear | `source` (`upload`, `socket_upload`, `revert`, `startup`, `clear`), `width`, `height`, `channels`, `bytes`, `mime`, `quality` (overall score, grade, dimension scores, issue count), `face_report` (numbers only), `face_detected`; with content capture on also `sha256`, `artifact`, `thumb_width`, `thumb_height`, `preview_url`, `preview_expires` |
| `drone_comm` (`drone_comm`) | INFO; WARNING for nacks, emergency lands, failed sends, link down; ERROR when reconnecting gave up | `flightrisk/obs_comm.py` from the ground and edge transports; `flightrisk/drone/instrument.py` for local drone commands | `direction` (`in`, `out`, `link`), `type` (message type or `command`), `peer`/`drone_id`, `size` (bytes), `outcome`, and when present `cmd_id`, `epoch`, `plan_id`, `seq`, `for_`, `reason`, `lanes`, `detections`, `reached`/`clients` (broadcasts), `has_reid_embedding`/`has_face_embedding` (target pushes; never the vectors). Local commands add `channel=local`, `command`, `backend`, `duration_ms`, `move_direction`, `distance_cm`, `degrees`, `detail`. High-rate types (`telemetry`, `heartbeat`, `ping`, `pong`, `detections`, `peer_heartbeat`, `node_state`) are logged once per 5 s per side, direction, type and drone with `aggregated`, `count`, `bytes`, `window_s`, `rate_per_s`. A clock sync is `type=clock_sync` with `rtt_ms` and `offset_ms` |
| `drone_telemetry_snapshot` (`drone_telemetry`) | INFO, once per drone every 5 s | frame loop (local drones) and the ground transport (edge `telemetry`) | `backend`, `connected`, `is_flying`, `is_flying_source` (`commanded`/`sensed`), `flight_mode`, `readings` (per reading: `value`, `unit`, `source` such as `tello.h`, `tello.tof`, `sim.height`, `mavlink.position`, and `age_s`), `oldest_age_s`, `quality_flags` (`height_zero_while_flying`), `samples_since_last`. Edge snapshots carry the telemetry message fields plus `height_source` and `position_source` |
| `alert` (`alert`) | WARNING for opened, renotified, confirmed; INFO for dismissed and aborted; DEBUG for the per-session reset | alert latch listener (`flightrisk/dashboard/alert_latch.py`) | `transition`, `phase`, `alert_id`, `awaiting_s` (operator response time), `renotify_count`, `alert_level` |
| `flight_safety` (`flight_safety`) | WARNING; ERROR for critical battery, land failures, a failed confirm sequence | pipeline battery policy, search executor, fleet supervisor, recovery, drone instrumentation | `kind` (the edge events above add `land`, `link_loss`, `watchdog_land`, `manual_land` and the other kinds of `SAFETY_EVENTS` in `flightrisk/obs_events.py`; the dashboard adds `battery_low`, `battery_critical`, `land_failed`, `drone_lost`, `fleet_start_refused`, `obstacle`, `obstacle_check_skipped`, `obstacle_guard_unavailable`, `waypoint_skipped`, `confirm_recovery`), `reason`, `drone_id` and kind-specific numbers (`battery_pct`, `threshold`, `center_depth`, `margin`, `confidence`, `waypoint`, `retry`, `duration_s`) |
| Lane-executor and edge events | as logged | `flightrisk/drone/lane_executor.py`, `flightrisk/edge_device.py` (unchanged) | `edge_land`, `edge_link_lost`, `edge_land_not_executed`, `edge_land_error`, `edge_geofence_refused`, `edge_takeoff_refused`, `edge_assign_lanes_refused`, `edge_start_refused`, `edge_fail_safe_idle`, `edge_altitude_unknown`, `edge_local_land_key`, `edge_sigint_land`, `edge_pose_reset` (a takeoff made the drone's spot its new takeoff point; carries where the previous flight ended as `previous_x_cm`/`previous_y_cm`) and similar get `category=flight_safety` and are counted in `flightrisk_flight_safety_events`; an `edge_land` with reason `link_loss` counts as `kind=watchdog_land`. `edge_telemetry_dropped` and `edge_send_error` count as dropped messages |
| `detection_pipeline` (`detection_pipeline`) | INFO, once per 5 s | frame loop and the edge-detection consumer | `source`, `frames`, `window_s`, `fps`, `persons`, `stages_ms` (mean and max per stage), `scores` (mean, max, n per signal), `alert_levels` |
| `model_load` (`detection_pipeline`) | INFO when loaded, WARNING when it failed or is unavailable | dashboard initialisation, signal registry | `model`, `outcome`, `error_type`, `error` |
| `reasoning` (`reasoning`) | INFO; WARNING when the outcome is `error` or `unavailable` | the reasoning worker (`flightrisk/dashboard/alerts.py`) | `provider`, `model`, `kind` (`analyze`/`describe`), `duration_ms`, `outcome` (`match`, `no_match`, `error`, `unavailable`, `off`), `tokens_in`, `tokens_out`, `error_type`. Never the prompt or the reasoning text |
| `queue_dropped` (`error`) | WARNING, at most once per 10 s per queue | frame loop | `queue`, `kind` |
| `operator_error` (`error`) | WARNING | any `error` event the dashboard sends to the browser outside a handler | `message` (redacted) |
| `edge_drone_link_unhealthy` (`flight_safety`) | WARNING, once per change of reason | lane-executor monitor (`flightrisk/drone/lane_monitor.py`), checked every 0.5 s with telemetry | `drone_id`, `reason` (`not_connected`, `no_state`, `state_stale`, `command_failed`, `unreadable`), `detail` (operator text such as `Tello unreachable (no state for 12 s)`), `drone_link_age_s`, `is_flying`. Only a controller that reports its link (the Tello) logs it. It never lands or holds the drone; it only refuses takeoffs (see `docs/fleet-protocol-v2.md`, "Drone link preflight") |
| `edge_drone_link_restored` (`flight_safety`) | INFO | lane-executor monitor | `drone_id`, `reason` (the problem that ended) |
| `edge_takeoff_refused` with `reason=drone_unreachable` (`flight_safety`) | WARNING | `flightrisk/drone/lane_flight.py`, right before a takeoff would be sent | `drone_id`, `reason=drone_unreachable`, `link_reason` (as above), `detail`, `drone_link_age_s`. Nothing was sent to the drone. The same event also has `reason` `battery`, `link_loss` or `landing` |
| `edge_start_refused` with `reason=drone_unreachable` (`flight_safety`) | WARNING | `flightrisk/drone/lane_executor.py`, when `start_at` arrives | `drone_id`, `reason=drone_unreachable`, `link_reason`, `detail`, `drone_link_age_s`; a v2 `start_at` is answered `nack{reason: drone_unreachable, detail}` |
| Tello console events | as printed | `flightrisk/drone/tello.py` | `tello_connected`, `tello_link_lost`, `tello_land_confirmed`, `tello_land_unconfirmed`, `tello_takeoff_refused`, `tello_stream_stale`, ...: each operator console line is also a structured event. `tello_get_frame_failed`, `tello_keepalive_failed`, `tello_poll_failed` (with `consecutive`) and `tello_command_dropped` record failures that used to be silent, at most once per 10 s per drone |

### What `input` contains

`input` is the event's payload after `flightrisk/obs_redact.py`:

- Credential-named keys (`api_key`, `auth`, `token`, ...) are `[REDACTED]`. The Socket.IO
  connect `auth` object is therefore always redacted.
- Image and binary values (`image`, `photo`, `frame`, `file`, ...) become their size
  (`b64_chars`, `approx_bytes`). An uploaded file appears as its form field, file-name length
  and MIME type. Numeric vectors longer than 32 items become `{"numbers": n}`.
- With `FLIGHTRISK_TELEMETRY_CONTENT` off (the default in code), free text (`description`,
  `notes`, `comment`, `text`, `query`) and any string that is not a short control token become
  `{"chars": n}`. Short tokens such as a preset name, a command, a drone id or an alert id are
  kept.
- With `FLIGHTRISK_TELEMETRY_CONTENT=1`, free text is kept after redaction, capped at 500
  characters.

## Metrics

Prometheus names are the OTel names from `flightrisk/obs_metrics.py` with dots turned into
underscores (no `_total` or unit suffix). Histograms add `_bucket`, `_sum` and `_count`. Every
series also carries `service_name` and `service_instance_id` (the node).

| Metric | Type | Labels |
| --- | --- | --- |
| `flightrisk_http_requests` | counter | `route`, `method`, `status_class`, `outcome` |
| `flightrisk_http_duration_ms` | histogram | `route`, `method` |
| `flightrisk_socket_events` | counter | `event`, `outcome` |
| `flightrisk_socket_duration_ms` | histogram | `event` |
| `flightrisk_socket_emits` | counter | `event` |
| `flightrisk_stage_duration_ms` | histogram | `stage` (`detect`, `track`, `reid`, `face`, `score`, `annotate`, `encode`, `emit`, `edge_score`) |
| `flightrisk_frames` | counter | `source` |
| `flightrisk_detections_per_frame` | histogram | none |
| `flightrisk_signal_score` | histogram (0 to 1) | `signal` |
| `flightrisk_alert_levels` | counter | `level` |
| `flightrisk_alert_transitions`, `flightrisk_alert_phase`, `flightrisk_alert_response_s` | counter, gauge, histogram | `transition`, `phase`, `action` |
| `flightrisk_model_loads` | counter | `model`, `outcome` |
| `flightrisk_reasoning_calls`, `flightrisk_reasoning_duration_ms`, `flightrisk_reasoning_tokens` | counter, histogram, counter | `provider`, `kind`, `outcome`, `direction` |
| `flightrisk_queue_depth`, `flightrisk_queue_dropped` | gauge, counter | `queue` |
| `flightrisk_error_logs` | counter | `component`, `error_type`, `fingerprint`, `level` |
| `flightrisk_target_changes` | counter | `source` |
| `flightrisk_drone_battery_pct`, `flightrisk_drone_flying` | gauge | `drone_id`, `backend` |
| `flightrisk_drone_height_m` | gauge | `drone_id`, `backend`, `source`, `kind` |
| `flightrisk_drone_speed_mps` | gauge | `drone_id`, `backend`, `axis`, `source` |
| `flightrisk_drone_attitude_deg` | gauge | `drone_id`, `backend`, `axis` |
| `flightrisk_drone_telemetry_age_s` | gauge, computed at collection | `drone_id`, `backend` |
| `flightrisk_drone_commands` | counter | `drone_id`, `backend`, `command`, `outcome` |
| `flightrisk_drone_command_duration_ms` | histogram | `backend`, `command`, `outcome` |
| `flightrisk_drone_land_duration_ms` | histogram | `backend`, `outcome` |
| `flightrisk_flight_safety_events` | counter | `kind`, `reason`, `drone_id` |
| `flightrisk_obstacle_checks`, `flightrisk_obstacle_depth_ratio` | counter, histogram | `action` |
| `flightrisk_comm_messages`, `flightrisk_comm_bytes` | counters | `side`, `direction`, `type`, `drone_id`, `outcome` |
| `flightrisk_comm_acks`, `flightrisk_comm_dropped` | counters | `kind`, `for`, `reason`, `drone_id`, `side`, `type` |
| `flightrisk_comm_rtt_ms`, `flightrisk_link_clock_offset_ms` | histogram, gauge | `drone_id` |
| `flightrisk_link_age_s` | gauge, computed at collection | `drone_id`, `side` |
| `flightrisk_link_connected` | gauge, computed at collection | `drone_id` |
| `flightrisk_link_reconnects` | counter | `outcome` |
| `flightrisk_frame_loop_last_frame_age_s` | gauge, computed at collection | `source_active` |
| `flightrisk_process_usage` | gauge | `resource` (`cpu_percent`, `memory_rss_bytes`, `threads`, `open_fds`) |
| `flightrisk_process_uptime_s`, `flightrisk_log_dropped` | gauges | |

The PR #88 metrics (`flightrisk_fps`, `flightrisk_drone_battery`, `flightrisk_frame_duration_ms`,
`flightrisk_match_count`, `flightrisk_match_score`, `flightrisk_face_detection_rate`,
`flightrisk_detections_count`, `flightrisk_errors_count`) are unchanged.

Labels never carry alert ids, track ids, file names or free text. A `type` label is a known
protocol message type (anything else is `other`), and at most 64 distinct drone ids from the
network become a `drone_id` label (later ones are `other`), so a client that invents ids
cannot create series without end. Every series from a process also carries `process_pid`.

## Traces

- `frame`: one frame in `FLIGHTRISK_FRAME_TRACE_EVERY` (default 5) is traced, with a child span
  per stage (`detect`, `reid_match`, `face_match`). Every frame is timed in the metrics.
- `socket <event>`: every Socket.IO handler. REST requests get spans from the Flask
  instrumentation.
- `drone.<command>`: every command to a registry-built drone (sim, Tello, MAVLink).
- `fleet.command <type>`, `fleet.plan`, `fleet.start`: coordinator sends and fleet actions.
- `fleet.send <type>`, `fleet.recv <type>`: short spans around each individually logged
  ground/edge message, so its log line links to a trace. Trace context is not carried across
  the WebSocket, so a ground send and the edge's receipt are separate traces.

Histograms carry exemplars (the trace id of a sampled observation), so a latency spike in
Grafana links to a trace.
