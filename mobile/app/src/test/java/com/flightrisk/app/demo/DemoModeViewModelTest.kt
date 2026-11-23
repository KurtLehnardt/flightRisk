package com.flightrisk.app.demo

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.app.ground.eventually
import com.flightrisk.app.ui.ground.CommandPhase
import com.flightrisk.app.ui.ground.DemoFixtures
import com.flightrisk.app.ui.ground.GroundConsoleViewModel
import com.flightrisk.app.ui.ground.GroundConsoleViewModel.DemoNotice
import com.flightrisk.app.ui.ground.GroundConsoleViewModel.DemoRequest
import com.flightrisk.app.ui.ground.GroundHarness
import com.flightrisk.app.ui.ground.GroundLink
import com.flightrisk.app.ui.ground.GroundText
import com.flightrisk.app.ui.ground.KeyState
import com.flightrisk.app.ui.ground.harnessTest
import com.flightrisk.contract.VerdictKind
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Demo mode on the Ground console: it never reaches the network, it refuses while the real ground needs
 * the operator, it asks before it switches a real link off, it leaves that link off, and nothing it shows
 * survives it or mixes with a real ground's data.
 */
class DemoModeViewModelTest {

    private val host = "127.0.0.1:5555"

    /** Moves the demo replay on by [ms], one second at a time. */
    private suspend fun GroundHarness.play(ms: Long) {
        var left = ms
        while (left > 0) {
            val step = minOf(1_000L, left)
            demoClock.advance(step)
            left -= step
            delay(3)
        }
    }

    /** Moves the demo replay and the ViewModel's own clock on together, ticking each second, as the app does. */
    private suspend fun GroundHarness.playLive(ms: Long) {
        var left = ms
        while (left > 0) {
            demoClock.advance(1_000)
            delay(3)
            advance(1_000)
            left -= 1_000
        }
    }

    private suspend fun GroundHarness.startDemo(confirmed: Boolean = true) {
        vm.enterDemo(disconnectConfirmed = confirmed).join()
        eventually("demo connected") { ui.demo && ui.link == GroundLink.Connected }
        eventually("demo fleet shown") { ui.cards.size == 2 && !ui.isStateStale }
    }

    private fun GroundHarness.card(id: String) = ui.cards.first { it.id == id }

    /**
     * The real transport here fails the test when it is touched: every real client and every health probe
     * throws and is counted. With a real ground set up (address and key saved), demo mode runs its whole
     * command set and every settings action, switching the link on included, and the count stays at zero.
     */
    @Test
    fun `demo commands never touch the network`() = harnessTest(demo = true, enabled = false, forbidRealNetwork = true) {
        startDemo()
        foreground()
        play(8_000)
        eventually("drones flying") { ui.cards.all { it.controlText == "Flying" } }

        vm.landDrone("demo-drone-1").join()
        eventually("land acknowledged") { card("demo-drone-1").landFeedback?.phase == CommandPhase.SUCCEEDED }
        vm.landAll().join()
        eventually("LAND ALL acknowledged") { ui.landAll?.phase == CommandPhase.SUCCEEDED }
        eventually("every drone on the ground") { ui.cards.all { it.controlText == "On the ground" } }
        vm.resumeHeldDrone("demo-drone-2").join()
        onMain { vm.selectFleetProfile("level") }
        onMain { vm.testConnection() }
        eventually("test refused in demo") { ui.connection.testResult == DemoText.TEST_IN_DEMO }
        onMain { vm.retryConnection() }
        onMain { vm.setEnabled(false) }
        onMain { vm.setEnabled(true) }
        assertEquals(DemoText.LINK_ON_IN_DEMO, ui.connection.message)
        assertFalse("the link stays off in demo mode", ui.connection.enabled)
        onMain { vm.saveAddress("192.168.1.30:5555") }
        onMain { vm.saveKey("another-test-key") }
        vm.awaitIdle()
        onMain { vm.onBackground() }
        foreground()
        play(2_000)
        vm.awaitIdle()

        assertEquals("a real client or probe was used in demo mode", 0, realNetworkCalls.get())
        assertTrue(ui.demo)
        assertFalse(settings.enabled)
        // One demo session the whole time: the settings actions did not restart anything.
        val session = demoPorts.single().connections.single()
        assertEquals(listOf("drone_command", "land_all"), session.acknowledged)
        assertTrue(ui.toasts.toString(), ui.toasts.any { it.text == "DEMO: " + DemoText.LAND_ALL_ACKNOWLEDGED })
        assertTrue(ui.toasts.any { it.text == "DEMO: " + DemoText.landAcknowledged("demo-drone-1") })
        // Every result the demo shows says it is a demo.
        assertTrue(ui.landAllText!!.startsWith("DEMO: "))
        assertTrue(ui.landAll!!.text.startsWith("DEMO: "))
        assertTrue(card("demo-drone-1").landFeedbackText!!.startsWith("DEMO: "))
        assertTrue(card("demo-drone-2").resumeFeedbackText!!.startsWith("DEMO: "))
        assertTrue((ui.banners.map { it.text } + ui.toasts.map { it.text }).all { it.startsWith("DEMO: ") })
    }

