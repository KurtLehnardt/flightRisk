# Android privacy and data-handling changes

This note describes what the Android privacy change altered, so the privacy documents can be updated from it. It describes the code in this branch. Nothing here was verified on a device or emulator.

## Recording

- Settings has a switch "Record search video" that applies from the next search. It is off by default and is stored in `flightrisk_privacy_local.xml`, which backup and device transfer exclude. An existing install reads false because the key did not exist before; no value is migrated to on.
- `MainActivity` creates a `SessionRecorder` only when the switch is on. With the switch off, no video file is written and no recordings folder is created by the recorder.
- Match pictures (the JPEG files in `files/snapshots`) are saved only while the recording switch is on. Nothing in the app reads `snapshotPath` or `cropPath` (no screen shows them), so skipping them does not affect any screen. The match rows themselves (scores, type, time) are still saved in the database.
- Videos and pictures that an older version already saved stay until the person uses the delete action.

## Backup

- `backup_rules.xml` and both sections of `data_extraction_rules.xml` (cloud backup and device transfer) now also exclude: the database `flightrisk_sessions.db` and its journal, wal and shm files, `files/recordings`, `files/snapshots`, `flightrisk_ground_settings.xml`, `flightrisk_prefs.xml` (the onboarding flag) and `flightrisk_privacy_local.xml` (the notice acknowledgement and the recording switch).
- A restored phone therefore shows the privacy notice again, repeats onboarding and does not record video until the person turns it on.

## Delete action

- Settings has "Delete saved sessions and recordings", with a confirmation dialog. It deletes all rows of the sessions, matches and match feedback tables in one transaction, and all files in `files/recordings` and `files/snapshots`, then shows how many records and files were deleted. It is disabled while a search is running.
- It does not delete the downloaded model, the saved keys or the settings.

## Target photo taken with the camera

- The cache file `flightrisk_target_*.jpg` is deleted right after it is decoded (or when the capture is cancelled), and any left over are deleted when the app process starts.

## Permissions and the notice

- The app shows the privacy notice first. The camera and location prompts are requested only after "I Understand", and onboarding starts after that. The acknowledgement is stored under a new key (`privacy_notice_acknowledged_v2`) in the excluded file, so people who used an older version see the corrected notice once.
- `CHANGE_WIFI_STATE` was removed from the manifest. The Tello code only reads the Wi-Fi name (`TelloWifiChecker`), which needs `ACCESS_WIFI_STATE`, and nothing in the code changes Wi-Fi state.
- The notice text now covers the ground station, the phone-as-edge mode, the Tello drone and the cloud opt-in, no longer says that images leave the phone only with cloud reasoning on, says that video is not recorded unless the person turns it on, and links to the privacy policy.

## Appearance embeddings on the edge link

- The phone-as-edge link can now carry an appearance embedding (`reid_embedding`) for each of the 8 most confident people in a frame, so that the ground station could match phone detections to the search target. No model that makes such embeddings ships with the app, so the app's provider (`NoEmbeddingProvider`) makes none and every `reid_embedding` is still null. So no embedding leaves the phone, and the disclosure, the privacy notice and the privacy policy stay true as written ("The ground station cannot yet match these detections"). The one other change to what the phone sends is the camera-view thumbnail, which now goes out after the ground asks for video (last point below).
- An embedding is a list of 512 numbers that describes how a person looks. It is treated like biometric data. It is computed only for a frame that the edge publisher accepts, which happens only while the ground station link is on (after the disclosure was accepted for that address, with the edge token for a non-loopback ground), the app is in view and a search is running. It is computed on the publisher's worker and kept in memory only for the message being written. It is never written to the database, a recording, a snapshot, a backup or a log, and never given to the cloud reasoning path; the only place it goes is the ground station's edge link. `EdgeEmbeddingAuditTest` checks the source for this.
- Only an embedding from the ground's own model (open_clip ViT-B-16 with the `laion2b_s34b_b88k` weights) is sent. The phone checks each vector (512 finite numbers, normalized to unit length) and sends null for one that fails.
- Before a model is wired in, three texts must be changed to say that appearance embeddings are sent to the ground station and that the ground can then match people: the Settings disclosure (`GroundLinkText.DISCLOSURE`), the privacy notice (`PrivacyNoticeText`) and `docs/privacy-policy.md`. The policy now says that face embeddings and appearance feature vectors never leave the phone under any setting, that no embeddings are transmitted (Sections 2 and 3, including the BIPA, GDPR and other-laws paragraphs) and that the ground station cannot yet match phone detections (Section 5B); all of that becomes false. `EdgeEmbeddingAuditTest` fails if a provider is wired in while the disclosure or the notice does not mention embeddings or while the policy still holds any of those sentences. `mobile/README.md` ("Appearance embeddings for the ground") names the model that is needed.
- The Settings link wrapper now passes the publisher's wishes to the camera pipeline: before this change it never asked for the whole camera view, so the thumbnail the disclosure describes was never sent even after the ground asked for video, while the Search screen said "Sending detections and camera view". Now the thumbnail is sent after the ground's `stream_video` request, as the disclosure, the notice and the contract say. Crops stay off.

## Unused target canon code and the database migration

A later change (branch `fix/android-tests-and-schema`) removed the unused target canon code and its table:

- `canon/TargetCanon.kt` (with `TargetVersionEntity` and `TargetVersionDao`) was deleted. Nothing called it. It could store a reference photo, as base64 text, in the `target_versions` table.
- The Room database version went from 1 to 2. The migration `FlightRiskMigrations.MIGRATION_1_2` runs one statement, ``DROP TABLE IF EXISTS `target_versions` ``, and nothing else. The sessions, matches and match feedback tables are identical in both versions, so the saved sessions, matches and feedback are kept on update. A `target_versions` row, if any existed, is deleted with the table.
- The destructive fallback (`fallbackToDestructiveMigration`) was removed, with no downgrade exception. When Room finds no migration path (a future version bump without a migration, or a database from a newer version after a sideloaded older build), it now refuses to open the database instead of erasing it, and the files stay on disk. The search then runs without saving a session and the delete action reports a failure; both callers already handle that error. Erasing the sessions without a word was judged worse than failing visibly, and an older build can only be installed over a newer one by hand (for example `adb install -d`), never through the Play Store.
- Room now exports its schema to `mobile/app/schemas` (`1.json` is the shipped version 1 schema; its identity hash `9347872e6a5de369480558b4daa31978` equals the one the version 1 build generated). `FlightRiskMigrationTest` checks that the migrations form one chain up to the current version, that a schema file exists for each version, that version 2 differs from version 1 only by the dropped table, that the migration runs only the drop, and that the database builder has no destructive fallback. It also runs the version 1 schema and the migration on a real SQLite engine and checks that the rows survive and that the result matches the version 2 schema. That engine is the desktop SQLite bundled with sqlite-jdbc on the JVM, not Android's SQLite, and Room's own migration and schema validation do not run there (Robolectric and `androidx.room:room-testing` are not in the build).
- `FlightRiskMigrationInstrumentedTest` (androidTest) runs the migration through Room itself, using the app's database builder, and checks that a database with no migration path is refused rather than erased. It compiles but has not been run: the androidTest APK cannot be built offline here because `org.hamcrest:hamcrest-library:1.3`, `org.hamcrest:hamcrest-integration:1.3` and `com.squareup:javawriter:2.1.1` (dependencies of espresso-core) are not in the Gradle cache.

## Not changed

- iOS was unchanged in this change. The iOS follow-ups (the cloud opt-in carried by device backup, and the unused `TargetStore.swift`) were done in a separate iOS change; see `docs/ios-ground-client.md`.
