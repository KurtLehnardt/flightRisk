# FlightRisk: draft answers for Apple App Privacy and Google Play Data Safety

Status: DRAFT for the owner to confirm. These are legal declarations. Nothing here has had a legal review. Where the code does not settle an answer, the item is marked **OWNER TO CONFIRM** and is listed again in section 3.

Sources: `docs/app-store-compliance.md`, `docs/mobile-followups.md`, `docs/privacy-policy.md` as it stands on `origin/feature/android-model-integration` and `origin/feature/ios-model-integration` (neither merged), and greps of `origin/main` and the two model branches.

## 0. Facts from the code

- **Neither phone app does face detection or face recognition.** The model branches use YOLOX-S for person detection and open_clip ViT-B-16 for appearance matching. The Android model branch's privacy policy says so explicitly.
- **Stale wording on the iOS branch.** `origin/feature/ios-model-integration:docs/privacy-policy.md` still says "The Android app may also run face detection and face recognition" and calls the templates "biometric". It is out of date against the Android branch. The two policies must agree before the policy URL is submitted (OWNER TO CONFIRM item 1).
- **No analytics, crash reporting or ad SDKs.**
  - iOS (`ios/project.yml`): the only packages are mlx-swift, mlx-swift-examples (which brings in swift-transformers and others) and the local FlightRiskGround package. No analytics, crash or ad package. The app's onboarding text and `PrivacyInfo.xcprivacy` say there is no analytics or tracking code.
  - Android (`mobile/app/build.gradle.kts`): AndroidX, Compose, CameraX, ONNX Runtime, LiteRT, MediaPipe tasks-genai, `play-services-location`, security-crypto, Room, OkHttp, kotlinx. No Firebase, Crashlytics, ads or analytics. The model branch only changes the ONNX Runtime version and removes LiteRT.
  - A grep of `ios/` and `mobile/` on `origin/main` for firebase, crashlytics, sentry, analytics, admob, appsflyer, amplitude and mixpanel found only the "no analytics" statements.
- **No developer-run server.** No code sends data to any server the developer operates. The developer receives nothing.
- **What leaves the phone, and where** (every path is started by the user):

| Path | Platform | Data | Destination | Encryption |
|---|---|---|---|---|
| Ground station link | iOS and Android | Commands the user taps (land, resume search, detection profile) and status requests, plus the ground station key as a credential. No camera frames, crops, reference photo or location. | The address the user types, normally the user's own laptop on the local network | TLS for `https://` and `wss://`. Plain `http://` and `ws://` only to a local-network address (the app refuses other plain addresses). |
| Cloud reasoning (opt-in, off by default) | iOS and Android | The cropped image of each candidate person, the reference photo (photo-comparison mode), and the typed description. Images are JPEG and can show a face. No embeddings, no location. | Anthropic, `https://api.anthropic.com/v1/messages`, with the user's own Claude API key | TLS |
| Phone as an edge (opt-in, off by default) | Android only | Detection box positions and scores. When the ground station asks for video, a small picture of the whole camera view, which can show people. No crops, no face data. Needs the edge token. | The ground station address the user typed, port 9000 by default | None: plain `ws://` |
| Model download (user taps the button) | iOS: Hugging Face (`huggingface.co`). Android: `storage.googleapis.com` | No user data. The host sees the IP address and the file requested. | Third-party host | TLS |
| Direct Tello drone link | Android | Flight commands to the drone, video from the drone to the phone | The drone's own Wi-Fi network (UDP) | None |

- **On-device storage.**
  - iOS: match results are kept in memory only; the app writes no database and no session files. Keys are in the Keychain.
  - Android: a Room database (session times, counts, match scores, reasoning text, the typed description, file paths), and, only with "Record search video" on (off by default), an MP4 and match images in private storage. Backup and device transfer exclude all of it. GPS is kept in memory only and is never stored.
