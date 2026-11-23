package com.flightrisk.app.demo

import com.flightrisk.app.ground.eventually
import com.flightrisk.app.ui.ground.CommandPhase
import com.flightrisk.app.ui.ground.DemoFixtures
import com.flightrisk.app.ui.ground.GroundConsoleViewModel
import com.flightrisk.app.ui.ground.GroundHarness
import com.flightrisk.app.ui.ground.GroundLink
import com.flightrisk.app.ui.ground.harnessTest
import com.flightrisk.contract.VerdictKind
import kotlinx.coroutines.delay
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Demo mode on the Ground console: it never reaches the network, it asks before it replaces a real
 * link, and nothing it shows survives it or mixes with a real ground's data.
 */
class DemoModeViewModelTest {

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

    private suspend fun GroundHarness.startDemo() {
        vm.enterDemo().join()
        eventually("demo connected") { ui.demo && ui.link == GroundLink.Connected }
        eventually("demo fleet shown") { ui.cards.size == 2 && !ui.isStateStale }
    }

    /**
     * The real transport here fails the test when it is touched: every real client and every health probe
     * throws and is counted. With a real ground set up and switched on, demo mode runs its whole command
     * set and every settings action, and the count stays at zero.
     */
    @Test
    fun `demo commands never touch the network`() = harnessTest(demo = true, forbidRealNetwork = true) {
        startDemo()
        foreground()
        play(8_000)
        eventually("drones flying") { ui.cards.all { it.controlText == "Flying" } }

        vm.landDrone("drone-1").join()
        eventually("land acknowledged") { ui.cards.first { it.id == "drone-1" }.landFeedback?.phase == CommandPhase.SUCCEEDED }
        vm.landAll().join()
        eventually("LAND ALL acknowledged") { ui.landAll?.phase == CommandPhase.SUCCEEDED }
        eventually("every drone on the ground") { ui.cards.all { it.controlText == "On the ground" } }
        vm.resumeHeldDrone("drone-2").join()
        onMain { vm.testConnection() }
        eventually("test refused in demo") { ui.connection.testResult == DemoText.TEST_IN_DEMO }
        onMain { vm.retryConnection() }
        onMain { vm.setEnabled(false) }
        onMain { vm.setEnabled(true) }
        onMain { vm.saveAddress("192.168.1.30:5555") }
        onMain { vm.saveKey("another-test-key") }
        vm.awaitIdle()
        onMain { vm.onBackground() }
        foreground()
        play(2_000)
        vm.awaitIdle()

        assertEquals("a real client or probe was used in demo mode", 0, realNetworkCalls.get())
        assertTrue(port.opened.isEmpty())
        assertTrue(probedWith.isEmpty())
        assertTrue(ui.demo)
        // One demo session the whole time: the settings actions did not restart anything.
        val session = demoPorts.single().connections.single()
        assertEquals(listOf("drone_command", "land_all"), session.acknowledged)
        assertTrue(ui.toasts.any { it.text == DemoText.LAND_ALL_ACKNOWLEDGED } || ui.toasts.any { it.text.startsWith("Demo:") })
    }

    @Test
    fun `starting the demo over a real link needs a confirmation and closes that link first`() = harnessTest(demo = true) {
        val real = connectWithFleet()
        real.fixture("match_alert.json", "edge_match")
        eventually("real alert shown") { ui.alerts.rows.isNotEmpty() }

        val request = onMain { vm.demoRequest() }
        assertEquals(GroundConsoleViewModel.DemoRequest.NeedsDisconnect("127.0.0.1:5555"), request)
        vm.enterDemo(disconnectConfirmed = false).join()
        assertFalse(ui.demo)
        assertEquals(GroundLink.Connected, ui.link)
        assertFalse(real.closed)
        assertEquals(1, ui.alerts.rows.size)

        vm.enterDemo(disconnectConfirmed = true).join()
        eventually("real link closed") { real.closed }
        eventually("demo connected") { ui.demo && ui.link == GroundLink.Connected }
        // Nothing of the real ground stays, and a late real event cannot reach the demo screen.
        assertTrue(ui.alerts.rows.isEmpty())
        real.fixture("match_alert.json", "edge_match")
        play(1_000)
        assertTrue(ui.alerts.rows.isEmpty())
        assertEquals(GroundConsoleViewModel.DemoRequest.AlreadyOn, onMain { vm.demoRequest() })
    }

    @Test
    fun `the demo plays the mission through the verdict to the landing`() = harnessTest(demo = true, forbidRealNetwork = true) {
        startDemo()
        val alertAt = DemoFixtures.scenario.steps.first { it.event == "match_alert" }.atMs
        val verdictAt = DemoFixtures.scenario.steps.first { it.event == "alert_upgrade" }.atMs
        play(alertAt + 100)
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        assertEquals(VerdictKind.PENDING, ui.alerts.rows.single().verdict)
        assertTrue("the obstacle cut is announced", ui.banners.any { it.text == "lane_blocked" })
        play(verdictAt - alertAt + 500)
        eventually("verdict shown") { ui.alerts.rows.single().verdict == VerdictKind.MATCH }
        assertEquals("Strong match", ui.alerts.rows.single().levelText)
        play(DemoFixtures.scenario.durationMs - verdictAt + 1_000)
        eventually("landed") { ui.cards.all { it.controlText == "On the ground" } }
        assertTrue(ui.snapshot.fleetPlan != null)
        assertEquals(0, realNetworkCalls.get())
    }

    @Test
    fun `leaving the demo clears everything and the real link comes back`() = harnessTest(demo = true) {
        startDemo()
        val alertAt = DemoFixtures.scenario.steps.first { it.event == "alert_upgrade" }.atMs
        play(alertAt + 500)
        eventually("demo alert") { ui.alerts.rows.isNotEmpty() }
        vm.landAll().join()
        eventually("demo LAND ALL answered") { ui.landAll != null }
        assertTrue(port.opened.isEmpty())

        onMain { vm.exitDemo() }
        assertFalse(ui.demo)
        assertTrue(ui.cards.isEmpty())
        assertTrue(ui.alerts.rows.isEmpty())
        assertTrue(ui.banners.isEmpty())
        assertTrue(ui.toasts.isEmpty())
        assertTrue(ui.videoTiles.isEmpty())
        assertNull(ui.landAll)
        assertNull(ui.snapshot.fleetPlan)

        // The real ground set up before the demo connects again, and starts from nothing.
        foreground()
        val real = port.connection(0)
        real.server(com.flightrisk.app.ground.ContractFixtures.frame("engineio_open"))
        eventually("namespace connect sent") { real.sent.isNotEmpty() }
        real.server(com.flightrisk.app.ground.ContractFixtures.frame("socketio_connect_ack"))
        eventually("real link") { ui.link == GroundLink.Connected && !ui.demo }
        assertTrue(ui.alerts.rows.isEmpty())
    }

    @Test
    fun `demo mode does not start without a mission and the real link is unaffected`() {
        val h = GroundHarness(demo = false, demoScenario = { null })
        try {
            kotlinx.coroutines.runBlocking {
                h.vm.enterDemo().join()
                assertFalse(h.ui.demo)
                assertEquals(DemoText.UNAVAILABLE, h.ui.demoProblem)
                h.foreground()
                h.port.connection(0)
            }
        } finally {
            h.close()
        }
    }
}
