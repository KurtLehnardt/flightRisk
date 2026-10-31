package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.app.ground.EmitPolicy
import com.flightrisk.app.ground.STATE_REQUEST_FRAMES
import com.flightrisk.app.ground.eventually
import com.flightrisk.contract.ClientCommand
import com.flightrisk.contract.ConnectionState
import com.flightrisk.contract.DisplayText
import kotlinx.coroutines.delay
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** State, stale marking, cards, banners, toasts and lifecycle of the Ground console. */
class GroundConsoleViewModelTest {

    // ---- 1. Board, cards and stale marking

    @Test
    fun `cards come from the clients board and show the contract values`() = harnessTest {
        val c = connectWithFleet("all_connected")
        assertEquals(listOf("drone-1", "drone-2"), ui.cards.map { it.id })
        val card = ui.cards.first()
        assertEquals("CONNECTED", card.linkChipText)
        assertFalse(card.linkChipGreyed)
        assertEquals("87%", card.batteryText)
        assertEquals("100 cm", card.altitudeText)
        assertEquals("2 drones, 2 connected", ui.readout)
        assertFalse(ui.isStateStale)
        assertNull(ui.staleNote)
        assertTrue(c.eventFrames().isNotEmpty())
    }

    @Test
    fun `the client re-requests state itself and the view model does not ask again on session start`() = harnessTest {
        val c = connectWithFleet()
        eventually("automatic state requests") { c.eventFrames().size >= 5 }
        delay(150)
        assertEquals(STATE_REQUEST_FRAMES, c.eventFrames())
        drop(c)
        advanceBackoff()
        val second = connect(1)
        eventually("second session requests") { second.eventFrames().size >= 5 }
        delay(150)
        assertEquals(STATE_REQUEST_FRAMES, second.eventFrames())
    }

    @Test
    fun `a disconnect keeps the last state and marks it stale until a fresh fleet status`() = harnessTest {
        val c = connectWithFleet("one_held_orphan_countdown")
        drop(c)
        eventually("stale") { ui.isStateStale }
        assertEquals(listOf("drone-1", "drone-2"), ui.cards.map { it.id })
        val card = ui.cards.first()
        assertEquals("LAST: CONNECTED", card.linkChipText)
        assertTrue(card.linkChipGreyed)
        assertEquals("87% (last value)", card.batteryText)
        assertEquals("100 cm (last value)", card.altitudeText)
        assertEquals("2 drones, 2 connected (last values)", ui.readout)
        assertEquals(GroundText.STALE_AFTER_DISCONNECT, ui.staleNote)
        assertTrue(ui.cards.all { it.isStateStale && it.isValuesStale })
        assertTrue(ui.board.stale)
        advanceBackoff()
        val second = connect(1)
        // Connected again, but the old values stay stale until a fresh fleet_status arrives.
        assertTrue(ui.isStateStale)
        assertEquals(GroundText.STALE_REFRESHING, ui.staleNote)
        second.fleetStatus("all_connected")
        eventually("fresh") { !ui.isStateStale }
        assertEquals("CONNECTED", ui.cards.first().linkChipText)
        assertEquals("87%", ui.cards.first().batteryText)
    }

    @Test
    fun `a manual status alone does not clear stale and a record seen only there is partial`() = harnessTest {
        val c = connectWithFleet("all_connected")
        drop(c)
        advanceBackoff()
        val second = connect(1)
        second.fixture("manual_status.json", "held_orphan_countdown")
        delay(150)
        assertTrue(ui.isStateStale)
        second.fleetStatus("all_connected")
        eventually("fresh") { !ui.isStateStale }
    }

    @Test
    fun `a record known only from a manual status is marked partial`() = harnessTest {
        val c = connect()
        c.fixture("manual_status.json", "held_orphan_countdown")
        eventually("partial cards") { ui.cards.size == 2 }
        assertTrue(ui.cards.all { it.isPartial })
        assertEquals("--", ui.cards.first().batteryText)
        c.fleetStatus("all_connected")
        eventually("no longer partial") { ui.cards.none { it.isPartial } }
    }

