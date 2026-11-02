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

The Android answers below describe what the code does. **The Data Safety answers are the owner's decision.** Every answer marked provisional depends on how Google's definitions are read, and nothing here has been reviewed by a lawyer.

### Permission Rationale

Declared in `mobile/app/src/main/AndroidManifest.xml`.

| Permission | Rationale |
|-----------|-----------|
| `android.permission.CAMERA` | Required for real-time person detection and face matching to locate a missing child. Detection runs on the device. |
| `android.permission.ACCESS_FINE_LOCATION` and `ACCESS_COARSE_LOCATION` | Reads the location (through Google Play services, `FusedLocationProviderClient`) when a match is detected so the user can navigate back to that location. Kept in memory only; the database has no location fields. |
| `android.permission.INTERNET` | Needed for the paths the user starts: the connection to the ground station address the user enters (dashboard link on port 5555 by default, and the optional phone-as-edge link on port 9000 by default), the optional cloud reasoning request to Anthropic after the opt-in, and the optional on-device model download. |
| `android.permission.ACCESS_NETWORK_STATE` | Used to check that a network is available before a cloud reasoning request (`NET_CAPABILITY_VALIDATED`) and to check that the phone is on Wi-Fi when it looks for a Tello drone. |
| `android.permission.ACCESS_WIFI_STATE` | Used to read the current Wi-Fi connection when checking whether the phone is joined to a Tello drone's own Wi-Fi network. |
| `android.permission.CHANGE_WIFI_STATE` | Declared. **Unsure:** no code that changes Wi-Fi state was found. The owner should confirm whether it is still needed, and remove it if not. |
| `android.permission.VIBRATE` | Vibrates the phone on a match alert. |

The manifest also declares that a camera and Wi-Fi hardware are required (`uses-feature`).

### Network destinations in the Android app

Found by searching `mobile/app/src/main/java` for connection-opening code. A unit test (`CloudPathAuditTest`) fails if a new file opens a connection or names a cloud host.

1. The ground station address typed in the Ground screen: a plain socket WebSocket (`RawWebSocket`) and a plain HTTP health check (`MiniHttp`). Commands, status requests and the dashboard key; no images. Not encrypted (see "Cleartext policy" below).
2. The ground station's edge port, only when the user turns on "Send detections to the ground station" in Settings (phone-as-edge): box positions and scores, and a small picture of the whole camera view only when the ground asks for video. Not encrypted. See below.
3. `https://api.anthropic.com/v1/messages` through OkHttp, only after the cloud opt-in.
4. `https://storage.googleapis.com/mediapipe-models/...` through `HttpURLConnection`, only when the user taps "Download Model" for the on-device Gemma model. It is a plain GET for a file; no photo, crop, description or location is sent. Google sees the phone's IP address and the requested file. Its handling is outside this project and was not reviewed. (The iOS app downloads from Hugging Face instead.)
5. The Tello drone on its own Wi-Fi network (UDP), when the user flies a Tello from the Android app. This is a local link to the drone; video is received, not sent.

### Cleartext policy

The Android network security config (`res/xml/network_security_config.xml`) blocks cleartext for every platform HTTP stack except `localhost` and `127.0.0.1`, and trusts only system certificate authorities. The ground clients use plain sockets, which that file does not govern, and enforce their own rule in code (`LanAddressPolicy`): cleartext only to private, link-local and loopback addresses (10/8, 172.16/12, 192.168/16, 169.254/16, 127/8, `::1`, `fc00::/7`, `fe80::/10`), judged on the resolved address, before a connection is made or the key is sent. A host name that resolves to any public address is refused. The ground link is not encrypted, so the dashboard key and the edge token travel in the clear on the user's network. The Android app does not support `wss://` or `https://` for the ground link.

### Data Safety Section

**Provisional.** Facts first:

