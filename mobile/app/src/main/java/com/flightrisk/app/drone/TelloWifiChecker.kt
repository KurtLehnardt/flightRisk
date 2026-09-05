package com.flightrisk.app.drone

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.util.Log

/**
 * Checks whether the device is connected to a Tello drone's WiFi AP.
 *
 * The Tello creates a soft-AP with a static gateway at [TELLO_GATEWAY_IP].
 * This checker inspects the DHCP gateway of the active WiFi connection to
 * distinguish Tello WiFi from other networks.
 *
 * @param context Android application context (for system services).
 */
class TelloWifiChecker(private val context: Context) {

    companion object {
        private const val TAG = "TelloWifiChecker"
        private const val TELLO_GATEWAY_IP = "192.168.10.1"
    }

    /**
     * Result of a Tello WiFi check.
     */
    sealed class WifiStatus {
        /** Device is connected to the Tello's WiFi AP. */
        data object OnTelloWifi : WifiStatus()

        /** Device is on WiFi, but not the Tello's network. */
        data class OnOtherWifi(val ssid: String?) : WifiStatus()

        /** Device has no WiFi connection. */
        data object NoWifi : WifiStatus()
    }

    /**
     * Check the current WiFi connection and return the [WifiStatus].
     */
    @Suppress("DEPRECATION")
    fun check(): WifiStatus {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        if (cm == null) {
            Log.w(TAG, "ConnectivityManager unavailable")
            return WifiStatus.NoWifi
        }

        val network = cm.activeNetwork
        val capabilities = network?.let { cm.getNetworkCapabilities(it) }
        if (capabilities == null || !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
            return WifiStatus.NoWifi
        }

        val wifiManager = context.applicationContext
            .getSystemService(Context.WIFI_SERVICE) as? WifiManager
        if (wifiManager == null) {
            Log.w(TAG, "WifiManager unavailable")
            return WifiStatus.NoWifi
        }

        val dhcpInfo = wifiManager.dhcpInfo
        val gatewayIp = intToIp(dhcpInfo.gateway)

        return if (gatewayIp == TELLO_GATEWAY_IP) {
            Log.d(TAG, "Connected to Tello WiFi (gateway=$gatewayIp)")
            WifiStatus.OnTelloWifi
        } else {
            val ssid = wifiManager.connectionInfo?.ssid?.removeSurrounding("\"")
            Log.d(TAG, "On other WiFi: ssid=$ssid, gateway=$gatewayIp")
            WifiStatus.OnOtherWifi(ssid)
        }
    }

    /**
     * Return a user-facing guidance message for the given [WifiStatus].
     */
    fun getGuidanceMessage(status: WifiStatus): String {
        return when (status) {
            is WifiStatus.OnTelloWifi ->
                "Connected to Tello WiFi. Ready to fly."
            is WifiStatus.OnOtherWifi ->
                "Connected to \"${status.ssid ?: "unknown"}\". " +
                    "Switch to the Tello WiFi network to connect to the drone."
            is WifiStatus.NoWifi ->
                "No WiFi connection. Turn on WiFi and connect to the Tello network."
        }
    }

    /**
     * Convert an integer IP address from [android.net.DhcpInfo] to a
     * dotted-decimal string. DhcpInfo stores IPs in little-endian
     * byte order on most devices.
     */
    private fun intToIp(ip: Int): String {
        return "${ip and 0xFF}" +
            ".${ip shr 8 and 0xFF}" +
            ".${ip shr 16 and 0xFF}" +
            ".${ip shr 24 and 0xFF}"
    }
}
