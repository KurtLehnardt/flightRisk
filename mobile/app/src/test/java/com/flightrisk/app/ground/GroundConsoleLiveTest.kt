package com.flightrisk.app.ground

import com.flightrisk.contract.FleetStatus
import com.flightrisk.contract.LandAllAck
import com.flightrisk.contract.ServerMessage
import com.flightrisk.contract.Status
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.net.InetAddress
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Talks to a real dashboard. Skipped unless `FLIGHTRISK_LIVE_TEST_URL` is set,
 * for example to `http://127.0.0.1:18555` for a throwaway dashboard started
 * with `FLIGHTRISK_API_KEY=test-key` on a loopback port. The key comes from
 * `FLIGHTRISK_LIVE_TEST_KEY` (default `test-key`). Never point it at a live
 * ground station: the test sends LAND ALL, so it refuses any address that is
 * not loopback.
 */
class GroundConsoleLiveTest {
    private val url: String? = System.getenv("FLIGHTRISK_LIVE_TEST_URL")?.takeIf { it.isNotBlank() }
    private val key: String = System.getenv("FLIGHTRISK_LIVE_TEST_KEY") ?: TEST_KEY

    /** Only a loopback dashboard is accepted: this test sends LAND ALL. */
    private fun endpoint(): GroundEndpoint {
        assumeTrue("FLIGHTRISK_LIVE_TEST_URL is not set", url != null)
        val endpoint = GroundEndpoint.parse(url!!) ?: throw AssertionError("FLIGHTRISK_LIVE_TEST_URL is not host:port")
        val addresses = InetAddress.getAllByName(endpoint.host)
        if (addresses.isEmpty() || !addresses.all { it.isLoopbackAddress }) {
            throw AssertionError("the live test runs only against a throwaway dashboard on a loopback address")
        }
        return endpoint
    }

    @Test
    fun `connect, receive status, request the full state, send LAND ALL, and probe REST`() = runBlocking {
        val endpoint = endpoint()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val log = CapturingLog()
        val client = GroundConsoleClient(endpoint, DashboardApiKey.of(key), scope, logSink = log)
        val events = CopyOnWriteArrayList<GroundEvent>()
        scope.launch {
            client.events.collect {
                events += it
            }
        }
        try {
            client.start()
            eventually("connected", 15_000) { client.connection.value is ConnectionStatus.Connected }
            // The server sends status before its connect ack; the client delivers it after SessionStarted.
            eventually("status", 15_000) { events.any { it is GroundEvent.Server && it.event is Status } }
            assertTrue(events.first() is GroundEvent.SessionStarted)
            eventually("fleet_status", 15_000) { events.any { it is GroundEvent.Server && it.event is FleetStatus } }
            assertEquals(SendResult.Sent("land_all"), client.landAll())
            eventually("land_all answer", 15_000) {
                events.any { it is GroundEvent.Server && (it.event is LandAllAck || it.event is ServerMessage) }
            }
            val probe = client.healthProbe()
            assertTrue(probe.toString(), probe is HealthProbeResult.Ok)
            println("live: ${events.size} events, probe $probe, state ${client.connection.value}")
        } finally {
            client.shutdown()
            scope.cancel()
        }
        log.lines.forEach { assertFalse(it, it.contains(key)) }
    }

    @Test
    fun `a wrong key fails without a retry loop, and REST reports it`() = runBlocking {
        val endpoint = endpoint()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val opened = CopyOnWriteArrayList<GroundEndpoint>()
        val real = WebSocketSocketIoPort()
        val countingPort = object : SocketIoPort {
            override suspend fun open(endpoint: GroundEndpoint, secrets: List<String>): SocketIoConnection {
                opened += endpoint
                return real.open(endpoint, secrets)
            }
        }
        val client = GroundConsoleClient(endpoint, DashboardApiKey.of("wrong-test-key"), scope, port = countingPort)
        try {
            client.start()
            eventually("failed", 15_000) { client.connection.value is ConnectionStatus.Failed }
            Thread.sleep(3_000)
            assertEquals(1, opened.size)
            val probe = client.healthProbe()
            assertTrue(probe.toString(), probe is HealthProbeResult.WrongKey)
        } finally {
            client.shutdown()
            scope.cancel()
        }
    }
}
