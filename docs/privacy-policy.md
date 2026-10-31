# FlightRisk Mobile -- Privacy Policy

**Effective Date:** September 4, 2026
**Last Updated:** October 30, 2026
**Contact:** krlehnardt@gmail.com

---

## 1. Overview

FlightRisk is an AI-powered tool designed to help parents and guardians locate a missing child in crowded environments. It uses on-device face recognition and appearance matching via the phone camera. This privacy policy explains what data the app processes, how it is handled, and your rights.

---

## 2. On-Device Processing

All face detection, face recognition (ArcFace), and appearance matching (CLIP ReID) processing runs on your device using ONNX Runtime and/or CoreML. **Face embeddings and appearance feature vectors never leave your phone under any setting.** Specifically:

- Face embeddings (numerical representations of facial features) are computed locally and exist only in device memory during the active session.
- Body detection and appearance feature extraction happen locally.
- No biometric templates or embeddings are transmitted to any server, cloud service, or third party.
- **Images are different from templates.** With the default settings, no image of any person leaves your device. Only if you turn on cloud reasoning, save a Claude API key and select Cloud Claude (Section 5) are images sent off the device: the reference photo you provided and the cropped image of each candidate person that is sent for analysis. Those images can show a face. See Section 5 for exactly what is sent and how to turn it off.

---

## 3. Biometric Data Handling

### Ephemeral Processing

All biometric templates (face embeddings and appearance features) are **session-scoped and ephemeral**:

- They are created in memory when you start a search session.
- They are discarded when the session ends or the app is closed.
- They are never written to persistent storage, transmitted over the network, or shared with any party.

This section covers templates and embeddings only. Images (the reference photo and person crops) are handled as described in Sections 4 and 5.

### Regulatory Compliance

- **BIPA (Illinois Biometric Information Privacy Act):** Biometric templates and embeddings are not stored beyond the active session and are not transmitted. If you enable a cloud LLM backend, images that may show a face are transmitted to the provider you chose (Section 5). Whether that transmission is subject to BIPA depends on the provider's processing and has not been determined; do not enable the feature for other people's images without that review.
- **GDPR (EU General Data Protection Regulation):** Templates and embeddings are processed ephemerally and locally under your sole control. When you enable a cloud LLM backend, the provider you choose receives images and acts as a recipient of that data. You are responsible for having a lawful basis for that transfer.
- **Other biometric privacy laws:** The same rule applies in every jurisdiction: templates and embeddings stay on the device; images leave the device only through the opt-in cloud LLM feature.

---

## 4. Session Data (Local Storage)

During a search session, the app may store the following data locally in a SQLite database on your device:

- Match thumbnails (cropped images of detected persons who matched your search criteria)
- Match confidence scores
- GPS coordinates of where matches were detected
- Timestamps

This data:

- Is stored **only on your device** in a local SQLite database.
- Is **never transmitted** by the app to any server, cloud service, or third party.
- Is **user-deletable** at any time from within the app settings.
- Is not backed up to iCloud or Google Drive by the app itself. Your operating system's own backup (iCloud or iTunes backup on iPhone, Android Auto Backup to your Google account) may include the app's local database if you have backup turned on; that is outside the app's control. On Android the app excludes only its preference files from backup (the encrypted secret store, the cloud reasoning choice, the ground station link consent and the general config file), not the database.

---

## 5. Cloud Reasoning (Optional)

FlightRisk supports an **optional** cloud reasoning feature that uses Claude, a service of Anthropic, for additional analysis of potential matches. Claude is the only cloud service either app (iOS or Android) can use.

**When disabled (default):** No data is transmitted to any cloud service. Cloud reasoning is off until you do all three of the following in Settings:

1. Turn on "Allow cloud reasoning" and confirm the consent dialog.
2. Save a Claude API key.
3. Select Cloud Claude as the reasoning provider.

Saving a key by itself does not turn cloud reasoning on. With cloud reasoning off, the app uses the on-device model or no reasoning at all.

**When enabled:**

