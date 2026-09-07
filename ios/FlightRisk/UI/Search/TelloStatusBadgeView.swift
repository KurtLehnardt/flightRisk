import SwiftUI

/// Small badge showing the current drone connection and telemetry status.
///
/// Displays a colored status dot, state label, and -- when streaming --
/// battery percentage and height. When in error state, shows the error
/// message.
struct TelloStatusBadgeView: View {

    /// Current Tello drone state.
    let droneState: TelloState

    var body: some View {
        HStack(spacing: 8) {
            // Status dot
            Circle()
                .fill(dotColor)
                .frame(width: 8, height: 8)

            // State label
            Text(stateLabel)
                .font(.caption)
                .fontWeight(.medium)
                .foregroundStyle(FlightRiskTheme.hudWhite)

            // Battery and height (when streaming)
            if droneState.connectionState == .streaming {
                if let battery = droneState.telemetry.battery {
                    Text("\(battery)%")
                        .font(.caption2)
                        .fontWeight(.bold)
                        .foregroundStyle(batteryColor(battery))
                }

                Text("\(droneState.telemetry.height)cm")
                    .font(.caption2)
                    .foregroundStyle(Color(white: 0.67))
            }

            // Error message
            if droneState.connectionState == .error,
               let errorMsg = droneState.errorMessage {
                Text(errorMsg)
                    .font(.caption2)
                    .foregroundStyle(FlightRiskTheme.alertRed)
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(Color.black.opacity(0.8))
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityDescription)
    }

    // MARK: - Computed Properties

    private var dotColor: Color {
        switch droneState.connectionState {
        case .disconnected: return .red
        case .connecting:   return .orange
        case .connected:    return FlightRiskTheme.detectionBlue
        case .streaming:    return FlightRiskTheme.matchGreen
        case .error:        return .red
        }
    }

    private var stateLabel: String {
        switch droneState.connectionState {
        case .disconnected: return "Disconnected"
        case .connecting:   return "Connecting"
        case .connected:    return "Connected"
        case .streaming:    return "Streaming"
        case .error:        return "Error"
        }
    }

    private func batteryColor(_ level: Int) -> Color {
        if level <= 10 { return FlightRiskTheme.alertRed }
        if level <= 20 { return FlightRiskTheme.alertOrange }
        return FlightRiskTheme.hudWhite
    }

    private var accessibilityDescription: String {
        var desc = "Drone status: \(stateLabel)"
        if droneState.connectionState == .streaming {
            if let battery = droneState.telemetry.battery {
                desc += ", battery \(battery) percent"
            }
            desc += ", height \(droneState.telemetry.height) centimeters"
        }
        if let error = droneState.errorMessage,
           droneState.connectionState == .error {
            desc += ", error: \(error)"
        }
        return desc
    }
}
