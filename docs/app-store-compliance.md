# FlightRisk Mobile -- App Store Compliance Checklist

---

## Apple App Store

### Permission Usage Descriptions (Info.plist)

| Key | Value |
|-----|-------|
| `NSCameraUsageDescription` | "FlightRisk uses the camera to detect and match people to help locate a missing child. All processing happens on your device." |
| `NSLocationWhenInUseUsageDescription` | "FlightRisk records your location when a match is detected so you can navigate back to where the match occurred." |

### Privacy Nutrition Labels (App Privacy)

| Data Type | Collected | Linked to User | Used for Tracking | Purpose |
|-----------|-----------|----------------|-------------------|---------|
| Camera | Yes | No | No | App Functionality |
| Precise Location | Yes | No | No | App Functionality |
| Photos or Videos | No | -- | -- | -- |
| Identifiers | No | -- | -- | -- |
| Usage Data | No | -- | -- | -- |
| Diagnostics | No | -- | -- | -- |
| Contact Info | No | -- | -- | -- |

**Summary for App Store Connect:**
- Data Not Collected: The app does not collect data that leaves the device.
- Data Not Linked to User: No user accounts, no identifiers collected.
- Data Not Used for Tracking: No advertising or tracking SDKs.

### App Review Guidelines Compliance

#### Guideline 5.1.1 -- Data Collection and Storage

- **Compliance:** The app collects camera frames and location data solely for core functionality (locating a missing child). No data is transmitted off-device except via the optional Cloud LLM feature, which transmits only cropped person images and requires explicit user opt-in. No user accounts are created. No personal information is collected, stored off-device, or shared.
- **Documentation to provide:** Link to privacy policy. Description of on-device-only processing architecture. Explanation of ephemeral biometric data handling.

#### Guideline 5.1.2 -- Data Use and Sharing

- **Compliance:** No data is shared with third parties. No analytics, advertising, or tracking SDKs are included. The optional Cloud LLM feature transmits cropped images only to the provider selected by the user, with explicit opt-in and clear disclosure.
- **Documentation to provide:** Privacy policy URL. In-app disclosure of Cloud LLM data transmission. First-launch privacy notice (see `first-launch-notice.md`).

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

> FlightRisk is an AI-powered tool for parents and guardians to locate a missing child in crowded environments. All face recognition and appearance matching runs entirely on-device using ONNX Runtime / CoreML. No biometric data leaves the device. Face embeddings are ephemeral (session-scoped) and are never stored permanently or transmitted. The optional Cloud LLM feature requires explicit opt-in and transmits only cropped person images. No user accounts, no data collection from minors, no third-party data sharing. See the in-app privacy notice shown at first launch.

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
- [ ] User data deletion flow implemented and tested
- [ ] No third-party analytics/tracking/advertising SDKs included
