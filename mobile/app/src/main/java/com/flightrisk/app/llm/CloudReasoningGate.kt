package com.flightrisk.app.llm

import com.flightrisk.app.ground.Redact
import com.flightrisk.app.ground.SecretRead
import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.ground.SecretStore
import java.util.concurrent.atomic.AtomicInteger

/** The result of reading the Claude key. [toString] never shows the key. */
sealed interface ClaudeKeyLookup {
    class Found(val key: String) : ClaudeKeyLookup {
        override fun toString(): String = "Found(${Redact.PLACEHOLDER})"
    }

    data object Missing : ClaudeKeyLookup

    /** The secure storage could not be opened or read (for example a Keystore failure). */
    data object Unavailable : ClaudeKeyLookup
}

/** What the app last learned about the saved key. Holds no key material. */
enum class ClaudeKeyState { UNKNOWN, FOUND, MISSING, UNAVAILABLE }

/**
 * Where the Claude key lives. Every method may block on the Keystore and
 * must run off the main thread.
 */
interface ClaudeKeySource {
    fun lookup(): ClaudeKeyLookup
    fun save(key: String): Boolean
    fun remove(): Boolean
}

/** [ClaudeKeySource] over [SecretStore] ([SecretName.CLAUDE_API_KEY]). */
class SecretStoreClaudeKeySource(private val secrets: SecretStore) : ClaudeKeySource {
    override fun lookup(): ClaudeKeyLookup =
        // A storage that cannot be opened or read is reported as such, never as "no key".
        when (val read = secrets.lookup(SecretName.CLAUDE_API_KEY)) {
            SecretRead.Unreadable -> ClaudeKeyLookup.Unavailable
            SecretRead.Absent -> ClaudeKeyLookup.Missing
            is SecretRead.Value -> ReasoningSettings.normalizedKey(read.value)?.let { ClaudeKeyLookup.Found(it) }
                ?: ClaudeKeyLookup.Missing
        }

    override fun save(key: String): Boolean = secrets.set(SecretName.CLAUDE_API_KEY, key)

    override fun remove(): Boolean = secrets.clear(SecretName.CLAUDE_API_KEY)

    override fun toString(): String = "SecretStoreClaudeKeySource"
}

/**
 * The cloud gate: the single place that decides whether an image may leave the
 * device right now. It yields the Claude key only while the saved opt-in is on,
 * Cloud Claude is the selected provider, no key removal is in progress and a
 * non-empty key can be read from [ClaudeKeySource]. It reads all of them at
 * call time, so turning cloud off or removing the key takes effect for the
 * next check without waiting for any refresh.
 *
 * Threading: [isAllowedBySettings], [keyState] and [addListener] are main-safe.
 * [keyIfOpen], [refreshKeyState], [saveKey] and [removeKey] block on the
 * Keystore and must run off the main thread.
 */
class CloudReasoningGate(
    private val settings: ReasoningSettings,
    private val keys: ClaudeKeySource,
) {
    private val removalsInProgress = AtomicInteger(0)

    @Volatile
    var keyState: ClaudeKeyState = ClaudeKeyState.UNKNOWN
        private set

    /** Main-safe: true while the opt-in is on, Cloud Claude is selected and no key removal is running. */
    fun isAllowedBySettings(): Boolean = removalsInProgress.get() == 0 && settings.cloudAllowedBySettings

    /** Registers [listener] for changes that may close the gate (opt-out, provider switch, key removal). */
    fun addListener(listener: CloudGateListener) = settings.addGateListener(listener)

    /** Removes a listener added with [addListener]. */
    fun removeListener(listener: CloudGateListener) = settings.removeGateListener(listener)

    /**
     * The key, or null when cloud use is not allowed right now. Blocking.
     * The settings are checked before and after the Keystore read, so an
     * opt-out that lands during the read still closes the gate.
     */
    fun keyIfOpen(): String? {
        if (!isAllowedBySettings()) return null
        val lookup = keys.lookup()
        keyState = lookup.toState()
        val key = (lookup as? ClaudeKeyLookup.Found)?.key?.takeIf { it.isNotBlank() } ?: return null
        if (!isAllowedBySettings()) return null
        return key
    }

    /** Re-reads whether a key is saved (not whether cloud is allowed). Blocking. */
    fun refreshKeyState(): ClaudeKeyState {
        val state = keys.lookup().toState()
        keyState = state
        return state
    }

    /**
     * Saves a trimmed key. Refused (false) unless the opt-in is on, because the
     * key field is only reachable after consent. Blocking.
     */
    fun saveKey(input: String): Boolean {
        if (!settings.cloudOptIn) return false
        val key = ReasoningSettings.normalizedKey(input) ?: return false
        val saved = keys.save(key)
        refreshKeyState()
        return saved
    }

    /**
     * Removes the saved key. The gate is closed and every cloud request is
     * cancelled before the delete starts, and again after it, so no request
     * can pick up the key while it is being removed. Blocking.
     */
    fun removeKey(): Boolean {
        removalsInProgress.incrementAndGet()
        try {
            settings.notifyGateMayHaveClosed(keyRemoved = true)
            return keys.remove()
        } finally {
            refreshKeyState()
            removalsInProgress.decrementAndGet()
            settings.notifyGateMayHaveClosed(keyRemoved = true)
        }
    }

    override fun toString(): String = "CloudReasoningGate(keyState=$keyState, allowed=${isAllowedBySettings()})"

    private fun ClaudeKeyLookup.toState(): ClaudeKeyState = when (this) {
        is ClaudeKeyLookup.Found -> ClaudeKeyState.FOUND
        ClaudeKeyLookup.Missing -> ClaudeKeyState.MISSING
        ClaudeKeyLookup.Unavailable -> ClaudeKeyState.UNAVAILABLE
    }
}
