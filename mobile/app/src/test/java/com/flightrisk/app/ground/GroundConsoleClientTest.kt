package com.flightrisk.app.ground

import com.flightrisk.contract.ClientCommand
import com.flightrisk.contract.FleetStatus
import com.flightrisk.contract.MatchAlert
import com.flightrisk.contract.ServerEvent
import com.flightrisk.contract.ServerEvents
import com.flightrisk.contract.Status
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.util.concurrent.CopyOnWriteArrayList

class GroundConsoleClientTest {

    private val open = ContractFixtures.frame("engineio_open")
    private val ack = ContractFixtures.frame("socketio_connect_ack")
    private val connectError = ContractFixtures.frame("socketio_connect_error")
    private val statusFrame = ContractFixtures.eventFrame("status.json", "fleet_two_drones")
    private val fleetStatusFrame = ContractFixtures.eventFrame("fleet_status.json", "all_connected")
    private val reminderFrame = ContractFixtures.eventFrame("match_alert.json", "reminder_on_connect")
    private val videoFrame = ContractFixtures.eventFrame("frame.json", "edge_detections_only")

    private class Harness(
        config: GroundClientConfig = GroundClientConfig(),
        policy: EmitPolicy = EmitPolicy.CONTRACT,
        random: Double = 0.0,
        key: String? = TEST_KEY,
    ) {
        val clock = FakeClock()
        val port = FakePort()
        val log = CapturingLog()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val client = GroundConsoleClient(
            endpoint = GroundEndpoint.of("127.0.0.1", 5555)!!,
            apiKey = DashboardApiKey.of(key),
            scope = scope,
            port = port,
            config = config,
            emitPolicy = policy,
            clock = clock,
            random = { random },
            logSink = log,
        )
        val events = CopyOnWriteArrayList<GroundEvent>()

        fun collect() {
            scope.launch { client.events.collect { events += it } }
        }

        val status: ConnectionStatus get() = client.connection.value
    }

    private fun harnessTest(
        config: GroundClientConfig = GroundClientConfig(),
        policy: EmitPolicy = EmitPolicy.CONTRACT,
        random: Double = 0.0,
        block: suspend Harness.() -> Unit,
    ) = runBlocking {
        val h = Harness(config, policy, random)
        try {
            h.block()
            // Nothing the client logged may carry the key.
            h.log.lines.forEach { assertFalse("key in log: $it", it.contains(TEST_KEY)) }
        } finally {
            h.scope.cancel()
        }
    }

    private suspend fun Harness.openSession(index: Int): FakeConnection {
        val c = port.connection(index)
        c.server(open)
        eventually("namespace connect sent") { c.sent.isNotEmpty() }
        return c
    }

    private suspend fun Harness.connect(index: Int = 0): FakeConnection {
        val c = openSession(index)
        c.server(ack)
        eventually("connected") { status is ConnectionStatus.Connected && (status as ConnectionStatus.Connected).info.connectCount == index + 1 }
        return c
    }

    /** Waits for the backoff sleeper of the current Reconnecting status, then lets that time pass. */
    private suspend fun Harness.advanceBackoff(): Long {
        eventually("reconnecting") { status is ConnectionStatus.Reconnecting }
        val delayMs = (status as ConnectionStatus.Reconnecting).retryInMs
        val deadline = clock.nowMs() + delayMs
        eventually("backoff sleeper") { deadline in clock.sleepers() }
        clock.advance(delayMs)
        return delayMs
    }

    private suspend inline fun <reified T : ConnectionStatus> Harness.awaitStatus(): T {
        eventually("status ${T::class.simpleName}, now $status") { status is T }
        return status as T
    }

    // Connecting

    @Test
    fun `the key goes only into the namespace connect frame, exactly as the fixture`() = harnessTest {
        client.start()
        val c = openSession(0)
        assertEquals(listOf(ContractFixtures.frame("socketio_connect_with_auth")), c.sent.toList())
        c.server(ack)
        val connected = awaitStatus<ConnectionStatus.Connected>()
        assertEquals(SessionInfo(1, 25_000, 20_000), connected.info)
        assertFalse(connected.toString().contains(TEST_KEY))
    }