    @Test
    fun `starting the demo over a real link needs a confirmation, then switches the link off and closes it`() = harnessTest(demo = true) {
        val real = connectWithFleet()
        real.fixture("match_alert.json", "edge_match")
        eventually("real alert shown") { ui.alerts.rows.isNotEmpty() }

        assertEquals(DemoRequest.NeedsDisconnect(host), onMain { vm.demoRequest() })
        vm.enterDemo(disconnectConfirmed = false).join()
        assertFalse(ui.demo)
        assertEquals(DemoNotice.ConfirmDisconnect(host), ui.demoNotice)
        assertEquals(GroundLink.Connected, ui.link)
        assertFalse(real.closed)
        assertEquals(1, ui.alerts.rows.size)
        onMain { vm.dismissDemoNotice() }
        assertNull(ui.demoNotice)

        vm.enterDemo(disconnectConfirmed = true).join()
        eventually("real link closed") { real.closed }
        eventually("demo connected") { ui.demo && ui.link == GroundLink.Connected }
        vm.awaitIdle()
        assertFalse("the link switch-off is saved", settings.enabled)
        assertFalse(ui.connection.enabled)
        // Nothing of the real ground stays, and a late real event cannot reach the demo screen.
        assertTrue(ui.alerts.rows.isEmpty())
        real.fixture("match_alert.json", "edge_match")
        play(1_000)
        assertTrue(ui.alerts.rows.isEmpty())
        assertEquals(DemoRequest.AlreadyOn, onMain { vm.demoRequest() })
    }

    @Test
    fun `a confirmation is asked whenever real ground state exists, even with no client`() = harnessTest(demo = true, enabled = false) {
        // The link is off and nothing was ever connected: nothing to switch off or clear.
        assertEquals(DemoRequest.Ready, onMain { vm.demoRequest() })
        // The link switched on and connected, and the ground reported its drones: starting the demo would
        // switch the link off, so it asks.
        onMain { vm.setEnabled(true) }
        val real = connectWithFleet()
        assertEquals(DemoRequest.NeedsDisconnect(host), onMain { vm.demoRequest() })
        // The link off again, but a real ground's report is still on screen.
        real.fixture("match_alert.json", "edge_match")
        eventually("alert") { ui.alerts.rows.isNotEmpty() }
        onMain { vm.setEnabled(false) }
        eventually("closed") { real.closed }
        assertEquals(DemoRequest.NeedsDisconnect(host), onMain { vm.demoRequest() })
        vm.enterDemo(disconnectConfirmed = false).join()
        assertFalse(ui.demo)
        assertEquals(DemoNotice.ConfirmDisconnect(host), ui.demoNotice)
        assertEquals(1, ui.alerts.rows.size)
    }

