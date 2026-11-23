package com.flightrisk.app.vision

import org.junit.Assert.assertEquals
import org.junit.Test

/** Which detections of a 1920x1080 aerial frame get an appearance score. */
class MatchSelectionTest {
    private val sel = MatchSelection(minCropHeightPx = 40, maxPerFrame = 5)

    @Test
    fun `a 39 px crop is not scored and a 40 px crop is`() {
        assertEquals(listOf(1), sel.eligible(listOf(39, 40), listOf(0.9f, 0.5f)))
    }

    @Test
    fun `small aerial people are left out and the rest are kept`() {
        val heights = listOf(12, 24, 39, 40, 64)
        assertEquals(listOf(4, 3), sel.eligible(heights, listOf(0.9f, 0.9f, 0.9f, 0.5f, 0.8f)))
    }

    @Test
    fun `at most 5 are scored per frame, the most confident first`() {
        val heights = List(8) { 64 }
        val scores = listOf(0.3f, 0.9f, 0.5f, 0.7f, 0.2f, 0.8f, 0.6f, 0.4f)
        assertEquals(listOf(1, 5, 3, 6, 2), sel.eligible(heights, scores))
    }

    @Test
    fun `the cap applies after the height filter`() {
        val heights = listOf(12, 12, 12, 64, 64, 64, 64, 64, 64)
        val scores = listOf(0.99f, 0.98f, 0.97f, 0.5f, 0.6f, 0.7f, 0.8f, 0.9f, 0.4f)
        assertEquals(listOf(7, 6, 5, 4, 3), sel.eligible(heights, scores))
    }

    @Test
    fun `equal scores keep their order`() {
        assertEquals(listOf(0, 1, 2), sel.eligible(listOf(50, 50, 50), listOf(0.5f, 0.5f, 0.5f)))
    }

    @Test
    fun `an empty frame gives nothing`() {
        assertEquals(emptyList<Int>(), sel.eligible(emptyList(), emptyList()))
    }

    @Test
    fun `heights and scores of different sizes are rejected`() {
        val e = runCatching { sel.eligible(listOf(50), emptyList()) }.exceptionOrNull()
        assertEquals(IllegalArgumentException::class, e?.let { it::class })
    }
}
