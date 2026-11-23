package com.flightrisk.app.vision

import com.flightrisk.app.vision.UltralyticsContract.Layout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The contract of an imported detector, on synthetic tensors. */
class UltralyticsContractTest {

    @Test
    fun `the letterbox is centred with the usual rounding`() {
        assertEquals(Letterbox(0.5f, 640, 360, 0, 140), UltralyticsContract.letterbox(1280, 720))
        assertEquals(Letterbox(0.8f, 640, 384, 0, 128), UltralyticsContract.letterbox(800, 480))
        // 1000 x 333: r = 0.64, height round(213.12) = 213, padding (640 - 213) / 2 = 213.5,
        // top round(213.4) = 213 and the extra pixel at the bottom.
        val odd = UltralyticsContract.letterbox(1000, 333)
        assertEquals(640, odd.resizedWidth)
        assertEquals(213, odd.resizedHeight)
        assertEquals(213, odd.padY)
        assertEquals(0, odd.padX)
    }

    @Test
    fun `the input is RGB scaled to 0 to 1 with grey padding`() {
        val w = 64
        val h = 32
        val px = IntArray(w * h) { (0xFF shl 24) or (255 shl 16) or (51 shl 8) }
        val out = FloatArray(3 * 640 * 640)
        val lb = UltralyticsContract(Layout.ANCHORS).preprocess(px, w, h, out)
        assertEquals(Letterbox(10f, 640, 320, 0, 160), lb)
        val plane = 640 * 640
        assertEquals(114f / 255f, out[0], 0f)
        val inside = 160 * 640
        assertEquals(1f, out[inside], 0f)
        assertEquals(0.2f, out[plane + inside], 1e-6f)
        assertEquals(0f, out[2 * plane + inside], 0f)
    }

    @Test
    fun `layouts are told apart by shape`() {
        assertEquals(Layout.ANCHORS, UltralyticsContract.layoutOf(longArrayOf(1, 84, 8400)))
        assertEquals(Layout.ANCHORS, UltralyticsContract.layoutOf(longArrayOf(1, 84, 2100)))
        assertEquals(Layout.END_TO_END, UltralyticsContract.layoutOf(longArrayOf(1, 300, 6)))
        assertNull(UltralyticsContract.layoutOf(longArrayOf(1, 8400, 85)))
        assertNull(UltralyticsContract.layoutOf(longArrayOf(1, 84, -1)))
        assertNull(UltralyticsContract.layoutOf(longArrayOf(1, 85, 8400)))
        assertNull(UltralyticsContract.layoutOf(longArrayOf(84, 8400)))
    }

    /** A [1, 84, n] output: channel-major, one column per anchor. */
    private fun anchors(n: Int, vararg cols: Pair<Int, FloatArray>): FloatArray {
        val out = FloatArray(84 * n)
        for ((a, v) in cols) for (c in 0 until 84) out[c * n + a] = v[c]
        return out
    }

    private fun col(cx: Float, cy: Float, w: Float, h: Float, vararg scores: Pair<Int, Float>) =
        FloatArray(84).also { it[0] = cx; it[1] = cy; it[2] = w; it[3] = h; for ((c, s) in scores) it[4 + c] = s }

    @Test
    fun `anchor outputs decode to persons in frame pixels after NMS`() {
        val lb = UltralyticsContract.letterbox(1280, 720)
        val out = anchors(
            10,
            0 to col(320f, 320f, 40f, 80f, 0 to 0.9f),
            1 to col(324f, 320f, 40f, 80f, 0 to 0.8f),
            2 to col(100f, 200f, 20f, 40f, 0 to 0.6f, 2 to 0.7f),
            3 to col(500f, 400f, 20f, 40f, 0 to 0.2f),
        )
        val dets = UltralyticsContract(Layout.ANCHORS).decode(out, longArrayOf(1, 84, 10), lb, 0.25f)
        assertEquals(1, dets.size)
        val d = dets[0]
        assertEquals(0.9f, d.score, 0f)
        // x: (300 - 0) / 0.5 = 600 and (340 - 0) / 0.5 = 680; y: (280 - 140) / 0.5 = 280 and (360 - 140) / 0.5 = 440.
        assertEquals(600f, d.x1, 1e-3f)
        assertEquals(280f, d.y1, 1e-3f)
        assertEquals(680f, d.x2, 1e-3f)
        assertEquals(440f, d.y2, 1e-3f)
    }

    @Test
    fun `end-to-end outputs keep persons above the threshold without NMS`() {
        val lb = UltralyticsContract.letterbox(800, 480)
        val out = FloatArray(300 * 6)
        fun put(r: Int, vararg v: Float) = v.copyInto(out, r * 6)
        put(0, 100f, 228f, 140f, 308f, 0.8f, 0f)
        put(1, 102f, 228f, 142f, 308f, 0.7f, 0f)
        put(2, 300f, 228f, 340f, 308f, 0.9f, 2f)
        put(3, 400f, 228f, 440f, 308f, 0.1f, 0f)
        val dets = UltralyticsContract(Layout.END_TO_END).decode(out, longArrayOf(1, 300, 6), lb, 0.25f)
        assertEquals(listOf(0.8f, 0.7f), dets.map { it.score })
        assertEquals(125f, dets[0].x1, 1e-3f)
        assertEquals(125f, dets[0].y1, 1e-3f)
    }

    @Test
    fun `a decoder refuses the other layout`() {
        val lb = UltralyticsContract.letterbox(640, 640)
        assertTrue(runCatching { UltralyticsContract(Layout.ANCHORS).decode(FloatArray(1800), longArrayOf(1, 300, 6), lb, 0.25f) }.isFailure)
    }
}
