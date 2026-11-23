package com.flightrisk.app.demo

import com.flightrisk.app.ground.GroundClock
import com.flightrisk.app.ground.GroundEndpoint
import com.flightrisk.app.ground.SocketIoConnection
import com.flightrisk.app.ground.SocketIoPort
import com.flightrisk.contract.EngineIoType
import com.flightrisk.contract.ServerEvents
import com.flightrisk.contract.SocketIoFrames
import com.flightrisk.contract.SocketIoType
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.io.IOException
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The demo ground: a [SocketIoPort] that plays a FlightRisk ground station inside the app.
 *
 * [GroundConsoleClient][com.flightrisk.app.ground.GroundConsoleClient] runs over it exactly as it runs over
 * a real WebSocket, so the demo exercises the real decoders, the drone state reducer and every Ground
 * screen card. The connection it opens is a pair of in-memory queues: it opens no socket, resolves no
 * host and sends nothing off the phone. It
 *
 * - replays the [DemoScenario] steps on [clock], from the moment the client's namespace connect arrives;
 * - answers the five state requests from the scenario's state and the latest replayed fleet status;
 * - acknowledges every operator command (LAND ALL, Land, Resume search, a profile selection) locally with
 *   the scenario's contract replies, followed by a `warning` that says nothing was sent. A landed drone
 *   reads as on the ground in every later fleet status, and LAND ALL ends the replay with every drone on
 *   the ground.
 *
 * Only [ENDPOINT] is served; any other endpoint is refused, so this port can never stand in for a real one.
 */
class DemoGroundPort(
    private val scenario: DemoScenario,
    private val scope: CoroutineScope,
    private val clock: GroundClock = GroundClock.SYSTEM,
) : SocketIoPort {

    /** The connections opened so far, for tests. */
    val connections: List<DemoConnection> get() = opened

    private val opened = CopyOnWriteArrayList<DemoConnection>()

    override suspend fun open(endpoint: GroundEndpoint, secrets: List<String>): SocketIoConnection {
        if (endpoint != ENDPOINT) throw IOException("the demo ground serves only the demo endpoint")
        return DemoConnection(scenario, scope, clock).also { opened += it }
    }

    companion object {
        /**
         * The demo's address. `.invalid` is reserved (RFC 2606) and never resolves, so even code that
         * mistook it for a real address could not reach anything with it.
         */
        val ENDPOINT: GroundEndpoint = requireNotNull(GroundEndpoint.of("demo.invalid", GroundEndpoint.DEFAULT_PORT))

        /** The ping interval the demo announces; it pings well within it. */
        const val PING_INTERVAL_MS = 25_000L
        const val PING_TIMEOUT_MS = 20_000L
        internal const val PING_EVERY_MS = 15_000L

        internal const val OPEN_FRAME =
            "0{\"sid\":\"demo\",\"upgrades\":[],\"pingInterval\":$PING_INTERVAL_MS,\"pingTimeout\":$PING_TIMEOUT_MS,\"maxPayload\":1000000}"
        internal const val CONNECT_ACK = "40{\"sid\":\"demo-sid\"}"

        /** A dashboard event as the Engine.IO text frame the server would send. */
        fun eventFrame(event: String, payload: JsonElement): String =
            "42" + JsonArray(listOf(JsonPrimitive(event), payload)).toString()
    }
}

/**
 * One demo session. Frames from the "server" wait in [outbox] until the client reads them; frames the
 * client sends are answered at once, in memory.
 */