    @Test
    fun `every connect and reconnect signals the app, which then requests the five states`() = harnessTest {
        collect()
        client.start()
        val first = connect(0)
        eventually { events.isNotEmpty() }
        assertEquals(GroundEvent.SessionStarted(SessionInfo(1, 25_000, 20_000)), events.first())
        val results = client.requestFullState()
        assertTrue(results.all { it is SendResult.Sent })
        assertEquals(
            listOf(
                "42[\"get_fleet_status\"]", "42[\"get_detection_profiles\"]", "42[\"get_ceiling_settings\"]",
                "42[\"get_reasoning_provider\"]", "42[\"get_manual_status\"]",
            ),
            first.eventFrames(),
        )
        first.server("41")
        awaitStatus<ConnectionStatus.Reconnecting>()
        advanceBackoff()
        connect(1)
        eventually { events.count { it is GroundEvent.SessionStarted } == 2 }
        assertEquals(2, (events.last { it is GroundEvent.SessionStarted } as GroundEvent.SessionStarted).info.connectCount)
    }

    @Test
    fun `without a key the connect frame is the fixture's bare connect`() = runBlocking {
        val h = Harness(key = null)
        try {
            h.client.start()
            val c = h.port.connection(0)
            c.server(open)
            eventually { c.sent.isNotEmpty() }
            assertEquals(ContractFixtures.frame("socketio_connect_without_auth"), c.sent.first())
        } finally {
            h.scope.cancel()
        }
    }

    // Early events

    @Test
    fun `events before the connect acknowledgement are held and delivered after it in order`() = harnessTest {
        collect()
        client.start()
        val c = openSession(0)
        c.server(statusFrame)
        c.server(reminderFrame)
        c.server(fleetStatusFrame)
        delay(50)
        assertTrue("nothing delivered before the ack", events.isEmpty())
        assertTrue("board untouched before the ack", client.board.value.drones.isEmpty())
        assertNull(client.snapshot.value.status)
        c.server(ack)
        eventually { events.size == 4 }
        assertTrue(events[0] is GroundEvent.SessionStarted)
        assertTrue((events[1] as GroundEvent.Server).event is Status)
        assertTrue((events[2] as GroundEvent.Server).event is MatchAlert)
        assertTrue((events[3] as GroundEvent.Server).event is FleetStatus)
        assertEquals(listOf("drone-1", "drone-2"), client.board.value.droneIds)
        assertTrue(client.snapshot.value.status != null)
    }

    @Test
    fun `held events are dropped when the connect is refused`() = harnessTest {
        collect()
        client.start()
        val c = openSession(0)
        c.server(statusFrame)
        c.server(connectError)
        val failed = awaitStatus<ConnectionStatus.Failed>()
        assertEquals(FailureReason.AuthRejected("Connection rejected by server", null), failed.reason)
        delay(50)
        assertTrue(events.isEmpty())
        assertNull(client.snapshot.value.status)
    }

    @Test
    fun `too many early events by count is a typed drop, not a silent loss`() = harnessTest(
        config = GroundClientConfig(maxEarlyEvents = 2),
    ) {
        client.start()
        val c = openSession(0)
        repeat(3) { c.server(statusFrame) }
        val r = awaitStatus<ConnectionStatus.Reconnecting>()
        assertTrue(r.reason is DropReason.EarlyEventOverflow)
        assertEquals(3, (r.reason as DropReason.EarlyEventOverflow).events)
    }

    @Test
    fun `too many early bytes is a typed drop`() = harnessTest(
        config = GroundClientConfig(maxEarlyBytes = statusFrame.length * 2L - 1),
    ) {
        client.start()
        val c = openSession(0)
        c.server(statusFrame)
        c.server(statusFrame)
        val r = awaitStatus<ConnectionStatus.Reconnecting>()
        assertTrue(r.reason is DropReason.EarlyEventOverflow)
    }

    // Bad key

