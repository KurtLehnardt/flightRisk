package com.flightrisk.app.llm

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The Messages API request the cloud backend sends and how it reads the
 * response, against a stub server on 127.0.0.1. The request must follow the
 * rules for the model it names (Claude Sonnet 5.5).
 */
class CloudClaudeRequestTest {

    private val harnesses = mutableListOf<CloudHarness>()
    private val bitmap = fakeBitmap()

    private fun enabledHarness(rawResponse: String? = null) = CloudHarness().also {
        harnesses += it
        it.server.rawResponse = rawResponse
        it.enableCloud()
    }

    @After
    fun tearDown() = harnesses.forEach { it.close() }

    private fun http200(json: String): String {
        val bytes = json.toByteArray()
        return "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\n" +
            "Connection: close\r\n\r\n$json"
    }

    private fun sentBody(h: CloudHarness): JsonObject = Json.parseToJsonElement(h.server.requests.single().body).jsonObject

    @Test
    fun `the default model is Claude Sonnet 5_5 and the deprecated model is gone`() {
        assertEquals("claude-sonnet-5-5", CloudClaudeLlmBackend.DEFAULT_MODEL)
        assertFalse(CloudClaudeLlmBackend.DEFAULT_MODEL.contains("20250514"))
    }

    @Test
    fun `the request names the default model and follows the Sonnet 5_5 request rules`() = runBlocking {
        val h = enabledHarness()
        h.backend.analyzeMatch(bitmap, bitmap, "red jacket")
        val body = sentBody(h)

        assertEquals(CloudClaudeLlmBackend.DEFAULT_MODEL, body.getValue("model").jsonPrimitive.content)
        assertEquals(CloudClaudeLlmBackend.MAX_TOKENS, body.getValue("max_tokens").jsonPrimitive.int)
        assertEquals("high", body.getValue("output_config").jsonObject.getValue("effort").jsonPrimitive.content)
        // Sampling parameters are rejected on this model, and "thinking": {"type": "disabled"} is a 400.
        for (field in listOf("temperature", "top_p", "top_k", "thinking", "tool_choice", "stop_sequences")) {
            assertNull("unexpected field $field", body[field])
        }
        // One user turn: no assistant prefill.
        val messages = body.getValue("messages").jsonArray
        assertEquals(1, messages.size)
        val turn = messages.single().jsonObject
        assertEquals("user", turn.getValue("role").jsonPrimitive.content)
        val types = turn.getValue("content").jsonArray.map { it.jsonObject.getValue("type").jsonPrimitive.content }
        assertEquals(listOf("image", "image", "text"), types)
    }

    @Test
    fun `the describe request has the same shape with one image`() = runBlocking {
        val h = enabledHarness()
        h.backend.describeMatch(bitmap, "blue cap")
        val body = sentBody(h)
        assertEquals(CloudClaudeLlmBackend.DEFAULT_MODEL, body.getValue("model").jsonPrimitive.content)
        val content = body.getValue("messages").jsonArray.single().jsonObject.getValue("content").jsonArray
        assertEquals(listOf("image", "text"), content.map { it.jsonObject.getValue("type").jsonPrimitive.content })
    }

    @Test
    fun `the key is sent only in x-api-key and never in the body or the log`() = runBlocking {
        val h = enabledHarness()
        h.backend.analyzeMatch(bitmap, bitmap, null)
        val request = h.server.requests.single()
        assertEquals(TEST_CLAUDE_KEY, request.apiKey)
        assertFalse(request.body.contains(TEST_CLAUDE_KEY))
        assertTrue(h.logs.none { it.contains(TEST_CLAUDE_KEY) })
    }

    @Test
    fun `a thinking block before the answer is skipped by type`() = runBlocking {
        val h = enabledHarness(
            http200(
                """{"stop_reason":"end_turn","content":[{"type":"thinking","thinking":"","signature":"sig"},""" +
                    """{"type":"text","text":"MATCH: yes\nCONFIDENCE: medium\nREASONING: same backpack"}]}""",
            ),
        )
        val result = h.backend.analyzeMatch(bitmap, bitmap, null)
        assertTrue(result.isMatch)
        assertEquals("medium", result.confidence)
        assertEquals("same backpack", result.reasoning)
    }

