package com.flightrisk.app.llm

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The resolved reasoning state: what the user selected, what is active, and
 * why anything is unavailable. It holds no key material, so [toString] is
 * safe to log or show.
 *
 * @property selected The provider the user selected (or the default).
 * @property active The provider answering reasoning requests ([ReasoningProvider.OFF] for none).
 * @property cloudOptIn Whether the user opted in to cloud reasoning.
 * @property keyState What is known about the saved Claude key.
 * @property unavailable Providers that cannot be used now, with the reason to show.
 * @property notice Set when the selected provider could not be used and the
 *   app fell back, so the user is told instead of left guessing.
 */
data class ReasoningStatus(
    val selected: ReasoningProvider = ReasoningProvider.OFF,
    val active: ReasoningProvider = ReasoningProvider.OFF,
    val cloudOptIn: Boolean = false,
    val keyState: ClaudeKeyState = ClaudeKeyState.UNKNOWN,
    val unavailable: Map<ReasoningProvider, String> = emptyMap(),
    val notice: String? = null,
) {
    /** True whenever cloud reasoning is the active backend. Settings and Search show "Cloud active" then. */
    val isCloudActive: Boolean get() = active.isCloud
}

/**
 * Chooses the active [LlmBackend] from the user's reasoning settings and
 * re-evaluates on connectivity changes.
 *
 * Cloud reasoning is opt-in and off by default, as on the dashboard
 * (`flightrisk/reasoning/__init__.py`) and on iOS:
 * - The active backend is the provider the user selected, or the default when
 *   none is stored: Local when the on-device model is available, otherwise
 *   none. The default is never cloud.
 * - Claude is usable only while the explicit opt-in is on, Cloud Claude is
 *   selected and a key is saved in SecretStore. A saved key alone never turns
 *   cloud on. The backend reads all of these itself at call time through
 *   [CloudReasoningGate], so turning cloud off or removing the key stops sending
 *   at once, without waiting for [refresh].
 * - When the selected provider cannot be used, the selector falls back to the
 *   on-device backend, or to [NoOpLlmBackend], and sets a notice in [status]. A
 *   cloud backend is never chosen as a fallback.
 *
 * [refresh] reads only SharedPreferences and the last known key state, so it
 * is safe on any thread; [refreshWithKeyLookup] also re-reads the key and must
 * run off the main thread.
 */
