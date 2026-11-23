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
import kotlinx.serialization.json.JsonNull
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
 *   reads as on the ground, where it landed, in every later fleet status; a held drone whose search was
 *   resumed reads as flying its plan; and LAND ALL ends the replay with every drone on the ground. Resume
 *   search for a drone that is not held is refused as the real ground refuses it (`not_manual`);
 * - keeps sending the current fleet status every [STATUS_EVERY_MS] after the replay ended (or LAND ALL
 *   ended it), so the demo's state never goes "not live".
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

        /**
         * How often the demo sends the current fleet status once the replay ended or a demo LAND ALL stopped it
         * (the scenario's own interval), well inside a phone's 30 s freshness limit.
         */
        const val STATUS_EVERY_MS = 4_000L

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
 *
 * Every frame that carries the fleet's state (a replayed step, a state answer, a command's answer) is put
 * in [outbox] while [lock] is held, so the client always reads the fleet in the order its state changed:
 * a replayed "flying" status can never arrive after the "landed" status of a demo LAND ALL.
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
    private var latestManualStatus: JsonObject? = scenario.state[ServerEvents.MANUAL_STATUS]

    /** Drones the operator landed here, with the entry they keep from then on (on the ground, where they landed). */
    private val landedDrones = LinkedHashMap<String, JsonObject>()

    /** Held drones whose search the operator resumed here: they read as flying their plan again. */
    private val resumedDrones = LinkedHashSet<String>()
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
            // After the replay (or a demo LAND ALL) the fleet stays on screen as a live ground keeps it: a
            // fleet status every few seconds, so the Ground screen never marks the demo's state "not live".
            while (true) {
                clock.sleep(DemoGroundPort.STATUS_EVERY_MS)
                synchronized(lock) { currentFleetStatus()?.let { pushEvent(ServerEvents.FLEET_STATUS, it) } }
            }
        }
    }

    /** Sends one scenario step; false once LAND ALL ended the replay. */
    private fun replay(step: DemoScenario.Step): Boolean = synchronized(lock) {
        if (allLanded) return false
        val payload = when (step.event) {
            ServerEvents.FLEET_STATUS -> {
                latestFleetStatus = step.payload
                withOperatorChanges(step.payload)
            }
            ServerEvents.FLEET_PLAN -> {
                latestPlan = step.payload
                step.payload
            }
            ServerEvents.MANUAL_STATUS -> {
                latestManualStatus = step.payload
                manualWithOperatorChanges(step.payload)
            }
            else -> step.payload
        }
        pushEvent(step.event, payload)
        true
    }

    private fun onClientEvent(event: String, payload: JsonElement?) {
        when (event) {
            "get_fleet_status" -> synchronized(lock) {
                currentFleetStatus()?.let { pushEvent(ServerEvents.FLEET_STATUS, it) }
                latestPlan?.let { pushEvent(ServerEvents.FLEET_PLAN, it) }
            }
            "get_detection_profiles" -> scenario.state[ServerEvents.DETECTION_PROFILES]?.let { pushEvent(ServerEvents.DETECTION_PROFILES, it) }
            "get_ceiling_settings" -> scenario.state[ServerEvents.CEILING_SETTINGS]?.let { pushEvent(ServerEvents.CEILING_SETTINGS, it) }
            "get_reasoning_provider" -> scenario.state[ServerEvents.REASONING_PROVIDER]?.let { pushEvent(ServerEvents.REASONING_PROVIDER, it) }
            "get_manual_status" -> synchronized(lock) {
                currentManualStatus()?.let { pushEvent(ServerEvents.MANUAL_STATUS, it) }
            }
            "land_all" -> synchronized(lock) {
                reply(event, null)
                // Every drone lands where it is now, and stays there.
                currentFleetStatus()?.let { status -> forEachDrone(status) { id, drone -> landedDrones.getOrPut(id) { onTheGround(drone) } } }
                allLanded = true
                currentFleetStatus()?.let { pushEvent(ServerEvents.FLEET_STATUS, it) }
                currentManualStatus()?.let { pushEvent(ServerEvents.MANUAL_STATUS, it) }
                acknowledge(event, DemoText.LAND_ALL_ACKNOWLEDGED)
            }
            "drone_command" -> {
                val droneId = droneIdOf(payload) ?: return
                if (commandOf(payload) != "land") return // The phone sends only land; a real ground refuses the rest.
                synchronized(lock) {
                    reply(event, droneId)
                    // The drone lands where it is now: later replayed statuses no longer move it.
                    currentFleetStatus()?.let { status -> droneOf(status, droneId) }?.let { landedDrones.getOrPut(droneId) { onTheGround(it) } }
                    resumedDrones -= droneId
                    currentFleetStatus()?.let { pushEvent(ServerEvents.FLEET_STATUS, it) }
                    currentManualStatus()?.let { pushEvent(ServerEvents.MANUAL_STATUS, it) }
                    acknowledge(event, DemoText.landAcknowledged(droneId))
                }
            }
            "manual_resume_search" -> {
                val droneId = droneIdOf(payload) ?: return
                synchronized(lock) {
                    if (!isHeld(droneId)) {
                        // As the real ground answers a drone that is not paused (manual_ack.json resume_search_not_manual).
                        pushEvent(ServerEvents.MANUAL_ACK, notManual(droneId))
                        return
                    }
                    reply(event, droneId)
                    resumedDrones += droneId
                    currentFleetStatus()?.let { pushEvent(ServerEvents.FLEET_STATUS, it) }
                    currentManualStatus()?.let { pushEvent(ServerEvents.MANUAL_STATUS, it) }
                    acknowledge(event, DemoText.resumeAcknowledged(droneId))
                }
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

    /** True when [id] hovers paused (held, or under the laptop's control) and was not landed or resumed here. Caller holds [lock]. */
    private fun isHeld(id: String): Boolean {
        if (allLanded || id in landedDrones || id in resumedDrones) return false
        val drone = latestFleetStatus?.let { droneOf(it, id) } ?: return false
        val mode = (drone["control_mode"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        return mode == "held" || mode == "manual"
    }

    private fun notManual(droneId: String): JsonObject = buildJsonObject {
        put("action", JsonPrimitive("resume_search"))
        put("detail", JsonPrimitive("$droneId is not under manual control"))
        put("drone_id", JsonPrimitive(droneId))
        put("ok", JsonPrimitive(false))
        put("reason", JsonPrimitive("not_manual"))
    }

    /** The fleet status to show now. Caller holds [lock]. */
    private fun currentFleetStatus(): JsonObject? {
        val base = latestFleetStatus ?: scenario.steps.firstOrNull { it.event == ServerEvents.FLEET_STATUS }?.payload
            ?: return scenario.landedFleetStatus
        return withOperatorChanges(base)
    }

    /** [status] with the operator's demo commands applied: landed drones on the ground where they landed, resumed drones flying their plan. Caller holds [lock]. */
    private fun withOperatorChanges(status: JsonObject): JsonObject {
        if (landedDrones.isEmpty() && resumedDrones.isEmpty() && !allLanded) return status
        var out = mapDrones(status) { id, drone ->
            when {
                id in landedDrones -> landedDrones.getValue(id)
                allLanded -> onTheGround(drone)
                id in resumedDrones -> resumed(drone)
                else -> drone
            }
        }
        val controlled = (out["manual_controlled"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        if (controlled != null && (allLanded || controlled in landedDrones || controlled in resumedDrones)) {
            out = JsonObject(out + ("manual_controlled" to JsonNull))
        }
        return out
    }

    /** The manual status to show now. Caller holds [lock]. */
    private fun currentManualStatus(): JsonObject? = latestManualStatus?.let { manualWithOperatorChanges(it) }

    /** [status] (a `manual_status`) with the operator's demo commands applied. Caller holds [lock]. */
    private fun manualWithOperatorChanges(status: JsonObject): JsonObject {
        if (landedDrones.isEmpty() && resumedDrones.isEmpty() && !allLanded) return status
        val changed: (String) -> Boolean = { id -> allLanded || id in landedDrones || id in resumedDrones }
        var out = mapDrones(status) { id, drone -> if (changed(id)) released(drone) else drone }
        val controlled = (out["controlled"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        if (controlled != null && changed(controlled)) out = JsonObject(out + ("controlled" to JsonNull))
        return out
    }

    private fun mapDrones(status: JsonObject, change: (String, JsonObject) -> JsonObject): JsonObject {
        val drones = status["drones"] as? JsonObject ?: return status
        val next = JsonObject(drones.mapValues { (id, d) -> (d as? JsonObject)?.let { change(id, it) } ?: d })
        return JsonObject(status + ("drones" to next))
    }

    private fun forEachDrone(status: JsonObject, action: (String, JsonObject) -> Unit) {
        val drones = status["drones"] as? JsonObject ?: return
        for ((id, d) in drones) (d as? JsonObject)?.let { action(id, it) }
    }

    private fun droneOf(status: JsonObject, id: String): JsonObject? = (status["drones"] as? JsonObject)?.get(id) as? JsonObject

    /** A drone entry that has landed: not flying, at height 0, no longer held. */
    private fun onTheGround(drone: JsonObject): JsonObject = JsonObject(
        released(drone) + mapOf("is_flying" to JsonPrimitive(false), "height_cm" to JsonPrimitive(0)),
    )

    /** A drone entry flying its plan again after Resume search. */
    private fun resumed(drone: JsonObject): JsonObject = released(drone)

    /** A drone entry no longer paused: autonomous control and no orphan countdown (only the keys it carries). */
    private fun released(drone: JsonObject): JsonObject {
        val changes = buildMap<String, JsonElement> {
            if (drone.containsKey("control_mode")) put("control_mode", JsonPrimitive("autonomous"))
            if (drone.containsKey("edge_control_mode")) put("edge_control_mode", JsonPrimitive("autonomous"))
            if (drone.containsKey("orphan_land_in_s")) put("orphan_land_in_s", JsonNull)
        }
        return if (changes.isEmpty()) drone else JsonObject(drone + changes)
    }

    private fun droneIdOf(payload: JsonElement?): String? =
        ((payload as? JsonObject)?.get("drone_id") as? JsonPrimitive)?.takeIf { it.isString }?.content?.takeIf { it.isNotEmpty() }

    private fun commandOf(payload: JsonElement?): String? =
        ((payload as? JsonObject)?.get("command") as? JsonPrimitive)?.takeIf { it.isString }?.content
}
