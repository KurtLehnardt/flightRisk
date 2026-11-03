package com.flightrisk.app.ground

import com.flightrisk.contract.ClientCommand
import com.flightrisk.contract.SocketIoFrames
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.lang.reflect.Modifier
import java.net.InetAddress

class EmitPolicyTest {

    private fun commandFor(event: String, payload: JsonObject?): ClientCommand {
        fun field(name: String) = (payload!![name] as JsonPrimitive).content
        return when (event) {
            "get_fleet_status" -> ClientCommand.GetFleetStatus
            "get_detection_profiles" -> ClientCommand.GetDetectionProfiles
            "get_ceiling_settings" -> ClientCommand.GetCeilingSettings
            "get_reasoning_provider" -> ClientCommand.GetReasoningProvider
            "get_manual_status" -> ClientCommand.GetManualStatus
            "land_all" -> ClientCommand.LandAll
            "drone_command" -> ClientCommand.landDrone(field("drone_id"))!!
            "set_detection_profile" -> ClientCommand.setDetectionProfile(field("drone_id"), field("profile"))!!
            "set_fleet_detection_profile" -> ClientCommand.SetFleetDetectionProfile(field("profile"))
            "manual_resume_search" -> ClientCommand.resumeHeldDrone(field("drone_id"))!!
            else -> throw AssertionError("no command for $event")
        }
    }

    @Test
    fun `the default policy is exactly the fixture allow-list and excludes every forbidden event`() {
        val cases = ContractFixtures.cases("client_emits.json")
        val allowed = cases.values.filter { (it["allowed"] as JsonPrimitive).boolean }
            .map { (it["event"] as JsonPrimitive).content }.toSet()
        val forbidden = cases.values.filter { !(it["allowed"] as JsonPrimitive).boolean }
            .map { (it["event"] as JsonPrimitive).content }.toSet()
        assertEquals(10, allowed.size)
        assertEquals(allowed, EmitPolicy.CONTRACT.allowedEvents)
        assertTrue(forbidden.isNotEmpty())
        assertTrue(forbidden.none { it in EmitPolicy.CONTRACT.allowedEvents })
    }

    @Test
    fun `every allowed fixture encodes to the same payload`() {
        for ((name, case) in ContractFixtures.cases("client_emits.json")) {
            if (!(case["allowed"] as JsonPrimitive).boolean) continue
            val event = (case["event"] as JsonPrimitive).content
            val payload = case["payload"].takeUnless { it is JsonNull } as JsonObject?
            val command = commandFor(event, payload)
            assertTrue(name, EmitPolicy.CONTRACT.allows(command))
            val expected = "42[" + JsonPrimitive(event) + (payload?.let { ",$it" } ?: "") + "]"
            assertEquals(name, expected, SocketIoFrames.event(command))
        }
    }

    @Test
    fun `a policy can only narrow, and land_all always passes`() {
        val narrowed = EmitPolicy.CONTRACT.narrowedTo(setOf("get_fleet_status", "plan_fleet", "start_fleet"))
        assertEquals(setOf("get_fleet_status", "land_all"), narrowed.allowedEvents)
        assertFalse(narrowed.allows(ClientCommand.landDrone("drone-1")!!))
        val empty = EmitPolicy.CONTRACT.narrowedTo(emptySet()).without("land_all")
        assertEquals(setOf("land_all"), empty.allowedEvents)
        assertTrue(empty.allows(ClientCommand.LandAll))
        assertFalse(empty.allows(ClientCommand.GetFleetStatus))
        // Narrowing a narrowed policy never brings an event back.
        assertEquals(setOf("land_all"), empty.narrowedTo(ClientCommand.ALLOWED_EVENTS).allowedEvents)
    }

    @Test
    fun `the client has no way to emit a free-form event name`() {
        val sendLike = GroundConsoleClient::class.java.methods.filter {
            Modifier.isPublic(it.modifiers) && (it.name.startsWith("send") || it.name.startsWith("emit"))
        }
        assertTrue(sendLike.isNotEmpty())
        for (m in sendLike) {
            // Suspend functions take a trailing Continuation; no parameter may be a String.
            assertTrue(m.toString(), m.parameterTypes.none { it == String::class.java })
            assertTrue(m.toString(), m.parameterTypes.first() == ClientCommand::class.java)
        }
    }
}

class EndpointAndAddressTest {

    private fun ip(text: String): InetAddress = InetAddress.getByName(text)

    @Test
    fun `private, link-local and loopback addresses are allowed`() {
        for (a in listOf(
            "127.0.0.1", "127.1.2.3", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.254",
            "192.168.0.1", "192.168.10.20", "169.254.1.1", "::1", "fe80::1", "fd12:3456::1", "fc00::1",
        )) {
            assertTrue(a, LanAddressPolicy.isAllowed(ip(a)))
        }
    }

