package com.flightrisk.app.ground

import android.content.Context
import android.os.Looper
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

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

/** Whether the secret storage can be used. */
sealed interface SecretAvailability {
    data object Available : SecretAvailability

    /**
     * The storage could not be opened or decrypted (for example a backup
     * restored to another device, whose Keystore key is gone, or a corrupted
     * file). [reason] names the error type only and never holds a secret.
     * Reads return null and writes fail until the user re-enters the secrets.
     */
    data class Unavailable(val reason: String) : SecretAvailability
}

/**
 * The result of reading one secret, telling "nothing stored" apart from "could
 * not be read". [toString] never shows the value.
 */
sealed interface SecretRead {
    class Value(val value: String) : SecretRead {
        override fun toString(): String = "Value(${Redact.PLACEHOLDER})"
    }

    data object Absent : SecretRead

    /** The storage could not be opened or the read failed. */
    data object Unreadable : SecretRead
}

/**
 * The storage behind [SecretStore]. On a device it is
 * [EncryptedPrefsSecretStorage]; tests use an in-memory map. Implementations
 * never throw: a failure reads as null, writes as false, and shows in
 * [availability].
 */
interface SecretStoragePort {
    /** The stored value for [key], or null when there is none or it cannot be read. */
    fun read(key: String): String?

    /**
     * Like [read], but a failure is [SecretRead.Unreadable] instead of null.
     * The default asks [availability] after the read; implementations that
     * can tell the cases apart directly override it.
     */
    fun readResult(key: String): SecretRead {
        val value = read(key)
        if (availability() is SecretAvailability.Unavailable) return SecretRead.Unreadable
        return value?.let { SecretRead.Value(it) } ?: SecretRead.Absent
    }

    /**
     * Applies every change in one commit: a null value removes the key.
     * Returns true when the change was stored.
     */
    fun write(changes: Map<String, String?>): Boolean

    /** Whether the storage works. */
    fun availability(): SecretAvailability = SecretAvailability.Available
}

/** Thrown when a blocking [SecretStore] call is made on the main thread. */
class MainThreadSecretAccessException :
    IllegalStateException("SecretStore.get/set block on the Android Keystore; call them off the main thread or use load/save")

/**
 * Reads and writes the app's secrets. The values live only in the storage
 * port (EncryptedSharedPreferences on a device) and never appear in
 * [toString], logs or exception messages.
 *
 * Threading: [get], [set], [has], [clear] and [migrateLegacyKeys] block on
 * the Android Keystore and refuse to run on the main thread
 * ([MainThreadSecretAccessException]); [load] and [save] are the
 * main-safe suspend versions, which run on [ioDispatcher].
 *
 * Migration: nothing moves on its own. [migrateLegacyKeys] moves the Claude
 * key from its old, misleading name [LEGACY_CLAUDE_KEY]
 * (`FLIGHTRISK_API_KEY`, which on the ground means the dashboard key) to
 * [SecretName.CLAUDE_API_KEY]. Call it only in the same change that moves
 * every reader and writer of the old name to [SecretName.CLAUDE_API_KEY]
 * (today `FlightRiskConfig.fromPreferences` reads it and `MainActivity`
 * writes it); running it while old code still writes the old name would let
 * a later run delete a freshly typed key.
 */
class SecretStore(
    private val storage: SecretStoragePort,
    private val isMainThread: () -> Boolean = ::onAndroidMainThread,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
) {

    /** The Android store: EncryptedSharedPreferences in the app's existing secure preferences file. */
    constructor(context: Context) : this(EncryptedPrefsSecretStorage(context))

    /** The secret [name], or null when none is stored or the storage is unavailable. Not on the main thread. */
    fun get(name: SecretName): String? {
        requireOffMain()
        return storage.read(name.storageKey)?.takeIf { it.isNotEmpty() }
    }

    /**
     * Stores [value] under [name]. Surrounding whitespace is removed; a null,
     * empty or blank value removes the secret. Returns true when stored. Not
     * on the main thread.
     */
    fun set(name: SecretName, value: String?): Boolean {
        requireOffMain()
        val cleaned = value?.trim()?.takeIf { it.isNotEmpty() }
        return storage.write(mapOf(name.storageKey to cleaned))
    }

    /** True when a value is stored under [name]. Not on the main thread. */
    fun has(name: SecretName): Boolean = get(name) != null

    /** Removes the secret [name]. Not on the main thread. */
    fun clear(name: SecretName): Boolean = set(name, null)

    /** [get] on the IO dispatcher; safe to call from the main thread. */
    suspend fun load(name: SecretName): String? = withContext(ioDispatcher) { get(name) }

    /** [set] on the IO dispatcher; safe to call from the main thread. */
    suspend fun save(name: SecretName, value: String?): Boolean = withContext(ioDispatcher) { set(name, value) }

    /** Whether the storage works (see [SecretAvailability.Unavailable]). */
    fun availability(): SecretAvailability = storage.availability()

    /**
     * Every stored secret value, for [Redact.text]. Pass the result to the
     * redaction helper only; never log or display it. Not on the main thread.
     */
    fun valuesForRedaction(): List<String> = SecretName.entries.mapNotNull { get(it) }

    /**
     * The secret [name], telling a missing value apart from storage that cannot
     * be read. Not on the main thread.
     */
    fun lookup(name: SecretName): SecretRead {
        requireOffMain()
        return when (val read = storage.readResult(name.storageKey)) {
            is SecretRead.Value -> if (read.value.isEmpty()) SecretRead.Absent else read
            else -> read
        }
    }

    /**
     * Moves the Claude key from [LEGACY_CLAUDE_KEY] to
     * [SecretName.CLAUDE_API_KEY] in one commit: the new name is written and
     * the old one removed together, so a crash leaves either the old state or
     * the new one. When both names hold a value, the new name wins. Returns
     * true only when there was nothing to move or the move was stored; when the
     * storage cannot be opened or read it returns false and changes nothing,
     * so the caller tries again later instead of recording the move as done.
     * Not on the main thread; see the class comment for when to call it.
     */
    fun migrateLegacyKeys(): Boolean {
        requireOffMain()
        if (storage.availability() is SecretAvailability.Unavailable) return false
        val legacy = when (val read = storage.readResult(LEGACY_CLAUDE_KEY)) {
            SecretRead.Absent -> return true
            SecretRead.Unreadable -> return false
            is SecretRead.Value -> read.value
        }
        val current = when (val read = storage.readResult(SecretName.CLAUDE_API_KEY.storageKey)) {
            SecretRead.Unreadable -> return false
            SecretRead.Absent -> null
            is SecretRead.Value -> read.value
        }
        val changes = linkedMapOf<String, String?>()
        if (current.isNullOrEmpty() && legacy.isNotBlank()) {
            changes[SecretName.CLAUDE_API_KEY.storageKey] = legacy.trim()
        }
        changes[LEGACY_CLAUDE_KEY] = null
        return storage.write(changes)
    }

    override fun toString(): String = "SecretStore(${Redact.PLACEHOLDER})"

    private fun requireOffMain() {
        if (isMainThread()) throw MainThreadSecretAccessException()
    }

    companion object {
        /** The old preference name of the Claude key, removed by [migrateLegacyKeys]. */
        const val LEGACY_CLAUDE_KEY = "FLIGHTRISK_API_KEY"
    }
}

/** True on Android's main thread; false on the JVM, where the Looper stubs throw. */
internal fun onAndroidMainThread(): Boolean =
    try {
        val main = Looper.getMainLooper()
        main != null && main.thread === Thread.currentThread()
    } catch (_: RuntimeException) {
        false
    }
