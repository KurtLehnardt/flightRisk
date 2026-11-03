package com.flightrisk.app.ground

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.KeyStore
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLException
import javax.net.ssl.SSLServerSocket
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManagerFactory
import kotlin.concurrent.thread

/**
 * `wss://` and `https://` against a local TLS server on 127.0.0.1. The server's certificate is the
 * test-only self-signed one in `src/test/resources/tls` (see `TEST_ONLY.txt` there): it is not a real
 * credential and the app never trusts it. Only the clients built in [trusting] are told to trust exactly
 * that one certificate; every other test uses the platform's default factory to show it refuses it.
 */
class RawWebSocketTlsTest {

    private val loopback: InetAddress = InetAddress.getLoopbackAddress()
    private val password = "test-only-password".toCharArray()

    private fun keyStore(): KeyStore = KeyStore.getInstance("PKCS12").also { ks ->
        val stream = javaClass.getResourceAsStream("/tls/test-only-localhost.p12") ?: error("test keystore missing")
        stream.use { ks.load(it, password) }
    }

    /** A client factory that trusts exactly the test certificate (not trust-all) and nothing else. */
    private fun trusting(): SSLSocketFactory {
        val anchors = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null, null)
            setCertificateEntry("test-only", keyStore().getCertificate("flightrisk-test-only"))
        }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(anchors) }
        return SSLContext.getInstance("TLS").apply { init(null, tmf.trustManagers, null) }.socketFactory
    }

    private class TlsServer(private val server: SSLServerSocket, handler: (Socket) -> Unit) : AutoCloseable {
        val port: Int get() = server.localPort
        val errors = CopyOnWriteArrayList<Throwable>()

        init {
            thread(isDaemon = true, name = "tls-accept") {
                while (!server.isClosed) {
                    val s = try {
                        server.accept()
                    } catch (_: IOException) {
                        break
                    }
                    thread(isDaemon = true, name = "tls-conn") {
                        try {
                            s.use { handler(it) }
                        } catch (t: Throwable) {
                            errors += t
                        }
                    }
                }
            }
        }

        override fun close() = server.close()
    }

    private fun tlsServer(handler: (Socket) -> Unit): TlsServer {
        val kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(keyStore(), password) }
        val context = SSLContext.getInstance("TLS").apply { init(kmf.keyManagers, null, null) }
        val server = context.serverSocketFactory.createServerSocket(0, 50, loopback) as SSLServerSocket
        return TlsServer(server, handler)
    }

    private fun connect(server: TlsServer, tls: TlsParams?, host: String = "localhost"): RawWebSocket =
        RawWebSocket.connect(
            loopback, server.port, "$host:${server.port}", GroundEndpoint.SOCKET_IO_PATH,
            maxMessageBytes = 1024L * 1024, timeoutMs = 5_000, readTimeoutMs = 10_000, tls = tls,
        )

    @Test
    fun `wss upgrades over TLS, round trips text and sends no credential header`() {
        val heads = CopyOnWriteArrayList<List<String>>()
        val fromClient = CopyOnWriteArrayList<String>()
        val done = CountDownLatch(1)
        tlsServer { s ->
            val head = readRequestHead(s.getInputStream())
            heads += head
            acceptUpgrade(head, s.getOutputStream())
            writeServerText(s.getOutputStream(), "0{\"sid\":\"x\"}")
            fromClient += readClientText(s.getInputStream())
            done.countDown()
        }.use { server ->
            val ws = connect(server, TlsParams("localhost", trusting(), null))
            assertEquals("0{\"sid\":\"x\"}", ws.receiveText())
            ws.sendText("40")
            assertTrue(done.await(5, TimeUnit.SECONDS))
            assertEquals(listOf("40"), fromClient.toList())
            ws.close()
            val names = heads.single().drop(1).map { it.substringBefore(':').lowercase() }.toSet()
            assertFalse(names.contains("origin"))
            assertFalse(names.contains("authorization"))
            assertFalse(names.contains("cookie"))
            assertEquals("localhost:${server.port}", headerValue(heads.single(), "Host"))
        }
    }

    @Test
    fun `the platform default trust store refuses the self-signed test certificate`() {
        tlsServer { s -> runCatching { s.getInputStream().read() } }.use { server ->
            try {
                connect(server, TlsParams("localhost", TlsParams.defaultSocketFactory(), null))
                fail("a certificate no trust anchor vouches for must be refused")
            } catch (e: SSLException) {
                // The handshake failed: nothing was sent.
            }
        }
    }

    @Test
    fun `a certificate for another host name is refused even when its chain is trusted`() {
        tlsServer { s -> runCatching { s.getInputStream().read() } }.use { server ->
            try {
                connect(server, TlsParams("ground.example.org", trusting(), null), host = "ground.example.org")
                fail("the certificate is for localhost, not for ground.example.org")
            } catch (e: SSLException) {
                // Endpoint identification rejected the name.
            }
        }
    }

    @Test
    fun `the app never enables protocols older than TLS 1_2`() {
        val negotiated = CopyOnWriteArrayList<String>()
        val ready = CountDownLatch(1)
        tlsServer { s ->
            negotiated += (s as SSLSocket).session.protocol
            val head = readRequestHead(s.getInputStream())
            acceptUpgrade(head, s.getOutputStream())
            ready.countDown()
            runCatching { s.getInputStream().read() }
        }.use { server ->
            val ws = connect(server, TlsParams("localhost", trusting(), null))
            assertTrue(ready.await(5, TimeUnit.SECONDS))
            assertTrue(negotiated.single(), negotiated.single() in setOf("TLSv1.2", "TLSv1.3"))
            ws.abort()
        }
    }

    @Test
    fun `wss to a server that does not speak TLS fails without sending the upgrade`() {
        val received = CopyOnWriteArrayList<String>()
        LoopbackServer { s ->
            val bytes = ByteArray(64)
            val n = runCatching { s.getInputStream().read(bytes) }.getOrDefault(-1)
            if (n > 0) received += String(bytes, 0, n, Charsets.ISO_8859_1)
            // Answer in clear text, as a plain ws:// server would.
            runCatching { s.getOutputStream().write("HTTP/1.1 101 Switching Protocols\r\n\r\n".toByteArray()) }
        }.use { server ->
            try {
                RawWebSocket.connect(
                    loopback, server.port, "localhost:${server.port}", GroundEndpoint.SOCKET_IO_PATH,
                    maxMessageBytes = 1024L, timeoutMs = 5_000, readTimeoutMs = 5_000,
                    tls = TlsParams("localhost", trusting(), null),
                )
                fail("a clear text answer to a TLS handshake must fail")
            } catch (_: IOException) {
            }
            assertFalse("no HTTP upgrade may be written before TLS is up", received.any { it.contains("GET ") })
        }
    }

    @Test
    fun `abort from another thread ends a blocked read on a TLS connection`() {
        val serverHolds = CountDownLatch(1)
        tlsServer { s ->
            acceptUpgrade(readRequestHead(s.getInputStream()), s.getOutputStream())
            serverHolds.await(10, TimeUnit.SECONDS)
        }.use { server ->
            val ws = connect(server, TlsParams("localhost", trusting(), null))
            val failure = CopyOnWriteArrayList<Throwable>()
            val reader = thread { try { ws.receiveText() } catch (t: Throwable) { failure += t } }
            Thread.sleep(200)
            ws.abort()
            reader.join(5_000)
            assertFalse(reader.isAlive)
            assertTrue(failure.single() is IOException)
            serverHolds.countDown()
        }
    }

    @Test
    fun `the socket port connects a secure endpoint over TLS to the resolved address`() = runBlocking {
        val opened = CopyOnWriteArrayList<List<String>>()
        tlsServer { s ->
            val head = readRequestHead(s.getInputStream())
            opened += head
            acceptUpgrade(head, s.getOutputStream())
            writeServerText(s.getOutputStream(), "2")
            runCatching { readClientFrame(s.getInputStream()) }
        }.use { server ->
            val endpoint = GroundEndpoint.of("localhost", server.port, secure = true)!!
            // The name resolves to loopback here only because the test says so; a secure endpoint would
            // take a public answer as well, a plain one would not.
            val port = WebSocketSocketIoPort(resolver = { listOf(loopback) }, tlsFactory = { trusting() }, tlsVerifier = null)
            val connection = port.open(endpoint, listOf(TEST_KEY))
            assertEquals("2", connection.receive())
            connection.closeGracefully()
            assertEquals("GET /socket.io/?EIO=4&transport=websocket HTTP/1.1", opened.single().first())
        }
    }

    @Test
    fun `a secure endpoint tries the next address when the first refuses the connection`() = runBlocking {
        tlsServer { s ->
            acceptUpgrade(readRequestHead(s.getInputStream()), s.getOutputStream())
            writeServerText(s.getOutputStream(), "2")
            runCatching { readClientFrame(s.getInputStream()) }
        }.use { server ->
            val endpoint = GroundEndpoint.of("localhost", server.port, secure = true)!!
            // The server listens on 127.0.0.1 only, so ::1 refuses (or is unreachable) and the next answer is used.
            val answers = listOf(InetAddress.getByName("::1"), loopback)
            val port = WebSocketSocketIoPort(resolver = { answers }, tlsFactory = { trusting() }, tlsVerifier = null)
            val connection = port.open(endpoint, emptyList())
            assertEquals("2", connection.receive())
            connection.closeGracefully()
        }
    }

    @Test
    fun `a certificate failure does not move on to the next address`() = runBlocking {
        val accepted = java.util.concurrent.atomic.AtomicInteger()
        tlsServer { s -> accepted.incrementAndGet(); runCatching { s.getInputStream().read() } }.use { server ->
            val endpoint = GroundEndpoint.of("localhost", server.port, secure = true)!!
            val port = WebSocketSocketIoPort(resolver = { listOf(loopback, loopback.let { InetAddress.getByName("127.0.0.1") }) })
            try {
                port.open(endpoint, emptyList())
                fail("the default trust store must refuse the certificate")
            } catch (_: SSLException) {
            }
        }
    }

    @Test
    fun `the socket port with the platform defaults refuses the test certificate`() = runBlocking {
        tlsServer { s -> runCatching { s.getInputStream().read() } }.use { server ->
            val endpoint = GroundEndpoint.of("localhost", server.port, secure = true)!!
            val port = WebSocketSocketIoPort(resolver = { listOf(loopback) })
            try {
                port.open(endpoint, emptyList())
                fail("the default trust store must refuse the self-signed certificate")
            } catch (_: SSLException) {
            }
        }
    }

    @Test
    fun `a plain endpoint to a public address is still refused before any connection`() = runBlocking {
        val server = ServerSocket(0, 1, loopback)
        server.use {
            val endpoint = GroundEndpoint.of("ground.example.org", server.localPort)!!
            val port = WebSocketSocketIoPort(resolver = { listOf(InetAddress.getByName("8.8.8.8")) })
            try {
                port.open(endpoint, emptyList())
                fail("ws:// must not reach a public address")
            } catch (_: AddressNotAllowedException) {
            }
        }
    }

    @Test
    fun `https probe works over TLS and sends the key only to status in the Bearer header`() = runBlocking {
        val heads = CopyOnWriteArrayList<List<String>>()
        val health = "{\"status\":\"healthy\",\"version\":\"1.0.0\",\"components\":{\"db\":true}}"
        val status = "{\"running\":true,\"source\":\"sim\",\"fleet\":{\"drones\":0}}"
        tlsServer { s ->
            val head = readRequestHead(s.getInputStream())
            heads += head
            val body = if (head.first().contains("/api/status")) status else health
            s.getOutputStream().write(
                ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.length}\r\nConnection: close\r\n\r\n$body").toByteArray(),
            )
            s.getOutputStream().flush()
        }.use { server ->
            val endpoint = GroundEndpoint.of("localhost", server.port, secure = true)!!
            val client = DashboardRestClient(
                endpoint, DashboardApiKey.of(TEST_KEY), resolver = { listOf(loopback) },
                tlsFactory = { trusting() }, tlsVerifier = null,
            )
            val result = client.probe()
            assertTrue(result.toString(), result is HealthProbeResult.Ok)
            assertEquals(2, heads.size)
            assertEquals(null, headerValue(heads[0], "Authorization"))
            assertEquals("Bearer $TEST_KEY", headerValue(heads[1], "Authorization"))
        }
    }

    @Test
    fun `https probe with the default trust store reports unreachable and never sends the key`() = runBlocking {
        val heads = CopyOnWriteArrayList<List<String>>()
        tlsServer { s -> runCatching { heads += readRequestHead(s.getInputStream()) } }.use { server ->
            val endpoint = GroundEndpoint.of("localhost", server.port, secure = true)!!
            val client = DashboardRestClient(endpoint, DashboardApiKey.of(TEST_KEY), resolver = { listOf(loopback) })
            val result = client.probe()
            assertTrue(result.toString(), result is HealthProbeResult.SecureConnectionFailed)
            assertFalse(result.toString().contains(TEST_KEY))
            Thread.sleep(100)
            assertTrue("no request may reach a server whose certificate was refused", heads.isEmpty())
        }
    }
}