- With the default settings, no photo, crop, face data or location leaves the phone. Camera frames are not uploaded.
- With cloud reasoning on (opt-in, a saved Claude key and Cloud Claude selected), the cropped image of each detected person, the reference photo and the typed description are sent to Anthropic over TLS. The images can show a face.
- With the ground station link on, commands and status requests go to the address the user entered. With the phone-as-edge link on, box positions and scores go to that address. The user's own ground station is not a third party the developer runs, but Google's rules on "shared" and "collected" data may still treat these as data transmitted off the device. The owner decides.
- Every Android search is recorded as an MP4 video of the camera view with the detection boxes drawn on it (`SearchPipeline.kt`, `SessionRecorder`, in `filesDir/recordings`), with no setting to turn it off. Match images are saved as JPEG files in `filesDir/snapshots`. The Room database holds scores, timestamps, reasoning text, file paths and the typed description. A reference photo taken with the camera stays as a file in the cache. GPS coordinates are kept in memory only. All of this is in the app's private storage and is not sent anywhere by the app, but the backup rules exclude only preference files, so Android Auto Backup and device transfer can copy the video, images and database. The app has no delete control yet (planned); data is removed by clearing app storage or uninstalling. **Unsure:** how Google's form treats data that stays on the device but is backed up by the operating system.
- Location comes through Google Play services (`FusedLocationProviderClient`), which is a Google component on the phone; the owner should consider whether to name it on the form.

#### Data collected or shared (provisional draft)

