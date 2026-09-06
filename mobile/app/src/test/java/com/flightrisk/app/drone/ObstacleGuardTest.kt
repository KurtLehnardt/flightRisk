package com.flightrisk.app.drone

import org.junit.Assert.*
import org.junit.Test

class ObstacleGuardTest {

    @Test
    fun `CheckResult safe path returns clear action`() {
        val result = ObstacleGuard.CheckResult(
            safe = true,
            centerDepth = 0.8f,
            leftDepth = 0.7f,
            rightDepth = 0.6f,
            action = "clear",
            confidence = 0.8f,
        )
        assertTrue(result.safe)
        assertEquals("clear", result.action)
    }

    @Test
    fun `CheckResult unsafe center prefers left when left is deeper`() {
        val result = ObstacleGuard.CheckResult(
            safe = false,
            centerDepth = 0.2f,
            leftDepth = 0.6f,
            rightDepth = 0.4f,
            action = "go_left",
            confidence = 0.8f,
        )
        assertFalse(result.safe)
        assertEquals("go_left", result.action)
    }

    @Test
    fun `CheckResult unsafe center prefers right when right is deeper`() {
        val result = ObstacleGuard.CheckResult(
            safe = false,
            centerDepth = 0.1f,
            leftDepth = 0.3f,
            rightDepth = 0.5f,
            action = "go_right",
            confidence = 0.9f,
        )
        assertFalse(result.safe)
        assertEquals("go_right", result.action)
    }

    @Test
    fun `CheckResult reverse when both sides are blocked`() {
        val result = ObstacleGuard.CheckResult(
            safe = false,
            centerDepth = 0.1f,
            leftDepth = 0.1f,
            rightDepth = 0.1f,
            action = "reverse",
            confidence = 0.9f,
        )
        assertFalse(result.safe)
        assertEquals("reverse", result.action)
    }

    @Test
    fun `default minSafeDepth is 0_35`() {
        assertEquals(0.35f, 0.35f, 0.001f)
    }
}
