package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.ConnectionStatus
import com.flightrisk.app.ground.eventually
import com.flightrisk.contract.DisplayText
import kotlinx.coroutines.delay
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/** LAND ALL: always possible to tap, always answered with a visible result, never queued or retried. */
class GroundLandAllTest {

    private val landAllFrame = "42[\"land_all\"]"
    private val notConnected = "LAND ALL was NOT sent: this phone is not connected to the ground. " +
        "Land the drones now with their own controls, or press LAND ALL on the ground laptop."
    private val wrongKey = "LAND ALL was NOT sent: the ground refused this phone's key. " +
        "Land the drones now with their own controls, or press LAND ALL on the ground laptop."

    private suspend fun GroundHarness.tapLandAll() = vm.landAll().join()

    @Test
    fun `the notes in the contract constants are the new texts`() {
        assertEquals(notConnected, DisplayText.LAND_ALL_NOT_SENT)
        assertEquals(wrongKey, DisplayText.LAND_ALL_NOT_SENT_WRONG_KEY)
    }

    // ---- every state leaves a visible, plain result

    @Test
    fun `not configured gives a plain not sent result and starts nothing`() = harnessTest(address = null, key = null, enabled = false) {
        foreground()
        assertEquals(GroundLink.NotConfigured, ui.link)
        tapLandAll()
        val result = ui.landAll!!
        assertEquals(CommandPhase.FAILED, result.phase)
        assertEquals(GroundText.LAND_ALL_NOT_SENT_LINK_OFF, ui.landAllText)
        assertTrue(ui.landAllText!!.contains("LAND ALL was NOT sent"))
        assertTrue(port.opened.isEmpty())
    }

    @Test
    fun `a tap never switches a disabled link on`() = harnessTest(enabled = false) {
        foreground()
        assertEquals(GroundLink.Off, ui.link)
        tapLandAll()
        assertEquals(GroundText.LAND_ALL_NOT_SENT_LINK_OFF, ui.landAllText)
        assertTrue(port.opened.isEmpty())
        assertFalse(settings.enabled)
    }

    @Test
    fun `while connecting the result says it was not sent`() = harnessTest {
        foreground()
        port.connection(0)
        eventually("connecting") { ui.link is GroundLink.Connecting }
        tapLandAll()
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
        assertEquals(notConnected, ui.landAllText)
    }

    @Test
    fun `while reconnecting the result says it was not sent and nothing is queued`() = harnessTest {
        val c = connectWithFleet()
        drop(c)
        tapLandAll()
        assertEquals(notConnected, ui.landAllText)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
        // The cut-short backoff opens a new connection; it must not carry the old LAND ALL.
        val second = connect(1)
        delay(200)
        assertTrue(second.commandFrames().none { it == landAllFrame })
        assertTrue(c.commandFrames().none { it == landAllFrame })
    }

    @Test
    fun `with the key refused the result names the refused key and the retry button reconnects`() = harnessTest {
        foreground()
        val c = port.connection(0)
        c.server(com.flightrisk.app.ground.ContractFixtures.frame("engineio_open"))
        eventually("connect sent") { c.sent.isNotEmpty() }
        c.server(connectError)
        eventually("wrong key") { ui.link == GroundLink.WrongKey }
        assertTrue(ui.link.text.startsWith("Wrong key"))
        tapLandAll()
        assertEquals(wrongKey, ui.landAllText)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
        // Wrong key is distinct from network loss, and Retry calls retryNow().
        onMain { vm.retryConnection() }
        eventually("a second connection attempt") { port.opened.size >= 2 }
    }

    @Test
    fun `in the background the result says it was not sent`() = harnessTest {
        connectWithFleet()
        onMain { vm.onBackground() }
        assertEquals(GroundLink.Paused, ui.link)
        tapLandAll()
        assertEquals(GroundText.LAND_ALL_NOT_SENT_LINK_OFF, ui.landAllText)
    }

    @Test
    fun `a failure while writing says it was not sent`() = harnessTest {
        val c = connectWithFleet()
        c.failSends = IOException("broken pipe")
        tapLandAll()
        assertEquals(GroundText.LAND_ALL_SEND_FAILED, ui.landAllText)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
    }

