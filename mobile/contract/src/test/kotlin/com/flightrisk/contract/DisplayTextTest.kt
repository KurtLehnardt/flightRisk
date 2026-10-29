package com.flightrisk.contract

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Text helpers, worded as `ui.js` and docs/mobile-contract.md word them. */
class DisplayTextTest {
    private fun event(case: String) = Fixtures.eventAs<FleetEvent>("fleet_event.json", case)

    @Test
    fun fleetBannersMatchTheDashboard() {
        val expected = mapOf(
            "plan_refused_unconnected" to "PLAN REFUSED",
            "plan_refused_manual_control" to "PLAN REFUSED",
            "plan_invalidated" to "PLAN EDITED: press Plan search again",
            "start" to "start",
            "start_refused" to "start_refused",
            "start_refused_drone_unreachable" to "start_refused",
            "drone_lost" to "DRONE LOST: drone-2",
            "retask" to "RETASK: drone-2 no lanes accepted yet; 2 lanes awaiting drone-1",
            "retask_failed" to "RETASK FAILED: 2 lanes of drone-1 uncovered: refused by drone-2 (geofence)",
            "land_all" to "LAND ALL sent",
            "land_all_failed" to "LAND ALL sent",
            "profile_switch_pending" to "WARNING: detection profile switch still pending for drone-2; " +
                "once flying, the edge refuses it and keeps its current profile",
        )
        for ((case, text) in expected) assertEquals(case, text, DisplayText.fleetBanner(event(case)))
        assertEquals("swarm_merge", DisplayText.fleetBanner(FleetEvent.Unknown("swarm_merge", null, null)))
        assertNull(DisplayText.fleetBanner(FleetEvent.Unknown("", null, null)))
        assertEquals("RETASK FAILED: drone-1", DisplayText.fleetBanner(FleetEvent.RetaskFailed("drone-1", emptyList(), null)))
        assertEquals("WARNING: detection profile switch pending", DisplayText.fleetBanner(FleetEvent.ProfileSwitchPending(emptyList(), null)))
        assertEquals("DRONE LOST:", DisplayText.fleetBanner(FleetEvent.DroneLost(null, null)))
    }

    @Test
    fun refusalMessages() {
        assertEquals(
            "Plan refused: drone-2 under manual control; press Resume search first",
            DisplayText.refusal(event("plan_refused_manual_control")),
        )
        assertEquals(
            "Start refused: drone-2: Tello unreachable (no state for 12 s)",
            DisplayText.refusal(event("start_refused_drone_unreachable")),
        )
        assertTrue(DisplayText.refusal(event("plan_refused_unconnected"))!!.startsWith("Plan refused: drone-2: not connected."))
        assertEquals("Start refused: unknown", DisplayText.refusal(FleetEvent.StartRefused(null)))
        assertNull(DisplayText.refusal(event("start")))
        assertTrue(DisplayText.isRefusal(event("plan_refused_unconnected")))
        assertTrue(DisplayText.isRefusal(event("start_refused")))
        assertFalse(DisplayText.isRefusal(event("land_all")))
    }

    @Test
    fun landAllResults() {
        assertEquals(
            "LAND ALL reached no drone. Land every drone with its local control now.",
            DisplayText.landAllResult(Fixtures.eventAs("land_all_ack.json", "reached_no_drone")),
        )
        // The ground's own error text for the same failure is identical.
        assertEquals(
            ServerMessage(ServerMessage.Level.ERROR, DisplayText.LAND_ALL_REACHED_NO_DRONE),
            Fixtures.event("error.json", "land_all_reached_no_drone"),
        )
        assertEquals(
            "LAND ALL reached: coordinator 2, transport 2",
            DisplayText.landAllResult(Fixtures.eventAs("land_all_ack.json", "fleet_reached_all")),
        )
        assertEquals("LAND ALL reached: local 1", DisplayText.landAllResult(Fixtures.eventAs("land_all_ack.json", "local_fleet")))
    }

    @Test
    fun countdown() {
        assertEquals("auto-landing in 120 s", DisplayText.autoLandingIn(120.0))
        assertEquals(120.0, DisplayText.orphanCountdown(120.0, 1_000, 1_999), 0.0)
        assertEquals(119.0, DisplayText.orphanCountdown(120.0, 1_000, 2_000), 0.0)
        assertEquals(0.0, DisplayText.orphanCountdown(3.0, 0, 60_000), 0.0)
        assertEquals(30.0, DisplayText.orphanCountdown(30.0, 5_000, 1_000), 0.0)
    }

    @Test
    fun uncoveredLanes() {
        val failed = event("retask_failed") as FleetEvent.RetaskFailed
        assertEquals(
            "drone-1 lane 1 right 75 cm (0, 400) to (75, 400) cm: refused by drone-2 (geofence)",
            DisplayText.uncoveredLane(failed.droneId!!, failed.lanes[0], uncovered = true),
        )
        val retask = event("retask") as FleetEvent.Retask
        assertEquals(
            "drone-2 lane 2 back 400 cm (325, 400) to (325, 0) cm: awaiting drone-1",
            DisplayText.uncoveredLane(retask.droneId!!, retask.awaiting!![1], uncovered = false),
        )
        assertEquals("NOT SEARCHED: 1 lane uncovered, 2 awaiting a drone", DisplayText.notSearchedHeader(1, 2))
        assertEquals("NOT SEARCHED: 2 lanes uncovered, 0 awaiting a drone", DisplayText.notSearchedHeader(2, 0))
    }

    @Test
    fun valuesFormatLikeTheDashboard() {
        assertEquals("--", DisplayText.fmt(null))
        assertEquals("0", DisplayText.fmt(0.0))
        assertEquals("87", DisplayText.fmt(87.0))
        assertEquals("12.3", DisplayText.fmt(12.34))
        assertEquals("12.3", DisplayText.fmt(12.25))
        assertEquals("120", jsNumber(120.0))
        assertEquals("0.1", jsNumber(0.1))
    }

    @Test
    fun landAllNotSentNamesTheFallbacks() {
        assertTrue(DisplayText.LAND_ALL_NOT_SENT.contains("Shift+L"))
        assertTrue(DisplayText.LAND_ALL_NOT_SENT.startsWith("LAND ALL was not sent"))
    }
}
