package com.flightrisk.app.privacy

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PrivacyNoticeTextTest {
    private val text = PrivacyNoticeText.allText()

    @Test fun noStaleClaimThatImagesLeaveOnlyWithCloudReasoning() {
        assertFalse(text.contains("only with cloud reasoning", ignoreCase = true))
        assertFalse(text.contains("Images leave the phone", ignoreCase = true))
    }

    @Test fun mentionsEveryWayDataCanLeaveThePhone() {
        assertTrue(text.contains("Ground Station"))
        assertTrue(text.contains("Phone as an Edge"))
        assertTrue(text.contains("Tello"))
        assertTrue(text.contains("Cloud Reasoning (Off by Default)"))
        assertTrue(text.contains("sent to Anthropic"))
        assertTrue(text.contains("small picture of the whole camera view"))
    }

    @Test fun saysVideoIsNotRecordedUnlessTurnedOn() {
        assertTrue(text.contains("not recorded unless you turn on Record search video"))
    }

    @Test fun linksToThePrivacyPolicy() {
        assertTrue(PrivacyNoticeText.POLICY_URL.endsWith("docs/privacy-policy.md"))
        assertTrue(PrivacyNoticeText.POLICY_URL.startsWith("https://"))
    }

    @Test fun recordingExplanationSaysWhatWhereBackupAndDelete() {
        val e = PrivacySettingsText.RECORD_EXPLANATION
        assertTrue(e.contains("video"))
        assertTrue(e.contains("private storage on this phone"))
        assertTrue(e.contains("backup does not include"))
        assertTrue(e.contains(PrivacySettingsText.DELETE_TITLE))
        assertTrue(e.contains("off by default"))
    }

    @Test fun noticeNamesTheConditionsOfTheOptionalCases() {
        assertTrue(text.contains("except in the optional cases described below"))
        assertTrue(text.contains("does not join it"))
        assertTrue(text.contains("edge token is sent to the address you entered"))
    }

    @Test fun deleteResultMessages() {
        assertTrue(PrivacySettingsText.deleteResult(SavedDataEraser.Result(2, 5, 0)).contains("2 sessions and 5 files"))
        assertTrue(PrivacySettingsText.deleteResult(SavedDataEraser.Result(0, 1, 3)).contains("3 files could not be deleted"))
    }
}
