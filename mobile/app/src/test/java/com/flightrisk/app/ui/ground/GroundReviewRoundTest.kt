package com.flightrisk.app.ui.ground

import androidx.compose.ui.graphics.Color
import com.flightrisk.app.ground.eventually
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlin.coroutines.ContinuationInterceptor
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Fixes of the review round: long server text, the layout budget, contrast, expiry, freshness, threads. */
class GroundReviewRoundTest {

    private val notConnected = com.flightrisk.contract.DisplayText.LAND_ALL_NOT_SENT

    /** Every piece of text the screen can show from this state. */
    private fun allTexts(ui: GroundUiState): List<String> = buildList {
        add(ui.link.text)
        ui.staleNote?.let { add(it) }
        ui.landAllText?.let { add(it) }
        ui.landAll?.let { add(it.text); it.detail?.let(::add) }
        ui.banners.forEach { add(it.text) }
        ui.toasts.forEach { add(it.text) }
        add(ui.readout)
        for (c in ui.cards) {
            add(c.displayId)
            listOfNotNull(
                c.linkChipText, c.controlText, c.batteryText, c.altitudeText, c.laneText, c.linkNotice, c.staleNote,
                c.landFeedbackText, c.resumeFeedbackText, c.alertText,
            ).forEach(::add)
        }
    }

    private fun assertAllShort(ui: GroundUiState, limit: Int = 420) {
        for (t in allTexts(ui)) assertTrue("a text of ${t.length} characters is shown: ${t.take(80)}", t.length <= limit)
    }

    // ---- 1. long text from the ground

    @Test
    fun `a 1200 character LAND ALL failure detail is cut before it is shown`() = harnessTest {
        val c = connectWithFleet()
        val detail = "failed: " + "x".repeat(1_200)
        c.event("fleet_event", """{"event":"land_all","detail":"$detail"}""")
        eventually("failure") { ui.landAll?.phase == CommandPhase.FAILED }
        assertAllShort(ui)
        assertTrue(ui.landAll!!.text.startsWith("LAND ALL FAILED: failed: xxx"))
        assertTrue(ui.landAll!!.text.contains("…"))
        assertTrue(ui.banners.single().text.length <= GroundText.MAX_SERVER_TEXT)
    }

    @Test
    fun `a multi megabyte detail never reaches the screen at full length`() = harnessTest {
        val c = connectWithFleet()
        val huge = "y".repeat(3_000_000)
        c.event("fleet_event", """{"event":"land_all","detail":"failed: $huge"}""")
        c.event("fleet_event", """{"event":"plan_refused","detail":"$huge"}""")
        c.event("fleet_event", """{"event":"${"k".repeat(100_000)}","detail":"$huge"}""")
        c.event("error", """{"message":"$huge"}""")
        c.event("manual_result", """{"action":"${"a".repeat(100_000)}","detail":"$huge","drone_id":"drone-2","ok":false,"reason":"${"r".repeat(100_000)}"}""")
        eventually("everything arrived") { ui.toasts.isNotEmpty() && ui.cards.any { it.alert != null } && ui.banners.size >= 4 }
        assertAllShort(ui)
    }

    @Test
    fun `a very long drone id and drone link detail are cut`() = harnessTest {
        val c = connect()
        val id = "d".repeat(5_000)
        c.event("fleet_status", """{"drones":{"$id":{"connected":true,"lost":false,"battery":50,"drone_link_ok":false,"drone_link_detail":"${"z".repeat(5_000)}"}},"manual_controlled":null}""")
        eventually("card") { ui.cards.isNotEmpty() }
        assertAllShort(ui)
        vm.landDrone(id).join()
        assertAllShort(ui)
        // The command still carries the real id.
        eventually("frame") { c.commandFrames().size == 1 && c.commandFrames().single().contains(id) }
    }

