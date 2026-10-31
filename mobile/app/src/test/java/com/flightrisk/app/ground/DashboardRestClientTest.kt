package com.flightrisk.app.ground

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.CopyOnWriteArrayList

class DashboardRestClientTest {

    private val healthJson =
        "{\"status\":\"healthy\",\"version\":\"1.0.0\",\"components\":{\"db\":true},\"reasoning_provider\":\"off\",\"active_signals\":[]}"
    private val statusJson =
        "{\"running\":true,\"source\":\"sim\",\"fps\":0,\"appearance_only\":false,\"tiled\":false," +
            "\"fleet\":{\"drones\":0,\"net\":\"station\",\"synced\":false}}"

    private fun respond(out: OutputStream, status: String, headers: List<String>, body: String = "") {
        val bytes = body.toByteArray()
        val head = buildString {
            append("HTTP/1.1 ").append(status).append("\r\n")
            headers.forEach { append(it).append("\r\n") }
            if (headers.none { it.startsWith("Transfer-Encoding", ignoreCase = true) }) {
                append("Content-Length: ").append(bytes.size).append("\r\n")
            }
            append("Connection: close\r\n\r\n")
        }
        out.write(head.toByteArray())
        out.write(bytes)
        out.flush()
    }

    private fun json(out: OutputStream, body: String) =
        respond(out, "200 OK", listOf("Content-Type: application/json"), body)

    /** A dashboard stand-in: [routes] maps a path to a responder. Every request head is recorded. */
    private class FakeDashboard(routes: Map<String, (OutputStream) -> Unit>) : AutoCloseable {
        val requests = CopyOnWriteArrayList<List<String>>()
        val server = LoopbackServer { s ->
            val head = readRequestHead(s.getInputStream())
            requests += head
            val path = head.first().split(' ')[1]
            (routes[path] ?: { out -> out.write("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n".toByteArray()) })(s.getOutputStream())
        }
        val endpoint: GroundEndpoint get() = GroundEndpoint.of("127.0.0.1", server.port)!!
        override fun close() = server.close()
    }

    private fun probe(d: FakeDashboard, key: String? = TEST_KEY, maxBody: Int = 256 * 1024): HealthProbeResult =
        runBlocking { DashboardRestClient(d.endpoint, DashboardApiKey.of(key), maxBodyBytes = maxBody).probe() }

    @Test
    fun `a healthy dashboard that accepts the key is ok, and the key goes only to status in the Bearer header`() {
        FakeDashboard(mapOf("/api/health" to { o -> json(o, healthJson) }, "/api/status" to { o -> json(o, statusJson) })).use { d ->
            val r = probe(d)
            assertEquals(HealthProbeResult.Ok("1.0.0", StatusSummary(true, "sim", 0)), r)
            assertEquals(2, d.requests.size)
            val (health, status) = d.requests
            assertEquals("GET /api/health HTTP/1.1", health.first())
            assertEquals(null, headerValue(health, "Authorization"))
            assertEquals("Bearer $TEST_KEY", headerValue(status, "Authorization"))
            for (req in d.requests) {
                assertEquals(null, headerValue(req, "Origin"))
                assertFalse(req.first().contains(TEST_KEY))
            }
            assertFalse(r.toString().contains(TEST_KEY))
        }
    }

    @Test
    fun `a redirect is never followed`() {
        val elsewhere = ServerSocket(0, 50, InetAddress.getLoopbackAddress())
        elsewhere.use {
            FakeDashboard(
                mapOf(
                    "/api/health" to { o ->
                        respond(o, "302 Found", listOf("Location: http://127.0.0.1:${elsewhere.localPort}/api/health"))
                    },
                ),
            ).use { d ->
                assertEquals(HealthProbeResult.Redirected("/api/health", 302), probe(d))
                assertEquals(1, d.requests.size)
                // Nothing connected to the redirect target: accept() would find no pending connection.
                elsewhere.soTimeout = 200
                try {
                    elsewhere.accept().close()
                    throw AssertionError("the client followed the redirect")
                } catch (_: java.net.SocketTimeoutException) {
                }
            }
        }
        FakeDashboard(
            mapOf(
                "/api/health" to { o -> json(o, healthJson) },
                "/api/status" to { o -> respond(o, "301 Moved Permanently", listOf("Location: http://127.0.0.1:1/x")) },
            ),
        ).use { d ->
            assertEquals(HealthProbeResult.Redirected("/api/status", 301), probe(d))
            assertEquals(2, d.requests.size)
        }
    }

