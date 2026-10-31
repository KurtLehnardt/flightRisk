package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.GroundEvent
import com.flightrisk.app.ground.eventually
import kotlinx.coroutines.delay
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Per-drone Land and Resume: always possible for Land, offered for Resume only when the ground allows it. */
class GroundDroneCommandsTest {

    private fun landFrame(id: String) = "42[\"drone_command\",{\"command\":\"land\",\"drone_id\":\"$id\"}]"
    private fun resumeFrame(id: String) = "42[\"manual_resume_search\",{\"drone_id\":\"$id\"}]"

    private fun GroundHarness.card(id: String) = ui.cards.first { it.id == id }

    // ---- Land

    @Test
    fun `Land is sent through the land command and handed over until the ground answers`() = harnessTest {
        val c = connectWithFleet()
        vm.landDrone("drone-1").join()
        eventually("frame") { c.commandFrames() == listOf(landFrame("drone-1")) }
        assertEquals(CommandPhase.HANDED_OVER, card("drone-1").landFeedback!!.phase)
        assertEquals("drone-1: Land sending… Handed to the connection; waiting for the ground's reply.", card("drone-1").landFeedbackText)
        assertNull(card("drone-2").landFeedback)
    }

    @Test
    fun `Land with the drone link down is offered with the contract text and is sent`() = harnessTest {
        val c = connectWithFleet("drone_link_down")
        val card = card("drone-2")
        assertEquals("Drone link down: Tello unreachable (no state for 12 s). Land still works.", card.linkNotice)
        assertEquals("Land", card.landTitle)
        vm.landDrone("drone-2").join()
        eventually("frame") { c.commandFrames() == listOf(landFrame("drone-2")) }
    }

    @Test
    fun `Land works on a DOWN and a LOST drone and on a held drone`() = harnessTest {
        val c = connectWithFleet("one_down")
        vm.landDrone("drone-2").join()
        c.fleetStatus("one_lost")
        eventually("lost shown") { card("drone-2").connection.name == "LOST" }
        vm.landDrone("drone-2").join()
        c.fleetStatus("one_held_orphan_countdown")
        eventually("held shown") { card("drone-2").controlChip == "HELD" }
        vm.landDrone("drone-2").join()
        eventually("three frames") { c.commandFrames().size == 3 }
        assertTrue(c.commandFrames().all { it == landFrame("drone-2") })
    }

    @Test
    fun `Land with no connection says it was not sent`() = harnessTest {
        val c = connectWithFleet()
        drop(c)
        vm.landDrone("drone-1").join()
        val f = card("drone-1").landFeedback!!
        assertEquals(CommandPhase.FAILED, f.phase)
        assertEquals(
            "drone-1: Land was NOT sent: this phone is not connected to the ground. Land it with its own controls, or use the ground laptop.",
            f.text,
        )
        assertEquals("Land again", card("drone-1").landTitle)
    }

    @Test
    fun `a drone without a usable id is refused with a plain text and nothing is sent`() = harnessTest {
        val c = connect()
        c.event("fleet_status", """{"drones":{" ":{"connected":true,"lost":false,"battery":50}},"manual_controlled":null}""")
        eventually("blank id card") { ui.cards.size == 1 }
        vm.landDrone(" ").join()
        val f = ui.cards.single().landFeedback!!
        assertEquals("Land was not sent: this drone has no usable id. Land it with its own controls.", f.text)
        assertEquals(CommandPhase.FAILED, f.phase)
        delay(100)
        assertTrue(c.commandFrames().isEmpty())
    }

    @Test
    fun `accepted by the ground with a final answer shows success`() = harnessTest {
        val c = connectWithFleet()
        vm.landDrone("drone-1").join()
        c.fixture("manual_ack.json", "land_ok")
        eventually("accepted") { card("drone-1").landFeedback?.phase == CommandPhase.ACCEPTED }
        assertEquals("drone-1: Land accepted by the ground; waiting for the drone's edge.", card("drone-1").landFeedback!!.text)
        c.event("manual_result", """{"action":"land","detail":null,"drone_id":"drone-1","ok":true,"reason":null}""")
        eventually("succeeded") { card("drone-1").landFeedback?.phase == CommandPhase.SUCCEEDED }
        assertEquals("drone-1: Land acknowledged by the drone's edge.", card("drone-1").landFeedback!!.text)
        advance(GroundConsoleViewModel.DRONE_SUCCESS_FADE_MS + 1)
        assertNull(card("drone-1").landFeedback)
    }