class LlmSelector(
    private val settings: ReasoningSettings,
    private val gate: CloudReasoningGate,
    private val localModelUsable: () -> Boolean = { false },
    private val isOnline: () -> Boolean = { true },
    private val context: Context? = null,
) {

    private val backends = CopyOnWriteArrayList<LlmBackend>()
    private val noOp = NoOpLlmBackend()
    private val lock = Any()

    @Volatile
    private var cachedActive: LlmBackend = noOp
    private val monitoring = AtomicBoolean(false)
    private val mutableStatus = MutableStateFlow(ReasoningStatus())

    /** The resolved state after the last [refresh]. */
    val status: StateFlow<ReasoningStatus> = mutableStatus.asStateFlow()

    init {
        settings.migrateLegacyValues()
    }

    /** Whether a real (non-NoOp) backend is active. */
    val isLlmAvailable: Boolean
        get() = getActiveBackend().name != noOp.name

    /** Names of the registered backends, in registration order. */
    val registeredBackendNames: List<String> get() = backends.map { it.name }

    /** Registers a backend; the fallback walks registration order, so register the on-device backend first. */
    fun registerBackend(backend: LlmBackend) {
        backends.add(backend)
        refresh()
    }

    /** Registers the on-device backend first and the cloud backend second. */
    fun registerOnDeviceFirst(local: LlmBackend, cloud: LlmBackend) {
        registerBackend(local)
        registerBackend(cloud)
    }

    /**
     * The backend that should answer reasoning requests now. When the cached
     * backend is the cloud backend, the gate is checked again first, so a
     * request picked up after an opt-out (before any refresh ran) never gets
     * the cloud backend. Even if it did, the backend's own gate check refuses.
     */
    fun getActiveBackend(): LlmBackend {
        val current = cachedActive
        if (ReasoningProvider.fromBackendName(current.name)?.isCloud == true && !current.isAvailable) {
            refresh()
        }
        return cachedActive
    }

    /** Re-reads the saved key state (blocking, off the main thread), then [refresh]. */
    fun refreshWithKeyLookup() {
        gate.refreshKeyState()
        refresh()
    }

    /** Re-evaluates the active backend and [status] from the settings and the last known key state. */
    fun refresh() {
        synchronized(lock) {
            val localBackend = backends.firstOrNull { ReasoningProvider.fromBackendName(it.name) == ReasoningProvider.LOCAL }
            val cloudBackend = backends.firstOrNull { ReasoningProvider.fromBackendName(it.name)?.isCloud == true }

            val optIn = settings.cloudOptIn
            val keyState = gate.keyState
            val online = isOnline()

            val unavailable = linkedMapOf<ReasoningProvider, String>()
            when {
                !optIn -> unavailable[ReasoningProvider.CLOUD_CLAUDE] = ReasoningReason.CLOUD_BLOCKED
                keyState == ClaudeKeyState.UNAVAILABLE ->
                    unavailable[ReasoningProvider.CLOUD_CLAUDE] = ReasoningReason.SECURE_STORAGE_UNAVAILABLE
                keyState != ClaudeKeyState.FOUND -> unavailable[ReasoningProvider.CLOUD_CLAUDE] = ReasoningReason.CLAUDE_KEY_MISSING
                cloudBackend == null -> unavailable[ReasoningProvider.CLOUD_CLAUDE] = ReasoningReason.CLAUDE_NOT_REGISTERED
                !online -> unavailable[ReasoningProvider.CLOUD_CLAUDE] = ReasoningReason.OFFLINE
            }

            val localAvailable = localBackend != null && (localBackend.isAvailable || localModelUsable())
            if (!localAvailable) unavailable[ReasoningProvider.LOCAL] = ReasoningReason.LOCAL_NOT_READY

            val selected = settings.selectedProvider(localAvailable)
            var active: LlmBackend = noOp
            var activeProvider = ReasoningProvider.OFF
            var notice: String? = null

            when (selected) {
                ReasoningProvider.OFF -> Unit
                ReasoningProvider.LOCAL ->
                    if (localBackend != null && localAvailable) {
                        active = localBackend
                        activeProvider = ReasoningProvider.LOCAL
                    } else {
                        notice = "The on-device model is selected, but it is not downloaded yet. Reasoning is off until it is ready."
                    }
                ReasoningProvider.CLOUD_CLAUDE -> {
                    val reason = unavailable[ReasoningProvider.CLOUD_CLAUDE]
                    if (reason == null && cloudBackend != null && cloudBackend.isAvailable) {
                        active = cloudBackend
                        activeProvider = ReasoningProvider.CLOUD_CLAUDE
                    } else {
                        val why = reason ?: ReasoningReason.CLAUDE_NOT_REGISTERED
                        if (localBackend != null && localAvailable) {
                            active = localBackend
                            activeProvider = ReasoningProvider.LOCAL
                            notice = "Cloud Claude is unavailable: $why Using the on-device model instead."
                        } else {
                            notice = "Cloud Claude is unavailable: $why Reasoning is off."
                        }
                    }
                }
            }

            val previous = cachedActive
            cachedActive = active
            if (previous.name != active.name) {
                log("Active backend changed: ${previous.name} -> ${active.name}")
                if (activeProvider.isCloud) {
                    log("Cloud reasoning is on: person crops and the reference photo are sent to Anthropic.")
                }
            }

            mutableStatus.value = ReasoningStatus(
                selected = selected,
                active = activeProvider,
                cloudOptIn = optIn,
                keyState = keyState,
                unavailable = unavailable,
                notice = notice,
            )
        }
    }

    /**
     * Starts monitoring connectivity; [refresh] runs on every change. Safe to
     * call more than once. Does nothing without a [context].
     */
    fun startMonitoring() {
        val ctx = context ?: return
        if (!monitoring.compareAndSet(false, true)) return

        val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        if (cm == null) {
            log("ConnectivityManager unavailable; monitoring disabled")
            monitoring.set(false)
            return
        }
        val request = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        cm.registerNetworkCallback(request, object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = refresh()
            override fun onLost(network: Network) = refresh()
            override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) = refresh()
        })
    }

    /** Removes all backends and resets to NoOp. Primarily for tests. */
    fun clear() {
        backends.clear()
        cachedActive = noOp
        refresh()
    }

    override fun toString(): String = "LlmSelector(active=${cachedActive.name}, status=${mutableStatus.value})"

    private fun log(message: String) {
        try {
            Log.i(TAG, message)
        } catch (_: RuntimeException) {
            // android.util.Log is a stub on the JVM.
        }
    }

    companion object {
        private const val TAG = "LlmSelector"
    }
}
