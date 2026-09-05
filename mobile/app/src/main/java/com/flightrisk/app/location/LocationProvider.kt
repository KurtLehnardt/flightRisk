package com.flightrisk.app.location

import android.annotation.SuppressLint
import android.content.Context
import android.location.Location
import android.os.Looper
import android.util.Log
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * GPS location provider for tagging match events with coordinates.
 *
 * Uses Google Play Services [FusedLocationProviderClient] for
 * efficient, battery-friendly location updates during search.
 * The last known location is cached thread-safely and attached
 * to every [com.flightrisk.app.pipeline.MatchEntry].
 *
 * Callers must ensure `ACCESS_FINE_LOCATION` permission is granted
 * before calling [startUpdates]. The provider gracefully handles
 * unavailable location (returns null).
 *
 * @param context Android application context.
 * @param updateIntervalMs Location update interval in milliseconds.
 *   Default 5000 (5 seconds) balances accuracy vs. battery during
 *   active search.
 */
class LocationProvider(
    context: Context,
    private val updateIntervalMs: Long = 5_000L,
) {

    companion object {
        private const val TAG = "LocationProvider"
    }

    private val fusedClient: FusedLocationProviderClient =
        LocationServices.getFusedLocationProviderClient(context)

    private val lastLocation = AtomicReference<Location?>(null)
    private val isUpdating = AtomicBoolean(false)

    private val locationCallback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            result.lastLocation?.let { location ->
                lastLocation.set(location)
                Log.d(
                    TAG,
                    "Location updated: ${location.latitude}, ${location.longitude} " +
                        "(accuracy: ${location.accuracy}m)"
                )
            }
        }
    }

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    /**
     * Get the most recent known location.
     *
     * Returns the last location from continuous updates if active,
     * or attempts to fetch the device's last known location as a
     * fallback. May return null if location is unavailable.
     *
     * Thread-safe.
     */
    @SuppressLint("MissingPermission")
    fun getCurrentLocation(): Location? {
        val cached = lastLocation.get()
        if (cached != null) return cached

        // Try to get last known location as a one-shot fallback
        try {
            fusedClient.lastLocation.addOnSuccessListener { location ->
                if (location != null) {
                    lastLocation.compareAndSet(null, location)
                }
            }
        } catch (e: SecurityException) {
            Log.w(TAG, "Location permission not granted", e)
        }

        return lastLocation.get()
    }

    /**
     * Start continuous location updates during active search.
     *
     * Safe to call multiple times; only the first call starts updates.
     * Requires `ACCESS_FINE_LOCATION` permission to be already granted.
     */
    @SuppressLint("MissingPermission")
    fun startUpdates() {
        if (!isUpdating.compareAndSet(false, true)) {
            Log.d(TAG, "Location updates already active")
            return
        }

        try {
            val request = LocationRequest.Builder(
                Priority.PRIORITY_HIGH_ACCURACY,
                updateIntervalMs,
            ).apply {
                setMinUpdateIntervalMillis(updateIntervalMs / 2)
                setWaitForAccurateLocation(false)
            }.build()

            fusedClient.requestLocationUpdates(
                request,
                locationCallback,
                Looper.getMainLooper(),
            )
            Log.i(TAG, "Location updates started (interval: ${updateIntervalMs}ms)")
        } catch (e: SecurityException) {
            Log.w(TAG, "Location permission not granted; updates not started", e)
            isUpdating.set(false)
        }
    }

    /**
     * Stop continuous location updates.
     *
     * The last known location remains cached and available via
     * [getCurrentLocation] after stopping.
     */
    fun stopUpdates() {
        if (!isUpdating.compareAndSet(true, false)) return

        fusedClient.removeLocationUpdates(locationCallback)
        Log.i(TAG, "Location updates stopped")
    }

    /**
     * Whether continuous updates are currently active.
     */
    val isActive: Boolean
        get() = isUpdating.get()
}
