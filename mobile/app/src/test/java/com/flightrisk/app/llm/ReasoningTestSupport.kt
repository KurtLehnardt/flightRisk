package com.flightrisk.app.llm

import android.content.SharedPreferences
import android.graphics.Bitmap
import com.flightrisk.app.ground.LoopbackServer
import com.flightrisk.app.ground.SecretAvailability
import com.flightrisk.app.ground.SecretStoragePort
import com.flightrisk.app.ground.SecretStore
import com.flightrisk.app.ground.headerValue
import com.flightrisk.app.ground.readRequestHead
import java.io.DataInputStream
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger

/** The only key the reasoning tests use. */
const val TEST_CLAUDE_KEY = "test-key"

/** In-memory SharedPreferences: values change at once on apply() and commit(). Thread-safe. */
class FakePrefs(initial: Map<String, Any?> = emptyMap()) : SharedPreferences {
    val map = ConcurrentHashMap<String, Any>().apply { initial.forEach { (k, v) -> if (v != null) put(k, v) } }
    var failCommits = false

    override fun getAll(): MutableMap<String, *> = HashMap(map)
    override fun getString(key: String, defValue: String?): String? = map[key] as? String ?: defValue
    @Suppress("UNCHECKED_CAST")
    override fun getStringSet(key: String, defValues: MutableSet<String>?): MutableSet<String>? =
        map[key] as? MutableSet<String> ?: defValues
    override fun getInt(key: String, defValue: Int): Int = map[key] as? Int ?: defValue
    override fun getLong(key: String, defValue: Long): Long = map[key] as? Long ?: defValue
    override fun getFloat(key: String, defValue: Float): Float = map[key] as? Float ?: defValue
    override fun getBoolean(key: String, defValue: Boolean): Boolean = map[key] as? Boolean ?: defValue
    override fun contains(key: String): Boolean = map.containsKey(key)
    override fun registerOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener?) = Unit
    override fun unregisterOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener?) = Unit

    override fun edit(): SharedPreferences.Editor = object : SharedPreferences.Editor {
        private val puts = LinkedHashMap<String, Any>()
        private val removes = LinkedHashSet<String>()
        private var clear = false
        override fun putString(key: String, value: String?) = apply { if (value == null) removes += key else puts[key] = value }
        override fun putStringSet(key: String, values: MutableSet<String>?) = apply { if (values == null) removes += key else puts[key] = values }
        override fun putInt(key: String, value: Int) = apply { puts[key] = value }
        override fun putLong(key: String, value: Long) = apply { puts[key] = value }
        override fun putFloat(key: String, value: Float) = apply { puts[key] = value }
        override fun putBoolean(key: String, value: Boolean) = apply { puts[key] = value }
        override fun remove(key: String) = apply { removes += key }
        override fun clear() = apply { clear = true }
        override fun commit(): Boolean {
            if (failCommits) return false
            write()
            return true
        }
        override fun apply() = write()
        private fun write() {
            if (clear) map.clear()
            removes.forEach { map.remove(it) }
            map.putAll(puts)
        }
    }
}

/** Thread-safe secret storage whose reads, writes and availability can fail on demand. */
class FakeSecretStorage(initial: Map<String, String> = emptyMap()) : SecretStoragePort {
    val map = ConcurrentHashMap(initial)

    @Volatile var unavailable = false
    @Volatile var failWrites = false
    val reads = AtomicInteger()

    /** Runs before every read; tests use it to change state between two gate checks. */
    @Volatile var beforeRead: (() -> Unit)? = null

    override fun read(key: String): String? {
        reads.incrementAndGet()
        beforeRead?.invoke()
        if (unavailable) return null
        return map[key]
    }

    override fun write(changes: Map<String, String?>): Boolean {
        if (unavailable || failWrites) return false
        for ((k, v) in changes) if (v == null) map.remove(k) else map[k] = v
        return true
    }

    override fun availability(): SecretAvailability =
        if (unavailable) SecretAvailability.Unavailable("could not open the secure storage (KeyStoreException)")
        else SecretAvailability.Available
}