    @Test
    fun `a 401 from status means a wrong key`() {
        FakeDashboard(
            mapOf(
                "/api/health" to { o -> json(o, healthJson) },
                "/api/status" to { o -> respond(o, "401 UNAUTHORIZED", listOf("Content-Type: application/json"), "{\"error\":\"unauthorized\"}") },
            ),
        ).use { d ->
            assertEquals(HealthProbeResult.WrongKey("1.0.0"), probe(d))
        }
    }

    @Test
    fun `health must be the dashboard's JSON`() {
        FakeDashboard(mapOf("/api/health" to { o -> respond(o, "200 OK", listOf("Content-Type: text/html"), "<html>portal</html>") })).use { d ->
            assertEquals(HealthProbeResult.UnexpectedBody("/api/health", "not a JSON object"), probe(d))
        }
        FakeDashboard(mapOf("/api/health" to { o -> json(o, "{\"status\":\"healthy\"}") })).use { d ->
            assertEquals(HealthProbeResult.UnexpectedBody("/api/health", "not the dashboard's health object"), probe(d))
        }
        FakeDashboard(mapOf("/api/health" to { o -> json(o, "[1,2]") })).use { d ->
            assertEquals(HealthProbeResult.UnexpectedBody("/api/health", "not a JSON object"), probe(d))
        }
        FakeDashboard(mapOf("/api/health" to { o -> json(o, healthJson) }, "/api/status" to { o -> json(o, "{\"ok\":true}") })).use { d ->
            assertEquals(HealthProbeResult.UnexpectedBody("/api/status", "not the dashboard's status object"), probe(d))
        }
        FakeDashboard(mapOf("/api/health" to { o -> respond(o, "500 INTERNAL SERVER ERROR", emptyList()) })).use { d ->
            assertEquals(HealthProbeResult.UnexpectedStatus("/api/health", 500), probe(d))
        }
    }

    @Test
    fun `a body over the limit is refused and chunked bodies are read`() {
        FakeDashboard(mapOf("/api/health" to { o -> json(o, healthJson) })).use { d ->
            val r = probe(d, maxBody = 32)
            assertTrue(r.toString(), r is HealthProbeResult.UnexpectedBody)
        }
        val chunked = { o: OutputStream ->
            val half = healthJson.length / 2
            val a = healthJson.substring(0, half)
            val b = healthJson.substring(half)
            respond(
                o, "200 OK", listOf("Content-Type: application/json", "Transfer-Encoding: chunked"),
                Integer.toHexString(a.length) + "\r\n" + a + "\r\n" + Integer.toHexString(b.length) + "\r\n" + b + "\r\n0\r\n\r\n",
            )
        }
        FakeDashboard(mapOf("/api/health" to chunked, "/api/status" to { o -> json(o, statusJson) })).use { d ->
            assertTrue(probe(d) is HealthProbeResult.Ok)
        }
    }

    @Test
    fun `a public address is refused before any connection, and an unreachable ground is reported without the key`() {
        val public = runBlocking {
            DashboardRestClient(
                GroundEndpoint.of("ground.example")!!, DashboardApiKey.of(TEST_KEY),
                resolver = HostResolver { listOf(InetAddress.getByName("8.8.8.8")) },
            ).probe()
        }
        assertTrue(public.toString(), public is HealthProbeResult.AddressNotAllowed)
        val closedPort = ServerSocket(0, 1, InetAddress.getLoopbackAddress()).use { it.localPort }
        val r = runBlocking { DashboardRestClient(GroundEndpoint.of("127.0.0.1", closedPort)!!, DashboardApiKey.of(TEST_KEY)).probe() }
        assertTrue(r.toString(), r is HealthProbeResult.Unreachable)
        assertFalse(r.toString().contains(TEST_KEY))
    }
}
