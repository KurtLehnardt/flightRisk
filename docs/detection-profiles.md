# Detection profiles

A detection profile is a named set of person-detector settings: the model
weights, tiling, confidence and NMS thresholds, input size, the person sizes
it is expected to find, and hints for how the ground weights face and ReID
matching. The operator picks a profile per drone (or a fleet default) in the
dashboard, and the edge switches its detector to match. Profiles live in a
registry, so adding one is a single `register` call; no code branches on a
profile name.

## The profiles

| Name | Label | Weights | Tiling | Confidence | Face / ReID weight | Fallback |
| --- | --- | --- | --- | --- | --- | --- |
| `default` | Default (startup settings) | `yolo11n.pt` (stock, fetched by Ultralytics when missing) | follows `FLIGHTRISK_DETECTOR_TILED` (off by default) | 0.4 | 1.0 / 1.0 | none |
| `top_down` | Top-down search (HERIDAL) | `models/heridal_yolo11s.pt` (required on disk) | on, tile size and overlap from `FLIGHTRISK_DETECTOR_TILE_SIZE` / `FLIGHTRISK_DETECTOR_TILE_OVERLAP` (640 px, 0.2) | 0.5 | 0.5 / 1.0 | `level` |
| `level` | Level flight (faces) | `yolo11n.pt` | off (whole frame at the model's native 640 px input) | 0.4 | 1.5 / 1.0 | none |

- `default` is exactly the detector the edge built before profiles existed
  (`PersonDetector(model_name="yolo11n.pt", confidence=0.4)`). An edge with no
  profile selected runs it, so nothing changes until someone selects a
  profile.
- `top_down` is the fine-tuned YOLO11s from `docs/altitude-benchmark.md`, run
  tiled at confidence 0.5, the setting that benchmark measured. Its
  `min_person_px` is 32, the conservative reading of that benchmark; it is a
  planning hint, not a filter. The weights are not in git (see
  `scripts/finetune_heridal.py`); without them the edge falls back to `level`.
- `level` is the general model on the whole frame for level flight, where
  people are larger and faces may be visible.
- NMS uses each detector's default (`nms_iou` unset): the model's own
  threshold per call and 0.5 when merging tiles.

### Weighting hints

The face and ReID weight scales are applied by the ground, which does the
matching. For a detection message from a drone running a profile, the
ground multiplies the scorer's signal weights by the profile's scales before
normalising them (`MatchScorer.score(weight_scale=...)`). `default`, an
unknown profile and an edge that reports no profile use the configured
weights unchanged. The hints never turn face recognition on: whether an edge
sends face embeddings at all is still decided by the appearance-only rules
(`--with-face`, `FLIGHTRISK_APPEARANCE_ONLY`), so with the default
appearance-only flying edge only the ReID scale has an effect.

### Adding a profile

```python
from flightrisk.vision.profiles import REGISTRY, DetectionProfile

REGISTRY.register(DetectionProfile(
    name="thermal_nadir",            # lowercase letters, digits and _, at most 32 characters
    label="Thermal, top-down",
    description="IR camera looking straight down.",
    weights="models/thermal_yolo11s.pt",
    weights_required=True,
    confidence=0.45,
    tiled=True,
    fallback="top_down",
))
```

Register it on both the edge and the ground (the ground uses the registry for
the dashboard choices, the metric labels and the weighting hints). An edge
answers a request for a profile it does not know with
`nack{reason: unknown_profile}`.

## Selecting the startup profile

- `--detection-profile NAME` on `python -m flightrisk.edge_device`, or
- `FLIGHTRISK_DETECTION_PROFILE=NAME` (read through `flightrisk/config.py`).

The flag wins. An unknown name in the environment variable, or a profile
whose model fails to load, starts the edge on `default` with a
`profile_switch_refused` warning; a profile whose required weights are
missing starts on its fallback with a `profile_fallback` warning. Only a
failure to load `default` itself stops the edge, as before.

## Switching from the dashboard

The **Detection Profile** section of the Controls tab (edge source only)
has a fleet default selector and one row per drone with its active profile.

- **Per drone.** Choosing a profile sends `set_detection_profile` to that
  drone. Choosing "Fleet default" drops the drone's own choice and applies
  the fleet default.
- **Fleet default.** Setting it sends the profile to every drone that follows
  the default and runs another profile, and once to each drone that first
  reports profile support later. "Keep each drone's startup profile" (no
  default) sends nothing.
- **Status.** Each row shows the active profile, "Switching...", a fallback
  ("fallback: Top-down weights missing on the edge"), a refusal with the
  edge's reason ("refused in flight: Detection profiles can only change on
  the ground..."), no reply, or that the edge is too old to switch.

## Safety

- A switch happens only on the ground. The edge refuses it with
  `nack{reason: in_flight}` while the lane executor reports activity
  (`LaneExecutor.is_active`): the drone flying, a takeoff, a lane run, a
  scheduled start, a nudge, or a landing in progress. An unreadable flight
  state counts as flying. The check runs again after the new model has
  loaded and before it replaces the old one.
- The switcher only reads flight state. It sends no flight command and never
  touches landing, the geofence, the link watchdog or the lane executor.
  Its `handle` never raises, so a bad profile message can never reach the
  edge device's fail-safe landing path.
- The swap is atomic. The new detector is built first, on its own thread, so
  the frame loop and the control thread never wait for a model load; it then
  replaces the old detector and the profile name in one step
  (`EdgeRunner.swap_detector`). A frame already being processed finishes on
  the old detector. If the load fails, the old detector keeps running and
  the edge answers `nack{reason: load_failed}`.
- One switch runs at a time; another request meanwhile gets
  `nack{reason: busy}`.

## Fallback

When the requested profile's weights file is missing, the edge follows the
profile's `fallback` chain to the first profile that can be loaded
(`top_down` falls back to `level`). It loads that one, logs a
`profile_fallback` warning, and answers with an ack that says so:
`{"profile": "level", "requested": "top_down", "fallback": true, "fallback_reason": "weights_missing"}`.
Telemetry then carries `detection_profile_fallback: true` and
`detection_profile_requested`, so the dashboard keeps showing the fallback.
When nothing in the chain can be loaded the answer is
`nack{reason: unavailable}` and the current detector stays.

## Protocol

`set_detection_profile` is a configuration command
(`fleet_protocol.CONFIG_COMMAND_TYPES`). See `docs/fleet-protocol-v2.md` for
the message table.

| Direction | Message |
| --- | --- |
| ground to edge | `{"type": "set_detection_profile", "profile": "top_down", "cmd_id": "...", "drone_id": "drone-1"}` (`drone_id` optional) |
| edge to ground | `{"type": "ack", "for": "set_detection_profile", "drone_id": ..., "cmd_id": ..., "result": "ok", "profile": "level", "requested": "top_down", "fallback": true, "fallback_reason": "weights_missing"}` |
| edge to ground | `{"type": "nack", "for": "set_detection_profile", "drone_id": ..., "cmd_id": ..., "reason": "in_flight", "profile": "default", "requested": "top_down", "detail": "Detection profiles can only change on the ground: ..."}` |

- **Nack reasons** (`PROFILE_NACK_REASONS`): `in_flight`, `unknown_profile`,
  `unavailable`, `load_failed`, `busy`, `invalid`. The v1 and v2 nack rules for
  lane and flight commands are unchanged.
- **Every link version.** The command and its replies are valid on v1 and v2
  links and keep their `cmd_id` on both (`shape_for_peer` copies them), so
  the single-ground `--ws-url` edge (v1) and the multi-ground uplink (v2)
  both support it. It is not fenced, because it cannot move the drone. A
  multi-ground edge obeys it only from its authority, like `stream_video`;
  from any other ground it is ignored.
- **At most once.** The edge keeps the last 64 replies by `cmd_id`; a repeat
  gets the same reply and never loads a model twice.
- **Status.** An edge that supports profiles names its active profile in
  every `detections` message (`detection_profile`) and, when it flies, in its
  telemetry (`detection_profile`, `detection_profile_fallback`,
  `detection_profile_requested`). These are additional fields, which every
  ground version accepts on an edge link.

### Compatibility

| Ground | Edge | What happens |
| --- | --- | --- |
| old | new | The ground never sends the command. It ignores the extra fields in detections and telemetry. The edge runs its startup profile. In a mixed two-ground fleet, a multi-ground edge sends its profile ack to every ground; an old ground drops it with a "Malformed ack message, ignoring" warning. |
| new | old | The old edge never reports a profile, so the dashboard shows it as not supporting profiles and the ground never sends it the command. Had one been sent, the old edge would drop it as an unknown type (both the single-ground and the multi-ground edges do). |
| new | new | Full support on v1 and v2 links. |

No protocol version change was needed: support is advertised by the
`detection_profile` field, in the same way an extra field on an edge link is
allowed under "Unknown fields" in `docs/fleet-protocol-v2.md`.

## Observability

- Edge log events, each with `drone_id` and category `detection_pipeline`:
  `profile_selected`, `profile_fallback` (WARNING) and
  `profile_switch_refused` (WARNING). See `docs/observability-events.md`.
- `flightrisk_detection_profile_events` counts them by `event`, `profile`
  and `reason`.
- `flightrisk_frames` and `flightrisk_detections_per_frame` carry a
  `profile` label for edge detections from an edge that reports one. Labels
  are bounded: a registered profile name, otherwise `other`.

## Limitations

- A camera or file edge (`--source camera` or `file`) sends no `drone_id`,
  so the dashboard cannot address it; it keeps its startup profile.
- A multi-ground edge in legacy mode (`FLIGHTRISK_FLEET_LEGACY=1`) ignores
  the command from a v1 ground, as it ignores `stream_video` there; the
  dashboard then shows "no reply".
- The edge `hello` does not carry the profile; detections and telemetry do.
- An edge that restarts comes back on its startup profile. The dashboard
  shows the profile it reports; it does not re-send the operator's earlier
  per-drone choice.
- The thresholds and weight scales are first settings, not tuned values.
  Only `top_down` has measured numbers (`docs/altitude-benchmark.md`).
