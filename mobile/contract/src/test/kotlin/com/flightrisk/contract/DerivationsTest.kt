package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * One test per rule of the dashboard's `connState` and `modeText`
 * (`flightrisk/dashboard/static/ui/ui.js`; docs/mobile-contract.md,
 * "Derived state"), using the fixture cases.
 */
class DerivationsTest {
    private fun fleet(case: String) = Fixtures.eventAs<FleetStatus.Fleet>("fleet_status.json", case)

    private fun drone(case: String, id: String) = fleet(case).drones.getValue(id)

    /** A fixture drone with some keys replaced, to reach rules no fixture case isolates. */
    private fun droneWith(case: String, id: String, vararg changes: Pair<String, Any?>): FleetDrone {
        val original = Fixtures.payload("fleet_status.json", case)["drones"]!!.jsonObject[id] as JsonObject
        val map = original.toMutableMap()
        for ((k, v) in changes) {
            if (v == REMOVE) map.remove(k) else map[k] = toJson(v)
        }
        return FleetDrone.fromJson(id, JsonObject(map))
    }

    private fun toJson(v: Any?) = when (v) {
        null -> kotlinx.serialization.json.JsonNull
        is String -> JsonPrimitive(v)
        is Number -> JsonPrimitive(v)
        is Boolean -> JsonPrimitive(v)
        else -> error("unsupported $v")
    }

    // Connection state, rule 1: lost == true -> LOST.
    @Test
    fun connectionRule1Lost() {
        assertEquals(ConnectionState.LOST, ConnectionState.of(drone("one_lost", "drone-2")))
        // lost wins over connected.
        assertEquals(ConnectionState.LOST, ConnectionState.of(droneWith("one_lost", "drone-2", "connected" to false)))
        assertTrue(ConnectionState.LOST.isStale)
    }

    // Connection state, rule 2: connected == false -> DOWN.
    @Test
    fun connectionRule2Down() {
        assertEquals(ConnectionState.DOWN, ConnectionState.of(drone("one_down", "drone-2")))
        assertEquals(ConnectionState.CONNECTED, ConnectionState.of(drone("one_down", "drone-1")))
        assertTrue(ConnectionState.DOWN.isStale)
        assertEquals("No live link: values are the last ones received", ConnectionState.STALE_NOTE)
    }

    // Connection state, rule 3: connected == true -> CONNECTED.
    @Test
    fun connectionRule3Connected() {
        for (d in fleet("all_connected").drones.values) assertEquals(ConnectionState.CONNECTED, ConnectionState.of(d))
        assertFalse(ConnectionState.CONNECTED.isStale)
    }

    // Connection state, rule 4: otherwise UNKNOWN, shown as "--".
    @Test
    fun connectionRule4Unknown() {
        val legacy = Fixtures.eventAs<FleetStatus.Legacy>("fleet_status.json", "non_fleet_local_drone")
        assertEquals(ConnectionState.UNKNOWN, ConnectionState.of(legacy.drones.getValue("drone-1")))
        assertEquals(ConnectionState.UNKNOWN, ConnectionState.of(droneWith("all_connected", "drone-1", "connected" to REMOVE)))
        assertEquals(ConnectionState.UNKNOWN, ConnectionState.of(droneWith("all_connected", "drone-1", "connected" to null)))
        // A non-boolean is not `=== true`.
        assertEquals(ConnectionState.UNKNOWN, ConnectionState.of(droneWith("all_connected", "drone-1", "connected" to "true")))
        assertEquals("--", ConnectionState.UNKNOWN.label)
    }

    // Control mode, rule 1: orphan_land_in_s is a number -> HELD with the countdown.
    @Test
    fun controlRule1OrphanCountdown() {
        val s = fleet("one_held_orphan_countdown")
        val mode = ControlModeDisplay.of(s.drones.getValue("drone-2"), s.manualControlled)
        assertEquals(ControlModeDisplay.Held(120.0), mode)
        assertEquals("HELD: auto-landing in 120 s, no controller", mode.text)
        // The countdown wins over manual_controlled.
        val controlled = ControlModeDisplay.of(s.drones.getValue("drone-2"), "drone-2")
        assertEquals("HELD: auto-landing in 120 s, no controller", controlled.text)
        // A fractional value prints as JavaScript prints it.
        assertEquals(
            "HELD: auto-landing in 12.5 s, no controller",
            ControlModeDisplay.of(droneWith("all_connected", "drone-1", "orphan_land_in_s" to 12.5), null).text,
        )
    }

    // Control mode, rule 2: the drone is manual_controlled -> MANUAL.
    @Test
    fun controlRule2ManualControlled() {
        val s = fleet("one_manual")
        assertEquals(ControlModeDisplay.Manual, ControlModeDisplay.of(s.drones.getValue("drone-2"), s.manualControlled))
        assertEquals("MANUAL", ControlModeDisplay.Manual.text)
        // manual_controlled wins over a held control_mode.
        val held = droneWith("one_manual", "drone-2", "control_mode" to "held")
        assertEquals(ControlModeDisplay.Manual, ControlModeDisplay.of(held, "drone-2"))
        // Only the controlled drone is MANUAL.
        assertEquals(ControlModeDisplay.Autonomous(true), ControlModeDisplay.of(s.drones.getValue("drone-1"), s.manualControlled))
    }

