# Mobile follow-ups

Open items found while building and documenting the iOS and Android updates. Each one was checked against the code on `main` when this file was written. An item marked "reported, not reproduced" comes from a review round or the project owner and was not re-run here.

## Privacy and consent

- **iOS cloud opt-in is carried by backups.** `ReasoningSettings` stores the opt-in and the provider in `UserDefaults` (suite `flightrisk_config`), which device backups are expected to include (not verified on a device). The Claude key (`ThisDeviceOnly`) is not restored, so on a restored phone pasting a key could turn cloud reasoning on without the consent dialog. Android already excludes the equivalent file from backup. A fix would store the flag in a backup-excluded location or add a device check.
- **Android asks for camera and location permission before the privacy notice.** `MainActivity.onCreate` calls `requestPermissionsIfNeeded()` before `setContent`, so the system prompts appear before the first-launch notice, which contradicts `docs/first-launch-notice.md`. The fix is a small code change (request permissions after the notice).
- **The Android notice is incomplete and weaker than the iOS one.** It has no ground station item, no mention of the phone-as-edge mode or the direct Tello link, and no privacy policy link. The acknowledgement key `flightrisk_privacy_notice_acknowledged` is written but never read, so a person who quits after tapping "I Understand" sees the notice again.
- **iOS `migrateLegacyAcknowledgement` runs only on the onboarding path** (reading either key is correct, so this is minor).
- **Android Auto Backup includes the local database.** The rules exclude four preference files only. The Room database (match thumbnails, GPS fixes, scores, timestamps) can be copied to the user's Google account if backup is on. The privacy policy says so; the owner should decide whether to exclude the database.
- **Unused persistence code that would break the "not stored" claims if wired in.** `ios/FlightRisk/Vision/TargetStore.swift` and, on Android, `canon/TargetCanon.kt` plus the `target_versions` table in `FlightRiskDatabase` can write a reference photo and embeddings to disk, but nothing calls them. (The matching Android `vision/TargetStore.kt` was unreferenced and was removed in this change.) The iOS file is referenced only by the Xcode project file. The privacy policy states the reference photo and embeddings are not stored beyond the session; that is true only while this code stays unused.
- **`CHANGE_WIFI_STATE` is declared in the Android manifest but no code that changes Wi-Fi state was found.** Remove it if it is not needed, because Play review reads the permission list.
- **Store answers are provisional.** The Google Play Data Safety answers and the face detection declaration in `docs/app-store-compliance.md`, and the App Privacy answers for iOS, are the owner's decision and have not had a legal review.

## Wording and behaviour parity

- **"was NOT changed" after a lost link.** After a detection profile selection is sent and the link then drops, both apps say the profile "was NOT changed", although the ground may have received the command. The text should say the result is unknown.
- **`match_alert_resolved` is ignored.** The ground sends it when the operator answers an alert (`flightrisk/dashboard/recovery.py`). Neither phone handles it, so an iOS reminder row never ends. It needs a fixture, a decoder on both platforms and a row state.
- **The Android Ground screen shows no video tiles and no match alerts.** The client already receives `frame` and `match_alert` events; the screen does not draw them.
- **LAN policy parity.** iOS connects to the allowed answers of a host name and refuses it only when none is allowed. Android refuses a name when any answer is public. Android also refuses IPv6 zone ids and does not parse numeric spellings itself. One policy should be chosen and both apps should follow it.
- **Score rounding.** Python rounds the combined match score to three decimals (`round`); the Swift and Kotlin scorers truncate (`Int(x * 1000) / 1000`). Scores can differ by 0.001 between the ground and a phone for the same inputs.

## Ground link features

- **`wss://` for the Android ground link.** Android supports only plain `http://` and `ws://`, and refuses `wss://` and `https://`. The iOS app accepts both.
- **Embeddings for phone-as-edge.** The Android matcher does not expose per-detection appearance embeddings, so `reid_embedding` is null and the ground cannot match phone detections to a search target. The mode is for testing and mapping only until this is done. Crops are also null by default.

## Tests and flakes

- **`PipelineE2ETest.kt` does not compile on `main`.** `./gradlew :app:compileDebugAndroidTestKotlin` fails with unresolved references to `androidx.test.filters` (`SmallTest`, `MediumTest`) because the dependency is not declared. `GroundConsoleScreenTest.kt` compiles but has never been run.
- **Compose UI tests have never been run** (Android `GroundConsoleScreenTest`), and no iOS UI tests exist.
- **`CloudClaudeGateTests.testRequestInFlightIsCancelledOnOptOut` timing flake** (iOS). Reported, not reproduced here. The test asserts `StubURLProtocol.stopCount` immediately after the cancelled call returns, which depends on when URLSession calls `stopLoading`.
- **Android `RawWebSocketTest` 40 MB message test** failed once in a full run during the cloud opt-in review ("wrote 1769472 bytes, bound 1438256") and passed in later runs. Reported, not reproduced here.
- **Launcher process-restart tests** (Python) are timing flakes that were reported in the review rounds (a Ctrl+C teardown test and three launcher process tests); they pass alone. Reported, not reproduced here.
- **Simulator crash.** The default `iPhone 17` simulator crashed on launch repeatedly in one iOS review round; using the other simulator by id worked.

## Real-device verification list

None of these has been done:

- iOS: a physical iPhone; App Transport Security and Network.framework behaviour for the ground link (host-literal connections with a `Host` header, scoped link-local IPv6, TLS server name for `wss://`, whether `NSAllowsLocalNetworking` covers bare IP addresses); the local network permission prompt; mDNS (`.local`) names; real proxy and VPN settings; the Ground tab layout and Dynamic Type; the on-device Gemma download and inference; a real Anthropic request.
- Android: a physical device or emulator; Compose rendering and the dimming of stale values; Android Keystore and `EncryptedSharedPreferences` behaviour (including a restored backup); cleartext behaviour under the network security config; the camera path and the thumbnail JPEG compression; battery and thermal cost of the edge mode; MediaPipe inference; a real Anthropic request.
- Both: a real ground station and a real fleet. The phone-as-edge messages were checked against the contract fixtures, not a live ground.

## Small cleanups found

- `ios/FlightRisk/Config/KeychainHelper.swift` still mentions `cloud_gemini` and `cloud_openai` in a doc comment (the providers were removed).
- The Android app still carries the direct Tello flight stack; the iOS app does not. Decide whether that is intended.
- The `AmberConfig` comments in `FlightRiskConfig.swift` and `FlightRiskConfig.kt` belong to the rename in PR #155 and were not touched here. This change rewrote `mobile/README.md` without naming the Python configuration class, so PR #155 will conflict on that file; keep its renamed words (`FlightRiskConfig`) if the sentence is still there when it merges.
