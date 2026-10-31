package com.flightrisk.app.ground

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SecretStoreTest {

    @Test
    fun `the storage keys are the names both apps use`() {
        assertEquals("FLIGHTRISK_DASHBOARD_API_KEY", SecretName.DASHBOARD_API_KEY.storageKey)
        assertEquals("FLIGHTRISK_EDGE_TOKEN", SecretName.EDGE_TOKEN.storageKey)
        assertEquals("FLIGHTRISK_CLAUDE_API_KEY", SecretName.CLAUDE_API_KEY.storageKey)
        assertEquals("FLIGHTRISK_API_KEY", SecretStore.LEGACY_CLAUDE_KEY)
    }

    @Test
    fun `get, set and clear`() {
        val storage = MemoryStorage()
        val store = SecretStore(storage)
        assertNull(store.get(SecretName.DASHBOARD_API_KEY))
        assertTrue(store.set(SecretName.DASHBOARD_API_KEY, "  $TEST_KEY \n"))
        assertEquals(TEST_KEY, store.get(SecretName.DASHBOARD_API_KEY))
        assertEquals(TEST_KEY, storage.map["FLIGHTRISK_DASHBOARD_API_KEY"])
        assertTrue(store.has(SecretName.DASHBOARD_API_KEY))
        assertFalse(store.has(SecretName.EDGE_TOKEN))
        store.set(SecretName.DASHBOARD_API_KEY, "   ")
        assertNull(store.get(SecretName.DASHBOARD_API_KEY))
        assertFalse(storage.map.containsKey("FLIGHTRISK_DASHBOARD_API_KEY"))
        store.set(SecretName.EDGE_TOKEN, TEST_KEY)
        store.clear(SecretName.EDGE_TOKEN)
        assertNull(store.get(SecretName.EDGE_TOKEN))
    }

    @Test
    fun `reading never migrates on its own, so old code keeps its key until U5 switches over`() {
        val storage = MemoryStorage(mapOf("FLIGHTRISK_API_KEY" to TEST_KEY))
        val store = SecretStore(storage)
        assertNull(store.get(SecretName.CLAUDE_API_KEY))
        store.get(SecretName.DASHBOARD_API_KEY)
        store.set(SecretName.EDGE_TOKEN, TEST_KEY)
        assertEquals(TEST_KEY, storage.map["FLIGHTRISK_API_KEY"])
    }

    @Test
    fun `the explicit migration moves the misnamed Claude key in one commit`() {
        val storage = MemoryStorage(mapOf("FLIGHTRISK_API_KEY" to TEST_KEY))
        val store = SecretStore(storage)
        assertTrue(store.migrateLegacyKeys())
        assertEquals(1, storage.writes)
        assertEquals(TEST_KEY, store.get(SecretName.CLAUDE_API_KEY))
        assertFalse(storage.map.containsKey("FLIGHTRISK_API_KEY"))
        assertNull(store.get(SecretName.DASHBOARD_API_KEY))
        // Running it again changes nothing.
        assertTrue(store.migrateLegacyKeys())
        assertEquals(1, storage.writes)
    }

    @Test
    fun `when both names hold a key, the new name wins and the old entry goes`() {
        val storage = MemoryStorage(mapOf("FLIGHTRISK_API_KEY" to "old-test-key", "FLIGHTRISK_CLAUDE_API_KEY" to TEST_KEY))
        val store = SecretStore(storage)
        assertTrue(store.migrateLegacyKeys())
        assertEquals(TEST_KEY, store.get(SecretName.CLAUDE_API_KEY))
        assertEquals(setOf("FLIGHTRISK_CLAUDE_API_KEY"), storage.map.keys)
    }

    @Test
    fun `a failed migration write leaves the old key in place`() {
        val storage = MemoryStorage(mapOf("FLIGHTRISK_API_KEY" to TEST_KEY))
        storage.failWrites = true
        val store = SecretStore(storage)
        assertFalse(store.migrateLegacyKeys())
        assertEquals(TEST_KEY, storage.map["FLIGHTRISK_API_KEY"])
        storage.failWrites = false
        assertTrue(store.migrateLegacyKeys())
        assertEquals(TEST_KEY, store.get(SecretName.CLAUDE_API_KEY))
    }

    @Test
    fun `blocking calls refuse the main thread and the suspend versions are main-safe`() = kotlinx.coroutines.runBlocking {
        val mainThread = Thread.currentThread()
        val store = SecretStore(MemoryStorage(), isMainThread = { Thread.currentThread() === mainThread })
        for (call in listOf<() -> Unit>(
            { store.get(SecretName.DASHBOARD_API_KEY) },
            { store.set(SecretName.DASHBOARD_API_KEY, TEST_KEY) },
            { store.migrateLegacyKeys() },
        )) {
            try {
                call()
                throw AssertionError("expected MainThreadSecretAccessException")
            } catch (_: MainThreadSecretAccessException) {
            }
        }
        assertTrue(store.save(SecretName.DASHBOARD_API_KEY, TEST_KEY))
        assertEquals(TEST_KEY, store.load(SecretName.DASHBOARD_API_KEY))
    }

    @Test
    fun `storage that cannot be opened or decrypted reads as unavailable instead of crashing`() {
        val broken = EncryptedPrefsSecretStorage { throw java.security.GeneralSecurityException("keyset unreadable") }
        val store = SecretStore(broken)
        assertNull(store.get(SecretName.DASHBOARD_API_KEY))
        assertFalse(store.set(SecretName.DASHBOARD_API_KEY, TEST_KEY))
        val state = store.availability() as SecretAvailability.Unavailable
        assertTrue(state.reason.contains("GeneralSecurityException"))

        val failingRead = EncryptedPrefsSecretStorage { FakePrefs(failReads = true) }
        assertNull(SecretStore(failingRead).get(SecretName.EDGE_TOKEN))
        assertTrue(failingRead.availability() is SecretAvailability.Unavailable)

        val working = EncryptedPrefsSecretStorage { FakePrefs() }
        val ok = SecretStore(working)
        assertTrue(ok.set(SecretName.EDGE_TOKEN, TEST_KEY))
        assertEquals(TEST_KEY, ok.get(SecretName.EDGE_TOKEN))
        assertEquals(SecretAvailability.Available, ok.availability())
    }

    @Test
    fun `no secret appears in toString`() {
        val store = SecretStore(MemoryStorage())
        store.set(SecretName.DASHBOARD_API_KEY, TEST_KEY)
        assertFalse(store.toString().contains(TEST_KEY))
        assertEquals(listOf(TEST_KEY), store.valuesForRedaction())
    }
}

