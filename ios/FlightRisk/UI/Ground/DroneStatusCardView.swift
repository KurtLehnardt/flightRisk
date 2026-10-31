import SwiftUI

/// One drone's status card: link chip, control chip with the HELD countdown, battery, height,
/// lane progress, the drone-link notice, stale and partial markers, Land and (only when the
/// ground allows it) Resume search.
///
/// Land is always enabled while the card exists, whatever the drone's state.
struct DroneStatusCardView: View {
    let model: GroundDroneCardModel
    let onLand: () -> Void
    let onResume: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(model.id).font(.headline)
                chip(model.card.link.text, color: linkColor)
                if let control = model.controlChip {
                    chip(control, color: FlightRiskTheme.alertOrange)
                }
                Spacer(minLength: 4)
            }
            HStack(spacing: 16) {
                value("Battery", model.card.batteryText)
                value("Height", model.card.altitudeText)
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
            HStack(spacing: 12) {
                Button(action: onLand) {
                    Label("Land", systemImage: "arrow.down.to.line")
                }
                .buttonStyle(.borderedProminent)
                .tint(.primary)
                .accessibilityLabel("Land \(model.id)")
                if model.canResume {
                    Button(action: onResume) {
                        Label("Resume search", systemImage: "play")
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel("Resume the search of \(model.id)")
                }
            }
            if let f = model.landFeedback { feedbackLine(f) }
            if let f = model.resumeFeedback { feedbackLine(f) }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
        // Stale values are greyed, but the Land button inside stays fully usable.
        .opacity(model.isStale ? 0.75 : 1)
        .accessibilityElement(children: .contain)
    }

    private var linkColor: Color {
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

    private func feedbackLine(_ f: GroundCommandFeedback) -> some View {
        Text(f.text)
            .font(.footnote.weight(f.isFailure ? .bold : .regular))
            .foregroundStyle(f.isFailure || f.phase == .noReplyYet ? FlightRiskTheme.alertOrange : .secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}