    @Test
    fun `a refused key fails and does not retry in a loop`() = harnessTest {
        client.start()
        val c = openSession(0)
        c.server(connectError)
        awaitStatus<ConnectionStatus.Failed>()
        repeat(6) { clock.advance(10 * 60_000) }
        delay(100)
        assertEquals(1, port.opened.size)
        assertTrue(status is ConnectionStatus.Failed)
        // A user action retries once.
        client.retryNow()
        eventually { port.opened.size == 2 }
    }

    @Test
    fun `a new key after a refusal retries at once`() = harnessTest {
        client.start()
        openSession(0).server(connectError)
        awaitStatus<ConnectionStatus.Failed>()
        client.updateApiKey(DashboardApiKey.of("test-key-2"))
        eventually { port.opened.size == 2 }
        val c = port.connection(1)
        c.server(open)
        eventually { c.sent.isNotEmpty() }
        assertEquals("40{\"api_key\":\"test-key-2\"}", c.sent.first())
    }

    @Test
    fun `an explicit slow retry after a refused key waits its delay`() = harnessTest(
        config = GroundClientConfig(authRetryDelayMs = 60_000),
    ) {
        client.start()
        openSession(0).server(connectError)
        val failed = awaitStatus<ConnectionStatus.Failed>()
        assertEquals(60_000L, (failed.reason as FailureReason.AuthRejected).retryInMs)
        eventually { 60_000L in clock.sleepers() }
        clock.advance(59_999)
        delay(50)
        assertEquals(1, port.opened.size)
        clock.advance(1)
        eventually { port.opened.size == 2 }
    }

    // LAND ALL and sending

    @Test
    fun `LAND ALL before start is not sent`() = harnessTest {
        val r = client.landAll()
        assertEquals(SendResult.NotSent("land_all", NotSentReason.NotStarted), r)
    }

    @Test
    fun `LAND ALL while connecting or before the connect ack is not sent and never queued`() = harnessTest {
        client.start()
        val c = port.connection(0)
        val beforeOpen = client.landAll() as SendResult.NotSent
        assertTrue(beforeOpen.reason is NotSentReason.NotConnected)
        c.server(open)
        eventually { c.sent.isNotEmpty() }
        val beforeAck = client.landAll() as SendResult.NotSent
        assertTrue((beforeAck.reason as NotSentReason.NotConnected).status is ConnectionStatus.Connecting)
        c.server(ack)
        awaitStatus<ConnectionStatus.Connected>()
        delay(50)
        assertTrue("never sent later", c.eventFrames().isEmpty())
    }

    @Test
    fun `LAND ALL while reconnecting is not sent, cuts the wait short, and is not sent on reconnect`() = harnessTest {
        client.start()
        val first = connect(0)
        first.server("1")
        val r = awaitStatus<ConnectionStatus.Reconnecting>()
        assertEquals(DropReason.ServerClosed("Engine.IO close"), r.reason)
        val result = client.landAll() as SendResult.NotSent
        assertTrue((result.reason as NotSentReason.NotConnected).status is ConnectionStatus.Reconnecting)
        // The backoff (1 s on the fake clock) was cut short without advancing the clock.
        eventually { port.opened.size == 2 }
        val second = connect(1)
        delay(50)
        assertTrue(first.eventFrames().isEmpty())
        assertTrue(second.eventFrames().isEmpty())
    }

    @Test
    fun `LAND ALL after a refused key is not sent`() = harnessTest {
        client.start()
        openSession(0).server(connectError)
        awaitStatus<ConnectionStatus.Failed>()
        val r = client.landAll() as SendResult.NotSent
        assertTrue((r.reason as NotSentReason.NotConnected).status is ConnectionStatus.Failed)
    }

    @Test
    fun `LAND ALL after shutdown is not sent and nothing reconnects`() = harnessTest {
        client.start()
        val c = connect(0)
        client.shutdown()
        assertEquals(ConnectionStatus.ShutDown, status)
        assertEquals("41", c.sent.last())
        assertEquals(SendResult.NotSent("land_all", NotSentReason.ShutDown), client.landAll())
        repeat(5) { clock.advance(60_000) }
        delay(50)
        assertEquals(1, port.opened.size)
        client.start()
        delay(50)
        assertEquals(1, port.opened.size)
    }

