package com.flightrisk.mission

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class ScaffoldTest {
    @Test
    fun fixturesDirectoryExists() {
        assertTrue(MissionFixtures.dir.isDirectory)
    }

    @Test
    fun guardAllowsOnlyLoopback() {
        for (host in listOf("127.0.0.1", "::1", "[::1]", "10.0.2.2")) assertTrue(host, LoopbackGuard.isAllowed(host))
        for (host in listOf("192.168.10.1", "192.168.1.1", "localhost.evil.com", "8.8.8.8", "")) {
            assertFalse(host, LoopbackGuard.isAllowed(host))
        }
        assertEquals("127.0.0.1", LoopbackGuard.require("127.0.0.1"))
        try {
            LoopbackGuard.require("192.168.10.1")
            fail("expected refusal")
        } catch (_: IllegalArgumentException) {
        }
    }

    @Test
    fun moduleLoads() {
        assertEquals("mission", MissionCore.MODULE)
    }
}