    @Test
    fun `accepted with no final answer turns into a sticky failure after nine seconds`() = harnessTest {
        val c = connectWithFleet()
        vm.landDrone("drone-1").join()
        c.fixture("manual_ack.json", "land_ok")
        eventually("accepted") { card("drone-1").landFeedback?.phase == CommandPhase.ACCEPTED }
        advance(GroundConsoleViewModel.REPLY_TIMEOUT_MS + GroundConsoleViewModel.MANUAL_REPLY_TIMEOUT_MS - 1)
        assertEquals(CommandPhase.ACCEPTED, card("drone-1").landFeedback!!.phase)
        advance(1)
        val f = card("drone-1").landFeedback!!
        assertEquals(CommandPhase.FAILED, f.phase)
        assertEquals("drone-1: no final answer from the drone's edge. Land it with its own controls.", f.text)
        assertEquals("Land again", card("drone-1").landTitle)
        advance(10 * 60_000L)
        assertEquals(CommandPhase.FAILED, card("drone-1").landFeedback!!.phase)
        // Land again is just another land command.
        vm.landDrone("drone-1").join()
        eventually("second frame") { c.commandFrames().size == 2 }
    }

    @Test
    fun `not answered at all becomes no reply from the ground yet after five seconds`() = harnessTest {
        connectWithFleet()
        vm.landDrone("drone-1").join()
        advance(5_000)
        val f = card("drone-1").landFeedback!!
        assertEquals(CommandPhase.NO_REPLY_YET, f.phase)
        assertEquals("drone-1: Land: no reply from the ground yet. Check the connection.", f.text)
        assertTrue(f.offersRetry)
    }

    @Test
    fun `a refused land of the phone shows the grounds reason on the card`() = harnessTest {
        val c = connectWithFleet()
        vm.landDrone("drone-1").join()
        c.event("manual_ack", """{"action":"land","detail":"drone-1 is busy landing","drone_id":"drone-1","ok":false,"reason":"landing"}""")
        eventually("failed") { card("drone-1").landFeedback?.phase == CommandPhase.FAILED }
        assertEquals("drone-1: drone-1 is busy landing. Land the drone with its own controls.", card("drone-1").landFeedback!!.text)
    }

    @Test
    fun `a land_no_reply result is a failure with the grounds detail`() = harnessTest {
        val c = connectWithFleet()
        vm.landDrone("drone-2").join()
        c.fixture("manual_result.json", "land_no_reply")
        eventually("failed") { card("drone-2").landFeedback?.phase == CommandPhase.FAILED }
        assertTrue(card("drone-2").landFeedback!!.text.contains("no answer from drone-2 within 4 s"))
    }

    @Test
    fun `any failed manual reply shows on the drones card until dismissed, an auto land included`() = harnessTest {
        val c = connectWithFleet()
        c.event("manual_result", """{"action":"auto_land","detail":null,"drone_id":"drone-2","ok":false,"reason":"drone_unreachable"}""")
        eventually("alert") { card("drone-2").alert != null }
        assertEquals("drone-2: auto_land failed (drone unreachable). Land it with its own controls.", card("drone-2").alertText)
        advance(10 * 60_000L)
        assertNotNull(card("drone-2").alert)
        assertNull(card("drone-1").alert)
        onMain { vm.dismissDroneAlert("drone-2") }
        assertNull(card("drone-2").alert)
    }

    @Test
    fun `a failed manual ack of another action with a detail shows the detail`() = harnessTest {
        val c = connectWithFleet()
        c.event("manual_ack", """{"action":"take_control","detail":"drone-1 is not under manual control","drone_id":"drone-1","ok":false,"reason":"not_manual"}""")
        eventually("alert") { card("drone-1").alert != null }
        assertEquals("drone-1: take_control failed (drone-1 is not under manual control). Land it with its own controls.", card("drone-1").alertText)
    }