| Question | Draft answer |
|----------|--------------|
| Does your app collect or share any of the required user data types? | Provisional. The earlier answer "No" does not hold when cloud reasoning is on, because images are sent to Anthropic. Images also leave the phone through phone-as-edge (a picture of the whole camera view, sent to the user's own ground station when it asks for video). The owner decides how to answer, including whether the optional cloud feature and the user's own ground station count. |
| Is all of the user data collected by your app encrypted in transit? | Provisional. Cloud reasoning: yes (TLS). The ground station and edge links: no, they are plain `http://` and `ws://` (only on a private network, by the app's own rule). |
| Do you provide a way for users to request that their data is deleted? | Provisional. **No, not yet:** the app has no delete control. Android session data (video, images, database) is removed by clearing the app's storage in system Settings or uninstalling. A delete action is planned; do not answer "Yes" until it ships. |

#### Data types (provisional draft)

| Data Type | Collected | Shared | Ephemeral | Purpose |
|-----------|-----------|--------|-----------|---------|
| Location (approximate) | No (the coarse permission is requested together with the fine one) | No | -- | -- |
| Location (precise) | Read on the device, kept in memory only | No | Yes (not stored) | App functionality |
| Photos/Videos | Provisional: the app records every search as a video and saves match images in private storage (not sent by the app, but backed up by Android); with cloud reasoning on, the reference photo and crops go to Anthropic; with phone-as-edge on, a picture of the whole camera view goes to the user's ground station when it asks | Provisional | No (stored until removed) | App functionality |
| Face / biometric data | Processed on-device. With cloud reasoning on, crops and the reference photo that can show a face go to Anthropic | Provisional | Yes (session-scoped) for templates and embeddings | App functionality |
| Personal identifiers | No | No | -- | -- |
| App activity | No | No | -- | -- |
| App diagnostics | No | No | -- | -- |

**Summary for the Data Safety form:**
- The developer runs no server and receives no data from the app.
- Data leaves the phone only through paths the user starts: the ground station links the user configures, cloud reasoning after the opt-in, and the on-device model download.
- No analytics, advertising or tracking SDKs are included.
- Camera and location are used for core functionality only.

### Face Detection Feature Flag

- **Declaration required:** Yes. Google Play requires disclosure when an app uses face detection.
- **Declaration text (revised, provisional):** "This app uses on-device face detection and recognition to match detected persons against a reference photo provided by the user. Face embeddings are computed on the device, kept only in memory for the active session, and never transmitted. If the user turns on the optional cloud reasoning feature, images of detected persons and the reference photo, which can show a face, are sent to Anthropic for analysis. That feature is off by default. On Android, searches are recorded as video in the app's private storage." The earlier text said that no face data is transmitted; that is not true of images when cloud reasoning is on.

### Age Rating (IARC)

- **Rating:** 17+ or equivalent (PEGI 18 / USK 18 / GRAC 18 depending on region)
- **Rationale:** Crisis/safety context involving missing children. Intended for adult users only.
- **Content descriptors:** None applicable (no violence, gambling, etc.). Rating is based on sensitive subject matter.

### Privacy Policy URL

- **Required:** Yes. Must be provided in Google Play Console under "Store listing > Privacy policy."
- **URL:** To be set at deployment (host `docs/privacy-policy.md` as a web page). The Android onboarding notice does not link to the policy yet (the iOS notice does).

### Store Listing Notes (Recommended)

Include in the app description or "About this app" section, after the owner and a lawyer have reviewed it:

> Person detection and matching run on your device, and face templates are never sent anywhere. Cloud reasoning is off by default; if you turn it on, the cropped images of detected people, your reference photo and your description are sent to Anthropic. Each search is recorded as a video in the app's private storage on your phone. The optional ground station link sends commands to a computer you run, on your own network, and the optional phone-as-edge mode can send detections and, when asked, a picture of your camera view to that computer. See our privacy policy for full details.

The earlier recommended text ("No biometric data is collected, transmitted, or stored") is withdrawn because it conflicts with the cloud path.

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

- Settings keys (UserDefaults suite `flightrisk_config`): `flightrisk_reasoning_provider` (`local_gemma`, `cloud_claude` or `off`) and `flightrisk_allow_cloud_llm` (default false). A stored cloud choice without the opt-in flag is ignored and removed.
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
- The first-launch notice (`OnboardingScreen.kt`, line 175) states that camera frames are not uploaded and that images leave the phone only with cloud reasoning on. That second sentence is not true when phone-as-edge is on and the ground asks for video.

### Phone as an Edge (Android)

Off by default, and not available on iOS. When the user turns on "Send detections to the ground station" after reading the disclosure, the app sends detection boxes and scores to the ground station address the user entered (port 9000 by default), only while a search is running and the app is in view. It sends no person crops and no face data (crops are null by default). It sends a small picture of the whole camera view, which can show people, only after the ground station asks for video. The link is not encrypted and requires the ground's edge token for any address except loopback. The consent is stored in `flightrisk_ground_link`, which backup excludes, and is tied to the address. The ground station cannot yet match these detections to a search target.

### Pre-Submission Checklist

- [ ] Privacy policy created and hosted
- [ ] First-launch privacy notice implemented (see `first-launch-notice.md`)
- [ ] All permission usage descriptions set in Info.plist (iOS) and AndroidManifest.xml (Android)
- [ ] Privacy Nutrition Labels configured in App Store Connect (iOS)
- [ ] Data Safety section completed in Google Play Console (Android)
- [ ] Face detection declaration submitted (Google Play)
- [ ] Age rating set to 17+ on both platforms
- [ ] App Review Notes prepared (iOS)
- [ ] Cloud LLM opt-in disclosure implemented in-app
- [ ] App Review notes for the local network permission and the ATS exception pasted into App Store Connect
- [ ] Owner confirmation of the App Privacy questionnaire answers for the ground link and the cloud opt-in
- [ ] Owner and lawyer confirmation of the Google Play Data Safety answers and the face detection declaration, which are provisional (see `docs/mobile-followups.md`)
- [ ] Owner decision on `CHANGE_WIFI_STATE` (declared; no use found) and on the Android onboarding notice, which says nothing about the ground station link or phone-as-edge (each has its own consent step) and has no privacy policy link
- [x] Desktop dashboard: cloud LLM (Claude) off by default and gated on `FLIGHTRISK_ALLOW_CLOUD_LLM=1`
- [ ] User data deletion flow implemented and tested
- [ ] No third-party analytics/tracking/advertising SDKs included
