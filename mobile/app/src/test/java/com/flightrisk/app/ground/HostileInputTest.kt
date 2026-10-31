package com.flightrisk.app.ground

import com.flightrisk.contract.SocketIoFrames
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger

class JsonGuardTest {

    private fun nestedArrays(depth: Int) = "[".repeat(depth) + "]".repeat(depth)
    private fun nestedObjects(depth: Int) = "{\"a\":".repeat(depth) + "1" + "}".repeat(depth)

    @Test
    fun `deep arrays and objects are refused, shallow ones pass`() {
        assertNull(JsonGuard.check(nestedArrays(32)))
        assertNotNull(JsonGuard.check(nestedArrays(33)))
        assertNotNull(JsonGuard.check(nestedArrays(5_000)))
        assertNull(JsonGuard.check(nestedObjects(32)))
        assertNotNull(JsonGuard.check(nestedObjects(33)))
    }

    @Test
    fun `brackets inside strings, escaped quotes included, do not count`() {
        val s = "[\"" + "[".repeat(10_000) + "\\\"" + "{".repeat(10_000) + "\"]"
        assertNull(JsonGuard.check(s))
        assertNull(JsonGuard.check("42[\"x\",\"a\\\\\",[1]]"))
    }

    @Test
    fun `too many values is refused`() {
        val many = "[" + "1,".repeat(100_000) + "1]"
        assertNotNull(JsonGuard.check(many))
        assertNull(JsonGuard.check("[" + "1,".repeat(1_000) + "1]"))
    }

    @Test
    fun `every contract fixture passes the guard`() {
        for (file in ContractFixtures.dir.listFiles()!!.filter { it.name.endsWith(".json") }) {
            for ((name, case) in ContractFixtures.cases(file.name)) {
                val payload = case["payload"] ?: continue
                assertNull("${file.name}/$name", JsonGuard.check(payload.toString()))
            }
        }
    }

    @Test
    fun `the unguarded parser overflows on a 10 KB frame, which is why the guard exists`() {
        val frame = "42[\"status\"," + nestedArrays(5_000) + "]"
        var thrown: Throwable? = null
        val t = Thread(null, {
            try {
                SocketIoFrames.parse(frame)
            } catch (e: Throwable) {
                thrown = e
            }
        }, "small-stack", 1L shl 20)
        t.start()
        t.join()
        assertTrue("expected a StackOverflowError, got $thrown", thrown is StackOverflowError)
        assertNotNull(JsonGuard.check(frame))
    }
}

class HostileFrameClientTest {
    private val open = ContractFixtures.frame("engineio_open")
    private val ack = ContractFixtures.frame("socketio_connect_ack")

    private class H(val clock: FakeClock = FakeClock(), val port: SocketIoPort = FakePort()) {
        val errors = CopyOnWriteArrayList<Throwable>()
        val scope = CoroutineScope(
            SupervisorJob() + Dispatchers.Default +
                kotlinx.coroutines.CoroutineExceptionHandler { _, e -> errors += e },
        )
        val log = CapturingLog()
        val client = GroundConsoleClient(
            GroundEndpoint.of("127.0.0.1", 5555)!!, DashboardApiKey.of(TEST_KEY), scope, port = port, clock = clock,
            random = { 0.0 }, logSink = log,
        )
        val fake: FakePort get() = port as FakePort
        val status: ConnectionStatus get() = client.connection.value
    }

    private fun test(block: suspend H.() -> Unit) = runBlocking {
        val h = H()
        try {
            h.block()
            assertTrue("uncaught: ${h.errors}", h.errors.isEmpty())
        } finally {
            h.scope.cancel()
        }
    }

    private suspend fun H.reconnectsAfter(reason: (DropReason) -> Boolean) {
        eventually("dropped, now $status") { (status as? ConnectionStatus.Reconnecting)?.reason?.let(reason) == true }
        val opened = fake.opened.size
        val delayMs = (status as ConnectionStatus.Reconnecting).retryInMs
        eventually { (clock.nowMs() + delayMs) in clock.sleepers() }
        clock.advance(delayMs)
        eventually("the loop reconnects") { fake.opened.size == opened + 1 }
    }

    private fun deep(depth: Int, open: String, close: String) = "42[\"status\"," + open.repeat(depth) + close.repeat(depth) + "]"

    @Test
    fun `a deeply nested frame before the connect ack fails the session and the loop reconnects`() = test {
        client.start()
        val c = fake.connection(0)
        c.server(open)
        eventually { c.sent.isNotEmpty() }
        c.server(deep(200_000, "[", "]"))
        reconnectsAfter { it is DropReason.ProtocolError }
    }

    @Test
    fun `a deeply nested frame after the connect ack fails the session and the loop reconnects`() = test {
        client.start()
        val c = fake.connection(0)
        c.server(open)
        eventually { c.sent.isNotEmpty() }
        c.server(ack)
        eventually { status is ConnectionStatus.Connected }
        c.server(deep(5_000, "{\"a\":", "}"))
        reconnectsAfter { it is DropReason.ProtocolError }
    }

    @Test
    fun `a frame with too many values is refused before parsing`() = test {
        client.start()
        val c = fake.connection(0)
        c.server(open)
        eventually { c.sent.isNotEmpty() }
        c.server(ack)
        eventually { status is ConnectionStatus.Connected }
        c.server("42[\"status\",[" + "1,".repeat(200_000) + "1]]")
        reconnectsAfter { it is DropReason.ProtocolError }
    }

