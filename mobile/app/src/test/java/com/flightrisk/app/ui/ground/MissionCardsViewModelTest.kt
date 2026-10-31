package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.EmitPolicy
import com.flightrisk.app.ground.FakeConnection
import com.flightrisk.app.ground.eventually
import com.flightrisk.contract.ClientCommand
import kotlinx.coroutines.delay
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Profile selection, the widened emit policy and the plan-current flag of the Ground view model. */
class MissionCardsViewModelTest {

    private fun dronePayload(id: String, profile: String) =
        "42[\"set_detection_profile\",{\"drone_id\":\"$id\",\"profile\":\"$profile\"}]"

    private fun fleetPayload(profile: String) = "42[\"set_fleet_detection_profile\",{\"profile\":\"$profile\"}]"

    /** Connected, fresh fleet, and the given profile case loaded. */
    private suspend fun GroundHarness.withProfiles(case: String): FakeConnection {
        val c = connectWithFleet()
        c.fixture("detection_profiles.json", case)
        eventually("profiles in the snapshot") { ui.snapshot.detectionProfiles != null }
        return c
    }

    private suspend fun GroundHarness.assertNothingSent(c: FakeConnection) {
        delay(120)
        assertEquals(emptyList<String>(), c.commandFrames())
    }

    // ---- selection sends the exact payload

    @Test
    fun `selecting a profile for a drone sends exactly one set_detection_profile`() = harnessTest {
        val c = withProfiles("unsupported")
        vm.selectDroneProfile("drone-1", "top_down").join()
        eventually("frame") { c.commandFrames() == listOf(dronePayload("drone-1", "top_down")) }
        val f = ui.profileFeedback!!
        assertFalse(f.isFailure)
        assertTrue(f.awaitsAnswer)
        assertEquals("Profile change sent for drone-1: Top-down search (HERIDAL). Waiting for the ground's answer.", f.text)
    }

    @Test
    fun `an empty profile follows the fleet default and clears it`() = harnessTest {
        val c = withProfiles("unsupported")
        vm.selectDroneProfile("drone-1", "").join()
        vm.selectFleetProfile("").join()
        eventually("frames") { c.commandFrames() == listOf(dronePayload("drone-1", ""), fleetPayload("")) }
    }

    @Test
    fun `selecting the fleet default sends exactly one set_fleet_detection_profile`() = harnessTest {
        val c = withProfiles("unsupported")
        vm.selectFleetProfile("level").join()
        eventually("frame") { c.commandFrames() == listOf(fleetPayload("level")) }
        assertEquals("Fleet default profile change sent: Level flight (faces). Waiting for the ground's answer.", ui.profileFeedback!!.text)
    }

    @Test
    fun `the ground's next profile state replaces the sent message with its own status`() = harnessTest {
        val c = withProfiles("unsupported")
        vm.selectDroneProfile("drone-1", "top_down").join()
        assertNotNull(ui.profileFeedback)
        c.fixture("detection_profiles.json", "pending")
        eventually("feedback cleared") { ui.profileFeedback == null }
        assertEquals(true, ui.snapshot.detectionProfiles!!.drones.getValue("drone-1").pending)
    }

    // ---- refusals: nothing is sent

    @Test
    fun `nothing is sent while the state is stale or the link is down, and the card says why`() = harnessTest {
        val c = withProfiles("unsupported")
        drop(c)
        eventually("stale") { ui.isStateStale }
        vm.selectDroneProfile("drone-1", "top_down").join()
        val f = ui.profileFeedback!!
        assertTrue(f.isFailure)
        assertEquals("Profile for drone-1 was NOT changed: ${MissionFormat.NOT_CONNECTED_NOTE}", f.text)
        vm.selectFleetProfile("level").join()
        assertEquals("Fleet default profile was NOT changed: ${MissionFormat.NOT_CONNECTED_NOTE}", ui.profileFeedback!!.text)
        advanceBackoff()
        val second = connect(1)
        // Connected again, but no fresh fleet_status yet: still not live.
        second.fixture("detection_profiles.json", "unsupported")
        eventually("profiles") { ui.snapshot.detectionProfiles != null }
        assertTrue(ui.isStateStale)
        vm.selectDroneProfile("drone-1", "top_down").join()
        assertEquals("Profile for drone-1 was NOT changed: ${MissionFormat.NOT_LIVE_NOTE}", ui.profileFeedback!!.text)
        assertNothingSent(c)
        assertNothingSent(second)
    }

