package com.flightrisk.mission

/**
 * Every test transport goes through this guard. It refuses any address other than loopback
 * (127.0.0.1, ::1) and the Android emulator's host alias (10.0.2.2), so a test can never reach
 * a real drone or join a drone's network.
 */
object LoopbackGuard {
    private val allowed = setOf("127.0.0.1", "::1", "10.0.2.2")

    fun isAllowed(host: String): Boolean = host.trim().removePrefix("[").removeSuffix("]") in allowed

    /** Returns [host] unchanged when it is a loopback address, and throws otherwise. */
    fun require(host: String): String {
        require(isAllowed(host)) { "Refusing non-loopback test address: $host" }
        return host
    }
}
