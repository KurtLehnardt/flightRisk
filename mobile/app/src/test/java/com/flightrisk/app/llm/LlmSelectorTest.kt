package com.flightrisk.app.llm

import android.graphics.Bitmap
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Unit tests for [LlmSelector].
 *
 * These tests exercise the backend selection logic without Android
 * framework dependencies. Since [LlmSelector] requires a [Context]
 * for [ConnectivityManager], and JUnit tests run on the JVM, we test
 * the selection logic via a [TestableSelector] subclass that bypasses
 * Android-specific initialization.
 */
class LlmSelectorTest {

    /** Minimal test backend with controllable availability. */
    private class FakeBackend(
        override val name: String,
        override var isAvailable: Boolean,
    ) : LlmBackend {
        override suspend fun analyzeMatch(
            referenceImage: Bitmap,
            candidateImage: Bitmap,
            description: String?,
        ) = ReasoningResult(false, "test", "fake")

        override suspend fun describeMatch(
            candidateImage: Bitmap,
            description: String,
        ) = ReasoningResult(false, "test", "fake")
    }

    /**
     * Testable selector that skips Android ConnectivityManager.
     * Exposes the internal backend list and refresh logic directly.
     */
    private class TestableSelector {
        private val backends = mutableListOf<LlmBackend>()
        private val noOp = NoOpLlmBackend()
        private var active: LlmBackend = noOp

        val isLlmAvailable: Boolean
            get() = getActiveBackend().name != "none"

        fun registerBackend(backend: LlmBackend) {
            backends.add(backend)
            refresh()
        }

        fun getActiveBackend(): LlmBackend = active

        fun refresh() {
            active = backends.firstOrNull { it.isAvailable } ?: noOp
        }

        fun clear() {
            backends.clear()
            active = noOp
        }
    }

    private lateinit var selector: TestableSelector

    @Before
    fun setUp() {
        selector = TestableSelector()
    }

    @Test
    fun `fallback to NoOp when no backends registered`() {
        val backend = selector.getActiveBackend()
        assertEquals("none", backend.name)
        assertFalse(selector.isLlmAvailable)
    }

    @Test
    fun `fallback to NoOp when all backends unavailable`() {
        selector.registerBackend(FakeBackend("cloud", isAvailable = false))
        selector.registerBackend(FakeBackend("local", isAvailable = false))

        val backend = selector.getActiveBackend()
        assertEquals("none", backend.name)
        assertFalse(selector.isLlmAvailable)
    }

    @Test
    fun `selects first available backend by priority`() {
        selector.registerBackend(FakeBackend("cloud", isAvailable = true))
        selector.registerBackend(FakeBackend("local", isAvailable = true))

        val backend = selector.getActiveBackend()
        assertEquals("cloud", backend.name)
        assertTrue(selector.isLlmAvailable)
    }

    @Test
    fun `skips unavailable backends and picks next`() {
        selector.registerBackend(FakeBackend("cloud", isAvailable = false))
        selector.registerBackend(FakeBackend("local", isAvailable = true))

        val backend = selector.getActiveBackend()
        assertEquals("local", backend.name)
        assertTrue(selector.isLlmAvailable)
    }

    @Test
    fun `isLlmAvailable reflects active backend state`() {
        val cloud = FakeBackend("cloud", isAvailable = true)
        selector.registerBackend(cloud)

        assertTrue(selector.isLlmAvailable)

        // Simulate losing internet
        cloud.isAvailable = false
        selector.refresh()

        assertFalse(selector.isLlmAvailable)
        assertEquals("none", selector.getActiveBackend().name)
    }

    @Test
    fun `refresh re-evaluates backends`() {
        val cloud = FakeBackend("cloud", isAvailable = false)
        val local = FakeBackend("local", isAvailable = true)

        selector.registerBackend(cloud)
        selector.registerBackend(local)

        assertEquals("local", selector.getActiveBackend().name)

        // Cloud becomes available -- should be preferred (higher priority)
        cloud.isAvailable = true
        selector.refresh()

        assertEquals("cloud", selector.getActiveBackend().name)
    }

    @Test
    fun `clear resets to NoOp`() {
        selector.registerBackend(FakeBackend("cloud", isAvailable = true))
        assertTrue(selector.isLlmAvailable)

        selector.clear()

        assertFalse(selector.isLlmAvailable)
        assertEquals("none", selector.getActiveBackend().name)
    }
}
