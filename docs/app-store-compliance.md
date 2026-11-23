# FlightRisk Mobile -- App Store Compliance Checklist

The Apple sections describe the iOS app and the Google Play sections describe the Android app, each as the code on `main` does it. Answers to the store questionnaires are the owner's decision. Statements the author could not verify are marked provisional or unsure, and `docs/mobile-followups.md` lists them.

---

## Apple App Store

### Permission Usage Descriptions (Info.plist)

These are the strings in `ios/FlightRisk/Info.plist` and the `info.properties` block of `ios/project.yml`. A unit test checks that the two agree.

| Key | Value |
|-----|-------|
| `NSCameraUsageDescription` | "FlightRisk uses your camera to scan for people matching the target photo." |
| `NSLocationWhenInUseUsageDescription` | "FlightRisk attaches GPS coordinates to match alerts so you can navigate to the location." |
| `NSPhotoLibraryUsageDescription` | "FlightRisk needs access to your photo library to select a target reference photo." |
| `NSLocalNetworkUsageDescription` | "FlightRisk connects to the ground station address you enter in Settings, over your local network, to show its status and send commands." |

The app has no direct link to a drone. The earlier direct Tello link was removed, and the local network string no longer names it. The app does not browse for Bonjour services, so `NSBonjourServices` is not declared.

### App Transport Security

`Info.plist` sets `NSAppTransportSecurity` with `NSAllowsLocalNetworking` = true and nothing else. There is no `NSAllowsArbitraryLoads`, no `NSAllowsArbitraryLoadsInWebContent` and no exception domain. A test checks this.

- Why it is needed: the ground station is a computer on the user's local network that serves plain `ws://` and `http://`. The app talks to the address typed in Settings over a WebSocket (`NWConnection`) and over HTTP (`URLSession`, for the "Test connection" health check). Apple documents `NSAllowsLocalNetworking` as allowing loads of local resources, such as unqualified and `.local` host names, without disabling App Transport Security for the rest. Whether it also covers plain IP addresses has not been verified here.
- What it does not do: App Transport Security applies to `URLSession`, not to `NWConnection`, so this setting does not restrict the WebSocket link. The restriction comes from the app's own address policy (`LanAddressPolicy`, `ios/FlightRiskGround/Sources/SocketIOMini`): a `ws://` or `http://` address is used only if it resolves to a local network address, and the check runs before the key or any request is sent. That policy was checked with unit tests and an injected resolver only; its behaviour with Network.framework on a device has not been verified.
- What the user should know: a `ws://` or `http://` link is not encrypted, and the API key for the ground station travels in the clear on that link (in the `Authorization` header of the health check, and as the `api_key` field of the Socket.IO connect packet). Use a network you trust, such as a private WiFi network or the fleet access point, and not a public or shared one. Away from the local network, use an encrypted address (`wss://` or `https://`); a VPN also works if it gives the ground station a private-range address (100.64.0.0/10, used by Tailscale, is refused for cleartext). If a user enters a `ws://` or `http://` address that is not local, the app refuses it and sends nothing. The app also accepts `wss://` and `https://` addresses; those are encrypted but need a certificate the phone trusts.

### Network destinations in the iOS app

Found by searching `ios/FlightRisk` and `ios/FlightRiskGround/Sources` for `URLSession`, `NWConnection`, WebSocket and hub use:

1. The ground station address typed in Settings: WebSocket via `NWConnection` (`GroundTransport.swift`) and the health check via `URLSession` (`URLSessionGroundHTTPClient`).
2. `https://api.anthropic.com/v1/messages` via `URLSession` (`CloudClaudeLlmBackend.swift`), only after the cloud opt-in.
3. Hugging Face (`huggingface.co`), through `defaultHubApi` in `GemmaModelManager.download()`, only when the user taps the model download button in Settings. It downloads `mlx-community/gemma-2-2b-it-4bit`. Hugging Face sees the phone's IP address and the requested file names; no user data is sent. Its handling of that is outside this project and was not reviewed.

### Privacy Nutrition Labels (App Privacy)

**Provisional.** The table below is a draft and the owner decides the final answers. Apple counts data sent to a server that the developer does not run (a third party) as collected unless the narrow exceptions apply, so the cloud path and the ground link probably change some rows. What is known: with cloud reasoning on, the reference photo and person crops (which can show a face) and the typed description go to Anthropic; the camera and location data are otherwise processed and stored on the device; the Hugging Face download exposes the IP address to Hugging Face. Rows marked "Provisional" depend on that decision.