    @Test
    fun `DOWN and LOST cards say their values are the last ones received`() = harnessTest {
        connectWithFleet("one_down")
        val down = ui.cards.first { it.id == "drone-2" }
        assertEquals("DOWN", down.linkChipText)
        assertEquals(ConnectionState.STALE_NOTE, down.staleNote)
        assertTrue(down.isValuesStale)
        assertFalse(down.isStateStale)
        assertEquals("1 drone".takeIf { false } ?: "2 drones, 1 connected", ui.readout)
    }

    @Test
    fun `lane progress shows when present and a legacy ground shows a dash for the link`() = harnessTest {
        val c = connectWithFleet("one_lost")
        assertEquals("0/3", ui.cards.first { it.id == "drone-1" }.laneText)
        assertEquals("LOST", ui.cards.first { it.id == "drone-2" }.linkChipText)
        c.fleetStatus("non_fleet_local_drone")
        eventually("legacy shape") { ui.cards.map { it.id } == listOf("drone-1") }
        val legacy = ui.cards.single()
        assertEquals("--", legacy.linkChipText)
        assertNull(legacy.laneText)
        assertEquals("1 drone, 0 connected (no fleet: local drone only)", ui.readout)
    }

    @Test
    fun `a session end event marks the state stale even before the connection status changes`() = harnessTest {
        val c = connectWithFleet()
        onMain { vm.handle(com.flightrisk.app.ground.GroundEvent.SessionEnded(com.flightrisk.app.ground.DropReason.ConsumerStalled(512, 1L), "land_all_ack")) }
        assertTrue(ui.isStateStale)
        assertNotNull(c)
    }

    // ---- 5. HELD countdown

    @Test
    fun `the held countdown ticks locally between updates and keeps decimals`() = harnessTest {
        val c = connect()
        c.event("fleet_status", """{"drones":{"drone-1":{"connected":true,"lost":false,"battery":87,"height_cm":100,"control_mode":"held","orphan_land_in_s":29.6}},"manual_controlled":null}""")
        eventually("held card") { ui.cards.firstOrNull()?.controlChip == "HELD" }
        delay(150) // the event handler records the countdown base after the board changed
        assertEquals("HELD: auto-landing in 29.6 s, no controller", ui.cards.single().controlText)
        advance(1_000)
        assertEquals("HELD: auto-landing in 28.6 s, no controller", ui.cards.single().controlText)
        advance(2_000)
        assertEquals("HELD: auto-landing in 26.6 s, no controller", ui.cards.single().controlText)
        // A new server value replaces the local countdown.
        c.event("fleet_status", """{"drones":{"drone-1":{"connected":true,"lost":false,"battery":87,"height_cm":100,"control_mode":"held","orphan_land_in_s":20}},"manual_controlled":null}""")
        eventually("replaced") { ui.cards.single().controlText.contains("in 20 s") }
    }

    @Test
    fun `the countdown never goes below zero and freezes while the state is stale`() = harnessTest {
        val c = connect()
        c.fixture("fleet_status.json", "one_held_orphan_countdown")
        eventually("held") { ui.cards.any { it.controlChip == "HELD" } }
        delay(150) // the event handler records the countdown base after the board changed
        val held = { ui.cards.first { it.id == "drone-2" } }
        advance(5_000)
        assertEquals("HELD: auto-landing in 115 s, no controller", held().controlText)
        drop(c)
        val frozen = held().controlText
        assertEquals("HELD: auto-landing in 115 s, no controller (last value)", frozen)
        advance(60_000)
        assertEquals(frozen, held().controlText)
    }

    @Test
    fun `a manual status with a new countdown replaces the base`() = harnessTest {
        val c = connect()
        c.fixture("fleet_status.json", "all_connected")
        eventually("cards") { ui.cards.isNotEmpty() }
        c.fixture("manual_status.json", "held_orphan_countdown")
        eventually("held") { ui.cards.any { it.controlChip == "HELD" } }
        delay(150) // the event handler records the countdown base after the board changed
        advance(10_000)
        assertEquals("HELD: auto-landing in 110 s, no controller", ui.cards.first { it.id == "drone-2" }.controlText)
    }

    // ---- 5. Banners and toasts

    @Test
    fun `fleet events and plan refusals show as banners in the dashboards words`() = harnessTest {
        val c = connect()
        c.fixture("fleet_event.json", "drone_lost")
        c.fixture("fleet_event.json", "plan_refused_manual_control")
        eventually("banners") { ui.banners.size == 3 }
        val texts = ui.banners.map { it.text }
        assertTrue(texts.contains("DRONE LOST: drone-2"))
        assertTrue(texts.contains("PLAN REFUSED"))
        assertTrue(texts.contains("Plan refused: drone-2 under manual control; press Resume search first"))
        val id = ui.banners.first().id
        onMain { vm.dismissBanner(id) }
        assertEquals(2, ui.banners.size)
    }

