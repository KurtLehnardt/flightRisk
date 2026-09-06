package com.flightrisk.app.drone

import android.content.Context
import android.util.Log
import com.flightrisk.app.config.FlightRiskConfig
import java.util.concurrent.ConcurrentHashMap

class FleetManager(
    private val context: Context,
    private val config: FlightRiskConfig,
) {
    companion object {
        private const val TAG = "FleetManager"
    }

    private val drones = ConcurrentHashMap<String, DroneManager>()
    private val droneHosts = ConcurrentHashMap<String, String>()
    private val pending = ConcurrentHashMap.newKeySet<String>()

    @Volatile
    var primaryId: String? = null
        private set

    val primary: DroneManager?
        get() = primaryId?.let { drones[it] }

    val count: Int get() = drones.size

    val droneIds: List<String> get() = drones.keys().toList()

    fun hasHost(host: String): Boolean =
        droneHosts.containsValue(host)

    suspend fun register(droneId: String, host: String = config.drone.telloDefaultHost): Boolean {
        if (drones.containsKey(droneId) || pending.contains(droneId)) return false
        if (hasHost(host)) return false

        pending.add(droneId)
        try {
            val manager = DroneManager(context, config.copy(
                drone = config.drone.copy(telloDefaultHost = host)
            ))
            val connected = manager.connectAndStream()
            if (!connected) {
                Log.w(TAG, "Failed to connect drone $droneId at $host")
                return false
            }
            drones[droneId] = manager
            droneHosts[droneId] = host
            if (primaryId == null) {
                primaryId = droneId
            }
            Log.i(TAG, "Registered drone $droneId at $host (total: ${drones.size})")
            return true
        } finally {
            pending.remove(droneId)
        }
    }

    suspend fun deregister(droneId: String): Boolean {
        val manager = drones.remove(droneId) ?: return false
        droneHosts.remove(droneId)
        if (primaryId == droneId) {
            primaryId = drones.keys.firstOrNull()
        }
        try {
            manager.disconnect()
        } catch (e: Exception) {
            Log.w(TAG, "Error disconnecting drone $droneId: ${e.message}")
        }
        Log.i(TAG, "Deregistered drone $droneId (remaining: ${drones.size})")
        return true
    }

    fun get(droneId: String): DroneManager? = drones[droneId]

    fun setPrimary(droneId: String): Boolean {
        if (!drones.containsKey(droneId)) return false
        primaryId = droneId
        return true
    }

    fun getAllTelemetry(): Map<String, Map<String, Any?>> {
        return drones.entries.associate { (id, manager) ->
            val state = manager.droneState.value
            id to mapOf(
                "battery" to state.telemetry.battery,
                "height" to state.telemetry.height,
                "temperature" to state.telemetry.temperature,
                "flightTime" to state.telemetry.flightTime,
                "isFlying" to state.telemetry.isFlying,
                "isConnected" to (state.connectionState != TelloConnectionState.DISCONNECTED),
            )
        }
    }

    suspend fun broadcastCommand(command: suspend (DroneManager) -> Unit): Map<String, Exception?> {
        val results = mutableMapOf<String, Exception?>()
        for ((id, manager) in drones) {
            try {
                command(manager)
                results[id] = null
            } catch (e: Exception) {
                Log.e(TAG, "Command failed on $id: ${e.message}")
                results[id] = e
            }
        }
        return results
    }

    suspend fun disconnectAll() {
        val managers = drones.values.toList()
        drones.clear()
        droneHosts.clear()
        primaryId = null
        for (manager in managers) {
            try {
                manager.disconnect()
            } catch (_: Exception) {}
        }
        Log.i(TAG, "All drones disconnected")
    }
}