    @Test
    fun `the demo is refused while a real drone flies, with the link on`() = harnessTest(demo = true) {
        val real = connectWithFleet("one_held_orphan_countdown")
        val reason = DemoText.blockedByFlying(listOf("drone-1", "drone-2"))
        assertEquals(DemoRequest.Blocked(reason), onMain { vm.demoRequest() })
        // Even with the user's confirmation: the refusal is checked again right before the demo starts.
        vm.enterDemo(disconnectConfirmed = true).join()
        assertFalse(ui.demo)
        assertEquals(DemoNotice.Blocked(reason), ui.demoNotice)
        // Nothing of the live safety state was cleared, and the link is untouched.
        assertFalse(real.closed)
        assertEquals(GroundLink.Connected, ui.link)
        assertEquals(2, ui.cards.size)
        vm.awaitIdle()
        assertTrue(settings.enabled)
        assertTrue(demoPorts.isEmpty())
    }

    @Test
    fun `the demo is refused while a real drone reads as flying, with the link off`() = harnessTest(demo = true) {
        val real = connectWithFleet("one_held_orphan_countdown")
        onMain { vm.setEnabled(false) }
        eventually("link closed") { real.closed && ui.link == GroundLink.Off }
        // The last values (stale) still say the drones fly: the demo must not hide them.
        assertTrue(ui.isStateStale)
        assertEquals(2, ui.cards.size)
        val reason = DemoText.blockedByFlying(listOf("drone-1", "drone-2"))
        assertEquals(DemoRequest.Blocked(reason), onMain { vm.demoRequest() })
        vm.enterDemo(disconnectConfirmed = true).join()
        assertFalse(ui.demo)
        assertEquals(DemoNotice.Blocked(reason), ui.demoNotice)
        assertEquals(2, ui.cards.size)
        assertTrue(demoPorts.isEmpty())
    }

    @Test
    fun `the demo is refused while a LAND ALL waits or failed, or a failure banner shows`() = harnessTest(demo = true) {
        val real = connectWithFleet()
        vm.landAll().join()
        eventually("LAND ALL waiting") { ui.landAll?.isWaiting == true }
        assertEquals(DemoRequest.Blocked(DemoText.BLOCKED_BY_LAND_ALL_WAITING), onMain { vm.demoRequest() })
        vm.enterDemo(disconnectConfirmed = true).join()
        assertFalse(ui.demo)
        assertEquals(DemoNotice.Blocked(DemoText.BLOCKED_BY_LAND_ALL_WAITING), ui.demoNotice)
        assertTrue("the LAND ALL result stays", ui.landAll?.isWaiting == true)

        // The ground answers that LAND ALL failed: still refused until it is dismissed.
        real.fixture("land_all_ack.json", "reached_no_drone")
        eventually("LAND ALL failed") { ui.landAll?.phase == CommandPhase.FAILED }
        assertEquals(DemoRequest.Blocked(DemoText.BLOCKED_BY_LAND_ALL_FAILED), onMain { vm.demoRequest() })
        onMain { vm.dismissLandAll() }
        // A failure banner from the ground keeps it refused as well.
        if (ui.banners.none { it.isFailure }) real.fixture("fleet_event.json", "start_refused")
        eventually("failure banner") { ui.banners.any { it.isFailure } }
        assertEquals(DemoRequest.Blocked(DemoText.BLOCKED_BY_FAILURE), onMain { vm.demoRequest() })
        for (banner in ui.banners) onMain { vm.dismissBanner(banner.id) }
        assertEquals(DemoRequest.NeedsDisconnect(host), onMain { vm.demoRequest() })
    }

    @Test
    fun `a real link that connects after the app asked is not dropped silently`() = harnessTest(demo = true, enabled = false) {
        assertEquals(DemoRequest.Ready, onMain { vm.demoRequest() })
        // Before the app calls enterDemo, the user switches the link on and it connects.
        onMain { vm.setEnabled(true) }
        val real = connect()
        real.fleetStatus()
        eventually("fleet shown") { ui.cards.isNotEmpty() }
        vm.enterDemo(disconnectConfirmed = false).join()
        assertFalse(ui.demo)
        assertEquals("the app shows the disconnect dialog", DemoNotice.ConfirmDisconnect(host), ui.demoNotice)
        assertFalse(real.closed)
        assertEquals(GroundLink.Connected, ui.link)
    }