    @Test
    fun `a refusal is not read as an answer`() = runBlocking {
        val h = enabledHarness(
            http200(
                """{"stop_reason":"refusal","stop_details":{"type":"refusal","category":"general_harms",""" +
                    """"explanation":"x"},"content":[{"type":"text","text":"MATCH: yes\nCONFIDENCE: high"}]}""",
            ),
        )
        val result = h.backend.analyzeMatch(bitmap, bitmap, null)
        assertFalse(result.isMatch)
        assertEquals("error", result.confidence)
        assertTrue(h.logs.any { it.contains("refusal") && it.contains("general_harms") })
        assertTrue(h.logs.none { it.contains(TEST_CLAUDE_KEY) })
    }

    @Test
    fun `an answer cut off by max_tokens before any text is an error, not a no-match`() = runBlocking {
        val h = enabledHarness(
            http200("""{"stop_reason":"max_tokens","content":[{"type":"thinking","thinking":"","signature":"sig"}]}"""),
        )
        val result = h.backend.analyzeMatch(bitmap, bitmap, null)
        assertFalse(result.isMatch)
        assertEquals("error", result.confidence)
    }

    @Test
    fun `the request body builder is the only place the body is put together`() {
        val body = Json.parseToJsonElement(
            CloudClaudeLlmBackend.requestBody("some-model", listOf(JsonObject(mapOf("type" to JsonPrimitive("text"))))),
        ).jsonObject
        assertEquals(setOf("model", "max_tokens", "output_config", "messages"), body.keys)
        assertEquals("some-model", body.getValue("model").jsonPrimitive.content)
    }

    @Test
    fun `effort is high, the output limit is 8192 and the timeouts cover a non-streamed generation`() {
        assertEquals("high", CloudClaudeLlmBackend.EFFORT)
        assertEquals(8192, CloudClaudeLlmBackend.MAX_TOKENS)
        assertTrue(CloudClaudeLlmBackend.READ_TIMEOUT_MS >= 120_000L)
        assertEquals(150_000L, CloudClaudeLlmBackend.CALL_TIMEOUT_MS)
    }

    @Test
    fun `no beta header and no fallbacks field are sent`() = runBlocking {
        val h = enabledHarness()
        h.backend.analyzeMatch(bitmap, bitmap, null)
        assertNull(h.server.requests.single().beta)
        assertNull(sentBody(h)["fallbacks"])
    }

    @Test
    fun `an answer with no text is an error, not a no-match`() = runBlocking {
        val h = enabledHarness(http200("""{"stop_reason":"end_turn","content":[{"type":"thinking","thinking":"","signature":"sig"}]}"""))
        val result = h.backend.analyzeMatch(bitmap, bitmap, null)
        assertFalse(result.isMatch)
        assertEquals("error", result.confidence)
    }

    @Test
    fun `images are scaled to 768 pixels on the long edge and small ones are left alone`() {
        assertEquals(768 to 432, CloudClaudeLlmBackend.scaledSize(1920, 1080))
        assertEquals(432 to 768, CloudClaudeLlmBackend.scaledSize(1080, 1920))
        assertEquals(768 to 768, CloudClaudeLlmBackend.scaledSize(4000, 4000))
        assertEquals(640 to 480, CloudClaudeLlmBackend.scaledSize(640, 480))
        assertEquals(768 to 600, CloudClaudeLlmBackend.scaledSize(768, 600))
    }

    @Test
    fun `the reference photo is encoded once per search`() = runBlocking {
        val h = enabledHarness()
        val reference = fakeBitmap()
        h.backend.analyzeMatch(reference, fakeBitmap(), null)
        h.backend.analyzeMatch(reference, fakeBitmap(), null)
        h.backend.analyzeMatch(reference, fakeBitmap(), null)
        // One reference encode plus three crops.
        assertEquals(4, h.encodeCount.get())
        // A different reference (a new search) is encoded again.
        h.backend.analyzeMatch(fakeBitmap(), fakeBitmap(), null)
        assertEquals(6, h.encodeCount.get())
    }