    @Test
    fun `shutdown during a backoff wait never reconnects`() = harnessTest {
        client.start()
        connect(0).server("1")
        awaitStatus<ConnectionStatus.Reconnecting>()
        client.shutdown()
        repeat(5) { clock.advance(60_000) }
        delay(50)
        assertEquals(1, port.opened.size)
        assertEquals(ConnectionStatus.ShutDown, status)
    }

    @Test
    fun `LAND ALL that fails to write never looks sent`() = harnessTest {
        client.start()
        val c = connect(0)
        c.failSends = IOException("broken pipe while sending to test-key")
        val r = client.landAll()
        assertTrue(r is SendResult.NotSent)
        val reason = (r as SendResult.NotSent).reason as NotSentReason.SendFailed
        assertFalse(reason.detail.contains(TEST_KEY))
        assertTrue(reason.detail.contains(Redact.PLACEHOLDER))
        awaitStatus<ConnectionStatus.Reconnecting>()
    }

    @Test
    fun `connected LAND ALL, per-drone land, resume and profiles go out as contract frames`() = harnessTest {
        client.start()
        val c = connect(0)
        assertEquals(SendResult.Sent("land_all"), client.landAll())
        assertEquals(SendResult.Sent("drone_command"), client.landDrone("drone-1"))
        assertEquals(SendResult.Sent("manual_resume_search"), client.resumeHeldDrone("drone-2"))
        assertEquals(SendResult.Sent("set_detection_profile"), client.setDetectionProfile("drone-1", "top_down"))
        assertEquals(SendResult.Sent("set_fleet_detection_profile"), client.setFleetDetectionProfile("top_down"))
        assertEquals(SendResult.NotSent("drone_command", NotSentReason.InvalidDroneId), client.landDrone(" "))
        assertEquals(
            listOf(
                "42[\"land_all\"]",
                ContractFixtures.frame("socketio_event_client_to_server_with_payload"),
                "42[\"manual_resume_search\",{\"drone_id\":\"drone-2\"}]",
                "42[\"set_detection_profile\",{\"drone_id\":\"drone-1\",\"profile\":\"top_down\"}]",
                "42[\"set_fleet_detection_profile\",{\"profile\":\"top_down\"}]",
            ),
            c.eventFrames(),
        )
    }

    // Emit policy

    @Test
    fun `a narrowed policy blocks other events but never LAND ALL`() = harnessTest(
        policy = EmitPolicy.CONTRACT.narrowedTo(emptySet()).without("land_all"),
    ) {
        client.start()
        val c = connect(0)
        assertEquals(
            SendResult.NotSent("get_fleet_status", NotSentReason.BlockedByPolicy),
            client.send(ClientCommand.GetFleetStatus),
        )
        assertEquals(
            SendResult.NotSent("drone_command", NotSentReason.BlockedByPolicy),
            client.landDrone("drone-1"),
        )
        assertEquals(SendResult.Sent("land_all"), client.landAll())
        assertEquals(listOf("42[\"land_all\"]"), c.eventFrames())
    }

    // Video and state paths

    @Test
    fun `video frames keep only the latest per drone, bounded, and never enter the event queue`() = harnessTest(
        config = GroundClientConfig(maxVideoDrones = 3, maxVideoFrameChars = 4_096),
    ) {
        collect()
        client.start()
        val c = connect(0)
        for (i in 1..5) {
            c.server(videoFrame.replace("\"drone_id\":\"drone-2\"", "\"drone_id\":\"drone-$i\""))
        }
        c.server(
            videoFrame.replace("\"drone_id\":\"drone-2\"", "\"drone_id\":\"drone-4\"")
                .replace("\"fps\":", "\"fps\":99,\"x\":"),
        )
        c.server(fleetStatusFrame)
        c.server("42[\"frame\",{\"drone_id\":\"drone-9\",\"image\":\"" + "A".repeat(5_000) + "\"}]")
        eventually { events.any { it is GroundEvent.Server && it.event is FleetStatus } }
        delay(50)
        val frames = client.frames.value
        assertEquals(setOf("drone-3", "drone-4", "drone-5"), frames.keys)
        assertEquals(99.0, frames.getValue("drone-4").fps)
        assertTrue(client.framesDiscarded >= 1)
        assertTrue(events.none { it is GroundEvent.Server && it.event is com.flightrisk.contract.Frame })
    }

