# FlightRisk Mobile -- App Store Compliance Checklist

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
- What it does not do: App Transport Security applies to `URLSession`, not to `NWConnection`. The WebSocket link is therefore not restricted by this setting, and `GroundEndpoint.parse` accepts any host. The app does not itself limit unencrypted connections to the local network.
- What the user should know: a `ws://` or `http://` link is not encrypted, and the API key for the ground station travels in the clear on that link (in the `Authorization` header of the health check, and as the `api_key` field of the Socket.IO connect packet). Use a network you trust, such as a private WiFi network or the fleet access point, and not a public or shared one. Away from the local network, use an encrypted address (`wss://` or `https://`) or a VPN. If a user enters a `ws://` address on the internet, the app connects and sends the key in the clear. The app also accepts `wss://` and `https://` addresses; those are encrypted but need a certificate the phone trusts.
- Follow-up: a separate change is planned to add a local-address policy for the ground address, like the one on Android. Until it ships, the docs must not say the app or iOS blocks non-local cleartext addresses, because neither does.

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

- **Compliance:** The app collects camera frames and location data solely for core functionality (locating a missing child). No user data is transmitted off-device except through two user-started paths, and a third user-started network use exposes the IP address. First, the ground station link: the app connects to the address the user enters in Settings and sends land, resume-search and detection-profile commands and status requests, never camera frames, crops or the reference photo. The app does not restrict that address to the local network. Third, the on-device model download from Hugging Face, which sends no user data (the host sees the IP address). Second, the optional cloud reasoning feature, which is off by default and sends the cropped person images, the reference photo and the typed description to Anthropic only after the opt-in toggle is on, a Claude API key is saved and Cloud Claude is selected. No user accounts are created. No personal information is collected, stored off-device, or shared.
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
> App Transport Security: Info.plist sets NSAllowsLocalNetworking only, so that the plain-HTTP health check to a ground station on the local network works. There is no NSAllowsArbitraryLoads. The WebSocket link uses Network.framework, to which App Transport Security does not apply. The app does not itself restrict cleartext addresses to the local network.
>
> The Settings screen can download an on-device language model from Hugging Face when the user taps the download button; nothing is downloaded otherwise.
>
> Cloud reasoning is off by default. It sends the cropped image of a detected person, the reference photo and the typed description to Anthropic only after the user turns on "Allow cloud reasoning" and confirms the consent dialog, saves a Claude API key and selects Cloud Claude. Reviewers can leave it off; all other features work without it.
>
> The first-launch privacy notice is shown before the camera starts.

---

## Google Play Store

### Permission Rationale

| Permission | Rationale |
|-----------|-----------|
| `android.permission.CAMERA` | Required for real-time person detection and face matching to locate a missing child. All processing is on-device. |
| `android.permission.ACCESS_FINE_LOCATION` | Records GPS coordinates when a match is detected so the user can navigate back to that location. Stored locally only. |

### Data Safety Section

#### Data collected or shared

| Question | Answer |
|----------|--------|
| Does your app collect or share any of the required user data types? | No |
| Is all of the user data collected by your app encrypted in transit? | N/A (no data transmitted by default). When Cloud LLM is enabled, yes (TLS). |
| Do you provide a way for users to request that their data is deleted? | Yes. All local session data is user-deletable from within the app. |

#### Data types

| Data Type | Collected | Shared | Ephemeral | Purpose |
|-----------|-----------|--------|-----------|---------|
| Location (approximate) | No | No | -- | -- |
| Location (precise) | Yes (local only) | No | No (user-deletable) | App functionality |
| Photos/Videos | No | No | -- | -- |
| Face / biometric data | Processed on-device | No | Yes (session-scoped) | App functionality |
| Personal identifiers | No | No | -- | -- |
| App activity | No | No | -- | -- |
| App diagnostics | No | No | -- | -- |

**Summary for Data Safety form:**
- No data collected (in Google's definition: data is not transmitted off-device).
- No data shared with third parties.
- Camera and location used for core functionality only.

### Face Detection Feature Flag

- **Declaration required:** Yes. Google Play requires disclosure when an app uses face detection.
- **Declaration text:** "This app uses on-device face detection and recognition to match detected persons against a reference photo provided by the user. All face processing happens entirely on-device. No face data is collected, transmitted, or stored beyond the active session."

### Age Rating (IARC)

- **Rating:** 17+ or equivalent (PEGI 18 / USK 18 / GRAC 18 depending on region)
- **Rationale:** Crisis/safety context involving missing children. Intended for adult users only.
- **Content descriptors:** None applicable (no violence, gambling, etc.). Rating is based on sensitive subject matter.

### Privacy Policy URL

- **Required:** Yes. Must be provided in Google Play Console under "Store listing > Privacy policy."
- **URL:** To be set at deployment (host `docs/privacy-policy.md` as a web page).

### Store Listing Notes (Recommended)

Include in the app description or "About this app" section:

> All face recognition and person matching runs entirely on your device. No biometric data is collected, transmitted, or stored. See our privacy policy for full details.

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
- [x] Desktop dashboard: cloud LLM (Claude) off by default and gated on `FLIGHTRISK_ALLOW_CLOUD_LLM=1`
- [ ] User data deletion flow implemented and tested
- [ ] No third-party analytics/tracking/advertising SDKs included