    @Test
    fun `the demo plays the mission through the verdict to the landing`() = harnessTest(demo = true, enabled = false, forbidRealNetwork = true) {
        startDemo()
        val alertAt = DemoFixtures.scenario.steps.first { it.event == "match_alert" }.atMs
        val verdictAt = DemoFixtures.scenario.steps.first { it.event == "alert_upgrade" }.atMs
        play(alertAt + 100)
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        assertEquals(VerdictKind.PENDING, ui.alerts.rows.single().verdict)
        assertEquals("demo-alert-1", ui.alerts.rows.single().id)
        assertTrue("the obstacle cut is announced", ui.banners.any { it.text == "DEMO: lane_blocked" })
        play(verdictAt - alertAt + 500)
        eventually("verdict shown") { ui.alerts.rows.single().verdict == VerdictKind.MATCH }
        assertEquals("Strong match", ui.alerts.rows.single().levelText)
        assertEquals("Track demo-track-1", ui.alerts.rows.single().trackText)
        play(DemoFixtures.scenario.durationMs - verdictAt + 1_000)
        eventually("landed") { ui.cards.all { it.controlText == "On the ground" } }
        assertTrue(ui.snapshot.fleetPlan != null)
        assertEquals(listOf("demo-drone-1", "demo-drone-2"), ui.cards.map { it.id })
        assertEquals(0, realNetworkCalls.get())
    }

    @Test
    fun `Resume search works on the held demo drone through the ViewModel`() = harnessTest(demo = true, enabled = false, forbidRealNetwork = true) {
        startDemo()
        val held = DemoFixtures.scenario.held!!
        assertEquals("demo-drone-2", held.droneId)
        play(held.fromMs + 1_000)
        eventually("held, with Resume offered") { card("demo-drone-2").controlChip == "HELD" && card("demo-drone-2").canResume }
        assertTrue(card("demo-drone-2").controlText.startsWith("HELD"))

        vm.resumeHeldDrone("demo-drone-2").join()
        eventually("resumed") { card("demo-drone-2").resumeFeedback?.phase == CommandPhase.SUCCEEDED }
        assertTrue(card("demo-drone-2").resumeFeedbackText!!.startsWith("DEMO: "))
        eventually("flying its plan again") { card("demo-drone-2").controlChip == null && card("demo-drone-2").controlText == "Flying" }
        // The later replayed statuses (which still say held) do not hold it again.
        play(8_000)
        assertNull(card("demo-drone-2").controlChip)
        assertEquals("Flying", card("demo-drone-2").controlText)
        assertTrue(ui.toasts.any { it.text == "DEMO: " + DemoText.resumeAcknowledged("demo-drone-2") })
        assertEquals(listOf("manual_resume_search"), demoPorts.single().connections.single().acknowledged)
        assertEquals(0, realNetworkCalls.get())
    }

    @Test
    fun `demo results do not fade and the demo state never goes not live`() = harnessTest(demo = true, enabled = false, forbidRealNetwork = true) {
        startDemo()
        play(8_000)
        vm.landDrone("demo-drone-1").join()
        eventually("land acknowledged") { card("demo-drone-1").landFeedback?.phase == CommandPhase.SUCCEEDED }
        vm.landAll().join()
        eventually("LAND ALL acknowledged") { ui.landAll?.phase == CommandPhase.SUCCEEDED }
        // Well past every fade time and the 30 s freshness limit, after the replay ended.
        playLive(DemoFixtures.scenario.durationMs + 90_000)
        assertFalse("the demo fleet is still live", ui.isStateStale)
        assertEquals(CommandPhase.SUCCEEDED, ui.landAll?.phase)
        assertEquals(CommandPhase.SUCCEEDED, card("demo-drone-1").landFeedback?.phase)
        assertTrue(ui.toasts.any { it.text == "DEMO: " + DemoText.LAND_ALL_ACKNOWLEDGED })
    }