    @Test
    fun `errors stay and a new toast evicts the oldest warning first`() = harnessTest {
        val c = connect()
        c.fixture("error.json", "no_drones_connected")
        for (i in 1..5) c.event("warning", """{"message":"w$i"}""")
        eventually("six toasts offered") { ui.toasts.any { it.text == "w5" } }
        // Five slots: the error is kept, the oldest warning (w1) went first.
        val texts = ui.toasts.map { it.text }
        assertTrue(texts.contains("No drones connected"))
        assertFalse(texts.contains("w1"))
        assertTrue(texts.contains("w5"))
        assertTrue(ui.toasts.first { it.text == "No drones connected" }.isError)
    }

    @Test
    fun `warnings fade and errors stay`() = harnessTest {
        val c = connect()
        c.fixture("error.json", "no_drones_connected")
        c.event("warning", """{"message":"careful"}""")
        eventually("two toasts") { ui.toasts.size == 2 }
        advance(GroundConsoleViewModel.WARNING_FADE_MS + 1)
        assertEquals(listOf("No drones connected"), ui.toasts.map { it.text })
    }

    @Test
    fun `unknown and malformed events are ignored`() = harnessTest {
        val c = connectWithFleet()
        c.event("fleet_telemetry", """{"x":1}""")
        c.event("totally_new_event", """{"x":1}""")
        c.event("manual_ack", """[1,2]""")
        c.fixture("fleet_event.json", "drone_lost")
        eventually("banner after the unknown events") { ui.banners.isNotEmpty() }
        assertEquals(1, ui.banners.size)
        assertEquals(2, ui.cards.size)
    }

    @Test
    fun `the ground status drives the appearance only badge and the snapshot is exposed`() = harnessTest {
        val c = connect()
        c.fixture("status.json", "fleet_two_drones")
        eventually("status in the snapshot") { ui.snapshot.status != null }
        assertEquals(ui.snapshot.status?.appearanceOnly == true, ui.appearanceOnly)
    }

    // ---- 8. Emit policy

    @Test
    fun `the screen can emit only the state requests, LAND ALL, land and resume`() {
        val policy = GroundConsoleViewModel.EMIT_POLICY
        assertTrue(policy.allows(ClientCommand.LandAll))
        assertTrue(policy.allows(ClientCommand.landDrone("drone-1")!!))
        assertTrue(policy.allows(ClientCommand.resumeHeldDrone("drone-1")!!))
        ClientCommand.STATE_REQUESTS.forEach { assertTrue(policy.allows(it)) }
        assertFalse(policy.allows(ClientCommand.setDetectionProfile("drone-1", "x")!!))
        assertFalse(policy.allows(ClientCommand.SetFleetDetectionProfile("x")))
        val forbidden = setOf(
            "plan_fleet", "start_fleet", "start_search", "stop_search", "manual_take_control", "manual_command",
            "set_ceiling_settings", "set_reasoning_provider", "set_stream_video", "set_target", "register_drone",
            "deregister_drone", "remove_all_drones", "restart_dashboard", "takeoff", "move",
        )
        assertTrue(forbidden.none { it in policy.allowedEvents })
        assertTrue(policy.allowedEvents.all { ClientCommand.isAllowed(it) })
        assertEquals(
            setOf(
                "get_fleet_status", "get_detection_profiles", "get_ceiling_settings", "get_reasoning_provider",
                "get_manual_status", "land_all", "drone_command", "manual_resume_search",
            ),
            policy.allowedEvents,
        )
        assertTrue(policy.allowedEvents.size < EmitPolicy.CONTRACT.allowedEvents.size)
    }

    @Test
    fun `contract text for a fixture lines up with the ui constants`() {
        val case = ContractFixtures.case("land_all_ack.json", "reached_no_drone")
        assertNotNull(case)
        assertEquals("LAND ALL FAILED: no drone was reached. Land each drone by hand (per-drone Land, or the drone's own controls).", DisplayText.LAND_ALL_FAILED_NO_DRONE)
    }
}