class RedactTest {

    @Test
    fun `secrets, Bearer values and credential fields are removed`() {
        val text = "connect 40{\"api_key\":\"$TEST_KEY\"} header Authorization: Bearer abc.def and $TEST_KEY again"
        val out = Redact.text(text, listOf(TEST_KEY))
        assertFalse(out.contains(TEST_KEY))
        assertFalse(out.contains("abc.def"))
        assertTrue(out.contains("Authorization: ${Redact.PLACEHOLDER}"))
        assertEquals("Bearer ${Redact.PLACEHOLDER}", Redact.text("Bearer abc.def"))
        // Credential fields are caught even when the value is not a known secret.
        assertFalse(Redact.text("{\"token\": \"edge-secret-value\"}").contains("edge-secret-value"))
    }

    @Test
    fun `line breaks cannot forge log lines`() {
        assertEquals("a b c", Redact.text("a\nb\rc"))
    }

    @Test
    fun `text of exactly 200 characters is unchanged and 201 is cut to 200`() {
        val exact = "a".repeat(200)
        assertEquals(exact, Redact.text(exact))
        val cut = Redact.text("a".repeat(201))
        assertEquals(200, cut.length)
        assertTrue(cut.endsWith(Redact.ELLIPSIS))
    }

    @Test
    fun `redaction happens before truncation, so no part of a key survives the cut`() {
        for (start in 185..199) {
            val text = "x".repeat(start) + TEST_KEY + "y".repeat(100)
            val out = Redact.text(text, listOf(TEST_KEY))
            assertTrue(out.length <= Redact.MAX_LENGTH)
            // Truncating first would leave a prefix such as "test-k" visible.
            for (prefixLength in 3..TEST_KEY.length) {
                assertFalse("start $start: $out", out.contains(TEST_KEY.substring(0, prefixLength)))
            }
        }
    }

    @Test
    fun `a surrogate pair is never split`() {
        val out = Redact.truncate("a".repeat(198) + "🚁" + "b", 200)
        assertFalse(Character.isHighSurrogate(out[out.length - 2]))
    }

    @Test
    fun `keys and endpoints never print a secret`() {
        assertEquals("DashboardApiKey(<redacted>)", DashboardApiKey.of(TEST_KEY).toString())
        assertNull(DashboardApiKey.of("has space"))
        assertNull(DashboardApiKey.of("line\nbreak"))
        assertNull(DashboardApiKey.of(""))
        assertEquals("http://192.168.1.20:5555", GroundEndpoint.of("192.168.1.20").toString())
    }
}

/** A minimal in-memory SharedPreferences; [failReads] makes every read throw like a corrupted file. */
class FakePrefs(private val failReads: Boolean = false) : android.content.SharedPreferences {
    private val map = LinkedHashMap<String, String>()

    override fun getString(key: String?, defValue: String?): String? {
        if (failReads) throw SecurityException("could not decrypt")
        return map[key] ?: defValue
    }

    override fun edit(): android.content.SharedPreferences.Editor = object : android.content.SharedPreferences.Editor {
        val puts = LinkedHashMap<String, String?>()
        override fun putString(key: String, value: String?) = apply { puts[key] = value }
        override fun remove(key: String) = apply { puts[key] = null }
        override fun commit(): Boolean {
            for ((k, v) in puts) if (v == null) map.remove(k) else map[k] = v
            return true
        }
        override fun apply() {
            commit()
        }
        override fun putStringSet(key: String?, values: MutableSet<String>?) = this
        override fun putInt(key: String?, value: Int) = this
        override fun putLong(key: String?, value: Long) = this
        override fun putFloat(key: String?, value: Float) = this
        override fun putBoolean(key: String?, value: Boolean) = this
        override fun clear() = this
    }

    override fun getAll(): MutableMap<String, *> = map
    override fun getStringSet(key: String?, defValues: MutableSet<String>?) = defValues
    override fun getInt(key: String?, defValue: Int) = defValue
    override fun getLong(key: String?, defValue: Long) = defValue
    override fun getFloat(key: String?, defValue: Float) = defValue
    override fun getBoolean(key: String?, defValue: Boolean) = defValue
    override fun contains(key: String?) = map.containsKey(key)
    override fun registerOnSharedPreferenceChangeListener(l: android.content.SharedPreferences.OnSharedPreferenceChangeListener?) = Unit
    override fun unregisterOnSharedPreferenceChangeListener(l: android.content.SharedPreferences.OnSharedPreferenceChangeListener?) = Unit
}