    @Test
    fun `a profile selection in the demo is answered on the phone`() = harnessTest(demo = true, enabled = false, forbidRealNetwork = true) {
        startDemo()
        play(1_000)
        eventually("profiles shown") { ui.snapshot.detectionProfiles != null }
        onMain { vm.selectFleetProfile("level") }
        vm.awaitIdle()
        eventually("feedback") { ui.profileFeedback != null }
        assertTrue(ui.profileFeedback!!.text, ui.profileFeedback!!.text.startsWith("DEMO: "))
        assertEquals(0, realNetworkCalls.get())
    }

    @Test
    fun `removing the key in the demo keeps the demo and leaves the link off afterwards`() = harnessTest(demo = true, enabled = false, forbidRealNetwork = true) {
        startDemo()
        onMain { vm.removeKey() }
        vm.awaitIdle()
        assertTrue(ui.demo)
        assertEquals(2, ui.cards.size)
        assertEquals(KeyState.NONE, ui.connection.keyState)
        onMain { vm.exitDemo() }
        foreground()
        assertEquals(GroundLink.NotConfigured, ui.link)
        assertEquals(DemoText.ended(null), ui.demoEndedNote)
        assertEquals(0, realNetworkCalls.get())
    }

    @Test
    fun `leaving the demo clears everything, leaves the link off and creates no client`() = harnessTest(demo = true) {
        val before = connectWithFleet()
        startDemo()
        eventually("real link closed") { before.closed }
        val alertAt = DemoFixtures.scenario.steps.first { it.event == "alert_upgrade" }.atMs
        play(alertAt + 500)
        eventually("demo alert") { ui.alerts.rows.isNotEmpty() }
        vm.landAll().join()
        eventually("demo LAND ALL answered") { ui.landAll != null }

        onMain { vm.exitDemo() }
        assertFalse(ui.demo)
        assertTrue(ui.cards.isEmpty())
        assertTrue(ui.alerts.rows.isEmpty())
        assertTrue(ui.banners.isEmpty())
        assertTrue(ui.toasts.isEmpty())
        assertTrue(ui.videoTiles.isEmpty())
        assertNull(ui.landAll)
        assertNull(ui.snapshot.fleetPlan)
        assertEquals(GroundLink.Off, ui.link)
        assertEquals("Demo ended. The ground link is off; turn it on to reconnect to $host.", ui.demoEndedNote)

        // The app comes to the foreground: the link stays off and no real client is created.
        foreground()
        vm.awaitIdle()
        assertEquals("no real client after the demo", 1, port.opened.size)
        assertFalse(settings.enabled)
        assertEquals(GroundLink.Off, ui.link)

        // Only the user's switch reconnects, and the real ground starts from nothing.
        onMain { vm.setEnabled(true) }
        val real = port.connection(1)
        real.server(ContractFixtures.frame("engineio_open"))
        eventually("namespace connect sent") { real.sent.isNotEmpty() }
        real.server(ContractFixtures.frame("socketio_connect_ack"))
        eventually("real link") { ui.link == GroundLink.Connected && !ui.demo }
        assertNull(ui.demoEndedNote)
        assertTrue(ui.alerts.rows.isEmpty())
    }

    @Test
    fun `leaving and starting the demo again replays it from the start`() = harnessTest(demo = true, enabled = false, forbidRealNetwork = true) {
        startDemo()
        play(20_000)
        vm.landAll().join()
        eventually("LAND ALL acknowledged") { ui.landAll?.phase == CommandPhase.SUCCEEDED }
        onMain { vm.exitDemo() }
        // Nothing real is left and the link is off: no confirmation is needed.
        assertEquals(DemoRequest.Ready, onMain { vm.demoRequest() })
        startDemo(confirmed = false)
        assertEquals(2, demoPorts.size)
        assertTrue(ui.cards.all { it.controlText == "On the ground" })
        assertNull(ui.landAll)
        assertNull(ui.demoEndedNote)
        assertTrue(demoPorts.last().connections.single().acknowledged.isEmpty())
        assertEquals(0, realNetworkCalls.get())
    }

