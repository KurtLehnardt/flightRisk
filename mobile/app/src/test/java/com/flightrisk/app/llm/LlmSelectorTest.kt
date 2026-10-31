package com.flightrisk.app.llm

import com.flightrisk.app.ground.SecretName
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The real [LlmSelector], with a fake on-device backend, the gated cloud
 * backend and in-memory settings and secrets. The selector follows the user's
 * selection; cloud is never preferred, never the default and never a fallback.
 */
class LlmSelectorTest {

    private val harnesses = mutableListOf<CloudHarness>()

    private fun harness(localUsable: Boolean = false, withKey: Boolean = false) = CloudHarness(
        secrets = if (withKey) mapOf(SecretName.CLAUDE_API_KEY.storageKey to TEST_CLAUDE_KEY) else emptyMap(),
        localUsable = localUsable,
    ).also { harnesses += it }

    @After
    fun tearDown() = harnesses.forEach { it.close() }

    @Test
    fun `falls back to NoOp when nothing is usable`() {
        val h = harness()
        assertEquals("none", h.selector.getActiveBackend().name)
        assertFalse(h.selector.isLlmAvailable)
    }

    @Test
    fun `defaults to the on-device backend when it is usable, even with a saved key`() {
        val h = harness(localUsable = true, withKey = true)
        assertEquals(h.local, h.selector.getActiveBackend())
        assertEquals(ReasoningProvider.LOCAL, h.selector.status.value.selected)
        assertTrue(h.selector.isLlmAvailable)
    }

    @Test
    fun `cloud is used only when opted in, selected and a key is saved`() {
        val h = harness(localUsable = true, withKey = true)
        h.settings.setCloudOptIn(true)
        h.selector.refresh()
        assertEquals(h.local, h.selector.getActiveBackend())
        h.settings.select(ReasoningProvider.CLOUD_CLAUDE)
        h.selector.refresh()
        assertEquals(h.backend, h.selector.getActiveBackend())
    }

    @Test
    fun `selecting off uses no backend even when others are usable`() {
        val h = harness(localUsable = true)
        h.settings.select(ReasoningProvider.OFF)
        h.selector.refresh()
        assertEquals("none", h.selector.getActiveBackend().name)
    }

    @Test
    fun `refresh follows the on-device model becoming usable`() {
        val h = harness(localUsable = false)
        assertEquals("none", h.selector.getActiveBackend().name)
        h.local.usable = true
        h.selector.refresh()
        assertEquals(h.local, h.selector.getActiveBackend())
    }

    @Test
    fun `clear resets to NoOp`() {
        val h = harness(localUsable = true)
        h.selector.clear()
        assertEquals("none", h.selector.getActiveBackend().name)
        assertTrue(h.selector.registeredBackendNames.isEmpty())
    }
}
