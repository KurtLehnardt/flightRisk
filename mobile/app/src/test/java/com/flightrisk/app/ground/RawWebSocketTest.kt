package com.flightrisk.app.ground

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.net.InetAddress
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class RawWebSocketTest {

    private val loopback: InetAddress = InetAddress.getLoopbackAddress()
    private val oneMiB = 1024L * 1024

    private fun connect(server: LoopbackServer, maxBytes: Long = oneMiB, timeoutMs: Int = 5_000): RawWebSocket =
        RawWebSocket.connect(
            loopback, server.port, "127.0.0.1:${server.port}", GroundEndpoint.SOCKET_IO_PATH,
            maxMessageBytes = maxBytes, timeoutMs = timeoutMs, readTimeoutMs = 10_000,
        )

    @Test
    fun `the upgrade request carries no Origin, no cookie and no credential`() {
        val requests = CopyOnWriteArrayList<List<String>>()
        LoopbackServer { s ->
            val head = readRequestHead(s.getInputStream())
            requests += head
            acceptUpgrade(head, s.getOutputStream())
            writeServerText(s.getOutputStream(), "2")
            readClientFrame(s.getInputStream())
        }.use { server ->
            val ws = connect(server)
            assertEquals("2", ws.receiveText())
            ws.close()
            val head = requests.single()
            assertEquals("GET /socket.io/?EIO=4&transport=websocket HTTP/1.1", head.first())
            val names = head.drop(1).map { it.substringBefore(':').lowercase() }.toSet()
            assertEquals(
                setOf("host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version", "user-agent"),
                names,
            )
            assertEquals("127.0.0.1:${server.port}", headerValue(head, "Host"))
            assertFalse(names.contains("origin"))
            assertFalse(names.contains("authorization"))
            assertFalse(names.contains("cookie"))
            assertFalse(head.joinToString().contains(TEST_KEY))
        }
    }

    @Test
    fun `text frames round trip, pings are answered and fragments are joined`() {
        val fromClient = CopyOnWriteArrayList<Pair<Int, String>>()
        val done = CountDownLatch(1)
        LoopbackServer { s ->
            val input = s.getInputStream()
            val out = s.getOutputStream()
            acceptUpgrade(readRequestHead(input), out)
            writeServerFrame(out, 0x9, "hi".toByteArray())
            writeServerFrame(out, 0x1, "42[\"sta".toByteArray(), fin = false)
            writeServerFrame(out, 0x0, "tus\",{}]".toByteArray(), fin = true)
            repeat(2) {
                val (op, payload) = readClientFrame(input)
                fromClient += op to String(payload)
            }
            done.countDown()
        }.use { server ->
            val ws = connect(server)
            assertEquals("42[\"status\",{}]", ws.receiveText())
            ws.sendText("3")
            assertTrue(done.await(5, TimeUnit.SECONDS))
            assertEquals(listOf(0xA to "hi", 0x1 to "3"), fromClient.toList())
            ws.close()
        }
    }

    @Test
    fun `a 40 MB message is refused from its header, before the payload is read`() {
        val written = AtomicLong(-1)
        val buffers = AtomicLong(0)
        val finished = CountDownLatch(1)
        val declared = 40L * 1024 * 1024
        LoopbackServer { s ->
            buffers.set(s.sendBufferSize.toLong() + s.receiveBufferSize.toLong())
            acceptUpgrade(readRequestHead(s.getInputStream()), s.getOutputStream())
            written.set(streamHugeMessage(s.getOutputStream(), declared))
            finished.countDown()
        }.use { server ->
            val ws = connect(server)
            try {
                ws.receiveText()
                fail("expected MessageTooLargeException")
            } catch (e: MessageTooLargeException) {
                assertEquals(oneMiB, e.limitBytes)
            } finally {
                ws.close()
            }
            assertTrue("server stopped writing", finished.await(20, TimeUnit.SECONDS))
            assertTrue("the client stopped the transfer early (${written.get()} bytes)", written.get() < declared)
            // Nothing but the kernel's socket buffers (both ends, which may grow up to
            // twice their reported size) holds the bytes the client never read.
            val bound = 2 * (buffers.get() + java.net.Socket().use { it.receiveBufferSize.toLong() }) + 64 * 1024
            assertTrue("wrote ${written.get()} bytes, bound $bound", written.get() <= bound)
        }
    }

    @Test
    fun `a continuation frame declaring 2^63-1 bytes is refused, not wrapped around`() {
        LoopbackServer { s ->
            val o = s.getOutputStream()
            acceptUpgrade(readRequestHead(s.getInputStream()), o)
            writeServerFrame(o, 0x1, "a".toByteArray(), fin = false)
            o.write(0x80)
            o.write(127)
            for (i in 0 until 8) o.write(if (i == 0) 0x7f else 0xff)
            o.flush()
            writeServerText(o, "b")
            Thread.sleep(500)
        }.use { server ->
            val ws = connect(server)
            try {
                val got = ws.receiveText()
                fail("expected MessageTooLargeException, got \"$got\"")
            } catch (_: MessageTooLargeException) {
            } finally {
                ws.abort()
            }
        }
    }

    @Test
    fun `a server that trickles its upgrade answer cannot stretch the connect past its deadline`() {
        val stop = java.util.concurrent.atomic.AtomicBoolean(false)
        LoopbackServer { s ->
            readRequestHead(s.getInputStream())
            val o = s.getOutputStream()
            for (c in "HTTP/1.1 101 Switching Protocols\r\nX-Pad: " + "a".repeat(5_000)) {
                if (stop.get()) break
                o.write(c.code)
                o.flush()
                Thread.sleep(100)
            }
        }.use { server ->
            val start = System.nanoTime()
            try {
                connect(server, timeoutMs = 1_000)
                fail("expected a timeout")
            } catch (_: java.net.SocketTimeoutException) {
            } finally {
                stop.set(true)
            }
            val ms = (System.nanoTime() - start) / 1_000_000
            assertTrue("took $ms ms", ms < 2_500)
        }
    }

    @Test
    fun `shutdown during a trickled handshake returns at once`() = runBlocking {
        val stop = java.util.concurrent.atomic.AtomicBoolean(false)
        val server = LoopbackServer { s ->
            readRequestHead(s.getInputStream())
            val o = s.getOutputStream()
            for (c in "HTTP/1.1 101 Switching Protocols\r\nX-Pad: " + "a".repeat(5_000)) {
                if (stop.get()) break
                o.write(c.code)
                o.flush()
                Thread.sleep(100)
            }
        }
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val client = GroundConsoleClient(
            GroundEndpoint.of("127.0.0.1", server.port)!!, DashboardApiKey.of(TEST_KEY), scope,
            port = WebSocketSocketIoPort(connectTimeoutMs = 60_000),
        )
        try {
            client.start()
            eventually { server.accepted.get() == 1 }
            kotlinx.coroutines.delay(300)
            val start = System.nanoTime()
            val done = kotlinx.coroutines.withTimeoutOrNull(2_000) { client.shutdown() }
            assertNotNull("shutdown did not return", done)
            assertTrue((System.nanoTime() - start) / 1_000_000 < 2_000)
            assertEquals(ConnectionStatus.ShutDown, client.connection.value)
        } finally {
            stop.set(true)
            scope.cancel()
            server.close()
        }
    }

    @Test
    fun `hostile control frames, bad UTF-8, reserved bits and unknown opcodes are protocol errors`() {
        fun outcome(write: (java.io.OutputStream) -> Unit): Exception? {
            var result: Exception? = null
            LoopbackServer { s ->
                val o = s.getOutputStream()
                acceptUpgrade(readRequestHead(s.getInputStream()), o)
                write(o)
                o.flush()
                Thread.sleep(300)
            }.use { server ->
                val ws = connect(server)
                try {
                    ws.receiveText()
                } catch (e: Exception) {
                    result = e
                } finally {
                    ws.abort()
                }
            }
            return result
        }
        val cases = mapOf<String, (java.io.OutputStream) -> Unit>(
            "ping126" to { o -> writeFrameHeader(o, 0x9, 126); o.write(ByteArray(126)) },
            "fragmentedPing" to { o -> writeServerFrame(o, 0x9, "x".toByteArray(), fin = false) },
            "badUtf8" to { o -> writeServerFrame(o, 0x1, byteArrayOf(0xC3.toByte(), 0x28)) },
            "rsv1" to { o -> o.write(0xC1); o.write(1); o.write('a'.code) },
            "unknownOp" to { o -> writeServerFrame(o, 0x3, "a".toByteArray()) },
            "oneByteClose" to { o -> writeServerFrame(o, 0x8, byteArrayOf(0x03)) },
        )
        for ((name, write) in cases) {
            val e = outcome(write)
            assertTrue("$name -> $e", e is ProtocolViolationException)
        }
    }

    @Test
    fun `a close reason is redacted with the connection's secrets`() {
        LoopbackServer { s ->
            val o = s.getOutputStream()
            acceptUpgrade(readRequestHead(s.getInputStream()), o)
            writeServerFrame(o, 0x8, byteArrayOf(0x0f, 0xa1.toByte()) + "bad token $TEST_KEY".toByteArray())
            Thread.sleep(300)
        }.use { server ->
            val ws = RawWebSocket.connect(
                loopback, server.port, "127.0.0.1:${server.port}", GroundEndpoint.SOCKET_IO_PATH,
                maxMessageBytes = oneMiB, timeoutMs = 5_000, readTimeoutMs = 10_000, secrets = listOf(TEST_KEY),
            )
            try {
                ws.receiveText()
                fail("expected a close")
            } catch (e: WebSocketClosedException) {
                assertEquals(4001, e.code)
                assertFalse(e.message!!.contains(TEST_KEY))
                assertTrue(e.reason.contains("bad token"))
            } finally {
                ws.abort()
            }
        }
    }

    @Test
    fun `an upgrade answer that picks a subprotocol is refused`() {
        LoopbackServer { s ->
            val head = readRequestHead(s.getInputStream())
            val key = headerValue(head, "Sec-WebSocket-Key")!!
            val accept = java.util.Base64.getEncoder().encodeToString(
                java.security.MessageDigest.getInstance("SHA-1").digest((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").toByteArray()),
            )
            s.getOutputStream().write(
                ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
                    "Sec-WebSocket-Accept: $accept\r\nSec-WebSocket-Protocol: chat\r\n\r\n").toByteArray(),
            )
            Thread.sleep(300)
        }.use { server ->
            try {
                connect(server)
                fail("expected a protocol error")
            } catch (_: ProtocolViolationException) {
            }
        }
    }

    @Test
    fun `every frame gets a fresh mask and concurrent sends never interleave`() {
        val masks = CopyOnWriteArrayList<String>()
        val bad = CopyOnWriteArrayList<String>()
        val n = 200
        val done = CountDownLatch(1)
        LoopbackServer { s ->
            val i = s.getInputStream()
            acceptUpgrade(readRequestHead(i), s.getOutputStream())
            val din = java.io.DataInputStream(i)
            repeat(n) {
                val b0 = din.readUnsignedByte()
                val b1 = din.readUnsignedByte()
                val len = when (val l = b1 and 0x7f) {
                    126 -> din.readUnsignedShort().toLong()
                    127 -> din.readLong()
                    else -> l.toLong()
                }
                val m = ByteArray(4).also { din.readFully(it) }
                masks += m.joinToString(",")
                val p = ByteArray(len.toInt()).also { din.readFully(it) }
                for (k in p.indices) p[k] = (p[k].toInt() xor m[k and 3].toInt()).toByte()
                if (b0 != 0x81 || p.toSet().size != 1) bad += "b0=$b0"
            }
            done.countDown()
        }.use { server ->
            val ws = connect(server)
            val threads = listOf('a', 'b').map { ch -> Thread { repeat(n / 2) { ws.sendText(ch.toString().repeat(70_000)) } } }
            threads.forEach { it.start() }
            threads.forEach { it.join(20_000) }
            assertTrue(done.await(20, TimeUnit.SECONDS))
            assertEquals(emptyList<String>(), bad.toList())
            assertTrue(masks.toSet().size > n - 5)
            ws.close()
        }
    }

    @Test
    fun `fragments that add up past the limit are refused at the frame that crosses it`() {
        LoopbackServer { s ->
            val out = s.getOutputStream()
            acceptUpgrade(readRequestHead(s.getInputStream()), out)
            writeServerFrame(out, 0x1, ByteArray(600 * 1024) { 'a'.code.toByte() }, fin = false)
            // The continuation frame declares another 600 KiB: 1.2 MiB in total, over the 1 MiB limit.
            writeFrameHeader(out, 0x0, 600L * 1024)
            try {
                out.write(ByteArray(600 * 1024))
                out.flush()
            } catch (_: java.io.IOException) {
            }
        }.use { server ->
            val ws = connect(server)
            try {
                ws.receiveText()
                fail("expected MessageTooLargeException")
            } catch (_: MessageTooLargeException) {
            } finally {
                ws.close()
            }
        }
    }

    @Test
    fun `a wrong accept value or a refused upgrade fails the connect`() {
        LoopbackServer { s ->
            acceptUpgrade(readRequestHead(s.getInputStream()), s.getOutputStream(), acceptOverride = "bogus")
        }.use { server ->
            try {
                connect(server)
                fail("expected a protocol error")
            } catch (_: ProtocolViolationException) {
            }
        }
        LoopbackServer { s ->
            readRequestHead(s.getInputStream())
            s.getOutputStream().write("HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n".toByteArray())
        }.use { server ->
            try {
                connect(server)
                fail("expected a rejected handshake")
            } catch (e: HandshakeRejectedException) {
                assertEquals(400, e.status)
            }
        }
    }

    @Test
    fun `a masked server frame is a protocol error`() {
        LoopbackServer { s ->
            val out = s.getOutputStream()
            acceptUpgrade(readRequestHead(s.getInputStream()), out)
            out.write(byteArrayOf(0x81.toByte(), 0x81.toByte(), 1, 2, 3, 4, 5))
            out.flush()
            Thread.sleep(500)
        }.use { server ->
            val ws = connect(server)
            try {
                ws.receiveText()
                fail("expected a protocol error")
            } catch (_: ProtocolViolationException) {
            } finally {
                ws.close()
            }
        }
    }

    @Test
    fun `the port refuses a public address before connecting`() = runBlocking {
        val port = WebSocketSocketIoPort(resolver = HostResolver { listOf(InetAddress.getByName("8.8.8.8")) })
        try {
            port.open(GroundEndpoint.of("ground.example")!!, emptyList())
            fail("expected a refusal")
        } catch (_: AddressNotAllowedException) {
        }
    }

    @Test
    fun `the client over a real socket closes on a 40 MB message with a typed reason`() = runBlocking {
        val connectFrames = CopyOnWriteArrayList<String>()
        val written = AtomicLong(-1)
        val declared = 40L * 1024 * 1024
        val server = LoopbackServer { s ->
            val input = s.getInputStream()
            val out = s.getOutputStream()
            acceptUpgrade(readRequestHead(input), out)
            writeServerText(out, ContractFixtures.frame("engineio_open"))
            connectFrames += readClientText(input)
            writeServerText(out, ContractFixtures.eventFrame("status.json", "fleet_two_drones"))
            writeServerText(out, ContractFixtures.frame("socketio_connect_ack"))
            written.set(streamHugeMessage(out, declared))
        }
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val log = CapturingLog()
        val client = GroundConsoleClient(
            endpoint = GroundEndpoint.of("127.0.0.1", server.port)!!,
            apiKey = DashboardApiKey.of(TEST_KEY),
            scope = scope,
            port = WebSocketSocketIoPort(maxMessageBytes = oneMiB),
            config = GroundClientConfig(backoff = Backoff(initialMs = 600_000, maxMs = 600_000)),
            logSink = log,
        )
        try {
            client.start()
            eventually("reconnecting", timeoutMs = 20_000) { client.connection.value is ConnectionStatus.Reconnecting }
            val status = client.connection.value as ConnectionStatus.Reconnecting
            assertEquals(DropReason.MessageTooLarge(oneMiB), status.reason)
            assertEquals(listOf("40{\"api_key\":\"$TEST_KEY\"}"), connectFrames.toList())
            assertTrue(client.snapshot.value.status != null)
            eventually("server finished", timeoutMs = 20_000) { written.get() >= 0 }
            assertTrue(written.get() < declared)
            log.lines.forEach { assertFalse(it, it.contains(TEST_KEY)) }
        } finally {
            client.shutdown()
            scope.cancel()
            server.close()
        }
    }
}
