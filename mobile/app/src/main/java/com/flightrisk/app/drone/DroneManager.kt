package com.flightrisk.app.drone

import android.content.Context
import android.util.Log
import com.flightrisk.app.config.FlightRiskConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

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
            Log.w(TAG, "Stream start failed, continuing without video")
        }

        // Start frame source
        frameSource.start()

        // Wire frozen-stream recovery
        frameSource.onStreamFrozen = { recoverStream() }

        // Start monitoring coroutine
        startMonitoring()

        Log.i(TAG, "Connected and streaming")
        return true
    }

    /**
     * Disconnect from the Tello and release all resources.
     */
    fun disconnect() {
        monitorJob?.cancel()
        monitorJob = null

        frameSource.stop()

        scope.launch {
            try {
                connection.stopStream()
            } catch (e: Exception) {
                Log.w(TAG, "Error stopping stream: ${e.message}")
            }
            connection.disconnect()
        }

        Log.i(TAG, "Disconnected")
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
                connection.sendCommand("streamoff")
                delay(500)
                connection.sendCommand("streamon")
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
     * and disconnect.
     */
    fun onActivityDestroy() {
        val state = droneState.value
        if (state.telemetry.isFlying) {
            Log.w(TAG, "Activity destroying while flying -- landing")
            scope.launch {
                connection.land()
                disconnect()
            }
        } else {
            disconnect()
        }
    }

    // ------------------------------------------------------------------
    // Flight command delegates
    // ------------------------------------------------------------------

    /** Command the drone to take off. */
    suspend fun takeoff() = connection.takeoff()

    /** Command the drone to land. */
    suspend fun land() = connection.land()

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

                // Battery critical
                val battery = state.telemetry.battery
                if (battery in 1..config.drone.batteryCriticalThreshold && !batteryAlertSent) {
                    Log.w(TAG, "Battery critical: $battery%")
                    _alerts.tryEmit(DroneAlert.BatteryCritical(battery))
                    batteryAlertSent = true
                    // Auto-land on critical battery
                    if (state.telemetry.isFlying) {
                        Log.w(TAG, "Auto-landing due to critical battery")
                        connection.land()
                    }
                }
                if (battery > config.drone.batteryCriticalThreshold) {
                    batteryAlertSent = false
                }

                // Crash detection: was flying but TelloConnection flipped
                // isFlying to false (after 3 consecutive zero-height polls)
                if (wasFlying && !state.telemetry.isFlying &&
                    state.telemetry.height == 0 && isConnected
                ) {
                    Log.e(TAG, "Crash detected")
                    _alerts.tryEmit(DroneAlert.CrashDetected)
                }
                wasFlying = state.telemetry.isFlying
            }
        }
    }
}
