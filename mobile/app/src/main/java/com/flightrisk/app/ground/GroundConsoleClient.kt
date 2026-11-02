package com.flightrisk.app.ground

import com.flightrisk.contract.CeilingSettings
import com.flightrisk.contract.ClientCommand
import com.flightrisk.contract.DetectionProfiles
import com.flightrisk.contract.DroneBoard
import com.flightrisk.contract.DroneStateReducer
import com.flightrisk.contract.EngineIoType
import com.flightrisk.contract.FleetPlan
import com.flightrisk.contract.FleetStatus
import com.flightrisk.contract.Frame
import com.flightrisk.contract.ManualReply
import com.flightrisk.contract.ManualStatus
import com.flightrisk.contract.ReasoningProvider
import com.flightrisk.contract.ServerEvent
import com.flightrisk.contract.ServerEvents
import com.flightrisk.contract.ServerMessage
import com.flightrisk.contract.SocketIoFrames
import com.flightrisk.contract.SocketIoPacket
import com.flightrisk.contract.SocketIoType
import com.flightrisk.contract.Status
import com.flightrisk.contract.StreamVideoState
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.selects.select
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.longOrNull
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.random.Random

/**
 * The phone's Socket.IO client for one FlightRisk dashboard, as
 * docs/mobile-contract.md describes.
 *
 * - [start] runs a connection loop until [shutdown]. Network loss leads to
 *   [ConnectionStatus.Reconnecting] with bounded exponential backoff and
 *   jitter; the backoff resets only after a session stayed up for at least
 *   one ping interval. A refused key leads to [ConnectionStatus.Failed] and
 *   no retry loop: the client waits for [retryNow] or [updateApiKey] (or a
 *   slow retry when [GroundClientConfig.authRetryDelayMs] is set).
 * - Whole-state events update [board] (through the contract's
 *   [DroneStateReducer], in arrival order) and [snapshot]. Every decoded
 *   event except video goes to [events], in order, through a bounded queue:
 *   when the app stops collecting and the queue fills, the session is dropped
 *   with [DropReason.ConsumerStalled] rather than losing events silently.
 * - Video `frame` events take their own path: [frames] keeps only the latest
 *   frame per drone, for a bounded number of drones.
 * - The real server runs its connect handler, and so sends `status`, before
 *   it acknowledges the namespace connect. Such early events are held
 *   (bounded by count and bytes) and delivered right after
 *   [GroundEvent.SessionStarted], in order; they are dropped if the connect
 *   is refused.
 * - After every connect and reconnect [events] delivers
 *   [GroundEvent.SessionStarted], then the held events, and the client sends
 *   the five state requests itself. When a connected session ends, [events]
 *   delivers [GroundEvent.SessionEnded] with the reason.
 * - [send] accepts only a [ClientCommand] (the contract's ten events) that
 *   the [EmitPolicy] allows; the policy can only narrow the set and never
 *   blocks `land_all`. A command that cannot be written now returns
 *   [SendResult.NotSent] and is never queued or retried.
 * - The API key goes only into the Socket.IO connect frame and the REST
 *   Bearer header. Log lines and every surfaced text are redacted first and
 *   truncated after.
 * - The loop and its timers run in [scope]. Call [shutdown] (or cancel the
 *   scope) when the client is no longer needed; dropping the reference alone
 *   leaves the connection open.
 * - [scope] may run on Android's main thread (for example `viewModelScope`),
 *   and every public function may be called from it: socket IO always runs
 *   on the port's IO dispatcher, and closing a connection never writes.
 * - Untrusted text is checked with [JsonGuard] before it is parsed, and any
 *   error inside a session (including a `StackOverflowError` or an
 *   `OutOfMemoryError`) ends only that session, which then reconnects with
 *   backoff.
 * - After every connect the client itself sends the five state requests,
 *   right after the held early events are delivered.
 */