    @Test
    fun `public and special addresses are refused`() {
        for (a in listOf(
            "8.8.8.8", "172.15.255.255", "172.32.0.1", "192.169.0.1", "100.64.0.1", "0.0.0.0",
            "224.0.0.1", "255.255.255.255", "2001:4860:4860::8888", "::",
        )) {
            assertFalse(a, LanAddressPolicy.isAllowed(ip(a)))
        }
    }

    @Test
    fun `a name is refused when any of its addresses is public`() {
        val resolver = HostResolver { listOf(ip("192.168.1.20"), ip("8.8.8.8")) }
        try {
            LanAddressPolicy.resolve("ground.example", resolver)
            throw AssertionError("expected a refusal")
        } catch (e: AddressNotAllowedException) {
            assertTrue(e.message!!.contains("8.8.8.8"))
        }
        assertEquals(ip("192.168.1.20"), LanAddressPolicy.resolve("ground.example", HostResolver { listOf(ip("192.168.1.20")) }))
    }

    @Test
    fun `IPv4 mapped IPv6 addresses are judged by the IPv4 part`() {
        fun mapped(a: Int, b: Int, c: Int, d: Int): InetAddress =
            InetAddress.getByAddress(
                byteArrayOf(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1, -1, a.toByte(), b.toByte(), c.toByte(), d.toByte()),
            )
        assertTrue(LanAddressPolicy.isAllowed(mapped(192, 168, 1, 5)))
        assertTrue(LanAddressPolicy.isAllowed(mapped(10, 0, 0, 1)))
        assertTrue(LanAddressPolicy.isAllowed(mapped(127, 0, 0, 1)))
        assertFalse(LanAddressPolicy.isAllowed(mapped(8, 8, 8, 8)))
        assertFalse(LanAddressPolicy.isAllowed(mapped(100, 64, 0, 1)))
        assertFalse(LanAddressPolicy.isAllowed(mapped(172, 32, 0, 1)))
        assertFalse(LanAddressPolicy.isAllowed(ip("::ffff:8.8.8.8")))
        assertTrue(LanAddressPolicy.isAllowed(ip("::ffff:192.168.1.5")))
    }

    @Test
    fun `IPv6 ranges follow fc00 7 and fe80 10 exactly`() {
        for (a in listOf("fc00::", "fdff:ffff::1", "fe80::", "febf:ffff::1", "::1")) assertTrue(a, LanAddressPolicy.isAllowed(ip(a)))
        for (a in listOf("fb00::1", "fe00::1", "fec0::1", "ff02::1", "2001:db8::1", "2606:4700::1111", "::2", "64:ff9b::808:808")) {
            assertFalse(a, LanAddressPolicy.isAllowed(ip(a)))
        }
    }

    @Test
    fun `localhost and dot local names are allowed only when every answer is private`() {
        val localhostBoth = HostResolver { listOf(ip("127.0.0.1"), ip("::1")) }
        assertEquals(ip("127.0.0.1"), LanAddressPolicy.resolve("localhost", localhostBoth))
        // A .local name that also publishes a global IPv6 address is refused: the stricter rule both apps follow.
        val localWithGlobal = HostResolver { listOf(ip("192.168.1.20"), ip("2001:db8::20")) }
        try {
            LanAddressPolicy.resolve("ground.local", localWithGlobal)
            fail("a .local name with a public answer must be refused")
        } catch (e: AddressNotAllowedException) {
            assertTrue(e.message!!.contains("2001:db8"))
        }
        // The order of the answers does not matter.
        try {
            LanAddressPolicy.resolve("ground.local", HostResolver { listOf(ip("2001:db8::20"), ip("192.168.1.20")) })
            fail("a .local name with a public answer must be refused")
        } catch (_: AddressNotAllowedException) {
        }
        val localOnly = HostResolver { listOf(ip("fd12:3456::20"), ip("192.168.1.20")) }
        assertEquals(ip("fd12:3456::20"), LanAddressPolicy.resolve("ground.local", localOnly))
        // A name that is really a public host pretending to be local is refused too.
        try {
            LanAddressPolicy.resolve("localhost", HostResolver { listOf(ip("127.0.0.1"), ip("8.8.8.8")) })
            fail("localhost with a public answer must be refused")
        } catch (_: AddressNotAllowedException) {
        }
    }

