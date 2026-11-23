package com.flightrisk.app.vision

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.sqrt

/** The embedder contract's arithmetic and the detection size filter. */
class ClipAndFilterTest {

    @Test
    fun `the shortest side becomes 224 and the long side is truncated`() {
        assertEquals(224 to 577, ClipContract.resizedSize(19, 49))
        assertEquals(577 to 224, ClipContract.resizedSize(49, 19))
        assertEquals(224 to 224, ClipContract.resizedSize(300, 300))
        // 224 * 1000 / 333 = 672.67, truncated.
        assertEquals(672 to 224, ClipContract.resizedSize(1000, 333))
    }

    @Test
    fun `the centre crop rounds half offsets to the even neighbour`() {
        assertEquals(0 to 0, ClipContract.cropOffsets(224, 224))
        // Surplus 1: 0.5 rounds to 0. Surplus 3: 1.5 rounds to 2. Surplus 5: 2.5 rounds to 2.
        assertEquals(0 to 2, ClipContract.cropOffsets(225, 227))
        assertEquals(2 to 176, ClipContract.cropOffsets(229, 576))
        assertEquals(0 to 176, ClipContract.cropOffsets(224, 577))
    }

    @Test
    fun `the input is RGB, scaled to 0 to 1 and normalised with the CLIP mean and std`() {
        val px = IntArray(224 * 224) { (0xFF shl 24) or (255 shl 16) or (0 shl 8) or 128 }
        val out = FloatArray(3 * 224 * 224)
        ClipContract.preprocess(px, 224, 224, out)
        val plane = 224 * 224
        assertEquals((1f - 0.48145466f) / 0.26862954f, out[0], 0f)
        assertEquals((0f - 0.4578275f) / 0.26130258f, out[plane], 0f)
        assertEquals((128f / 255f - 0.40821073f) / 0.27577711f, out[2 * plane + plane - 1], 0f)
    }

    @Test
    fun `l2 normalisation and cosine`() {
        val v = ClipContract.l2Normalize(floatArrayOf(3f, 4f))
        assertEquals(0.6f, v[0], 1e-7f)
        assertEquals(0.8f, v[1], 1e-7f)
        assertEquals(0f, ClipContract.l2Normalize(floatArrayOf(0f, 0f))[0], 0f)
        assertEquals(1.0, ClipContract.cosine(floatArrayOf(1f, 2f), floatArrayOf(2f, 4f)), 1e-12)
        assertEquals(1 / sqrt(2.0), ClipContract.cosine(floatArrayOf(1f, 0f), floatArrayOf(1f, 1f)), 1e-12)
    }

    // ------------------------------------------------------------------
    // Size filter
    // ------------------------------------------------------------------

    private fun box(w: Int, h: Int) = intArrayOf(100, 100, 100 + w, 100 + h)

    @Test
    fun `the aerial default keeps small people the old filter dropped`() {
        // A person 16 px tall and 6 px wide in a 1280 x 720 frame: 2.2% of the height.
        val small = box(6, 16)
        assertTrue(DetectionSizeFilter.AERIAL.keeps(small, 1280, 720))
        assertFalse(DetectionSizeFilter.LEGACY.keeps(small, 1280, 720))
        // 24 px tall in a 1920 x 1080 frame, and 48 px in 1080p (4.4%): both kept now, both dropped before.
        assertTrue(DetectionSizeFilter.AERIAL.keeps(box(10, 24), 1920, 1080))
        assertTrue(DetectionSizeFilter.AERIAL.keeps(box(20, 48), 1920, 1080))
        assertFalse(DetectionSizeFilter.LEGACY.keeps(box(20, 48), 1920, 1080))
    }

    @Test
    fun `the aerial default keeps a person seen from above or lying down`() {
        // Wider than tall (3:1), which the old 1.5 limit dropped.
        assertTrue(DetectionSizeFilter.AERIAL.keeps(box(60, 20), 1280, 720))
        assertFalse(DetectionSizeFilter.LEGACY.keeps(box(60, 20), 1280, 720))
    }

    @Test
    fun `the aerial default still drops specks and slivers`() {
        // 5 px tall in a 720p frame is under 1% of the height.
        assertFalse(DetectionSizeFilter.AERIAL.keeps(box(3, 5), 1280, 720))
        // A 1 x 40 sliver (aspect 0.025) and a 200 x 20 band (aspect 10).
        assertFalse(DetectionSizeFilter.AERIAL.keeps(box(1, 40), 1280, 720))
        assertFalse(DetectionSizeFilter.AERIAL.keeps(box(200, 20), 1280, 720))
        assertFalse(DetectionSizeFilter.AERIAL.keeps(intArrayOf(10, 10, 10, 30), 1280, 720))
    }

    @Test
    fun `the filter is configurable`() {
        val strict = DetectionSizeFilter(minHeightRatio = 0.05f, minAreaRatio = 0f, minAspectRatio = 0f, maxAspectRatio = 10f)
        assertFalse(strict.keeps(box(10, 30), 1280, 720))
        assertTrue(strict.keeps(box(10, 36), 1280, 720))
        assertTrue(DetectionSizeFilter.NONE.keeps(box(1, 1), 1280, 720))
        assertTrue(runCatching { DetectionSizeFilter(2f, 0f, 0f, 1f) }.isFailure)
        assertTrue(runCatching { DetectionSizeFilter(0f, 0f, 2f, 1f) }.isFailure)
    }
}