    @Test
    fun `a StackOverflowError or OutOfMemoryError from the transport reconnects with backoff`() = runBlocking {
        val clock = FakeClock()
        val calls = AtomicInteger()
        val port = object : SocketIoPort {
            override suspend fun open(endpoint: GroundEndpoint, secrets: List<String>): SocketIoConnection =
                object : SocketIoConnection {
                    override suspend fun receive(): String {
                        if (calls.incrementAndGet() == 1) throw StackOverflowError()
                        throw OutOfMemoryError("test")
                    }
                    override suspend fun send(frame: String) = Unit
                    override suspend fun closeGracefully() = Unit
                    override fun close() = Unit
                }
        }
        val h = H(clock, port)
        try {
            h.client.start()
            eventually { (h.status as? ConnectionStatus.Reconnecting)?.reason is DropReason.InternalError }
            val first = h.status as ConnectionStatus.Reconnecting
            eventually { (clock.nowMs() + first.retryInMs) in clock.sleepers() }
            clock.advance(first.retryInMs)
            eventually { (h.status as? ConnectionStatus.Reconnecting)?.attempt == 2 }
            assertTrue((h.status as ConnectionStatus.Reconnecting).reason is DropReason.InternalError)
            assertTrue(h.errors.isEmpty())
        } finally {
            h.scope.cancel()
        }
    }

    @Test
    fun `the handshake timeout also covers an open that never returns`() = runBlocking {
        val clock = FakeClock()
        val cancelled = AtomicInteger()
        val port = object : SocketIoPort {
            override suspend fun open(endpoint: GroundEndpoint, secrets: List<String>): SocketIoConnection {
                try {
                    awaitCancellation()
                } finally {
                    cancelled.incrementAndGet()
                }
            }
        }
        val h = H(clock, port)
        try {
            h.client.start()
            eventually { 10_000L in clock.sleepers() }
            clock.advance(10_000)
            eventually { (h.status as? ConnectionStatus.Reconnecting)?.reason == DropReason.HandshakeTimeout }
            assertEquals(1, cancelled.get())
        } finally {
            h.scope.cancel()
        }
    }

    @Test
    fun `a stalled consumer gets a SessionEnded marker in order, naming the lost event`() = runBlocking {
        val h = H()
        val client = GroundConsoleClient(
            GroundEndpoint.of("127.0.0.1", 5555)!!, DashboardApiKey.of(TEST_KEY), h.scope, port = h.port, clock = h.clock,
            config = GroundClientConfig(maxQueuedEvents = 5),
        )
        try {
            client.start()
            val c = h.fake.connection(0)
            c.server(open)
            eventually { c.sent.isNotEmpty() }
            c.server(ack)
            eventually { client.connection.value is ConnectionStatus.Connected }
            repeat(6) { c.server(ContractFixtures.eventFrame("land_all_ack.json", "fleet_reached_all")) }
            eventually { client.connection.value is ConnectionStatus.Reconnecting }
            val events = CopyOnWriteArrayList<GroundEvent>()
            h.scope.launch { client.events.collect { events += it } }
            eventually { events.any { it is GroundEvent.SessionEnded } }
            delay(50)
            val last = events.last() as GroundEvent.SessionEnded
            assertTrue(last.reason is DropReason.ConsumerStalled)
            assertEquals("LandAllAck", last.lostEvent)
            assertEquals(5, events.size) // SessionStarted, 3 acks, the marker
        } finally {
            h.scope.cancel()
        }
    }

    @Test
    fun `secrets the client only knows through extraSecrets are redacted before the cut`() = runBlocking {
        val claude = "claude-test-key"
        val h = H()
        val log = CapturingLog()
        val client = GroundConsoleClient(
            GroundEndpoint.of("127.0.0.1", 5555)!!, DashboardApiKey.of(TEST_KEY), h.scope, port = h.port, clock = h.clock,
            logSink = log, extraSecrets = { listOf(claude) },
        )
        try {
            client.start()
            val c = h.fake.connection(0)
            c.server(open)
            eventually { c.sent.isNotEmpty() }
            c.server("44{\"message\":\"" + "x".repeat(192) + claude + "y".repeat(100) + "\"}")
            eventually { client.connection.value is ConnectionStatus.Failed }
            val shown = ((client.connection.value as ConnectionStatus.Failed).reason as FailureReason.AuthRejected).serverMessage!!
            assertFalse(shown, shown.contains("claude"))
            for (line in log.lines) {
                for (n in 6..claude.length) assertFalse(line, line.contains(claude.substring(0, n)))
            }
        } finally {
            h.scope.cancel()
        }
    }

    @Test
    fun `a server-chosen tiny ping interval does not reset the backoff`() = test {
        val tinyOpen = "0{\"sid\":\"s\",\"upgrades\":[],\"pingTimeout\":1000,\"pingInterval\":1000,\"maxPayload\":1000000}"
        client.start()
        for (attempt in 1..3) {
            val c = fake.connection(attempt - 1)
            c.server(tinyOpen)
            eventually { c.sent.isNotEmpty() }
            c.server(ack)
            eventually { status is ConnectionStatus.Connected }
            c.server("2")
            eventually { c.sent.contains("3") }
            clock.advance(1_500)
            c.server("1")
            eventually { (status as? ConnectionStatus.Reconnecting)?.attempt == attempt }
            val delayMs = (status as ConnectionStatus.Reconnecting).retryInMs
            assertEquals(1_000L shl (attempt - 1), delayMs)
            eventually { (clock.nowMs() + delayMs) in clock.sleepers() }
            clock.advance(delayMs)
        }
    }

}
