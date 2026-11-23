import SwiftUI

/// The pinned bar at the top of the Ground tab: the live pill, the appearance-only badge, the
/// connection banner and LAND ALL.
///
/// LAND ALL is always visible and always enabled, in every connection and fleet state, and it
/// is the only element of the Ground tab in the danger colour. One tap sends it, as on the
/// dashboard; the result line below it always says what happened. While the demo runs it reads
/// "LAND ALL (demo)" and its result starts with "DEMO:", so it is never taken for a live one.
struct GroundTopBar: View {
    let viewModel: GroundViewModel
    /// The most height the scrolling text area may take (`GroundLayout.feedbackMaxHeight`).
    var feedbackMaxHeight: CGFloat = GroundLayout.minFeedbackHeight

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // LAND ALL beside the chips when it fits on one line; otherwise on its own row, full
            // width, where its text wraps and is never cut ("LAND ALL (demo)" at large sizes).
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) {
                    chips
                    Spacer(minLength: 8)
                    landAllButton(fullWidth: false)
                }
                VStack(alignment: .leading, spacing: 8) {
                    chips
                    landAllButton(fullWidth: true)
                }
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

    /// The link pill and the appearance-only badge, side by side or stacked. They stop growing
    /// at the `accessibility2` text size, so they leave room for LAND ALL.
    private var chips: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) { chipViews }
            VStack(alignment: .leading, spacing: 6) { chipViews }
        }
        .dynamicTypeSize(...DynamicTypeSize.accessibility2)
    }

    @ViewBuilder
    private var chipViews: some View {
        LinkPill(status: viewModel.linkStatus)
        if viewModel.groundStatus?.appearanceOnly == true {
            Text("APPEARANCE ONLY")
                .font(.caption2.weight(.bold))
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(Color.secondary.opacity(0.2), in: Capsule())
                .accessibilityLabel("Appearance-only matching")
        }
    }

    private func landAllButton(fullWidth: Bool) -> some View {
        let demo = viewModel.isDemoActive
        return Button {
            Task { await viewModel.landAll() }
        } label: {
            Text(demo ? GroundUIText.landAllButtonDemo : GroundUIText.landAllButton)
                .font(.headline.weight(.heavy))
                .foregroundStyle(.white)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 18)
                .padding(.vertical, 10)
                .frame(maxWidth: fullWidth ? .infinity : nil)
                .background(FlightRiskTheme.alertRed, in: RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(demo ? GroundUIText.landAllButtonDemo : GroundUIText.landAllLabel)
        .accessibilityHint(demo ? GroundUIText.landAllHintDemo : GroundUIText.landAllHint)
        .accessibilityIdentifier("ground.landAll")
    }

    /// The connection text and the LAND ALL result, which can be long and scroll. In the demo the
    /// result comes first: the demo's connection text is two lines that the banner already says,
    /// and it must not push the result out of the visible part of the area.
    private var textArea: some View {
        VStack(alignment: .leading, spacing: 8) {
            if viewModel.linkStatus != .demo { connectionBanner }
            if let feedback = viewModel.landAllFeedback {
                LandAllFeedbackText(feedback: feedback, text: viewModel.shownCommandText(feedback.text))
            }
            if viewModel.linkStatus == .demo { connectionBanner }
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
        case .demo: return .purple
        case .connecting, .reconnecting, .wrongKey, .keychainLocked: return FlightRiskTheme.alertOrange
        default: return .gray
        }
    }
}

/// The text of the latest LAND ALL result. It scrolls with the bar's text area.
private struct LandAllFeedbackText: View {
    let feedback: GroundCommandFeedback
    /// `feedback.text` as shown ("DEMO: " in front while the demo runs).
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: icon)
                Text(text)
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
            Button(viewModel.isDemoActive ? GroundUIText.landAllAgainDemo : GroundUIText.landAllAgain) {
                Task { await viewModel.landAll() }
            }
            .buttonStyle(.bordered)
            if feedback.isFailure || feedback.phase == .partial {
                Button("Dismiss") { viewModel.dismissLandAllFeedback() }
                    .buttonStyle(.borderless)
            }
        }
    }
}
