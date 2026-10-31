package com.flightrisk.app.llm

import android.content.Context
import android.content.SharedPreferences
import com.flightrisk.app.ground.SecretName
import com.flightrisk.app.ground.SecretStore
import java.util.concurrent.CopyOnWriteArrayList

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

/**
 * The reasoning providers the phone offers in Settings.
 *
 * They mirror the Python providers in `flightrisk/reasoning/__init__.py`
 * (`gemma`, `claude`, `off`) and the iOS `ReasoningProvider`. The stored
 * values are the strings earlier Android builds already used for the radio
 * buttons and must not change.
 */
enum class ReasoningProvider(val storedValue: String, val displayName: String) {
    LOCAL("local", "Local Model (On-Device)"),
    CLOUD_CLAUDE("cloud_claude", "Cloud Claude"),
    OFF("none", "None (Vision Only)"),
    ;

    /** True for providers that send images off the device. */
    val isCloud: Boolean get() = this == CLOUD_CLAUDE

    companion object {
        /**
         * Parses a stored value, including the spellings used by the Python
         * side and by iOS. Unknown values (for example "cloud_gemini" or
         * "cloud_openai") return null.
         */
        fun fromStored(raw: String?): ReasoningProvider? =
            when (raw?.trim()?.lowercase()) {
                "local", "local_gemma", "gemma", "ollama" -> LOCAL
                "cloud_claude", "claude" -> CLOUD_CLAUDE
                "none", "off", "disabled" -> OFF
                else -> null
            }

        /**
         * Maps an [LlmBackend.name] to its provider. The no-op backend and
         * unknown names return null, so an unknown backend can never be taken
         * for the cloud provider.
         */
        fun fromBackendName(name: String): ReasoningProvider? =
            when {
                name == CloudClaudeLlmBackend.NAME -> CLOUD_CLAUDE
                name.startsWith("local_") -> LOCAL
                else -> null
            }
    }
}

// ---------------------------------------------------------------------------
// Reason texts
// ---------------------------------------------------------------------------

/**
 * User-facing reasons why a provider cannot be used right now. The
 * cloud-blocked text mirrors the dashboard's `cloud_block_reason` and the
 * iOS `ReasoningReason`, with the phone's opt-in control named in place of
 * the ground's environment variable.
 */
object ReasoningReason {
    const val CLOUD_BLOCKED_PREFIX =
        "Cloud reasoning is not enabled. Claude would send person crops and the reference photo to Anthropic;"
    const val CLOUD_BLOCKED = "$CLOUD_BLOCKED_PREFIX turn on \"Allow cloud reasoning\" to opt in."
    const val CLAUDE_KEY_MISSING = "No Claude API key is saved on this device."
    const val SECURE_STORAGE_UNAVAILABLE = "Secure storage unavailable on this device"
    const val OFFLINE = "No internet connection."
    const val CLAUDE_NOT_REGISTERED = "The Claude backend is not loaded."
    const val LOCAL_NOT_READY = "The on-device model is not downloaded yet."
}

/** The consent text shown before cloud reasoning can be turned on. The same words as iOS. */
object CloudConsentText {
    const val TITLE = "Send images to Anthropic?"
    const val CONFIRM = "Allow Cloud Reasoning"
    const val CANCEL = "Cancel"
    const val MESSAGE =
        "With cloud reasoning on, the cropped image of each detected person, the reference photo you chose " +
            "and the text description you entered are sent to Anthropic (Claude) for analysis. The images can " +
            "show a face, and Anthropic's privacy policy applies to them. Nothing is sent until you also save a " +
            "Claude API key and select Cloud Claude. You can turn this off at any time here; turning it off " +
            "stops sending at once and cancels any request still in progress."
}

// ---------------------------------------------------------------------------
// Persisted settings
// ---------------------------------------------------------------------------

/** Called whenever a change may have closed the cloud gate. */
fun interface CloudGateListener {
    /**
     * @param keyRemoved true when the Claude key is being removed, so every
     *   cloud request must be cancelled whatever the settings say.
     */
    fun onGateMayHaveClosed(keyRemoved: Boolean)
}