| Data Type | Collected | Linked to User | Used for Tracking | Purpose |
|-----------|-----------|----------------|-------------------|---------|
| Camera | Yes | No | No | App Functionality |
| Precise Location | Yes | No | No | App Functionality |
| Photos or Videos | Provisional: yes when cloud reasoning is on (reference photo and crops go to Anthropic); otherwise no | -- | -- | App Functionality |
| Identifiers | No | -- | -- | -- |
| Usage Data | No | -- | -- | -- |
| Diagnostics | No | -- | -- | -- |
| Contact Info | No | -- | -- | -- |

**Summary for App Store Connect:**
- The app has no server of the developer's own and sends the developer nothing. Data leaves the device in three cases, each started by the user: commands to the ground station address typed in Settings, images and text to Anthropic after the cloud opt-in, and a model download from Hugging Face that reveals the IP address. These are not decided here; the owner answers the questionnaire. The earlier line "Data Not Collected" is withdrawn because it conflicts with the Camera and Location rows and with Apple's definition.
- Data Not Linked to User: No user accounts, no identifiers collected.
- Data Not Used for Tracking: No advertising or tracking SDKs.

### App Review Guidelines Compliance

#### Guideline 5.1.1 -- Data Collection and Storage

- **Compliance:** The app collects camera frames and location data solely for core functionality (locating a missing child). No user data is transmitted off-device except through two user-started paths, and a third user-started network use exposes the IP address. First, the ground station link: the app connects to the address the user enters in Settings and sends land, resume-search and detection-profile commands and status requests, never camera frames, crops or the reference photo. The app refuses a ws:// or http:// address unless it resolves to a local network address; an encrypted wss:// or https:// address is allowed anywhere, and the app does not pin certificates. Third, the on-device model download from Hugging Face, which sends no user data (the host sees the IP address). Second, the optional cloud reasoning feature, which is off by default and sends the cropped person images, the reference photo and the typed description to Anthropic only after the opt-in toggle is on, a Claude API key is saved and Cloud Claude is selected. No user accounts are created. No personal information is collected, stored off-device, or shared.
- **Documentation to provide:** Link to privacy policy. Description of the on-device processing architecture and of the three user-started network paths above. Explanation of ephemeral biometric data handling.

#### Guideline 5.1.2 -- Data Use and Sharing

- **Compliance:** No user data is shared with third parties other than Anthropic after the cloud opt-in; Hugging Face sees the IP address during a user-started model download. No analytics, advertising, or tracking SDKs are included. The optional cloud reasoning feature transmits to Anthropic only, with explicit opt-in and a consent dialog. No other cloud provider is supported on iOS.
- **Documentation to provide:** Privacy policy URL. In-app disclosure of cloud reasoning data transmission (the consent dialog in Settings). First-launch privacy notice (see `first-launch-notice.md`).

#### Guideline 5.1.3 -- Health and Health Research (Not Applicable)

- The app does not use HealthKit or health-related APIs.

#### Guideline 2.5.14 -- Face ID / Biometric Authentication (Not Applicable)

- The app does not use Face ID or Touch ID for authentication. It uses its own on-device face recognition pipeline for person matching, which does not invoke system biometric APIs.

### Age Rating

- **Rating:** 17+
- **Rationale:** The app is used in crisis/safety contexts involving missing children. The subject matter and use case are appropriate for adult users only.

### Privacy Policy URL

- **Required:** Yes. Must be provided in App Store Connect under "App Information > Privacy Policy URL."
- **URL:** To be set at deployment (host `docs/privacy-policy.md` as a web page).

### App Review Notes (Recommended)

Include the following in the "Notes" field during submission:

