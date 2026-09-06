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
        private const val MAX_AVOIDANCE_RETRIES = 5
        private const val AVOIDANCE_SIDE_CM = 30
        private const val AVOIDANCE_REVERSE_CM = 50
    }

    val wifiChecker = TelloWifiChecker(context)
    val connection = TelloConnection(config.drone)
    val frameSource = TelloFrameSource(config.drone)

    /** Observable drone state for UI binding. */
    val droneState: StateFlow<TelloState> = connection.state

    // ------------------------------------------------------------------
    // Alert system
    // ------------------------------------------------------------------

    sealed class DroneAlert {
        data object ConnectionLost : DroneAlert()
        data class BatteryCritical(val percent: Int) : DroneAlert()
        data object CrashDetected : DroneAlert()
        data object StreamFrozen : DroneAlert()
        data class ObstacleDetected(val action: String, val centerDepth: Float) : DroneAlert()
        data object MatchPause : DroneAlert()
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
    // Obstacle avoidance
    // ------------------------------------------------------------------

    private var obstacleGuard: ObstacleGuard? = null

    fun initObstacleGuard() {
        if (obstacleGuard != null) return
        try {
            obstacleGuard = ObstacleGuard(context)
            if (obstacleGuard?.isAvailable == true) {
                Log.i(TAG, "Obstacle avoidance enabled")
            } else {
                Log.w(TAG, "Obstacle avoidance: MiDaS model not available")
                obstacleGuard = null
            }
        } catch (e: Exception) {
            Log.w(TAG, "Obstacle avoidance init failed: ${e.message}")
            obstacleGuard = null
        }
    }

    // ------------------------------------------------------------------
    // Connect / disconnect
    // ------------------------------------------------------------------

    suspend fun connectAndStream(): Boolean {
        val wifiStatus = wifiChecker.check()
        if (wifiStatus !is TelloWifiChecker.WifiStatus.OnTelloWifi) {
            val guidance = wifiChecker.getGuidanceMessage(wifiStatus)
            Log.w(TAG, "Not on Tello WiFi: $guidance")
            return false
        }

        val connected = connection.connect()
        if (!connected) {
            Log.e(TAG, "Connection failed")
            return false
        }

        val streaming = connection.startStream()
        if (!streaming) {
            Log.e(TAG, "Stream start failed")
            connection.disconnect()
            return false
        }

        frameSource.onStreamFrozen = { recoverStream() }
        frameSource.start()
        startMonitoring()

        // Initialize obstacle avoidance in background
        scope.launch { initObstacleGuard() }

        Log.i(TAG, "Connected and streaming")
        return true
    }

    suspend fun disconnect() {
        monitorJob?.cancel()
        monitorJob = null

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
        obstacleGuard?.close()
        obstacleGuard = null
        Log.i(TAG, "Disconnected")
    }

    fun shutdown() {
        scope.cancel()
        obstacleGuard?.close()
        obstacleGuard = null
        Log.i(TAG, "Shutdown complete")
    }

    // ------------------------------------------------------------------
    // Stream recovery
    // ------------------------------------------------------------------

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

    fun onActivityPause() {
        val state = droneState.value
        if (state.telemetry.isFlying) {
            Log.i(TAG, "Activity pausing while flying -- hovering")
            scope.launch { connection.hover() }
        }
    }

    fun onActivityDestroy() {
        monitorJob?.cancel()
        monitorJob = null
        val state = droneState.value
        Thread {
            frameSource.stop()
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
            obstacleGuard?.close()
            scope.cancel()
        }.start()
        Log.i(TAG, "Destroyed")
    }

    // ------------------------------------------------------------------
    // Flight command delegates
    // ------------------------------------------------------------------

    suspend fun takeoff() = connection.takeoff()

    suspend fun land(): Boolean {
        commandedLanding = true
        return connection.land()
    }

    suspend fun move(direction: String, distanceCm: Int) =
        connection.move(direction, distanceCm)

    suspend fun rotate(degrees: Int) = connection.rotate(degrees)

    suspend fun rcControl(lr: Int, fb: Int, ud: Int, yaw: Int) =
        connection.rcControl(lr, fb, ud, yaw)

    suspend fun hover() = connection.hover()

    suspend fun emergencyStop(): Boolean {
        commandedLanding = true
        return connection.emergencyStop()
    }

    // ------------------------------------------------------------------
    // Pause / resume for match detection
    // ------------------------------------------------------------------

    @Volatile
    var searchPaused: Boolean = false
        private set

    /**
     * Pause the active search pattern and hover in place.
     * Called when the vision pipeline detects a confident match.
     */
    fun pauseSearchForMatch() {
        if (!searchActive || searchPaused) return
        searchPaused = true
        Log.i(TAG, "Search paused — match detected, hovering")
        scope.launch {
            try {
                connection.hover()
            } catch (e: Exception) {
                Log.w(TAG, "Hover on match pause failed: ${e.message}")
            }
        }
        _alerts.tryEmit(DroneAlert.MatchPause)
    }

    /**
     * Resume the search pattern after the operator dismisses a match alert.
     */
    fun resumeSearch() {
        if (!searchPaused) return
        searchPaused = false
        Log.i(TAG, "Search resumed")
    }

    // ------------------------------------------------------------------
    // Search pattern execution (with obstacle avoidance)
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
        searchPaused = false
        searchProgress = Pair(0, waypoints.size)
        Log.i(TAG, "Starting search pattern: ${pattern.displayName}, ${waypoints.size} waypoints")

        searchJob = scope.launch {
            for ((index, wp) in waypoints.withIndex()) {
                if (!searchActive) break

                // Wait while paused for match inspection
                while (searchPaused && searchActive) {
                    delay(200)
                }
                if (!searchActive) break

                searchProgress = Pair(index + 1, waypoints.size)

                try {
                    if (wp.distanceCm > 0) {
                        // Obstacle check before each forward move
                        if (!checkAndAvoidObstacles(index)) {
                            Log.w(TAG, "Skipping waypoint ${index + 1} — obstacle avoidance exhausted")
                            continue
                        }

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
            searchPaused = false
            searchProgress = Pair(0, 0)
            Log.i(TAG, "Search pattern complete")
        }
    }

    /**
     * Check for obstacles before moving. If an obstacle is detected,
     * perform evasive maneuvers up to [MAX_AVOIDANCE_RETRIES] times.
     *
     * @return true if the path is now clear, false if retries exhausted.
     */
    private suspend fun checkAndAvoidObstacles(waypointIndex: Int): Boolean {
        val guard = obstacleGuard ?: return true

        for (retry in 1..MAX_AVOIDANCE_RETRIES) {
            if (!searchActive) return false

            val frame = frameSource.getLatestFrame() ?: return true

            val check = guard.checkPath(frame)
            if (check.safe) return true

            Log.w(TAG, "Obstacle at waypoint ${waypointIndex + 1}: " +
                "action=${check.action}, depth=${check.centerDepth}, retry=$retry")
            _alerts.tryEmit(DroneAlert.ObstacleDetected(check.action, check.centerDepth))

            if (retry >= MAX_AVOIDANCE_RETRIES) {
                Log.w(TAG, "Max avoidance retries at waypoint ${waypointIndex + 1}")
                return false
            }

            try {
                when (check.action) {
                    "go_left" -> {
                        connection.move("left", AVOIDANCE_SIDE_CM)
                        delay(500)
                    }
                    "go_right" -> {
                        connection.move("right", AVOIDANCE_SIDE_CM)
                        delay(500)
                    }
                    "reverse" -> {
                        connection.move("back", AVOIDANCE_REVERSE_CM)
                        delay(500)
                        connection.rotate(90)
                        delay(500)
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "Avoidance maneuver failed: ${e.message}")
                return false
            }
        }
        return false
    }

    fun stopSearchPattern() {
        searchActive = false
        searchPaused = false
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

    private fun startMonitoring() {
        monitorJob?.cancel()
        monitorJob = scope.launch {
            var wasConnected = true
            var wasFlying = false
            var batteryAlertSent = false

            connection.state.collect { state ->
                val isConnected = state.connectionState != TelloConnectionState.DISCONNECTED &&
                    state.connectionState != TelloConnectionState.ERROR
                if (wasConnected && !isConnected) {
                    Log.e(TAG, "Connection lost")
                    _alerts.tryEmit(DroneAlert.ConnectionLost)
                }
                wasConnected = isConnected

                val battery = state.telemetry.battery
                if (battery != null && battery in 0..config.drone.batteryCriticalThreshold && !batteryAlertSent) {
                    Log.w(TAG, "Battery critical: $battery%")
                    _alerts.tryEmit(DroneAlert.BatteryCritical(battery))
                    batteryAlertSent = true
                    if (state.telemetry.isFlying) {
                        Log.w(TAG, "Auto-landing due to critical battery")
                        land()
                    }
                }
                if (battery != null && battery > config.drone.batteryCriticalThreshold) {
                    batteryAlertSent = false
                }

                if (wasFlying && !state.telemetry.isFlying) {
                    if (state.telemetry.height == 0 && isConnected && !commandedLanding) {
                        Log.e(TAG, "Crash detected")
                        _alerts.tryEmit(DroneAlert.CrashDetected)
                    }
                    commandedLanding = false
                }
                wasFlying = state.telemetry.isFlying
            }
        }
    }
}
