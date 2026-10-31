package com.flightrisk.app.ground

import com.flightrisk.contract.ContractJson
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.channels.Channel
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import java.io.File
import java.io.IOException
import java.util.concurrent.CopyOnWriteArrayList

/** The only key the tests use. */
const val TEST_KEY = "test-key"

/** A clock that moves only when the test calls [advance]. */
class FakeClock : GroundClock {
    private var now = 0L
    private val waiters = mutableListOf<Pair<Long, CompletableDeferred<Unit>>>()

    @Synchronized
    override fun nowMs(): Long = now

    override suspend fun sleep(ms: Long) {
        if (ms <= 0) return
        val done = CompletableDeferred<Unit>()
        synchronized(this) { waiters += (now + ms) to done }
        try {
            done.await()
        } finally {
            synchronized(this) { waiters.removeAll { it.second === done } }
        }
    }

    /** The deadlines of the coroutines sleeping now. */
    @Synchronized
    fun sleepers(): List<Long> = waiters.map { it.first }

    /** Moves time forward and wakes every sleeper whose deadline has passed. */
    fun advance(ms: Long) {
        val due: List<CompletableDeferred<Unit>>
        synchronized(this) {
            now += ms
            due = waiters.filter { it.first <= now }.map { it.second }
            waiters.removeAll { it.first <= now }
        }
        due.forEach { it.complete(Unit) }
    }
}

/** One fake connection: the test plays the server. */
class FakeConnection : SocketIoConnection {
    private val inbox = Channel<Any>(Channel.UNLIMITED)
    val sent = CopyOnWriteArrayList<String>()

    @Volatile var closed = false

    @Volatile var failSends: Exception? = null

    override suspend fun receive(): String {
        val item = inbox.receiveCatching().getOrNull() ?: throw IOException("connection closed")
        if (item is Exception) throw item
        return item as String
    }

    override suspend fun send(frame: String) {
        if (closed) throw IOException("connection closed")
        failSends?.let { throw it }
        sent += frame
    }

    override fun close() {
        closed = true
        inbox.cancel()
    }

    /** The server sends one text frame. */
    fun server(text: String) {
        inbox.trySend(text)
    }

    /** The transport fails with [e] on the next receive. */
    fun serverError(e: Exception) {
        inbox.trySend(e)
    }

    /** The event frames the client sent (`42[...]`). */
    fun eventFrames(): List<String> = sent.filter { it.startsWith("42") }
}

/** A port that hands out [FakeConnection]s and records them. */
class FakePort : SocketIoPort {
    val opened = CopyOnWriteArrayList<FakeConnection>()

    @Volatile var failOpen: Exception? = null

    override suspend fun open(endpoint: GroundEndpoint): SocketIoConnection {
        failOpen?.let { throw it }
        return FakeConnection().also { opened += it }
    }

    suspend fun connection(index: Int): FakeConnection {
        eventually("connection $index opened") { opened.size > index }
        return opened[index]
    }
}

/** Records every log line. */
class CapturingLog : GroundLog {
    val lines = CopyOnWriteArrayList<String>()
    override fun log(level: GroundLog.Level, message: String) {
        lines += "$level $message"
    }
}

/** An in-memory [SecretStoragePort]. */
class MemoryStorage(initial: Map<String, String> = emptyMap()) : SecretStoragePort {
    val map = LinkedHashMap(initial)
    var writes = 0
    var failWrites = false

    override fun read(key: String): String? = map[key]

    override fun write(changes: Map<String, String?>): Boolean {
        if (failWrites) return false
        writes++
        for ((k, v) in changes) if (v == null) map.remove(k) else map[k] = v
        return true
    }
}

/** Polls [condition] until it holds or [timeoutMs] of real time passes. */
suspend fun eventually(what: String = "condition", timeoutMs: Long = 5_000, condition: () -> Boolean) {
    val deadline = System.currentTimeMillis() + timeoutMs
    while (!condition()) {
        if (System.currentTimeMillis() > deadline) throw AssertionError("timed out waiting for $what")
        delay(2)
    }
}

/**
 * The shared contract fixtures in tests/fixtures/mobile_contract, found the
 * same way the contract module's tests find them: the system property
 * `flightrisk.fixturesDir` when set, otherwise the path relative to the
 * module directory (`mobile/app`), which is the test working directory.
 */
object ContractFixtures {
    val dir: File by lazy {
        val configured = System.getProperty("flightrisk.fixturesDir")
        val candidate = (configured?.let(::File) ?: File("../../tests/fixtures/mobile_contract")).canonicalFile
        if (!candidate.isDirectory) {
            throw AssertionError(
                "Contract fixture directory not found at $candidate; run the tests from mobile/ with " +
                    "./gradlew :app:testDebugUnitTest so the path resolves from mobile/app.",
            )
        }
        candidate
    }

    fun cases(file: String): Map<String, JsonObject> {
        val root = ContractJson.parseToJsonElement(File(dir, file).readText()).jsonObject
        return (root["cases"] as JsonObject).mapValues { it.value as JsonObject }
    }

    fun case(file: String, name: String): JsonObject = cases(file)[name] ?: throw AssertionError("$file has no case $name")

    fun frame(name: String): String = (case("socketio_frames.json", name)["frame"] as JsonPrimitive).content

    /** A server event case as the `42[...]` frame the dashboard would send. */
    fun eventFrame(file: String, name: String): String {
        val c = case(file, name)
        val event = (c["event"] as JsonPrimitive).content
        return rawEventFrame(event, c["payload"].toString())
    }

    fun rawEventFrame(event: String, payloadJson: String): String = "42[" + JsonPrimitive(event) + "," + payloadJson + "]"
}
