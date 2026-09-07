import MapKit
import SwiftUI

/// Alert card displaying match details, snapshot, scores, GPS
/// coordinates, and action buttons.
///
/// This is the critical UX moment: the user decides whether the
/// detection is their child. Mirrors the Android `MatchAlertCard`
/// composable with iOS-native styling.
///
/// Action buttons are placed at the bottom for one-handed reachability.
struct MatchAlertCardView: View {

    /// The match entry with all detection details.
    let matchEntry: MatchEntry

    /// Dismiss the alert (Confirm or X button).
    let onDismiss: () -> Void

    /// "Not My Child" -- suppress this track from future alerts.
    let onNotMyChild: () -> Void

    /// Navigate to match location in Maps.
    let onNavigate: (Double, Double) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // Top row: snapshot + score details + dismiss button
            HStack(alignment: .top, spacing: 12) {
                // Snapshot image
                if let snapshot = matchEntry.snapshot {
                    Image(decorative: snapshot, scale: 1.0)
                        .resizable()
                        .aspectRatio(contentMode: .fill)
                        .frame(width: 80, height: 80)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .accessibilityLabel("Match snapshot")
                }

                // Score details
                VStack(alignment: .leading, spacing: 4) {
                    Text("Score: \(Int(matchEntry.score * 100))%")
                        .font(.title3)
                        .fontWeight(.bold)
                        .foregroundStyle(.primary)
                        .accessibilityLabel(
                            "Match score: \(Int(matchEntry.score * 100)) percent"
                        )

                    Text("ReID: \(Int(matchEntry.reidScore * 100))% | Face: \(Int(matchEntry.faceScore * 100))%")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel(
                            "ReID score: \(Int(matchEntry.reidScore * 100)) percent, Face score: \(Int(matchEntry.faceScore * 100)) percent"
                        )

                    // GPS coordinates
                    if let lat = matchEntry.latitude,
                       let lon = matchEntry.longitude {
                        Text(String(format: "%.5f, %.5f", lat, lon))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .accessibilityLabel(
                                String(format: "GPS coordinates: %.5f latitude, %.5f longitude", lat, lon)
                            )
                    }

                    // Detection time
                    Text("Detected at \(matchEntry.time)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                // Dismiss X button (top-right)
                Button(action: onDismiss) {
                    Image(systemName: "xmark")
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .frame(width: 44, height: 44)
                }
                .accessibilityLabel("Dismiss alert")
            }

            // Action buttons
            HStack(spacing: 8) {
                // Navigate button
                if let lat = matchEntry.latitude,
                   let lon = matchEntry.longitude {
                    Button {
                        onNavigate(lat, lon)
                    } label: {
                        Label("Navigate", systemImage: "map.fill")
                            .font(.subheadline)
                            .fontWeight(.semibold)
                    }
                    .buttonStyle(.bordered)
                    .tint(FlightRiskTheme.detectionBlue)
                    .frame(minHeight: 44)
                    .accessibilityLabel("Navigate to match location in Maps")
                }

                // CONFIRM button
                Button(action: onDismiss) {
                    Text("CONFIRM")
                        .font(.subheadline)
                        .fontWeight(.bold)
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.borderedProminent)
                .tint(FlightRiskTheme.matchGreen)
                .accessibilityLabel("Confirm match")

                // NOT MY CHILD button
                Button(action: onNotMyChild) {
                    Text("NOT MY CHILD")
                        .font(.subheadline)
                        .fontWeight(.bold)
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.borderedProminent)
                .tint(FlightRiskTheme.alertOrange)
                .accessibilityLabel("Not my child, suppress this track")
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(.ultraThinMaterial)
                .shadow(color: .black.opacity(0.4), radius: 12, y: 4)
        )
        .padding(.horizontal, 16)
    }
}
