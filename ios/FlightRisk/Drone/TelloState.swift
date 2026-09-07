import Foundation

/// Connection lifecycle states for the Tello UDP link.
enum TelloConnectionState: String {
    case disconnected, connecting, connected, streaming, error
}

/// Telemetry snapshot from periodic state polling.
struct TelloTelemetry {
    var battery: Int? = nil    // nil = no reading yet, 0 = genuinely 0%
    var height: Int = 0
    var temperature: Int = 0
    var flightTime: Int = 0
    var isFlying: Bool = false
}

/// Observable aggregate of connection + telemetry + error state.
struct TelloState {
    var connectionState: TelloConnectionState = .disconnected
    var telemetry: TelloTelemetry = TelloTelemetry()
    var errorMessage: String? = nil
    var isOnTelloWifi: Bool = false
}
