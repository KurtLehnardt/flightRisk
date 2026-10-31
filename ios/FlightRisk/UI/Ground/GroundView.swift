import SwiftUI

/// The Ground tab: an operator console for one FlightRisk ground station.
///
/// The top bar with LAND ALL is pinned outside the scrolling content, so it is visible in
/// every state. Everything below is a pure function of `GroundViewModel`.
struct GroundView: View {
    let viewModel: GroundViewModel
    /// The height of this view, measured; nil until the first layout pass.
    @State private var availableHeight: CGFloat?

    var body: some View {
        VStack(spacing: 0) {
            GroundTopBar(viewModel: viewModel, feedbackMaxHeight: GroundLayout.feedbackMaxHeight(available: availableHeight ?? 700))
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if let note = viewModel.staleNote, viewModel.isStateStale, !viewModel.droneCards.isEmpty || viewModel.hasNoRecentUpdate {
                        // First in the list: nothing below is live.
                        Label(note + " Values below are not live.", systemImage: "clock.arrow.circlepath")
                            .font(.subheadline.weight(.semibold))
                            .padding(10)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.secondary.opacity(0.18), in: RoundedRectangle(cornerRadius: 8))
                            .accessibilityIdentifier("ground.staleBanner")
                    }
                    toastList
                    bannerList
                    Text(viewModel.readoutText)
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("ground.readout")
                    ForEach(viewModel.droneCards) { card in
                        DroneStatusCardView(
                            model: card,
                            onLand: { Task { await viewModel.landDrone(card.id) } },
                            onResume: { Task { await viewModel.resumeHeldDrone(card.id) } },
                            onDismissAlert: { viewModel.dismissDroneAlert(card.id) }
                        )
                    }
                    if viewModel.droneCards.isEmpty {
                        Text(emptyText)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }

                    // Slots for the views later units add. Each reads its state from the view
                    // model (see the "State for the views of later units" properties).
                    Group {
                        // U8 video tiles: `viewModel.latestFrames`, `viewModel.streamVideoEnabled`.
                        VideoTilesView(viewModel: viewModel)
                    }
                    Group {
                        PlanGridView(state: PlanGridViewState(plan: viewModel.fleetPlan, isStale: viewModel.isStateStale,
                                                    planIsCurrent: viewModel.planIsCurrent))
                    }
                    Group {
                        // U10 detection profiles and ceiling: `viewModel.detectionProfiles`,
                        // `viewModel.ceilingSettings`.
                    }
                    Group {
                        // U11 ground reasoning provider (read only): `viewModel.reasoningProvider`.
                    }
                    Group {
                        // U11 ground match alerts: `viewModel.matchAlerts`.
                    }
                }
                .padding(16)
            }
            .refreshable { await viewModel.refresh() }
        }
        .background(GeometryReader { proxy in
            Color.clear.preference(key: GroundHeightKey.self, value: proxy.size.height)
        })
        .onPreferenceChange(GroundHeightKey.self) { availableHeight = $0 }
    }

    private var emptyText: String {
        switch viewModel.linkStatus {
        case .connected: return "The ground reports no drones."
        default: return "No drone state yet."
        }
    }

    @ViewBuilder
    private var toastList: some View {
        ForEach(viewModel.toasts) { toast in
            HStack(alignment: .firstTextBaseline) {
                Image(systemName: toast.isError ? "exclamationmark.octagon" : "exclamationmark.triangle")
                Text(toast.text).font(.footnote).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Button("Dismiss") { viewModel.dismissToast(toast.id) }.font(.footnote)
            }
            .foregroundStyle(toast.isError ? FlightRiskTheme.alertOrange : .primary)
            .padding(8)
            .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    @ViewBuilder
    private var bannerList: some View {
        ForEach(viewModel.banners) { banner in
            HStack(alignment: .firstTextBaseline) {
                Text(banner.text)
                    .font(.footnote.weight(banner.isFailure ? .bold : .regular))
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Button("Dismiss") { viewModel.dismissBanner(banner.id) }.font(.footnote)
            }
            .foregroundStyle(banner.isFailure ? FlightRiskTheme.alertOrange : .primary)
            .padding(8)
            .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        }
    }
}

private struct GroundHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}
