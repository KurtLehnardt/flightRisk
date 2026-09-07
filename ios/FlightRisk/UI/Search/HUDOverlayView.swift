import SwiftUI

/// Heads-up display overlay showing real-time pipeline metrics as
/// translucent pills at the top of the screen.
///
/// Mirrors the Android `HudOverlay` composable. Displays FPS, person
/// count, and (when non-zero) the highest match score as translucent
/// pills.
struct HUDOverlayView: View {

    /// Current frames per second from the pipeline.
    let fps: Float

    /// Number of persons detected in the most recent frame.
    let personsDetected: Int

    /// Highest match score seen during this search session (0.0 - 1.0).
    let matchScore: Float

    var body: some View {
        HStack(spacing: 8) {
            HUDPill(
                label: "FPS",
                value: String(format: "%.1f", fps),
                accessibilityText: "Frames per second: \(String(format: "%.1f", fps))"
            )

            HUDPill(
                label: "Persons",
                value: "\(personsDetected)",
                accessibilityText: "\(personsDetected) person\(personsDetected != 1 ? "s" : "") detected"
            )

            if matchScore > 0 {
                HUDPill(
                    label: "Match",
                    value: "\(Int(matchScore * 100))%",
                    accessibilityText: "Best match score: \(Int(matchScore * 100)) percent"
                )
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Search metrics")
    }
}

// MARK: - HUD Pill

/// A single translucent pill for the HUD overlay.
private struct HUDPill: View {

    let label: String
    let value: String
    let accessibilityText: String

    var body: some View {
        HStack(spacing: 0) {
            Text("\(label): ")
                .font(.caption2)
                .foregroundStyle(Color(white: 0.67))

            Text(value)
                .font(.caption)
                .fontWeight(.bold)
                .foregroundStyle(FlightRiskTheme.hudWhite)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(Color.black.opacity(0.8))
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }
}
