package com.flightrisk.app.transport

import com.flightrisk.app.ground.CapturingLog
import com.flightrisk.app.ground.FakeClock
import com.flightrisk.app.ground.GroundEndpoint
import com.flightrisk.app.ground.HostResolver
import com.flightrisk.app.ground.IoGuard
import com.flightrisk.app.ground.LoopbackServer
import com.flightrisk.app.ground.acceptUpgrade
import com.flightrisk.app.ground.eventually
import com.flightrisk.app.ground.readClientText
import com.flightrisk.app.ground.readRequestHead
import com.flightrisk.app.ground.writeServerFrame
import com.flightrisk.app.ground.writeServerText
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.InetAddress
import java.util.concurrent.CopyOnWriteArrayList

/** The publisher over a real [EdgeSocketPort] and [com.flightrisk.app.ground.RawWebSocket] to a loopback server. */
class EdgeLoopbackTest {

    private fun scope() = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private fun publisher(
        port: Int,
        scope: CoroutineScope,
        clock: FakeClock = FakeClock(),
        log: CapturingLog = CapturingLog(),
        edgePort: EdgeSocketPort = EdgeSocketPort(),
        host: String = "127.0.0.1",
    ) = EdgePublisher(
        endpoint = GroundEndpoint.of(host, port)!!,
        tokenProvider = { TEST_TOKEN },
        scope = scope,
        port = edgePort,
        clock = clock,
        random = { 0.0 },
        logSink = log,
    )

    @Test
    fun `a real WebSocket server sees auth first and then a detections message`() {
        val received = CopyOnWriteArrayList<String>()
        val heads = CopyOnWriteArrayList<List<String>>()
        LoopbackServer { socket ->
            val head = readRequestHead(socket.getInputStream())
            heads += head
            acceptUpgrade(head, socket.getOutputStream())
            // A hello from the ground must be ignored, not answered.
            writeServerText(socket.getOutputStream(), "{\"type\":\"hello\",\"proto\":2}")
            while (true) received += readClientText(socket.getInputStream())
        }.use { server ->
            val scope = scope()
            val log = CapturingLog()
            val p = publisher(server.port, scope, log = log)
            try {
                p.start()
                runBlocking { eventually("connected") { p.status.value == EdgeLinkStatus.Connected } }
                p.publish(testFrame())
                runBlocking { eventually("two frames") { received.size >= 2 } }
                assertEquals(listOf("auth", "detections"), received.map(::typeOf))
                assertTrue(heads.single().first().startsWith("GET / HTTP/1.1"))
                assertTrue("no token in the upgrade request", heads.single().none { it.contains(TEST_TOKEN) })
                assertTrue("no Origin header", heads.single().none { it.startsWith("Origin", ignoreCase = true) })
                val dump = log.lines.joinToString("\n") + p + p.status.value + p.stats()
                assertFalse("token in state dump", dump.contains(TEST_TOKEN))
                assertEquals(1, server.accepted.get())
            } finally {
                p.stop()
                scope.cancel()
            }
        }
    }

    @Test
    fun `a 4001 close frame over the wire stops the link and is not retried`() {
        LoopbackServer { socket ->
            val head = readRequestHead(socket.getInputStream())
            acceptUpgrade(head, socket.getOutputStream())
            readClientText(socket.getInputStream())
            val reason = "unauthorized".toByteArray()
            writeServerFrame(socket.getOutputStream(), 0x8, byteArrayOf((4001 shr 8).toByte(), (4001 and 0xff).toByte()) + reason)
            Thread.sleep(200)
        }.use { server ->
            val scope = scope()
            val clock = FakeClock()
            val p = publisher(server.port, scope, clock = clock)
            try {
                p.start()
                runBlocking { eventually("rejected") { p.status.value == EdgeLinkStatus.TokenRejected } }
                repeat(5) { clock.advance(60_000) }
                Thread.sleep(150)
                assertEquals(1, server.accepted.get())
                assertEquals(EdgeLinkStatus.TokenRejected, p.status.value)
            } finally {
                p.stop()
                scope.cancel()
            }
        }
    }

    @Test
    fun `an oversize message from the ground ends the session instead of being buffered`() {
        LoopbackServer { socket ->
            val head = readRequestHead(socket.getInputStream())
            acceptUpgrade(head, socket.getOutputStream())
            readClientText(socket.getInputStream())
            com.flightrisk.app.ground.streamHugeMessage(socket.getOutputStream(), 64L * 1024 * 1024)
        }.use { server ->
            val scope = scope()
            val p = publisher(server.port, scope)
            try {
                p.start()
                runBlocking { eventually("reconnecting") { p.status.value is EdgeLinkStatus.Reconnecting } }
                assertTrue((p.status.value as EdgeLinkStatus.Reconnecting).reason.contains("MessageTooLarge"))
            } finally {
                p.stop()
                scope.cancel()
            }
        }
    }

    @Test
    fun `a public address is refused and zero bytes are written`() {
        val operations = CopyOnWriteArrayList<String>()
        val publicIp = InetAddress.getByAddress("ground.example", byteArrayOf(8, 8, 8, 8))
        val edgePort = EdgeSocketPort(
            resolver = HostResolver { listOf(publicIp) },
            guard = IoGuard { operations += it },
        )
        val scope = scope()
        val clock = FakeClock()
        val p = publisher(9000, scope, clock = clock, edgePort = edgePort, host = "ground.example")
        try {
            p.start()
            runBlocking { eventually("refused") { p.status.value is EdgeLinkStatus.AddressRefused } }
            val reason = (p.status.value as EdgeLinkStatus.AddressRefused).reason
            assertTrue(reason, reason.contains("private network"))
            assertTrue("no connect or write happened: $operations", operations.none { it == "connect" || it == "write" })
            repeat(3) { clock.advance(60_000) }
            Thread.sleep(100)
            assertTrue(p.status.value is EdgeLinkStatus.AddressRefused)
        } finally {
            p.stop()
            scope.cancel()
        }
    }

    @Test
    fun `a name that resolves to a mix of private and public addresses is refused`() {
        val operations = CopyOnWriteArrayList<String>()
        val edgePort = EdgeSocketPort(
            resolver = HostResolver {
                listOf(InetAddress.getByAddress(byteArrayOf(192.toByte(), 168.toByte(), 1, 20)), InetAddress.getByAddress(byteArrayOf(1, 1, 1, 1)))
            },
            guard = IoGuard { operations += it },
        )
        val scope = scope()
        val p = publisher(9000, scope, edgePort = edgePort, host = "ground.local")
        try {
            p.start()
            runBlocking { eventually("refused") { p.status.value is EdgeLinkStatus.AddressRefused } }
            assertTrue(operations.none { it == "connect" || it == "write" })
        } finally {
            p.stop()
            scope.cancel()
        }
    }
}
