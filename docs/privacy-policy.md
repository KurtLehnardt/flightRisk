# FlightRisk Mobile -- Privacy Policy

**Effective Date:** September 4, 2026
**Last Updated:** September 4, 2026
**Contact:** krlehnardt@gmail.com

---

## 1. Overview

FlightRisk is an AI-powered tool designed to help parents and guardians locate a missing child in crowded environments. It uses on-device face recognition and appearance matching via the phone camera. This privacy policy explains what data the app processes, how it is handled, and your rights.

---

## 2. On-Device Processing

All face detection, face recognition (ArcFace), and appearance matching (CLIP ReID) processing runs entirely on your device using ONNX Runtime and/or CoreML. **No biometric data ever leaves your phone.** Specifically:

- Face embeddings (numerical representations of facial features) are computed locally and exist only in device memory during the active session.
- Body detection and appearance feature extraction happen locally.
- No biometric templates, face embeddings, or detection crops are transmitted to any server, cloud service, or third party.

---

## 3. Biometric Data Handling

### Ephemeral Processing

All biometric templates (face embeddings and appearance features) are **session-scoped and ephemeral**:

- They are created in memory when you start a search session.
- They are discarded when the session ends or the app is closed.
- They are never written to persistent storage, transmitted over the network, or shared with any party.

### Regulatory Compliance

- **BIPA (Illinois Biometric Information Privacy Act):** No biometric identifiers or biometric information is collected, stored, or transmitted. All biometric processing is ephemeral and on-device. No consent for biometric storage is required because nothing is stored beyond the active session.
- **GDPR (EU General Data Protection Regulation):** No personal biometric data is processed in a manner that requires a lawful basis for storage or transfer, as all processing is ephemeral, local, and under the sole control of the user. No data is transmitted to any data controller or processor.
- **Other biometric privacy laws:** The same ephemeral, on-device-only architecture applies regardless of jurisdiction.

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

**When disabled (default):** No data is transmitted to any cloud service. All processing is entirely on-device.

**When enabled:**

- Only **cropped person images** are transmitted to the configured LLM endpoint. No face embeddings, biometric templates, GPS coordinates, or other session data are included.
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
- Camera frames are processed on-device in real time and are not recorded, stored, or transmitted (except for match thumbnails stored locally as described in Section 4).

---

## 8. Data Sharing

**FlightRisk does not share any data with third parties.** Specifically:

- No biometric data, session data, location data, or camera data is sold, shared, rented, or disclosed to any third party.
- No analytics, advertising, or tracking SDKs are included in the app.
- No user accounts are created or required.

The only exception is the optional Cloud LLM feature (Section 5), which transmits cropped images to the LLM provider you explicitly select and enable.

---

## 9. Children's Privacy (COPPA Compliance)

- FlightRisk is a tool designed **for parents and guardians**, not for children.
- The app does not target children as users.
- The app does not collect any personal information from children.
- No user accounts are created. No names, email addresses, or identifying information of any person (adult or child) are collected by the app.
- The reference photo used for matching is provided by the parent/guardian, processed entirely on-device, and never transmitted or stored beyond the session.

---

## 10. Data Retention and Deletion

- **Biometric data:** Not retained. Ephemeral and session-scoped. Discarded when the session ends.
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