/** A Bitmap instance for signatures only; the tests' image encoder never touches it. */
fun fakeBitmap(): Bitmap {
    val field = Class.forName("sun.misc.Unsafe").getDeclaredField("theUnsafe").apply { isAccessible = true }
    val unsafe = field.get(null)
    val allocate = unsafe.javaClass.getMethod("allocateInstance", Class::class.java)
    return allocate.invoke(unsafe, Bitmap::class.java) as Bitmap
}

/** One request the stub server received. */
data class StubRequest(val requestLine: String, val apiKey: String?, val body: String)

/**
 * A stand-in for the Messages API on 127.0.0.1. It answers with a fixed match,
 * or, with [hold], keeps the request open until the client gives up, so a test
 * can cancel it while it is in flight. Nothing leaves the machine.
 */
class StubClaudeServer(@Volatile var hold: Boolean = false) : AutoCloseable {
    val requests = CopyOnWriteArrayList<StubRequest>()
    val clientClosed = AtomicInteger()

    private val server = LoopbackServer { socket ->
        val input = socket.getInputStream()
        val head = readRequestHead(input)
        val length = headerValue(head, "Content-Length")?.toIntOrNull() ?: 0
        val body = ByteArray(length).also { DataInputStream(input).readFully(it) }
        requests += StubRequest(head.first(), headerValue(head, "x-api-key"), String(body, Charsets.UTF_8))
        if (hold) {
            // Wait for the client to close the connection (a cancelled call).
            while (input.read() >= 0) Unit
            clientClosed.incrementAndGet()
        } else {
            val answer = """{"content":[{"type":"text","text":"MATCH: yes\nCONFIDENCE: high\nREASONING: same jacket"}]}"""
            val bytes = answer.toByteArray()
            socket.getOutputStream().apply {
                write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n".toByteArray())
                write(bytes)
                flush()
            }
        }
    }

    val url: String get() = "http://127.0.0.1:${server.port}/v1/messages"
    val accepted: Int get() = server.accepted.get()

    override fun close() = server.close()
}

/** Everything a cloud test needs, wired like MainActivity but with fakes. */
class CloudHarness(
    prefs: Map<String, Any?> = emptyMap(),
    secrets: Map<String, String> = emptyMap(),
    val online: () -> Boolean = { true },
    localUsable: Boolean = false,
) : AutoCloseable {
    val prefs = FakePrefs(prefs)
    val storage = FakeSecretStorage(secrets)
    val secretStore = SecretStore(storage, isMainThread = { false })
    val settings = ReasoningSettings(this.prefs)
    val gate = CloudReasoningGate(settings, SecretStoreClaudeKeySource(secretStore))
    val server = StubClaudeServer()
    val logs = CopyOnWriteArrayList<String>()

    /** Runs inside the image encoder, between the first and the second gate check. */
    @Volatile var duringEncode: (() -> Unit)? = null

    val backend = CloudClaudeLlmBackend(
        gate = gate,
        isOnline = online,
        apiUrl = server.url,
        timeoutMs = 10_000,
        encodeImage = { duringEncode?.invoke(); "aW1hZ2U=" },
        log = { logs += it },
    )
    val local = FakeLocalBackend(usable = localUsable)
    val selector = LlmSelector(settings, gate, localModelUsable = { local.usable }, isOnline = online).apply {
        registerOnDeviceFirst(local, backend)
        refreshWithKeyLookup()
    }

    /** Opts in, selects Cloud Claude and saves the test key, as the user would. */
    fun enableCloud() {
        settings.confirmCloudConsent(selectCloud = true)
        check(gate.saveKey(TEST_CLAUDE_KEY))
        selector.refreshWithKeyLookup()
    }

    override fun close() {
        backend.close()
        server.close()
    }
}

/** An on-device backend stand-in. */
class FakeLocalBackend(@Volatile var usable: Boolean) : LlmBackend {
    override val name: String = "local_fake"
    override val isAvailable: Boolean get() = usable
    override suspend fun analyzeMatch(referenceImage: Bitmap, candidateImage: Bitmap, description: String?) =
        ReasoningResult(isMatch = false, confidence = "low", reasoning = "local")
    override suspend fun describeMatch(candidateImage: Bitmap, description: String) =
        ReasoningResult(isMatch = false, confidence = "low", reasoning = "local")
}
