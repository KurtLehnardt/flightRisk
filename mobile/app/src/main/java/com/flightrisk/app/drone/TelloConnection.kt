package com.flightrisk.app.drone

import android.util.Log
import com.flightrisk.app.config.DroneConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketException
import java.net.SocketTimeoutException

/**
 * Core Tello UDP command protocol implementation.
 *
 * Port of the Python `TelloController` (`flightrisk/drone/tello.py`) to Kotlin
 * coroutines + DatagramSocket. All commands are serialized through [commandMutex]
 * to prevent concurrent UDP packets from saturating the Tello's single channel.
 *
 * @param config Drone connection parameters (ports, timeouts, polling intervals).
 */
class TelloConnection(private val config: DroneConfig) {

    companion object {
        private const val TAG = "TelloConnection"
        private const val TELLO_HOST = "192.168.10.1"
        private const val RC_MIN_INTERVAL_MS = 50L
    }

    // Observable state
    private val _state = MutableStateFlow(TelloState())
    val state: StateFlow<TelloState> = _state.asStateFlow()

    // UDP command socket (port 8889)
    private var commandSocket: DatagramSocket? = null

    // Coroutine management
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val commandMutex = Mutex()
    private var keepaliveJob: Job? = null
    private var statePollingJob: Job? = null

    // Connection state
    private var isConnected = false

    // RC rate limiting
    private var lastRcTime = 0L

    /**
     * Connect to the Tello and enter SDK mode.
     *
     * Creates a DatagramSocket bound to [DroneConfig.telloCommandPort],
     * sends "command" to enter SDK mode, and starts keepalive + state polling
     * on success.
     *
     * @return true on successful connection, false on failure.
     */
    suspend fun connect(): Boolean = withContext(Dispatchers.IO) {
        try {
            updateState(connectionState = TelloConnectionState.CONNECTING)

            commandSocket = DatagramSocket(config.telloCommandPort)
            Log.i(TAG, "Socket bound to port ${config.telloCommandPort}")

            val response = sendCommand("command")
            if (response == null || !response.trim().equals("ok", ignoreCase = true)) {
                Log.e(TAG, "SDK mode failed, response: $response")
                commandSocket?.close()
                commandSocket = null
                updateState(
                    connectionState = TelloConnectionState.ERROR,
                    errorMessage = "SDK mode failed: ${response ?: "timeout"}"
                )
                return@withContext false
            }

            isConnected = true
            updateState(connectionState = TelloConnectionState.CONNECTED)
            Log.i(TAG, "Connected to Tello")

            startKeepalive()
            startStatePolling()
            return@withContext true
        } catch (e: SocketException) {
            Log.e(TAG, "Connection failed: ${e.message}")
            updateState(
                connectionState = TelloConnectionState.ERROR,
                errorMessage = "Socket error: ${e.message}"
            )
            return@withContext false
        } catch (e: IOException) {
            Log.e(TAG, "Connection failed: ${e.message}")
            updateState(
                connectionState = TelloConnectionState.ERROR,
                errorMessage = "IO error: ${e.message}"
            )
            return@withContext false
        }
    }

    /**
     * Disconnect from the Tello.
     *
     * Cancels keepalive and state polling, closes the socket, and resets
     * state to DISCONNECTED.
     */
    fun disconnect() {
        keepaliveJob?.cancel()
        keepaliveJob = null
        statePollingJob?.cancel()
        statePollingJob = null
        isConnected = false

        try {
            commandSocket?.close()
        } catch (e: Exception) {
            Log.w(TAG, "Error closing socket: ${e.message}")
        }
        commandSocket = null

        updateState(
            connectionState = TelloConnectionState.DISCONNECTED,
            telemetry = TelloTelemetry()
        )
        Log.i(TAG, "Disconnected")
    }

    /**
     * Send a command string to the Tello and wait for a response.
     *
     * Acquires [commandMutex] to serialize commands. The Tello can only
     * process one command at a time over its single UDP channel.
     *
     * @param command The SDK command string (e.g., "takeoff", "battery?").
     * @param timeoutMs Response timeout in milliseconds.
     * @return The response string, or null on timeout/error.
     */
    suspend fun sendCommand(
        command: String,
        timeoutMs: Long = config.commandTimeoutMs,
    ): String? = commandMutex.withLock {
        sendCommandInternal(command, timeoutMs)
    }

