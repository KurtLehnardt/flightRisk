import SwiftUI

/// Progress bar shown during multi-frame corroboration.
///
/// Mirrors the Android `ConfidenceProgressBar` composable. Displays
/// "Confidence building... N/M frames" with a linear progress indicator
/// on a translucent background.
struct ConfidenceProgressView: View {

    /// Number of frames the current track has been corroborated.
    let framesMatched: Int

    /// Total frames needed for corroboration.
    let framesNeeded: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Confidence building... \(framesMatched)/\(framesNeeded) frames")
                .font(.caption)
                .fontWeight(.medium)
                .foregroundStyle(FlightRiskTheme.hudWhite)

            ProgressView(
                value: Double(framesMatched),
                total: Double(framesNeeded)
            )
            .tint(.teal)
            .frame(height: 4)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.black.opacity(0.8))
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
            "Confidence building: \(framesMatched) of \(framesNeeded) frames matched"
        )
    }
}
