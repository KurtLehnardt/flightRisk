package com.flightrisk.app.llm

import kotlinx.coroutines.runBlocking
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
        assertEquals("low", body.getValue("output_config").jsonObject.getValue("effort").jsonPrimitive.content)
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
}
