package com.flightrisk.contract

import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The emit allow-list against `client_emits.json`. */
class ClientCommandTest {
    private val cases = Fixtures.cases("client_emits.json")

    private fun eventsWhere(allowed: Boolean): Set<String> = cases.values
        .filter { (it["allowed"] as JsonPrimitive).booleanOrNull == allowed }
        .map { (it["event"] as JsonPrimitive).content }
        .toSet()

    /** One instance of every command. The `when` in [eventOf] fails to compile if a case is added without a sample. */
    private val samples: List<ClientCommand> = listOf(
        ClientCommand.GetFleetStatus,
        ClientCommand.GetDetectionProfiles,
        ClientCommand.GetCeilingSettings,
        ClientCommand.GetReasoningProvider,
        ClientCommand.GetManualStatus,
        ClientCommand.LandAll,
        ClientCommand.LandDrone("drone-1"),
        ClientCommand.SetDetectionProfile("drone-1", "top_down"),
        ClientCommand.SetFleetDetectionProfile("top_down"),
        ClientCommand.ManualResumeSearch("drone-2"),
    )

    private fun eventOf(c: ClientCommand): String = when (c) {
        ClientCommand.GetFleetStatus -> "get_fleet_status"
        ClientCommand.GetDetectionProfiles -> "get_detection_profiles"
        ClientCommand.GetCeilingSettings -> "get_ceiling_settings"
        ClientCommand.GetReasoningProvider -> "get_reasoning_provider"
        ClientCommand.GetManualStatus -> "get_manual_status"
        ClientCommand.LandAll -> "land_all"
        is ClientCommand.LandDrone -> "drone_command"
        is ClientCommand.SetDetectionProfile -> "set_detection_profile"
        is ClientCommand.SetFleetDetectionProfile -> "set_fleet_detection_profile"
        is ClientCommand.ManualResumeSearch -> "manual_resume_search"
    }

    @Test
    fun allowListIsExactlyTheFixtureSet() {
        val allowed = eventsWhere(true)
        assertEquals(10, allowed.size)
        assertEquals(allowed, ClientCommand.ALLOWED_EVENTS)
        assertEquals(allowed, samples.map { it.event }.toSet())
        for (c in samples) assertEquals(eventOf(c), c.event)
    }

    @Test
    fun everyCommandClassIsSampled() {
        // Nested subclasses of the sealed class, found by reflection, so a new
        // command cannot slip past the allow-list test.
        val nested = ClientCommand::class.java.declaredClasses
            .filter { ClientCommand::class.java.isAssignableFrom(it) }
            .map { it.name }
            .toSet()
        assertEquals(nested, samples.map { it::class.java.name }.toSet())
    }

    @Test
    fun noForbiddenEventCanBeProduced() {
        val forbidden = eventsWhere(false)
        assertTrue(forbidden.containsAll(listOf("plan_fleet", "start_fleet", "manual_command", "manual_take_control")))
        for (name in forbidden) {
            assertFalse(name, ClientCommand.isAllowed(name))
            assertFalse(name, samples.any { it.event == name })
            assertTrue("$name case is named forbidden_$name", cases.containsKey("forbidden_$name"))
        }
        assertTrue((ClientCommand.ALLOWED_EVENTS intersect forbidden).isEmpty())
        assertFalse(ClientCommand.isAllowed("match_alert_response"))
    }

    @Test
    fun payloadsMatchTheFixture() {
        for (c in samples) {
            val case = cases[c.event] ?: bad("no allowed case for ${c.event}")
            val expected = case["payload"]
            val actual = c.payload()
            if (expected == null || expected is JsonNull) {
                assertEquals(c.event, null, actual)
                assertEquals(c.event, null, c.payloadJson())
            } else {
                assertTrue("${c.event}: $actual", jsonSemanticallyEqual(expected, actual))
            }
        }
    }

    @Test
    fun emptyProfileIsAllowedAndBlankDroneIdsAreRejected() {
        assertEquals("""{"drone_id":"drone-1","profile":""}""", ClientCommand.SetDetectionProfile("drone-1", "").payloadJson())
        assertEquals("""{"profile":""}""", ClientCommand.SetFleetDetectionProfile("").payloadJson())
        for (build in listOf<() -> Unit>(
            { ClientCommand.LandDrone(" ") },
            { ClientCommand.SetDetectionProfile("", "top_down") },
            { ClientCommand.ManualResumeSearch("") },
        )) {
            try {
                build()
                bad("a blank drone id was accepted")
            } catch (expected: IllegalArgumentException) {
                // expected
            }
        }
    }

    @Test
    fun stateRequestsAreTheFiveGets() {
        assertEquals(
            listOf("get_fleet_status", "get_detection_profiles", "get_ceiling_settings", "get_reasoning_provider", "get_manual_status"),
            ClientCommand.STATE_REQUESTS.map { it.event },
        )
    }
}