    /**
     * Internal command sender — caller must already hold [commandMutex].
     */
    private suspend fun sendCommandInternal(
        command: String,
        timeoutMs: Long = config.commandTimeoutMs,
    ): String? = withContext(Dispatchers.IO) {
        val socket = commandSocket ?: run {
            Log.w(TAG, "sendCommand($command): no socket")
            return@withContext null
        }

        try {
            val address = InetAddress.getByName(TELLO_HOST)
            val sendData = command.toByteArray(Charsets.UTF_8)
            val sendPacket = DatagramPacket(
                sendData, sendData.size, address, config.telloCommandPort
            )
            socket.send(sendPacket)
            Log.d(TAG, ">> $command")

            socket.soTimeout = timeoutMs.toInt()
            val recvBuf = ByteArray(1024)
            val recvPacket = DatagramPacket(recvBuf, recvBuf.size)
            socket.receive(recvPacket)

            val response = String(recvPacket.data, 0, recvPacket.length, Charsets.UTF_8)
            Log.d(TAG, "<< $response")
            response
        } catch (e: SocketTimeoutException) {
            Log.w(TAG, "Timeout waiting for response to: $command")
            null
        } catch (e: SocketException) {
            Log.e(TAG, "Socket error sending $command: ${e.message}")
            null
        } catch (e: IOException) {
            Log.e(TAG, "IO error sending $command: ${e.message}")
            null
        }
    }

    /**
     * Enable the Tello video stream.
     *
     * @return true if "streamon" succeeded, false otherwise.
     */
    suspend fun startStream(): Boolean {
        val response = sendCommand("streamon")
        return if (response != null && response.trim().equals("ok", ignoreCase = true)) {
            updateState(connectionState = TelloConnectionState.STREAMING)
            Log.i(TAG, "Video stream started")
            true
        } else {
            Log.w(TAG, "startStream failed: $response")
            false
        }
    }

    /**
     * Disable the Tello video stream.
     */
    suspend fun stopStream() {
        sendCommand("streamoff")
        if (isConnected) {
            updateState(connectionState = TelloConnectionState.CONNECTED)
        }
        Log.i(TAG, "Video stream stopped")
    }

    /**
     * Command the Tello to take off.
     */
    suspend fun takeoff() {
        val response = sendCommand("takeoff")
        if (response != null && response.trim().equals("ok", ignoreCase = true)) {
            val current = _state.value
            updateState(telemetry = current.telemetry.copy(isFlying = true))
            Log.i(TAG, "Takeoff")
        } else {
            Log.w(TAG, "Takeoff failed: $response")
        }
    }

    /**
     * Command the Tello to land.
     */
    suspend fun land() {
        val response = sendCommand("land")
        if (response != null && response.trim().equals("ok", ignoreCase = true)) {
            val current = _state.value
            updateState(telemetry = current.telemetry.copy(isFlying = false))
            Log.i(TAG, "Landing")
        } else {
            Log.w(TAG, "Land failed: $response")
        }
    }

    /**
     * Move the drone in a cardinal direction.
     *
     * Non-blocking: silently drops the command if another is in-flight
     * (matching the Python `move()` behavior with non-blocking lock).
     *
     * @param direction One of "forward", "back", "left", "right", "up", "down".
     * @param distanceCm Distance in centimeters, clamped to 20..500.
     */
    suspend fun move(direction: String, distanceCm: Int) {
        val validDirections = setOf("forward", "back", "left", "right", "up", "down")
        if (direction !in validDirections) {
            Log.w(TAG, "Invalid direction: $direction")
            return
        }

        if (!commandMutex.tryLock()) {
            Log.d(TAG, "move($direction) dropped — command in flight")
            return
        }
        try {
            val clamped = distanceCm.coerceIn(20, 500)
            sendCommandInternal("$direction $clamped")
        } finally {
            commandMutex.unlock()
        }
    }

    /**
     * Rotate the drone clockwise or counter-clockwise.
     *
     * Non-blocking: silently drops if another command is in-flight.
     *
     * @param degrees Positive for clockwise, negative for counter-clockwise.
     *                Clamped to -360..360.
     */
    suspend fun rotate(degrees: Int) {
        if (!commandMutex.tryLock()) {
            Log.d(TAG, "rotate($degrees) dropped — command in flight")
            return
        }
        try {
            val clamped = degrees.coerceIn(-360, 360)
            val command = if (clamped >= 0) {
                "cw $clamped"
            } else {
                "ccw ${-clamped}"
            }
            sendCommandInternal(command)
        } finally {
            commandMutex.unlock()
        }
    }

