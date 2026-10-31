package com.flightrisk.app.llm

import android.content.SharedPreferences
import com.flightrisk.app.config.SingleInstance
import com.flightrisk.app.ground.EncryptedPrefsSecretStorage
import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.ground.SecretRead
import com.flightrisk.app.ground.SecretStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import javax.xml.parsers.DocumentBuilderFactory
import kotlin.concurrent.thread

/** Tests for the findings of the PR #164 review round. */
@OptIn(ExperimentalCoroutinesApi::class)
class ReviewRoundFixesTest {

    private val bitmap = fakeBitmap()
    private val legacyName = SecretStore.LEGACY_CLAUDE_KEY
    private val newName = SecretName.CLAUDE_API_KEY.storageKey

    // ---- 1. The key migration is not marked done while the store cannot be read ----

    @Test
    fun `migration with an unavailable store moves nothing, deletes nothing and is retried`() {
        val plainPrefs = FakePrefs(mapOf(legacyName to "plain-test-key"))
        val reasoningPrefs = FakePrefs()
        val storage = FakeSecretStorage(mapOf(legacyName to TEST_CLAUDE_KEY)).apply { unavailable = true }
        val secrets = SecretStore(storage, isMainThread = { false })
        val settings = ReasoningSettings(reasoningPrefs, legacyPrefs = plainPrefs)

        assertFalse(settings.migrateLegacyKeyOnce(secrets))
        assertFalse(reasoningPrefs.map.containsKey(ReasoningSettings.KEY_MIGRATION_DONE_KEY))
        assertEquals(TEST_CLAUDE_KEY, storage.map[legacyName])
        assertFalse(storage.map.containsKey(newName))
        assertEquals("plain-test-key", plainPrefs.map[legacyName])

        // The next launch, with a working store.
        storage.unavailable = false
        assertTrue(settings.migrateLegacyKeyOnce(secrets))
        assertEquals(TEST_CLAUDE_KEY, storage.map[newName])
        assertFalse(storage.map.containsKey(legacyName))
        assertFalse(plainPrefs.map.containsKey(legacyName))
        assertEquals(true, reasoningPrefs.map[ReasoningSettings.KEY_MIGRATION_DONE_KEY])
        assertFalse(settings.cloudOptIn)
    }

    @Test
    fun `migrateLegacyKeys returns false when the store reports unavailable`() {
        val storage = FakeSecretStorage(mapOf(legacyName to TEST_CLAUDE_KEY)).apply { unavailable = true }
        assertFalse(SecretStore(storage, isMainThread = { false }).migrateLegacyKeys())
        assertEquals(TEST_CLAUDE_KEY, storage.map[legacyName])
    }

    /** SharedPreferences whose reads throw until [broken] is false. */
    private class ThrowingPrefs(private val inner: FakePrefs) : SharedPreferences by inner {
        constructor(initial: Map<String, Any?>) : this(FakePrefs(initial))

        @Volatile var broken = true
        override fun getString(key: String, defValue: String?): String? {
            if (broken) throw SecurityException("decrypt failed")
            return inner.getString(key, defValue)
        }
        val map get() = inner.map
    }

    @Test
    fun `a read that throws is unreadable, not absent, and the migration is retried`() {
        val prefs = ThrowingPrefs(mapOf(legacyName to TEST_CLAUDE_KEY))
        val storage = EncryptedPrefsSecretStorage { prefs }
        assertEquals(SecretRead.Unreadable, storage.readResult(legacyName))
        val reasoningPrefs = FakePrefs()
        val settings = ReasoningSettings(reasoningPrefs)
        assertFalse(settings.migrateLegacyKeyOnce(SecretStore(storage, isMainThread = { false })))
        assertEquals(TEST_CLAUDE_KEY, prefs.map[legacyName])
        assertFalse(reasoningPrefs.map.containsKey(ReasoningSettings.KEY_MIGRATION_DONE_KEY))

        // A later launch opens the store normally (a new storage instance).
        prefs.broken = false
        val fresh = EncryptedPrefsSecretStorage { prefs }
        assertTrue(settings.migrateLegacyKeyOnce(SecretStore(fresh, isMainThread = { false })))
        assertEquals(TEST_CLAUDE_KEY, prefs.map[newName])
        assertFalse(prefs.map.containsKey(legacyName))
        assertEquals(true, reasoningPrefs.map[ReasoningSettings.KEY_MIGRATION_DONE_KEY])
    }