    @Test
    fun `nothing is sent when the ground says controls are disabled`() = harnessTest {
        val c = withProfiles("standby_controls_disabled")
        vm.selectDroneProfile("drone-1", "top_down").join()
        assertTrue(ui.profileFeedback!!.text.contains("This ground station is the standby."))
        vm.selectFleetProfile("level").join()
        assertTrue(ui.profileFeedback!!.isFailure)
        assertNothingSent(c)
    }

    @Test
    fun `nothing is sent for an unsupported drone, an unknown drone or an unknown profile`() = harnessTest {
        val c = withProfiles("unsupported")
        vm.selectDroneProfile("drone-2", "top_down").join()
        assertEquals("Profile for drone-2 was NOT changed: ${MissionFormat.WHY_UNSUPPORTED}", ui.profileFeedback!!.text)
        vm.selectDroneProfile("drone-9", "top_down").join()
        assertEquals("Profile for drone-9 was NOT changed: ${MissionFormat.WHY_UNKNOWN_DRONE}", ui.profileFeedback!!.text)
        vm.selectDroneProfile("drone-1", "not_a_profile").join()
        assertEquals("Profile for drone-1 was NOT changed: ${MissionFormat.WHY_UNKNOWN_PROFILE}", ui.profileFeedback!!.text)
        vm.selectFleetProfile("not_a_profile").join()
        assertTrue(ui.profileFeedback!!.text.endsWith(MissionFormat.WHY_UNKNOWN_PROFILE))
        vm.selectDroneProfile("  ", "top_down").join()
        assertTrue(ui.profileFeedback!!.isFailure)
        assertNothingSent(c)
    }

    @Test
    fun `nothing is sent without a profile board`() = harnessTest {
        val c = connectWithFleet()
        vm.selectFleetProfile("level").join()
        assertEquals("Fleet default profile was NOT changed: ${MissionFormat.WHY_NO_BOARD}", ui.profileFeedback!!.text)
        c.fixture("detection_profiles.json", "no_board")
        eventually("board") { ui.snapshot.detectionProfiles != null }
        vm.selectFleetProfile("level").join()
        assertTrue(ui.profileFeedback!!.text.endsWith(MissionFormat.WHY_NO_BOARD))
        assertNothingSent(c)
    }

    @Test
    fun `a selection that waits for the ground becomes a failure when the link is lost`() = harnessTest {
        val c = withProfiles("unsupported")
        vm.selectDroneProfile("drone-1", "top_down").join()
        assertTrue(ui.profileFeedback!!.awaitsAnswer)
        drop(c)
        eventually("failure") { ui.profileFeedback?.isFailure == true }
        assertTrue(ui.profileFeedback!!.text.contains("the connection was lost before the ground answered"))
        vm.dismissProfileFeedback()
        assertNull(ui.profileFeedback)
    }

    @Test
    fun `a new address forgets the profile message`() = harnessTest {
        withProfiles("unsupported")
        vm.selectDroneProfile("drone-9", "x").join()
        assertNotNull(ui.profileFeedback)
        onMain { vm.saveAddress("127.0.0.2:5555") }
        eventually("forgotten") { ui.profileFeedback == null }
    }

    // ---- emit policy

