package com.flightrisk.app.ui.ground

import com.flightrisk.contract.Frame
import com.flightrisk.contract.FrameBox
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64

class VideoTileLogicTest {

    private fun frame(
        image: String? = "abc",
        boxes: List<FrameBox> = emptyList(),
        streamVideo: Boolean? = true,
        w: Int? = 960,
        h: Int? = 720,
    ) = Frame(image, boxes, null, null, null, null, streamVideo, "d", w, h, null, null)

    private fun received(f: Frame, at: Long = 0) = ReceivedFrame(f, at)

    private fun tiles(frames: Map<String, ReceivedFrame>, now: Long = 1_000, stream: Boolean? = true, stale: Boolean = false) =
        VideoTileLogic.makeTiles(frames, now, stream, stale)

    // ---- freshness

    @Test
    fun `a frame is stale after three seconds and a missing arrival time is stale`() {
        assertFalse(VideoTileLogic.isStale(0, 3_000))
        assertTrue(VideoTileLogic.isStale(0, 3_001))
        assertTrue(VideoTileLogic.isStale(null, 0))
        // A receive time far in the future (a clock that moved) is stale too; small jitter is not.
        assertTrue(VideoTileLogic.isStale(10_000, 1_000))
        assertFalse(VideoTileLogic.isStale(1_500, 1_000))
    }

    // ---- boxes

    @Test
    fun `boxes are clamped, ordered and refused when unusable`() {
        assertEquals(VideoTileBox(0.0, 0.0, 1.0, 1.0, false, null), VideoTileLogic.box(listOf(-1.0, -2.0, 3.0, 4.0)))
        assertEquals(VideoTileBox(0.2, 0.1, 0.8, 0.9, true, null), VideoTileLogic.box(listOf(0.8, 0.9, 0.2, 0.1), matched = true))
        assertNull(VideoTileLogic.box(listOf(0.5, 0.5, 0.5, 0.9)))
        assertNull(VideoTileLogic.box(listOf(0.1, 0.1, 0.2)))
        assertNull(VideoTileLogic.box(listOf(0.1, Double.NaN, 0.2, 0.2)))
        assertNull(VideoTileLogic.box(listOf(0.1, 0.1, Double.POSITIVE_INFINITY, 0.2)))
        assertNull(VideoTileLogic.box(null))
        assertNull(VideoTileLogic.box(listOf(0.0, 0.0, 1.0, 1.0), trackId = "  ")!!.trackId)
        assertEquals("t-7", VideoTileLogic.box(listOf(0.0, 0.0, 1.0, 1.0), trackId = "t-7")!!.trackId)
        assertTrue(VideoTileLogic.box(listOf(0.0, 0.0, 1.0, 1.0), trackId = "z".repeat(500))!!.trackId!!.length <= GroundText.MAX_ID_TEXT)
    }

    @Test
    fun `at most 64 boxes are drawn and the rest are counted`() {
        val raw = List(100) { FrameBox(listOf(0.1, 0.1, 0.2, 0.2), null, false, null) }
        val (boxes, hidden) = VideoTileLogic.cappedBoxes(raw)
        assertEquals(64, boxes.size)
        assertEquals(36, hidden)
        val tile = tiles(mapOf("d" to received(frame(boxes = raw)))).single()
        assertEquals("d video, 100 detections", tile.accessibilityText)
        assertEquals(36, tile.hiddenBoxCount)
    }

    // ---- tiles

    @Test
    fun `one tile per drone in natural order`() {
        val f = received(frame())
        val ids = listOf("drone-10", "drone-2", "drone-1", "Drone-3")
        assertEquals(
            listOf("drone-1", "drone-2", "Drone-3", "drone-10"),
            tiles(ids.associateWith { f }).map { it.id },
        )
    }

    @Test
    fun `at most sixteen tiles`() {
        val f = received(frame())
        assertEquals(VideoTileLogic.MAX_TILES, tiles((0 until 40).associate { "d$it" to f }).size)
    }

    @Test
    fun `a stale tile has no image and no boxes and says why`() {
        val f = frame(boxes = listOf(FrameBox(listOf(0.1, 0.1, 0.2, 0.2), null, true, null)))
        val stale = tiles(mapOf("drone-1" to received(f, at = 0)), now = 10_000).single()
        assertEquals(VideoTileMode.NO_VIDEO, stale.mode)
        assertNull(stale.imageBase64)
        assertTrue(stale.boxes.isEmpty())
        assertFalse(stale.videoOffAtGround)
        assertEquals("no recent frame from drone-1", stale.noVideoReason)
        assertEquals("drone-1 video, no video, no recent frame from drone-1", stale.accessibilityText)
        val linkDown = tiles(mapOf("drone-1" to received(f, at = 0)), now = 10_000, stale = true).single()
        assertEquals("no frames from the ground", linkDown.noVideoReason)
    }

    @Test
    fun `a fresh frame while the ground state is stale is tagged not live`() {
        val tile = tiles(mapOf("d" to received(frame(), at = 900)), now = 1_000, stale = true).single()
        assertEquals(VideoTileMode.NOT_LIVE, tile.mode)
        assertEquals("d video, not live, 0 detections", tile.accessibilityText)
        assertEquals("Not live", VideoTileLogic.NOT_LIVE_TAG)
    }

