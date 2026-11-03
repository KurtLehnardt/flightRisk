package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.eventually
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The Ground screen's video tiles and match alerts through the real client with a fake ground. Both are
 * read only: the phone shows what the ground sends and never starts or stops video or answers an alert.
 */
class GroundVideoAlertsTest {

    private val forbidden = listOf("set_stream_video", "match_alert_response", "set_target", "stop_search")

    private fun com.flightrisk.app.ground.FakeConnection.assertNothingForbiddenSent() {
        val frames = eventFrames().joinToString("\n")
        for (name in forbidden) assertFalse("the phone must never send $name", frames.contains(name))
    }

    // ---- video tiles

    @Test
    fun `a frame becomes a live tile and a detections only frame shows the video off note`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("frame.json", "edge_video_with_match")
        eventually("tile shown") { ui.videoTiles.isNotEmpty() }
        val tile = ui.videoTiles.single()
        assertEquals("drone-1", tile.id)
        assertEquals(VideoTileMode.LIVE, tile.mode)
        assertEquals(2, tile.boxes.size)
        assertEquals(1, tile.boxes.count { it.matched })
        assertTrue(tile.imageBase64!!.isNotEmpty())
        assertFalse(tile.videoOffAtGround)

        c.fixture("frame.json", "edge_detections_only")
        eventually("second drone shown") { ui.videoTiles.size == 2 }
        val off = ui.videoTiles.first { it.id == "drone-2" }
        assertNull(off.imageBase64)
        assertTrue(off.videoOffAtGround)
        assertEquals(listOf("drone-1", "drone-2"), ui.videoTiles.map { it.id })
        c.assertNothingForbiddenSent()
    }

    @Test
    fun `a tile shows NO VIDEO three seconds after its last frame and the image is let go`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("frame.json", "edge_video_with_match")
        eventually("tile shown") { ui.videoTiles.isNotEmpty() }
        advance(2_900)
        assertEquals(VideoTileMode.LIVE, ui.videoTiles.single().mode)
        advance(200)
        val stale = ui.videoTiles.single()
        assertEquals(VideoTileMode.NO_VIDEO, stale.mode)
        assertNull(stale.imageBase64)
        assertTrue(stale.boxes.isEmpty())
        assertEquals("no recent frame from drone-1", stale.noVideoReason)
    }

    @Test
    fun `a newer frame replaces the older one for the same drone and the freshness restarts`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("frame.json", "edge_video_with_match")
        eventually("tile shown") { ui.videoTiles.isNotEmpty() }
        advance(2_500)
        c.fixture("frame.json", "edge_video_with_match")
        advance(1_000)
        // The second frame is only 1 s old even though the first is 3.5 s old.
        eventually("still live") { ui.videoTiles.singleOrNull()?.mode == VideoTileMode.LIVE }
        assertEquals(1, ui.videoTiles.size)
    }

    @Test
    fun `frames go away with the link so no old image stays on screen`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("frame.json", "edge_video_with_match")
        eventually("tile shown") { ui.videoTiles.isNotEmpty() }
        drop(c)
        eventually("tiles cleared") { ui.videoTiles.isEmpty() }
    }

    @Test
    fun `the app going to the background releases the frames`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("frame.json", "edge_video_with_match")
        eventually("tile shown") { ui.videoTiles.isNotEmpty() }
        onMain { vm.onBackground() }
        eventually("tiles cleared") { ui.videoTiles.isEmpty() }
    }

    @Test
    fun `a stream_video_state off with no frames explains it and starts nothing`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("stream_video_state.json", "off")
        eventually("off note") { ui.videoOffAtGround }
        assertTrue(ui.videoTiles.isEmpty())
        c.assertNothingForbiddenSent()
    }

    @Test
    fun `a hostile frame with the wrong types or an oversized box list does not break the screen`() = harnessTest {
        val c = connectWithFleet()
        val boxes = (0 until 500).joinToString(",") { """{"bbox_norm":[0.1,0.1,0.2,0.2],"matched":false,"track_id":$it}""" }
        c.event("frame", """{"drone_id":"d1","image":"@@not base64@@","frame_width":"wide","boxes":[$boxes,{"bbox_norm":"x"},7]}""")
        eventually("tile shown") { ui.videoTiles.isNotEmpty() }
        val tile = ui.videoTiles.single()
        assertEquals(VideoTileLogic.MAX_BOXES_PER_TILE, tile.boxes.size)
        assertTrue(tile.hiddenBoxCount > 0)
        assertEquals(VideoTileLogic.DEFAULT_ASPECT, tile.aspect, 0.0)
        // The text is passed on as is; the decode guard refuses it when the tile is drawn.
        assertTrue(VideoTileLogic.guard(tile.imageBase64) is VideoTileLogic.Guarded.Refused)
        c.event("frame", "[1,2,3]")
        c.event("frame", """{"drone_id":{"x":1},"boxes":"none"}""")
        eventually("tile with no drone id") { ui.videoTiles.any { it.id == "" } }
        assertEquals("unknown drone", ui.videoTiles.first { it.id == "" }.droneLabel)
    }

    // ---- match alerts

    @Test
    fun `a match alert is listed as waiting and a reminder for it does not add a row`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("match_alert.json", "edge_match")
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        val row = ui.alerts.rows.single()
        assertEquals("Possible match", row.levelText)
        assertEquals("Track 8_7", row.trackText)
        assertEquals("Score 86% (ReID 86%, face 0%)", row.scoreText)
        assertEquals(AlertStatus.WAITING, row.status)
        assertFalse(row.isReminder)

        c.fixture("match_alert.json", "reminder_on_connect")
        eventually("reminder merged") { ui.alerts.rows.single().isReminder }
        assertEquals(1, ui.alerts.rows.size)
        assertEquals(GroundAlertsLog.reminderTag("12:34:56"), ui.alerts.rows.single().reminderText)
        c.assertNothingForbiddenSent()
    }

    @Test
    fun `an operator answer marks the alert resolved and the phone sends nothing`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("match_alert.json", "edge_match")
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        c.fixture("match_alert_resolved.json", "dismiss")
        eventually("dismissed") { ui.alerts.rows.single().status == AlertStatus.DISMISSED }
        assertEquals(GroundAlertsLog.DISMISSED_TEXT, ui.alerts.rows.single().statusText)
        c.fixture("match_alert.json", "reminder_on_connect")
        eventually("reminder merged") { ui.alerts.rows.single().isReminder }
        // A late reminder does not bring a dismissed alert back.
        assertEquals(AlertStatus.DISMISSED, ui.alerts.rows.single().status)
        c.fixture("match_alert_resolved.json", "confirm")
        eventually("confirmed") { ui.alerts.rows.single().status == AlertStatus.CONFIRMED }
        c.assertNothingForbiddenSent()
    }

    @Test
    fun `an answer for an alert this phone never saw adds nothing`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("match_alert_resolved.json", "confirm")
        c.fixture("match_alert.json", "edge_match")
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        assertEquals(AlertStatus.WAITING, ui.alerts.rows.single().status)
    }

    @Test
    fun `after a drop the alert is not called waiting and says it is not live`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("match_alert.json", "edge_match")
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        drop(c)
        eventually("earlier") { ui.alerts.rows.singleOrNull()?.status == AlertStatus.EARLIER }
        assertEquals(MissionFormat.NOT_LIVE_VALUES, ui.alerts.notLiveNote)
        assertEquals(GroundAlertsLog.EARLIER_TEXT, ui.alerts.rows.single().statusText)
    }

    @Test
    fun `a reconnect that repeats the alert makes it waiting again`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("match_alert.json", "edge_match")
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        drop(c)
        advanceBackoff()
        val c2 = connect(1)
        c2.fleetStatus()
        eventually("fresh again") { !ui.isStateStale }
        assertEquals(AlertStatus.EARLIER, ui.alerts.rows.single().status)
        c2.fixture("match_alert.json", "reminder_on_connect")
        eventually("waiting again") { ui.alerts.rows.single().status == AlertStatus.WAITING }
    }

    @Test
    fun `an alert snapshot and a very long reasoning are not kept`() = harnessTest {
        val c = connectWithFleet()
        val long = "x".repeat(200_000)
        c.event(
            "match_alert",
            """{"alert_id":"a-1","time":"10:00:00","score":1.7,"alert_level":"confirmed_match","snapshot":"${"A".repeat(50_000)}","reasoning":"$long","track_id":"t"}""",
        )
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        val row = ui.alerts.rows.single()
        assertTrue(row.reasonText!!.length <= GroundText.MAX_SERVER_TEXT)
        assertEquals("Strong match", row.levelText)
        // A score above 1 is shown as 100 percent, never as 170.
        assertEquals("Score 100% (ReID --, face --)", row.scoreText)
        assertFalse(ui.toString().contains("AAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
    }

    @Test
    fun `there is no way to answer an alert from the view model`() {
        val methods = GroundConsoleViewModel::class.java.declaredMethods.map { it.name.lowercase() }
        assertTrue(methods.none { "alertresponse" in it || "confirmalert" in it || "dismissalert" in it && "drone" !in it })
        assertTrue(methods.none { "streamvideo" in it })
    }
}
