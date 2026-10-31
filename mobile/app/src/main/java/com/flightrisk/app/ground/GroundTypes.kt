package com.flightrisk.app.ground

import com.flightrisk.contract.CeilingSettings
import com.flightrisk.contract.ClientCommand
import com.flightrisk.contract.DetectionProfiles
import com.flightrisk.contract.FleetPlan
import com.flightrisk.contract.ReasoningProvider
import com.flightrisk.contract.ServerEvent
import com.flightrisk.contract.Status
import kotlinx.coroutines.delay
import kotlin.math.min
import kotlin.math.pow

/** What one established session looks like. */
data class SessionInfo(
    /** 1 for the first connect, then one more for every reconnect. */
    val connectCount: Int,
    val pingIntervalMs: Long,
    val pingTimeoutMs: Long,
)

/** Why a connection that was open, or being opened, ended. Every text here is already redacted. */
sealed interface DropReason {
    /** The Engine.IO open packet or the namespace connect acknowledgement did not arrive in time. */
    data object HandshakeTimeout : DropReason

    /** No ping from the server within pingInterval + pingTimeout. */
    data object PingTimeout : DropReason

    /** The server sent an Engine.IO close or a WebSocket close frame. */
    data class ServerClosed(val detail: String) : DropReason

    /** The server sent a Socket.IO disconnect. */
    data object ServerDisconnected : DropReason

    /** A message exceeded the size limit, so the connection was closed without reading it. */
    data class MessageTooLarge(val limitBytes: Long) : DropReason

    /** Too many events, or too many bytes of them, arrived before the connect acknowledgement. */
    data class EarlyEventOverflow(val events: Int, val bytes: Long) : DropReason

    /** The app did not collect [GroundConsoleClient.events] and the bounded queue filled up. */
    data class ConsumerStalled(val queuedEvents: Int, val queuedBytes: Long) : DropReason

    /** The server broke the protocol. */
    data class ProtocolError(val detail: String) : DropReason

    /** The network failed. */
    data class TransportError(val detail: String) : DropReason
}

/** Why the client stopped trying until the user acts. */
sealed interface FailureReason {
    /**
     * The server refused the API key (`connect_error`). [serverMessage] is
     * redacted and truncated. [retryInMs] is null unless a slow retry was
     * configured: the client then waits for [GroundConsoleClient.retryNow]
     * or [GroundConsoleClient.updateApiKey].
     */
    data class AuthRejected(val serverMessage: String?, val retryInMs: Long?) : FailureReason

    /** The host is not on a private network, so the client never connected. */
    data class AddressNotAllowed(val detail: String) : FailureReason
}

/** The client's connection state. */
sealed interface ConnectionStatus {
    /** [GroundConsoleClient.start] has not been called. */
    data object Idle : ConnectionStatus

    /** Opening the WebSocket and waiting for the namespace connect acknowledgement. */
    data class Connecting(val attempt: Int) : ConnectionStatus

    /** The session is up and commands can be sent. */
    data class Connected(val info: SessionInfo) : ConnectionStatus

    /** The link dropped; the next attempt starts after [retryInMs]. */
    data class Reconnecting(val attempt: Int, val retryInMs: Long, val reason: DropReason) : ConnectionStatus

    /** Waiting for the user (a wrong key or a refused address). No retry loop. */
    data class Failed(val reason: FailureReason) : ConnectionStatus

    /** [GroundConsoleClient.shutdown] was called; the client never connects again. */
    data object ShutDown : ConnectionStatus
}

/** The result of a send. Nothing that is not sent now is ever sent later. */
sealed interface SendResult {
    /** The frame was written to the socket. The ground's answer arrives as an event. */
    data class Sent(val event: String) : SendResult

    /** The frame was not sent, for [reason]. */
    data class NotSent(val event: String, val reason: NotSentReason) : SendResult
}

/** Why a command was not sent. */
sealed interface NotSentReason {
    /** The client was never started. */
    data object NotStarted : NotSentReason

    /** The session is not established (connecting, reconnecting or failed); [status] says which. */
    data class NotConnected(val status: ConnectionStatus) : NotSentReason

    /** The client is shut down. */
    data object ShutDown : NotSentReason

    /** Writing to the socket failed; [detail] is redacted. The command must be treated as not sent. */
    data class SendFailed(val detail: String) : NotSentReason

    /** The app's emit policy does not allow this event. */
    data object BlockedByPolicy : NotSentReason

    /** The drone id was empty or blank. */
    data object InvalidDroneId : NotSentReason
}

/** Everything [GroundConsoleClient.events] delivers, in arrival order. */
sealed interface GroundEvent {
    /**
     * A session was established (first connect or reconnect). Request the
     * full state now with [GroundConsoleClient.requestFullState] and treat
     * older state as stale.
     */
    data class SessionStarted(val info: SessionInfo) : GroundEvent

    /** A decoded server event other than a video `frame`. */
    data class Server(val event: ServerEvent) : GroundEvent
}