- **Deletion flow.**
  - Android: "Delete saved sessions and recordings" (Settings, with a confirmation) deletes the sessions, matches, feedback, recordings and match images. There are also remove-key controls, "Delete Model", and uninstall. `docs/mobile-followups.md` marks this as fixed in the Android privacy change but not verified on a device. The checklist line "User data deletion flow implemented and tested" is still unchecked.
  - iOS: nothing is stored beyond settings and keys, so there is no data-deletion control. Keys can be removed in Settings, and uninstalling removes everything.
  - Neither app has accounts. The developer holds no user data, so the developer cannot delete anything on a user's request.

## 1. Apple App Privacy (iOS)

### Top-line answer

**"Do you or your third-party partners collect data from this app?" Draft answer: Yes.**

Reason: Apple's definition of "collect" is data transmitted off the device in a way that the developer or third-party partners can access for longer than needed to service the request. With cloud reasoning on, images and text go to Anthropic, which is a third party and can access them. The current `PrivacyInfo.xcprivacy` declares exactly Photos or Videos and Other User Content, and this draft matches it.

**OWNER TO CONFIRM (item 2):** Anthropic is not a "partner" the developer chose; the user supplies their own API key and sends the data to their own Anthropic account. Apple's guidance asks developers to disclose data sent to third-party code or services the app integrates, so the conservative answer is Yes. Answering No is defensible only if the owner decides that a user-keyed, opt-in call counts as the user's own transmission. If in doubt, answer Yes. A wrong No is the riskier mistake.

### Per data type

"Linked to the user" means linked to the user's identity or account. "Tracking" means linking with third-party data for advertising or sharing with data brokers.

| Apple data type | Collected? | Linked | Tracking | Purpose | Reason and code evidence |
|---|---|---|---|---|---|
| Photos or Videos | **Yes, only when cloud reasoning is on** | Yes (see item 3) | No | App Functionality | The reference photo and each person crop go to Anthropic. Consent dialog, `SettingsView.cloudConsentMessage`; `PrivacyInfo.xcprivacy` declares `NSPrivacyCollectedDataTypePhotosorVideos`. Camera frames themselves are never sent. |
| Other User Content | **Yes, only when cloud reasoning is on** | Yes (see item 3) | No | App Functionality | The typed description is added to the request. `PrivacyInfo.xcprivacy` declares `NSPrivacyCollectedDataTypeOtherUserContent`. |
| Precise Location | No | -- | -- | -- | Location is read on the device and shown on match alerts only. The cloud request carries no coordinates, and the ground link sends none. Policy section 5A and Section 5 say so. |
| Coarse Location | No | -- | -- | -- | Same. |
| Contacts, Contact Info (name, email, phone, address) | No | -- | -- | -- | No accounts, no sign-in, no contact access. |
| Health and Fitness, Financial Info, Sensitive Info, Browsing History, Search History, Purchases | No | -- | -- | -- | No such code. "Search" in the app means a physical search; it is not a search-history feature. |
| Identifiers (User ID, Device ID) | No | -- | -- | -- | No device or advertising identifier is read. The Claude API key is the user's credential, sent only to Anthropic. See item 3 on whether Apple counts that as a User ID. |
| Usage Data, Diagnostics | No | -- | -- | -- | No analytics or crash SDK. The app does not collect telemetry. |
| Audio Data | No | -- | -- | -- | The app does not record audio. (Confirm no microphone permission string; `docs/app-store-compliance.md` lists the permissions.) |
| Body, Other Data Types | No | -- | -- | -- | No face or biometric data leaves the phone. Appearance features stay in memory. |

Tracking: **No** for everything. `NSPrivacyTracking` is false and `NSPrivacyTrackingDomains` is empty.

### The ground station link: is it "collected"?

Draft position: **not collected.** Reasoning:

