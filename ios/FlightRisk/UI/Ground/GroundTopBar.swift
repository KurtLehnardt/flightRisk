import SwiftUI

/// The pinned bar at the top of the Ground tab: the live pill, the appearance-only badge, the
/// connection banner and LAND ALL.
///
/// LAND ALL is always visible and always enabled, in every connection and fleet state, and it
/// is the only element of the Ground tab in the danger colour. One tap sends it, as on the
/// dashboard; the result line below it always says what happened.
struct GroundTopBar: View {
    let viewModel: GroundViewModel
    /// The most height the scrolling text area may take (`GroundLayout.feedbackMaxHeight`).
    var feedbackMaxHeight: CGFloat = GroundLayout.minFeedbackHeight

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
            // The texts from the ground scroll inside a height cap; LAND ALL and every Retry,
            // Dismiss and "Send again" button stay outside it, always visible.
            ViewThatFits(in: .vertical) {
                textArea
                ScrollView { textArea }
            }
            .frame(maxHeight: feedbackMaxHeight)
            connectionActions
            if let feedback = viewModel.landAllFeedback, feedback.offersRetry {
                LandAllFeedbackActions(feedback: feedback, viewModel: viewModel)
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

    /// The connection text and the LAND ALL result, which can be long and scroll.
    private var textArea: some View {
        VStack(alignment: .leading, spacing: 8) {
            connectionBanner
            if let feedback = viewModel.landAllFeedback {
                LandAllFeedbackText(feedback: feedback)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The Retry buttons of the connection banner. They never scroll away.
    @ViewBuilder
    private var connectionActions: some View {
        switch viewModel.linkStatus {
        case .wrongKey:
            Button("Retry") { viewModel.retryConnection() }
                .buttonStyle(.bordered)
                .foregroundStyle(FlightRiskTheme.alertOrange)
                .accessibilityIdentifier("ground.retry")
        case .reconnecting, .connecting:
            Button("Retry now") { viewModel.retryConnection() }
                .buttonStyle(.bordered)
        default:
            EmptyView()
        }
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
            }
            .foregroundStyle(FlightRiskTheme.alertOrange)
        case .reconnecting, .connecting:
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                ProgressView().controlSize(.small)
                Text(viewModel.linkStatus.text)
                    .font(.footnote)
                    .fixedSize(horizontal: false, vertical: true)
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

/// The text of the latest LAND ALL result. It scrolls with the bar's text area.
private struct LandAllFeedbackText: View {
    let feedback: GroundCommandFeedback

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
                Text(detail)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
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

/// "Send LAND ALL again" and Dismiss: failures stay until dismissed; "no reply yet" and
/// failures offer sending LAND ALL again (a new command, never an automatic retry). These
/// buttons are outside the scrolling text area, so they are always reachable.
private struct LandAllFeedbackActions: View {
    let feedback: GroundCommandFeedback
    let viewModel: GroundViewModel

    var body: some View {
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
