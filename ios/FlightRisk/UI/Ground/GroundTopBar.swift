import SwiftUI

/// The pinned bar at the top of the Ground tab: the live pill, the appearance-only badge, the
/// connection banner and LAND ALL.
///
/// LAND ALL is always visible and always enabled, in every connection and fleet state, and it
/// is the only element of the Ground tab in the danger colour. One tap sends it, as on the
/// dashboard; the result line below it always says what happened.
struct GroundTopBar: View {
    let viewModel: GroundViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                LinkPill(status: viewModel.linkStatus)
                if viewModel.groundStatus?.appearanceOnly == true {
                    Text("APPEARANCE ONLY")
                        .font(.caption2.weight(.bold))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 3)
                        .background(Color.secondary.opacity(0.2), in: Capsule())
                        .accessibilityLabel("Appearance-only matching")
                }
                Spacer(minLength: 8)
                landAllButton
            }
            connectionBanner
            if let feedback = viewModel.landAllFeedback {
                LandAllFeedbackView(feedback: feedback, viewModel: viewModel)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.bar)
    }

    private var landAllButton: some View {
        Button {
            Task { await viewModel.landAll() }
        } label: {
            Text("LAND ALL")
                .font(.headline.weight(.heavy))
                .foregroundStyle(.white)
                .padding(.horizontal, 18)
                .padding(.vertical, 10)
                .background(FlightRiskTheme.alertRed, in: RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Land all drones")
        .accessibilityHint("Sends LAND ALL to the ground station at once, without a confirmation.")
        .accessibilityIdentifier("ground.landAll")
    }

    @ViewBuilder
    private var connectionBanner: some View {
        switch viewModel.linkStatus {
        case .connected:
            EmptyView()
        case .wrongKey:
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "key.slash")
                Text(viewModel.linkStatus.text)
                    .font(.footnote)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Button("Retry") { viewModel.retryConnection() }
                    .buttonStyle(.bordered)
                    .accessibilityIdentifier("ground.retry")
            }
            .foregroundStyle(FlightRiskTheme.alertOrange)
        case .reconnecting, .connecting:
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                ProgressView().controlSize(.small)
                Text(viewModel.linkStatus.text)
                    .font(.footnote)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Button("Retry now") { viewModel.retryConnection() }
                    .buttonStyle(.bordered)
            }
            .foregroundStyle(.secondary)
        default:
            Text(viewModel.linkStatus.text)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct LinkPill: View {
    let status: GroundLinkStatus

    var body: some View {
        Text(status.pillText)
            .font(.caption.weight(.bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(color, in: Capsule())
            .accessibilityLabel("Ground link: \(status.text)")
    }

    private var color: Color {
        switch status {
        case .connected: return FlightRiskTheme.matchGreen
        case .connecting, .reconnecting, .wrongKey, .keychainLocked: return FlightRiskTheme.alertOrange
        default: return .gray
        }
    }
}

/// The result of the latest LAND ALL. Failures stay until dismissed; "no reply yet" and
/// failures offer sending LAND ALL again (a new command, never an automatic retry).
private struct LandAllFeedbackView: View {
    let feedback: GroundCommandFeedback
    let viewModel: GroundViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: icon)
                Text(feedback.text)
                    .font(.subheadline.weight(feedback.isFailure ? .bold : .regular))
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("ground.landAllResult")
            }
            if let detail = feedback.detail {
                Text(detail).font(.footnote).foregroundStyle(.secondary)
            }
            if feedback.offersRetry {
                HStack {
                    Button("Send LAND ALL again") { Task { await viewModel.landAll() } }
                        .buttonStyle(.bordered)
                    if feedback.isFailure || feedback.phase == .partial {
                        Button("Dismiss") { viewModel.dismissLandAllFeedback() }
                            .buttonStyle(.borderless)
                    }
                }
            }
        }
        .foregroundStyle(feedback.isWarning ? FlightRiskTheme.alertOrange : .primary)
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .contain)
    }

    private var icon: String {
        switch feedback.phase {
        case .sending, .handedOver, .accepted: return "paperplane"
        case .noReplyYet: return "clock.badge.exclamationmark"
        case .succeeded: return "checkmark.circle"
        case .partial: return "exclamationmark.triangle"
        case .failed: return "exclamationmark.triangle.fill"
        }
    }
}