    @Test
    fun `a store that cannot be opened is unreadable and the migration is not marked done`() {
        val storage = EncryptedPrefsSecretStorage { throw java.security.KeyStoreException("locked") }
        assertEquals(SecretRead.Unreadable, storage.readResult(legacyName))
        val reasoningPrefs = FakePrefs()
        assertFalse(ReasoningSettings(reasoningPrefs).migrateLegacyKeyOnce(SecretStore(storage, isMainThread = { false })))
        assertFalse(reasoningPrefs.map.containsKey(ReasoningSettings.KEY_MIGRATION_DONE_KEY))
    }

    // ---- 2. No redirects and no re-sends ----

    @Test
    fun `a 307 redirect is not followed and nothing reaches the other host`() = runBlocking {
        CloudHarness().use { h ->
            StubClaudeServer().use { other ->
                h.enableCloud()
                for (code in listOf(307, 308, 302)) {
                    h.server.rawResponse =
                        "HTTP/1.1 $code Redirect\r\nLocation: ${other.url}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    val result = h.backend.analyzeMatch(bitmap, bitmap, "red jacket")
                    assertEquals("error", result.confidence)
                    assertEquals("API error $code", result.reasoning)
                }
                assertEquals(3, h.server.requests.size)
                assertEquals(0, other.accepted)
            }
        }
    }

    @Test
    fun `a 503 with Retry-After 0 is not re-sent`() = runBlocking {
        CloudHarness().use { h ->
            h.enableCloud()
            h.server.rawResponse =
                "HTTP/1.1 503 Unavailable\r\nRetry-After: 0\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            val result = h.backend.describeMatch(bitmap, "red jacket")
            assertEquals("error", result.confidence)
            assertEquals(1, h.server.requests.size)
        }
    }

    // ---- 3. No wrong "no key" notice before the key has been read ----

    @Test
    fun `before the key is read there is no wrong notice, and the key check resolves it`() = runBlocking {
        CloudHarness(
            prefs = mapOf(ReasoningSettings.CLOUD_OPT_IN_KEY to true, ReasoningSettings.PROVIDER_KEY to "cloud_claude"),
            secrets = mapOf(newName to TEST_CLAUDE_KEY),
            localUsable = true,
            lookupKeyAtStart = false,
        ).use { h ->
            val early = h.selector.status.value
            assertTrue(early.checkingKey)
            assertNull(early.notice)
            assertFalse(early.unavailable.containsKey(ReasoningProvider.CLOUD_CLAUDE))
            assertFalse(early.isCloudActive)
            assertEquals(ReasoningProvider.LOCAL, early.active)

            val waited = async(Dispatchers.Default) { h.selector.awaitKeyChecked(timeoutMs = 5_000) }
            delay(50)
            withContext(Dispatchers.IO) { h.selector.refreshWithKeyLookup() }
            val resolved = withTimeout(5_000) { waited.await() }
            assertFalse(resolved.checkingKey)
            assertTrue(resolved.isCloudActive)
        }
    }

    @Test
    fun `waiting for the key check times out with the current status`() = runBlocking {
        CloudHarness(
            prefs = mapOf(ReasoningSettings.CLOUD_OPT_IN_KEY to true, ReasoningSettings.PROVIDER_KEY to "cloud_claude"),
            lookupKeyAtStart = false,
        ).use { h ->
            val status = h.selector.awaitKeyChecked(timeoutMs = 50)
            assertTrue(status.checkingKey)
            assertNull(status.notice)
        }
    }

    // ---- 4. Backup rules and the separate settings file ----

    private fun excludes(file: String): Map<String, Set<String>> {
        val dir = listOf(File("src/main/res/xml"), File("app/src/main/res/xml"), File("mobile/app/src/main/res/xml"))
            .first { it.isDirectory }
        val doc = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(File(dir, file))
        val result = mutableMapOf<String, MutableSet<String>>()
        val nodes = doc.getElementsByTagName("exclude")
        for (i in 0 until nodes.length) {
            val e = nodes.item(i) as Element
            val section = (e.parentNode as Element).tagName
            if (e.getAttribute("domain") == "sharedpref") result.getOrPut(section) { mutableSetOf() } += e.getAttribute("path")
        }
        return result
    }

    @Test
    fun `reasoning settings, the secret store and the old config file are excluded from backup and transfer`() {
        val required = setOf(
            "flightrisk_secure_prefs.xml",
            "${ReasoningSettings.PREFS_NAME}.xml",
            "${ReasoningSettings.LEGACY_PREFS_NAME}.xml",
        )
        val full = excludes("backup_rules.xml")
        assertTrue(full.getValue("full-backup-content").containsAll(required))
        val modern = excludes("data_extraction_rules.xml")
        assertTrue(modern.getValue("cloud-backup").containsAll(required))
        assertTrue(modern.getValue("device-transfer").containsAll(required))
        assertFalse(ReasoningSettings.PREFS_NAME == ReasoningSettings.LEGACY_PREFS_NAME)
    }

