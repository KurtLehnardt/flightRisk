# FlightRisk Mobile -- Privacy Policy

**Effective Date:** September 4, 2026
**Last Updated:** September 4, 2026
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
- **Images are different from templates.** With the default settings, no image of any person leaves your device. Only if you select a cloud LLM backend and store an API key for it (Section 5) are images sent off the device: the reference photo you provided and the cropped image of each candidate person that is sent for analysis. Those images can show a face. See Section 5 for exactly what is sent and how to turn it off.

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
- Is **never transmitted** to any server, cloud service, or third party.
- Is **user-deletable** at any time from within the app settings.
- Is not backed up to iCloud or Google Drive unless you have enabled full-device backups through your operating system (which is outside the app's control).

---

## 5. Cloud LLM Calls (Optional)

FlightRisk supports an **optional** cloud-based LLM reasoning feature (Claude, OpenAI, or Gemini) that provides additional analysis of potential matches.

**When disabled (default):** No data is transmitted to any cloud service. All processing is entirely on-device. The feature stays disabled until you choose a cloud backend and store an API key for it; with no API key stored, the app falls back to the on-device model or to no reasoning at all.

**When enabled:**

- The app sends **images** to the configured LLM endpoint (for Claude, `https://api.anthropic.com/v1/messages`): for a photo comparison, the **reference photo** you provided plus the **cropped image of the candidate person**; for a description comparison, the cropped image alone together with your text description. Images are JPEG-encoded. These images can show a face.
- No face embeddings, appearance feature vectors, GPS coordinates, or other session data are included.
- You are warned and must explicitly opt in before this feature is activated.
- The data transmitted is subject to the privacy policy of the LLM provider you select (Anthropic, OpenAI, or Google).
- You can disable this feature at any time, which immediately stops all cloud transmission.

---

## 6. Location Data

- GPS coordinates are recorded **locally** when a match event is detected, so you can navigate back to the location where the match occurred.
- Location data is stored only in the local SQLite database on your device.
- Location data is **never transmitted** to any server, cloud service, or third party.
- You can delete all location data from within the app settings at any time.

---

## 7. Camera Usage

- The camera is used solely for real-time person detection and face matching to help locate a missing child.
- Camera frames are processed on-device in real time and are not recorded, stored, or transmitted, except for match thumbnails stored locally as described in Section 4 and the crops and reference photo sent when the optional cloud LLM feature is enabled (Section 5).

---

## 8. Data Sharing

**FlightRisk does not share any data with third parties.** Specifically:

- No biometric data, session data, location data, or camera data is sold, shared, rented, or disclosed to any third party.
- No analytics, advertising, or tracking SDKs are included in the app.
- No user accounts are created or required.

The only exception is the optional Cloud LLM feature (Section 5), which transmits the reference photo and cropped person images to the LLM provider you explicitly select and enable.

---

## 9. Children's Privacy (COPPA Compliance)

- FlightRisk is a tool designed **for parents and guardians**, not for children.
- The app does not target children as users.
- The app does not collect any personal information from children.
- No user accounts are created. No names, email addresses, or identifying information of any person (adult or child) are collected by the app.
- The reference photo used for matching is provided by the parent/guardian and processed on-device. It is not stored beyond the session and is not transmitted unless the optional Cloud LLM feature (Section 5) is enabled, in which case it is sent to the provider you selected.

---

## 10. Data Retention and Deletion

- **Biometric templates and embeddings:** Not retained. Ephemeral and session-scoped. Discarded when the session ends.
- **Session data (thumbnails, scores, locations):** Stored locally until the user deletes it. The user can delete all session data at any time from within the app.
- **App deletion:** Uninstalling the app removes all locally stored data.

---

## 11. Security

- All on-device data is protected by the operating system's native app sandboxing (iOS App Sandbox / Android app-private storage).
- No data is transmitted over the network unless the optional Cloud LLM feature is enabled, in which case transmissions use TLS encryption.

---

## 12. Changes to This Policy

We may update this privacy policy from time to time. Changes will be posted within the app and at the privacy policy URL provided during app store submission. Continued use of the app after changes constitutes acceptance of the updated policy.

---

## 13. Contact

For questions or concerns about this privacy policy or the app's data practices:

**Email:** krlehnardt@gmail.com
