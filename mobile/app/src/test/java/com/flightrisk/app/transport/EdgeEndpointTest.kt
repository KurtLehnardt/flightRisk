package com.flightrisk.app.transport

import com.flightrisk.app.ground.EndpointParseResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EdgeEndpointTest {
    private fun ok(text: String) = (EdgeEndpoint.parse(text) as EndpointParseResult.Ok).endpoint

    @Test
    fun `the port defaults to 9000`() {
        assertEquals(9000, ok("192.168.1.20").port)
        assertEquals(9000, ok("ground.local").port)
        assertEquals(9000, ok("ws://192.168.1.20").port)
        assertEquals(9000, ok("[fd00::1]").port)
    }

    @Test
    fun `an explicit port is kept and a ws prefix is accepted`() {
        assertEquals(9100, ok("192.168.1.20:9100").port)
        assertEquals(9100, ok("ws://192.168.1.20:9100/").port)
        assertEquals(5555, ok("192.168.1.20:5555").port)
        assertEquals(9100, ok("[fd00::1]:9100").port)
        assertEquals("192.168.1.20", ok(" 192.168.1.20 ").host)
    }

    @Test
    fun `wss and https are refused with a plain explanation`() {
        for (t in listOf("wss://192.168.1.20:9000", "https://ground.local")) {
            val r = EdgeEndpoint.parse(t)
            assertTrue(t, r is EndpointParseResult.Invalid)
            assertTrue((r as EndpointParseResult.Invalid).reason.contains("encrypted"))
        }
    }

    @Test
    fun `junk is refused with a reason`() {
        for (t in listOf("", "  ", "a b", "192.168.1.20:99999", "192.168.1.20/path", "user@host", "ftp://host", "[fe80::1%wlan0]")) {
            val r = EdgeEndpoint.parse(t)
            assertTrue("'$t' should be refused", r is EndpointParseResult.Invalid)
            assertTrue((r as EndpointParseResult.Invalid).reason.isNotBlank())
        }
        assertNull(EdgeEndpoint.parseOrNull("not a host"))
    }
}