    @Test
    fun `a failed reply for a drone without a card shows as a failure banner`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("manual_ack.json", "land_unknown_drone")
        eventually("banner") { ui.banners.isNotEmpty() }
        assertTrue(ui.banners.first().isFailure)
        assertTrue(ui.banners.first().text.startsWith("drone-9: land failed"))
    }

    @Test
    fun `a successful land result shows on the card even when the laptop asked for it`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("manual_result.json", "land_acked")
        eventually("shown") { card("drone-1").landFeedback?.phase == CommandPhase.SUCCEEDED }
        assertEquals("drone-1: Land acknowledged by the drone's edge.", card("drone-1").landFeedback!!.text)
        assertTrue(ui.banners.isEmpty())
        assertTrue(c.commandFrames().isEmpty())
    }

    @Test
    fun `a legacy command ack answers the oldest outstanding land`() = harnessTest {
        val c = connectWithFleet("non_fleet_local_drone")
        vm.landDrone("drone-1").join()
        eventually("sent") { c.commandFrames().size == 1 }
        c.fixture("command_ack.json", "land")
        eventually("accepted") { ui.cards.single().landFeedback?.phase == CommandPhase.ACCEPTED }
        assertEquals("drone-1: Land accepted by the ground (no confirmation from the drone).", ui.cards.single().landFeedback!!.text)
        // A ground without a fleet never sends a final answer, so this is not turned into a failure.
        advance(60_000)
        assertEquals(CommandPhase.ACCEPTED, ui.cards.single().landFeedback!!.phase)
    }

    @Test
    fun `a command ack with nothing outstanding is ignored`() = harnessTest {
        val c = connectWithFleet("non_fleet_local_drone")
        c.fixture("command_ack.json", "land")
        delay(150)
        assertNull(ui.cards.single().landFeedback)
    }

    @Test
    fun `a link lost while a land waits becomes a visible failure`() = harnessTest {
        val c = connectWithFleet()
        vm.landDrone("drone-1").join()
        vm.landDrone("drone-2").join()
        c.fixture("manual_ack.json", "land_ok") // drone-1 accepted, final answer pending
        eventually("accepted") { card("drone-1").landFeedback?.phase == CommandPhase.ACCEPTED }
        drop(c)
        eventually("failures") { card("drone-1").landFeedback?.phase == CommandPhase.FAILED && card("drone-2").landFeedback?.phase == CommandPhase.FAILED }
        assertEquals("drone-1: connection lost before the answer; land it with its own controls.", card("drone-1").landFeedback!!.text)
        assertEquals("drone-2: connection lost before the answer; land it with its own controls.", card("drone-2").landFeedback!!.text)
    }

    @Test
    fun `Land stays tappable on every card in every state`() = harnessTest {
        connectWithFleet("one_lost")
        assertTrue(ui.cards.all { it.landTitle.startsWith("Land") })
        onMain { vm.onBackground() }
        // Cards are kept (stale) and each one still offers Land.
        assertEquals(2, ui.cards.size)
        vm.landDrone("drone-1").join()
        assertEquals(CommandPhase.FAILED, card("drone-1").landFeedback!!.phase)
    }

    // ---- Resume

    @Test
    fun `Resume is offered for a held drone nobody controls and sent through the resume command`() = harnessTest {
        val c = connectWithFleet("one_held_orphan_countdown")
        assertTrue(card("drone-2").canResume)
        assertFalse(card("drone-1").canResume)
        vm.resumeHeldDrone("drone-2").join()
        eventually("frame") { c.commandFrames() == listOf(resumeFrame("drone-2")) }
        c.fixture("manual_ack.json", "resume_search_ok")
        eventually("accepted") { card("drone-2").resumeFeedback?.phase == CommandPhase.ACCEPTED }
        c.fixture("manual_result.json", "resume_search_acked")
        eventually("resumed") { card("drone-2").resumeFeedback?.phase == CommandPhase.SUCCEEDED }
        assertEquals("drone-2: search resumed.", card("drone-2").resumeFeedback!!.text)
    }

    @Test
    fun `Resume is hidden while the laptop controls a drone`() = harnessTest {
        connectWithFleet("one_manual")
        assertTrue(ui.cards.none { it.canResume })
    }

    @Test
    fun `Resume is hidden while the state is stale`() = harnessTest {
        val c = connectWithFleet("one_held_orphan_countdown")
        assertTrue(card("drone-2").canResume)
        drop(c)
        assertFalse(card("drone-2").canResume)
        advanceBackoff()
        val second = connect(1)
        assertFalse(card("drone-2").canResume)
        second.fleetStatus("one_held_orphan_countdown")
        eventually("resume back") { card("drone-2").canResume }
    }

    @Test
    fun `Resume is hidden when the drones own link is down`() = harnessTest {
        val c = connect()
        c.event("fleet_status", """{"drones":{"drone-2":{"connected":false,"lost":false,"control_mode":"held","edge_control_mode":"held","orphan_land_in_s":90}},"manual_controlled":null}""")
        eventually("card") { ui.cards.isNotEmpty() }
        assertFalse(card("drone-2").canResume)
        onMain { assertFalse(vm.canResume("drone-2")) }
        vm.resumeHeldDrone("drone-2").join()
        delay(100)
        assertTrue(c.commandFrames().isEmpty())
    }

    @Test
    fun `a resume that is not offered sends nothing and says why`() = harnessTest {
        val c = connectWithFleet("all_connected")
        vm.resumeHeldDrone("drone-1").join()
        delay(100)
        assertTrue(c.commandFrames().isEmpty())
        // Nothing is sent, and the tap is answered: it never does nothing silently.
        val f = card("drone-1").resumeFeedback!!
        assertEquals(CommandPhase.FAILED, f.phase)
        assertEquals("drone-1: Resume search was not sent: the ground no longer reports this drone as held.", f.text)
    }

    @Test
    fun `a refused resume shows the grounds reason`() = harnessTest {
        val c = connectWithFleet("one_held_orphan_countdown")
        vm.resumeHeldDrone("drone-2").join()
        c.event("manual_ack", """{"action":"resume_search","detail":"drone-2 is not under manual control","drone_id":"drone-2","ok":false,"reason":"not_manual"}""")
        eventually("failed") { card("drone-2").resumeFeedback?.phase == CommandPhase.FAILED }
        assertEquals("drone-2: drone-2 is not under manual control.", card("drone-2").resumeFeedback!!.text)
    }

    // ---- Nothing else reaches the wire

    @Test
    fun `only LAND ALL, land, resume and the state requests are ever written`() = harnessTest {
        val c = connectWithFleet("one_held_orphan_countdown")
        vm.landAll().join()
        vm.landDrone("drone-1").join()
        vm.resumeHeldDrone("drone-2").join()
        eventually("three commands") { c.commandFrames().size == 3 }
        val events = c.eventFrames().map { it.removePrefix("42[\"").substringBefore("\"") }.toSet()
        assertEquals(
            setOf("get_fleet_status", "get_detection_profiles", "get_ceiling_settings", "get_reasoning_provider", "get_manual_status",
                "land_all", "drone_command", "manual_resume_search"),
            events,
        )
        // The only drone_command ever written is a land.
        assertTrue(c.eventFrames().filter { it.contains("drone_command") }.all { it.contains("\"command\":\"land\"") })
    }

    @Test
    fun `a session start does not make the view model request state itself`() = harnessTest {
        val c = connectWithFleet()
        eventually("the clients own requests") { c.eventFrames().size >= 5 }
        delay(100)
        val before = c.eventFrames().size
        onMain { vm.handle(GroundEvent.SessionStarted(com.flightrisk.app.ground.SessionInfo(9, 1, 1))) }
        delay(150)
        assertEquals(before, c.eventFrames().size)
        assertTrue(ui.isStateStale)
    }
}
