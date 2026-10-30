package com.flightrisk.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [DroneStateReducer] applies `fleet_status` and `manual_status` in arrival
 * order, newest wins, as the dashboard does.
 */
class DroneStateReducerTest {
    private fun fleetStatus(entry: JsonElement, manualControlled: JsonElement = JsonNull): ServerEvent =
        ServerEvents.decode(
            "fleet_status",
            buildJsonObject {
                put("drones", buildJsonObject { put("d1", entry) })
                put("manual_controlled", manualControlled)
            },
        )

    private fun manualStatus(payload: JsonElement): ServerEvent = ServerEvents.decode("manual_status", payload)

    private fun json(text: String): JsonObject = ContractJson.parseToJsonElement(text).jsonObject

    private fun board(vararg events: ServerEvent) = DroneStateReducer.reduceAll(DroneStateReducer.INITIAL, events.toList())

    // The critic's first ordering case: manual_status says autonomous, then a newer fleet_status says held.
    private val heldFleet = json("""{"control_mode":"held","edge_control_mode":"held","is_flying":true,"orphan_land_in_s":null,"connected":true}""")
    private val autonomousManual = json("""{"controlled":null,"drones":{"d1":{"control_mode":"autonomous","edge_control_mode":"autonomous","manual_last":null,"manual_supported":true,"orphan_land_in_s":null}}}""")

    // The second: manual_status counts down from 120, then a newer fleet_status says 37.
    private val fleet37 = json("""{"control_mode":"held","edge_control_mode":"held","is_flying":true,"orphan_land_in_s":37,"connected":true}""")
    private val manual120 = json("""{"controlled":null,"drones":{"d1":{"control_mode":"held","edge_control_mode":"held","manual_last":null,"manual_supported":true,"orphan_land_in_s":120}}}""")

    @Test
    fun newerFleetStatusHeldWinsOverOlderManualAutonomous() {
        val b = board(manualStatus(autonomousManual), fleetStatus(heldFleet))
        assertEquals("HELD: hovering, search paused", b.controlMode("d1")!!.text)
    }

    @Test
    fun newerManualAutonomousWinsOverOlderFleetHeld() {
        val b = board(fleetStatus(heldFleet), manualStatus(autonomousManual))
        assertEquals("Flying", b.controlMode("d1")!!.text)
    }

    @Test
    fun newerFleetCountdownWinsOverOlderManualCountdown() {
        val b = board(manualStatus(manual120), fleetStatus(fleet37))
        assertEquals("HELD: auto-landing in 37 s, no controller", b.controlMode("d1")!!.text)
    }

    @Test
    fun newerManualCountdownWinsOverOlderFleetCountdown() {
        val b = board(fleetStatus(fleet37), manualStatus(manual120))
        assertEquals("HELD: auto-landing in 120 s, no controller", b.controlMode("d1")!!.text)
    }

    /**
     * After a ground restart, `get_manual_status` can answer before the edge's
     * first telemetry: the manual answer arrives first, for a drone the phone
     * has no record of yet, and the fleet_status after it must win.
     */
    @Test
    fun groundRestartManualAnswerBeforeFirstTelemetry() {
        val afterManual = board(manualStatus(autonomousManual))
        val partial = afterManual.drones.getValue("d1") as DroneRecord.Fleet
        assertTrue(partial.partial)
        assertEquals(ConnectionState.UNKNOWN, partial.connection)
        assertEquals(ControlMode.Autonomous, partial.drone.controlMode)
        assertEquals("", afterManual.controlMode("d1")!!.text)

        val afterFleet = DroneStateReducer.reduce(afterManual, fleetStatus(fleet37))
        val full = afterFleet.drones.getValue("d1") as DroneRecord.Fleet
        assertFalse(full.partial)
        assertEquals(ConnectionState.CONNECTED, full.connection)
        assertEquals("HELD: auto-landing in 37 s, no controller", afterFleet.controlMode("d1")!!.text)
    }

    @Test
    fun manualStatusReplacesOnlyTheKeysItCarries() {
        val b = board(fleetStatus(heldFleet), manualStatus(json("""{"controlled":null,"drones":{"d1":{}}}""")))
        assertEquals("HELD: hovering, search paused", b.controlMode("d1")!!.text)
        val nullCountdown = board(
            fleetStatus(fleet37),
            manualStatus(json("""{"controlled":null,"drones":{"d1":{"orphan_land_in_s":null}}}""")),
        )
        // A carried null replaces the value, as Object.assign does.
        assertEquals("HELD: hovering, search paused", nullCountdown.controlMode("d1")!!.text)
    }