    private fun http(code: Int, text: String) =
        "HTTP/1.1 $code $text\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"

    @Test
    fun `a 401 marks the key invalid, shows a notice without the key and stops sending`() = runBlocking {
        val h = enabledHarness(http(401, "Unauthorized"))
        val result = h.backend.analyzeMatch(bitmap, bitmap, null)
        assertEquals("error", result.confidence)
        assertTrue(result.reasoning.contains("401"))
        assertEquals(ClaudeKeyState.INVALID, h.gate.keyState)
        assertNull(h.gate.keyIfOpen())
        val status = h.selector.status.value
        assertFalse(status.isCloudActive)
        assertTrue(status.notice!!.contains("rejected"))
        assertTrue(h.logs.none { it.contains(TEST_CLAUDE_KEY) })
        assertFalse(status.toString().contains(TEST_CLAUDE_KEY))
        // No second request is sent with the rejected key.
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, null))
        assertEquals(1, h.server.requests.size)
        // Saving a new key gives it a fresh try.
        assertTrue(h.gate.saveKey("another-key"))
        assertEquals(ClaudeKeyState.FOUND, h.gate.keyState)
        // The old key's failure notice is gone once the status is refreshed.
        h.selector.refreshWithKeyLookup()
        val after = h.selector.status.value
        assertNull(after.cloudProblem)
        assertNull(after.notice)
        assertTrue(after.isCloudActive)
    }

    @Test
    fun `saving a key clears the failure count and the breaker as well as the notice`() = runBlocking {
        val h = enabledHarness(http(500, "Server Error"))
        repeat(3) { h.backend.analyzeMatch(bitmap, bitmap, null) }
        assertTrue(h.backend.breakerOpen)
        assertTrue(h.gate.saveKey("another-key"))
        assertFalse(h.backend.breakerOpen)
        assertNull(h.backend.problem)
        // One failure after the save is a first failure, not a probe: the breaker stays closed.
        h.backend.analyzeMatch(bitmap, bitmap, null)
        assertFalse(h.backend.breakerOpen)
        // Removing the key clears the state too.
        h.backend.analyzeMatch(bitmap, bitmap, null)
        h.backend.analyzeMatch(bitmap, bitmap, null)
        assertTrue(h.backend.breakerOpen)
        h.gate.removeKey()
        assertFalse(h.backend.breakerOpen)
        assertNull(h.backend.problem)
    }

    @Test
    fun `a late 401 for a key that was replaced does not mark the new key invalid`() = runBlocking {
        val h = enabledHarness(http(401, "Unauthorized"))
        // The user saves a new key after the request was prepared but before the answer comes back.
        h.backend.beforeSend = { h.gate.saveKey("another-key") }
        val result = h.backend.analyzeMatch(bitmap, bitmap, null)
        assertEquals(1, h.server.requests.size)
        assertEquals(CloudClaudeLlmBackend.BLOCKED, result)
        assertEquals(ClaudeKeyState.FOUND, h.gate.keyState)
        assertNull(h.backend.problem)
        assertEquals("another-key", h.gate.keyIfOpen())
    }

    @Test
    fun `a 401 after the key was removed does not leave the state invalid`() {
        val h = enabledHarness()
        val generationAtSend = h.gate.keyGeneration
        assertTrue(h.gate.removeKey())
        assertFalse(h.gate.markKeyInvalid(generationAtSend))
        assertEquals(ClaudeKeyState.MISSING, h.gate.keyState)
        assertEquals(ClaudeKeyState.MISSING, h.gate.refreshKeyState())
        // A 401 for the key that is currently saved still counts.
        assertTrue(h.gate.saveKey("another-key"))
        assertTrue(h.gate.markKeyInvalid(h.gate.keyGeneration))
        assertEquals(ClaudeKeyState.INVALID, h.gate.keyState)
    }

    @Test
    fun `after the cool-off a single failed probe opens the breaker again`() = runBlocking {
        val h = enabledHarness(http(500, "Server Error"))
        repeat(3) { h.backend.analyzeMatch(bitmap, bitmap, null) }
        assertTrue(h.backend.breakerOpen)
        h.nowMs += CloudClaudeLlmBackend.COOL_OFF_MS + 1
        assertFalse(h.backend.breakerOpen)
        // The probe fails: one failure is enough to open it again.
        h.backend.analyzeMatch(bitmap, bitmap, null)
        assertEquals(4, h.server.requests.size)
        assertTrue(h.backend.breakerOpen)
    }

    @Test
    fun `cancelling the calling coroutine cancels the http call`() = runBlocking {
        val h = enabledHarness()
        h.server.hold = true
        val job = launch(Dispatchers.IO) { h.backend.analyzeMatch(bitmap, bitmap, null) }
        withTimeout(10_000) { while (h.server.requests.isEmpty()) delay(10) }
        assertEquals(1, h.backend.inFlightCount())
        job.cancel()
        withTimeout(10_000) { job.join() }
        withTimeout(10_000) { while (h.server.clientClosed.get() == 0) delay(10) }
        assertEquals(0, h.backend.inFlightCount())
    }

    @Test
    fun `the cached reference is dropped when requests are cancelled and when the backend closes`() = runBlocking {
        val h = enabledHarness()
        val reference = fakeBitmap()
        h.backend.analyzeMatch(reference, bitmap, null)
        h.backend.analyzeMatch(reference, bitmap, null)
        assertEquals(3, h.encodeCount.get())
        h.backend.cancelAllRequests()
        h.backend.analyzeMatch(reference, bitmap, null)
        // The reference is encoded again after the cancel (one reference plus one crop).
        assertEquals(5, h.encodeCount.get())
        h.settings.setCloudOptIn(false)
        h.settings.confirmCloudConsent(selectCloud = true)
        h.backend.analyzeMatch(reference, bitmap, null)
        assertEquals(7, h.encodeCount.get())
    }

    @Test
    fun `another 4xx status is shown in the reasoning notice while cloud stays active`() = runBlocking {
        val h = enabledHarness(http(400, "Bad Request"))
        val result = h.backend.analyzeMatch(bitmap, bitmap, null)
        assertTrue(result.reasoning.contains("400"))
        val status = h.selector.status.value
        assertTrue(status.notice!!.contains("HTTP 400"))
        assertTrue(status.cloudProblem!!.contains("400"))
        assertTrue(status.isCloudActive)
    }

    @Test
    fun `three failures in a row open the breaker for 60 seconds and the banner follows`() = runBlocking {
        val h = enabledHarness(http(500, "Server Error"))
        repeat(3) { h.backend.analyzeMatch(bitmap, bitmap, null) }
        assertEquals(3, h.server.requests.size)
        assertTrue(h.backend.breakerOpen)
        assertFalse(h.backend.isAvailable)
        var status = h.selector.status.value
        assertTrue(status.cloudCoolingOff)
        assertFalse(status.isCloudActive)
        assertTrue(status.notice!!.contains("Cloud Claude is unavailable"))

        // While open, nothing is encoded or sent.
        val encodedBefore = h.encodeCount.get()
        assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, null))
        assertEquals(3, h.server.requests.size)
        assertEquals(encodedBefore, h.encodeCount.get())

        // After the cool-off one request is allowed again, and a success closes the breaker.
        h.nowMs += CloudClaudeLlmBackend.COOL_OFF_MS + 1
        assertFalse(h.backend.breakerOpen)
        h.server.rawResponse = http200("""{"content":[{"type":"text","text":"MATCH: no\nCONFIDENCE: high\nREASONING: x"}]}""")
        val ok = h.backend.analyzeMatch(bitmap, bitmap, null)
        assertEquals("high", ok.confidence)
        status = h.selector.status.value
        assertFalse(status.cloudCoolingOff)
        assertNull(status.cloudProblem)
        assertTrue(status.isCloudActive)
    }

    @Test
    fun `a refusal does not count toward the breaker`() = runBlocking {
        val h = enabledHarness(http200("""{"stop_reason":"refusal","stop_details":{"type":"refusal","category":"cyber"},"content":[]}"""))
        repeat(4) { h.backend.analyzeMatch(bitmap, bitmap, null) }
        assertFalse(h.backend.breakerOpen)
        assertEquals(4, h.server.requests.size)
    }
}