- The app sends **images and text** to Anthropic (`https://api.anthropic.com/v1/messages`): for a photo comparison, the **reference photo** you provided plus the **cropped image of the candidate person**; for a description comparison, the cropped image alone. In both cases the **text description you typed** is added to the request when you entered one. Images are JPEG-encoded. These images can show a face.
- No face embeddings, appearance feature vectors, GPS coordinates, or other session data are included.
- The consent dialog tells you this before the feature can be turned on. It reads: "With cloud reasoning on, the cropped image of each detected person, the reference photo you chose and the text description you entered are sent to Anthropic (Claude) for analysis. The images can show a face, and Anthropic's privacy policy applies to them. Nothing is sent until you also save a Claude API key and select Cloud Claude. You can turn this off at any time here; turning it off stops sending at once and cancels any request still in progress."
- The data transmitted is subject to Anthropic's privacy policy. FlightRisk does not control how Anthropic retains it.
- You can turn this off at any time, which stops cloud transmission at once and cancels any request still in progress.
- Your Claude API key is stored on your device only: in the iOS Keychain on iPhone (this device only, available when the phone is unlocked), or on Android in encrypted preferences (`EncryptedSharedPreferences`, protected by a key in the Android Keystore). It is sent only to Anthropic, as the credential for these requests.

---

## 5A. Ground Station Link on iPhone (Optional)

The iPhone app's Ground tab can connect to a ground station, which is a computer you run yourself. This is off until you type a ground station address and key in Settings and switch the link on.

- The app connects to the address you enter and to no other ground station address. It does not search for stations, and it does not use Bluetooth or Bonjour discovery. The app has no direct connection to a drone.
- Over this link the app reads status from the ground station and sends commands that you tap (land commands, resume-search commands and detection profile choices). It does not send camera frames, person crops, the reference photo or your location.
- The address is saved on your device. The ground station key is stored in the iOS Keychain on your device (this device only, available when the phone is unlocked) and is sent to the ground station you entered so that it accepts the connection.
- If the address starts with `ws://` or `http://`, which is normal on a home or field network, the link is **not encrypted**. Anyone on that network could read the traffic, including the key. Use a network you trust. The app refuses a `ws://` or `http://` address unless the address it resolves to is a local network address (10.x, 172.16 to 172.31, 192.168.x, 127.x, 169.254.x, or the IPv6 loopback, unique-local and link-local ranges). A host name is looked up first and the app connects only to those of its addresses that are local (a name that publishes both a private and a public address is used by its private address; a name with no local address is refused). It never connects to the name itself, and it connects directly, not through a proxy, when a direct connection works. This check happens before the key or any request is sent. Away from your local network, use an encrypted `wss://` or `https://` address. A VPN also works if it gives the ground station an address in a private range; addresses in 100.64.0.0/10, which Tailscale uses, are refused for `ws://` and `http://`. An encrypted address is not restricted to the local network, and the app does not pin certificates: it trusts the certificates that iOS trusts. The app also declares the iOS local network permission and the App Transport Security setting `NSAllowsLocalNetworking`.
- What the ground station does with data is governed by whoever runs it. That is outside this policy and outside the app.

---

## 5B. Android: Ground Station Link, Phone as an Edge, and Cloud Reasoning (Optional)

This section applies to the Android app. Everything in Section 5 about cloud reasoning also applies to Android, with these differences.

**Cloud reasoning on Android.** It is off until you turn on "Allow cloud reasoning" and confirm the consent dialog, save a Claude API key and select Cloud Claude. The consent dialog has the same words as on iPhone. Your choice is stored in a preferences file that Android backup and device transfer exclude, so a restored or new phone starts with cloud reasoning off. Your Claude key is stored in encrypted preferences protected by the Android Keystore. Turning the switch off, selecting another provider or removing the key stops sending at once and cancels a request in progress.

**Ground station link (console).** The Ground screen connects to the ground station address you type there, and to no other. The Android app accepts only plain `http://` and `ws://` addresses and refuses a host name that resolves to any public address, so it connects only to private-network, link-local and loopback addresses; the check happens before the key or any request is sent. That link is not encrypted, so the ground station key travels in the clear on your network. Use a network you trust. The Android app does not support `wss://` or `https://` for this link. The key is stored in encrypted preferences protected by the Android Keystore, and the Ground screen never shows it again after you save it. Over this link the app sends the commands you tap (land, resume search and detection profile choices) and status requests. It does not send camera frames, person crops, the reference photo or your location.

**Phone as an edge (optional, off by default).** In Settings, "Ground station link" can send what this phone's camera detects to the edge port of a ground station you run (port 9000 by default). It starts only after you read a disclosure and turn it on, and only while a search is running and the app is on screen. It sends box positions (where each person appears in the picture) and detection scores. It sends no pictures of people and no face data. If the ground station asks for video, the phone also sends a small picture of the whole camera view, which can show people. The ground station cannot yet match these detections to a search target, so use this mode for testing and mapping only. The link is not encrypted, requires the ground station's edge token (kept in the same encrypted preferences), and your consent covers the address you entered, not a network: on a different Wi-Fi network, another device with the same address would receive what is sent, including the edge token. Your choice is stored in a preferences file that backup excludes. What the ground station does with the data is governed by whoever runs it.

