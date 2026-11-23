package com.flightrisk.app.demo

import com.flightrisk.app.ground.ConnectionStatus
import com.flightrisk.app.ground.FakeClock
import com.flightrisk.app.ground.GroundConsoleClient
import com.flightrisk.app.ground.GroundEndpoint
import com.flightrisk.app.ground.GroundEvent
import com.flightrisk.app.ground.eventually
import com.flightrisk.app.ui.ground.DemoFixtures
import com.flightrisk.app.ui.ground.GroundConsoleViewModel
import com.flightrisk.contract.AlertUpgrade
import com.flightrisk.contract.CeilingSettings
import com.flightrisk.contract.DetectionProfiles
import com.flightrisk.contract.DroneRecord
import com.flightrisk.contract.FleetEvent
import com.flightrisk.contract.FleetStatus
import com.flightrisk.contract.LandAllAck
import com.flightrisk.contract.ManualReply
import com.flightrisk.contract.ManualStatus
import com.flightrisk.contract.MatchAlert
import com.flightrisk.contract.ReasoningProvider
import com.flightrisk.contract.ReasoningResult
import com.flightrisk.contract.ServerEvent
import com.flightrisk.contract.ServerMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The demo ground under the real [GroundConsoleClient]: the replay, the state answers and the local
 * acknowledgement of every command. The clock is a fake, so the mission runs as fast as the test moves it.
 */
class DemoGroundPortTest {

    private class Session : AutoCloseable {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val clock = FakeClock()
        val port = DemoGroundPort(DemoFixtures.scenario, scope, clock)
        val client = GroundConsoleClient(
            DemoGroundPort.ENDPOINT, null, scope, port = port, emitPolicy = GroundConsoleViewModel.EMIT_POLICY,
            clock = clock, random = { 0.0 },
            restClientFactory = { _, _ -> throw AssertionError("the demo made a REST request") },
        )
        val events = CopyOnWriteArrayList<ServerEvent>()

        init {
            scope.launch { client.events.collect { if (it is GroundEvent.Server) events += it.event } }
            client.start()
        }

        val connection get() = port.connections.single()

        inline fun <reified T : ServerEvent> all(): List<T> = events.filterIsInstance<T>()

        /** Moves the replay on by [ms], one second at a time, letting each step arrive. */
        suspend fun play(ms: Long) {
            var left = ms
            while (left > 0) {
                val step = minOf(1_000L, left)
                clock.advance(step)
                left -= step
                delay(3)
            }
        }

        override fun close() {
            runBlocking { client.shutdown() }
            scope.cancel()
        }
    }

    @Test
    fun `the demo connects and answers the state requests at once`() = runBlocking {
        Session().use { s ->
            eventually("connected") { s.client.connection.value is ConnectionStatus.Connected }
            eventually("state answered") {
                s.all<DetectionProfiles>().isNotEmpty() && s.all<CeilingSettings>().isNotEmpty() &&
                    s.all<ReasoningProvider>().isNotEmpty() && s.all<ManualStatus>().isNotEmpty() && s.all<FleetStatus>().isNotEmpty()
            }
            assertTrue(s.connection.acknowledged.isEmpty())
        }
    }

    @Test
    fun `the mission replays to the verdict and the landing`() = runBlocking {
        Session().use { s ->
            eventually("connected") { s.client.connection.value is ConnectionStatus.Connected }
            s.play(DemoFixtures.scenario.durationMs + 1_000)
            eventually("replay finished") { s.connection.finished }
            eventually("last step delivered") { s.all<FleetEvent>().any { it is FleetEvent.LandAll } && s.all<AlertUpgrade>().isNotEmpty() }
            val alert = s.all<MatchAlert>().single()
            val verdict = s.all<ReasoningResult>().single()
            assertEquals(alert.alertId, verdict.alertId)
            assertEquals(alert.alertId, s.all<AlertUpgrade>().single().alertId)
            assertTrue(s.all<FleetEvent>().any { it.kind == "lane_blocked" })
            eventually("landed") {
                val drones = (s.client.board.value.drones.values).filterIsInstance<DroneRecord.Fleet>()
                drones.size == 2 && drones.all { it.drone.isFlying == false }
            }
            // The order of the scenario is kept.
            val order = s.events.map { it::class }
            assertTrue(order.indexOf(MatchAlert::class) < order.indexOf(ReasoningResult::class))
            assertTrue(order.indexOf(ReasoningResult::class) < order.indexOf(AlertUpgrade::class))
        }
    }

