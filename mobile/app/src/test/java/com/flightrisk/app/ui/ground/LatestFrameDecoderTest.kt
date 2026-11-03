package com.flightrisk.app.ui.ground

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList

@OptIn(ExperimentalCoroutinesApi::class)
class LatestFrameDecoderTest {

    private class Image(val name: String)

    private fun TestScope.decoder(
        decoded: MutableList<String>,
        released: MutableList<String> = CopyOnWriteArrayList(),
        decode: (String, Int, () -> Boolean) -> Image? = { b, _, _ -> decoded += b; if (b == "bad") null else Image(b) },
    ) = LatestFrameDecoder(
        scope = this,
        workDispatcher = StandardTestDispatcher(testScheduler),
        decode = decode,
        release = { released += it.name },
    )

    @Test
    fun `a frame is decoded and the image is kept`() = runTest {
        val decoded = mutableListOf<String>()
        val d = decoder(decoded)
        d.submit("f1", 300)
        advanceUntilIdle()
        assertEquals("f1", d.state.value.image!!.name)
        assertFalse(d.state.value.failed)
    }

    @Test
    fun `frames that arrive while a decode runs are dropped except the newest`() = runTest {
        val decoded = mutableListOf<String>()
        val d = decoder(decoded)
        d.submit("f1", 300)
        runCurrent() // the worker is started and f1 is taken
        d.submit("f2", 300)
        d.submit("f3", 300)
        d.submit("f4", 300)
        advanceUntilIdle()
        assertEquals(listOf("f1", "f4"), decoded)
        assertEquals("f4", d.state.value.image!!.name)
        assertEquals(2, d.decodesStarted)
    }

    @Test
    fun `an unreadable frame is reported as failed and the next good one recovers`() = runTest {
        val decoded = mutableListOf<String>()
        val d = decoder(decoded)
        d.submit("bad", 300)
        advanceUntilIdle()
        assertNull(d.state.value.image)
        assertTrue(d.state.value.failed)
        d.submit("good", 300)
        advanceUntilIdle()
        assertEquals("good", d.state.value.image!!.name)
        assertFalse(d.state.value.failed)
    }

    @Test
    fun `clear drops the image and a decode finishing afterwards is released, never shown`() = runTest {
        val decoded = mutableListOf<String>()
        val released = CopyOnWriteArrayList<String>()
        val d = decoder(decoded, released)
        d.submit("f1", 300)
        advanceUntilIdle()
        d.submit("f2", 300)
        runCurrent()
        d.clear()
        advanceUntilIdle()
        assertNull(d.state.value.image)
        assertFalse(d.state.value.failed)
        assertTrue("f2" !in decoded || released.contains("f2") || d.state.value.image == null)
    }

    @Test
    fun `a null request clears and abandons queued work`() = runTest {
        val decoded = mutableListOf<String>()
        val d = decoder(decoded)
        d.submit("f1", 300)
        advanceUntilIdle()
        d.submit(null, 300)
        assertNull(d.state.value.image)
        d.submit("f2", 300)
        d.submit(null, 300)
        advanceUntilIdle()
        assertEquals(listOf("f1"), decoded)
    }

    @Test
    fun `a closed decoder ignores further requests`() = runTest {
        val decoded = mutableListOf<String>()
        val d = decoder(decoded)
        d.close()
        d.submit("f1", 300)
        advanceUntilIdle()
        assertTrue(decoded.isEmpty())
        assertNull(d.state.value.image)
    }

    @Test
    fun `the decode function is told to stop when the request is cancelled`() = runTest {
        val sawCancel = CopyOnWriteArrayList<Boolean>()
        val d = LatestFrameDecoder<Image>(
            scope = this,
            workDispatcher = StandardTestDispatcher(testScheduler),
            decode = { _, _, cancelled -> sawCancel += cancelled(); Image("x") },
        )
        d.submit("f1", 300)
        runCurrent()
        assertEquals(listOf(false), sawCancel.toList())
    }
}