**Direct drone link.** The Android app can also fly a DJI Tello drone directly over the drone's own Wi-Fi network (UDP). That link carries flight commands to the drone and video from the drone to the phone; the app sends nothing else on it.

**On-device model download.** When you tap "Download Model" for the on-device Gemma model, the Android app downloads it from Google's storage (`storage.googleapis.com`). No photo, crop, description or location is sent, but Google necessarily sees the phone's IP address and which file is requested; what it does with that is governed by its own policy, which this project has not reviewed. The iPhone app downloads its model from Hugging Face instead (Section 11).

---

## 6. Location Data

- GPS coordinates are recorded **locally** when a match event is detected, so you can navigate back to the location where the match occurred.
- Location data is stored only in the local SQLite database on your device.
- Location data is **never transmitted** to any server, cloud service, or third party.
- You can delete all location data from within the app settings at any time.

---

## 7. Camera Usage

- The camera is used solely for real-time person detection and face matching to help locate a missing child.
- Camera frames are processed on-device in real time and are not recorded, stored, or transmitted, except for match thumbnails stored locally as described in Section 4 and the crops, reference photo and description sent when the optional cloud reasoning feature is enabled (Section 5).

---

## 8. Data Sharing

**FlightRisk does not share any data with third parties.** Specifically:

- No biometric data, session data, location data, or camera data is sold, shared, rented, or disclosed to any third party.
- No analytics, advertising, or tracking SDKs are included in the app.
- No user accounts are created or required.

The only exceptions are the optional features you turn on or start yourself: cloud reasoning (Section 5), which transmits the reference photo, cropped person images and your typed description to Anthropic, the ground station link (Sections 5A and 5B), which sends your commands to the address you entered, the Android phone-as-edge mode (Section 5B), which sends detection boxes and scores (and, when the ground station asks, a picture of the camera view) to the ground station you entered, and the on-device model download (Section 11), from Hugging Face on iPhone and from Google storage on Android, which sends no photos or session data.

---

## 9. Children's Privacy (COPPA Compliance)

- FlightRisk is a tool designed **for parents and guardians**, not for children.
- The app does not target children as users.
- The app does not collect any personal information from children.
- No user accounts are created. No names, email addresses, or identifying information of any person (adult or child) are collected by the app.
- The reference photo used for matching is provided by the parent/guardian and processed on-device. It is not stored beyond the session and is not transmitted unless the optional cloud reasoning feature (Section 5) is enabled, in which case it is sent to Anthropic.

---

## 10. Data Retention and Deletion

- **Biometric templates and embeddings:** Not retained. Ephemeral and session-scoped. Discarded when the session ends.
- **Session data (thumbnails, scores, locations):** Stored locally until the user deletes it. The user can delete all session data at any time from within the app.
- **App deletion:** Uninstalling the app removes all locally stored data.

---

## 11. Security

- All on-device data is protected by the operating system's native app sandboxing (iOS App Sandbox / Android app-private storage).
- The app makes network connections in three cases, each started by you. Cloud reasoning, when you have turned it on, uses TLS encryption to Anthropic. The ground station link goes to the address you entered. On iPhone it is encrypted only if you entered a `wss://` or `https://` address; a `ws://` or `http://` link is not encrypted, and the app refuses it unless the address is on your local network (Section 5A). On Android the link is never encrypted and the app uses only private-network addresses (Section 5B). The on-device model download is a user-started model download: when you tap the download button for the on-device Gemma model in Settings, the iPhone app downloads the model `mlx-community/gemma-2-2b-it-4bit` from Hugging Face (huggingface.co), and the Android app downloads its Gemma 2 2B model from Google storage (`storage.googleapis.com`), each a third party. No photo, crop, description or location is sent, but the host necessarily sees the phone's IP address and which model file is requested; what it does with that is governed by its own policy, which this project has not reviewed.
- API keys are kept in the iOS Keychain on iPhone and in encrypted preferences protected by the Android Keystore on Android, not in plain app preferences, files or logs. The Android secret store is excluded from Android backup.

---

## 12. Changes to This Policy

We may update this privacy policy from time to time. Changes will be posted within the app and at the privacy policy URL provided during app store submission. Continued use of the app after changes constitutes acceptance of the updated policy.

---

## 13. Contact

For questions or concerns about this privacy policy or the app's data practices:

**Email:** krlehnardt@gmail.com
