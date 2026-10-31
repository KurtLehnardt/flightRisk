package com.flightrisk.app.ground

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Stands in for Android's main thread: the client's scope runs on one thread
 * named "fake-main", and the transport's [IoGuard] throws (like Android's
 * NetworkOnMainThreadException, a RuntimeException) whenever blocking socket
 * IO is attempted on it. Every violation is also recorded, so one that some
 * code swallowed still fails the test.
 */
class MainThreadSafetyTest {
    private val mainExecutor = Executors.newSingleThreadExecutor { r -> Thread(r, "fake-main") }
    private val main = mainExecutor.asCoroutineDispatcher()
    private val violations = CopyOnWriteArrayList<String>()

    @Volatile private var failWrites = false

    private val guard = IoGuard { op ->
        if (Thread.currentThread().name == "fake-main") {
            violations += op
            throw IllegalStateException("blocking $op on the main thread")
        }
        if (op == "write" && failWrites) throw IOException("simulated write failure")
    }

    @After
    fun tearDown() {
        main.close()
    }

    /** A fake dashboard: upgrade, open, connect ack, then it reads (and ignores) until the client leaves. */
    private fun dashboard(connected: CountDownLatch = CountDownLatch(1)) = LoopbackServer { s: Socket ->
        val input = s.getInputStream()
        val out = s.getOutputStream()
        acceptUpgrade(readRequestHead(input), out)
        writeServerText(out, ContractFixtures.frame("engineio_open"))
        readClientText(input)
        writeServerText(out, ContractFixtures.frame("socketio_connect_ack"))
        connected.countDown()
        try {
            while (true) readClientFrame(input)
        } catch (_: Exception) {
        }
    }

    private fun client(server: LoopbackServer, scope: CoroutineScope, clock: GroundClock = GroundClock.SYSTEM) =
        GroundConsoleClient(
            GroundEndpoint.of("127.0.0.1", server.port)!!, DashboardApiKey.of(TEST_KEY), scope,
            port = WebSocketSocketIoPort(guard = guard), clock = clock,
            config = GroundClientConfig(backoff = Backoff(initialMs = 600_000, maxMs = 600_000)),
        )

    @Test
    fun `LAND ALL whose write fails returns NotSent from the main thread`() = runBlocking {
        val connected = CountDownLatch(1)
        dashboard(connected).use { server ->
            val scope = CoroutineScope(SupervisorJob() + main)
            val c = client(server, scope)
            try {
                withContext(main) { c.start() }
                assertTrue(connected.await(10, TimeUnit.SECONDS))
                eventually { c.connection.value is ConnectionStatus.Connected }
                failWrites = true
                val result = withContext(main) { c.landAll() }
                assertTrue(result.toString(), (result as SendResult.NotSent).reason is NotSentReason.SendFailed)
                eventually { c.connection.value is ConnectionStatus.Reconnecting }
                // Plain send failure too, for another command.
                assertTrue(withContext(main) { c.landDrone("drone-1") } is SendResult.NotSent)
                assertEquals(emptyList<String>(), violations.toList())
            } finally {
                failWrites = false
                withContext(main) { c.shutdown() }
                scope.cancel()
            }
        }
    }

    @Test
    fun `shutdown while connected runs from the main thread without blocking IO on it`() = runBlocking {
        val connected = CountDownLatch(1)
        dashboard(connected).use { server ->
            val scope = CoroutineScope(SupervisorJob() + main)
            val c = client(server, scope)
            withContext(main) { c.start() }
            assertTrue(connected.await(10, TimeUnit.SECONDS))
            eventually { c.connection.value is ConnectionStatus.Connected }
            withContext(main) { c.shutdown() }
            assertEquals(ConnectionStatus.ShutDown, c.connection.value)
            assertEquals(emptyList<String>(), violations.toList())
            scope.cancel()
        }
    }

    @Test
    fun `the ping watchdog closes the link from the main thread without blocking IO on it`() = runBlocking {
        val connected = CountDownLatch(1)
        val clock = FakeClock()
        dashboard(connected).use { server ->
            val scope = CoroutineScope(SupervisorJob() + main)
            val c = client(server, scope, clock)
            try {
                withContext(main) { c.start() }
                assertTrue(connected.await(10, TimeUnit.SECONDS))
                eventually { c.connection.value is ConnectionStatus.Connected }
                eventually { 45_000L in clock.sleepers() }
                clock.advance(45_000)
                eventually { (c.connection.value as? ConnectionStatus.Reconnecting)?.reason == DropReason.PingTimeout }
                assertEquals(emptyList<String>(), violations.toList())
            } finally {
                withContext(main) { c.shutdown() }
                scope.cancel()
            }
        }
    }

    @Test
    fun `the health probe can be called from the main thread`() = runBlocking {
        LoopbackServer { s ->
            readRequestHead(s.getInputStream())
            s.getOutputStream().write("HTTP/1.1 500 Oops\r\nContent-Length: 0\r\n\r\n".toByteArray())
        }.use { server ->
            val rest = DashboardRestClient(GroundEndpoint.of("127.0.0.1", server.port)!!, DashboardApiKey.of(TEST_KEY), guard = guard)
            val result = withContext(main) { rest.probe() }
            assertEquals(HealthProbeResult.UnexpectedStatus("/api/health", 500), result)
            assertEquals(emptyList<String>(), violations.toList())
        }
    }
}