> FlightRisk is a tool for parents and guardians to locate a missing child. Person detection and appearance matching run on the device using CoreML. The app has no accounts and no analytics.
>
> Local network permission: the Ground tab can connect to a "ground station", a computer the user runs on their own network. The user types its address in Settings (for example ws://192.168.1.20:5555) and a key, which is stored in the Keychain. The app connects to that address and to no other ground station. To test this without a ground station, leave the address empty; the app then makes no local network connection and the permission prompt does not appear.
>
> App Transport Security: Info.plist sets NSAllowsLocalNetworking only, so that the plain-HTTP health check to a ground station on the local network works. There is no NSAllowsArbitraryLoads. The WebSocket link uses Network.framework, to which App Transport Security does not apply. The app itself refuses cleartext (ws:// and http://) connections unless the resolved address is a local network address, checked before the key or any request is sent. The app does not pin certificates for wss:// and https:// connections.
>
> The Settings screen can download an on-device language model from Hugging Face when the user taps the download button; nothing is downloaded otherwise.
>
> Cloud reasoning is off by default. It sends the cropped image of a detected person, the reference photo and the typed description to Anthropic only after the user turns on "Allow cloud reasoning" and confirms the consent dialog, saves a Claude API key and selects Cloud Claude. Reviewers can leave it off; all other features work without it.
>
> The first-launch privacy notice is shown before the camera starts.

---

## Google Play Store

The Android answers below describe what the code does. **The Data Safety answers are the owner's decision.** Every answer marked provisional depends on how Google's definitions are read, and nothing here has been reviewed by a lawyer. Nothing in this section was checked on a device.

### Build requirements

| Requirement | State in the code |
|---|---|
| Target API level 36 (Android 16), required for new apps and updates from 2026-08-31 | `compileSdk = 36`, `targetSdk = 36` in `mobile/app/build.gradle.kts` |
| 16 KB memory pages (64-bit native libraries) | Every `arm64-v8a` and `x86_64` library is 16 KB aligned and stored on 16 KB boundaries: CameraX 1.4.2, ONNX Runtime 1.23.2, LiteRT 1.4.2 (which replaced TensorFlow Lite 2.16.1) and MediaPipe tasks-genai 0.10.22. Check a build with `mobile/scripts/check-16kb-alignment.sh`. |
| Android App Bundle, signed with the upload key, Play App Signing | `./gradlew bundleRelease`. Signing is read from `mobile/keystore.properties` or environment variables; without them the bundle is unsigned and Gradle warns. See `docs/android-release-signing.md`. |
| Release build with R8 | `isMinifyEnabled = true`; keep rules for ONNX Runtime, LiteRT, MediaPipe and protobuf are in `mobile/app/proguard-rules.pro`. |
| Launcher and store icon | Adaptive icon with a monochrome layer, legacy mipmaps, and `branding/play_store_icon_512.png`, all generated from `branding/flightRiskIcon.png` by `branding/generate_android_icons.py`. |
| Edge-to-edge (enforced from target 35) | `MainActivity` calls `enableEdgeToEdge()`. The navigation shell pads for the system bars and display cutouts and consumes them; the privacy notice and onboarding, which are shown before it, pad themselves. |

Other Android 15 and 16 behaviour changes were reviewed against the code. The app has no foreground service, no `PendingIntent`, no broadcast receiver, no `onBackPressed` override, no orientation or resizability lock and no explicit intent to another app. Predictive back works through the AndroidX back dispatcher; onboarding uses `BackHandler` only from its second step, so the back-to-home animation still plays on the first. The intents it sends (dial, map, camera capture, photo picker, privacy policy link) all carry an action, and each launch is guarded so that a device without a matching app does not crash, and the camera, photo picker and privacy policy link tell the person (a short message) when nothing can handle them. Onboarding's "Call 911" button shows "This device can't place calls. Call 911 from a phone now." when the device has no telephony or no dialer. Android 16's local network protection is only an opt-in test for target 36; when a later target enforces it, the Tello and ground station links will need the new permission.

### Permission Rationale

Declared in `mobile/app/src/main/AndroidManifest.xml`.

| Permission | Rationale |
|-----------|-----------|
| `android.permission.CAMERA` | Real-time person detection and face matching to locate a missing child, and taking the reference photo. Detection runs on the device. Not requested on a device without a camera. |
| `android.permission.ACCESS_FINE_LOCATION` and `ACCESS_COARSE_LOCATION` | Reads the location (through Google Play services, `FusedLocationProviderClient`) when a match is detected so the user can navigate back to that location. Kept in memory only; the database has no location fields. |
| `android.permission.INTERNET` | Needed for the paths the user starts: the connection to the ground station address the user enters (dashboard link on port 5555 by default, and the optional phone-as-edge link on port 9000 by default), the optional cloud reasoning request to Anthropic after the opt-in, and the optional on-device model download. |
| `android.permission.ACCESS_NETWORK_STATE` | Used to check that a network is available before a cloud reasoning request (`NET_CAPABILITY_VALIDATED`) and to check that the phone is on Wi-Fi when it looks for a Tello drone. |
| `android.permission.ACCESS_WIFI_STATE` | Used to read the current Wi-Fi connection when checking whether the phone is joined to a Tello drone's own Wi-Fi network. The app never changes Wi-Fi state (`CHANGE_WIFI_STATE` is not declared). |
| `android.permission.VIBRATE` | Vibrates the phone on a match alert. |

No hardware feature is required (`uses-feature ... android:required="false"` for the camera, camera autofocus, Wi-Fi and location), so Google Play does not hide the app from devices without them. Without a camera, the Search screen says so and a Tello drone can be used instead, and the target photo is picked from the gallery. Without Wi-Fi, a Tello cannot be reached. Without location hardware, match alerts have no coordinates.

### Network destinations in the Android app

Found by searching `mobile/app/src/main/java` for connection-opening code. A unit test (`CloudPathAuditTest`) fails if a new file opens a connection or names a cloud host.

1. The ground station address typed in the Ground screen: a plain socket WebSocket (`RawWebSocket`) and a plain HTTP health check (`MiniHttp`), or TLS for `https://` and `wss://` addresses. Commands, status requests and the dashboard key; no images. A plain link is not encrypted (see "Cleartext policy" below).
2. The ground station's edge port, only when the user turns on "Send detections to the ground station" in Settings (phone-as-edge): box positions and scores, and a small picture of the whole camera view only when the ground asks for video. Not encrypted. See below.
3. `https://api.anthropic.com/v1/messages` through OkHttp, only after the cloud opt-in. The model is `claude-sonnet-5-5` (`CloudClaudeLlmBackend.DEFAULT_MODEL`).
4. `https://storage.googleapis.com/mediapipe-models/...` through `HttpURLConnection`, only when the user taps "Download Model" for the on-device Gemma model. It is a plain GET for a file; no photo, crop, description or location is sent. Google sees the phone's IP address and the requested file. Its handling is outside this project and was not reviewed. (The iOS app downloads from Hugging Face instead.)
5. The Tello drone on its own Wi-Fi network (UDP), when the user flies a Tello from the Android app. This is a local link to the drone; video is received, not sent.

The privacy policy link in the privacy notice is opened in the browser; the app itself opens no connection to it.

### Cleartext policy

The Android network security config (`res/xml/network_security_config.xml`) blocks cleartext for every platform HTTP stack except `localhost` and `127.0.0.1`, and trusts only system certificate authorities. The ground clients use plain sockets, which that file does not govern, and enforce their own rule in code (`LanAddressPolicy`): cleartext only to private, link-local and loopback addresses (10/8, 172.16/12, 192.168/16, 169.254/16, 127/8, `::1`, `fc00::/7`, `fe80::/10`), judged on the resolved address, before a connection is made or the key is sent. A host name that resolves to any public address is refused. A plain ground link is not encrypted, so the dashboard key and the edge token travel in the clear on the user's network, which is why cleartext is limited to those addresses. For the Ground screen the Android app also accepts `https://` and `wss://` addresses to any host: those connections use the platform TLS socket factory with the system certificate store and a host name check (TLS 1.2 and 1.3), with no option to trust an unknown certificate or skip the check, so the dashboard key goes only over TLS or over an allowed private-network cleartext link. The edge link (phone-as-edge) is still plain `ws://`. Video frames and match alerts the ground sends are shown only while the user is connected and are not stored.

### Data Safety Section

**Provisional.** Facts first, from the code (`docs/android-privacy-changes.md` has the details):

- With the default settings, no photo, crop, face data or location leaves the phone. Camera frames are not uploaded.
- With cloud reasoning on (opt-in, a saved Claude key and Cloud Claude selected), the cropped image of each detected person, the reference photo and the typed description are sent to Anthropic over TLS. The images can show a face.
- With the ground station link on, commands and status requests go to the address the user entered. With the phone-as-edge link on, box positions and scores go to that address, and a small picture of the whole camera view when the ground asks for video. The user's own ground station is not a third party the developer runs, but Google's rules on "shared" and "collected" data may still treat these as data transmitted off the device. The owner decides.
- **Search video is not recorded by default.** Settings has "Record search video" (off by default). Only while it is on, each search is saved as an MP4 of the camera view with the detection boxes (`files/recordings`), and match pictures are saved as JPEG files (`files/snapshots`).
- Every search saves a row in the Room database: start and end time, frame, detection and match counts, the typed description, and for each match its scores, type, time and the reasoning text. There are no location fields. The reference photo is kept in memory only; a reference photo taken with the camera is deleted from the cache as soon as it is decoded.
- Backup and device transfer exclude the database, `files/recordings`, `files/snapshots`, the secret store, the consents and the settings files (`backup_rules.xml`, `data_extraction_rules.xml`). Nothing the app saves about a search is copied off the phone by Android backup.
- GPS coordinates are kept in memory only. Location comes through Google Play services (`FusedLocationProviderClient`), which is a Google component on the phone; the owner should consider whether to name it on the form.

**Delete controls that exist** (all in the app, no account needed):

| Control | Where | What it deletes |
|---|---|---|
| Delete saved sessions and recordings | Settings > Saved data, with a confirmation; disabled while a search runs | Every row of the sessions, matches and match feedback tables, and every file in `files/recordings` and `files/snapshots`. It reports how many records and files it deleted. |
| Remove saved key | Settings > AI Reasoning | The Claude API key in the encrypted secret store. Turning cloud reasoning off, or removing the key, also cancels any request in flight. |
| Remove saved token | Settings > Ground station link | The edge token. |
| Remove saved key | Ground tab > connection card | The dashboard key. |
| Delete Model | Settings > AI Reasoning (on-device model) | The downloaded Gemma model file. |
| Clear storage or uninstall | Android system settings | Everything the app stored. |

The reference photo has no delete control: a photo taken with the camera is written to a temporary cache file that is deleted as soon as it has been decoded (`TargetPickerScreen`), a photo from the picker is never copied, and the decoded bitmap is held in memory only, so it is gone when the app process ends.

#### Data collected or shared (provisional draft)

| Question | Draft answer |
|----------|--------------|
| Does your app collect or share any of the required user data types? | Provisional. The answer "No" does not hold when cloud reasoning is on, because images are sent to Anthropic. Images also leave the phone through phone-as-edge (a picture of the whole camera view, sent to the user's own ground station when it asks for video). The owner decides how to answer, including whether the optional cloud feature and the user's own ground station count. |
| Is all of the user data collected by your app encrypted in transit? | Provisional. Cloud reasoning: yes (TLS). The ground station link: TLS for `https://` and `wss://` addresses, otherwise plain `http://` and `ws://` (only on a private network, by the app's own rule). The edge link: no, it is plain `ws://`. |
| Do you provide a way for users to request that their data is deleted? | Provisional: yes, in the app. The developer holds no user data. On the phone, "Delete saved sessions and recordings" deletes the saved sessions, matches, feedback, videos and match pictures, and the remove-key controls delete the saved keys (table above). Data sent to Anthropic under cloud reasoning is handled under Anthropic's policy, not by the app. |

#### Data types (provisional draft)

| Data Type | Collected | Shared | Ephemeral | Purpose |
|-----------|-----------|--------|-----------|---------|
| Location (approximate) | No (the coarse permission is requested together with the fine one) | No | -- | -- |
| Location (precise) | No. It is read only on the device and kept in memory; Google's definition of "collected" is data transmitted off the device, so this is "not collected" | No | -- | -- |
| Photos/Videos | Provisional: with "Record search video" on (off by default), search video and match pictures are saved in private storage, not sent by the app and not backed up; with cloud reasoning on, the reference photo and crops go to Anthropic; with phone-as-edge on, a picture of the whole camera view goes to the user's ground station when it asks | Provisional | Provisional: sent images are not kept by the app; saved recordings stay until deleted | App functionality |
| Personal info (all types, including name, email and user IDs) | No. Google Play has no "face" or "biometric" data type. Face embeddings and templates stay on the device in memory, so they are not collected; face images that go to Anthropic are declared under Photos/Videos above | No | -- | -- |
| Device or other IDs | No | No | -- | -- |
| App activity > Other user-generated content | Provisional: with cloud reasoning on, the typed description is sent to Anthropic (the same data the iOS privacy manifest declares as Other User Content) | Provisional | Provisional: not kept by the app | App functionality |
| App activity | No | No | -- | -- |
| App diagnostics | No | No | -- | -- |

**Summary for the Data Safety form:**
- The developer runs no server and receives no data from the app.
- Data leaves the phone only through paths the user starts: the ground station links the user configures, cloud reasoning after the opt-in, and the on-device model download.
- No analytics, advertising or tracking SDKs are included.
- Camera and location are used for core functionality only.

### Face matching description

- **No separate Play declaration.** The Play Console has no "face detection declaration" form, so none is claimed here. The Data Safety data types also have no face or biometric type. The wording below is for the Data Safety form, the store listing and the review notes, and the owner should confirm it against the current Play policy text.
- **Suggested text (provisional):** "This app uses on-device face detection and recognition to match detected persons against a reference photo provided by the user. Face embeddings are computed on the device, kept only in memory for the active session, and never transmitted. If the user turns on the optional cloud reasoning feature, images of detected persons and the reference photo, which can show a face, are sent to Anthropic for analysis. That feature is off by default. On Android, search video is saved in the app's private storage only if the user turns on Record search video, which is off by default."

### Age Rating (IARC)

- **Rating:** 17+ or equivalent (PEGI 18 / USK 18 / GRAC 18 depending on region)
- **Rationale:** Crisis/safety context involving missing children. Intended for adult users only.
- **Content descriptors:** None applicable (no violence, gambling, etc.). Rating is based on sensitive subject matter.

### Privacy Policy URL

- **Required:** Yes. Must be provided in Google Play Console under "App content > Privacy policy" and is linked from the privacy notice in the app.
- **In the app:** the notice reads the address from one constant, `PrivacyNoticeText.POLICY_URL`, which is `BuildConfig.PRIVACY_POLICY_URL`. The build sets it from the Gradle property `flightrisk.privacyPolicyUrl`, for example `./gradlew bundleRelease -Pflightrisk.privacyPolicyUrl=https://example.org/flightrisk/privacy`. The address must start with `https://`.
- **Before submission:** the default is the policy file on GitHub (`https://github.com/KurtLehnardt/flightRisk/blob/main/docs/privacy-policy.md`). Google expects a public, active web page that is not a PDF or a source file, so host `docs/privacy-policy.md` as a web page, then build the bundle with that address and enter the same address in Play Console.

### Store Listing Notes (Recommended)

Include in the app description or "About this app" section, after the owner and a lawyer have reviewed it:

> Person detection and matching run on your device, and face templates are never sent anywhere. Cloud reasoning is off by default; if you turn it on, the cropped images of detected people, your reference photo and your description are sent to Anthropic. Search video is not recorded unless you turn on Record search video, and you can delete saved sessions and recordings in Settings. The optional ground station link sends commands to a computer you run, on your own network, and the optional phone-as-edge mode can send detections and, when asked, a picture of your camera view to that computer. See our privacy policy for full details.

The earlier recommended text ("No biometric data is collected, transmitted, or stored") is withdrawn because it conflicts with the cloud path.

### Store checklist (Android): what still needs the owner

The Android app is not ready to submit. Two functional gaps remain in code, and further steps cannot be done in code:

- [ ] **Models are not bundled, so detection does nothing.** The person detector, ReID and face models are not in the app's assets, so a search finds no one until they are added.
- [ ] **Phone-camera analysis is not wired.** The CameraX `ImageAnalysis` use case has no caller, so the phone camera shows a preview but no frames reach the pipeline. It must be wired together with the model integration PR.

These steps cannot be done in code:

- [ ] **Upload key.** Create it and keep it safe, then sign the bundle with it (`docs/android-release-signing.md`). Never commit the keystore or its password.
- [ ] **Play Console app and Play App Signing.** Create the app, choose a Google-generated app signing key, and upload the first signed bundle to an internal testing track.
- [ ] **Hosted privacy policy.** Host `docs/privacy-policy.md` as a public web page, build with `-Pflightrisk.privacyPolicyUrl=<that address>`, and enter the same address in Play Console.
- [ ] **Data Safety form.** Decide the provisional answers above, ideally with a lawyer.
- [ ] **Content rating** questionnaire (17+ or equivalent). Google Play has no separate face detection declaration.
- [ ] **Target audience and content**: adults only; not designed for children.
- [ ] **Store listing**: title, short and full description (see "Store Listing Notes"), the 512 x 512 icon (`branding/play_store_icon_512.png`), a 1024 x 500 feature graphic, and at least two phone screenshots. None of the last two exist yet.
- [ ] **Test on real devices.** No device or emulator has run the app. At least: the privacy notice and onboarding (including "Set up a search later"), the Search screen with and without a target photo, edge-to-edge layout on Android 15 and 16 (status bar, navigation bar, a display cutout in landscape), predictive back, the camera preview, a device or emulator without a camera, and a 16 KB page size emulator image.
- [ ] **A real cloud reasoning call** with a Claude key, to confirm `claude-sonnet-5-5` answers in the MATCH / CONFIDENCE / REASONING format at high effort, and to measure how long a call takes against the 120 s read timeout (only a stub server was used).
- [ ] **Versioning.** Raise `versionCode` for every upload.
- [ ] **Model download host.** Decide whether the on-device model download (Google storage) is named in the privacy policy and the Data Safety form.

---

## Both Stores -- Common Requirements

### Privacy Policy URL

- [ ] Privacy policy hosted at a publicly accessible URL
- [ ] URL provided in both App Store Connect and Google Play Console
- [ ] Policy covers all data types processed by the app
- [ ] Policy is written in clear, understandable language

### Cloud LLM Opt-In (Desktop Dashboard)

The desktop dashboard (`python -m flightrisk --dashboard`) follows the same rule as the mobile apps: cloud LLM reasoning is off by default and requires explicit opt-in.

- The default reasoning backend is Gemma running locally in Ollama. With the default settings no image leaves the machine.
- The Claude backend sends the detected person's crop and the missing child's reference photo to Anthropic. It is refused unless the operator sets `FLIGHTRISK_ALLOW_CLOUD_LLM=1` and provides `FLIGHTRISK_CLAUDE_API_KEY`.
- A stray `ANTHROPIC_API_KEY` in the environment is ignored, so a key exported for other tools cannot enable uploads.
- The dashboard's AI Reasoning selector shows Claude as unavailable, with the reason, until cloud use is allowed. The runtime switch cannot opt itself in.
- When Claude is active, the startup log states that cloud reasoning is on and names the variable that supplied the key (never the key itself).

### Cloud Reasoning Opt-In (iOS)

Claude (Anthropic) is the only cloud provider on iOS. OpenAI and Gemini are not supported. Cloud reasoning is off by default, and a saved key alone never turns it on.

- Settings: the provider choice is `flightrisk_reasoning_provider` (`local_gemma`, `cloud_claude` or `off`) in the UserDefaults suite `flightrisk_config`. The cloud opt-in is not in UserDefaults: it is the file `Application Support/FlightRisk/cloud_consent`, which is marked excluded from backup and does not exist until the user confirms the consent dialog (default off). An earlier build kept the opt-in in UserDefaults as `flightrisk_allow_cloud_llm`; the first launch of the current build moves it (see `docs/ios-ground-client.md`). A stored cloud choice without the opt-in is ignored and removed.
- Cloud Claude is used only when the opt-in toggle is on, a Claude API key is saved and Cloud Claude is selected. Turning the toggle on shows a consent dialog; turning it off stops sending at once and cancels a request in progress.
- Consent dialog text (`SettingsView.cloudConsentMessage`): "With cloud reasoning on, the cropped image of each detected person, the reference photo you chose and the text description you entered are sent to Anthropic (Claude) for analysis. The images can show a face, and Anthropic's privacy policy applies to them. Nothing is sent until you also save a Claude API key and select Cloud Claude. You can turn this off at any time here; turning it off stops sending at once and cancels any request still in progress."
- The Claude API key and the ground station key are stored in the iOS Keychain (`kSecAttrAccessibleWhenUnlockedThisDeviceOnly`), never in UserDefaults, files or logs.
- The ground station's own reasoning provider is display only on the phone. The phone cannot switch it and cannot turn on cloud use on the ground.

### Cloud Reasoning Opt-In (Android)

Claude (Anthropic) is the only cloud provider on Android. Cloud reasoning is off by default, and a saved key alone never turns it on.

- Settings keys (SharedPreferences file `flightrisk_reasoning`): `flightrisk_reasoning_provider` (`local`, `cloud_claude` or `none`) and `flightrisk_allow_cloud_llm` (default false). Only a stored Boolean `true` counts as opted in. A stored cloud choice without the opt-in is ignored and removed. The default is the on-device model when it is available and none otherwise.
- Cloud Claude is used only when the opt-in switch is on, a Claude API key is saved and Cloud Claude is selected (`CloudReasoningGate`, checked at the start of every call and again immediately before the request). Turning the switch on, or picking Cloud Claude before opting in, shows the consent dialog. Turning it off, switching provider or removing the key cancels a request in flight.
- Consent dialog text (`ReasoningSettings.kt`, `MESSAGE`): the same words as on iOS.
- The Claude API key and the ground station keys are stored in `EncryptedSharedPreferences` (backed by the Android Keystore), never in plain preferences, files or logs. The opt-in file and the secure file are excluded from Auto Backup and device transfer, so the opt-in is not restored to another device.
- A "Cloud active: images are sent to Anthropic" indicator shows in Settings and on the Search screen while cloud is the active backend.
- The first-launch privacy notice (`PrivacyNoticeScreen`, text in `PrivacyNoticeText.kt`) is shown before any permission prompt. It describes the camera, on-device processing, the ground station, phone-as-edge, the Tello drone, cloud reasoning and location, says that video is not recorded unless the user turns it on, and links to the privacy policy.
- The request names `claude-sonnet-5-5` (one constant, `CloudClaudeLlmBackend.DEFAULT_MODEL`), sets high effort in `output_config` with `max_tokens` 8192, and sends no sampling parameters and no assistant prefill. The request is not streamed, so the read timeout is 120 s and the whole call is capped at 150 s; it runs on the existing background worker, one item at a time. It also sends the `server-side-fallback-2026-07-01` beta header and `"fallbacks": "default"`, as the desktop agent does for this model, so a safety-classifier decline is re-run on a fallback model on Anthropic's side. Images are scaled to 768 px on the long edge before they are sent, and the reference photo is encoded once per search. A 401 or 403 marks the saved key as rejected (no more requests until a new key is saved) and shows a notice; other 4xx codes are shown in the notice; three failures in a row pause cloud reasoning for 60 s. A refusal (`stop_reason: "refusal"`) is reported as an error, not read as an answer. The key is sent only in the `x-api-key` header and is never logged.

### Phone as an Edge (Android)

Off by default, and not available on iOS. When the user turns on "Send detections to the ground station" after reading the disclosure, the app sends detection boxes and scores to the ground station address the user entered (port 9000 by default), only while a search is running and the app is in view. It sends no person crops and no face data (crops are null by default). It sends a small picture of the whole camera view, which can show people, only after the ground station asks for video. The link is not encrypted and requires the ground's edge token for any address except loopback. The consent is stored in `flightrisk_ground_link`, which backup excludes, and is tied to the address. The ground station cannot yet match these detections to a search target.

### Pre-Submission Checklist

- [ ] Privacy policy created and hosted
- [ ] First-launch privacy notice implemented (see `first-launch-notice.md`)
- [ ] All permission usage descriptions set in Info.plist (iOS) and AndroidManifest.xml (Android)
- [ ] Privacy Nutrition Labels configured in App Store Connect (iOS)
- [ ] Data Safety section completed in Google Play Console (Android)
- [ ] Age rating set to 17+ on both platforms
- [ ] App Review Notes prepared (iOS)
- [ ] Cloud LLM opt-in disclosure implemented in-app
- [ ] App Review notes for the local network permission and the ATS exception pasted into App Store Connect
- [ ] Owner confirmation of the App Privacy questionnaire answers for the ground link and the cloud opt-in
- [ ] Owner and lawyer confirmation of the Google Play Data Safety answers, which are provisional (see `docs/mobile-followups.md`)
- [x] `CHANGE_WIFI_STATE` removed from the Android manifest; the Android privacy notice covers the ground station link and phone-as-edge and links to the privacy policy
- [x] Android: target API 36, 16 KB aligned native libraries, release build with R8, release signing configuration, adaptive icon (see "Build requirements")
- [ ] Android: the owner steps in "Store checklist (Android)"
- [x] Desktop dashboard: cloud LLM (Claude) off by default and gated on `FLIGHTRISK_ALLOW_CLOUD_LLM=1`
- [ ] User data deletion flow implemented and tested (Android: implemented, "Delete saved sessions and recordings"; not tested on a device)
- [ ] No third-party analytics/tracking/advertising SDKs included