/**
 * Persisted reasoning settings: the provider the user picked and the explicit
 * cloud opt-in. Stored in the plain `flightrisk_config` SharedPreferences
 * (the same file as `FlightRiskConfig`); the Claude key never lives here, it
 * stays in [SecretStore].
 *
 * Rules, the same as iOS and the dashboard:
 * - Cloud is never the default. With nothing stored, the provider is Local
 *   when the on-device model is available and Off otherwise.
 * - The opt-in is false unless the user confirmed the consent dialog. Only a
 *   stored Boolean true counts; a stored string such as "true" does not.
 * - A stored cloud choice without the opt-in (for example the "cloud_claude"
 *   default of earlier builds) is ignored, and [migrateLegacyValues] removes it.
 * - Turning the opt-in off clears a stored cloud choice.
 *
 * Every method reads and writes SharedPreferences only, so it is safe on the
 * main thread. Writes use `apply()`, which updates the in-memory values at
 * once, so the cloud gate sees a change before the method returns.
 */
class ReasoningSettings(private val prefs: SharedPreferences) {

    constructor(context: Context) :
        this(context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE))

    private val listeners = CopyOnWriteArrayList<CloudGateListener>()

    /** Registers [listener] for changes that may close the cloud gate. */
    fun addGateListener(listener: CloudGateListener) {
        listeners.addIfAbsent(listener)
    }

    /** Removes a listener added with [addGateListener]. */
    fun removeGateListener(listener: CloudGateListener) {
        listeners.remove(listener)
    }

    /**
     * Tells every listener, synchronously, that the gate may have closed. The
     * cloud backend cancels its requests in flight before this returns.
     */
    fun notifyGateMayHaveClosed(keyRemoved: Boolean = false) {
        listeners.forEach { it.onGateMayHaveClosed(keyRemoved) }
    }

    /** True only when the user confirmed the consent dialog and a Boolean true is stored. */
    val cloudOptIn: Boolean
        get() = readAll()[CLOUD_OPT_IN_KEY] == true

    /**
     * The provider the user explicitly chose, or null when nothing valid is
     * stored. A stored cloud choice counts only while the opt-in is on.
     */
    val storedProvider: ReasoningProvider?
        get() {
            val all = readAll()
            val provider = ReasoningProvider.fromStored(all[PROVIDER_KEY] as? String) ?: return null
            if (provider.isCloud && all[CLOUD_OPT_IN_KEY] != true) return null
            return provider
        }

    /** The provider the user selected, or the default when none is stored. */
    fun selectedProvider(localModelAvailable: Boolean): ReasoningProvider =
        storedProvider ?: defaultProvider(localModelAvailable)

    /** True while the opt-in is on and Cloud Claude is the selected provider. */
    val cloudAllowedBySettings: Boolean
        get() = storedProvider == ReasoningProvider.CLOUD_CLAUDE

    /**
     * Persists the user's choice. Selecting Cloud Claude is refused (returns
     * false, nothing stored) unless the opt-in is already on, so the consent
     * step can never be skipped.
     */
    fun select(provider: ReasoningProvider): Boolean {
        if (provider.isCloud && !cloudOptIn) return false
        prefs.edit().putString(PROVIDER_KEY, provider.storedValue).apply()
        if (!provider.isCloud) notifyGateMayHaveClosed()
        return true
    }

    /**
     * Records the user's answer to the consent step. Call with true only after
     * the user confirmed the dialog. Turning the opt-in off also clears a
     * stored cloud choice and tells the cloud backend to cancel at once.
     */
    fun setCloudOptIn(enabled: Boolean) {
        val editor = prefs.edit().putBoolean(CLOUD_OPT_IN_KEY, enabled)
        if (!enabled && ReasoningProvider.fromStored(readAll()[PROVIDER_KEY] as? String)?.isCloud == true) {
            editor.remove(PROVIDER_KEY)
        }
        editor.apply()
        if (!enabled) notifyGateMayHaveClosed()
    }

    // ------------------------------------------------------------------
    // Consent decisions
    // ------------------------------------------------------------------

    /** The user picked [provider]. Picking Cloud Claude before opting in asks for consent instead. */
    fun requestProvider(provider: ReasoningProvider): ConsentDecision {
        if (provider.isCloud && !cloudOptIn) return ConsentDecision.AskForConsent(selectCloud = true)
        select(provider)
        return ConsentDecision.Applied
    }

    /** The user flipped "Allow cloud reasoning". On asks for consent; off applies at once. */
    fun requestCloudOptIn(enabled: Boolean): ConsentDecision {
        if (enabled) {
            return if (cloudOptIn) ConsentDecision.Applied else ConsentDecision.AskForConsent(selectCloud = false)
        }
        setCloudOptIn(false)
        return ConsentDecision.Applied
    }

    /** The user confirmed the consent dialog. */
    fun confirmCloudConsent(selectCloud: Boolean) {
        setCloudOptIn(true)
        if (selectCloud) select(ReasoningProvider.CLOUD_CLAUDE)
    }

    // ------------------------------------------------------------------
    // Migration
    // ------------------------------------------------------------------

    /**
     * Cleans up values written by earlier builds. Removes a stored provider
     * that is unknown (for example "cloud_gemini" or "cloud_openai"), removes
     * a stored cloud choice without the opt-in, and removes an opt-in value
     * that is not a Boolean. It never sets the opt-in and never selects cloud.
     * Main-safe and idempotent.
     */
    fun migrateLegacyValues() {
        val all = readAll()
        val editor = prefs.edit()
        var changed = false
        val rawOptIn = all[CLOUD_OPT_IN_KEY]
        val optIn = rawOptIn == true
        if (rawOptIn != null && rawOptIn !is Boolean) {
            editor.remove(CLOUD_OPT_IN_KEY)
            changed = true
        }
        val rawProvider = all[PROVIDER_KEY]
        if (rawProvider != null) {
            val provider = ReasoningProvider.fromStored(rawProvider as? String)
            when {
                provider == null -> { editor.remove(PROVIDER_KEY); changed = true }
                provider.isCloud && !optIn -> { editor.remove(PROVIDER_KEY); changed = true }
                rawProvider != provider.storedValue -> { editor.putString(PROVIDER_KEY, provider.storedValue); changed = true }
            }
        }
        if (changed) editor.apply()
    }

    /**
     * Moves the Claude key to its new name, once. Earlier builds kept it as
     * `FLIGHTRISK_API_KEY`: in this plain preferences file (the oldest builds)
     * and in the encrypted file. This copies a plain-file key into
     * [SecretName.CLAUDE_API_KEY] when that is empty and only then deletes the
     * plain copy, then runs [SecretStore.migrateLegacyKeys], which writes the
     * new name and deletes the old one in one commit. A crash at any point
     * leaves the key under the old name or the new one, never lost; a failed
     * step is retried on the next launch. It never changes the opt-in or the
     * provider. Blocks on the Keystore: call it off the main thread.
     *
     * @return true when the migration is done (now or earlier).
     */
    fun migrateLegacyKeyOnce(secrets: SecretStore): Boolean {
        if (readAll()[KEY_MIGRATION_DONE_KEY] == true) return true
        val plain = (readAll()[SecretStore.LEGACY_CLAUDE_KEY] as? String)?.trim()
        if (plain != null) {
            if (plain.isNotEmpty() && secrets.get(SecretName.CLAUDE_API_KEY) == null) {
                if (!secrets.set(SecretName.CLAUDE_API_KEY, plain)) return false
            }
            if (!prefs.edit().remove(SecretStore.LEGACY_CLAUDE_KEY).commit()) return false
        }
        if (!secrets.migrateLegacyKeys()) return false
        return prefs.edit().putBoolean(KEY_MIGRATION_DONE_KEY, true).commit()
    }

    override fun toString(): String =
        "ReasoningSettings(provider=${storedProvider?.storedValue ?: "default"}, cloudOptIn=$cloudOptIn)"

    private fun readAll(): Map<String, *> =
        try {
            prefs.all ?: emptyMap<String, Any?>()
        } catch (_: RuntimeException) {
            emptyMap<String, Any?>()
        }

    companion object {
        const val PREFS_NAME = "flightrisk_config"
        const val PROVIDER_KEY = "flightrisk_reasoning_provider"
        const val CLOUD_OPT_IN_KEY = "flightrisk_allow_cloud_llm"
        const val KEY_MIGRATION_DONE_KEY = "flightrisk_claude_key_migrated"

        /** The default when nothing is stored: Local if the on-device model is available, otherwise Off. Never cloud. */
        fun defaultProvider(localModelAvailable: Boolean): ReasoningProvider =
            if (localModelAvailable) ReasoningProvider.LOCAL else ReasoningProvider.OFF

        /** Trims a pasted key. Returns null when nothing usable is left. */
        fun normalizedKey(input: String?): String? = input?.trim()?.takeIf { it.isNotEmpty() }
    }
}

/** What Settings should do after a user action. */
sealed interface ConsentDecision {
    /** The change was applied and saved. */
    data object Applied : ConsentDecision

    /** Show the consent dialog; on confirmation call [ReasoningSettings.confirmCloudConsent] with [selectCloud]. */
    data class AskForConsent(val selectCloud: Boolean) : ConsentDecision
}
