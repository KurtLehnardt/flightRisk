# FlightRisk Privacy Design

This document records how FlightRisk handles imagery of people, what is stored and sent and where, and which settings control it. It describes the current code on this branch, including the appearance-only default of the demo launchers and the off-by-default captures. The user-facing policy for the mobile app is `docs/privacy-policy.md`.

## Appearance-only

Appearance-only matching compares people by clothing color, height ratio and re-identification embeddings (OSNet, CLIP ReID) and never runs face recognition.

- Ground station: `VisionConfig.appearance_only` (env `FLIGHTRISK_APPEARANCE_ONLY`, CLI `--appearance-only` and `--with-face` on `python -m flightrisk.dashboard`) controls whether `_init_pipeline` constructs `FaceRecognizer` (`flightrisk/dashboard/app.py`, around lines 1017-1050). When it is true, no `FaceRecognizer` is built and the `insightface_face` signal is disabled. The config default is false, so a ground station embedded some other way (without the `python -m flightrisk.dashboard` launcher) builds the face recognizer. The `python -m flightrisk.dashboard` launcher defaults to appearance-only unless `--with-face` or `FLIGHTRISK_APPEARANCE_ONLY=0` is given, and `scripts/demo_ground.sh` exports `FLIGHTRISK_APPEARANCE_ONLY=1`, so the demo ground station is appearance-only. `/api/status` reports `appearance_only` and the UI shows a badge.
- On the edge, flight sources (`tello`, `fake`) are appearance-only by default: the edge builds no `FaceRecognizer` and sends no face embeddings. Face matching on the edge needs `--with-face` on `edge_device` or `FLIGHTRISK_APPEARANCE_ONLY=0`. The demo launchers set `FLIGHTRISK_APPEARANCE_ONLY=1`.
- Person crops (JPEG, one per detection) and ReID appearance embeddings do leave the edge: each edge sends them to the ground station over the edge WebSocket link, which is token-authenticated with the shared `FLIGHTRISK_EDGE_TOKEN` when that token is set. The demo launchers refuse to start without it, and the ground station refuses a non-loopback bind without it. In appearance-only mode, the default for the `tello` and `fake` sources, no face embeddings are computed or sent. Detections carry the drone id and relative position.
- The edge link is plain `ws://`, not `wss://`. The shared token authenticates the edge but nothing is encrypted in transit, so the link belongs on a private network (the fleet access point or a trusted LAN) and not on the open internet.
- Face and appearance embeddings are computed in process memory only. They are never written to disk. The dashboard and the mobile app never send face embeddings to an analytics or cloud service. There is one exception inside the edge link: when the ground station is not appearance-only and a target photo is set, `flightrisk/dashboard/app.py` (around lines 1569-1574) extracts the target's face embedding and `broadcast_target` sends it, with the ReID embedding, to every connected edge. This does not happen in the default demo configuration, where the ground station is appearance-only, no face recognizer exists and the face embedding is `None`. The edge also sends its own face embeddings back only when face matching is switched on as above.

## Faces at altitude

Faces are not resolvable at search altitude. The system searches from 60 to 120 m, where a person occupies roughly 20 to 60 px in height in the HERIDAL aerial dataset. A face is a few pixels wide at that scale, so face recognition has nothing to work with and appearance-only matching is the honest design, not a downgrade. The face path remains in the code for close-range use (for example the mobile app held at street level) and is off by default for the aerial demo. InsightFace pretrained model packs are licensed for non-commercial research only, which is a second reason not to depend on them.

## Captures

Captures are match snapshots: the full frame, the person crop and a metadata JSON.

- `DashboardConfig.save_captures` defaults to false (env `FLIGHTRISK_SAVE_CAPTURES`). `maybe_save_match_snapshot` (`flightrisk/dashboard/alerts.py`) is a no-op unless it is enabled, `captures/` is created only when a snapshot is actually saved, and the test suite points `CAPTURES_DIR` at a temporary directory. With the defaults, no bystander imagery is written to disk by the capture path. When enabled, the files are ordinary JPEGs that the application does not encrypt.
- Bystander blur and a consent gate for target upload are out of scope for this build. Keeping captures off is the mitigation.

