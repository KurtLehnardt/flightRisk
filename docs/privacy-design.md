# FlightRisk Privacy Design

This document records how FlightRisk handles imagery of people, what is stored and sent and where, and which settings control it. It describes two things separately: the behavior of the code on `origin/main` at commit `798f21a`, and the planned changes from tasks t05 (captures off by default) and t09 (appearance-only default for the demo launcher). Each statement is marked "today" or "planned" where the two differ. The user-facing policy for the mobile app is `docs/privacy-policy.md`.

## Appearance-only

Appearance-only matching compares people by clothing color, height ratio and re-identification embeddings (OSNet, CLIP ReID) and never runs face recognition.

- Today: `_init_pipeline` always constructs `FaceRecognizer` (`flightrisk/dashboard/app.py:345-350`). The `insightface_face` signal is enabled by default (`flightrisk/config.py`, `signal_insightface_face`).
- Planned (t09, R-001, R-002): `VisionConfig.appearance_only` and the env override `FLIGHTRISK_APPEARANCE_ONLY` are added. When it is true, `_init_pipeline` does not construct `FaceRecognizer` and disables the `insightface_face` signal. The demo launcher sets it to true, so the demo runs appearance-only by default. `/api/status` reports `appearance_only` and the UI shows a badge.
- On the edge, flight sources (`tello`, `fake`) are appearance-only by default: the edge builds no `FaceRecognizer` and sends no face embeddings. Face matching on the edge needs `--with-face` on `edge_device` or `FLIGHTRISK_APPEARANCE_ONLY=0`. The demo launchers set `FLIGHTRISK_APPEARANCE_ONLY=1`.
- Person crops do leave the edge: each edge sends them to the ground station over the token-authenticated WebSocket link, which needs the shared `FLIGHTRISK_EDGE_TOKEN`. Detections carry the drone id and relative position.
- Face embeddings and appearance embeddings are computed in process memory only. They are never written to disk. Face embeddings are never sent over the network by the dashboard or the mobile app, and not by the edge unless face matching is switched on as above.

## Faces at altitude

Faces are not resolvable at search altitude. The system searches from 60 to 120 m, where a person occupies roughly 20 to 60 px in height in the HERIDAL aerial dataset. A face is a few pixels wide at that scale, so face recognition has nothing to work with and appearance-only matching is the honest design, not a downgrade. The face path remains in the code for close-range use (for example the mobile app held at street level) and is off by default for the aerial demo. InsightFace pretrained model packs are licensed for non-commercial research only, which is a second reason not to depend on them.

## Captures

Captures are match snapshots: the full frame, the person crop and a metadata JSON.

- Today: every confirmed match is written to `captures/` by `_save_match_snapshot` (`flightrisk/dashboard/alerts.py:56-83`), called from the Gemma worker. `captures/` is created at import time (`alerts.py:29-30`). These are ordinary JPEG files; the application does not encrypt them.
- Planned (t05, R-003, R-005): `DashboardConfig.save_captures` defaults to false (env `FLIGHTRISK_SAVE_CAPTURES`). `maybe_save_match_snapshot` is a no-op unless it is enabled, the import-time `mkdir` is removed, and the test suite points `CAPTURES_DIR` at a temporary directory. With the defaults, no bystander imagery is written to disk by the capture path.
- Bystander blur and a consent gate for target upload are out of scope for this build (R-004, R-007 dropped). Keeping captures off is the mitigation.

## What is stored and sent, and where

| Data | Where | Today | Planned default |
|---|---|---|---|
| Face and appearance embeddings | Process memory | Never persisted, never sent | Same |
| Match snapshots (frame, crop, JSON) | `captures/` on the ground station | Written for every match | Not written unless `save_captures` is true |
| Match rows (scores, timestamps, reasoning text) | SQLite `flightrisk_sessions.db` (`flightrisk/persistence.py`) | Stored; reasoning and description fields are encrypted only if `FLIGHTRISK_ENCRYPTION_KEY` is set | Same |
| Target photo path and the photo itself | Path in SQLite; photo kept in memory and shown in the dashboard | Stored as given | Same |
| Match crops sent to the browser | Socket.IO to the operator's dashboard as base64 JPEG | Sent to the connected dashboard only | Same |
| Video recordings | `recordings/` (`flightrisk/recorder.py`), only when the operator starts a recording | Not encrypted | Same |
| Crops and reference photo sent to a reasoning model | Local Ollama by default (`flightrisk/reasoning/agent.py`; `OLLAMA_HOST` may point to another machine) | Sent to that host | Not started when `--no-llm` is used (t09, R-019) |
| Reference photo and person crops sent to a cloud LLM | Mobile app only: Anthropic, OpenAI or Google, via `CloudClaudeLlmBackend` and its siblings | Sent only after the user selects a cloud backend and stores an API key | Same |

The mobile app stores match thumbnails, scores, GPS fixes and timestamps in a local SQLite database on the phone. Nothing in the dashboard or the mobile app sends data to an analytics or advertising service.

The cloud LLM path is the one place images can leave the user's device. It sends the reference photo and the candidate crop (both can show a face) to the provider the user chose. The earlier wording of the policy that no biometric data leaves the phone was true of embeddings and templates only. The policy now states both facts.

## Test fixtures

Tests use only synthetic imagery: rectangles and noise arrays generated in code, and the drawn-rectangle fixtures planned under `tests/fixtures/aerial_tiny/` (t03). No real person images, face datasets or captured footage are committed. Tests that exercise snapshot saving, recording or the session database write only under pytest's `tmp_path`, never into `captures/` or `recordings/`, and tests never contact a cloud LLM or a real drone. HERIDAL images are downloaded by script into a gitignored `datasets/` directory and are not part of the repository.

## Retention

- Embeddings: discarded when the session or process ends.
- Captures: kept until the operator deletes the files in `captures/`; planned default is that none are written.
- Session database rows and recordings: kept until the operator deletes `flightrisk_sessions.db` or the files in `recordings/`. There is no automatic expiry.
- Mobile app: thumbnails, scores and locations stay until the user deletes them in settings or uninstalls the app.
- Cloud LLM: retention after transmission is governed by the provider's policy and is outside FlightRisk's control.
- Deployments that handle real imagery must use filesystem-level encryption, because image files are not encrypted by the application.
