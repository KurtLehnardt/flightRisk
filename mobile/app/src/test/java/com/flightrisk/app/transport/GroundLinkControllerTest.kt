package com.flightrisk.app.transport

import com.flightrisk.app.ground.FakePort
import com.flightrisk.app.ground.MemoryStorage
import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.ground.SecretStore
import com.flightrisk.app.ground.eventually
import com.flightrisk.app.ground.FakeClock
import com.flightrisk.app.ground.WebSocketClosedException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class GroundLinkControllerTest {

    private class MemoryPrefs(var enabled: Boolean = false, var address: String = "") : GroundLinkPrefs {
        var saves = 0
        override fun enabled() = enabled
        override fun address() = address
        override fun save(enabled: Boolean, address: String) {
            this.enabled = enabled
            this.address = address
            saves++
        }
    }

    private class Harness(val prefs: MemoryPrefs = MemoryPrefs(), val storage: MemoryStorage = MemoryStorage()) {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val secrets = SecretStore(storage, isMainThread = { false })
        val port = FakePort()
        val clock = FakeClock()
        val created = java.util.concurrent.CopyOnWriteArrayList<EdgePublisher>()
        val controller = GroundLinkController(prefs, secrets, scope) { endpoint ->
            EdgePublisher(endpoint, { secrets.load(SecretName.EDGE_TOKEN) }, scope, port, clock = clock, random = { 0.0 })
                .also { created += it }
        }
        val state get() = controller.state.value
        fun close() = scope.cancel()
    }

    @Test
    fun `the link is off by default and nothing connects`() {
        val h = Harness()
        try {
            h.controller.setForeground(true)
            h.controller.detectionPublisher.onSearchStarted()
            assertFalse(h.state.enabled)
            assertEquals(0, h.created.size)
            assertEquals(0, h.port.opened.size)
            assertFalse(h.controller.detectionPublisher.wantsFrame())
            assertEquals("Off. Nothing is sent.", GroundLinkText.statusLine(h.state))
            assertNull(GroundLinkText.indicator(h.state))
        } finally {
            h.close()
        }
    }

    @Test
    fun `a saved token alone never starts the link`() {
        val h = Harness(storage = MemoryStorage(mapOf(SecretName.EDGE_TOKEN.storageKey to TEST_TOKEN)))
        try {
            h.controller.setForeground(true)
            h.controller.detectionPublisher.onSearchStarted()
            Thread.sleep(100)
            assertEquals(0, h.port.opened.size)
            runBlocking { eventually("token seen") { h.state.hasToken } }
            assertFalse(h.state.enabled)
        } finally {
            h.close()
        }
    }

    @Test
    fun `enabling saves the choice and connects only while the app is in view and a search runs`() {
        val h = Harness()
        try {
            assertTrue(h.controller.enable("192.168.1.20"))
            assertTrue(h.prefs.enabled)
            assertEquals("192.168.1.20:9000", h.prefs.address)
            assertEquals(0, h.created.size)

            h.controller.setForeground(true)
            assertEquals(0, h.created.size)
            h.controller.detectionPublisher.onSearchStarted()
            assertEquals(1, h.created.size)
            assertEquals("192.168.1.20", h.created[0].endpoint.host)
            assertEquals(9000, h.created[0].endpoint.port)
            runBlocking { h.port.connection(0) }
            runBlocking { eventually("connected") { h.state.status == EdgeLinkStatus.Connected } }
            assertEquals("Sending detections to 192.168.1.20:9000", GroundLinkText.indicator(h.state)?.text)
            assertTrue(GroundLinkText.indicator(h.state)!!.sending)

            h.controller.detectionPublisher.onSearchStopped()
            assertEquals(EdgeLinkStatus.Off, h.state.status)
            assertNull(GroundLinkText.indicator(h.state))
            runBlocking { eventually("closed") { h.port.opened[0].closed } }
        } finally {
            h.close()
        }
    }

    @Test
    fun `going to the background stops the link`() {
        val h = Harness()
        try {
            h.controller.enable("192.168.1.20:9000")
            h.controller.setForeground(true)
            h.controller.detectionPublisher.onSearchStarted()
            runBlocking { h.port.connection(0) }
            h.controller.setForeground(false)
            assertFalse(h.state.running)
            runBlocking { eventually("closed") { h.port.opened[0].closed } }
            assertFalse(h.controller.detectionPublisher.wantsFrame())
            h.controller.setForeground(true)
            runBlocking { h.port.connection(1) }
        } finally {
            h.close()
        }
    }

    @Test
    fun `disabling stops the link and is saved`() {
        val h = Harness()
        try {
            h.controller.enable("192.168.1.20")
            h.controller.setForeground(true)
            h.controller.detectionPublisher.onSearchStarted()
            runBlocking { h.port.connection(0) }
            h.controller.disable()
            assertFalse(h.prefs.enabled)
            assertFalse(h.state.running)
            runBlocking { eventually("closed") { h.port.opened[0].closed } }
        } finally {
            h.close()
        }
    }

    @Test
    fun `a refused address shows its reason and the link stays off`() {
        val h = Harness()
        try {
            assertFalse(h.controller.enable("wss://192.168.1.20"))
            assertTrue(h.state.addressProblem!!.contains("encrypted"))
            assertFalse(h.state.enabled)
            assertFalse(h.prefs.enabled)
            assertFalse(h.controller.enable(""))
            assertEquals("Enter the ground station's address.", h.state.addressProblem)
            assertEquals(0, h.prefs.saves)
        } finally {
            h.close()
        }
    }

    @Test
    fun `changing the address turns the link off so consent covers the new destination`() {
        val h = Harness()
        try {
            h.controller.enable("192.168.1.20")
            h.controller.setForeground(true)
            h.controller.detectionPublisher.onSearchStarted()
            runBlocking { h.port.connection(0) }
            assertTrue(h.controller.changeAddress("192.168.1.99"))
            assertFalse(h.state.enabled)
            assertFalse(h.prefs.enabled)
            assertEquals("192.168.1.99:9000", h.state.address)
            assertFalse(h.state.running)
            // Saving the same address again changes nothing.
            h.controller.enable("192.168.1.99")
            assertTrue(h.controller.changeAddress("192.168.1.99:9000"))
            assertTrue(h.state.enabled)
        } finally {
            h.close()
        }
    }

    @Test
    fun `a saved on without a usable address is treated as off`() {
        val h = Harness(MemoryPrefs(enabled = true, address = "not a host"))
        try {
            assertFalse(h.state.enabled)
        } finally {
            h.close()
        }
        val h2 = Harness(MemoryPrefs(enabled = true, address = "192.168.1.20:9000"))
        try {
            assertTrue(h2.state.enabled)
            assertEquals(0, h2.created.size)
        } finally {
            h2.close()
        }
    }

    @Test
    fun `saving a token stores it, never echoes it, and reconnects with it`() {
        val h = Harness()
        try {
            h.controller.enable("192.168.1.20")
            h.controller.setForeground(true)
            h.controller.detectionPublisher.onSearchStarted()
            runBlocking { h.port.connection(0) }
            h.controller.saveToken("  $TEST_TOKEN ")
            runBlocking { eventually("saved") { h.state.hasToken } }
            assertEquals(TEST_TOKEN, h.storage.map[SecretName.EDGE_TOKEN.storageKey])
            assertEquals("Token saved.", h.state.tokenMessage)
            assertFalse(h.state.toString().contains(TEST_TOKEN))
            assertFalse(h.controller.toString().contains(TEST_TOKEN))
            runBlocking { h.port.connection(1) }
            runBlocking { eventually("auth with the new token") { h.port.opened[1].sent.firstOrNull()?.contains(TEST_TOKEN) == true } }
        } finally {
            h.close()
        }
    }

    @Test
    fun `a bad token is refused without being stored and the remove button deletes a saved one`() {
        val h = Harness()
        try {
            h.controller.saveToken("has space")
            assertTrue(h.state.tokenMessage!!.startsWith("The token was not saved"))
            assertFalse(h.state.tokenMessage!!.contains("has space"))
            assertNull(h.storage.map[SecretName.EDGE_TOKEN.storageKey])
            h.controller.saveToken(TEST_TOKEN)
            runBlocking { eventually("saved") { h.state.hasToken } }
            h.controller.removeToken()
            runBlocking { eventually("removed") { !h.state.hasToken } }
            assertNull(h.storage.map[SecretName.EDGE_TOKEN.storageKey])
            assertEquals("Token removed.", h.state.tokenMessage)
        } finally {
            h.close()
        }
    }

    @Test
    fun `when secure storage fails the token is not saved and the message says so`() {
        val storage = MemoryStorage().also { it.failWrites = true }
        val h = Harness(storage = storage)
        try {
            h.controller.saveToken(TEST_TOKEN)
            runBlocking { eventually("message") { h.state.tokenMessage != null } }
            assertTrue(h.state.tokenMessage!!.contains("secure storage is unavailable"))
            assertFalse(h.state.hasToken)
            assertFalse(h.state.tokenMessage!!.contains(TEST_TOKEN))
        } finally {
            h.close()
        }
    }

    @Test
    fun `a rejected token shows the fix-it message and survives the search ending`() {
        val h = Harness()
        try {
            h.controller.enable("192.168.1.20")
            h.controller.setForeground(true)
            h.controller.detectionPublisher.onSearchStarted()
            val c = runBlocking { h.port.connection(0) }
            c.serverError(WebSocketClosedException(4001, "unauthorized"))
            runBlocking { eventually("rejected") { h.state.status == EdgeLinkStatus.TokenRejected } }
            assertEquals("The ground rejected the edge token. Fix it in Settings.", GroundLinkText.statusLine(h.state))
            assertEquals("The ground rejected the edge token. Fix it in Settings.", GroundLinkText.indicator(h.state)?.text)
            assertFalse(GroundLinkText.indicator(h.state)!!.sending)
            h.controller.detectionPublisher.onSearchStopped()
            assertEquals(EdgeLinkStatus.TokenRejected, h.state.status)
            // Fixing the token starts clean.
            h.controller.saveToken(TEST_TOKEN)
            runBlocking { eventually("cleared") { h.state.status != EdgeLinkStatus.TokenRejected } }
            assertEquals(1, h.port.opened.size)
        } finally {
            h.close()
        }
    }

    @Test
    fun `status lines and the indicator follow the link state`() {
        val on = GroundLinkUiState(enabled = true, address = "192.168.1.20:9000")
        assertTrue(GroundLinkText.statusLine(on).startsWith("On."))
        assertEquals("Connecting to 192.168.1.20:9000...", GroundLinkText.statusLine(on.copy(status = EdgeLinkStatus.Connecting(1))))
        assertTrue(GroundLinkText.statusLine(on.copy(status = EdgeLinkStatus.Reconnecting(2, 2000, "IOException"))).contains("2 s"))
        assertTrue(GroundLinkText.statusLine(on.copy(status = EdgeLinkStatus.AddressRefused("must be private"))).contains("must be private"))
        assertNull(GroundLinkText.indicator(on))
        assertNull(GroundLinkText.indicator(on.copy(status = EdgeLinkStatus.Connecting(1))))
        assertNull(GroundLinkText.indicator(on.copy(status = EdgeLinkStatus.Reconnecting(1, 1000, "x"))))
        // Off wins over any leftover status.
        assertNull(GroundLinkText.indicator(on.copy(enabled = false, status = EdgeLinkStatus.Connected)))
    }

    @Test
    fun `the disclosure names what is sent, where, and that it is not encrypted`() {
        val d = GroundLinkText.DISCLOSURE
        for (word in listOf("detects", "small picture of each person", "appearance data", "address you entered", "not encrypted", "turn this off")) {
            assertTrue("missing '$word'", d.contains(word))
        }
    }

    @Test
    fun `the token normalizer accepts printable text only`() {
        assertEquals("abc-123", GroundLinkController.normalizedToken(" abc-123 "))
        assertNull(GroundLinkController.normalizedToken(""))
        assertNull(GroundLinkController.normalizedToken("a b"))
        assertNull(GroundLinkController.normalizedToken("a\nb"))
        assertNull(GroundLinkController.normalizedToken("x".repeat(513)))
    }
}
