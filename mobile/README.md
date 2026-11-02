# FlightRisk Android

The Android app has three modes. It is a standalone detector, a console for a FlightRisk ground station, and (optionally) an edge that sends the phone's own detections to a ground station. This README describes the code on `main`; every statement was checked against it. The wire contract shared with the iOS app is [`docs/mobile-contract.md`](../docs/mobile-contract.md). Open items are in [`docs/mobile-followups.md`](../docs/mobile-followups.md).

## The three modes

1. **Standalone detector.** The camera finds people and compares them with a reference photo, on the device (ONNX Runtime and TensorFlow Lite models). The optional reasoning step uses the on-device Gemma 2 2B model (MediaPipe), nothing, or Cloud Claude after an explicit opt-in. The app also still contains the direct Tello (UDP over the drone's own Wi-Fi) flight stack in `drone/`; the iOS app no longer has one.
2. **Ground console.** The Ground tab connects to a ground station (a computer the user runs) and shows the drones, the plan, the detection profiles, the ceiling settings and the ground's reasoning provider. It can send a small fixed set of commands.
3. **Phone as an edge** (Settings, "Ground station link"). Off by default. The phone sends its detections to the ground station's edge port. See "Phone as an edge".

## Modules

Gradle project `FlightRisk` with two modules (`settings.gradle.kts`):

- **`:contract`** is pure Kotlin on the JVM, with no Android dependency: the typed models for every server event, the state reducer and derivations, `ClientCommand` (the allow-list of what the phone may emit), `DisplayText` (the shared wording), the plan geometry, the Socket.IO frame codec, and `DetectionsWire` and `EdgeLink` for the edge link. It is tested against the JSON fixtures in `tests/fixtures/mobile_contract/`, which the real Python code produces.
- **`:app`** is the Android app: `ground/` (networking, secrets, the console client), `ui/ground/` (the Ground screen), `llm/` (reasoning backends and the cloud opt-in gate), `transport/` (the edge publisher), `config/`, `ui/settings/`, `ui/onboarding/`, and the detector (`vision/`, `pipeline/`, `camera/`, `drone/`).

## The Ground screen

The screen has a pinned top bar and a scrolling list. The top bar holds the connection state and LAND ALL, which is always visible and always enabled. The list holds, from the top: a stale banner when the state is not live, toasts and banners from the ground, a readout, one card per drone (link chip, control chip with the HELD countdown, battery, height, lane progress, the drone-link notice, Land, and Resume search only for a held drone nobody controls), the detection profiles card (the only card with controls), the ceiling card and the reasoning card (both read only), a read-only plan canvas, and, last, the connection card (address, dashboard API key, "Test connection", and the connect switch).

**What the phone can command** is exactly the contract allow-list: LAND ALL, Land for one drone, Resume search of a held drone, and the selection of a detection profile (per drone or the fleet default), plus the five state requests on every connect. Nothing else. `ClientCommand` is the only way to send, and the client's emit policy is narrowed to that set.

The safety display rules are the ones in `docs/mobile-contract.md` ("Safety display rules"): LAND ALL pinned and always enabled, per-drone Land always enabled, stale values marked "LAST: ..." and "(last value)", "no final answer from the drone's edge" after 9 seconds (5 plus the ground's 4 second edge reply timeout), a LAND ALL tap with no ack expiring after 40 seconds, and a connected link with no `fleet_status` for 30 seconds marked stale. Ground text is cut to 300 characters and drone ids to 40 before display.

**Differences from the iOS Ground tab.** The Android Ground screen shows no video tiles and no match alerts yet, although the client receives `frame` events (it keeps the latest frame of at most 8 drones) and `match_alert` events (it does not keep a list).

## Networking

The ground link uses no Socket.IO library and no OkHttp:

- **`RawWebSocket`** is a small RFC 6455 client on a plain socket. It reads each frame header and refuses a message that would exceed the cap (8 MiB by default, below the dashboard's 10 MiB limit) before reading the payload, checks the running total across fragments, rejects masked server frames, reserved bits and invalid UTF-8, verifies `Sec-WebSocket-Accept`, offers no extensions, and sends no `Origin` or cookie. `MiniHttp` is the matching HTTP/1.1 code for the health check.
- **Why not OkHttp or a Socket.IO library.** The network security config blocks cleartext to a LAN address for every platform HTTP stack, and it cannot name an address range. The raw socket lets the app apply its own address rule to the resolved address before it connects. OkHttp also has no inbound size limit for WebSocket messages. The Engine.IO and Socket.IO subset the app needs is small, and `:contract` already has the frame codec. (OkHttp is still used for the one cloud request to Anthropic.)
- **`GroundConsoleClient`** speaks Engine.IO 4 and Socket.IO 5. It holds events that arrive before the connect acknowledgement (at most 32 events and 2 MiB, as the real server sends `status` first) and delivers them in order afterwards. It answers pings at once and drops the link after `pingInterval + pingTimeout` (capped at 120 seconds) without one. The handshake has a 10 second deadline, the REST probe reads under one overall deadline, and reconnects use a capped exponential backoff with jitter (1 to 30 seconds) that resets only after a stable session. A refused key stops the connection until the user retries. Video frames over 1 MiB are discarded. The key is redacted from every log line and error text.
- **LAN policy.** Only `http://` and `ws://` are supported, so a `wss://` or `https://` address is refused with an explanation. `LanAddressPolicy` allows cleartext only to 127/8, 10/8, 172.16/12, 192.168/16, 169.254/16, `::1`, `fc00::/7` and `fe80::/10`, judged on the resolved address bytes. A host name is refused when any of its answers is public (the iOS app is more lenient: it uses only the allowed answers). The connection goes to the checked address, never to the name. IPv6 zone ids (`%`) are refused, so a link-local IPv6 address cannot be entered; use the ground's IPv4 address. 100.64.0.0/10 is refused. The manifest's network security config keeps `cleartextTrafficPermitted="false"` except for `localhost` and `127.0.0.1` and trusts only system certificates. The link is not encrypted.
- **Permissions.** `INTERNET` is needed for the ground link, the edge link, the optional cloud request and the model download. The other permissions are `CAMERA`, `ACCESS_FINE_LOCATION`, `ACCESS_COARSE_LOCATION`, `ACCESS_WIFI_STATE`, `ACCESS_NETWORK_STATE`, `CHANGE_WIFI_STATE` and `VIBRATE`.

## Secrets

`SecretStore` keeps three secrets in `EncryptedSharedPreferences` (file `flightrisk_secure_prefs`, backed by the Android Keystore), under the names the contract shares with iOS: `FLIGHTRISK_DASHBOARD_API_KEY` (the ground's API key), `FLIGHTRISK_EDGE_TOKEN` (the edge link) and `FLIGHTRISK_CLAUDE_API_KEY` (the phone's own optional Anthropic key). Secrets are never put in plain preferences, files or logs. A key that cannot be read is reported as "Secure storage unavailable on this device", and the app fails safe. The secure file, the reasoning settings file, the ground link consent file and the general config file are excluded from Auto Backup and device transfer (`backup_rules.xml`, `data_extraction_rules.xml`).

## Cloud reasoning opt-in

Cloud reasoning is off by default. `CloudReasoningGate` yields the key, and so lets a request out, only while the opt-in ("Allow cloud reasoning", after a consent dialog) is on, Cloud Claude is selected, and a non-empty key can be read. The gate is checked at the start of every call and again immediately before the request. Opting out, switching provider or removing the key cancels a request in flight. Redirects are not followed and a failed request is not resent. What is sent: the cropped image of a detected person, the reference photo and the typed description, to `https://api.anthropic.com/v1/messages`. The opt-in and the provider are stored in `flightrisk_reasoning` preferences, which backup excludes. `CloudPathAuditTest` pins every source file that opens a network connection.

## Phone as an edge

- **Off by default.** It starts only when the user turns on "Send detections to the ground station" in Settings after reading a disclosure that names what is sent and the address, and says the link is not encrypted. The consent is tied to the address (changing the address turns the link off). The choice is stored in `flightrisk_ground_link` preferences, which backup excludes. A saved token alone starts nothing.
- **When it runs.** Only while the link is on, the app is in view and a search is running. The Search screen shows "Sending detections to ADDRESS", or "Sending detections and camera view to ADDRESS" while the ground has asked for video.
- **What is sent.** The legacy `detections` message built by `DetectionsWire`: box positions, confidence scores, and `reid_embedding` and `crop` for each detection. Crops are null by default (no pictures of people are made or sent), and no embedding is produced yet, so `reid_embedding` is null as well. There is no `drone_id` and no `face_embedding`. At most 50 detections go into a message and at most two messages a second. A message is at most 1 MiB; when it is larger, crops and then the thumbnail are dropped, and a frame that is still too large is dropped and counted.
- **Camera view.** A small thumbnail of the whole camera view, which can show people, is sent only after the ground sends `stream_video` with `enabled: true`, and stops when it sends `false`.
- **The ground cannot match these detections yet**, because the app sends no appearance embeddings. The disclosure says to use this mode for testing and mapping only.
- **Token.** The ground's edge port is 9000 (the dashboard is 5555; the app hints at this if 5555 is typed). A non-loopback ground requires the edge token, so without a saved token the status reads "Save the edge token from the ground station first." and nothing connects. The token is read from `SecretStore` when a connection is made, sent only in the first `auth` message, and never logged or shown.
- **What the phone ignores and never sends.** It writes only `auth` and `detections`. It ignores `hello`, `heartbeat`, `clock`, `ping`, `set_target`, `land`, `emergency_land_all` and every unknown message. Once the ground has sent heartbeats (every second), 3 seconds of silence end the session.
- **Close code 4001** means the token is wrong: the app shows "The ground rejected the edge token. Fix it in Settings." and does not retry. Other failures reconnect with bounded backoff.
- **Transport.** `EdgeSocketPort` uses `RawWebSocket` and `LanAddressPolicy` (public addresses are refused before any socket operation). `wss://` is refused.

## Build and test

Prerequisites: Android Studio Narwhal 3 Feature Drop (2025.1.3) or later (the build uses Android Gradle Plugin 8.13.2), JDK 17, Android SDK 34, and the SDK location. Gradle finds the SDK from `ANDROID_HOME` or from `sdk.dir` in `mobile/local.properties` (which is not committed). If the Gradle caches are already populated, add `--offline`.

```bash
cd mobile
export ANDROID_HOME=$HOME/Library/Android/sdk      # or write sdk.dir=... into local.properties
./gradlew --offline :contract:test :app:testDebugUnitTest :app:assembleDebug
```

The debug APK is written to `app/build/outputs/apk/debug/`. `./gradlew :contract:test` needs only a JDK. Two live tests in `:app` are skipped unless `FLIGHTRISK_LIVE_TEST_URL` names a throwaway dashboard on a loopback port; they refuse any non-loopback URL. Never point them at a real ground.

**The fixtures** are in `tests/fixtures/mobile_contract/`. `:contract` reads them through the `flightrisk.fixturesDir` system property set in `contract/build.gradle.kts`. Regenerate them from the repository root with `venv/bin/python scripts/export_mobile_contract_fixtures.py`; a Python test fails if they are out of date.

## What is not verified

- **No device or emulator has run the app.** The Compose screens were compiled and their state logic is tested on the JVM, but nobody has looked at the rendering, the layout, touch targets or the dimming of stale values.
- **Android Keystore and `EncryptedSharedPreferences` behaviour** on a real device (including a backup restored to another device) is simulated only with fake storage.
- **No real ground station, edge or drone** was contacted. Network tests use loopback sockets and a throwaway local dashboard.
- **The camera, JPEG compression of the thumbnail and battery and thermal cost** of the edge mode.
- **A real call to Anthropic** was never made; the request shape was checked against a stub.
- **Compose UI tests have never been run.** `app/src/androidTest/.../GroundConsoleScreenTest.kt` compiles in isolation but was not executed. **`app/src/androidTest/.../PipelineE2ETest.kt` does not compile on `main`** (`./gradlew :app:compileDebugAndroidTestKotlin` fails with unresolved references to `androidx.test.filters` and `SmallTest`/`MediumTest`, because the dependency is not declared). This is a known follow-up; the JVM tests and `assembleDebug` are not affected.
- **The on-device Gemma inference** path (MediaPipe, a new session per call) was compiled, not run.

## Key dependencies

| Library | Version | Purpose |
|---------|---------|---------|
| Jetpack Compose (BOM) | 2024.08.00 | UI framework |
| CameraX | 1.3.4 | Camera access and lifecycle |
| ONNX Runtime | 1.18.0 | On-device ML inference |
| TensorFlow Lite | 2.16.1 | OSNet ReID and InsightFace models |
| MediaPipe tasks-genai | 0.10.22 | On-device Gemma |
| Navigation Compose | 2.7.7 | Screen navigation |
| Play Services Location | 21.3.0 | GPS coordinates |
| Room | 2.6.1 | Session storage |
| security-crypto | 1.1.0-alpha06 | `EncryptedSharedPreferences` |
| OkHttp | 4.12.0 | The cloud request only |

## Configuration

`FlightRiskConfig` ports the Python configuration (`flightrisk/config.py`) with identical defaults. Overrides are read from SharedPreferences (keys prefixed `FLIGHTRISK_`). It reads no secret.

Three sensitivity presets control alert thresholds:

| Preset | Reid | Scorer | Face |
|--------|------|--------|------|
| MORE_ALERTS | 0.40 | 0.35 | 0.30 |
| BALANCED | 0.55 | 0.45 | 0.45 |
| FEWER_ALERTS | 0.70 | 0.60 | 0.55 |