    @Test
    fun fleetStatusDropsDronesItDoesNotListAndSetsTheController() {
        val two = Fixtures.eventAs<FleetStatus.Fleet>("fleet_status.json", "one_manual")
        val b1 = DroneStateReducer.reduce(DroneStateReducer.INITIAL, two)
        assertEquals(listOf("drone-1", "drone-2"), b1.droneIds)
        assertEquals("drone-2", b1.manualControlled)
        assertEquals("MANUAL", b1.controlMode("drone-2")!!.text)

        val one = fleetStatus(heldFleet)
        val b2 = DroneStateReducer.reduce(b1, one)
        assertEquals(listOf("d1"), b2.droneIds)
        assertNull(b2.manualControlled)
    }

    @Test
    fun emptyControllerMeansNobody() {
        val b = board(fleetStatus(heldFleet, kotlinx.serialization.json.JsonPrimitive("")))
        assertNull(b.manualControlled)
        assertTrue(b.canResumeSearch("d1"))
        val m = board(fleetStatus(heldFleet), manualStatus(json("""{"controlled":"","drones":{}}""")))
        assertNull(m.manualControlled)
        assertTrue(m.canResumeSearch("d1"))
    }

    @Test
    fun legacyStatusAndOtherEvents() {
        val legacy = Fixtures.eventAs<FleetStatus.Legacy>("fleet_status.json", "non_fleet_local_drone")
        val b = DroneStateReducer.reduce(DroneStateReducer.INITIAL, legacy)
        assertEquals(ConnectionState.UNKNOWN, b.connection("drone-1"))
        assertEquals("On the ground", b.controlMode("drone-1")!!.text)
        assertFalse(b.canResumeSearch("drone-1"))
        // Unrelated events and unknown drones leave the board alone.
        assertEquals(b, DroneStateReducer.reduce(b, Fixtures.event("land_all_ack.json", "fleet_reached_all")))
        assertNull(b.controlMode("nope"))
        assertEquals(ConnectionState.UNKNOWN, b.connection("nope"))
    }

    @Test
    fun disconnectMarksStaleUntilTheNextFleetStatus() {
        val b = DroneStateReducer.disconnected(board(fleetStatus(heldFleet)))
        assertTrue(b.stale)
        assertEquals(listOf("d1"), b.droneIds)
        assertFalse(DroneStateReducer.reduce(b, fleetStatus(heldFleet)).stale)
    }

    /**
     * The critic's probe cases (`dashboard_probe_cases.json`): for each, the
     * connection label and mode text equal what the dashboard's own ui.js
     * prints for the same events in the same order.
     */
    @Test
    fun matchesTheDashboardOnEveryProbeCase() {
        val text = javaClass.getResource("/dashboard_probe_cases.json")?.readText() ?: bad("probe cases missing")
        val cases = ContractJson.parseToJsonElement(text).jsonObject["cases"] as JsonArray
        assertEquals(30, cases.size)
        for (c in cases) {
            val o = c.jsonObject
            val name = o.str("name")!!
            val fleet = fleetStatus(o["fleet_entry"]!!, o["manual_controlled"] ?: JsonNull)
            val manual = o["manual_status"].takeIf { it != null && it !is JsonNull }?.let(::manualStatus)
            val events = when {
                manual == null -> listOf(fleet)
                o.str("order") == "fleet_after_manual" -> listOf(manual, fleet)
                else -> listOf(fleet, manual)
            }
            val b = DroneStateReducer.reduceAll(DroneStateReducer.INITIAL, events)
            val dashboardText = o.str("dashboard_mode_text")!!
            // A phone never controls a drone, so it shows MANUAL where the dashboard says "under your control".
            val phoneText = if (dashboardText == "MANUAL: under your control") "MANUAL" else dashboardText
            assertEquals("$name connection", o.str("dashboard_conn"), b.connection("d1").label)
            assertEquals("$name mode", phoneText, b.controlMode("d1")!!.text)
        }
    }
}