    @Test
    fun `LAND ALL is acknowledged on the phone, lands every drone and ends the replay`() = runBlocking {
        Session().use { s ->
            eventually("connected") { s.client.connection.value is ConnectionStatus.Connected }
            s.play(8_000)
            eventually("flying") { s.client.board.value.drones.values.filterIsInstance<DroneRecord.Fleet>().any { it.drone.isFlying == true } }
            s.client.landAll()
            eventually("acknowledged") { s.all<LandAllAck>().isNotEmpty() && s.all<ServerMessage>().isNotEmpty() }
            assertEquals(listOf("land_all"), s.connection.acknowledged)
            assertEquals(true, s.all<LandAllAck>().single().reachedAny)
            assertEquals(DemoText.LAND_ALL_ACKNOWLEDGED, s.all<ServerMessage>().single().message)
            eventually("all landed") {
                s.client.board.value.drones.values.filterIsInstance<DroneRecord.Fleet>().all { it.drone.isFlying == false }
            }
            s.play(DemoFixtures.scenario.durationMs)
            eventually("replay ended") { s.connection.finished }
            // The replay stopped at LAND ALL: the later steps (the alert, the cut) never came, and nothing flies.
            assertTrue(s.all<MatchAlert>().isEmpty())
            assertFalse(s.all<FleetEvent>().any { it.kind == "lane_blocked" })
            assertTrue(s.client.board.value.drones.values.filterIsInstance<DroneRecord.Fleet>().all { it.drone.isFlying == false })
        }
    }

    @Test
    fun `a per-drone Land is acknowledged for that drone and the drone reads as on the ground`() = runBlocking {
        Session().use { s ->
            eventually("connected") { s.client.connection.value is ConnectionStatus.Connected }
            s.play(8_000)
            eventually("flying") { s.client.board.value.drones.values.filterIsInstance<DroneRecord.Fleet>().count { it.drone.isFlying == true } == 2 }
            s.client.landDrone("drone-2")
            eventually("acknowledged") { s.all<ManualReply>().size == 2 }
            assertTrue(s.all<ManualReply>().all { it.droneId == "drone-2" && it.ok == true })
            assertEquals(listOf(ManualReply.Kind.ACK, ManualReply.Kind.RESULT), s.all<ManualReply>().map { it.kind })
            s.play(4_000)
            eventually("drone-2 on the ground") {
                val drones = s.client.board.value.drones
                (drones["drone-2"] as? DroneRecord.Fleet)?.drone?.isFlying == false && (drones["drone-1"] as? DroneRecord.Fleet)?.drone?.isFlying == true
            }
            s.client.resumeHeldDrone("drone-1")
            eventually("resume acknowledged") { s.connection.acknowledged == listOf("drone_command", "manual_resume_search") }
        }
    }

    @Test
    fun `the demo port serves only the demo endpoint`() = runBlocking {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        try {
            val port = DemoGroundPort(DemoFixtures.scenario, scope, FakeClock())
            for (host in listOf("192.168.1.20", "127.0.0.1", "ground.example.com")) {
                val refused = try {
                    port.open(GroundEndpoint.of(host)!!, emptyList())
                    false
                } catch (_: IOException) {
                    true
                }
                assertTrue(host, refused)
            }
            assertTrue(port.connections.isEmpty())
            assertEquals("demo.invalid", DemoGroundPort.ENDPOINT.host)
        } finally {
            scope.cancel()
        }
    }
}
