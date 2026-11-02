package com.flightrisk.app.privacy

import org.junit.Assert.assertTrue
import org.junit.Test

class BackupRulesTest {

    private val required = listOf(
        ExcludeRule("database", "flightrisk_sessions.db"),
        ExcludeRule("database", "flightrisk_sessions.db-journal"),
        ExcludeRule("database", "flightrisk_sessions.db-wal"),
        ExcludeRule("database", "flightrisk_sessions.db-shm"),
        ExcludeRule("file", "recordings"),
        ExcludeRule("file", "snapshots"),
        ExcludeRule("sharedpref", "flightrisk_ground_settings.xml"),
        ExcludeRule("sharedpref", "flightrisk_prefs.xml"),
        ExcludeRule("sharedpref", SharedPrefsPrivacyPrefs.FILE + ".xml"),
        // Excluded before this change; they must stay excluded.
        ExcludeRule("sharedpref", "flightrisk_secure_prefs.xml"),
        ExcludeRule("sharedpref", "flightrisk_reasoning.xml"),
        ExcludeRule("sharedpref", "flightrisk_ground_link.xml"),
        ExcludeRule("sharedpref", "flightrisk_config.xml"),
    )

    private fun assertAll(where: String, actual: List<ExcludeRule>) {
        for (rule in required) {
            assertTrue("$where must exclude $rule but has $actual", rule in actual)
        }
    }

    @Test fun fullBackupContentExcludesPrivateData() {
        assertAll("backup_rules.xml", XmlRules.excludes(XmlRules.file("res/xml/backup_rules.xml")))
    }

    @Test fun cloudBackupExcludesPrivateData() {
        assertAll(
            "cloud-backup",
            XmlRules.excludes(XmlRules.file("res/xml/data_extraction_rules.xml"), "cloud-backup"),
        )
    }

    @Test fun deviceTransferExcludesPrivateData() {
        assertAll(
            "device-transfer",
            XmlRules.excludes(XmlRules.file("res/xml/data_extraction_rules.xml"), "device-transfer"),
        )
    }

    @Test fun databaseNameInRulesMatchesTheCode() {
        val src = java.io.File("src/main/java/com/flightrisk/app/persistence/FlightRiskDatabase.kt").readText()
        assertTrue(src.contains("\"flightrisk_sessions.db\""))
    }

    @Test fun recordingAndSnapshotFoldersMatchTheCode() {
        val repo = java.io.File("src/main/java/com/flightrisk/app/persistence/SessionRepository.kt").readText()
        val ui = java.io.File("src/main/java/com/flightrisk/app/MainActivity.kt").readText()
        assertTrue(repo.contains("File(context.filesDir, \"snapshots\")"))
        assertTrue(ui.contains("java.io.File(ctx.filesDir, \"recordings\")"))
    }

    @Test fun groundSettingsFileNameMatchesTheCode() {
        val src = java.io.File("src/main/java/com/flightrisk/app/ui/ground/GroundSettingsStore.kt").readText()
        assertTrue(src.contains("\"flightrisk_ground_settings\""))
    }
}