    @Test
    fun `the policy widened by exactly the two profile events and can still only narrow`() {
        val policy = GroundConsoleViewModel.EMIT_POLICY
        assertEquals(
            setOf(
                "get_fleet_status", "get_detection_profiles", "get_ceiling_settings", "get_reasoning_provider",
                "get_manual_status", "land_all", "drone_command", "manual_resume_search",
                "set_detection_profile", "set_fleet_detection_profile",
            ),
            policy.allowedEvents,
        )
        // Every other event the contract names stays blocked, including the ones the cards could be mistaken for.
        val blocked = setOf(
            "plan_fleet", "start_fleet", "start_search", "stop_search", "manual_take_control", "manual_command",
            "set_ceiling_settings", "set_reasoning_provider", "set_stream_video", "set_target", "register_drone",
            "deregister_drone", "remove_all_drones", "restart_dashboard", "match_alert_response",
        )
        assertTrue(blocked.none { it in policy.allowedEvents })
        assertTrue(policy.allowedEvents.all { it in ClientCommand.ALLOWED_EVENTS || it == EmitPolicy.LAND_ALL })
        assertTrue(policy.allows(ClientCommand.LandAll))
        // It can narrow and never widen beyond the contract.
        assertEquals(setOf("land_all"), policy.narrowedTo(emptySet()).allowedEvents)
        assertEquals(policy.allowedEvents, policy.narrowedTo(ClientCommand.ALLOWED_EVENTS).allowedEvents)
        assertTrue(policy.narrowedTo(setOf("plan_fleet", "set_ceiling_settings")).allowedEvents == setOf("land_all"))
        // land_all stays allowed even when everything else is removed.
        assertTrue(policy.without(*policy.allowedEvents.toTypedArray()).allows(ClientCommand.LandAll))
    }

    // ---- plan current flag

    @Test
    fun `the plan is not current until a fleet_plan arrives, and not after a reconnect`() = harnessTest {
        assertFalse(ui.planIsCurrent)
        val c = connectWithFleet()
        assertFalse(ui.planIsCurrent)
        c.fixture("fleet_plan.json", "two_drones")
        eventually("current") { ui.planIsCurrent }
        assertNotNull(ui.snapshot.fleetPlan)
        // The link drops: the plan is kept but no longer confirmed.
        drop(c)
        eventually("not current") { !ui.planIsCurrent }
        assertNotNull(ui.snapshot.fleetPlan)
        advanceBackoff()
        val second = connect(1)
        assertFalse(ui.planIsCurrent)
        second.fleetStatus("all_connected")
        eventually("fresh") { !ui.isStateStale }
        assertFalse(ui.planIsCurrent)
        second.fixture("fleet_plan.json", "map_plan_lost_zone_uncovered")
        eventually("current again") { ui.planIsCurrent }
    }

    @Test
    fun `a new session start clears the flag even without a drop event`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("fleet_plan.json", "two_drones")
        eventually("current") { ui.planIsCurrent }
        onMain { vm.handle(com.flightrisk.app.ground.GroundEvent.SessionEnded(com.flightrisk.app.ground.DropReason.PingTimeout, null)) }
        assertFalse(ui.planIsCurrent)
    }

    @Test
    fun `a stalled consumer that dropped events leaves the plan unconfirmed`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("fleet_plan.json", "two_drones")
        eventually("current") { ui.planIsCurrent }
        onMain {
            vm.handle(
                com.flightrisk.app.ground.GroundEvent.SessionEnded(
                    com.flightrisk.app.ground.DropReason.ConsumerStalled(10, 100), "FleetPlan",
                ),
            )
        }
        assertFalse(ui.planIsCurrent)
    }

    @Test
    fun `a new address clears the plan flag and the old plan`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("fleet_plan.json", "two_drones")
        eventually("current") { ui.planIsCurrent }
        onMain { vm.saveAddress("127.0.0.2:5555") }
        eventually("not current") { !ui.planIsCurrent }
        assertNull(ui.snapshot.fleetPlan)
    }

    @Test
    fun `backgrounding the app leaves the retained plan unconfirmed`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("fleet_plan.json", "two_drones")
        eventually("current") { ui.planIsCurrent }
        onMain { vm.onBackground() }
        eventually("not current") { !ui.planIsCurrent }
        assertNotNull(ui.snapshot.fleetPlan)
    }

    @Test
    fun `no secret appears in the state after profile and plan traffic`() = harnessTest {
        val c = withProfiles("refused")
        c.fixture("ceiling_settings.json", "edge_refused")
        c.fixture("reasoning_provider.json", "cloud_allowed_no_key")
        c.fixture("fleet_plan.json", "map_plan_lost_zone_uncovered")
        eventually("all loaded") { ui.snapshot.ceilingSettings != null && ui.snapshot.reasoningProvider != null && ui.planIsCurrent }
        vm.selectDroneProfile("drone-1", "top_down").join()
        assertNoKey(ui.toString())
        assertNoKey(MissionFormat.planCanvas(ui.snapshot.fleetPlan, ui.isStateStale, ui.planIsCurrent).accessibilitySummary)
    }
}
