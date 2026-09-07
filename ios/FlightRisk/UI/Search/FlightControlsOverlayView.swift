import SwiftUI

/// Collapsible flight controls overlay for drone operation.
///
/// Mirrors the Android `FlightControlsOverlay` composable. Provides:
/// - Toggle bar to expand/collapse (spring animation)
/// - Search pattern picker (dropdown with 5 patterns + descriptions)
/// - Start/Stop Search button
/// - Emergency Land button (red, top-right)
/// - D-pad for forward/back/left/right (30cm per tap, 56pt buttons)
/// - Altitude up/down (30cm per tap)
/// - Land button (center, red circle 64pt)
/// - Rotate CW/CCW buttons (45 degrees per tap)
/// - Safety disclaimer
///
/// Auto-collapses when a match alert appears. Semi-transparent black
/// background with rounded top corners.
struct FlightControlsOverlayView: View {

    /// Whether the drone is currently flying.
    let isFlying: Bool

    /// Whether the search pipeline is actively running.
    let isSearching: Bool

    /// Whether the controls panel is expanded.
    @Binding var isExpanded: Bool

    /// Currently selected search pattern.
    let selectedPattern: PatternType

    /// Callback when the user selects a different search pattern.
    let onPatternSelected: (PatternType) -> Void

    /// Callback to land the drone.
    let onLand: () -> Void

    /// Callback for drone directional movement (direction, distanceCm).
    let onMove: (String, Int) -> Void

    /// Callback for drone rotation (degrees).
    let onRotate: (Int) -> Void

    /// Callback to start the search pipeline.
    let onStartSearch: () -> Void

    /// Callback to stop the search pipeline.
    let onStopSearch: () -> Void

    /// Callback for emergency stop.
    let onEmergencyStop: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // Collapse/expand toggle bar
            toggleBar

