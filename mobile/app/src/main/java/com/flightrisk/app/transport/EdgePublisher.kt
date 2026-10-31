package com.flightrisk.app.transport

import com.flightrisk.app.ground.AddressNotAllowedException
import com.flightrisk.app.ground.Backoff
import com.flightrisk.app.ground.GroundClock
import com.flightrisk.app.ground.GroundEndpoint
import com.flightrisk.app.ground.GroundLog
import com.flightrisk.app.ground.Redact
import com.flightrisk.app.ground.SocketIoConnection
import com.flightrisk.app.ground.SocketIoPort
import com.flightrisk.app.ground.WebSocketClosedException
import com.flightrisk.app.pipeline.DetectionPublisher
import com.flightrisk.app.pipeline.PublishedFrame
import com.flightrisk.contract.DetectionsMessage
import com.flightrisk.contract.DetectionsWire
import com.flightrisk.contract.EdgeAction
import com.flightrisk.contract.EdgeLink
import com.flightrisk.contract.WireDetection
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.random.Random

/** The state of the link to the ground station's edge port. */
sealed interface EdgeLinkStatus {
    /** Not running. */
    data object Off : EdgeLinkStatus

    /** Opening the connection; [attempt] is 1 for the first try. */
    data class Connecting(val attempt: Int) : EdgeLinkStatus

    /** The connection is up and detections are sent. */
    data object Connected : EdgeLinkStatus

    /** The link dropped; the next try starts after [retryInMs]. [reason] is already redacted. */
    data class Reconnecting(val attempt: Int, val retryInMs: Long, val reason: String) : EdgeLinkStatus

    /** The ground closed the link with 4001: the edge token is wrong or missing. Not retried. */
    data object TokenRejected : EdgeLinkStatus

    /** The host is not on a private network, so nothing was sent. Not retried. */
    data class AddressRefused(val reason: String) : EdgeLinkStatus
}

/** Counters since [EdgePublisher.start]. Nothing here holds image or token data. */
data class EdgeStats(
    val framesSent: Long = 0,
    /** Frames skipped because the previous one was still being written. */
    val framesDroppedBusy: Long = 0,
    /** Frames skipped because they came faster than the rate limit. */
    val framesDroppedRate: Long = 0,
    /** Frames that stayed over 1 MiB even without crops and thumbnail. */
    val framesDroppedTooLarge: Long = 0,
    /** Frames the encoder refused (for example a timestamp that is not a number). */
    val framesDroppedInvalid: Long = 0,
    /** Detections left out because a box or confidence was not a finite number. */
    val detectionsDropped: Long = 0,
    /** Crops removed to fit the 1 MiB limit. */
    val cropsDropped: Long = 0,
)

/** Limits and timings of an [EdgePublisher]. */
data class EdgePublisherConfig(
    /** The shortest time between two sent frames. 500 ms is at most two frames a second. */
    val minFrameIntervalMs: Long = 500,
    val backoff: Backoff = Backoff(),
    /** The backoff starts again from the beginning only after a link stayed up this long. */
    val minStableMs: Long = 30_000,
    /** A write that takes longer than this ends the session. */
    val sendTimeoutMs: Long = 10_000,
    /** At most this many detections go into one message; the rest are left out. */
    val maxDetectionsPerFrame: Int = 50,
)

/**
 * Sends the phone's person detections to a ground station's edge port, as
 * `docs/mobile-contract.md` ("Phone as an edge") describes.
 *
 * - The only things written on the link are the `auth` message with the edge
 *   token (when one is saved) and `detections` messages built by the
 *   contract's [DetectionsWire]; [EdgeLink.OUTBOUND_TYPES] is the whole set.
 *   Nothing is ever written in answer to the ground, and no ground message
 *   is acted on except `stream_video`.
 * - The token is read when a connection is made and goes only into the first
 *   frame. It is not logged, not part of any status, and not in [toString].
 *   Text that comes from the network or from an exception is redacted with
 *   the token first.
 * - Frames are offered through [publish]. A frame is dropped, never queued,
 *   when the previous one is still being written or the rate limit says it is
 *   too soon, so memory use is bounded by one pending message.
 * - A ground that closes the link with 4001 means the token is wrong: the
 *   publisher stops and does not retry. Any other failure reconnects with
 *   bounded exponential backoff that restarts only after a stable link.
 * - Thumbnails are sent only after the ground sends `stream_video` with
 *   `enabled: true`; the default is detections only.
 *
 * Everything runs in [scope]; [start] and [stop] never block.
 */
