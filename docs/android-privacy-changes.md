# Android privacy and data-handling changes

This note describes what the Android privacy change altered, so the privacy documents can be updated from it. It describes the code in this branch. Nothing here was verified on a device or emulator.

## Recording

- Settings has a switch "Record search video". It is off by default and is stored in `flightrisk_privacy_local.xml`, which backup and device transfer exclude. An existing install reads false because the key did not exist before; no value is migrated to on.
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

## Not changed

- `canon/TargetCanon.kt` and the `target_versions` table remain. `FlightRiskDatabase` lists `TargetVersionEntity` as an entity, so removing the table changes the Room schema and needs a database version bump (the database uses `fallbackToDestructiveMigration`, so a bump would erase existing saved sessions). That is a decision for a separate change.
- iOS is unchanged. Open iOS follow-ups: the cloud opt-in carried by device backup, and the unused `TargetStore.swift`.