    @Test
    fun `the layout budget keeps the drone list on screen in both orientations`() {
        // Portrait phone, landscape phone, a small window.
        assertEquals(280f, GroundLayout.feedbackMaxHeightDp(800f), 0.01f)
        for (height in listOf(360f, 400f, 480f, 640f, 800f, 1000f, 1200f)) {
            val feedback = GroundLayout.feedbackMaxHeightDp(height)
            assertTrue("share at $height", feedback <= height * GroundLayout.FEEDBACK_SHARE || feedback == GroundLayout.MIN_FEEDBACK_DP)
            assertTrue("text area keeps a minimum at $height", feedback >= GroundLayout.MIN_FEEDBACK_DP)
            if (height >= GroundLayout.FIXED_BAR_DP + GroundLayout.MIN_LIST_DP + GroundLayout.MIN_FEEDBACK_DP) {
                assertTrue("the list keeps its minimum at $height", GroundLayout.listHeightDp(height) >= GroundLayout.MIN_LIST_DP - 0.01f)
            }
        }
        // Landscape: 700 characters of text no longer takes the screen.
        assertTrue(GroundLayout.feedbackMaxHeightDp(400f) <= 140f)
        assertTrue(GroundLayout.listHeightDp(400f) >= 180f)
    }

    // ---- 2. LAND ALL during the connect gap

    @Test
    fun `a tap before the client exists says not connected and starts connecting`() = harnessTest {
        vm.onForeground() // no awaitIdle: the keys are still being read
        vm.landAll().join()
        assertEquals(notConnected, ui.landAllText)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
        vm.awaitIdle()
        port.connection(0)
    }

    @Test
    fun `a land tap before the client exists says not connected for the drone too`() = harnessTest {
        val c = connectWithFleet()
        onMain { vm.onBackground() }
        eventually("closed") { c.closed }
        vm.onForeground()
        vm.landDrone("drone-1").join()
        assertTrue(ui.cards.first().landFeedback!!.text.contains("this phone is not connected to the ground"))
    }

    // ---- 3. threads

    @Test
    fun `the client runs on the default dispatcher and not on the main thread`() = harnessTest {
        connectWithFleet()
        val scope = lastClientScope!!
        val dispatcher = scope.coroutineContext[ContinuationInterceptor] as CoroutineDispatcher
        assertTrue(dispatcher === Dispatchers.Default)
    }

    // ---- 4. alert cap

    @Test
    fun `drone alerts are capped at 100 and the oldest go first`() = harnessTest {
        val c = connect()
        val drones = (0 until 120).joinToString(",") { """"d${it.toString().padStart(3, '0')}":{"connected":true,"lost":false,"battery":50}""" }
        c.event("fleet_status", """{"drones":{$drones},"manual_controlled":null}""")
        eventually("120 cards") { ui.cards.size == 120 }
        for (i in 0 until 120) {
            val id = "d${i.toString().padStart(3, '0')}"
            c.event("manual_result", """{"action":"auto_land","detail":"boom","drone_id":"$id","ok":false,"reason":"invalid"}""")
        }
        eventually("last alert shown") { ui.cards.last().alert != null }
        assertEquals(100, ui.cards.count { it.alert != null })
        assertNull(ui.cards.first().alert)
        assertNotNull(ui.cards.last().alert)
    }

    // ---- 6. unanswered LAND ALL expiry

    @Test
    fun `a tap the ground never answers expires and a later ack is credited to the newer tap`() = harnessTest {
        val c = connectWithFleet()
        vm.landAll().join()
        advance(20_000)
        vm.landAll().join()
        eventually("two frames") { c.commandFrames().size == 2 }
        advance(21_000) // the first tap is now 41 s old
        assertTrue(ui.banners.any { it.text == GroundText.EARLIER_LAND_ALL_NO_ANSWER && it.isFailure })
        c.fixture("land_all_ack.json", "fleet_reached_all")
        eventually("credited to the newer tap") { ui.landAll?.phase == CommandPhase.SUCCEEDED }
    }