class DemoConnection internal constructor(
    private val scenario: DemoScenario,
    scope: CoroutineScope,
    private val clock: GroundClock,
) : SocketIoConnection {
    private val outbox = Channel<String>(Channel.UNLIMITED)
    private val connected = CompletableDeferred<Unit>()
    private val lock = Any()
    private var latestFleetStatus: JsonObject? = null
    private var latestPlan: JsonObject? = null
    private val landedDrones = LinkedHashSet<String>()
    private var allLanded = false

    /** Every command event this session acknowledged locally, in order. For tests. */
    val acknowledged: MutableList<String> = CopyOnWriteArrayList()

    /** True once the scenario was replayed to its end or ended by LAND ALL. */
    @Volatile var finished = false
        private set

    private val driver: Job = scope.launch { run() }

    override suspend fun receive(): String =
        outbox.receiveCatching().getOrNull() ?: throw IOException("the demo connection is closed")

    override suspend fun send(frame: String) {
        if (outbox.isClosedForSend) throw IOException("the demo connection is closed")
        val parsed = SocketIoFrames.parse(frame) ?: return
        when (parsed.type) {
            EngineIoType.CLOSE -> close()
            EngineIoType.MESSAGE -> {
                val packet = parsed.socketIo ?: return
                when (packet.type) {
                    SocketIoType.CONNECT -> connected.complete(Unit)
                    SocketIoType.DISCONNECT -> close()
                    SocketIoType.EVENT -> packet.eventName?.let { onClientEvent(it, packet.eventPayload) }
                    else -> Unit
                }
            }
            else -> Unit // A pong needs no answer.
        }
    }

    override suspend fun closeGracefully() = close()

    override fun close() {
        outbox.close()
        driver.cancel()
    }

    private fun push(frame: String) {
        outbox.trySend(frame)
    }

    private fun pushEvent(event: String, payload: JsonObject) = push(DemoGroundPort.eventFrame(event, payload))

    private suspend fun run() {
        push(DemoGroundPort.OPEN_FRAME)
        connected.await()
        push(DemoGroundPort.CONNECT_ACK)
        coroutineScope {
            launch {
                while (true) {
                    clock.sleep(DemoGroundPort.PING_EVERY_MS)
                    push("2")
                }
            }
            // Each step is due at a fixed time after the connect, so a slow step never delays the next ones.
            val start = clock.nowMs()
            for (step in scenario.steps) {
                val wait = start + step.atMs - clock.nowMs()
                if (wait > 0) clock.sleep(wait)
                if (!replay(step)) break
            }
            finished = true
        }
    }

    /** Sends one scenario step; false once LAND ALL ended the replay. */
    private fun replay(step: DemoScenario.Step): Boolean {
        val payload = synchronized(lock) {
            if (allLanded) return false
            when (step.event) {
                ServerEvents.FLEET_STATUS -> {
                    latestFleetStatus = step.payload
                    withLandedDrones(step.payload)
                }
                ServerEvents.FLEET_PLAN -> {
                    latestPlan = step.payload
                    step.payload
                }
                else -> step.payload
            }
        }
        pushEvent(step.event, payload)
        return true
    }

    private fun onClientEvent(event: String, payload: JsonElement?) {
        when (event) {
            "get_fleet_status" -> {
                val (status, plan) = synchronized(lock) { currentFleetStatus() to latestPlan }
                status?.let { pushEvent(ServerEvents.FLEET_STATUS, it) }
                plan?.let { pushEvent(ServerEvents.FLEET_PLAN, it) }
            }
            "get_detection_profiles" -> scenario.state[ServerEvents.DETECTION_PROFILES]?.let { pushEvent(ServerEvents.DETECTION_PROFILES, it) }
            "get_ceiling_settings" -> scenario.state[ServerEvents.CEILING_SETTINGS]?.let { pushEvent(ServerEvents.CEILING_SETTINGS, it) }
            "get_reasoning_provider" -> scenario.state[ServerEvents.REASONING_PROVIDER]?.let { pushEvent(ServerEvents.REASONING_PROVIDER, it) }
            "get_manual_status" -> scenario.state[ServerEvents.MANUAL_STATUS]?.let { pushEvent(ServerEvents.MANUAL_STATUS, it) }
            "land_all" -> {
                reply(event, null)
                val landed = synchronized(lock) {
                    allLanded = true
                    currentFleetStatus()
                }
                landed?.let { pushEvent(ServerEvents.FLEET_STATUS, it) }
                acknowledge(event, DemoText.LAND_ALL_ACKNOWLEDGED)
            }
            "drone_command" -> {
                val droneId = droneIdOf(payload) ?: return
                reply(event, droneId)
                val status = synchronized(lock) {
                    landedDrones += droneId
                    currentFleetStatus()
                }
                status?.let { pushEvent(ServerEvents.FLEET_STATUS, it) }
                acknowledge(event, DemoText.landAcknowledged(droneId))
            }
            "manual_resume_search" -> {
                val droneId = droneIdOf(payload) ?: return
                reply(event, droneId)
                acknowledge(event, DemoText.resumeAcknowledged(droneId))
            }
            "set_detection_profile", "set_fleet_detection_profile" -> {
                scenario.state[ServerEvents.DETECTION_PROFILES]?.let { pushEvent(ServerEvents.DETECTION_PROFILES, it) }
                acknowledge(event, DemoText.PROFILE_ACKNOWLEDGED)
            }
            else -> Unit // A real ground ignores an event it does not know; so does the demo.
        }
    }

    private fun acknowledge(event: String, note: String) {
        acknowledged += event
        pushEvent(ServerEvents.WARNING, buildJsonObject { put("message", JsonPrimitive(note)) })
    }

    /** Sends the scenario's replies to [event], with `drone_id` set to the drone the operator chose. */
    private fun reply(event: String, droneId: String?) {
        for (r in scenario.replies[event].orEmpty()) {
            val payload = if (droneId != null && r.payload.containsKey("drone_id")) {
                JsonObject(r.payload + ("drone_id" to JsonPrimitive(droneId)))
            } else {
                r.payload
            }
            pushEvent(r.event, payload)
        }
    }

    /** The fleet status to show now. Caller holds [lock]. */
    private fun currentFleetStatus(): JsonObject? {
        if (allLanded) return scenario.landedFleetStatus ?: latestFleetStatus?.let { allOnTheGround(it) }
        val base = latestFleetStatus ?: scenario.steps.firstOrNull { it.event == ServerEvents.FLEET_STATUS }?.payload
        return base?.let { withLandedDrones(it) }
    }

    /** [status] with every drone the operator landed reading as on the ground. Caller holds [lock]. */
    private fun withLandedDrones(status: JsonObject): JsonObject {
        if (landedDrones.isEmpty()) return status
        return mapDrones(status) { id, drone -> if (id in landedDrones) onTheGround(drone) else drone }
    }

    private fun allOnTheGround(status: JsonObject): JsonObject = mapDrones(status) { _, drone -> onTheGround(drone) }

    private fun mapDrones(status: JsonObject, change: (String, JsonObject) -> JsonObject): JsonObject {
        val drones = status["drones"] as? JsonObject ?: return status
        val next = JsonObject(drones.mapValues { (id, d) -> (d as? JsonObject)?.let { change(id, it) } ?: d })
        return JsonObject(status + ("drones" to next))
    }

    private fun onTheGround(drone: JsonObject): JsonObject =
        JsonObject(drone + ("is_flying" to JsonPrimitive(false)))

    private fun droneIdOf(payload: JsonElement?): String? =
        ((payload as? JsonObject)?.get("drone_id") as? JsonPrimitive)?.takeIf { it.isString }?.content?.takeIf { it.isNotEmpty() }
}