- The data goes only to a computer the user runs and enters by hand. The developer cannot access it and has no server in the path.
- No third party receives it. It is the user's own device on the user's own network, so Apple's test, that the developer or a third-party partner can access it, is not met.
- The link carries commands, status and a credential, not photos or location.

Counter-argument: if the user types a public `wss://` or `https://` address, data goes to whatever host they chose. That host is still the user's choice, not the developer's or a partner's.

**OWNER TO CONFIRM (item 4):** accept "not collected" for the ground link.

### The Hugging Face download

Draft position: **not collected.** It sends no user data. Hugging Face sees the IP address and the file name, to answer a request the user made by tapping a button. Apple's definition excludes data used only to service the request in real time. **OWNER TO CONFIRM (item 5).** Hugging Face's own handling was not reviewed.

### Other App Store Connect items

- Privacy policy URL: `https://github.com/KurtLehnardt/flightRisk/blob/main/docs/privacy-policy.md` (hosted; item 3 is done).
- Age rating and the Guideline 5.1.3 (health) and 2.5.13 (Face ID) answers in `docs/app-store-compliance.md` stand. The app has no face recognition, so the "own face recognition pipeline" sentence in the Guideline 2.5.13 compliance text is obsolete. Remove it (item 6).

## 2. Google Play Data Safety (Android)

### Top-line answers