    @Test
    fun `a stalled consumer drops the session with a typed reason instead of losing state events silently`() = harnessTest(
        config = GroundClientConfig(maxQueuedEvents = 5),
    ) {
        client.start()
        val c = connect(0)
        repeat(10) { c.server(fleetStatusFrame) }
        val r = awaitStatus<ConnectionStatus.Reconnecting>()
        assertTrue(r.reason is DropReason.ConsumerStalled)
        // The board still saw every event that was accepted.
        assertEquals(listOf("drone-1", "drone-2"), client.board.value.droneIds)
    }

    @Test
    fun `an oversized frame is a typed drop`() = harnessTest(config = GroundClientConfig(maxMessageChars = 1_000)) {
        client.start()
        val c = connect(0)
        c.server("42[\"status\",{\"x\":\"" + "A".repeat(2_000) + "\"}]")
        val r = awaitStatus<ConnectionStatus.Reconnecting>()
        assertEquals(DropReason.MessageTooLarge(1_000), r.reason)
    }

    @Test
    fun `a transport size error is reported as MessageTooLarge`() = harnessTest {
        client.start()
        val c = connect(0)
        c.serverError(MessageTooLargeException(8L * 1024 * 1024))
        val r = awaitStatus<ConnectionStatus.Reconnecting>()
        assertEquals(DropReason.MessageTooLarge(8L * 1024 * 1024), r.reason)
    }

    @Test
    fun `the board is stale after a drop and fresh after the next fleet_status`() = harnessTest {
        collect()
        client.start()
        val c = connect(0)
        c.server(fleetStatusFrame)
        eventually { client.board.value.drones.isNotEmpty() }
        assertFalse(client.board.value.stale)
        c.server("1")
        eventually { client.board.value.stale }
        advanceBackoff()
        val c2 = connect(1)
        c2.server(fleetStatusFrame)
        eventually { !client.board.value.stale }
    }

    // Backoff and dead links

    @Test
    fun `reconnect backoff is exponential, bounded, and jittered`() = harnessTest(
        config = GroundClientConfig(backoff = Backoff(initialMs = 1_000, maxMs = 8_000, jitterFraction = 0.2)),
        random = 1.0,
    ) {
        port.failOpen = IOException("connection refused")
        client.start()
        val seen = mutableListOf<Long>()
        for (attempt in 1..6) {
            eventually { (status as? ConnectionStatus.Reconnecting)?.attempt == attempt }
            seen += advanceBackoff()
        }
        assertEquals(listOf(800L, 1_600L, 3_200L, 6_400L, 6_400L, 6_400L), seen)
    }

    @Test
    fun `backoff resets only after the link stayed up for one ping interval`() = harnessTest {
        client.start()
        connect(0).server("1")
        assertEquals(1, awaitStatus<ConnectionStatus.Reconnecting>().attempt)
        advanceBackoff()
        val short = connect(1)
        clock.advance(24_999)
        short.server("1")
        eventually { (status as? ConnectionStatus.Reconnecting)?.attempt == 2 }
        assertEquals(2_000L, (status as ConnectionStatus.Reconnecting).retryInMs)
        assertEquals(2_000L, advanceBackoff())
        val long = connect(2)
        clock.advance(25_000)
        long.server("1")
        eventually { (status as? ConnectionStatus.Reconnecting)?.attempt == 1 }
        assertEquals(1_000L, (status as ConnectionStatus.Reconnecting).retryInMs)
    }

