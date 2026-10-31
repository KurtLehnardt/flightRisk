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
    fun `the misnamed Claude key moves to its new name once and the old entry is removed`() {
        val storage = MemoryStorage(mapOf("FLIGHTRISK_API_KEY" to TEST_KEY))
        val store = SecretStore(storage)
        assertEquals(TEST_KEY, store.get(SecretName.CLAUDE_API_KEY))
        assertFalse(storage.map.containsKey("FLIGHTRISK_API_KEY"))
        // The dashboard key is not confused with the old name.
        assertNull(store.get(SecretName.DASHBOARD_API_KEY))
        val writes = storage.writes
        store.get(SecretName.CLAUDE_API_KEY)
        store.get(SecretName.EDGE_TOKEN)
        assertEquals("migration runs once", writes, storage.writes)
    }

    @Test
    fun `when both names hold a key, the new name wins and the old entry goes`() {
        val storage = MemoryStorage(mapOf("FLIGHTRISK_API_KEY" to "old-test-key", "FLIGHTRISK_CLAUDE_API_KEY" to TEST_KEY))
        val store = SecretStore(storage)
        store.migrateLegacyKeys()
        assertEquals(TEST_KEY, store.get(SecretName.CLAUDE_API_KEY))
        assertEquals(setOf("FLIGHTRISK_CLAUDE_API_KEY"), storage.map.keys)
    }

    @Test
    fun `a failed migration write is retried on the next access`() {
        val storage = MemoryStorage(mapOf("FLIGHTRISK_API_KEY" to TEST_KEY))
        storage.failWrites = true
        val store = SecretStore(storage)
        store.get(SecretName.EDGE_TOKEN)
        assertTrue(storage.map.containsKey("FLIGHTRISK_API_KEY"))
        storage.failWrites = false
        assertEquals(TEST_KEY, store.get(SecretName.CLAUDE_API_KEY))
        assertFalse(storage.map.containsKey("FLIGHTRISK_API_KEY"))
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
