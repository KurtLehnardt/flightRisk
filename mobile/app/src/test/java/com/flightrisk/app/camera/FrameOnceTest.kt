package com.flightrisk.app.camera

import com.flightrisk.app.ui.search.DroneLinkLoss
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class FrameOnceTest {

    private var now = 10_000L
    private val slot = LatestFrameSlot<String>({ now })

    /** Stands in for the pipeline loop: one score per frame that gets past the deduper. */
    private fun poll(deduper: FrameDeduper, scores: MutableList<Float>) {
        val stamped = slot.latest() ?: return
        if (deduper.isNew(stamped.sequence)) scores.add(0.9f)
    }

    @Test
    fun `the same frame offered repeatedly adds exactly one score and cannot confirm alone`() {
        val corroborationThreshold = 3
        val deduper = FrameDeduper()
        val scores = mutableListOf<Float>()
        slot.put("frame")
        repeat(500) { poll(deduper, scores) }
        assertEquals(1, scores.size)
        assertTrue(scores.size < corroborationThreshold)
    }

    @Test
    fun `distinct frames each add a score`() {
        val deduper = FrameDeduper()
        val scores = mutableListOf<Float>()
        repeat(3) { i ->
            slot.put("frame$i")
            poll(deduper, scores)
            poll(deduper, scores)
        }
        assertEquals(3, scores.size)
    }

    @Test
    fun `sequence numbers differ for each stored frame`() {
        slot.put("a")
        val first = slot.latest()!!.sequence
        slot.put("a")
        assertNotEquals(first, slot.latest()!!.sequence)
    }

    @Test
    fun `a frame older than two seconds returns null`() {
        slot.put("frame")
        now += 1_900
        assertEquals("frame", slot.latest()?.frame)
        now += 200
        assertNull(slot.latest())
    }

    @Test
    fun `a stalled feed never yields the last frame again`() {
        val deduper = FrameDeduper()
        val scores = mutableListOf<Float>()
        slot.put("frame")
        poll(deduper, scores)
        now += 60_000
        repeat(100) { poll(deduper, scores) }
        assertEquals(1, scores.size)
        assertNull(slot.latest())
    }

    @Test
    fun `a cleared slot returns null`() {
        slot.put("frame")
        assertEquals("frame", slot.clear())
        assertNull(slot.latest())
    }

    @Test
    fun `a disconnect mid-search stops the search`() {
        var stopped = 0
        assertTrue(DroneLinkLoss.stopSearchIfRunning(searchRunning = true) { stopped++ })
        assertEquals(1, stopped)
    }

    @Test
    fun `a disconnect with no search running stops nothing`() {
        var stopped = 0
        assertFalse(DroneLinkLoss.stopSearchIfRunning(searchRunning = false) { stopped++ })
        assertEquals(0, stopped)
    }
}
