import SwiftUI

/// One drone's status card: link chip, control chip with the HELD countdown, battery, height,
/// lane progress, the drone-link notice, stale and partial markers, failures the ground
/// reported for the drone, Land and (only when the ground allows it) Resume search.
///
/// Land is always enabled while the card exists, whatever the drone's state, and it is never
/// dimmed: only the values are greyed when they are not live.
struct DroneStatusCardView: View {
    let model: GroundDroneCardModel
    let onLand: () -> Void
    let onResume: () -> Void
    let onDismissAlert: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            values
                .opacity(model.isStale ? 0.75 : 1)
            if let alert = model.alertText {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                    Text(alert)
                        .font(.footnote.weight(.bold))
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    Button("Dismiss", action: onDismissAlert).font(.footnote)
                }
                .foregroundStyle(FlightRiskTheme.alertOrange)
                .accessibilityIdentifier("ground.card.alert")
            }
            HStack(spacing: 12) {
                Button(action: onLand) {
                    Label(landTitle, systemImage: "arrow.down.to.line")
                        .foregroundStyle(.white)
                }
                .buttonStyle(.borderedProminent)
                .tint(FlightRiskTheme.detectionBlue)
                .accessibilityLabel("Land \(model.displayId)")
                if model.canResume {
                    Button(action: onResume) {
                        Label("Resume search", systemImage: "play")
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel("Resume the search of \(model.displayId)")
                }
            }
            if let f = model.landFeedback, let text = model.landFeedbackText { feedbackLine(f, text) }
            if let f = model.resumeFeedback, let text = model.resumeFeedbackText { feedbackLine(f, text) }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .contain)
    }

    /// "Land again" once a land failed or got no answer: a second tap is simply another land.
    private var landTitle: String {
        (model.landFeedback?.offersRetry ?? false) ? "Land again" : "Land"
    }

    private var values: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(model.displayId).font(.headline)
                chip(model.linkChipText, color: linkColor)
                    .accessibilityIdentifier("ground.card.link")
                if let control = model.controlChip {
                    chip(control, color: model.isStateStale ? .gray : FlightRiskTheme.alertOrange)
                }
                Spacer(minLength: 4)
            }
            HStack(spacing: 16) {
                value("Battery", model.batteryText)
                value("Height", model.altitudeText)
                if let lane = model.card.laneText {
                    value("Lane", lane)
                }
            }
            if !model.controlText.isEmpty {
                Text(model.controlText)
                    .font(.subheadline.weight(model.card.control.isHeld ? .semibold : .regular))
                    .accessibilityIdentifier("ground.card.control")
            }
            if let notice = model.card.linkNotice {
                Label(notice, systemImage: "antenna.radiowaves.left.and.right.slash")
                    .font(.footnote)
                    .foregroundStyle(FlightRiskTheme.alertOrange)
            }
            if model.isPartial {
                Text(GroundUIText.partialRecord).font(.footnote).foregroundStyle(.secondary)
            }
            if let note = model.staleNote {
                Text(note).font(.footnote).italic().foregroundStyle(.secondary)
            }
        }
    }

    /// Grey while the whole state is stale: a live colour would claim a live link.
    private var linkColor: Color {
        if model.linkChipGreyed { return .gray }
        switch model.card.link {
        case .connected: return FlightRiskTheme.matchGreen
        case .down, .lost: return FlightRiskTheme.alertOrange
        case .unknown: return .gray
        }
    }

    private func chip(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.caption.weight(.bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .background(color, in: Capsule())
    }

    private func value(_ label: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(text).font(.subheadline.monospacedDigit())
        }
        .foregroundStyle(model.isStale ? .secondary : .primary)
        .accessibilityElement(children: .combine)
    }

    private func feedbackLine(_ f: GroundCommandFeedback, _ text: String) -> some View {
        Text(text)
            .font(.footnote.weight(f.isFailure ? .bold : .regular))
            .foregroundStyle(f.isWarning ? FlightRiskTheme.alertOrange : .secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}
