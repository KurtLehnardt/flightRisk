package com.flightrisk.app.llm

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * Manages available [LlmBackend] instances with priority-based
 * auto-selection and connectivity monitoring.
 *
 * Backends are tried in registration order (highest priority first).
 * The first backend whose [LlmBackend.isAvailable] returns true is the
 * active backend. A [NoOpLlmBackend] is always appended as the
 * last-resort fallback.
 *
 * When the device loses internet (e.g. switching to Tello WiFi), the
 * selector automatically re-evaluates and falls back. When internet
 * returns, it re-evaluates and may promote a cloud backend.
 *
 * Thread-safe: the backend list uses [CopyOnWriteArrayList], the
 * cached active backend uses [AtomicReference], and the connectivity
 * callback runs on the system handler thread.
 *
 * @param context Android application context (for [ConnectivityManager]).
 */
class LlmSelector(private val context: Context) {

    companion object {
        private const val TAG = "LlmSelector"
    }

    private val backends = CopyOnWriteArrayList<LlmBackend>()
    private val noOp = NoOpLlmBackend()
    private val cachedActive = AtomicReference<LlmBackend>(noOp)
    private val monitoring = AtomicBoolean(false)

    /**
     * Whether a real (non-NoOp) LLM backend is currently available.
     */
    val isLlmAvailable: Boolean
        get() = getActiveBackend().name != "none"

    /**
     * Register a backend. Backends are checked in registration order,
     * so register the highest-priority backend first.
     */
    fun registerBackend(backend: LlmBackend) {
        backends.add(backend)
        refresh()
        Log.d(TAG, "Registered backend: ${backend.name}")
    }

    /**
     * Return the highest-priority available backend, falling back to
     * [NoOpLlmBackend] if none are available.
     */
    fun getActiveBackend(): LlmBackend {
        return cachedActive.get()
    }

    /**
     * Re-evaluate backend availability and update the cached active
     * backend. Called automatically on connectivity changes.
     */
    fun refresh() {
        val active = backends.firstOrNull { it.isAvailable } ?: noOp
        val previous = cachedActive.getAndSet(active)
        if (previous.name != active.name) {
            Log.i(TAG, "Active backend changed: ${previous.name} -> ${active.name}")
        }
    }

    /**
     * Start monitoring network connectivity changes. When internet is
     * lost or regained, [refresh] is called automatically.
     *
     * Safe to call multiple times; only the first call registers the
     * callback.
     */
    fun startMonitoring() {
        if (!monitoring.compareAndSet(false, true)) return

        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        if (cm == null) {
            Log.w(TAG, "ConnectivityManager unavailable; monitoring disabled")
            monitoring.set(false)
            return
        }

        val request = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()

        cm.registerNetworkCallback(request, object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                Log.d(TAG, "Network available")
                refresh()
            }

            override fun onLost(network: Network) {
                Log.d(TAG, "Network lost")
                refresh()
            }

            override fun onCapabilitiesChanged(
                network: Network,
                capabilities: NetworkCapabilities,
            ) {
                refresh()
            }
        })

        Log.d(TAG, "Connectivity monitoring started")
    }

    /**
     * Remove all registered backends and reset to NoOp.
     * Primarily for testing.
     */
    fun clear() {
        backends.clear()
        cachedActive.set(noOp)
    }
}
