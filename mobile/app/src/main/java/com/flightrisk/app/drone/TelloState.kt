package com.flightrisk.app.drone

enum class TelloConnectionState {
    DISCONNECTED,
    CONNECTING,
    CONNECTED,
    STREAMING,
    ERROR,
}

data class TelloTelemetry(
    val battery: Int = 0,
    val height: Int = 0,
    val temperature: Int = 0,
    val flightTime: Int = 0,
    val isFlying: Boolean = false,
)

data class TelloState(
    val connectionState: TelloConnectionState = TelloConnectionState.DISCONNECTED,
    val telemetry: TelloTelemetry = TelloTelemetry(),
    val errorMessage: String? = null,
    val isOnTelloWifi: Boolean = false,
)
