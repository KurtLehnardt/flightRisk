package com.flightrisk.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** [NackReasons] against `nack_reasons.json`. */
class NackReasonsTest {
    @Test
    fun everyVocabularyOfTheFixtureIsInTheCode() {
        val cases = Fixtures.cases("nack_reasons.json")
        assertEquals(cases.keys, NackReasons.BY_FIXTURE_CASE.keys)
        for ((name, case) in cases) {
            val values = (case["payload"] as JsonArray).map { (it as JsonPrimitive).content }.toSet()
            val code = NackReasons.BY_FIXTURE_CASE.getValue(name)
            // Fails when the fixture gains a value the code lacks.
            assertTrue("$name: the code lacks ${values - code}", code.containsAll(values))
            assertEquals("$name: the code has values the fixture does not", values, code)
        }
    }

    @Test
    fun controlModesMatchTheSealedType() {
        assertEquals(NackReasons.CONTROL_MODES, ControlMode.known().map { it.raw }.toSet())
    }

    @Test
    fun everyReasonInTheFixturesIsKnown() {
        val reasons = mutableSetOf<String>()
        for (file in Fixtures.EXPECTED_FILES - setOf("nack_reasons.json", "fleet_plan.json", "fleet_event.json", "unknown_keys.json")) {
            for (case in Fixtures.cases(file).values) collectReasons(case["payload"], reasons)
        }
        assertTrue(reasons.isNotEmpty())
        for (r in reasons) assertTrue("unknown reason code $r", ReasonCode(r).known)
    }

    /** `reason` and `fallback_reason` values anywhere in a payload (plan and retask reasons are free text). */
    private fun collectReasons(e: JsonElement?, out: MutableSet<String>) {
        when (e) {
            is JsonObject -> for ((k, v) in e) {
                if ((k == "reason" || k == "fallback_reason") && v is JsonPrimitive && v.isString) out += v.content
                collectReasons(v, out)
            }
            is JsonArray -> e.forEach { collectReasons(it, out) }
            else -> Unit
        }
    }
}
