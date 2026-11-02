package com.flightrisk.app.privacy

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PrivacyGateTest {

    @Test fun freshInstallShowsNoticeAndRequestsNoPermission() {
        val prefs = MemoryPrivacyPrefs()
        assertTrue(PrivacyNoticeGate.shouldShowNotice(prefs))
        assertFalse(PrivacyNoticeGate.mayRequestPermissions(prefs))
    }

    @Test fun acknowledgingOpensTheGate() {
        val prefs = MemoryPrivacyPrefs()
        PrivacyNoticeGate.acknowledge(prefs)
        assertFalse(PrivacyNoticeGate.shouldShowNotice(prefs))
        assertTrue(PrivacyNoticeGate.mayRequestPermissions(prefs))
    }

    @Test fun showingAndRequestingAreNeverBothTrue() {
        for (ack in listOf(false, true)) {
            val prefs = MemoryPrivacyPrefs(noticeAcknowledged = ack)
            assertTrue(PrivacyNoticeGate.shouldShowNotice(prefs) != PrivacyNoticeGate.mayRequestPermissions(prefs))
        }
    }

    @Test fun recordingIsOffByDefault() {
        assertFalse(RecordingPolicy.shouldRecord(MemoryPrivacyPrefs()))
        assertTrue(RecordingPolicy.shouldRecord(MemoryPrivacyPrefs(recordSearchVideo = true)))
    }

    @Test fun acknowledgingTheNoticeDoesNotTurnRecordingOn() {
        val prefs = MemoryPrivacyPrefs()
        PrivacyNoticeGate.acknowledge(prefs)
        assertFalse(RecordingPolicy.shouldRecord(prefs))
    }

    @Test fun snapshotsAreDroppedWhenNotRecording() {
        assertNull(RecordingPolicy.snapshotToSave(false, "crop"))
        assertEquals("crop", RecordingPolicy.snapshotToSave(true, "crop"))
        assertNull(RecordingPolicy.snapshotToSave<String>(true, null))
    }

    @Test fun preferenceKeysAreTheOnesTheBackupRulesExclude() {
        assertEquals("flightrisk_privacy_local", SharedPrefsPrivacyPrefs.FILE)
        // A new key, so a notice flag restored from an older backup does not count.
        assertEquals("privacy_notice_acknowledged_v2", SharedPrefsPrivacyPrefs.KEY_NOTICE)
        assertEquals("record_search_video", SharedPrefsPrivacyPrefs.KEY_RECORD)
    }

    @Test fun permissionRequestIsOnlyReachableAfterTheGate() {
        val src = java.io.File("src/main/java/com/flightrisk/app/MainActivity.kt").readText()
        // Definition + the gated call at startup + the call after acknowledgement.
        assertEquals(3, Regex("requestPermissionsIfNeeded\\(\\)").findAll(src).count())
        assertTrue(
            Regex("if \\(PrivacyNoticeGate\\.mayRequestPermissions\\(privacyPrefs\\)\\) \\{\\s*requestPermissionsIfNeeded\\(\\)\\s*\\}")
                .containsMatchIn(src),
        )
        assertTrue(
            Regex("PrivacyNoticeGate\\.acknowledge\\(privacyPrefs\\)\\s*noticeAcknowledged = true\\s*requestPermissionsIfNeeded\\(\\)")
                .containsMatchIn(src),
        )
    }

    @Test fun pipelineSavesMatchPicturesOnlyThroughThePolicy() {
        val src = java.io.File("src/main/java/com/flightrisk/app/pipeline/SearchPipeline.kt").readText()
        assertFalse(src.contains("sessionRepository.addMatch("))
        assertTrue(src.contains("recording = sessionRecorder != null"))
    }
}