/** The latest whole-state payloads, kept by the client in arrival order. */
data class GroundSnapshot(
    val status: Status? = null,
    val detectionProfiles: DetectionProfiles? = null,
    val ceilingSettings: CeilingSettings? = null,
    val reasoningProvider: ReasoningProvider? = null,
    val fleetPlan: FleetPlan? = null,
    val streamVideo: Boolean? = null,
)

/**
 * Bounded exponential reconnect delay with jitter: attempt n waits
 * `min(initialMs * 2^(n-1), maxMs)`, reduced by up to [jitterFraction] of
 * itself at random.
 */
data class Backoff(
    val initialMs: Long = 1_000,
    val maxMs: Long = 30_000,
    val jitterFraction: Double = 0.2,
) {
    init {
        require(initialMs > 0 && maxMs >= initialMs) { "bad backoff bounds" }
        require(jitterFraction in 0.0..1.0) { "jitterFraction must be in 0..1" }
    }

    /** The delay before [attempt] (1-based); [random] is in 0..1. */
    fun delayMs(attempt: Int, random: Double): Long {
        val exponent = (attempt - 1).coerceIn(0, 30)
        val base = min(initialMs * 2.0.pow(exponent), maxMs.toDouble())
        val jittered = base * (1 - jitterFraction * random.coerceIn(0.0, 1.0))
        return jittered.toLong().coerceAtLeast(0)
    }
}

/**
 * Which events the app lets the client send. It starts as exactly the
 * contract's allow-list ([ClientCommand.ALLOWED_EVENTS]) and can only be
 * narrowed. `land_all` is always allowed, whatever the policy says.
 */
class EmitPolicy private constructor(private val allowed: Set<String>) {
    /** True when [command] may be sent. `land_all` always may. */
    fun allows(command: ClientCommand): Boolean =
        command is ClientCommand.LandAll || (command.event in allowed && ClientCommand.isAllowed(command.event))

    /** The events this policy allows, always including `land_all`. */
    val allowedEvents: Set<String> get() = allowed + LAND_ALL

    /** A policy that allows only events in both this policy and [events]. Names outside the contract are ignored. */
    fun narrowedTo(events: Set<String>): EmitPolicy = EmitPolicy(allowed intersect events)

    /** A policy that allows this policy's events except [events]. `land_all` stays allowed. */
    fun without(vararg events: String): EmitPolicy = EmitPolicy(allowed - events.toSet())

    override fun toString(): String = "EmitPolicy(${allowedEvents.sorted()})"

    companion object {
        const val LAND_ALL = "land_all"

        /** Exactly the contract's allow-list. */
        val CONTRACT: EmitPolicy = EmitPolicy(ClientCommand.ALLOWED_EVENTS)
    }
}

/** Monotonic time and sleeping, replaced by a fake clock in tests. */
interface GroundClock {
    fun nowMs(): Long
    suspend fun sleep(ms: Long)

    companion object {
        val SYSTEM: GroundClock = object : GroundClock {
            override fun nowMs(): Long = System.nanoTime() / 1_000_000
            override suspend fun sleep(ms: Long) = delay(ms)
        }
    }
}

/** Where the client's log lines go. Every message is redacted before it gets here. */
fun interface GroundLog {
    enum class Level { DEBUG, INFO, WARN, ERROR }

    fun log(level: Level, message: String)

    companion object {
        val NONE: GroundLog = GroundLog { _, _ -> }
    }
}

/** Limits and timings of a [GroundConsoleClient]. */
data class GroundClientConfig(
    val backoff: Backoff = Backoff(),
    /** How long the Engine.IO open and the connect acknowledgement may each take. */
    val handshakeTimeoutMs: Long = 10_000,
    /** Upper bound for pingInterval + pingTimeout, so a hostile open packet cannot disable dead-link detection. */
    val maxPingWindowMs: Long = 120_000,
    /** After a refused key, retry this slowly; null (the default) waits for the user. */
    val authRetryDelayMs: Long? = null,
    /** Frames longer than this (in characters) are refused even if the transport let them through. */
    val maxMessageChars: Int = WebSocketSocketIoPort.DEFAULT_MAX_MESSAGE_BYTES.toInt(),
    /** The most events held between the namespace connect and its acknowledgement. */
    val maxEarlyEvents: Int = 32,
    /** The most bytes of events held between the namespace connect and its acknowledgement. */
    val maxEarlyBytes: Long = 2L * 1024 * 1024,
    /** The most undelivered events in [GroundConsoleClient.events] before the session is dropped. */
    val maxQueuedEvents: Int = 512,
    /** The most undelivered bytes in [GroundConsoleClient.events] before the session is dropped. */
    val maxQueuedBytes: Long = 16L * 1024 * 1024,
    /** Video frames larger than this are discarded (counted in [GroundConsoleClient.framesDiscarded]). */
    val maxVideoFrameChars: Int = 1024 * 1024,
    /** The most drones kept on the video path; the least recently updated is evicted. */
    val maxVideoDrones: Int = 8,
)
