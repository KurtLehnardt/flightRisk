import SwiftUI

/// Connection card for the Tello drone, shown when the drone is not
/// yet streaming video.
///
/// Mirrors the Android `DroneConnectionCard` composable. Displays
/// different content based on connection state:
/// - Disconnected: "Connect to Drone" button + Wi-Fi hint
/// - Connecting: spinner + "Connecting..." + Cancel button
/// - Connected: green status + "Disconnect" button
/// - Error: red status + troubleshooting message + Retry button
struct DroneConnectionCardView: View {

    /// Current Tello drone state, or nil if drone not active.
    let droneState: TelloState?

    /// User-facing status/error message for the drone connection.
    let droneConnectionMessage: String?

    /// Callback to initiate drone connection.
    let onConnect: () -> Void

    /// Callback to disconnect from the drone.
    let onDisconnect: () -> Void

    var body: some View {
        let connectionState = droneState?.connectionState ?? .disconnected

        VStack(alignment: .leading, spacing: 12) {
            switch connectionState {
            case .connecting:
                connectingContent

            case .connected:
                connectedContent

            case .error:
                errorContent

            case .streaming:
                // Streaming is handled by FlightControlsOverlayView;
                // this card is never rendered during streaming.
                EmptyView()

            case .disconnected:
                disconnectedContent
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(Color.black.opacity(0.9))
        )
        .shadow(color: .black.opacity(0.3), radius: 4, y: 2)
        .padding(.horizontal, 16)
    }

    // MARK: - Connecting

    private var connectingContent: some View {
        HStack(spacing: 12) {
            ProgressView()
                .tint(FlightRiskTheme.detectionBlue)
                .frame(width: 24, height: 24)

            Text("Connecting to drone...")
                .font(.headline)
                .fontWeight(.bold)
                .foregroundStyle(FlightRiskTheme.hudWhite)
                .frame(maxWidth: .infinity, alignment: .leading)

            Button("Cancel", action: onDisconnect)
                .foregroundStyle(FlightRiskTheme.hudWhite)
                .frame(minHeight: 44)
                .accessibilityLabel("Cancel drone connection")
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Connecting to drone")
    }

    // MARK: - Connected

    private var connectedContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Connected to Drone")
                .font(.headline)
                .fontWeight(.bold)
                .foregroundStyle(FlightRiskTheme.matchGreen)

            Button(action: onDisconnect) {
                Text("Disconnect")
                    .font(.subheadline)
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.bordered)
            .tint(FlightRiskTheme.hudWhite)
            .accessibilityLabel("Disconnect from drone")
        }
    }

    // MARK: - Error

    private var errorContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Connection Failed")
                .font(.headline)
                .fontWeight(.bold)
                .foregroundStyle(FlightRiskTheme.alertRed)

            if let message = droneConnectionMessage {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(FlightRiskTheme.hudWhite)
            }

            Button(action: onConnect) {
                Label("Retry", systemImage: "arrow.clockwise")
                    .font(.subheadline)
                    .fontWeight(.bold)
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.borderedProminent)
            .tint(FlightRiskTheme.alertRed)
            .accessibilityLabel("Retry drone connection")
        }
    }

    // MARK: - Disconnected

    private var disconnectedContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Connect to Drone")
                .font(.headline)
                .fontWeight(.bold)
                .foregroundStyle(FlightRiskTheme.hudWhite)

            Button(action: onConnect) {
                Text("Connect to Drone")
                    .font(.subheadline)
                    .fontWeight(.bold)
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.borderedProminent)
            .tint(FlightRiskTheme.detectionBlue)
            .accessibilityLabel("Connect to Tello drone")

            Text("Connect your phone to the Tello WiFi network first")
                .font(.caption)
                .foregroundStyle(Color(white: 0.67))

            if let message = droneConnectionMessage {
                Text(message)
                    .font(.caption)
                    .fontWeight(.medium)
                    .foregroundStyle(FlightRiskTheme.alertRed)
            }
        }
    }
}
