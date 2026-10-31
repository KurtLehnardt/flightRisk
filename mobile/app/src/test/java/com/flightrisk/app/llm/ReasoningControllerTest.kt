package com.flightrisk.app.llm

import com.flightrisk.app.ground.SecretName
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The consent and key flow behind the Settings reasoning section. */
@OptIn(ExperimentalCoroutinesApi::class)
class ReasoningControllerTest {

    private val h = CloudHarness(localUsable = false)

    @After
    fun tearDown() = h.close()

    private fun kotlinx.coroutines.test.TestScope.controller(): ReasoningController {
        val dispatcher = UnconfinedTestDispatcher(testScheduler)
        return ReasoningController(h.settings, h.gate, h.selector, backgroundScope, io = dispatcher)
    }

    @Test
    fun `initial state is off with no key field and no consent`() = runTest(UnconfinedTestDispatcher()) {
        val c = controller()
        val ui = c.ui.value
        assertEquals(ReasoningProvider.OFF, ui.selected)
        assertFalse(ui.cloudOptIn)
        assertFalse(ui.showKeyField)
        assertFalse(ui.showRemoveKey)
        assertNull(ui.consent)
        assertFalse(ui.isCloudActive)
    }

    @Test
    fun `picking Cloud Claude before opt-in opens consent and selects nothing until confirmed`() = runTest(UnconfinedTestDispatcher()) {
        val c = controller()
        c.onAction(ReasoningAction.PickProvider(ReasoningProvider.CLOUD_CLAUDE))
        assertEquals(ConsentRequest(selectCloud = true), c.ui.value.consent)
        assertFalse(h.settings.cloudOptIn)
        assertEquals(ReasoningProvider.OFF, c.ui.value.selected)

        c.onAction(ReasoningAction.ConfirmConsent)
        assertNull(c.ui.value.consent)
        assertTrue(c.ui.value.cloudOptIn)
        assertEquals(ReasoningProvider.CLOUD_CLAUDE, c.ui.value.selected)
        assertTrue(c.ui.value.showKeyField)
        // No key yet: cloud is selected but not active, with a visible notice.
        assertFalse(c.ui.value.isCloudActive)
        assertTrue(c.ui.value.status.notice!!.contains(ReasoningReason.CLAUDE_KEY_MISSING))
    }

    @Test
    fun `dismissing consent changes nothing`() = runTest(UnconfinedTestDispatcher()) {
        val c = controller()
        c.onAction(ReasoningAction.SetCloudOptIn(true))
        assertEquals(ConsentRequest(selectCloud = false), c.ui.value.consent)
        c.onAction(ReasoningAction.DismissConsent)
        assertNull(c.ui.value.consent)
        assertFalse(h.settings.cloudOptIn)
        assertFalse(c.ui.value.showKeyField)
    }

    @Test
    fun `saving a key before opt-in is ignored`() = runTest(UnconfinedTestDispatcher()) {
        val c = controller()
        c.onAction(ReasoningAction.SaveKey(TEST_CLAUDE_KEY))
        assertFalse(h.storage.map.containsKey(SecretName.CLAUDE_API_KEY.storageKey))
    }

    @Test
    fun `opt in, save key, cloud active, then opt out turns it off at once`() = runTest(UnconfinedTestDispatcher()) {
        val c = controller()
        c.onAction(ReasoningAction.PickProvider(ReasoningProvider.CLOUD_CLAUDE))
        c.onAction(ReasoningAction.ConfirmConsent)
        c.onAction(ReasoningAction.SaveKey("  $TEST_CLAUDE_KEY  "))
        assertEquals(TEST_CLAUDE_KEY, h.storage.map[SecretName.CLAUDE_API_KEY.storageKey])
        assertTrue(c.ui.value.isCloudActive)
        assertTrue(c.ui.value.hasSavedKey)
        assertEquals("Claude API key saved.", c.ui.value.message)

        c.onAction(ReasoningAction.SetCloudOptIn(false))
        assertFalse(c.ui.value.isCloudActive)
        assertFalse(c.ui.value.cloudOptIn)
        assertFalse(c.ui.value.showKeyField)
        // The key is kept, and it can still be removed while opted out.
        assertTrue(c.ui.value.showRemoveKey)
    }

    @Test
    fun `remove saved key works while opted out and is reported`() = runTest(UnconfinedTestDispatcher()) {
        h.storage.map[SecretName.CLAUDE_API_KEY.storageKey] = TEST_CLAUDE_KEY
        h.selector.refreshWithKeyLookup()
        val c = controller()
        assertFalse(c.ui.value.cloudOptIn)
        assertTrue(c.ui.value.showRemoveKey)
        c.onAction(ReasoningAction.RemoveKey)
        assertFalse(h.storage.map.containsKey(SecretName.CLAUDE_API_KEY.storageKey))
        assertFalse(c.ui.value.showRemoveKey)
        assertEquals("Saved Claude API key removed.", c.ui.value.message)
    }

    @Test
    fun `secure storage failure is reported distinctly and cloud stays off`() = runTest(UnconfinedTestDispatcher()) {
        val c = controller()
        c.onAction(ReasoningAction.PickProvider(ReasoningProvider.CLOUD_CLAUDE))
        c.onAction(ReasoningAction.ConfirmConsent)
        h.storage.unavailable = true
        c.onAction(ReasoningAction.SaveKey(TEST_CLAUDE_KEY))
        assertTrue(c.ui.value.secureStorageUnavailable)
        assertTrue(c.ui.value.message!!.startsWith(ReasoningReason.SECURE_STORAGE_UNAVAILABLE))
        assertFalse(c.ui.value.isCloudActive)
        assertFalse(c.ui.value.hasSavedKey)
    }

    @Test
    fun `start runs the migration once off the given dispatcher and never opts in`() = runTest(UnconfinedTestDispatcher()) {
        h.storage.map["FLIGHTRISK_API_KEY"] = TEST_CLAUDE_KEY
        val c = controller()
        c.start { h.settings.migrateLegacyKeyOnce(h.secretStore) }
        assertEquals(TEST_CLAUDE_KEY, h.storage.map[SecretName.CLAUDE_API_KEY.storageKey])
        assertTrue(c.ui.value.hasSavedKey)
        assertFalse(c.ui.value.cloudOptIn)
        assertFalse(c.ui.value.isCloudActive)
        assertTrue(c.ui.value.showRemoveKey)
    }
}