    @Test
    fun `values in the old backed-up file are removed and the opt-in is never carried over`() {
        val old = FakePrefs(mapOf(
            ReasoningSettings.CLOUD_OPT_IN_KEY to true,
            ReasoningSettings.PROVIDER_KEY to "cloud_claude",
            ReasoningSettings.KEY_MIGRATION_DONE_KEY to true,
        ))
        val prefs = FakePrefs()
        val settings = ReasoningSettings(prefs, legacyPrefs = old)
        settings.migrateLegacyValues()
        assertFalse(settings.cloudOptIn)
        assertNull(settings.storedProvider)
        assertTrue(prefs.map.isEmpty())
        assertTrue(old.map.isEmpty())

        val oldLocal = FakePrefs(mapOf(ReasoningSettings.PROVIDER_KEY to "local"))
        val prefs2 = FakePrefs()
        ReasoningSettings(prefs2, legacyPrefs = oldLocal).migrateLegacyValues()
        assertEquals("local", prefs2.map[ReasoningSettings.PROVIDER_KEY])
        assertTrue(oldLocal.map.isEmpty())
    }

    // ---- 5. One encrypted store per process ----

    @Test
    fun `the shared instance is created once under concurrent first use`() {
        val holder = SingleInstance<Any>()
        val created = AtomicInteger()
        val start = CountDownLatch(1)
        val results = java.util.Collections.synchronizedList(mutableListOf<Any>())
        val threads = (1..16).map {
            thread {
                start.await()
                results += holder.get {
                    created.incrementAndGet()
                    Thread.sleep(20)
                    Any()
                }
            }
        }
        start.countDown()
        threads.forEach { it.join() }
        assertEquals(1, created.get())
        assertEquals(1, results.toSet().size)
    }

    @Test
    fun `a failed creation is not stored and is tried again`() {
        val holder = SingleInstance<String>()
        try {
            holder.get { throw IllegalStateException("keystore busy") }
        } catch (_: IllegalStateException) {
        }
        assertEquals("ok", holder.get { "ok" })
    }

    // ---- 7. A cancel between the second check and the send ----

    @Test
    fun `an opt-out between the second gate check and registering the call sends nothing`() = runBlocking {
        CloudHarness().use { h ->
            h.enableCloud()
            h.backend.beforeSend = { h.settings.setCloudOptIn(false) }
            assertEquals(CloudClaudeLlmBackend.BLOCKED, h.backend.analyzeMatch(bitmap, bitmap, "red jacket"))
            assertEquals(0, h.server.accepted)
            assertEquals(0, h.backend.inFlightCount())
        }
    }

    @Test
    fun `key changes and the migration run on the io dispatcher, not the caller's thread`() {
        val executor = Executors.newSingleThreadExecutor { Thread(it, "reasoning-io-test") }
        val io = executor.asCoroutineDispatcher()
        try {
            CloudHarness().use { h ->
                runTest(UnconfinedTestDispatcher()) {
                    val c = ReasoningController(h.settings, h.gate, h.selector, backgroundScope, io = io)
                    var migrateThread = ""
                    c.start { migrateThread = Thread.currentThread().name }
                    assertTrue(migrateThread, migrateThread.startsWith("reasoning-io-test"))

                    c.onAction(ReasoningAction.PickProvider(ReasoningProvider.CLOUD_CLAUDE))
                    c.onAction(ReasoningAction.ConfirmConsent)
                    c.onAction(ReasoningAction.SaveKey(TEST_CLAUDE_KEY))
                    withContext(Dispatchers.Default) {
                        withTimeout(5_000) { while (c.ui.value.message == null) delay(5) }
                    }
                    c.onAction(ReasoningAction.RemoveKey)
                    withContext(Dispatchers.Default) {
                        withTimeout(5_000) { while (h.storage.map.containsKey(newName) || c.ui.value.hasSavedKey) delay(5) }
                    }
                    assertTrue(h.storage.writeThreads.isNotEmpty())
                    assertTrue(h.storage.writeThreads.toString(), h.storage.writeThreads.all { it.startsWith("reasoning-io-test") })
                }
            }
        } finally {
            io.close()
        }
    }
}
