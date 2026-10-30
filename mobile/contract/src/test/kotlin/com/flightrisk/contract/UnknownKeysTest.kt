package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `unknown_keys.json`: a real payload of every event with `x_future_field`
 * added to every record object. Decoding it must give exactly what decoding
 * the original gives.
 */
class UnknownKeysTest {
    @Test
    fun everyCaseDecodesExactlyLikeItsOriginal() {
        val cases = Fixtures.cases("unknown_keys.json")
        assertTrue(cases.isNotEmpty())
        for ((name, case) in cases) {
            val sourceFile = case.str("source_file") ?: bad("$name has no source_file")
            val sourceCase = case.str("source_case") ?: bad("$name has no source_case")
            val original = Fixtures.payload(sourceFile, sourceCase)
            val withUnknown = case["payload"] as JsonObject
            assertNotEquals("$name should differ on the wire", original, withUnknown)

            val event = case.str("event")
            if (event != null) {
                val a = ServerEvents.decode(event, original)
                val b = ServerEvents.decode(event, withUnknown)
                assertTrue("$name: $a", a !is ServerEvent.Ignored && a !is ServerEvent.Malformed)
                assertEquals(name, a, b)
            } else {
                assertEquals(name, EdgeLink.parse(original), EdgeLink.parse(withUnknown))
            }
        }
    }
}