- **"Does your app collect or share any of the required user data types?" Draft answer: Yes.** Images and text leave the phone through cloud reasoning (to Anthropic) and, on Android, through phone-as-edge (to the user's ground station).
- **"Is all of the user data collected by your app encrypted in transit?" Draft answer: No.** Phone-as-edge uses plain `ws://`, and the ground station link may be plain `http://` or `ws://` on a local network. Answering Yes would be false. Cloud reasoning is TLS.
- **"Do you provide a way for users to request that their data is deleted?" Draft answer: Yes**, but see item 8: the in-app delete control is not verified on a device, and the developer holds no data. Data sent to Anthropic follows Anthropic's policy, not the app.

Google's definitions (as in `docs/app-store-compliance.md`): **collected** is data transmitted off the device; **shared** is data transferred to a third party. Data processed only on the device is neither. Google also lets a developer leave out data that is "processed ephemerally" (held in memory and not kept beyond servicing the request) and data a user sends on their own initiative to a service where the user is clearly told. Whether those exemptions apply here is item 7.

### Per data type

| Google data type | Collected | Shared | Ephemeral | Required or optional | Purpose | Notes and evidence |
|---|---|---|---|---|---|---|
| Photos and videos | **Yes** (opt-in features only) | **Yes** with Anthropic (cloud reasoning); the user's own ground station is the open question (item 7) | Yes for what the app sends: nothing is kept by the app after sending. Saved recordings on the phone are not collected. | **Optional** (cloud reasoning off by default; phone-as-edge off by default) | App functionality | Cloud: reference photo and person crops, TLS. Edge: a whole-camera-view picture when the ground asks, plain `ws://`. Local-only: MP4 and match images with "Record search video" on, never uploaded, not backed up. |
| App activity > Other user-generated content | **Yes** (cloud reasoning only) | **Yes** with Anthropic | Yes | Optional | App functionality | The typed description is sent with the cloud request. Matches the iOS "Other User Content" answer. |
| Location (precise and approximate) | No | No | -- | -- | -- | `FusedLocationProviderClient` (Google Play services) reads GPS on the device. It is held in memory only, never stored, never sent. **OWNER TO CONFIRM (item 9):** whether to say this on the form given the Play services component. |
| Personal info (name, email, user IDs, address, phone, race, political or religious views, sexual orientation, other info) | No | No | -- | -- | -- | No accounts. Google has no "face" or "biometric" type, and no face template exists in any case. Face images in cloud requests are declared under Photos and videos. |
| Financial info, Health and fitness, Messages, Contacts, Calendar, Files and docs, Audio, Web browsing | No | No | -- | -- | -- | No such code. |
| App activity (page views, in-app search history, installed apps) | No | No | -- | -- | -- | Sessions are stored on the device only and never sent. |
| App info and performance (crash logs, diagnostics) | No | No | -- | -- | -- | No crash or analytics SDK. |
| Device or other IDs | No | No | -- | -- | -- | No advertising ID or device ID read. |

### Security practices section

- Data encrypted in transit: **No** (see top line; plain `ws://` edge link and optional plain `http://` ground link).
- Users can request deletion: **Yes**, in the app (item 8).
- Committed to the Play Families policy: No. Age rating is 17+.
- Independent security review: No.

### Play Console declarations that depend on the answers

- **Face detection declaration:** The old checklist item and the "Face matching description" suggested text are **obsolete**. Neither app does face detection or face recognition, and the Play Console has no separate face declaration anyway. Delete the "Face matching description" section's wording ("on-device face detection and recognition ... face embeddings") from `docs/app-store-compliance.md`, the store listing, and the review notes, and replace it with appearance-only matching. The same stale text appears on the iOS policy branch.
- **Health apps declaration:** Not relevant. The app does not provide health or medical features and has no health data. Answer "my app does not have any health features".
- **Financial features declaration:** Not relevant. No payments, loans, wallets or crypto.
- **Government apps declaration:** Not relevant. The app is not made by or for a government. **OWNER TO CONFIRM (item 10):** that the owner is publishing as an individual or company and not on behalf of a government body.
- **Ads declaration:** No ads. **News, COVID, VPN, social features:** Not relevant.
- **Permissions declarations:** Camera and location are core. `docs/app-store-compliance.md` has the rationale text. There is no background location permission to declare (confirm in the manifest, item 11).

## 3. OWNER TO CONFIRM

1. Which privacy policy text is final? The iOS model-integration branch's policy still describes Android face recognition and calls the templates "biometric"; the Android branch's policy removes both. Merge to one wording and host it before submitting either form.
2. Apple top line: is "Yes, we collect data" right, given the data goes to Anthropic with the user's own key after an explicit opt-in? Draft says Yes.
3. Apple "Linked to user": the manifest says Yes because each request carries the user's Anthropic API key. Is that the answer to give? (Answering No would say Anthropic cannot tie the images to the user, which is not true while the key is in the request.)
4. Is the user-entered ground station link "not collected" for Apple, and is it outside Google's "shared" for the same reason?
5. Is the model download (IP address and file name seen by Hugging Face or Google storage) a collection event for either store? Draft says no.
6. Remove the stale face-recognition sentences (Apple Guideline 2.5.13 text, Play "Face matching description", store listing and review notes) after the model branches merge. Is the owner content that the doc and listing now say appearance-only?
7. Google: does the phone-as-edge picture, sent to the user's own laptop in plain `ws://`, count as "collected" and "shared"? Draft treats it as collected and conservatively declares Photos and videos. Is the owner content with that, and with whether the ephemeral exemption is claimed?
8. Google deletion answer: the "Delete saved sessions and recordings" control is described as fixed but "not verified on a device", and the checklist item "User data deletion flow implemented and tested" is unchecked. Test it on a device before answering Yes.
9. Google location: should the form name Google Play services' location component? Draft says Location is not collected because it never leaves the phone.
10. Google Government declaration: confirm the app is not made for a government body.
11. Confirm the final manifest has no background-location, microphone or other permission that would change a "No" above (check after the model branches merge).
12. Google "encrypted in transit": confirm answering No is acceptable, or decide to remove the plain `http://`, `ws://` paths (this would change the product).
13. Cloud reasoning images may show a child's face. Do the owner's legal advisers want extra wording (children's data, BIPA, GDPR) in either form or the policy? The policy says whether BIPA applies "has not been determined".
14. Hugging Face (iOS) and Google storage (Android) handling of IP addresses was not reviewed. Confirm whether the policy and forms should name them.
