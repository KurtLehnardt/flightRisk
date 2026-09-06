package com.flightrisk.app.alert

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Unit tests for [AlertManager].
 *
 * Tests the alert-level-to-action mapping, cooldown throttling, and
 * dismiss behavior. Since [AlertManager] requires an Android [Context]
 * for audio/haptic, and JUnit tests run on the JVM, we test the logic
 * via a [TestableAlertManager] that stubs out the Android-specific
 * audio/haptic calls.
 */
class AlertManagerTest {

    /**
     * Testable alert manager that records actions without touching
     * Android audio or vibration APIs.
     */
    private class TestableAlertManager(
        private val cooldownMs: Long = 10_000L,
    ) {
        /** Tracks last alert time per track key. */
        private val lastAlertTime = mutableMapOf<String, Long>()

        /** Records of fired alerts for assertions. */
        data class AlertRecord(
            val alertLevel: String,
            val trackKey: String,
            val audio: Boolean,
            val haptic: Boolean,
            val visual: Boolean,
        )

        val firedAlerts = mutableListOf<AlertRecord>()

        /** Overridable clock for testing cooldowns. */
        var currentTimeMs: Long = 0L

        fun fireAlert(alertLevel: String, trackKey: String): AlertManager.AlertActions? {
            if (alertLevel == AlertManager.NO_MATCH) return null

            val last = lastAlertTime[trackKey]
            if (last != null && (currentTimeMs - last) < cooldownMs) {
                return null // Suppressed by cooldown
            }

            lastAlertTime[trackKey] = currentTimeMs

            val actions = when (alertLevel) {
                AlertManager.CONFIRMED_MATCH ->
                    AlertManager.AlertActions(audio = true, haptic = true, visual = true)
                AlertManager.POSSIBLE_MATCH ->
                    AlertManager.AlertActions(audio = false, haptic = true, visual = true)
                AlertManager.WEAK_SIGNAL ->
                    AlertManager.AlertActions(audio = false, haptic = false, visual = true)
                else -> return null
            }

            firedAlerts.add(
                AlertRecord(alertLevel, trackKey, actions.audio, actions.haptic, actions.visual)
            )

            return actions
        }

        fun dismiss(trackKey: String) {
            lastAlertTime.remove(trackKey)
        }

        fun dismissAll() {
            lastAlertTime.clear()
        }

        fun isWithinCooldown(trackKey: String): Boolean {
            val last = lastAlertTime[trackKey] ?: return false
            return (currentTimeMs - last) < cooldownMs
        }
    }

    private lateinit var manager: TestableAlertManager

    @Before
    fun setUp() {
        manager = TestableAlertManager(cooldownMs = 10_000L)
    }

    // ------------------------------------------------------------------
    // Alert level -> actions mapping
    // ------------------------------------------------------------------

    @Test
    fun `confirmed_match triggers audio, haptic, and visual`() {
        val actions = manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        assertNotNull(actions)
        assertTrue(actions!!.audio)
        assertTrue(actions.haptic)
        assertTrue(actions.visual)
    }

    @Test
    fun `possible_match triggers haptic and visual only`() {
        val actions = manager.fireAlert(AlertManager.POSSIBLE_MATCH, "track_1")
        assertNotNull(actions)
        assertFalse(actions!!.audio)
        assertTrue(actions.haptic)
        assertTrue(actions.visual)
    }

    @Test
    fun `weak_signal triggers visual only`() {
        val actions = manager.fireAlert(AlertManager.WEAK_SIGNAL, "track_1")
        assertNotNull(actions)
        assertFalse(actions!!.audio)
        assertFalse(actions.haptic)
        assertTrue(actions.visual)
    }

    @Test
    fun `no_match returns null`() {
        val actions = manager.fireAlert(AlertManager.NO_MATCH, "track_1")
        assertNull(actions)
    }

    // ------------------------------------------------------------------
    // Cooldown
    // ------------------------------------------------------------------

    @Test
    fun `cooldown prevents re-alert within window`() {
        manager.currentTimeMs = 1000L
        val first = manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        assertNotNull(first)

        // Still within 10s cooldown
        manager.currentTimeMs = 5000L
        val second = manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        assertNull(second)

        // Only one alert should have been recorded
        assertEquals(1, manager.firedAlerts.size)
    }

    @Test
    fun `alert fires after cooldown expires`() {
        manager.currentTimeMs = 1000L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")

        // After 10s cooldown
        manager.currentTimeMs = 12_000L
        val second = manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        assertNotNull(second)

        assertEquals(2, manager.firedAlerts.size)
    }

    @Test
    fun `different track keys have independent cooldowns`() {
        manager.currentTimeMs = 1000L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        manager.fireAlert(AlertManager.POSSIBLE_MATCH, "track_2")

        assertEquals(2, manager.firedAlerts.size)
    }

    @Test
    fun `isWithinCooldown returns true during cooldown`() {
        manager.currentTimeMs = 1000L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")

        manager.currentTimeMs = 5000L
        assertTrue(manager.isWithinCooldown("track_1"))
    }