    @Test
    fun `the link stays off after the process dies or the ViewModel is recreated during the demo`() {
        val first = GroundHarness(demo = true)
        try {
            runBlocking {
                first.connectWithFleet()
                first.startDemo()
                first.vm.awaitIdle()
                assertFalse("link off saved at demo start", first.settings.enabled)
            }
            // The process dies while the demo runs (nothing is released) and the app starts again.
            val restarted = GroundHarness(demo = true, sharedSettings = first.settings, sharedStorage = first.storage)
            try {
                runBlocking {
                    restarted.foreground()
                    restarted.vm.awaitIdle()
                    assertFalse(restarted.ui.demo)
                    assertEquals(GroundLink.Off, restarted.ui.link)
                    assertTrue("no reconnect after a restart", restarted.port.opened.isEmpty())
                }
            } finally {
                restarted.close()
            }
        } finally {
            first.close() // The ViewModel is cleared (onCleared releases the demo client) ...
        }
        // ... and a new one is made for the same settings.
        val recreated = GroundHarness(demo = true, sharedSettings = first.settings, sharedStorage = first.storage)
        try {
            runBlocking {
                recreated.foreground()
                recreated.vm.awaitIdle()
                assertFalse(recreated.ui.demo)
                assertEquals(GroundLink.Off, recreated.ui.link)
                assertTrue(recreated.port.opened.isEmpty())
            }
        } finally {
            recreated.close()
        }
    }

    @Test
    fun `LAND ALL while the mission loads goes to the real ground, and the demo then does not start`() {
        val loading = CompletableDeferred<Unit>()
        val gate = CompletableDeferred<Unit>()
        val h = GroundHarness(demoScenario = {
            loading.complete(Unit)
            gate.await()
            DemoFixtures.scenario
        })
        try {
            runBlocking {
                val real = h.connectWithFleet()
                val entering = h.vm.enterDemo(disconnectConfirmed = true)
                loading.await()
                // The demo has not started: LAND ALL still reaches the real ground.
                h.vm.landAll().join()
                eventually("LAND ALL sent to the real ground") { real.commandFrames().any { it.contains("land_all") } }
                gate.complete(Unit)
                entering.join()
                assertFalse(h.ui.demo)
                assertEquals(DemoNotice.Blocked(DemoText.BLOCKED_BY_LAND_ALL_WAITING), h.ui.demoNotice)
                assertFalse(real.closed)
                assertEquals(GroundLink.Connected, h.ui.link)
                assertTrue(h.ui.landAll?.isWaiting == true)
                h.vm.awaitIdle()
                assertTrue(h.settings.enabled)
                assertTrue(h.demoPorts.isEmpty())
            }
        } finally {
            h.close()
        }
    }

    @Test
    fun `demo mode does not start without a mission and the real link is unaffected`() {
        val h = GroundHarness(demo = false, demoScenario = { null })
        try {
            runBlocking {
                val real = h.connectWithFleet()
                h.vm.enterDemo(disconnectConfirmed = true).join()
                assertFalse(h.ui.demo)
                assertEquals(DemoText.UNAVAILABLE, h.ui.demoProblem)
                assertEquals(DemoNotice.Blocked(DemoText.UNAVAILABLE), h.ui.demoNotice)
                h.vm.awaitIdle()
                assertTrue("the link is switched off only when the demo starts", h.settings.enabled)
                assertFalse(real.closed)
                assertEquals(GroundLink.Connected, h.ui.link)
                assertEquals(2, h.ui.cards.size)
            }
        } finally {
            h.close()
        }
    }

