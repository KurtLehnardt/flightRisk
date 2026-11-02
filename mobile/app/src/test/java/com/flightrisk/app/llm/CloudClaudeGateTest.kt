package com.flightrisk.app.llm

import com.flightrisk.app.ground.SecretName
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The cloud gate on [CloudClaudeLlmBackend], against a stub Messages API on
 * 127.0.0.1. No request reaches the internet; the only key is "test-key".
 */
class CloudClaudeGateTest {

    private val harnesses = mutableListOf<CloudHarness>()
    private val bitmap = fakeBitmap()

    private fun harness(
        prefs: Map<String, Any?> = emptyMap(),
        secrets: Map<String, String> = emptyMap(),
        localUsable: Boolean = false,
    ) = CloudHarness(prefs, secrets, localUsable = localUsable).also { harnesses += it }

    @After
    fun tearDown() = harnesses.forEach { it.close() }

    private suspend fun waitFor(what: String, condition: () -> Boolean) {
        withTimeout(5_000) { while (!condition()) delay(5) }
        assertTrue(what, condition())
    }

    // ---- Defaults ----

    @Test
    fun `default is off and a saved key alone sends nothing`() = runBlocking {
        val h = harness(secrets = mapOf(SecretName.CLAUDE_API_KEY.storageKey to TEST_CLAUDE_KEY))
        assertEquals(ReasoningProvider.OFF, h.selector.status.value.active)
        assertFalse(h.selector.status.value.isCloudActive)
        assertEquals(ReasoningReason.CLOUD_BLOCKED, h.selector.status.value.unavailable[ReasoningProvider.CLOUD_CLAUDE])
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, "red jacket"))
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.describeMatch(bitmap, "red jacket"))
        assertEquals(0, h.server.accepted)
    }

    @Test
    fun `old stored cloud choice with a saved key stays off`() = runBlocking {
        val h = harness(
            prefs = mapOf(ReasoningSettings.PROVIDER_KEY to "cloud_claude"),
            secrets = mapOf(SecretName.CLAUDE_API_KEY.storageKey to TEST_CLAUDE_KEY),
        )
        assertNotEquals(ReasoningProvider.CLOUD_CLAUDE, h.selector.status.value.active)
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, null))
        assertEquals(0, h.server.accepted)
    }

    // ---- Opted in ----

    @Test
    fun `opt-in plus key makes cloud active and sends crop, reference photo and description with the key`() = runBlocking {
        val h = harness()
        h.enableCloud()
        assertTrue(h.selector.status.value.isCloudActive)
        assertEquals(h.backend, h.selector.getActiveBackend())

        val result = h.backend.analyzeMatch(bitmap, bitmap, "red jacket, blue cap")
        assertTrue(result.isMatch)
        assertEquals("high", result.confidence)
        assertEquals(1, h.server.requests.size)
        val request = h.server.requests.single()
        assertEquals(TEST_CLAUDE_KEY, request.apiKey)
        assertTrue(request.body.contains("red jacket, blue cap"))
        assertEquals(2, Regex("\"type\":\"image\"").findAll(request.body).count())
    }

    @Test
    fun `pasted key is trimmed before it is saved and sent`() = runBlocking {
        val h = harness()
        h.settings.confirmCloudConsent(selectCloud = true)
        assertTrue(h.gate.saveKey("  $TEST_CLAUDE_KEY \n"))
        assertEquals(TEST_CLAUDE_KEY, h.storage.map[SecretName.CLAUDE_API_KEY.storageKey])
        h.selector.refreshWithKeyLookup()
        h.backend.describeMatch(bitmap, "red jacket")
        assertEquals(TEST_CLAUDE_KEY, h.server.requests.single().apiKey)
    }

    @Test
    fun `a key cannot be saved before opt-in`() {
        val h = harness()
        assertFalse(h.gate.saveKey(TEST_CLAUDE_KEY))
        assertFalse(h.storage.map.containsKey(SecretName.CLAUDE_API_KEY.storageKey))
    }

    // ---- Turning it off without any refresh ----

    @Test
    fun `opt-out with no refresh makes the next call send zero requests`() = runBlocking {
        val h = harness()
        h.enableCloud()
        h.settings.setCloudOptIn(false) // no selector refresh
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, "red jacket"))
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.describeMatch(bitmap, "red jacket"))
        assertEquals(0, h.server.accepted)
    }

    @Test
    fun `switching provider with no refresh makes the next call send zero requests`() = runBlocking {
        val h = harness()
        h.enableCloud()
        h.settings.select(ReasoningProvider.OFF)
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, null))
        assertEquals(0, h.server.accepted)
    }

    @Test
    fun `a request queued before opt-out never uploads`() = runBlocking {
        val h = harness()
        h.enableCloud()
        // The first gate check passes; the user opts out while the images are
        // being encoded, before the second check.
        h.duringEncode = { h.settings.setCloudOptIn(false) }
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, "red jacket"))
        assertEquals(0, h.server.accepted)
    }

    @Test
    fun `a key removed between the two checks is caught`() = runBlocking {
        val h = harness()
        h.enableCloud()
        h.duringEncode = { h.storage.map.remove(SecretName.CLAUDE_API_KEY.storageKey) }
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.describeMatch(bitmap, "red jacket"))
        assertEquals(0, h.server.accepted)
    }

    @Test
    fun `an opt-out during the keystore read closes the gate`() = runBlocking {
        val h = harness()
        h.enableCloud()
        h.storage.beforeRead = { h.settings.setCloudOptIn(false) }
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.describeMatch(bitmap, "red jacket"))
        assertEquals(0, h.server.accepted)
    }

    @Test
    fun `the selector never hands out the cloud backend after opt-out, even before a refresh`() {
        val h = harness(localUsable = true)
        h.enableCloud()
        assertEquals(h.backend, h.selector.getActiveBackend())
        h.settings.setCloudOptIn(false)
        val active = h.selector.getActiveBackend()
        assertEquals(h.local, active)
        assertFalse(h.selector.status.value.isCloudActive)
    }

    // ---- Cancelling in flight ----

    private suspend fun inFlightCancelledBy(revoke: (CloudHarness) -> Unit) {
        val h = harness()
        h.enableCloud()
        h.server.hold = true
        val result = withTimeout(10_000) {
            coroutineScope {
                val call = async(Dispatchers.IO) { h.backend.analyzeMatch(bitmap, bitmap, "red jacket") }
                waitFor("request reached the stub") { h.server.requests.size == 1 }
                assertEquals(1, h.backend.inFlightCount())
                revoke(h)
                call.await()
            }
        }
        assertEquals(CloudClaudeLlmBackend.BLOCKED, result)
        waitFor("stub saw the connection close") { h.server.clientClosed.get() == 1 }
        assertEquals(0, h.backend.inFlightCount())
        // And nothing more is sent afterwards.
        h.server.hold = false
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.describeMatch(bitmap, "red jacket"))
        assertEquals(1, h.server.requests.size)
    }

    @Test
    fun `in-flight request is cancelled on opt-out`() = runBlocking {
        inFlightCancelledBy { it.settings.setCloudOptIn(false) }
    }

    @Test
    fun `in-flight request is cancelled on key removal`() = runBlocking {
        inFlightCancelledBy { h ->
            assertTrue(h.gate.removeKey())
            assertFalse(h.storage.map.containsKey(SecretName.CLAUDE_API_KEY.storageKey))
        }
    }

    @Test
    fun `in-flight request is cancelled on provider switch`() = runBlocking {
        inFlightCancelledBy { it.settings.select(ReasoningProvider.LOCAL) }
    }

    // ---- Missing, empty or broken keys ----

    @Test
    fun `missing, empty or blank key never produces a request or an empty header`() = runBlocking {
        for (stored in listOf(null, "", "   ", "\n")) {
            val secrets = stored?.let { mapOf(SecretName.CLAUDE_API_KEY.storageKey to it) } ?: emptyMap()
            val h = harness(prefs = mapOf(
                ReasoningSettings.CLOUD_OPT_IN_KEY to true,
                ReasoningSettings.PROVIDER_KEY to "cloud_claude",
            ), secrets = secrets)
            assertFalse(h.selector.status.value.isCloudActive)
            assertEquals(ReasoningReason.CLAUDE_KEY_MISSING, h.selector.status.value.unavailable[ReasoningProvider.CLOUD_CLAUDE])
            assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, "x"))
            assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.describeMatch(bitmap, "x"))
            assertEquals(0, h.server.accepted)
        }
    }

    @Test
    fun `a key that a header cannot carry is refused without a request and without echoing it`() = runBlocking {
        val bad = "test-key\r\nX-Injected: 1"
        val h = harness(prefs = mapOf(
            ReasoningSettings.CLOUD_OPT_IN_KEY to true,
            ReasoningSettings.PROVIDER_KEY to "cloud_claude",
        ), secrets = mapOf(SecretName.CLAUDE_API_KEY.storageKey to bad))
        val result = h.backend.describeMatch(bitmap, "x")
        assertEquals("error", result.confidence)
        assertFalse(result.reasoning.contains("test-key"))
        assertEquals(0, h.server.accepted)
        assertTrue(h.logs.none { it.contains("test-key") })
    }

    @Test
    fun `secure storage failure keeps cloud off with its own reason`() = runBlocking {
        val h = harness(prefs = mapOf(
            ReasoningSettings.CLOUD_OPT_IN_KEY to true,
            ReasoningSettings.PROVIDER_KEY to "cloud_claude",
        ), secrets = mapOf(SecretName.CLAUDE_API_KEY.storageKey to TEST_CLAUDE_KEY))
        h.storage.unavailable = true
        h.selector.refreshWithKeyLookup()
        val status = h.selector.status.value
        assertEquals(ClaudeKeyState.UNAVAILABLE, status.keyState)
        assertEquals(ReasoningReason.SECURE_STORAGE_UNAVAILABLE, status.unavailable[ReasoningProvider.CLOUD_CLAUDE])
        assertFalse(status.isCloudActive)
        assertTrue(status.notice!!.contains(ReasoningReason.SECURE_STORAGE_UNAVAILABLE))
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, "x"))
        assertEquals(0, h.server.accepted)
    }

    @Test
    fun `offline keeps cloud off`() = runBlocking {
        val h = CloudHarness(online = { false }).also { harnesses += it }
        h.enableCloud()
        assertFalse(h.selector.status.value.isCloudActive)
        assertEquals(ReasoningReason.OFFLINE, h.selector.status.value.unavailable[ReasoningProvider.CLOUD_CLAUDE])
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.describeMatch(bitmap, "x"))
        assertEquals(0, h.server.accepted)
    }

    // ---- Fallback ----

    @Test
    fun `removing the key falls back to local with a visible notice, never to cloud`() {
        val h = harness(localUsable = true)
        h.enableCloud()
        assertTrue(h.selector.status.value.isCloudActive)
        assertTrue(h.gate.removeKey())
        h.selector.refresh()
        val status = h.selector.status.value
        assertEquals(ReasoningProvider.CLOUD_CLAUDE, status.selected)
        assertEquals(ReasoningProvider.LOCAL, status.active)
        assertTrue(status.notice!!.startsWith("Cloud Claude is unavailable"))
    }

    @Test
    fun `registration order is on-device first and cloud is never a fallback`() {
        val h = harness(localUsable = false)
        assertEquals(listOf("local_fake", CloudClaudeLlmBackend.NAME), h.selector.registeredBackendNames)
        h.settings.select(ReasoningProvider.LOCAL)
        h.storage.map[SecretName.CLAUDE_API_KEY.storageKey] = TEST_CLAUDE_KEY
        h.settings.setCloudOptIn(true)
        h.selector.refreshWithKeyLookup()
        // Local is selected but not ready: reasoning goes off, not to the cloud.
        assertEquals(ReasoningProvider.OFF, h.selector.status.value.active)
        assertTrue(h.selector.status.value.notice != null)
    }

    // ---- No key material anywhere ----

    @Test
    fun `no key material in any toString, status, log or result`() = runBlocking {
        val h = harness()
        h.enableCloud()
        h.backend.analyzeMatch(bitmap, bitmap, "red jacket")
        h.server.hold = true
        val job = async(Dispatchers.IO) { h.backend.describeMatch(bitmap, "red jacket") }
        waitFor("second request in flight") { h.server.requests.size == 2 }
        h.settings.setCloudOptIn(false)
        job.await()

        val dumps = listOf(
            h.backend.toString(), h.gate.toString(), h.selector.toString(), h.settings.toString(),
            h.selector.status.value.toString(), h.secretStore.toString(),
            ClaudeKeyLookup.Found(TEST_CLAUDE_KEY).toString(),
            ReasoningAction.SaveKey(TEST_CLAUDE_KEY).toString(),
            ReasoningUiState(status = h.selector.status.value).toString(),
        ) + h.logs
        for (dump in dumps) assertFalse("key leaked in: $dump", dump.contains(TEST_CLAUDE_KEY))
    }
}
