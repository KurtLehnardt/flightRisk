package com.flightrisk.app.ground

import android.content.Context

/**
 * The secrets the app keeps, with the storage key names both mobile apps use
 * (docs/mobile-contract.md, "Secrets").
 */
enum class SecretName(val storageKey: String) {
    /** The ground's `FLIGHTRISK_API_KEY`, used for Socket.IO and REST. */
    DASHBOARD_API_KEY("FLIGHTRISK_DASHBOARD_API_KEY"),

    /** The ground's `FLIGHTRISK_EDGE_TOKEN`, used only on the edge link. */
    EDGE_TOKEN("FLIGHTRISK_EDGE_TOKEN"),

    /** The phone's own optional Anthropic key for cloud reasoning. */
    CLAUDE_API_KEY("FLIGHTRISK_CLAUDE_API_KEY"),
}

/**
 * The storage behind [SecretStore]. On a device it is
 * [EncryptedPrefsSecretStorage]; tests use an in-memory map.
 */
interface SecretStoragePort {
    /** The stored value for [key], or null when there is none. */
    fun read(key: String): String?

    /**
     * Applies every change at once: a null value removes the key. Returns
     * true when the change was stored.
     */
    fun write(changes: Map<String, String?>): Boolean
}

/**
 * Reads and writes the app's secrets. The values live only in the storage
 * port (EncryptedSharedPreferences on a device) and never appear in
 * [toString], logs or exception messages.
 *
 * The first access moves the Claude key from the old, misleading name
 * [LEGACY_CLAUDE_KEY] (`FLIGHTRISK_API_KEY`, which on the ground means the
 * dashboard key) to [SecretName.CLAUDE_API_KEY] and removes the old entry.
 * When both names hold a value, the new name wins and the old entry is
 * removed.
 */
class SecretStore(private val storage: SecretStoragePort) {

    /** The Android store: EncryptedSharedPreferences in the app's existing secure preferences file. */
    constructor(context: Context) : this(EncryptedPrefsSecretStorage(context))

    @Volatile
    private var migrated = false
    private val migrationLock = Any()

    /** The secret [name], or null when none is stored. */
    fun get(name: SecretName): String? {
        ensureMigrated()
        return storage.read(name.storageKey)?.takeIf { it.isNotEmpty() }
    }

    /**
     * Stores [value] under [name]. Surrounding whitespace is removed; a null,
     * empty or blank value removes the secret. Returns true when stored.
     */
    fun set(name: SecretName, value: String?): Boolean {
        ensureMigrated()
        val cleaned = value?.trim()?.takeIf { it.isNotEmpty() }
        return storage.write(mapOf(name.storageKey to cleaned))
    }

    /** True when a value is stored under [name]. */
    fun has(name: SecretName): Boolean = get(name) != null

    /** Removes the secret [name]. */
    fun clear(name: SecretName): Boolean = set(name, null)

    /**
     * Every stored secret value, for [Redact.text]. Pass the result to the
     * redaction helper only; never log or display it.
     */
    fun valuesForRedaction(): List<String> = SecretName.entries.mapNotNull { get(it) }

    /** Runs the one-time key migration now (it otherwise runs on first access). */
    fun migrateLegacyKeys() = ensureMigrated()

    override fun toString(): String = "SecretStore(${Redact.PLACEHOLDER})"

    private fun ensureMigrated() {
        if (migrated) return
        synchronized(migrationLock) {
            if (migrated) return
            val legacy = storage.read(LEGACY_CLAUDE_KEY)
            val stored = if (legacy == null) {
                true
            } else {
                val current = storage.read(SecretName.CLAUDE_API_KEY.storageKey)
                val changes = linkedMapOf<String, String?>()
                if (current.isNullOrEmpty() && legacy.isNotBlank()) {
                    changes[SecretName.CLAUDE_API_KEY.storageKey] = legacy.trim()
                }
                changes[LEGACY_CLAUDE_KEY] = null
                storage.write(changes)
            }
            // A failed write is retried on the next access.
            if (stored) migrated = true
        }
    }

    companion object {
        /** The old preference name of the Claude key, removed by the migration. */
        const val LEGACY_CLAUDE_KEY = "FLIGHTRISK_API_KEY"
    }
}