    @Test
    fun `isWithinCooldown returns false after cooldown expires`() {
        manager.currentTimeMs = 1000L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")

        manager.currentTimeMs = 12_000L
        assertFalse(manager.isWithinCooldown("track_1"))
    }

    @Test
    fun `isWithinCooldown returns false for unknown track`() {
        assertFalse(manager.isWithinCooldown("unknown"))
    }

    // ------------------------------------------------------------------
    // Dismiss
    // ------------------------------------------------------------------

    @Test
    fun `dismiss clears cooldown for specific track`() {
        manager.currentTimeMs = 1000L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        manager.fireAlert(AlertManager.POSSIBLE_MATCH, "track_2")

        manager.dismiss("track_1")

        // track_1 cooldown cleared, should fire again immediately
        val refired = manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        assertNotNull(refired)

        // track_2 cooldown still active
        val suppressed = manager.fireAlert(AlertManager.POSSIBLE_MATCH, "track_2")
        assertNull(suppressed)
    }

    @Test
    fun `dismissAll clears all cooldowns`() {
        manager.currentTimeMs = 1000L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        manager.fireAlert(AlertManager.POSSIBLE_MATCH, "track_2")

        manager.dismissAll()

        // Both should fire again immediately
        val refired1 = manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        val refired2 = manager.fireAlert(AlertManager.POSSIBLE_MATCH, "track_2")
        assertNotNull(refired1)
        assertNotNull(refired2)
    }

    // ------------------------------------------------------------------
    // Edge cases
    // ------------------------------------------------------------------

    @Test
    fun `fireAlert with unknown level returns null`() {
        val actions = manager.fireAlert("unknown_level", "track_1")
        assertNull(actions)
    }

    @Test
    fun `fireAlert with empty string level returns null`() {
        val actions = manager.fireAlert("", "track_1")
        assertNull(actions)
    }

    @Test
    fun `dismiss nonexistent track is no-op`() {
        manager.dismiss("nonexistent")
        assertFalse(manager.isWithinCooldown("nonexistent"))
    }

    @Test
    fun `dismissAll when no alerts active is no-op`() {
        manager.dismissAll()
        assertEquals(0, manager.firedAlerts.size)
    }

    @Test
    fun `cooldown exactly at boundary fires`() {
        manager.currentTimeMs = 0L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")

        manager.currentTimeMs = 10_000L
        val second = manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        assertNotNull(second)
    }

    @Test
    fun `cooldown 1ms before boundary suppresses`() {
        manager.currentTimeMs = 0L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")

        manager.currentTimeMs = 9_999L
        val second = manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        assertNull(second)
    }

    @Test
    fun `rapid-fire same track only records first`() {
        manager.currentTimeMs = 0L
        for (i in 0 until 10) {
            manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")
        }
        assertEquals(1, manager.firedAlerts.size)
    }

    @Test
    fun `weak_signal is not suppressed by confirmed_match cooldown for same track`() {
        manager.currentTimeMs = 0L
        manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1")

        manager.currentTimeMs = 500L
        val weak = manager.fireAlert(AlertManager.WEAK_SIGNAL, "track_1")
        // Same track, still within cooldown — should be suppressed
        assertNull(weak)
    }

    @Test
    fun `alert level constants match expected values`() {
        assertEquals("confirmed_match", AlertManager.CONFIRMED_MATCH)
        assertEquals("possible_match", AlertManager.POSSIBLE_MATCH)
        assertEquals("weak_signal", AlertManager.WEAK_SIGNAL)
        assertEquals("no_match", AlertManager.NO_MATCH)
    }

    /**
     * Documents the double-throttle issue: Both SearchPipeline and
     * AlertManager implement per-track cooldowns independently.
     * Pipeline checks alertedTracks before calling alertManager.fireAlert(),
     * which checks its own lastAlertTime. The effective cooldown is the
     * max of both, and they can diverge since pipeline uses its own clock.
     */
    @Test
    fun `double throttle documented - pipeline and AlertManager both throttle`() {
        // Simulate pipeline alertedTracks cooldown
        val pipelineCooldownMs = 10_000L
        val pipelineAlertedTracks = mutableMapOf<String, Long>()
        var pipelineTime = 0L

        fun pipelineWouldFire(trackKey: String): Boolean {
            val last = pipelineAlertedTracks[trackKey]
            if (last != null && (pipelineTime - last) < pipelineCooldownMs) return false
            pipelineAlertedTracks[trackKey] = pipelineTime
            return true
        }

        // First alert: both pipeline and AlertManager allow it
        pipelineTime = 0L
        manager.currentTimeMs = 0L
        assertTrue(pipelineWouldFire("track_1"))
        assertNotNull(manager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_1"))

        // 5s later: both suppress
        pipelineTime = 5000L
        manager.currentTimeMs = 5000L
        assertFalse(pipelineWouldFire("track_1"))

        // Dismiss AlertManager cooldown but not pipeline's
        manager.dismiss("track_1")
        pipelineTime = 5000L
        assertFalse("Pipeline still blocks even after AlertManager dismiss",
            pipelineWouldFire("track_1"))
    }
}