    // Control mode, rule 3: control_mode or edge_control_mode == "held" -> HELD without a countdown.
    @Test
    fun controlRule3Held() {
        val held = droneWith("one_held_orphan_countdown", "drone-2", "orphan_land_in_s" to null)
        assertEquals(ControlModeDisplay.Held(null), ControlModeDisplay.of(held, null))
        assertEquals("HELD: hovering, search paused", ControlModeDisplay.of(held, null).text)
        val edgeOnly = droneWith(
            "one_held_orphan_countdown", "drone-2",
            "orphan_land_in_s" to null, "control_mode" to "autonomous",
        )
        assertEquals("HELD: hovering, search paused", ControlModeDisplay.of(edgeOnly, null).text)
    }

    // Control mode, rule 4: control_mode == "manual" -> MANUAL.
    @Test
    fun controlRule4Manual() {
        val s = fleet("one_manual")
        val mode = ControlModeDisplay.of(s.drones.getValue("drone-2"), null)
        assertEquals(ControlModeDisplay.Manual, mode)
        // edge_control_mode "manual" alone is not enough.
        val edgeOnly = droneWith("one_manual", "drone-2", "control_mode" to "autonomous")
        assertEquals(ControlModeDisplay.Autonomous(true), ControlModeDisplay.of(edgeOnly, null))
    }

    // Control mode, rule 5: Flying, On the ground, or nothing.
    @Test
    fun controlRule5Autonomous() {
        assertEquals("On the ground", ControlModeDisplay.of(drone("all_connected", "drone-1"), null).text)
        assertEquals("Flying", ControlModeDisplay.of(drone("one_lost", "drone-1"), null).text)
        val unknown = droneWith("all_connected", "drone-1", "is_flying" to REMOVE)
        assertEquals(ControlModeDisplay.Autonomous(null), ControlModeDisplay.of(unknown, null))
        assertEquals("", ControlModeDisplay.of(unknown, null).text)
    }

    @Test
    fun manualStatusIsMergedOnlyThroughTheReducer() {
        val s = fleet("all_connected")
        val manual = Fixtures.eventAs<ManualStatus>("manual_status.json", "held_orphan_countdown")
        val board = DroneStateReducer.reduceAll(DroneStateReducer.INITIAL, listOf(s, manual))
        assertEquals("HELD: auto-landing in 120 s, no controller", board.controlMode("drone-2")!!.text)
        // A ManualDroneStatus can be displayed on its own too.
        assertEquals(
            "HELD: auto-landing in 120 s, no controller",
            ControlModeDisplay.of("drone-2", manual.drones.getValue("drone-2"), manual.controlled).text,
        )
    }

    @Test
    fun fractionalCountdownPrintsAsTheDashboardDoes() {
        val d = droneWith("all_connected", "drone-1", "orphan_land_in_s" to 29.6, "control_mode" to "held")
        assertEquals("HELD: auto-landing in 29.6 s, no controller", ControlModeDisplay.of(d, null).text)
    }

    @Test
    fun legacyDronesShowOnlyFlyingOrOnTheGround() {
        val legacy = Fixtures.eventAs<FleetStatus.Legacy>("fleet_status.json", "non_fleet_local_drone")
        assertEquals("On the ground", ControlModeDisplay.of(legacy.drones.getValue("drone-1")).text)
        val flying = LegacyDrone("x", null, null, null, null, isFlying = true, isConnected = true)
        assertEquals("Flying", ControlModeDisplay.of(flying).text)
        assertEquals("", ControlModeDisplay.of(flying.copy(isFlying = null)).text)
    }

    @Test
    fun resumeSearchOnlyForAHeldDroneNobodyControls() {
        val held = drone("one_held_orphan_countdown", "drone-2")
        assertTrue(ClientCommand.canResumeSearch(held, null))
        assertFalse(ClientCommand.canResumeSearch(held, "drone-1"))
        assertFalse(ClientCommand.canResumeSearch(drone("one_held_orphan_countdown", "drone-1"), null))
        // The dashboard reads an empty manual_controlled as nobody.
        assertTrue(ClientCommand.canResumeSearch(held, ""))
    }

    @Test
    fun droneLinkText() {
        val s = fleet("drone_link_down")
        assertEquals(
            "Drone link down: Tello unreachable (no state for 12 s). Land still works.",
            DisplayText.droneLinkDown(s.drones.getValue("drone-2")),
        )
        assertNull(DisplayText.droneLinkDown(s.drones.getValue("drone-1")))
        assertNull(DisplayText.droneLinkDown(null, "ignored"))
        assertEquals(
            "Drone link down: the edge cannot reach the drone. Land still works.",
            DisplayText.droneLinkDown(false, null),
        )
        assertEquals(
            "Drone link down: the edge cannot reach the drone. Land still works.",
            DisplayText.droneLinkDown(false, ""),
        )
    }

    private companion object {
        val REMOVE = Any()
    }
}
