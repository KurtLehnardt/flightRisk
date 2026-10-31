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
    fun `endpoints parse what a user types and refuse the rest`() {
        assertEquals(GroundEndpoint.of("192.168.1.20", 5555), GroundEndpoint.parse("192.168.1.20"))
        assertEquals(GroundEndpoint.of("192.168.1.20", 8080), GroundEndpoint.parse("http://192.168.1.20:8080/"))
        assertEquals(GroundEndpoint.of("ground.local", 5555), GroundEndpoint.parse("ground.local"))
        assertEquals(GroundEndpoint.of("fe80::1", 5555), GroundEndpoint.parse("[fe80::1]:5555"))
        assertEquals("[fe80::1]:5555", GroundEndpoint.parse("[fe80::1]:5555")!!.hostHeader)
        assertNull(GroundEndpoint.parse("https://192.168.1.20"))
        assertNull(GroundEndpoint.parse("http://user@192.168.1.20"))
        assertNull(GroundEndpoint.parse("192.168.1.20/socket.io"))
        assertNull(GroundEndpoint.parse("192.168.1.20:0"))
        assertNull(GroundEndpoint.parse("192.168.1.20:70000"))
        assertNull(GroundEndpoint.parse("bad host"))
        assertNull(GroundEndpoint.parse(""))
    }
}