    @Test
    fun `the demo is refused while the real ground has not reported its drones`() = harnessTest(demo = true) {
        // The link is switched on but has not connected (or connected without a fleet status yet).
        assertEquals(DemoRequest.Blocked(DemoText.BLOCKED_BY_FLEET_UNKNOWN), onMain { vm.demoRequest() })
        vm.enterDemo(disconnectConfirmed = true).join()
        assertFalse(ui.demo)
        assertEquals(DemoNotice.Blocked(DemoText.BLOCKED_BY_FLEET_UNKNOWN), ui.demoNotice)
        assertEquals(
            "The ground has not reported its drones yet. Wait for it, or switch the link off in Settings first.",
            DemoText.BLOCKED_BY_FLEET_UNKNOWN,
        )
        // Connected, still no fleet status: still refused, and the real link is untouched.
        val real = connect()
        assertEquals(DemoRequest.Blocked(DemoText.BLOCKED_BY_FLEET_UNKNOWN), onMain { vm.demoRequest() })
        vm.enterDemo(disconnectConfirmed = true).join()
        assertFalse(ui.demo)
        assertFalse(real.closed)
        vm.awaitIdle()
        assertTrue(settings.enabled)
        // Once the ground reported its drones (all on the ground), the demo asks for the confirmation.
        real.fleetStatus()
        eventually("fleet shown") { ui.cards.isNotEmpty() }
        assertEquals(DemoRequest.NeedsDisconnect(host), onMain { vm.demoRequest() })
        // With the link switched off instead, nothing is refused for the unreported fleet.
        onMain { vm.setEnabled(false) }
        eventually("closed") { real.closed }
        assertEquals(DemoRequest.NeedsDisconnect(host), onMain { vm.demoRequest() })
        assertTrue(demoPorts.isEmpty())
    }

    @Test
    fun `the demo dialog stays until it is answered`() = harnessTest(demo = true) {
        connectWithFleet()
        vm.enterDemo(disconnectConfirmed = false).join()
        val notice = DemoNotice.ConfirmDisconnect(host)
        assertEquals(notice, ui.demoNotice)
        // Nothing but an answer clears it: a recreated activity reads it again from the ViewModel.
        play(2_000)
        vm.awaitIdle()
        assertEquals(notice, ui.demoNotice)
        onMain { vm.dismissDemoNotice() }
        assertNull(ui.demoNotice)
        onMain { vm.showDemoRefusal(DemoText.BLOCKED_BY_SEARCH) }
        assertEquals(DemoNotice.Blocked(DemoText.BLOCKED_BY_SEARCH), ui.demoNotice)
    }

    @Test
    fun `turning the demo off while the mission loads starts nothing`() {
        val loading = CompletableDeferred<Unit>()
        val gate = CompletableDeferred<Unit>()
        val h = GroundHarness(enabled = false, demoScenario = {
            loading.complete(Unit)
            gate.await()
            DemoFixtures.scenario
        })
        try {
            runBlocking {
                val entering = h.vm.enterDemo(disconnectConfirmed = false)
                loading.await()
                h.onMain { h.vm.exitDemo() }
                gate.complete(Unit)
                entering.join()
                h.vm.awaitIdle()
                assertFalse(h.ui.demo)
                assertTrue(h.demoPorts.isEmpty())
                assertNull(h.ui.demoNotice)
                // A later start works as usual.
                h.vm.enterDemo(disconnectConfirmed = false).join()
                eventually("demo connected") { h.ui.demo && h.ui.link == GroundLink.Connected }
            }
        } finally {
            h.close()
        }
    }

    @Test
    fun `a LAND ALL tapped in the demo is not sent once the demo has ended`() = harnessTest(demo = true, enabled = false) {
        startDemo(confirmed = false)
        play(8_000)
        // The tap is recorded in demo mode; the demo ends before the command goes out.
        onMain {
            vm.landAll()
            vm.exitDemo()
        }
        eventually("refused") { ui.landAll?.text == DemoText.LAND_ALL_DEMO_ENDED }
        assertEquals(CommandPhase.FAILED, ui.landAll?.phase)
        assertTrue(demoPorts.single().connections.single().acknowledged.isEmpty())
        assertTrue(port.opened.isEmpty())
        assertTrue(GroundText.LAND_ALL_NOT_SENT_DEMO_STARTED.contains("NOT sent"))
    }
}