    @Test
    fun `https and wss addresses are secure endpoints on any host with the TLS default port`() {
        val secure = GroundEndpoint.of("ground.example.org", 443, secure = true)!!
        assertEquals(secure, GroundEndpoint.parse("https://ground.example.org"))
        assertEquals(secure, GroundEndpoint.parse("wss://ground.example.org/"))
        assertEquals(GroundEndpoint.of("ground.example.org", 8443, secure = true), GroundEndpoint.parse("https://ground.example.org:8443"))
        assertEquals(GroundEndpoint.of("8.8.8.8", 443, secure = true), GroundEndpoint.parse("https://8.8.8.8"))
        assertTrue(secure.secure)
        assertFalse(GroundEndpoint.parse("http://192.168.1.20")!!.secure)
        // The secure flag is part of the identity and survives the settings round trip.
        assertFalse(secure == GroundEndpoint.of("ground.example.org", 443))
        assertEquals("https://ground.example.org:443", secure.savedText)
        assertEquals(secure, GroundEndpoint.parse(secure.savedText))
        assertEquals("192.168.1.20:5555", GroundEndpoint.parse("192.168.1.20")!!.savedText)
        assertEquals("https://ground.example.org:443", secure.httpBase)
        // Everything else about the address is still checked.
        assertNull(GroundEndpoint.parse("https://ground.example.org/path"))
        assertNull(GroundEndpoint.parse("https://user@ground.example.org"))
        assertNull(GroundEndpoint.parse("https://[fe80::1%wlan0]"))
    }

    @Test
    fun `a secure endpoint reaches a public address and a plain one still does not`() {
        val publicAnswer = HostResolver { listOf(ip("8.8.8.8")) }
        assertEquals(ip("8.8.8.8"), resolveTarget(GroundEndpoint.of("ground.example.org", 443, secure = true)!!, publicAnswer))
        try {
            resolveTarget(GroundEndpoint.of("ground.example.org", 5555)!!, publicAnswer)
            fail("a plain endpoint must not reach a public address")
        } catch (_: AddressNotAllowedException) {
        }
        assertFailsToResolve(GroundEndpoint.of("ground.example.org", 443, secure = true)!!)
    }

    private fun assertFailsToResolve(endpoint: GroundEndpoint) {
        try {
            resolveTarget(endpoint, HostResolver { emptyList() })
            fail("an empty answer must not resolve")
        } catch (_: java.io.IOException) {
        }
    }

    @Test
    fun `endpoints parse what a user types and refuse the rest`() {
        assertEquals(GroundEndpoint.of("192.168.1.20", 5555), GroundEndpoint.parse("192.168.1.20"))
        assertEquals(GroundEndpoint.of("192.168.1.20", 8080), GroundEndpoint.parse("http://192.168.1.20:8080/"))
        assertEquals(GroundEndpoint.of("ground.local", 5555), GroundEndpoint.parse("ground.local"))
        assertEquals(GroundEndpoint.of("fe80::1", 5555), GroundEndpoint.parse("[fe80::1]:5555"))
        assertEquals("[fe80::1]:5555", GroundEndpoint.parse("[fe80::1]:5555")!!.hostHeader)
        assertNull(GroundEndpoint.parse("ftp://192.168.1.20"))
        assertNull(GroundEndpoint.parse("ws://192.168.1.20"))
        assertNull(GroundEndpoint.parse("http://user@192.168.1.20"))
        assertNull(GroundEndpoint.parse("192.168.1.20/socket.io"))
        assertNull(GroundEndpoint.parse("192.168.1.20:0"))
        assertNull(GroundEndpoint.parse("192.168.1.20:70000"))
        assertNull(GroundEndpoint.parse("bad host"))
        assertNull(GroundEndpoint.parse(""))
        assertTrue((GroundEndpoint.parseDetailed("ftp://192.168.1.20") as EndpointParseResult.Invalid).reason.contains("https"))
        assertTrue((GroundEndpoint.parseDetailed("[fe80::1%wlan0]:5555") as EndpointParseResult.Invalid).reason.contains("zone"))
        assertTrue((GroundEndpoint.parseDetailed("192.168.1.20:99999") as EndpointParseResult.Invalid).reason.contains("port"))
        assertEquals(
            EndpointParseResult.Ok(GroundEndpoint.of("192.168.1.20", 5555)!!),
            GroundEndpoint.parseDetailed("http://192.168.1.20:5555"),
        )
        assertNull(GroundEndpoint.of("fe80:::1"))
        assertNull(GroundEndpoint.of("1::2::3"))
        assertNull(GroundEndpoint.of("12345::1"))
        assertNull(GroundEndpoint.of("1:2:3"))
        assertEquals("[2001:db8:0:0:0:0:0:1]:5555", GroundEndpoint.of("2001:db8:0:0:0:0:0:1")!!.hostHeader)
    }
}