class EdgePublisher(
    val endpoint: GroundEndpoint,
    private val tokenProvider: suspend () -> String?,
    private val scope: CoroutineScope,
    private val port: SocketIoPort,
    private val config: EdgePublisherConfig = EdgePublisherConfig(),
    private val clock: GroundClock = GroundClock.SYSTEM,
    private val random: () -> Double = { Random.nextDouble() },
    private val logSink: GroundLog = GroundLog.NONE,
    private val faceMatchingEnabled: () -> Boolean = { false },
) : DetectionPublisher {

    private val _status = MutableStateFlow<EdgeLinkStatus>(EdgeLinkStatus.Off)

    /** The link state. */
    val status: StateFlow<EdgeLinkStatus> = _status

    private val lock = Any()
    private var job: Job? = null

    @Volatile private var outbox: Channel<String>? = null
    @Volatile private var live = false
    @Volatile private var streamVideo = false
    @Volatile private var activeSecrets: List<String> = emptyList()
    private val busy = AtomicBoolean(false)
    @Volatile private var lastAcceptedAt = Long.MIN_VALUE / 2

    private val sent = AtomicLong()
    private val droppedBusy = AtomicLong()
    private val droppedRate = AtomicLong()
    private val droppedTooLarge = AtomicLong()
    private val droppedInvalid = AtomicLong()
    private val detectionsDropped = AtomicLong()
    private val cropsDropped = AtomicLong()

    /** A snapshot of the counters. */
    fun stats(): EdgeStats = EdgeStats(
        framesSent = sent.get(),
        framesDroppedBusy = droppedBusy.get(),
        framesDroppedRate = droppedRate.get(),
        framesDroppedTooLarge = droppedTooLarge.get(),
        framesDroppedInvalid = droppedInvalid.get(),
        detectionsDropped = detectionsDropped.get(),
        cropsDropped = cropsDropped.get(),
    )

    /** True while thumbnails are on (the ground sent `stream_video` enabled). */
    val thumbnailsEnabled: Boolean get() = streamVideo

    /** Starts the connection loop. Does nothing while it already runs. */
    fun start() {
        synchronized(lock) {
            if (job?.isActive == true) return
            log(GroundLog.Level.INFO, "starting the edge link to $endpoint")
            job = scope.launch { runLoop() }
        }
    }

    /** Stops the loop and closes the connection at once without writing. Safe on any thread. */
    fun stop() {
        synchronized(lock) {
            job?.cancel()
            job = null
            live = false
            outbox = null
            streamVideo = false
            busy.set(false)
            _status.value = EdgeLinkStatus.Off
        }
    }

    override fun wantsFrame(): Boolean =
        live && !busy.get() && clock.nowMs() - lastAcceptedAt >= config.minFrameIntervalMs

    override fun publish(frame: PublishedFrame) {
        val channel = outbox
        if (!live || channel == null) return
        val now = clock.nowMs()
        if (now - lastAcceptedAt < config.minFrameIntervalMs) {
            droppedRate.incrementAndGet()
            return
        }
        if (!busy.compareAndSet(false, true)) {
            droppedBusy.incrementAndGet()
            return
        }
        lastAcceptedAt = now
        val message = try {
            encode(frame)
        } catch (e: Exception) {
            null
        }
        if (message == null || !channel.trySend(message).isSuccess) {
            busy.set(false)
        }
    }

    /** The encoded message, or null when the frame is dropped (and counted). */
    private fun encode(frame: PublishedFrame): String? {
        val withVideo = streamVideo
        val faceOn = faceMatchingEnabled()
        val capped = frame.detections.take(config.maxDetectionsPerFrame)
        detectionsDropped.addAndGet((frame.detections.size - capped.size).toLong())
        val message = DetectionsMessage(
            timestamp = frame.timestampSeconds,
            frameId = frame.frameId,
            frameWidth = frame.frameWidth,
            frameHeight = frame.frameHeight,
            thumbnail = if (withVideo) frame.thumbnailJpeg()?.let(DetectionsWire::base64Jpeg) else null,
            detections = capped.map { d ->
                WireDetection(
                    bbox = d.bbox,
                    confidence = d.confidence,
                    reidEmbedding = d.reidEmbedding,
                    faceEmbedding = if (faceOn) d.faceEmbedding else null,
                    crop = d.cropJpeg()?.let(DetectionsWire::base64Jpeg),
                )
            },
        )
        return when (val result = DetectionsWire.encode(message, faceMatchingEnabled = faceOn, streamVideo = withVideo)) {
            is DetectionsWire.EncodeResult.Ok -> {
                detectionsDropped.addAndGet(result.droppedDetections.toLong())
                cropsDropped.addAndGet(result.droppedCrops.toLong())
                result.json
            }
            is DetectionsWire.EncodeResult.TooLarge -> {
                droppedTooLarge.incrementAndGet()
                null
            }
            is DetectionsWire.EncodeResult.Invalid -> {
                droppedInvalid.incrementAndGet()
                null
            }
        }
    }

    private sealed interface Outcome {
        data object TokenRejected : Outcome
        data class AddressRefused(val reason: String) : Outcome
        data class Dropped(val reason: String, val upMs: Long?) : Outcome
    }

    private suspend fun runLoop() {
        var failures = 0
        while (true) {
            _status.value = EdgeLinkStatus.Connecting(failures + 1)
            when (val outcome = runSession()) {
                Outcome.TokenRejected -> {
                    log(GroundLog.Level.WARN, "the ground rejected the edge token; not retrying")
                    _status.value = EdgeLinkStatus.TokenRejected
                    return
                }
                is Outcome.AddressRefused -> {
                    log(GroundLog.Level.WARN, "address refused: ${outcome.reason}")
                    _status.value = EdgeLinkStatus.AddressRefused(outcome.reason)
                    return
                }
                is Outcome.Dropped -> {
                    if (outcome.upMs != null && outcome.upMs >= config.minStableMs) failures = 0
                    failures++
                    val delayMs = config.backoff.delayMs(failures, random())
                    log(GroundLog.Level.INFO, "the edge link dropped (${outcome.reason}); retrying in $delayMs ms")
                    _status.value = EdgeLinkStatus.Reconnecting(failures, delayMs, outcome.reason)
                    clock.sleep(delayMs)
                }
            }
        }
    }

    private suspend fun runSession(): Outcome {
        val token = try {
            tokenProvider()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            null
        }
        activeSecrets = listOfNotNull(token)
        val connection: SocketIoConnection = try {
            port.open(endpoint, activeSecrets)
        } catch (e: CancellationException) {
            throw e
        } catch (e: AddressNotAllowedException) {
            return Outcome.AddressRefused(describe(e))
        } catch (e: Exception) {
            return Outcome.Dropped(describe(e), null)
        }
        val startedAt = clock.nowMs()
        try {
            if (token != null) {
                if (!sendWithTimeout(connection, EdgeLink.authMessage(token))) throw IOException("sending the first message timed out")
            }
            val channel = Channel<String>(1)
            busy.set(false)
            streamVideo = false
            outbox = channel
            live = true
            _status.value = EdgeLinkStatus.Connected
            coroutineScope {
                launch {
                    for (message in channel) {
                        try {
                            if (!sendWithTimeout(connection, message)) throw IOException("sending a frame timed out")
                            sent.incrementAndGet()
                        } finally {
                            busy.set(false)
                        }
                    }
                }
                while (true) handleGroundMessage(connection.receive())
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: WebSocketClosedException) {
            if (e.code == EdgeLink.CLOSE_UNAUTHORIZED) return Outcome.TokenRejected
            return Outcome.Dropped(describe(e), clock.nowMs() - startedAt)
        } catch (e: Exception) {
            return Outcome.Dropped(describe(e), clock.nowMs() - startedAt)
        } finally {
            live = false
            outbox = null
            streamVideo = false
            busy.set(false)
            connection.close()
        }
    }

    /** False when the write took longer than the send timeout. */
    private suspend fun sendWithTimeout(connection: SocketIoConnection, frame: String): Boolean =
        withTimeoutOrNull(config.sendTimeoutMs) { connection.send(frame) } != null

    /** Acts on `stream_video` only; everything else, including text that is not JSON, is ignored. */
    private fun handleGroundMessage(text: String) {
        when (val action = EdgeLink.actionFor(text)) {
            is EdgeAction.SetThumbnail -> streamVideo = action.enabled
            EdgeAction.Ignore -> Unit
        }
    }

    private fun describe(e: Throwable): String =
        Redact.text(e.javaClass.simpleName + (e.message?.let { ": $it" } ?: ""), activeSecrets, maxLength = 200)

    private fun log(level: GroundLog.Level, message: String) {
        logSink.log(level, Redact.text(message, activeSecrets))
    }

    override fun toString(): String = "EdgePublisher($endpoint, status=${_status.value})"
}