            // Collapsible content
            if isExpanded {
                expandedContent
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.spring(response: 0.4, dampingFraction: 0.7), value: isExpanded)
    }

    // MARK: - Toggle Bar

    private var toggleBar: some View {
        Button {
            isExpanded.toggle()
        } label: {
            HStack {
                Text(isFlying ? "Flight Controls" : "Drone Controls")
                    .font(.subheadline)
                    .fontWeight(.bold)
                    .foregroundStyle(FlightRiskTheme.hudWhite)

                Spacer()

                Image(systemName: isExpanded ? "chevron.down" : "chevron.up")
                    .foregroundStyle(FlightRiskTheme.hudWhite)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(
                UnevenRoundedRectangle(
                    topLeadingRadius: 16,
                    topTrailingRadius: 16
                )
                .fill(Color(white: 0.1, opacity: 0.8))
            )
        }
        .accessibilityLabel(isExpanded ? "Collapse controls" : "Expand controls")
    }

    // MARK: - Expanded Content

    private var expandedContent: some View {
        VStack(spacing: 12) {
            // Search pattern selector (only when not searching)
            if !isSearching {
                searchPatternSelector
            }

            // Start/Stop Search button
            searchToggleButton

            // Flight controls (only when flying)
            if isFlying {
                flightControls
            }

            // Safety disclaimer
            Text("Drone operation is at your own risk. Maintain visual line of sight at all times.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 16)
                .padding(.vertical, 4)
        }
        .padding(16)
        .background(Color.black.opacity(0.67))
    }

    // MARK: - Search Pattern Selector

    private var searchPatternSelector: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Search Pattern")
                .font(.caption)
                .fontWeight(.bold)
                .foregroundStyle(FlightRiskTheme.hudWhite)

            Menu {
                ForEach(PatternType.allCases) { pattern in
                    Button {
                        onPatternSelected(pattern)
                    } label: {
                        VStack(alignment: .leading) {
                            Text(pattern.rawValue)
                                .fontWeight(pattern == selectedPattern ? .bold : .regular)
                            Text(pattern.description)
                                .font(.caption)
                        }
                    }
                }
            } label: {
                HStack {
                    Text(selectedPattern.rawValue)
                        .foregroundStyle(FlightRiskTheme.hudWhite)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    Image(systemName: "chevron.down")
                        .font(.caption)
                        .foregroundStyle(FlightRiskTheme.hudWhite)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color.white.opacity(0.3), lineWidth: 1)
                )
            }
            .accessibilityLabel("Search pattern: \(selectedPattern.rawValue)")
        }
    }

    // MARK: - Search Toggle Button

    private var searchToggleButton: some View {
        Button(action: isSearching ? onStopSearch : onStartSearch) {
            Text(isSearching ? "Stop Search" : "Start Search")
                .font(.headline)
                .fontWeight(.bold)
                .foregroundStyle(FlightRiskTheme.hudWhite)
                .frame(maxWidth: .infinity, minHeight: 48)
        }
        .buttonStyle(.borderedProminent)
        .tint(isSearching ? FlightRiskTheme.alertRed : FlightRiskTheme.matchGreen)
        .clipShape(RoundedRectangle(cornerRadius: 24))
        .accessibilityLabel(isSearching ? "Stop search" : "Start search")
    }

    // MARK: - Flight Controls

    private var flightControls: some View {
        VStack(spacing: 12) {
            // Emergency Land button (top-right)
            HStack {
                Spacer()
                Button(action: onEmergencyStop) {
                    Text("Emergency Land")
                        .font(.caption2)
                        .fontWeight(.bold)
                        .foregroundStyle(FlightRiskTheme.hudWhite)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                }
                .frame(minWidth: 48, minHeight: 48)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .fill(FlightRiskTheme.alertRed)
                )
                .accessibilityLabel("Emergency land")
            }

            // Main control area: altitude | land | d-pad
            HStack(spacing: 16) {
                // Altitude controls
                VStack(spacing: 8) {
                    DirectionalButton(label: "Up", systemImage: "arrow.up") {
                        onMove("up", 30)
                    }
                    DirectionalButton(label: "Down", systemImage: "arrow.down") {
                        onMove("down", 30)
                    }
                }

                // Land button (center, red circle)
                Button(action: onLand) {
                    Text("Land")
                        .font(.caption2)
                        .fontWeight(.bold)
                        .foregroundStyle(FlightRiskTheme.hudWhite)
                }
                .frame(width: 64, height: 64)
                .background(Circle().fill(FlightRiskTheme.alertRed))
                .accessibilityLabel("Land drone")

                // D-pad
                dPad
            }

            // Rotate controls
            HStack(spacing: 16) {
                DirectionalButton(label: "Rotate CCW", systemImage: "arrow.counterclockwise") {
                    onRotate(-45)
                }
                DirectionalButton(label: "Rotate CW", systemImage: "arrow.clockwise") {
                    onRotate(45)
                }
            }
        }
    }

    // MARK: - D-Pad

    private var dPad: some View {
        VStack(spacing: 0) {
            DirectionalButton(label: "Forward", systemImage: "arrow.up") {
                onMove("forward", 30)
            }

            HStack(spacing: 8) {
                DirectionalButton(label: "Left", systemImage: "arrow.left") {
                    onMove("left", 30)
                }

                Color.clear.frame(width: 56, height: 56)

                DirectionalButton(label: "Right", systemImage: "arrow.right") {
                    onMove("right", 30)
                }
            }

            DirectionalButton(label: "Back", systemImage: "arrow.down") {
                onMove("back", 30)
            }
        }
    }
}

// MARK: - Directional Button

/// A single directional control button with 56pt minimum touch target.
private struct DirectionalButton: View {
    let label: String
    let systemImage: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.body)
                .fontWeight(.medium)
                .foregroundStyle(FlightRiskTheme.hudWhite)
                .frame(width: 56, height: 56)
        }
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.white.opacity(0.15))
        )
        .accessibilityLabel(label)
    }
}
