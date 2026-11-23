package com.flightrisk.app.camera

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** FrameHandOff with a fake frame type: closing, conversion and one-shot hand-out. */
class FrameHandOffTest {
    private class Frame(val id: Int) { var closed = 0 }

    private val converted = mutableListOf<Int>()
    private fun newHandOff() = FrameHandOff<Frame, String>()
    private fun FrameHandOff<Frame, String>.offerFrame(f: Frame): String? =
        offer(f, { it.closed++ }) { converted += it.id; "bmp${it.id}" }

    @Test
    fun `a frame is closed and not converted when nothing is wanted`() {
        val h = newHandOff()
        val f = Frame(1)
        assertNull(h.offerFrame(f))
        assertEquals(1, f.closed)
        assertEquals(emptyList<Int>(), converted)
    }

    @Test
    fun `a frame is closed and converted when one was wanted`() {
        val h = newHandOff().apply { start() }
        val f = Frame(1)
        assertEquals("bmp1", h.offerFrame(f))
        assertEquals(1, f.closed)
        assertEquals(listOf(1), converted)
    }

    @Test
    fun `a frame is closed and not converted after stop`() {
        val h = newHandOff().apply { start(); stop() }
        val f = Frame(1)
        assertNull(h.offerFrame(f))
        assertEquals(1, f.closed)
        assertEquals(emptyList<Int>(), converted)
    }

    @Test
    fun `a frame is closed once when conversion throws and the next one is tried`() {
        val h = newHandOff().apply { start() }
        val bad = Frame(1)
        assertNull(h.offer(bad, { it.closed++ }) { throw IllegalStateException("bad frame") })
        assertEquals(1, bad.closed)
        val next = Frame(2)
        assertEquals("bmp2", h.offerFrame(next))
        assertEquals(1, next.closed)
    }

    @Test
    fun `a frame is handed out only once`() {
        val h = newHandOff().apply { start() }
        h.offerFrame(Frame(1))
        assertEquals("bmp1", h.take())
        assertNull(h.take())
    }

    @Test
    fun `nothing is converted while a frame waits to be taken`() {
        val h = newHandOff().apply { start() }
        h.offerFrame(Frame(1))
        val extra = Frame(2)
        assertNull(h.offerFrame(extra))
        assertEquals(1, extra.closed)
        assertEquals(listOf(1), converted)
    }

    @Test
    fun `the next frame is converted only after take asked for it`() {
        val h = newHandOff().apply { start() }
        h.offerFrame(Frame(1))
        assertEquals("bmp1", h.take())
        val early = Frame(2)
        assertNull(h.offerFrame(early))
        assertEquals(1, early.closed)
        assertNull(h.take())
        assertEquals("bmp3", h.offerFrame(Frame(3)))
        assertEquals("bmp3", h.take())
        assertEquals(listOf(1, 3), converted)
    }

    @Test
    fun `frames after stop are dropped and the waiting one is gone`() {
        val h = newHandOff().apply { start() }
        h.offerFrame(Frame(1))
        h.stop()
        assertNull(h.take())
        assertNull(h.latest)
        val f = Frame(2)
        assertNull(h.offerFrame(f))
        assertEquals(1, f.closed)
        assertEquals(listOf(1), converted)
    }

    @Test
    fun `a frame converted while stopping is dropped and still closed`() {
        val h = newHandOff().apply { start() }
        val f = Frame(1)
        val r = h.offer(f, { it.closed++ }) { src -> h.stop(); "bmp${src.id}" }
        assertNull(r)
        assertEquals(1, f.closed)
        assertNull(h.take())
        assertNull(h.latest)
    }

    @Test
    fun `a frame converted across a stop and a start is dropped`() {
        val h = newHandOff().apply { start() }
        val old = Frame(1)
        val r = h.offer(old, { it.closed++ }) { src -> h.stop(); h.start(); "bmp${src.id}" }
        assertNull(r)
        assertEquals(1, old.closed)
        assertNull(h.take())
        assertNull(h.latest)
        // The new search still wants a frame: the next one is converted for it.
        assertEquals("bmp2", h.offerFrame(Frame(2)))
        assertEquals("bmp2", h.take())
    }

    @Test
    fun `a frame whose conversion runs out of memory is closed and dropped`() {
        val h = newHandOff().apply { start() }
        val big = Frame(1)
        assertNull(h.offer(big, { it.closed++ }) { throw OutOfMemoryError("frame") })
        assertEquals(1, big.closed)
        assertNull(h.take())
        assertNull(h.latest)
        val next = Frame(2)
        assertEquals("bmp2", h.offerFrame(next))
        assertEquals(1, next.closed)
    }

    @Test
    fun `latest is the last converted frame even after it was taken`() {
        val h = newHandOff().apply { start() }
        h.offerFrame(Frame(1))
        h.take()
        assertEquals("bmp1", h.latest)
    }
}