    // ---- connected: handed over, then the ground's answer

    @Test
    fun `sent shows handed to the connection until an ack arrives, then the contract text`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        eventually("frame sent") { c.commandFrames() == listOf(landAllFrame) }
        assertEquals(CommandPhase.HANDED_OVER, ui.landAll!!.phase)
        assertTrue(ui.landAllText!!.contains("Handed to the connection; waiting for the ground"))
        c.fixture("land_all_ack.json", "fleet_reached_all")
        eventually("answered") { ui.landAll?.phase == CommandPhase.SUCCEEDED }
        assertEquals("LAND ALL reached: coordinator 2, transport 2", ui.landAll!!.text)
        // A success may fade after about 10 s.
        advance(GroundConsoleViewModel.SUCCESS_FADE_MS + 1)
        assertNull(ui.landAll)
    }

    @Test
    fun `an ack with reached_any not true is a failure that never fades`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        c.fixture("land_all_ack.json", "reached_no_drone")
        eventually("failed") { ui.landAll?.phase == CommandPhase.FAILED }
        assertEquals(
            "LAND ALL FAILED: no drone was reached. Land each drone by hand (per-drone Land, or the drone's own controls).",
            ui.landAll!!.text,
        )
        advance(10 * 60_000L)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
        assertNotNull(ui.landAllText)
        assertTrue(ui.landAll!!.offersRetry)
        assertTrue(ui.landAll!!.canDismiss)
    }

    @Test
    fun `an ack without reached_any is also a failure`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        c.event("land_all_ack", """{"coordinator":2,"transport":2,"local":null}""")
        eventually("failed") { ui.landAll?.phase == CommandPhase.FAILED }
    }

    @Test
    fun `a partial ack is a warning that never fades`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        c.event("land_all_ack", """{"coordinator":2,"transport":0,"local":null,"reached_any":true}""")
        eventually("partial") { ui.landAll?.phase == CommandPhase.PARTIAL }
        assertEquals(
            "LAND ALL reached only some drones. Reached: coordinator 2. Reached none: transport. Land the rest by hand.",
            ui.landAll!!.text,
        )
        advance(10 * 60_000L)
        assertEquals(CommandPhase.PARTIAL, ui.landAll!!.phase)
        assertTrue(ui.landAll!!.isWarning)
    }

    @Test
    fun `a failed land_all fleet event shows its failure text and stays up`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        c.fixture("fleet_event.json", "land_all_failed")
        eventually("failed") { ui.landAll?.phase == CommandPhase.FAILED }
        val expected = "LAND ALL FAILED: failed: drone-2. Land each drone by hand (per-drone Land, or the drone's own controls)."
        assertEquals(expected, ui.landAll!!.text)
        assertTrue(ui.banners.any { it.text == expected && it.isFailure })
        // A later success ack does not replace the failure; its counts are added as a detail.
        c.fixture("land_all_ack.json", "fleet_reached_all")
        eventually("detail added") { ui.landAll?.detail != null }
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
        assertEquals(expected, ui.landAll!!.text)
        advance(60_000)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
    }

    @Test
    fun `a successful land_all fleet event is only a banner`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("fleet_event.json", "land_all")
        eventually("banner") { ui.banners.isNotEmpty() }
        assertEquals("LAND ALL sent", ui.banners.single().text)
        assertNull(ui.landAll)
    }

    // ---- no reply, retry, link loss

    @Test
    fun `no ack after 5 seconds shows no reply from the ground yet and a second tap is another LAND ALL`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        eventually("sent") { c.commandFrames().size == 1 }
        advance(GroundConsoleViewModel.REPLY_TIMEOUT_MS - 1)
        assertEquals(CommandPhase.HANDED_OVER, ui.landAll!!.phase)
        advance(1)
        assertEquals(CommandPhase.NO_REPLY_YET, ui.landAll!!.phase)
        assertTrue(ui.landAll!!.text.contains("no reply from the ground yet"))
        assertTrue(ui.landAll!!.offersRetry)
        // Nothing is resent by itself.
        advance(60_000)
        assertEquals(1, c.commandFrames().size)
        tapLandAll()
        eventually("second frame") { c.commandFrames().size == 2 }
        assertEquals(listOf(landAllFrame, landAllFrame), c.commandFrames())
    }

    @Test
    fun `a late ack after no reply yet shows the contract text`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        advance(6_000)
        assertEquals(CommandPhase.NO_REPLY_YET, ui.landAll!!.phase)
        c.fixture("land_all_ack.json", "fleet_reached_all")
        eventually("answered") { ui.landAll?.phase == CommandPhase.SUCCEEDED }
    }

    @Test
    fun `an ack answers the oldest tap, so a late ack of tap one is not tap twos success`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        tapLandAll()
        eventually("two frames") { c.commandFrames().size == 2 }
        val secondAttempt = ui.landAll!!.attempt
        c.fixture("land_all_ack.json", "fleet_reached_all")
        eventually("detail shown") { ui.landAll?.detail != null }
        // Tap two is still waiting: it is not shown as a success.
        assertEquals(secondAttempt, ui.landAll!!.attempt)
        assertEquals(CommandPhase.HANDED_OVER, ui.landAll!!.phase)
        assertEquals("Earlier LAND ALL: LAND ALL reached: coordinator 2, transport 2", ui.landAll!!.detail)
        c.fixture("land_all_ack.json", "reached_no_drone")
        eventually("tap two answered") { ui.landAll?.phase == CommandPhase.FAILED }
        assertEquals(secondAttempt, ui.landAll!!.attempt)
    }

    @Test
    fun `a failed earlier tap stays visible as a banner while a newer tap waits`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        tapLandAll()
        eventually("two frames") { c.commandFrames().size == 2 }
        c.fixture("land_all_ack.json", "reached_no_drone")
        eventually("banner") { ui.banners.any { it.isFailure } }
        assertEquals(CommandPhase.HANDED_OVER, ui.landAll!!.phase)
        assertTrue(ui.banners.first().text.startsWith("LAND ALL FAILED"))
    }

    @Test
    fun `a link lost while LAND ALL waits becomes a visible failure and nothing is resent`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        eventually("sent") { c.commandFrames().size == 1 }
        drop(c)
        eventually("failure shown") { ui.landAll?.phase == CommandPhase.FAILED }
        assertEquals(GroundText.LAND_ALL_CONNECTION_LOST, ui.landAll!!.text)
        assertTrue(ui.landAll!!.text.contains("connection was lost before the ground answered"))
        advanceBackoff()
        val second = connect(1)
        // The old, unanswered LAND ALL is not repeated and an ack on the new session does not revive it.
        delay(200)
        assertTrue(second.commandFrames().isEmpty())
        second.fixture("land_all_ack.json", "fleet_reached_all")
        delay(100)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
    }

    @Test
    fun `a new tap replaces an earlier result and dismiss clears a failure`() = harnessTest {
        val c = connectWithFleet()
        tapLandAll()
        c.fixture("land_all_ack.json", "reached_no_drone")
        eventually("failed") { ui.landAll?.phase == CommandPhase.FAILED }
        tapLandAll()
        eventually("new tap shown") { ui.landAll?.phase == CommandPhase.HANDED_OVER }
        c.fixture("land_all_ack.json", "reached_no_drone")
        eventually("failed again") { ui.landAll?.phase == CommandPhase.FAILED }
        onMain { vm.dismissLandAll() }
        assertNull(ui.landAll)
    }

    @Test
    fun `the client status is connected when LAND ALL is sent and not disabled by drone states`() = harnessTest {
        val c = connectWithFleet("one_held_orphan_countdown")
        c.fixture("fleet_event.json", "drone_lost")
        tapLandAll()
        eventually("sent") { c.commandFrames() == listOf(landAllFrame) }
        assertTrue(onMain { vm.currentClientStatusForTest() } is ConnectionStatus.Connected)
    }
}