## What is stored and sent, and where

| Data | Where | Behavior |
|---|---|---|
| Face and appearance embeddings | Process memory | Never persisted. Each edge sends ReID appearance embeddings to the ground station, and face embeddings only when face matching is on (see Appearance-only) |
| Match snapshots (frame, crop, JSON) | `captures/` on the ground station | Not written unless `save_captures` is true |
| Match rows (scores, timestamps, reasoning text) | SQLite `flightrisk_sessions.db` (`flightrisk/persistence.py`) | Stored; reasoning and description fields are encrypted only if `FLIGHTRISK_ENCRYPTION_KEY` is set |
| Target photo path and the photo itself | Path in SQLite; photo kept in memory and shown in the dashboard | Stored as given |
| Match crops sent to the browser | Socket.IO to the operator's dashboard as base64 JPEG | Sent to the connected dashboard only |
| Video recordings | `recordings/` (`flightrisk/recorder.py`), only when the operator starts a recording | Not encrypted |
| Crops and reference photo sent to a reasoning model | Local Ollama by default (`flightrisk/reasoning/agent.py`; `OLLAMA_HOST` may point to another machine) | Sent to that host; not started when `--no-llm` is used |
| Reference photo and person crops sent to a cloud LLM | Mobile app only: Anthropic, OpenAI or Google, via `CloudClaudeLlmBackend` and its siblings | Sent only after the user selects a cloud backend and stores an API key |
| Telemetry logs, metrics and traces | The local Grafana stack (Loki, Prometheus, Tempo volumes), only when `FLIGHTRISK_OTEL_ENABLED=true` | Operator actions, drone communication, flight-safety and pipeline events as numbers, ids and short tokens; secrets redacted; never embeddings, crops, frames or reasoning text. See `observability/README.md` |
| Operator text and the target photo in telemetry | Operator text in the Loki logs; the photo and a thumbnail in `~/.flightrisk/artifacts/targets/` (mode 0700, files 0600), served to Grafana by a 127.0.0.1-only preview server with expiring signed URLs | Only when `FLIGHTRISK_TELEMETRY_CONTENT=1` (off by default in code); text redacted and capped at 500 characters |

The mobile app stores match thumbnails, scores, GPS fixes and timestamps in a local SQLite database on the phone. Nothing in the dashboard or the mobile app sends data to an analytics or advertising service.

The cloud LLM path is the one place images can leave the user's device. It sends the reference photo and the candidate crop (both can show a face) to the provider the user chose. The earlier wording of the policy that no biometric data leaves the phone was true of embeddings and templates only. The policy now states both facts.

## Test fixtures

Tests use only synthetic imagery: rectangles and noise arrays generated in code, and the drawn-rectangle fixtures under `tests/fixtures/aerial_tiny/` (t03). No real person images, face datasets or captured footage are committed. Tests that exercise snapshot saving, recording or the session database write only under pytest's `tmp_path`, never into `captures/` or `recordings/`, and tests never contact a cloud LLM or a real drone. HERIDAL images are downloaded by script into a gitignored `datasets/` directory and are not part of the repository.

## Retention

- Embeddings: discarded when the session or process ends.
- Captures: kept until the operator deletes the files in `captures/`; with the default settings none are written.
- Session database rows and recordings: kept until the operator deletes `flightrisk_sessions.db` or the files in `recordings/`. There is no automatic expiry.
- Mobile app: thumbnails, scores and locations stay until the user deletes them in settings or uninstalls the app.
- Cloud LLM: retention after transmission is governed by the provider's policy and is outside FlightRisk's control.
- Telemetry: logs, metrics and traces are deleted after 7 days by the Grafana stack (`LOKI_RETENTION_PERIOD` for logs); target-photo artifacts after `FLIGHTRISK_ARTIFACT_RETENTION_DAYS` (default 7), at most 50 kept.
- Deployments that handle real imagery must use filesystem-level encryption, because image files are not encrypted by the application.