    /**
     * Send RC joystick control values.
     *
     * Rate-limited to one call every [RC_MIN_INTERVAL_MS] to avoid
     * flooding the Tello. Fire-and-forget — no response expected.
     *
     * @param lr Left/right (-100 to 100).
     * @param fb Forward/backward (-100 to 100).
     * @param ud Up/down (-100 to 100).
     * @param yaw Yaw (-100 to 100).
     */
    suspend fun rcControl(lr: Int, fb: Int, ud: Int, yaw: Int) = withContext(Dispatchers.IO) {
        val now = System.currentTimeMillis()
        if (now - lastRcTime < RC_MIN_INTERVAL_MS) return@withContext
        lastRcTime = now

        val socket = commandSocket ?: return@withContext
        val clampedLr = lr.coerceIn(-100, 100)
        val clampedFb = fb.coerceIn(-100, 100)
        val clampedUd = ud.coerceIn(-100, 100)
        val clampedYaw = yaw.coerceIn(-100, 100)

        val command = "rc $clampedLr $clampedFb $clampedUd $clampedYaw"
        try {
            val address = InetAddress.getByName(TELLO_HOST)
            val sendData = command.toByteArray(Charsets.UTF_8)
            val packet = DatagramPacket(
                sendData, sendData.size, address, config.telloCommandPort
            )
            socket.send(packet)
        } catch (e: IOException) {
            Log.w(TAG, "RC control send failed: ${e.message}")
        }
    }

    /**
     * Stop all movement and hover in place.
     */
    suspend fun hover() {
        rcControl(0, 0, 0, 0)
    }

    /**
     * Start the keepalive coroutine.
     *
     * Sends "command" every [DroneConfig.keepaliveIntervalSec] seconds
     * to prevent the Tello from auto-landing. Skips if the command mutex
     * is held by another operation.
     */
    private fun startKeepalive() {
        keepaliveJob = scope.launch {
            while (isConnected) {
                delay(config.keepaliveIntervalSec * 1000L)
                if (!isConnected) break
                if (!commandMutex.tryLock()) continue
                try {
                    sendCommandInternal("command", timeoutMs = 5000)
                } catch (e: Exception) {
                    Log.w(TAG, "Keepalive failed: ${e.message}")
                } finally {
                    commandMutex.unlock()
                }
            }
        }
    }

    /**
     * Start the state polling coroutine.
     *
     * Queries battery, height, temperature, and flight time every
     * [DroneConfig.statePollingIntervalSec] seconds. Tracks consecutive
     * failures for disconnect detection and zero-height polls for crash
     * detection.
     */
    private fun startStatePolling() {
        statePollingJob = scope.launch {
            var pollFailures = 0
            var zeroHeightCount = 0

            while (isConnected) {
                delay(config.statePollingIntervalSec * 1000L)
                if (!isConnected) break

                // Skip polling if a flight command is active
                if (commandMutex.isLocked) continue

                try {
                    commandMutex.withLock {
                        val battery = queryInt("battery?")
                        val height = queryInt("height?")
                        val temperature = queryInt("temp?")
                        val flightTime = queryInt("time?")

                        val current = _state.value
                        var isFlying = current.telemetry.isFlying

                        // Crash detection: 3 consecutive zero-height polls while flying
                        if (isFlying && height == 0) {
                            zeroHeightCount++
                            if (zeroHeightCount >= 3) {
                                isFlying = false
                                Log.w(TAG, "Crash detected — height 0 for $zeroHeightCount polls")
                                zeroHeightCount = 0
                            }
                        } else {
                            zeroHeightCount = 0
                        }

                        updateState(
                            telemetry = TelloTelemetry(
                                battery = battery,
                                height = height,
                                temperature = temperature,
                                flightTime = flightTime,
                                isFlying = isFlying,
                            )
                        )
                    }
                    pollFailures = 0
                } catch (e: Exception) {
                    pollFailures++
                    Log.w(TAG, "State poll failed ($pollFailures consecutive): ${e.message}")
                    if (pollFailures >= 3 && isConnected) {
                        Log.e(TAG, "Connection lost — $pollFailures consecutive poll failures")
                        isConnected = false
                        updateState(
                            connectionState = TelloConnectionState.DISCONNECTED,
                            errorMessage = "Connection lost"
                        )
                    }
                }
            }
        }
    }

    /**
     * Query an integer value from the Tello.
     *
     * Caller must hold [commandMutex].
     *
     * @param query The query command (e.g., "battery?").
     * @return The integer value, or 0 if the response cannot be parsed.
     */
    private suspend fun queryInt(query: String): Int {
        val response = sendCommandInternal(query) ?: return 0
        return response.trim().toIntOrNull() ?: 0
    }

    /**
     * Update the observable [TelloState], merging non-null parameters
     * with the current state.
     */
    private fun updateState(
        connectionState: TelloConnectionState? = null,
        telemetry: TelloTelemetry? = null,
        errorMessage: String? = null,
        isOnTelloWifi: Boolean? = null,
    ) {
        val current = _state.value
        _state.value = current.copy(
            connectionState = connectionState ?: current.connectionState,
            telemetry = telemetry ?: current.telemetry,
            errorMessage = if (connectionState != null && connectionState != TelloConnectionState.ERROR) {
                null
            } else {
                errorMessage ?: current.errorMessage
            },
            isOnTelloWifi = isOnTelloWifi ?: current.isOnTelloWifi,
        )
    }
}
