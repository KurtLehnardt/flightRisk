package com.flightrisk.app.transport

import com.flightrisk.app.ground.CapturingLog
import com.flightrisk.app.ground.FakeClock
import com.flightrisk.app.ground.FakeConnection
import com.flightrisk.app.ground.FakePort
import com.flightrisk.app.ground.GroundEndpoint
import com.flightrisk.app.ground.SocketIoConnection
import com.flightrisk.app.ground.SocketIoPort
import com.flightrisk.app.pipeline.PublishedDetection
import com.flightrisk.app.pipeline.PublishedFrame
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import java.util.concurrent.atomic.AtomicInteger

const val TEST_TOKEN = "test-token"

/** A publisher on a fake port with a fake clock. */
class EdgeHarness(
    val token: String? = TEST_TOKEN,
    val config: EdgePublisherConfig = EdgePublisherConfig(),
    face: Boolean = false,
    val port: FakePort = FakePort(),
) {
    val clock = FakeClock()
    val log = CapturingLog()
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val endpoint = GroundEndpoint.of("127.0.0.1", 9000)!!
    val publisher = EdgePublisher(
        endpoint = endpoint,
        tokenProvider = { token },
        scope = scope,
        port = port,
        config = config,
        clock = clock,
        random = { 0.0 },
        logSink = log,
        faceMatchingEnabled = { face },
    )

    fun close() {
        publisher.stop()
        scope.cancel()
    }

    /** Everything that could leak a token: logs, status, stats, toString. */
    fun stateDump(): String =
        listOf(log.lines.joinToString("\n"), publisher.toString(), publisher.status.value.toString(), publisher.stats().toString()).joinToString("\n")
}

fun parseObject(text: String): JsonObject = Json.parseToJsonElement(text).jsonObject

fun typeOf(text: String): String = (parseObject(text)["type"] as JsonPrimitive).content

/** A frame with [detections] people, each with a crop, and a thumbnail. */
fun testFrame(
    id: Long = 1,
    detections: Int = 1,
    timestamp: Double = 1_700_000_000.5,
    thumbCalls: AtomicInteger? = null,
    cropCalls: AtomicInteger? = null,
    reid: List<Double>? = listOf(0.25, -0.5),
    face: List<Double>? = null,
    crop: ByteArray = byteArrayOf(1, 2, 3),
): PublishedFrame =
    PublishedFrame(
        frameId = id,
        timestampSeconds = timestamp,
        frameWidth = 640,
        frameHeight = 480,
        thumbnailJpeg = {
            thumbCalls?.incrementAndGet()
            byteArrayOf(9, 8, 7)
        },
        detections = List(detections) { i ->
            PublishedDetection(
                bbox = listOf(10.0 + i, 20.0, 110.0 + i, 220.0),
                confidence = 0.9,
                reidEmbedding = reid,
                faceEmbedding = face,
                cropJpeg = {
                    cropCalls?.incrementAndGet()
                    crop
                },
            )
        },
    )

/** A connection whose sends wait until the test releases them. */
class GateConnection : SocketIoConnection {
    val sent = java.util.concurrent.CopyOnWriteArrayList<String>()
    private val gate = CompletableDeferred<Unit>()
    private val inbox = kotlinx.coroutines.channels.Channel<Any>(kotlinx.coroutines.channels.Channel.UNLIMITED)
    @Volatile var closed = false
    @Volatile var blockSends = false

    fun release() = gate.complete(Unit)

    override suspend fun receive(): String {
        val item = inbox.receiveCatching().getOrNull() ?: throw java.io.IOException("connection closed")
        if (item is Exception) throw item
        return item as String
    }

    override suspend fun send(frame: String) {
        if (blockSends && sent.size >= 1) gate.await()
        sent += frame
    }

    override suspend fun closeGracefully() = close()
    override fun close() {
        closed = true
        inbox.cancel()
    }
}

class GatePort(val connection: GateConnection) : SocketIoPort {
    val opened = AtomicInteger()
    override suspend fun open(endpoint: GroundEndpoint, secrets: List<String>): SocketIoConnection {
        opened.incrementAndGet()
        return connection
    }
}

/** A port that records the secrets it was given. */
class RecordingPort : SocketIoPort {
    val secretsSeen = java.util.concurrent.CopyOnWriteArrayList<List<String>>()
    val connections = java.util.concurrent.CopyOnWriteArrayList<FakeConnection>()
    override suspend fun open(endpoint: GroundEndpoint, secrets: List<String>): SocketIoConnection {
        secretsSeen += secrets
        return FakeConnection().also { connections += it }
    }
}