class GroundConsoleClient(
    val endpoint: GroundEndpoint,
    apiKey: DashboardApiKey?,
    private val scope: CoroutineScope,
    private val port: SocketIoPort = WebSocketSocketIoPort(),
    private val config: GroundClientConfig = GroundClientConfig(),
    private val emitPolicy: EmitPolicy = EmitPolicy.CONTRACT,
    private val clock: GroundClock = GroundClock.SYSTEM,
    private val random: () -> Double = { Random.nextDouble() },
    private val restClientFactory: (GroundEndpoint, DashboardApiKey?) -> DashboardRestClient =
        { e, k -> DashboardRestClient(e, k) },
    private val logSink: GroundLog = GroundLog.NONE,
    private val extraSecrets: () -> List<String> = { emptyList() },
) {
    private val _connection = MutableStateFlow<ConnectionStatus>(ConnectionStatus.Idle)
    private val _board = MutableStateFlow(DroneStateReducer.INITIAL)
    private val _snapshot = MutableStateFlow(GroundSnapshot())
    private val _frames = MutableStateFlow<Map<String, Frame>>(emptyMap())

    /** The connection state. */
    val connection: StateFlow<ConnectionStatus> = _connection.asStateFlow()

    /** The drone cards, built from `fleet_status` and `manual_status` in arrival order; stale after a drop. */
    val board: StateFlow<DroneBoard> = _board.asStateFlow()

    /** The latest `status`, `detection_profiles`, `ceiling_settings`, `reasoning_provider`, `fleet_plan` and video state. */
    val snapshot: StateFlow<GroundSnapshot> = _snapshot.asStateFlow()

    /**
     * The latest video frame per drone id (the empty string for a frame
     * without one), at most [GroundClientConfig.maxVideoDrones] entries.
     * Cleared when the link drops.
     */
    val frames: StateFlow<Map<String, Frame>> = _frames.asStateFlow()

    private class Queued(val event: GroundEvent, val size: Long)

    private val channel = Channel<Queued>(Channel.UNLIMITED)
    private val queuedCount = AtomicInteger(0)
    private val queuedBytes = AtomicLong(0)

    /**
     * Session starts and every decoded server event except video, in
     * arrival order. Collect it from exactly one place, for the client's
     * whole life.
     */
    val events: Flow<GroundEvent> = channel.receiveAsFlow().map { q ->
        queuedCount.decrementAndGet()
        queuedBytes.addAndGet(-q.size)
        q.event
    }

    private val discarded = AtomicLong(0)

    /** Video frames discarded (too large, before the connect acknowledgement, or undecodable). */
    val framesDiscarded: Long get() = discarded.get()

    private enum class Phase { AWAITING_OPEN, AWAITING_ACK, CONNECTED, CLOSED }

    private class Session(val conn: SocketIoConnection, val startedAt: Long) {
        @Volatile var phase = Phase.AWAITING_OPEN
        @Volatile var openedAt = 0L
        @Volatile var lastPingAt = 0L
        @Volatile var connectedAt: Long? = null
        @Volatile var dropReason: DropReason? = null
        var pingIntervalMs = 0L
        var pingTimeoutMs = 0L
        @Volatile var pingWindowMs = 0L
        var watchdog: Job? = null
        val held = ArrayList<Pair<ServerEvent, Long>>()
        var heldBytes = 0L
        @Volatile var lostEvent: String? = null
    }

    private sealed interface Outcome {
        data object Stopped : Outcome
        data class Rejected(val message: String?) : Outcome
        data class NotAllowed(val detail: String) : Outcome
        data class Dropped(val reason: DropReason, val upMs: Long?, val pingIntervalMs: Long) : Outcome
    }

    private val lock = Any()
    private val writeMutex = Mutex()
    private val wake = Channel<Unit>(Channel.CONFLATED)

    @Volatile private var apiKey: DashboardApiKey? = apiKey
    @Volatile private var secrets: List<String> = listOfNotNull(apiKey?.secret)
    @Volatile private var session: Session? = null
    @Volatile private var started = false
    @Volatile private var stopped = false
    private var runJob: Job? = null
    private var connectCount = 0

    /** Starts the connection loop. Calling it again, or after [shutdown], does nothing. */
    fun start() {
        synchronized(lock) {
            if (started || stopped) return
            started = true
            log(GroundLog.Level.INFO, "starting the connection to $endpoint")
            runJob = scope.launch { runLoop() }
        }
    }

    /** Ends a backoff wait or a failed state at once and tries to connect. */
    fun retryNow() {
        wake.trySend(Unit)
    }

    /**
     * Replaces the API key for the next connect. A client waiting after a
     * refused key tries again at once.
     */
    fun updateApiKey(key: DashboardApiKey?) {
        synchronized(lock) {
            apiKey = key
            if (key != null && key.secret !in secrets) secrets = secrets + key.secret
        }
        if (_connection.value is ConnectionStatus.Failed) wake.trySend(Unit)
    }

    /**
     * Sends [command] now, or reports why not. Never queues, never retries.
     * A `land_all` that cannot be sent also cuts any reconnect wait short.
     */
    suspend fun send(command: ClientCommand): SendResult {
        val event = command.event
        if (!emitPolicy.allows(command)) {
            log(GroundLog.Level.WARN, "refused to send $event: the emit policy does not allow it")
            return SendResult.NotSent(event, NotSentReason.BlockedByPolicy)
        }
        if (stopped) return notSent(command, NotSentReason.ShutDown)
        if (!started) return notSent(command, NotSentReason.NotStarted)
        val frame = SocketIoFrames.event(command)
        return writeMutex.withLock {
            val s = session
            when {
                stopped -> notSent(command, NotSentReason.ShutDown)
                s == null || s.phase != Phase.CONNECTED ->
                    notSent(command, NotSentReason.NotConnected(_connection.value))
                else -> try {
                    s.conn.send(frame)
                    log(GroundLog.Level.INFO, "sent $event")
                    SendResult.Sent(event)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Throwable) {
                    val detail = describe(e)
                    s.dropReason = DropReason.TransportError("sending failed: $detail")
                    s.conn.close()
                    notSent(command, NotSentReason.SendFailed(detail))
                }
            }
        }
    }

    /** LAND ALL. Never blocked by the emit policy or by any drone state. */
    suspend fun landAll(): SendResult = send(ClientCommand.LandAll)

    /** Lands one drone (`drone_command {command: "land"}`), in every drone state. */
    suspend fun landDrone(droneId: String): SendResult =
        ClientCommand.landDrone(droneId)?.let { send(it) }
            ?: SendResult.NotSent("drone_command", NotSentReason.InvalidDroneId)

    /**
     * Resumes the search of a held drone nobody controls
     * (`manual_resume_search`). Offer it only when [DroneBoard.canResumeSearch]
     * is true; the ground decides.
     */
    suspend fun resumeHeldDrone(droneId: String): SendResult =
        ClientCommand.resumeHeldDrone(droneId)?.let { send(it) }
            ?: SendResult.NotSent("manual_resume_search", NotSentReason.InvalidDroneId)

    /** Sets one drone's detection profile; an empty [profile] follows the fleet default. */
    suspend fun setDetectionProfile(droneId: String, profile: String): SendResult =
        ClientCommand.setDetectionProfile(droneId, profile)?.let { send(it) }
            ?: SendResult.NotSent("set_detection_profile", NotSentReason.InvalidDroneId)

    /** Sets the fleet's default detection profile; an empty [profile] clears it. */
    suspend fun setFleetDetectionProfile(profile: String): SendResult =
        send(ClientCommand.SetFleetDetectionProfile(profile))

    /** Sends the five state requests again, for a manual refresh. The client already sends them on every connect. */
    suspend fun requestFullState(): List<SendResult> = ClientCommand.STATE_REQUESTS.map { send(it) }

    /**
     * Checks over REST that the ground answers (`/api/health`) and accepts
     * the key (`/api/status`), without touching the socket.
     */
    suspend fun healthProbe(): HealthProbeResult = restClientFactory(endpoint, apiKey).probe()

    /**
     * Leaves the namespace (`41`) when connected, closes the link, stops
     * every timer and publishes [ConnectionStatus.ShutDown]. The client never
     * connects again and [events] completes.
     */
    suspend fun shutdown() {
        val job: Job?
        val first: Boolean
        synchronized(lock) {
            first = !stopped
            stopped = true
            job = runJob
        }
        if (!first) {
            job?.join()
            return
        }
        log(GroundLog.Level.INFO, "shutting down")
        val s = session
        if (s != null && s.phase == Phase.CONNECTED) {
            withTimeoutOrNull(LEAVE_TIMEOUT_MS) {
                writeMutex.withLock {
                    try {
                        s.conn.send(SocketIoFrames.DISCONNECT)
                        s.conn.closeGracefully()
                    } catch (e: CancellationException) {
                        throw e
                    } catch (_: Exception) {
                    }
                }
            }
        }
        s?.conn?.close()
        job?.cancelAndJoin()
        publish(ConnectionStatus.ShutDown)
        channel.close()
    }

    override fun toString(): String = "GroundConsoleClient($endpoint)"

    // Connection loop

    private suspend fun runLoop() {
        var failures = 0
        while (!stopped) {
            publish(ConnectionStatus.Connecting(failures + 1))
            val outcome = try {
                runSession()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Throwable) {
                Outcome.Dropped(reasonFor(e), null, 0)
            }
            if (stopped) break
            when (outcome) {
                Outcome.Stopped -> break
                is Outcome.Rejected -> {
                    failures = 0
                    val retry = config.authRetryDelayMs
                    publish(ConnectionStatus.Failed(FailureReason.AuthRejected(outcome.message, retry)))
                    pause(retry)
                }
                is Outcome.NotAllowed -> {
                    failures = 0
                    publish(ConnectionStatus.Failed(FailureReason.AddressNotAllowed(outcome.detail)))
                    pause(null)
                }
                is Outcome.Dropped -> {
                    val up = outcome.upMs
                    // The server chooses pingInterval, so a fixed minimum keeps a flapping
                    // server with a tiny interval from resetting the backoff.
                    if (up != null && up >= maxOf(outcome.pingIntervalMs, config.minStableMs)) failures = 0
                    failures++
                    val delayMs = config.backoff.delayMs(failures, random())
                    publish(ConnectionStatus.Reconnecting(failures, delayMs, outcome.reason))
                    pause(delayMs)
                }
            }
        }
    }

    /** Waits [ms], or until [retryNow], a refused-key [updateApiKey] or a disconnected LAND ALL; null waits for those only. */
    private suspend fun pause(ms: Long?) {
        if (stopped) return
        coroutineScope {
            val sleeper = ms?.let { async { clock.sleep(it) } }
            select<Unit> {
                wake.onReceive { }
                sleeper?.onAwait { }
            }
            sleeper?.cancel()
        }
    }

    private suspend fun runSession(): Outcome {
        val startedAt = clock.nowMs()
        val conn = try {
            openWithDeadline()
        } catch (e: CancellationException) {
            throw e
        } catch (e: AddressNotAllowedException) {
            val detail = redact(e.message)
            log(GroundLog.Level.ERROR, "not connecting: $detail")
            return Outcome.NotAllowed(detail)
        } catch (e: Throwable) {
            val reason = reasonFor(e)
            log(GroundLog.Level.WARN, "could not connect: $reason")
            return Outcome.Dropped(reason, null, 0)
        } ?: run {
            log(GroundLog.Level.WARN, "could not connect: ${DropReason.HandshakeTimeout}")
            return Outcome.Dropped(DropReason.HandshakeTimeout, null, 0)
        }
        val s = Session(conn, startedAt)
        synchronized(lock) {
            if (stopped) {
                conn.close()
                return Outcome.Stopped
            }
            session = s
        }
        restartWatchdog(s)
        try {
            while (true) {
                val text = conn.receive()
                val outcome = handleFrame(s, text)
                if (outcome != null) return outcome
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            // Every failure, an Error included, ends this session only; the loop reconnects.
            if (stopped) return Outcome.Stopped
            return dropped(s, s.dropReason ?: reasonFor(e))
        } finally {
            s.watchdog?.cancel()
            s.phase = Phase.CLOSED
            synchronized(lock) { if (session === s) session = null }
            conn.close()
            s.held.clear()
            _frames.value = emptyMap()
            if (s.connectedAt != null) _board.update { DroneStateReducer.disconnected(it) }
        }
    }

    /**
     * Opens the connection under the handshake timeout, on the injected
     * clock; the attempt is cancelled (and its socket closed) when the
     * timeout passes first. Null means the timeout passed.
     */
    private suspend fun openWithDeadline(): SocketIoConnection? = coroutineScope {
        val attempt = async { port.open(endpoint, allSecrets()) }
        val timer = async {
            clock.sleep(config.handshakeTimeoutMs)
            null
        }
        val result = select<SocketIoConnection?> {
            attempt.onAwait { it }
            timer.onAwait { it }
        }
        timer.cancel()
        if (result == null) {
            attempt.cancel()
            // An attempt that finished just as the timer fired must not leak its socket.
            try {
                attempt.await().close()
            } catch (_: Throwable) {
            }
        }
        result
    }

    private fun dropped(s: Session, reason: DropReason): Outcome {
        val upMs = s.connectedAt?.let { clock.nowMs() - it }
        if (s.connectedAt != null) markSessionEnded(s, reason)
        log(GroundLog.Level.WARN, "connection lost: $reason")
        return Outcome.Dropped(reason, upMs, s.pingIntervalMs)
    }

    // Frames

    private suspend fun handleFrame(s: Session, text: String): Outcome? {
        if (text.length > config.maxMessageChars) {
            return dropped(s, DropReason.MessageTooLarge(config.maxMessageChars.toLong()))
        }
        JsonGuard.check(text)?.let { problem ->
            return dropped(s, DropReason.ProtocolError(problem))
        }
        val frame = SocketIoFrames.parse(text)
        if (frame == null) {
            if (s.phase == Phase.AWAITING_OPEN) {
                return dropped(s, DropReason.ProtocolError("the first frame was not the Engine.IO open packet"))
            }
            log(GroundLog.Level.WARN, "ignored a malformed frame")
            return null
        }
        if (frame.type == EngineIoType.OPEN) {
            if (s.phase != Phase.AWAITING_OPEN) {
                log(GroundLog.Level.WARN, "ignored a second Engine.IO open packet")
                return null
            }
            val open = frame.data as? JsonObject
            val interval = open.millis("pingInterval", DEFAULT_PING_INTERVAL_MS)
            val timeout = open.millis("pingTimeout", DEFAULT_PING_TIMEOUT_MS)
            val now = clock.nowMs()
            s.pingIntervalMs = interval
            s.pingTimeoutMs = timeout
            s.pingWindowMs = minOf(interval + timeout, config.maxPingWindowMs)
            s.openedAt = now
            s.lastPingAt = now
            s.phase = Phase.AWAITING_ACK
            restartWatchdog(s)
            sendRaw(s, SocketIoFrames.connect(apiKey?.secret))
            log(GroundLog.Level.INFO, "Engine.IO open (pingInterval $interval ms, pingTimeout $timeout ms); sent the namespace connect")
            return null
        }
        if (s.phase == Phase.AWAITING_OPEN) {
            return dropped(s, DropReason.ProtocolError("the first frame was not the Engine.IO open packet"))
        }
        return when (frame.type) {
            EngineIoType.PING -> {
                s.lastPingAt = clock.nowMs()
                sendRaw(s, SocketIoFrames.PONG)
                null
            }
            EngineIoType.CLOSE -> dropped(s, DropReason.ServerClosed("Engine.IO close"))
            EngineIoType.MESSAGE -> frame.socketIo?.let { handleSocketIo(s, it, text.length.toLong()) }
            else -> null
        }
    }

    private suspend fun handleSocketIo(s: Session, packet: SocketIoPacket, size: Long): Outcome? {
        if (packet.namespace != "/") {
            log(GroundLog.Level.DEBUG, "ignored a packet for another namespace")
            return null
        }
        return when (packet.type) {
            SocketIoType.CONNECT -> onConnectAck(s)
            SocketIoType.CONNECT_ERROR -> {
                val message = ((packet.data as? JsonObject)?.get("message") as? JsonPrimitive)
                    ?.takeIf { it.isString }?.content?.let { redact(it) }
                s.held.clear()
                s.heldBytes = 0
                log(GroundLog.Level.ERROR, "the ground station refused the key")
                Outcome.Rejected(message)
            }
            SocketIoType.DISCONNECT -> dropped(s, DropReason.ServerDisconnected)
            SocketIoType.EVENT -> onEvent(s, packet, size)
            else -> null
        }
    }

    private suspend fun onConnectAck(s: Session): Outcome? {
        if (s.phase != Phase.AWAITING_ACK) {
            log(GroundLog.Level.WARN, "ignored an unexpected namespace connect")
            return null
        }
        connectCount++
        val info = SessionInfo(connectCount, s.pingIntervalMs, s.pingTimeoutMs)
        s.phase = Phase.CONNECTED
        s.connectedAt = clock.nowMs()
        restartWatchdog(s)
        while (wake.tryReceive().isSuccess) Unit
        publish(ConnectionStatus.Connected(info))
        enqueue(s, GroundEvent.SessionStarted(info), 0)?.let { return it }
        // python-socketio runs the connect handler before it acknowledges the
        // connect, so `status` arrives before `40{"sid":...}`. Deliver the held
        // events now, after the session is announced, in their original order.
        val held = s.held.toList()
        s.held.clear()
        s.heldBytes = 0
        for ((event, size) in held) {
            deliver(s, event, size)?.let { return it }
        }
        // Rebuild the full state on every connect and reconnect, without waiting for the app.
        for (command in ClientCommand.STATE_REQUESTS) {
            if (emitPolicy.allows(command)) sendRaw(s, SocketIoFrames.event(command))
        }
        log(GroundLog.Level.INFO, "requested the full state")
        return null
    }

    private suspend fun onEvent(s: Session, packet: SocketIoPacket, size: Long): Outcome? {
        packet.ackId?.let { sendRaw(s, "43$it[]") }
        val name = packet.eventName
        if (name == null) {
            log(GroundLog.Level.WARN, "ignored an event without a name")
            return null
        }
        if (name == ServerEvents.FRAME) {
            onVideoFrame(s, packet, size)
            return null
        }
        val event = surfaceable(ServerEvents.decode(name, packet.eventPayload))
        if (event is ServerEvent.Ignored) {
            log(GroundLog.Level.DEBUG, "ignored the event ${redact(name)}")
            return null
        }
        if (event is ServerEvent.Malformed) log(GroundLog.Level.WARN, "received a malformed $name")
        if (s.phase == Phase.CONNECTED) return deliver(s, event, size)
        if (s.held.size + 1 > config.maxEarlyEvents || s.heldBytes + size > config.maxEarlyBytes) {
            return dropped(s, DropReason.EarlyEventOverflow(s.held.size + 1, s.heldBytes + size))
        }
        log(GroundLog.Level.DEBUG, "holding $name until the connect acknowledgement")
        s.held += event to size
        s.heldBytes += size
        return null
    }

    private fun onVideoFrame(s: Session, packet: SocketIoPacket, size: Long) {
        if (s.phase != Phase.CONNECTED || size > config.maxVideoFrameChars) {
            discarded.incrementAndGet()
            return
        }
        val frame = ServerEvents.decode(ServerEvents.FRAME, packet.eventPayload) as? Frame
        if (frame == null) {
            discarded.incrementAndGet()
            return
        }
        val key = frame.droneId ?: ""
        _frames.update { current ->
            val next = LinkedHashMap(current)
            next.remove(key)
            next[key] = frame
            while (next.size > config.maxVideoDrones) next.remove(next.keys.first())
            next
        }
    }

    private fun deliver(s: Session, event: ServerEvent, size: Long): Outcome? {
        when (event) {
            is FleetStatus, is ManualStatus -> _board.update { DroneStateReducer.reduce(it, event) }
            is Status -> _snapshot.update { it.copy(status = event) }
            is DetectionProfiles -> _snapshot.update { it.copy(detectionProfiles = event) }
            is CeilingSettings -> _snapshot.update { it.copy(ceilingSettings = event) }
            is ReasoningProvider -> _snapshot.update { it.copy(reasoningProvider = event) }
            is FleetPlan -> _snapshot.update { it.copy(fleetPlan = event) }
            is StreamVideoState -> _snapshot.update { it.copy(streamVideo = event.enabled) }
            else -> Unit
        }
        return enqueue(s, GroundEvent.Server(event), size)
    }

    private fun enqueue(s: Session, event: GroundEvent, size: Long): Outcome? {
        val count = queuedCount.get()
        val bytes = queuedBytes.get()
        // The last slots are kept free for SessionEnded markers.
        if (count + 1 > config.maxQueuedEvents - MARKER_SLOTS || bytes + size > config.maxQueuedBytes) {
            val lost = (event as? GroundEvent.Server)?.event?.let { it::class.simpleName }
            log(GroundLog.Level.ERROR, "the app is not reading ground events ($count queued); dropping the session")
            val reason = DropReason.ConsumerStalled(count, bytes)
            s.lostEvent = lost
            return dropped(s, reason)
        }
        put(event, size)
        return null
    }

    private fun put(event: GroundEvent, size: Long) {
        queuedCount.incrementAndGet()
        queuedBytes.addAndGet(size)
        channel.trySend(Queued(event, size))
    }

    /**
     * Tells the app, in order with the events, that a connected session
     * ended and why. Uses the reserved slots; when even those are full the
     * connection status still carries the reason.
     */
    private fun markSessionEnded(s: Session, reason: DropReason) {
        if (queuedCount.get() >= config.maxQueuedEvents) return
        put(GroundEvent.SessionEnded(reason, s.lostEvent), 0)
    }

    /**
     * Redacts, then truncates, the server texts the app shows as toasts:
     * `error` and `warning` messages and the detail of a manual reply.
     */
    private fun surfaceable(event: ServerEvent): ServerEvent = when (event) {
        is ServerMessage -> event.copy(message = event.message?.let { redact(it) })
        is ManualReply -> event.copy(detail = event.detail?.let { redact(it) })
        else -> event
    }

    private suspend fun sendRaw(s: Session, frame: String) {
        writeMutex.withLock { s.conn.send(frame) }
    }

    // Dead-link detection

    private fun restartWatchdog(s: Session) {
        s.watchdog?.cancel()
        s.watchdog = scope.launch { watchdog(s) }
    }

    /** Closes the link when the handshake takes too long or the server's pings stop. */
    private suspend fun watchdog(s: Session) {
        while (session === s) {
            val (deadline, reason) = when (s.phase) {
                Phase.AWAITING_OPEN -> (s.startedAt + config.handshakeTimeoutMs) to DropReason.HandshakeTimeout
                Phase.AWAITING_ACK -> {
                    val handshake = s.openedAt + config.handshakeTimeoutMs
                    val ping = s.lastPingAt + s.pingWindowMs
                    if (handshake <= ping) handshake to DropReason.HandshakeTimeout else ping to DropReason.PingTimeout
                }
                Phase.CONNECTED -> (s.lastPingAt + s.pingWindowMs) to DropReason.PingTimeout
                Phase.CLOSED -> return
            }
            val now = clock.nowMs()
            if (now >= deadline) {
                s.dropReason = reason
                log(GroundLog.Level.WARN, "closing the link: $reason")
                s.conn.close()
                return
            }
            clock.sleep(deadline - now)
        }
    }

    // Helpers

    private fun notSent(command: ClientCommand, reason: NotSentReason): SendResult {
        if (command is ClientCommand.LandAll) {
            log(GroundLog.Level.ERROR, "land_all NOT sent: $reason")
            if (reason is NotSentReason.NotConnected) wake.trySend(Unit)
        } else {
            log(GroundLog.Level.WARN, "${command.event} not sent: $reason")
        }
        return SendResult.NotSent(command.event, reason)
    }

    private fun publish(status: ConnectionStatus) {
        _connection.value = status
        log(GroundLog.Level.INFO, "state: $status")
    }

    private fun reasonFor(e: Throwable): DropReason = when (e) {
        is MessageTooLargeException -> DropReason.MessageTooLarge(e.limitBytes)
        is WebSocketClosedException -> DropReason.ServerClosed(describe(e))
        is ProtocolViolationException, is HandshakeRejectedException -> DropReason.ProtocolError(describe(e))
        is Error -> DropReason.InternalError(describe(e))
        else -> DropReason.TransportError(describe(e))
    }

    private fun describe(e: Throwable): String = redact(e.javaClass.simpleName + (e.message?.let { ": $it" } ?: ""))

    private fun allSecrets(): List<String> = secrets + extraSecrets()

    // All secrets are removed before the text is cut.
    private fun redact(text: String?): String = Redact.text(text, allSecrets())

    private fun log(level: GroundLog.Level, message: String) {
        logSink.log(level, redact(message))
    }

    private fun JsonObject?.millis(key: String, default: Long): Long =
        ((this?.get(key) as? JsonPrimitive)?.takeIf { !it.isString }?.longOrNull ?: default)
            .coerceIn(MIN_PING_MS, MAX_PING_MS)

    companion object {
        const val DEFAULT_PING_INTERVAL_MS = 25_000L
        const val DEFAULT_PING_TIMEOUT_MS = 20_000L
        private const val MIN_PING_MS = 1_000L
        private const val MAX_PING_MS = 300_000L
        private const val LEAVE_TIMEOUT_MS = 1_000L

        /** Queue slots kept free for [GroundEvent.SessionEnded]. */
        private const val MARKER_SLOTS = 1
    }
}