    @Test
    fun `the video off note comes from the frame or from the ground state when it is live`() {
        val noImage = frame(image = null, streamVideo = false)
        assertTrue(tiles(mapOf("d" to received(noImage))).single().videoOffAtGround)
        assertNull(tiles(mapOf("d" to received(noImage))).single().imageBase64)
        val withImage = frame(streamVideo = null)
        assertTrue(tiles(mapOf("d" to received(withImage)), stream = false).single().videoOffAtGround)
        // The remembered flag is not trusted while the state is stale.
        assertFalse(tiles(mapOf("d" to received(withImage, at = 900)), stream = false, stale = true).single().videoOffAtGround)
        assertNull(tiles(mapOf("d" to received(frame(image = ""))))[0].imageBase64)
    }

    @Test
    fun `the aspect comes from the frame size and falls back when it is unusable`() {
        assertEquals(4.0 / 3.0, VideoTileLogic.aspect(960, 720), 1e-9)
        assertEquals(VideoTileLogic.DEFAULT_ASPECT, VideoTileLogic.aspect(null, 720), 0.0)
        assertEquals(VideoTileLogic.DEFAULT_ASPECT, VideoTileLogic.aspect(0, 720), 0.0)
        assertEquals(VideoTileLogic.DEFAULT_ASPECT, VideoTileLogic.aspect(-5, 720), 0.0)
        assertEquals(4.0, VideoTileLogic.aspect(100000, 1), 0.0)
        assertEquals(0.25, VideoTileLogic.aspect(1, 100000), 0.0)
    }

    @Test
    fun `tiles sit side by side on wide or landscape screens only`() {
        assertEquals(1, VideoTileLogic.columns(1, 900f, 400f))
        assertEquals(1, VideoTileLogic.columns(3, 411f, 891f))
        assertEquals(2, VideoTileLogic.columns(3, 891f, 411f))
        assertEquals(2, VideoTileLogic.columns(2, 720f, 1000f))
    }

    @Test
    fun `the image rectangle fits the whole image and centres it`() {
        val r = VideoTileLogic.imageRect(1600, 900, 400f, 400f)
        assertEquals(0f, r[0], 0.001f)
        assertEquals(87.5f, r[1], 0.001f)
        assertEquals(400f, r[2], 0.001f)
        assertEquals(225f, r[3], 0.001f)
        val whole = VideoTileLogic.imageRect(null, null, 300f, 200f)
        assertEquals(listOf(0f, 0f, 300f, 200f), whole.toList())
    }

    @Test
    fun `the decode size is bounded`() {
        assertEquals(600, VideoTileLogic.targetPixelSize(Float.NaN))
        assertEquals(100, VideoTileLogic.targetPixelSize(10f))
        assertEquals(450, VideoTileLogic.targetPixelSize(431f))
        assertEquals(VideoTileLogic.MAX_DECODE_EDGE, VideoTileLogic.targetPixelSize(100_000f))
    }

    // ---- decode guard

    private fun b64(bytes: ByteArray) = Base64.getEncoder().encodeToString(bytes)

    @Test
    fun `the guard accepts a JPEG and refuses everything else without throwing`() {
        val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte(), 0xE0.toByte(), 0, 1, 2, 3)
        val ok = VideoTileLogic.guard(b64(jpeg))
        assertTrue(ok is VideoTileLogic.Guarded.Ok)
        assertEquals(jpeg.toList(), (ok as VideoTileLogic.Guarded.Ok).jpeg.toList())
        assertTrue(VideoTileLogic.guard(null) is VideoTileLogic.Guarded.Refused)
        assertTrue(VideoTileLogic.guard("") is VideoTileLogic.Guarded.Refused)
        assertTrue(VideoTileLogic.guard("%%% not base64 %%%") is VideoTileLogic.Guarded.Refused)
        assertTrue(VideoTileLogic.guard("A") is VideoTileLogic.Guarded.Refused)
        assertTrue(VideoTileLogic.guard(b64("not a jpeg at all".toByteArray())) is VideoTileLogic.Guarded.Refused)
        assertTrue(VideoTileLogic.guard(b64(byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte()))) is VideoTileLogic.Guarded.Refused)
        assertTrue(VideoTileLogic.guard("A".repeat(VideoTileLogic.MAX_BASE64_CHARS + 4)) is VideoTileLogic.Guarded.Refused)
    }

    @Test
    fun `the fixture frame image passes the guard`() {
        val fixture = com.flightrisk.app.ground.ContractFixtures.case("frame.json", "edge_video_with_match")["payload"] as kotlinx.serialization.json.JsonObject
        val image = (fixture["image"] as kotlinx.serialization.json.JsonPrimitive).content
        val guarded = VideoTileLogic.guard(image)
        assertTrue(guarded is VideoTileLogic.Guarded.Ok)
        assertNotNull((guarded as VideoTileLogic.Guarded.Ok).jpeg)
    }

    @Test
    fun `source size and subsampling are bounded`() {
        assertTrue(VideoTileLogic.sourceSizeAllowed(320, 180))
        assertTrue(VideoTileLogic.sourceSizeAllowed(4000, 4000))
        assertFalse(VideoTileLogic.sourceSizeAllowed(5000, 5000))
        assertFalse(VideoTileLogic.sourceSizeAllowed(0, 100))
        assertFalse(VideoTileLogic.sourceSizeAllowed(Int.MAX_VALUE, Int.MAX_VALUE))
        assertEquals(1, VideoTileLogic.sampleSize(320, 180, 600))
        assertEquals(4, VideoTileLogic.sampleSize(4000, 3000, 900))
        // Never below the target: 4000/4 = 1000 >= 900 but 4000/8 = 500 < 900.
        assertTrue(4000 / VideoTileLogic.sampleSize(4000, 3000, 900) >= 900)
        assertEquals(1, VideoTileLogic.sampleSize(100, 100, 100_000))
    }
}
