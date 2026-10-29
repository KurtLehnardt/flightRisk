package com.flightrisk.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Decodes every case of every fixture file with the decoder a phone would
 * use, and checks that nothing in the directory is left undecoded.
 */
class AllFixturesDecodeTest {
    @Test
    fun fixtureDirectoryHoldsExactlyTheSpecifiedFiles() {
        val present = Fixtures.dir.listFiles { f -> f.isFile && f.name.endsWith(".json") }!!.map { it.name }.toSet()
        assertEquals(Fixtures.EXPECTED_FILES, present)
    }

    @Test
    fun everyCaseOfEveryFixtureDecodes() {
        var decoded = 0
        var total = 0
        for (file in Fixtures.EXPECTED_FILES.sorted()) {
            for ((caseName, case) in Fixtures.cases(file)) {
                total++
                decodeCase(file, caseName, case)
                decoded++
            }
        }
        assertEquals(total, decoded)
        // 23 files; the count only guards against an empty or truncated directory.
        assertTrue("expected well over 100 cases, got $total", total > 100)
    }

    private fun decodeCase(file: String, caseName: String, case: JsonObject) {
        val label = "$file/$caseName"
        val payload = case["payload"]
        when (file) {
            "socketio_frames.json" -> {
                val frame = (case["frame"] as JsonPrimitive).content
                val parsed = SocketIoFrames.parse(frame)
                assertNotNull("$label: frame did not parse", parsed)
                assertEquals(label, (case["engineio_type"] as JsonPrimitive).content, parsed!!.type.name)
            }
            "client_emits.json" -> {
                val event = (case["event"] as JsonPrimitive).content
                val allowed = (case["allowed"] as JsonPrimitive).booleanOrNull!!
                assertEquals(label, allowed, ClientCommand.isAllowed(event))
            }
            "nack_reasons.json" -> {
                val values = (payload as JsonArray).map { (it as JsonPrimitive).content }
                val known = NackReasons.BY_FIXTURE_CASE[caseName]
                assertNotNull("$label: vocabulary missing from NackReasons", known)
                assertTrue("$label: missing ${values - known!!}", known.containsAll(values))
            }
            "edge_ground_to_edge.json" -> {
                val message = EdgeLink.parse(payload as JsonObject)
                assertFalse(label, message is GroundToEdgeMessage.Malformed)
                assertEquals(label, payload["type"].stringValue(), message.type)
            }
            "edge_edge_to_ground.json" -> {
                val o = payload as JsonObject
                if (o.str("type") == DetectionsWire.TYPE) {
                    assertNotNull(label, DetectionsWire.decode(o))
                } else {
                    val reply = EdgeReply.fromJson(o)
                    assertTrue(label, reply.type in EdgeLink.NEVER_SENT_TYPES)
                }
            }
            "unknown_keys.json" -> {
                val event = case["event"].stringValue()
                if (event != null) {
                    assertDecodedEvent(label, event, payload)
                } else {
                    assertFalse(label, EdgeLink.parse(payload as JsonObject) is GroundToEdgeMessage.Malformed)
                }
            }
            else -> {
                val event = case["event"].stringValue() ?: bad("$label has no event name")
                assertDecodedEvent(label, event, payload)
            }
        }
    }

    private fun assertDecodedEvent(label: String, event: String, payload: JsonElement?) {
        val decoded = ServerEvents.decode(event, payload)
        assertFalse("$label decoded as $decoded", decoded is ServerEvent.Ignored)
        assertFalse("$label decoded as $decoded", decoded is ServerEvent.Malformed)
    }

    /**
     * Decoding never throws, whatever the types: every leaf of every event
     * payload is replaced in turn by a string, a number, a boolean, null, an
     * object and an array, and the result is decoded.
     */
    @Test
    fun decodingNeverThrowsOnWrongTypes() {
        val replacements: List<JsonElement> = listOf(
            JsonPrimitive("x"), JsonPrimitive(1.5), JsonPrimitive(true), JsonNull,
            JsonObject(emptyMap()), JsonArray(emptyList()),
        )
        for (file in Fixtures.EXPECTED_FILES) {
            for ((caseName, case) in Fixtures.cases(file)) {
                val event = case["event"].stringValue() ?: continue
                val payload = case["payload"] ?: continue
                for (replacement in replacements) {
                    val mutated = replaceLeaves(payload, replacement)
                    try {
                        ServerEvents.decode(event, mutated)
                        ServerEvents.decode(event, replacement)
                    } catch (e: Exception) {
                        throw AssertionError("$file/$caseName threw with leaves set to $replacement", e)
                    }
                }
            }
        }
    }

    @Test
    fun edgeDecodersNeverThrowOnWrongTypes() {
        val replacements: List<JsonElement> = listOf(JsonPrimitive("x"), JsonPrimitive(2), JsonNull, JsonArray(emptyList()))
        for (file in listOf("edge_ground_to_edge.json", "edge_edge_to_ground.json")) {
            for ((caseName, case) in Fixtures.cases(file)) {
                val payload = case["payload"] as JsonObject
                for (replacement in replacements) {
                    val mutated = replaceLeaves(payload, replacement, keepKeys = setOf("type")) as JsonObject
                    try {
                        EdgeLink.actionFor(EdgeLink.parse(mutated))
                        DetectionsWire.decode(mutated)
                        EdgeReply.fromJson(mutated)
                    } catch (e: Exception) {
                        throw AssertionError("$file/$caseName threw with leaves set to $replacement", e)
                    }
                }
            }
        }
    }

    private fun replaceLeaves(e: JsonElement, with: JsonElement, keepKeys: Set<String> = emptySet()): JsonElement =
        when (e) {
            is JsonObject -> JsonObject(e.mapValues { (k, v) -> if (k in keepKeys) v else replaceLeaves(v, with, keepKeys) })
            is JsonArray -> JsonArray(e.map { replaceLeaves(it, with, keepKeys) })
            else -> with
        }
}