    @Test
    fun `a single tap that is never answered becomes a no answer failure that stays`() = harnessTest {
        connectWithFleet()
        vm.landAll().join()
        advance(GroundConsoleViewModel.LAND_ALL_EXPIRES_MS - 1)
        assertEquals(CommandPhase.NO_REPLY_YET, ui.landAll!!.phase)
        advance(1)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
        assertEquals(GroundText.LAND_ALL_NO_ANSWER, ui.landAll!!.text)
        advance(10 * 60_000L)
        assertEquals(CommandPhase.FAILED, ui.landAll!!.phase)
    }

    // ---- 7. contrast

    @Test
    fun `every text and chip colour pair has at least 4_5 to 1 contrast`() {
        for ((fg, bg) in GroundColors.pairs) {
            assertTrue("$fg on $bg is ${GroundColors.contrast(fg, bg)}", GroundColors.contrast(fg, bg) >= 4.5)
        }
        // The old pairs were below the bar; the calculation must see that.
        assertTrue(GroundColors.contrast(Color.White, Color(0xFFEA580C)) < 4.5)
        assertTrue(GroundColors.contrast(Color.White, Color(0xFF16A34A)) < 4.5)
    }

    // ---- 8, 9. ticker, settings thread, freshness

    @Test
    fun `settings are read and written off the main thread`() = harnessTest {
        foreground()
        onMain { vm.saveAddress("127.0.0.2:5556") }
        onMain { vm.setEnabled(true) }
        vm.awaitIdle()
        assertTrue(settingsThreads.isNotEmpty())
        assertFalse(settingsThreads.contains("fake-main"))
        assertEquals("127.0.0.2:5556", settings.address)
    }

    @Test
    fun `the one second ticker stops in the background and runs again on return`() = harnessTest(ticker = true) {
        assertTrue(onMain { vm.tickerRunning() })
        onMain { vm.onBackground() }
        assertFalse(onMain { vm.tickerRunning() })
        foreground()
        assertTrue(onMain { vm.tickerRunning() })
    }

    @Test
    fun `no fleet status for 30 seconds while connected marks the state stale with a reason`() = harnessTest {
        val c = connectWithFleet("one_held_orphan_countdown")
        assertTrue(ui.cards.first { it.id == "drone-2" }.canResume)
        advance(GroundConsoleViewModel.FRESHNESS_TIMEOUT_MS - 1)
        assertFalse(ui.isStateStale)
        advance(1)
        assertTrue(ui.isStateStale)
        assertEquals(GroundText.STALE_NO_UPDATE, ui.staleNote)
        assertEquals("LAST: CONNECTED", ui.cards.first().linkChipText)
        assertFalse(ui.cards.first { it.id == "drone-2" }.canResume)
        assertTrue(ui.link.isConnected)
        c.fleetStatus("one_held_orphan_countdown")
        eventually("fresh again") { !ui.isStateStale }
        assertNull(ui.staleNote)
    }

    // ---- 10. nits

    @Test
    fun `lane progress also ends in last value while stale`() = harnessTest {
        val c = connectWithFleet("one_lost")
        assertEquals("0/3", ui.cards.first { it.id == "drone-1" }.laneText)
        drop(c)
        assertEquals("0/3 (last value)", ui.cards.first { it.id == "drone-1" }.laneText)
    }

    @Test
    fun `the land button label follows the state`() = harnessTest {
        connectWithFleet()
        assertEquals("Land", ui.cards.first().landTitle)
        onMain { vm.onBackground() }
        vm.landDrone("drone-1").join()
        assertEquals("Land again", ui.cards.first().landTitle)
    }

    @Test
    fun `seconds are formatted without the device locale`() {
        val old = java.util.Locale.getDefault()
        try {
            java.util.Locale.setDefault(java.util.Locale.GERMANY)
            assertEquals("Reconnecting (attempt 2, next try in 1.5 s): x", GroundLink.Reconnecting(2, 1.5, "x").text)
        } finally {
            java.util.Locale.setDefault(old)
        }
        delayNothing()
    }

    private fun delayNothing() = Unit
}
