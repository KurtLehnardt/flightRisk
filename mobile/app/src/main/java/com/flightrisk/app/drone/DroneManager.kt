package com.flightrisk.app.drone

import android.content.Context
import android.util.Log
import com.flightrisk.app.config.FlightRiskConfig
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Facade that owns [TelloConnection], [TelloFrameSource], and [TelloWifiChecker]
 * and coordinates them into a single lifecycle-aware entry point.
 *
 * DroneManager is created by [MainActivity] when the user taps "Connect to Drone"
 * and destroyed on disconnect or activity teardown. It monitors telemetry for
 * critical conditions (low battery, crash, connection loss, frozen stream) and
 * emits [DroneAlert]s that the UI layer can observe.
 *
 * @param context Android context for WiFi checking.
 * @param config  App-wide configuration (provides [DroneConfig]).
 */
class DroneManager(
    private val context: Context,
    private val config: FlightRiskConfig,
) {

    companion object {
        private const val TAG = "DroneManager"
    }

    val wifiChecker = TelloWifiChecker(context)
    val connection = TelloConnection(config.drone)
    val frameSource = TelloFrameSource(config.drone)

    /** Observable drone state for UI binding. */
    val droneState: StateFlow<TelloState> = connection.state

    // ------------------------------------------------------------------
    // Alert system
    // ------------------------------------------------------------------

    /** Critical drone alerts emitted to the UI layer. */
    sealed class DroneAlert {
        /** Connection to Tello was lost unexpectedly. */
        data object ConnectionLost : DroneAlert()

        /** Battery is at or below the critical threshold. */
        data class BatteryCritical(val percent: Int) : DroneAlert()

        /** Crash detected (height dropped to 0 while flying). */
        data object CrashDetected : DroneAlert()

        /** Video stream froze (identical frames exceeded threshold). */
        data object StreamFrozen : DroneAlert()
    }

    private val _alerts = MutableSharedFlow<DroneAlert>(extraBufferCapacity = 8)
    val alerts: SharedFlow<DroneAlert> = _alerts.asSharedFlow()

    // ------------------------------------------------------------------
    // Coroutine management
    // ------------------------------------------------------------------

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var monitorJob: Job? = null
    @Volatile
    private var commandedLanding = false

    // ------------------------------------------------------------------
    // Connect / disconnect
    // ------------------------------------------------------------------

    /**
     * Check WiFi, connect to the Tello, enable the video stream, and start
     * monitoring telemetry for critical alerts.
     *
     * @return true if the full connection + stream setup succeeded.
     */
    suspend fun connectAndStream(): Boolean {
        // WiFi gate
        val wifiStatus = wifiChecker.check()
        if (wifiStatus !is TelloWifiChecker.WifiStatus.OnTelloWifi) {
            val guidance = wifiChecker.getGuidanceMessage(wifiStatus)
            Log.w(TAG, "Not on Tello WiFi: $guidance")
            return false
        }

        // SDK connect
        val connected = connection.connect()
        if (!connected) {
            Log.e(TAG, "Connection failed")
            return false
        }

        // Enable video stream
        val streaming = connection.startStream()
        if (!streaming) {
            Log.e(TAG, "Stream start failed")
            connection.disconnect()
            return false
        }

        // Wire frozen-stream recovery before start to avoid missing early freezes
        frameSource.onStreamFrozen = { recoverStream() }

        // Start frame source
        frameSource.start()

        // Start monitoring coroutine
        startMonitoring()

        Log.i(TAG, "Connected and streaming")
        return true
    }

    /**
     * Disconnect from the Tello and release all resources.
     *
     * Suspend so callers await full teardown before reconnecting,
     * preventing port races on UDP 8889.
     */
    suspend fun disconnect() {
        monitorJob?.cancel()
        monitorJob = null

        // Safety: land first if still flying
        val state = droneState.value
        if (state.telemetry.isFlying) {
            Log.w(TAG, "Disconnecting while flying -- attempting to land first")
            try {
                withTimeoutOrNull(3000) {
                    connection.land()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Land attempt during disconnect failed: ${e.message}")
            }
        }

        frameSource.onStreamFrozen = null
        frameSource.stop()
        try {
            connection.stopStream()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(TAG, "Error stopping stream: ${e.message}")
        }
        connection.disconnect()
        Log.i(TAG, "Disconnected")
    }

    /**
     * Permanently cancel the coroutine scope. Call only when this
     * DroneManager instance will never be used again (e.g. activity destroyed).
     */
    fun shutdown() {
        scope.cancel()
        Log.i(TAG, "Shutdown complete")
    }

    // ------------------------------------------------------------------
    // Stream recovery
    // ------------------------------------------------------------------

    /**
     * Recover a frozen video stream by cycling streamon/off.
     *
     * Called by [TelloFrameSource.onStreamFrozen] when identical frames
     * exceed the threshold.
     */
    private fun recoverStream() {
        scope.launch {
            withContext(Dispatchers.IO) {
                Log.w(TAG, "Recovering frozen stream")
                frameSource.stop()
                connection.sendCommand("streamoff")
                delay(500)
                connection.sendCommand("streamon")
                delay(200)
                frameSource.onStreamFrozen = { recoverStream() }
                frameSource.start()
                _alerts.tryEmit(DroneAlert.StreamFrozen)
            }
        }
    }

    // ------------------------------------------------------------------
    // Lifecycle hooks
    // ------------------------------------------------------------------

    /**
     * Called when the activity pauses. If the drone is flying, hover in
     * place so it doesn't drift while the user is in another app.
     */
    fun onActivityPause() {
        val state = droneState.value
        if (state.telemetry.isFlying) {
            Log.i(TAG, "Activity pausing while flying -- hovering")
            scope.launch { connection.hover() }
        }
    }

    /**
     * Called when the activity is destroyed. Safety net: land the drone
     * and disconnect synchronously (cannot suspend on the main thread).
     */
    fun onActivityDestroy() {
        monitorJob?.cancel()
        monitorJob = null
        val state = droneState.value
        Thread {
            frameSource.stop()  // Blocking join happens off main thread
            if (state.telemetry.isFlying) {
                Log.w(TAG, "Activity destroying while flying -- emergency landing")
                try {
                    runBlocking(Dispatchers.IO) {
                        withTimeoutOrNull(1500) {
                            connection.land()
                        }
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Emergency land failed: ${e.message}")
                }
            }
            connection.disconnect()
            scope.cancel()
        }.start()
        Log.i(TAG, "Destroyed")
    }

    // ------------------------------------------------------------------
    // Flight command delegates
    // ------------------------------------------------------------------

    /** Command the drone to take off. */
    suspend fun takeoff() = connection.takeoff()

    /** Command the drone to land. */
    suspend fun land(): Boolean {
        commandedLanding = true
        return connection.land()
        // Don't reset commandedLanding on failure — the monitoring loop will
        // clear it once it observes !isFlying, preventing false crash alerts
        // from lost UDP acks
    }

    /**
     * Move the drone in a cardinal direction.
     *
     * @param direction One of "forward", "back", "left", "right", "up", "down".
     * @param distanceCm Distance in centimeters (clamped to 20..500).
     */
    suspend fun move(direction: String, distanceCm: Int) =
        connection.move(direction, distanceCm)

    /**
     * Rotate the drone.
     *
     * @param degrees Positive for clockwise, negative for counter-clockwise.
     */
    suspend fun rotate(degrees: Int) = connection.rotate(degrees)

    /**
     * Send RC joystick values.
     *
     * @param lr   Left/right (-100 to 100).
     * @param fb   Forward/backward (-100 to 100).
     * @param ud   Up/down (-100 to 100).
     * @param yaw  Yaw (-100 to 100).
     */
    suspend fun rcControl(lr: Int, fb: Int, ud: Int, yaw: Int) =
        connection.rcControl(lr, fb, ud, yaw)

    /** Stop all movement and hover in place. */
    suspend fun hover() = connection.hover()

    /**
     * Emergency motor stop. Use only as a last resort — cuts motors immediately.
     */
    suspend fun emergencyStop(): Boolean {
        commandedLanding = true
        return connection.emergencyStop()
    }

    // ------------------------------------------------------------------
    // Search pattern execution
    // ------------------------------------------------------------------

    private var searchJob: Job? = null

    @Volatile
    var searchActive: Boolean = false
        private set

    @Volatile
    var searchProgress: Pair<Int, Int> = Pair(0, 0)
        private set

    fun startSearchPattern(pattern: PatternType = PatternType.EXPANDING_SQUARE) {
        if (searchActive) return
        val waypoints = SearchPattern.generate(pattern)
        searchActive = true
        searchProgress = Pair(0, waypoints.size)
        Log.i(TAG, "Starting search pattern: ${pattern.displayName}, ${waypoints.size} waypoints")

        searchJob = scope.launch {
            for ((index, wp) in waypoints.withIndex()) {
                if (!searchActive) break

                searchProgress = Pair(index + 1, waypoints.size)

                try {
                    if (wp.distanceCm > 0) {
                        connection.move(wp.direction, wp.distanceCm)
                        delay(500)
                    }
                    if (wp.rotateDegrees != 0) {
                        connection.rotate(wp.rotateDegrees)
                        delay(500)
                    }
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    Log.e(TAG, "Search pattern error at waypoint ${index + 1}: ${e.message}")
                    break
                }
            }

            searchActive = false
            searchProgress = Pair(0, 0)
            Log.i(TAG, "Search pattern complete")
        }
    }

    fun stopSearchPattern() {
        searchActive = false
        searchJob?.cancel()
        searchJob = null
        searchProgress = Pair(0, 0)
        scope.launch {
            try { connection.hover() } catch (_: Exception) {}
        }
        Log.i(TAG, "Search pattern stopped")
    }

    // ------------------------------------------------------------------
    // Telemetry monitoring
    // ------------------------------------------------------------------

    /**
     * Start a coroutine that observes [connection.state] and emits
     * [DroneAlert]s for critical conditions.
     */
    private fun startMonitoring() {
        monitorJob?.cancel()
        monitorJob = scope.launch {
            var wasConnected = true
            var wasFlying = false
            var batteryAlertSent = false

            connection.state.collect { state ->
                // Connection loss detection
                val isConnected = state.connectionState != TelloConnectionState.DISCONNECTED &&
                    state.connectionState != TelloConnectionState.ERROR
                if (wasConnected && !isConnected) {
                    Log.e(TAG, "Connection lost")
                    _alerts.tryEmit(DroneAlert.ConnectionLost)
                }
                wasConnected = isConnected

                // Battery critical (null = no reading yet, skip check)
                val battery = state.telemetry.battery
                if (battery != null && battery in 0..config.drone.batteryCriticalThreshold && !batteryAlertSent) {
                    Log.w(TAG, "Battery critical: $battery%")
                    _alerts.tryEmit(DroneAlert.BatteryCritical(battery))
                    batteryAlertSent = true
                    // Auto-land on critical battery
                    if (state.telemetry.isFlying) {
                        Log.w(TAG, "Auto-landing due to critical battery")
                        land()  // NOT connection.land() — sets commandedLanding flag
                    }
                }
                if (battery != null && battery > config.drone.batteryCriticalThreshold) {
                    batteryAlertSent = false
                }

                // Crash detection: was flying but TelloConnection flipped
                // isFlying to false (after 3 consecutive zero-height polls).
                // Skip if this was a commanded landing.
                if (wasFlying && !state.telemetry.isFlying) {
                    if (state.telemetry.height == 0 && isConnected && !commandedLanding) {
                        Log.e(TAG, "Crash detected")
                        _alerts.tryEmit(DroneAlert.CrashDetected)
                    }
                    commandedLanding = false  // Reset after processing, regardless of crash or normal landing
                }
                wasFlying = state.telemetry.isFlying
            }
        }
    }
}
