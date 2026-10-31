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
- Is not backed up to iCloud or Google Drive unless you have enabled full-device backups through your operating system (which is outside the app's control).

---

## 5. Cloud Reasoning (Optional)

FlightRisk supports an **optional** cloud reasoning feature that uses Claude, a service of Anthropic, for additional analysis of potential matches. Claude is the only cloud service the iOS app can use.

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
- Your Claude API key is stored in the iOS Keychain on your device (this device only, available when the phone is unlocked). It is sent only to Anthropic, as the credential for these requests.

---

## 5A. Ground Station Link (Optional)

The Ground tab can connect to a ground station, which is a computer you run yourself. This is off until you type a ground station address and key in Settings and switch the link on.

- The app connects to the address you enter and to no other ground station address. It does not search for stations, and it does not use Bluetooth or Bonjour discovery. The app has no direct connection to a drone.
- Over this link the app reads status from the ground station and sends commands that you tap (land commands, resume-search commands and detection profile choices). It does not send camera frames, person crops, the reference photo or your location.
- The address is saved on your device. The ground station key is stored in the iOS Keychain on your device (this device only, available when the phone is unlocked) and is sent to the ground station you entered so that it accepts the connection.
- If the address starts with `ws://` or `http://`, which is normal on a home or field network, the link is **not encrypted**. Anyone on that network could read the traffic, including the key. Use a network you trust. The app refuses a `ws://` or `http://` address unless the address it resolves to is a local network address (10.x, 172.16 to 172.31, 192.168.x, 127.x, 169.254.x, or the IPv6 loopback, unique-local and link-local ranges). A host name is looked up first and the app connects only to those of its addresses that are local (a name that publishes both a private and a public address is used by its private address; a name with no local address is refused). It never connects to the name itself, and it connects directly, not through a proxy, when a direct connection works. This check happens before the key or any request is sent. Away from your local network, use an encrypted `wss://` or `https://` address. A VPN also works if it gives the ground station an address in a private range; addresses in 100.64.0.0/10, which Tailscale uses, are refused for `ws://` and `http://`. An encrypted address is not restricted to the local network, and the app does not pin certificates: it trusts the certificates that iOS trusts. The app also declares the iOS local network permission and the App Transport Security setting `NSAllowsLocalNetworking`.
- What the ground station does with data is governed by whoever runs it. That is outside this policy and outside the app.

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

The only exceptions are the optional features you turn on or start yourself: cloud reasoning (Section 5), which transmits the reference photo, cropped person images and your typed description to Anthropic, the ground station link (Section 5A), which sends your commands to the address you entered, and the on-device model download from Hugging Face (Section 11), which sends no photos or session data.

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
- The app makes network connections in three cases, each started by you. Cloud reasoning, when you have turned it on, uses TLS encryption to Anthropic. The ground station link goes to the address you entered and is encrypted only if you entered a `wss://` or `https://` address; a `ws://` or `http://` link is not encrypted, and the app does not restrict it to your local network (Section 5A). The on-device model download is a user-started model download: when you tap the download button for the on-device Gemma model in Settings, the app downloads the model `mlx-community/gemma-2-2b-it-4bit` from Hugging Face (huggingface.co), a third party. No photo, crop, description or location is sent, but Hugging Face necessarily sees the phone's IP address and which model files are requested; what it does with that is governed by its own policy, which this project has not reviewed.
- API keys are kept in the iOS Keychain, not in app preferences, files or logs.

---

## 12. Changes to This Policy

We may update this privacy policy from time to time. Changes will be posted within the app and at the privacy policy URL provided during app store submission. Continued use of the app after changes constitutes acceptance of the updated policy.

---

## 13. Contact

For questions or concerns about this privacy policy or the app's data practices:

**Email:** krlehnardt@gmail.com
