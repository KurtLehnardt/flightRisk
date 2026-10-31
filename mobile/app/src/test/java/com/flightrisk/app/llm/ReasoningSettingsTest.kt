package com.flightrisk.app.llm

import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.ground.SecretStore
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ReasoningSettingsTest {

    private fun settings(vararg values: Pair<String, Any?>) = FakePrefs(mapOf(*values)).let { it to ReasoningSettings(it) }

    // ---- Defaults ----

    @Test
    fun `defaults are off with cloud never selected`() {
        val (_, s) = settings()
        assertFalse(s.cloudOptIn)
        assertNull(s.storedProvider)
        assertEquals(ReasoningProvider.OFF, s.selectedProvider(localModelAvailable = false))
        assertEquals(ReasoningProvider.LOCAL, s.selectedProvider(localModelAvailable = true))
        assertFalse(s.cloudAllowedBySettings)
    }

    @Test
    fun `old stored cloud choice without opt-in is ignored and removed`() {
        for (legacy in listOf("cloud_claude", "claude", " Claude ")) {
            val (prefs, s) = settings(ReasoningSettings.PROVIDER_KEY to legacy)
            assertNull(s.storedProvider)
            assertEquals(ReasoningProvider.OFF, s.selectedProvider(false))
            s.migrateLegacyValues()
            assertFalse(prefs.map.containsKey(ReasoningSettings.PROVIDER_KEY))
            assertFalse(s.cloudOptIn)
        }
    }

    @Test
    fun `only a stored Boolean true counts as opt-in`() {
        for (value in listOf<Any>("true", "1", 1, "YES")) {
            val (prefs, s) = settings(
                ReasoningSettings.CLOUD_OPT_IN_KEY to value,
                ReasoningSettings.PROVIDER_KEY to "cloud_claude",
            )
            assertFalse("value $value must not opt in", s.cloudOptIn)
            assertNull(s.storedProvider)
            s.migrateLegacyValues()
            assertFalse(prefs.map.containsKey(ReasoningSettings.CLOUD_OPT_IN_KEY))
            assertFalse(prefs.map.containsKey(ReasoningSettings.PROVIDER_KEY))
        }
        val (_, ok) = settings(ReasoningSettings.CLOUD_OPT_IN_KEY to true)
        assertTrue(ok.cloudOptIn)
    }

    @Test
    fun `unknown Gemini and OpenAI provider values are removed and other spellings normalised`() {
        for (legacy in listOf("cloud_gemini", "cloud_openai", "gemini")) {
            val (prefs, s) = settings(ReasoningSettings.PROVIDER_KEY to legacy)
            s.migrateLegacyValues()
            assertFalse(prefs.map.containsKey(ReasoningSettings.PROVIDER_KEY))
        }
        val (prefs, s) = settings(ReasoningSettings.PROVIDER_KEY to "gemma")
        s.migrateLegacyValues()
        assertEquals("local", prefs.map[ReasoningSettings.PROVIDER_KEY])
        assertEquals(ReasoningProvider.LOCAL, s.storedProvider)
    }

    // ---- Selection and consent decisions ----

    @Test
    fun `selecting cloud is refused before opt-in`() {
        val (prefs, s) = settings()
        assertFalse(s.select(ReasoningProvider.CLOUD_CLAUDE))
        assertFalse(prefs.map.containsKey(ReasoningSettings.PROVIDER_KEY))
    }

    @Test
    fun `picking cloud before opt-in asks for consent and selects nothing`() {
        val (_, s) = settings()
        assertEquals(ConsentDecision.AskForConsent(selectCloud = true), s.requestProvider(ReasoningProvider.CLOUD_CLAUDE))
        assertNull(s.storedProvider)
        assertFalse(s.cloudOptIn)
    }

    @Test
    fun `toggle on asks for consent, confirm opts in, toggle off applies at once and clears cloud`() {
        val (prefs, s) = settings()
        assertEquals(ConsentDecision.AskForConsent(selectCloud = false), s.requestCloudOptIn(true))
        assertFalse(s.cloudOptIn)
        s.confirmCloudConsent(selectCloud = false)
        assertTrue(s.cloudOptIn)
        assertNull(s.storedProvider)
        assertEquals(ConsentDecision.Applied, s.requestCloudOptIn(true))

        assertEquals(ConsentDecision.Applied, s.requestProvider(ReasoningProvider.CLOUD_CLAUDE))
        assertEquals(ReasoningProvider.CLOUD_CLAUDE, s.storedProvider)

        assertEquals(ConsentDecision.Applied, s.requestCloudOptIn(false))
        assertFalse(s.cloudOptIn)
        assertEquals(false, prefs.map[ReasoningSettings.CLOUD_OPT_IN_KEY])
        assertFalse(prefs.map.containsKey(ReasoningSettings.PROVIDER_KEY))
    }

    @Test
    fun `confirming consent from the picker opts in and selects cloud`() {
        val (_, s) = settings()
        s.confirmCloudConsent(selectCloud = true)
        assertTrue(s.cloudOptIn)
        assertEquals(ReasoningProvider.CLOUD_CLAUDE, s.storedProvider)
        assertTrue(s.cloudAllowedBySettings)
    }

    @Test
    fun `local and off are applied without consent`() {
        val (_, s) = settings()
        assertEquals(ConsentDecision.Applied, s.requestProvider(ReasoningProvider.LOCAL))
        assertEquals(ReasoningProvider.LOCAL, s.storedProvider)
        assertEquals(ConsentDecision.Applied, s.requestProvider(ReasoningProvider.OFF))
        assertEquals(ReasoningProvider.OFF, s.storedProvider)
    }

    @Test
    fun `gate listeners hear opt-out and provider switch synchronously`() {
        val (_, s) = settings()
        s.confirmCloudConsent(selectCloud = true)
        val heard = mutableListOf<Boolean>()
        s.addGateListener { heard += it }
        s.select(ReasoningProvider.LOCAL)
        s.setCloudOptIn(false)
        assertEquals(listOf(false, false), heard)
    }

    @Test
    fun `pasted keys are trimmed and blank input is no key`() {
        assertEquals(TEST_CLAUDE_KEY, ReasoningSettings.normalizedKey("  $TEST_CLAUDE_KEY\n"))
        assertNull(ReasoningSettings.normalizedKey(" \t\n"))
        assertNull(ReasoningSettings.normalizedKey(null))
    }

    @Test
    fun `consent text names the crop, the reference photo, the description, a face and turning off`() {
        val text = CloudConsentText.MESSAGE
        for (phrase in listOf(
            "cropped image of each detected person", "reference photo", "text description you entered",
            "Anthropic", "face", "turn this off at any time", "cancels any request still in progress",
        )) {
            assertTrue("consent text must mention '$phrase'", text.contains(phrase))
        }
    }

    // ---- Key migration ----

    @Test
    fun `key migration moves the encrypted legacy key once and never enables the opt-in`() {
        val prefs = FakePrefs()
        val storage = FakeSecretStorage(mapOf(SecretStore.LEGACY_CLAUDE_KEY to TEST_CLAUDE_KEY))
        val secrets = SecretStore(storage, isMainThread = { false })
        val s = ReasoningSettings(prefs)

        assertTrue(s.migrateLegacyKeyOnce(secrets))
        assertEquals(TEST_CLAUDE_KEY, storage.map[SecretName.CLAUDE_API_KEY.storageKey])
        assertFalse(storage.map.containsKey(SecretStore.LEGACY_CLAUDE_KEY))
        assertFalse(s.cloudOptIn)
        assertNull(s.storedProvider)
        assertEquals(true, prefs.map[ReasoningSettings.KEY_MIGRATION_DONE_KEY])

        // A second run does nothing, even if something writes the old name again.
        storage.map[SecretStore.LEGACY_CLAUDE_KEY] = "other-test-key"
        val readsBefore = storage.reads.get()
        assertTrue(s.migrateLegacyKeyOnce(secrets))
        assertEquals(readsBefore, storage.reads.get())
        assertEquals(TEST_CLAUDE_KEY, storage.map[SecretName.CLAUDE_API_KEY.storageKey])
        assertFalse(s.cloudOptIn)
    }

    @Test
    fun `key migration moves a plain-preferences key from the oldest builds, writing new before deleting old`() {
        val prefs = FakePrefs(mapOf(SecretStore.LEGACY_CLAUDE_KEY to " $TEST_CLAUDE_KEY "))
        val storage = FakeSecretStorage()
        val s = ReasoningSettings(prefs)
        assertTrue(s.migrateLegacyKeyOnce(SecretStore(storage, isMainThread = { false })))
        assertEquals(TEST_CLAUDE_KEY, storage.map[SecretName.CLAUDE_API_KEY.storageKey])
        assertFalse(prefs.map.containsKey(SecretStore.LEGACY_CLAUDE_KEY))
        assertFalse(s.cloudOptIn)
    }

    @Test
    fun `failed key migration keeps the old key and is retried`() {
        val prefs = FakePrefs(mapOf(SecretStore.LEGACY_CLAUDE_KEY to TEST_CLAUDE_KEY))
        val storage = FakeSecretStorage().apply { failWrites = true }
        val secrets = SecretStore(storage, isMainThread = { false })
        val s = ReasoningSettings(prefs)
        assertFalse(s.migrateLegacyKeyOnce(secrets))
        assertEquals(TEST_CLAUDE_KEY, prefs.map[SecretStore.LEGACY_CLAUDE_KEY])
        assertFalse(prefs.map.containsKey(ReasoningSettings.KEY_MIGRATION_DONE_KEY))

        storage.failWrites = false
        assertTrue(s.migrateLegacyKeyOnce(secrets))
        assertEquals(TEST_CLAUDE_KEY, storage.map[SecretName.CLAUDE_API_KEY.storageKey])
        assertFalse(prefs.map.containsKey(SecretStore.LEGACY_CLAUDE_KEY))
        assertFalse(s.cloudOptIn)
    }

    @Test
    fun `settings toString holds no key`() {
        val (_, s) = settings(SecretStore.LEGACY_CLAUDE_KEY to TEST_CLAUDE_KEY)
        assertFalse(s.toString().contains(TEST_CLAUDE_KEY))
    }
}