    @Test
    fun `pings are answered and a silent server is dropped after pingInterval plus pingTimeout`() = harnessTest {
        client.start()
        val c = connect(0)
        c.server(ContractFixtures.frame("engineio_ping"))
        eventually { c.sent.contains(ContractFixtures.frame("engineio_pong")) }
        clock.advance(44_999)
        delay(50)
        assertTrue(status is ConnectionStatus.Connected)
        clock.advance(1)
        val r = awaitStatus<ConnectionStatus.Reconnecting>()
        assertEquals(DropReason.PingTimeout, r.reason)
        assertTrue(c.closed)
    }

    @Test
    fun `no open packet within the handshake timeout is a typed drop`() = harnessTest {
        client.start()
        port.connection(0)
        clock.advance(10_000)
        assertEquals(DropReason.HandshakeTimeout, awaitStatus<ConnectionStatus.Reconnecting>().reason)
    }

    @Test
    fun `a non-open first frame is a protocol error`() = harnessTest {
        client.start()
        port.connection(0).server(ack)
        assertTrue(awaitStatus<ConnectionStatus.Reconnecting>().reason is DropReason.ProtocolError)
    }

    @Test
    fun `an address outside the private ranges fails without a retry loop`() = harnessTest {
        port.failOpen = AddressNotAllowedException("the ground station must be on a private network")
        client.start()
        val failed = awaitStatus<ConnectionStatus.Failed>()
        assertTrue(failed.reason is FailureReason.AddressNotAllowed)
        repeat(5) { clock.advance(60_000) }
        delay(50)
        assertTrue(status is ConnectionStatus.Failed)
    }

    // Redaction

    @Test
    fun `a server message is redacted first, then truncated`() = harnessTest {
        client.start()
        val c = openSession(0)
        // The key straddles the 200-character boundary.
        val message = "x".repeat(195) + TEST_KEY + "y".repeat(300)
        c.server("44{\"message\":\"$message\"}")
        val failed = awaitStatus<ConnectionStatus.Failed>()
        val shown = (failed.reason as FailureReason.AuthRejected).serverMessage!!
        assertFalse(shown.contains("test"))
        assertFalse(shown.contains("-key"))
        assertTrue(shown.length <= Redact.MAX_LENGTH)
        assertFalse(failed.toString().contains(TEST_KEY))
    }

    @Test
    fun `nothing the client exposes carries the key`() = harnessTest {
        collect()
        client.start()
        val c = connect(0)
        c.server(statusFrame)
        c.server("42[\"error\",{\"message\":\"bad Bearer $TEST_KEY\"}]")
        eventually { events.size >= 3 }
        for (text in listOf(client.toString(), status.toString(), events.toString(), client.snapshot.value.toString())) {
            assertFalse(text, text.contains("Bearer $TEST_KEY"))
        }
        assertFalse(DashboardApiKey.of(TEST_KEY).toString().contains(TEST_KEY))
    }

    // Fixtures

    @Test
    fun `every server event fixture decodes through the client`() = harnessTest {
        collect()
        client.start()
        val c = connect(0)
        val files = listOf(
            "status.json", "fleet_status.json", "fleet_event.json", "fleet_plan.json", "fleet_detection.json",
            "match_alert.json", "detection_profiles.json", "ceiling_settings.json", "reasoning_provider.json",
            "manual_status.json", "manual_ack.json", "manual_result.json", "land_all_ack.json", "command_ack.json",
            "stream_video_state.json", "error.json", "unknown_keys.json",
        )
        var expected = 1 // SessionStarted
        for (file in files) {
            for ((name, case) in ContractFixtures.cases(file)) {
                val event = (case["event"] as? kotlinx.serialization.json.JsonPrimitive)?.content ?: continue
                if (event == ServerEvents.FRAME || event !in ServerEvents.HANDLED) continue
                c.server(ContractFixtures.eventFrame(file, name))
                expected++
            }
        }
        eventually("all fixture events delivered") { events.size == expected }
        val decoded = events.filterIsInstance<GroundEvent.Server>().map { it.event }
        assertTrue(decoded.none { it is ServerEvent.Malformed || it is ServerEvent.Ignored })
        assertTrue(client.snapshot.value.ceilingSettings != null)
        assertTrue(client.snapshot.value.reasoningProvider != null)
        assertTrue(client.snapshot.value.fleetPlan != null)
    }
}
