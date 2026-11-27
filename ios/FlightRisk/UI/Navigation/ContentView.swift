import GroundProtocol
import SwiftUI

struct ContentView: View {
    @AppStorage("flightrisk_onboarding_complete") private var onboardingComplete = false
    @State private var selectedTab = 0
    @State private var confirmingDemo = false
    @State private var demoRefusal: String?
    @Environment(\.scenePhase) private var scenePhase

    let viewModel: SearchViewModel
    let config: FlightRiskConfig
    let gemmaModelManager: (any GemmaModelManaging)?
    let pipeline: SearchPipeline?
    let llmSelector: LlmSelector?

    /// The demo shows the tabs during onboarding without completing it. It is read through
    /// `DemoModeState`, so the onboarding screens do not create the ground view model; only
    /// "Try the demo" does.
    private var demo: DemoModeState { .shared }

    /// The ground view model. Every use in this view goes through here, and it is computed, not
    /// stored: `GroundViewModel.live` is created on first use, so the onboarding screens never
    /// create it (it may open a link) unless "Try the demo" is tapped.
    private var groundViewModel: GroundViewModel { .live }

    var body: some View {
        if onboardingComplete || demo.isActive {
            TabView(selection: $selectedTab) {
                searchTab
                    .tabItem {
                        Label("Search", systemImage: "magnifyingglass")
                    }
                    .tag(0)
                targetTab
                    .tabItem {
                        Label("Target", systemImage: "person.crop.circle")
                    }
                    .tag(1)
                SettingsView(
                    config: config,
                    onPresetSelected: { _ in
                        // Config presets are read-only singletons; changes
                        // apply on next pipeline start
                    },
                    onThresholdChanged: { _, _ in
                        // Config is a singleton; threshold changes apply
                        // on next pipeline start
                    },
                    onLlmBackendChanged: { [llmSelector] _ in
                        // Re-evaluate backend availability after user changes selection
                        Task { await llmSelector?.refreshAsync() }
                    },
                    onApiKeyChanged: { [llmSelector] _ in
                        // API key saved to Keychain by SettingsView; refresh selector
                        // so it picks up the new key's availability status
                        Task { await llmSelector?.refreshAsync() }
                    },
                    modelManager: gemmaModelManager
                )
                    .tabItem {
                        Label("Settings", systemImage: "gearshape")
                    }
                    .tag(2)
                GroundView(viewModel: groundViewModel)
                    .tabItem {
                        Label("Ground", systemImage: "antenna.radiowaves.left.and.right")
                    }
                    .tag(3)
            }
            // The banner sits above the tabs, never over them: nothing can be hidden under it,
            // the Ground tab's pinned LAND ALL bar included.
            .modifier(DemoBannerAbove(isActive: demo.isActive) { groundViewModel.stopDemo() })
            .onChange(of: demo.isActive) { _, active in
                guard active else { return }
                // The demo replaces the drone search; a running search stops.
                if viewModel.isSearching { viewModel.stopSearch(reason: "demo_started") }
                selectedTab = 3
            }
            .onChange(of: viewModel.hasDrone) { _, connected in
                // Here, not only in the Search tab, so a disconnect stops the search even when
                // that tab is not on screen.
                if !connected { viewModel.droneDidDisconnect() }
            }
            .onChange(of: scenePhase) { _, phase in
                // Reconnect the ground link at once instead of after the rest of a
                // backoff wait. Only here, where the Ground tab exists, so the
                // onboarding screens never create the ground view model.
                if phase == .active {
                    groundViewModel.sceneDidBecomeActive()
                }
            }
        } else {
            OnboardingView(
                onPhotoSelected: { [pipeline] image, report in
                    viewModel.targetPhoto = image
                    viewModel.targetReport = report
                    if let pipeline {
                        Task { await pipeline.setTargetPhoto(image) }
                    }
                },
                onTryDemo: {
                    DemoStartConfirmation.request(groundViewModel, start: startDemo,
                                                  confirm: { confirmingDemo = true }, refuse: { demoRefusal = $0 })
                }
            )
            .modifier(DemoStartConfirmation(isPresented: $confirmingDemo, refusal: $demoRefusal, start: startDemo))
        }
    }

    /// Starts the demo after "Try the demo" (and its confirmation, when one was needed) and opens
    /// the Ground tab only once it runs.
    private func startDemo() {
        demoRefusal = DemoStartConfirmation.start(groundViewModel)
        if groundViewModel.isDemoActive { selectedTab = 3 }
    }

    /// Gives the Search tab its drone link and the pipeline its drone video, once. The link is
    /// the only video source: the phone's own camera is never used for searching.
    private func connectDroneLink() async {
        guard viewModel.droneLink == nil else { return }
        let link = GroundDroneLink(groundViewModel)
        // The source goes in first, so a start tapped in between never runs without one.
        await pipeline?.setFrameSource(DroneFrameSource(link: link))
        viewModel.droneLink = link
    }

    /// The drone search, or during the demo a screen with a drawn placeholder and no camera.
    @ViewBuilder
    private var searchTab: some View {
        if demo.isActive {
            DemoTabPlaceholder(title: DemoTabPlaceholder.searchTitle, explanation: DemoTabPlaceholder.searchText,
                               identifier: "demo.search") { selectedTab = 3 }
        } else {
            SearchView(viewModel: viewModel)
                .task { await connectDroneLink() }
        }
    }

    /// The target photo picker, or during the demo the same drawn placeholder: no photo picker.
    @ViewBuilder
    private var targetTab: some View {
        if demo.isActive {
            DemoTabPlaceholder(title: DemoTabPlaceholder.targetTitle, explanation: DemoTabPlaceholder.targetText,
                               identifier: "demo.target") { selectedTab = 3 }
        } else {
            TargetPickerView { [pipeline] image, report in
                viewModel.targetPhoto = image
                viewModel.targetReport = report
                Task { await pipeline?.setTargetPhoto(image) }
                selectedTab = 0
            }
        }
    }
}

// MARK: - Demo mode

/// Puts the demo banner above the content while the demo runs, never over it: the content
/// gets the space below the banner, so nothing it shows (the Ground tab's LAND ALL bar
/// included) can be covered.
struct DemoBannerAbove: ViewModifier {
    let isActive: Bool
    let onExit: () -> Void

    func body(content: Content) -> some View {
        VStack(spacing: 0) {
            if isActive {
                DemoBanner(onExit: onExit)
            }
            content
        }
    }
}

/// Shown above every tab while the demo runs. It cannot be dismissed; "Exit demo" ends the demo.
/// At large text sizes the button moves under the title, and the text stops growing at
/// `accessibility2`, so the banner never takes most of the screen.
struct DemoBanner: View {
    let onExit: () -> Void

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                title
                Spacer(minLength: 8)
                exitButton
            }
            VStack(alignment: .leading, spacing: 6) {
                title
                exitButton
            }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background { Color.purple.ignoresSafeArea(edges: .top) }
        .dynamicTypeSize(...DynamicTypeSize.accessibility2)
    }

    private var title: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "play.rectangle.fill")
                .accessibilityHidden(true)
            Text(DemoUIText.banner)
                .font(.subheadline.weight(.heavy))
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("demo.banner")
        }
    }

    private var exitButton: some View {
        Button("Exit demo", action: onExit)
            .font(.subheadline.weight(.semibold))
            .buttonStyle(.bordered)
            .tint(.white)
            .accessibilityIdentifier("demo.exit")
    }
}

/// The Search and Target tabs during the demo: no photo and no photo picker. The
/// reference is a drawn placeholder generated on the phone (`DemoPlaceholder`), never a photo of
/// a real person.
struct DemoTabPlaceholder: View {
    static let searchTitle = "Search (demo)"
    static let searchText = "Search needs a real drone. Open Ground to watch the simulated drones."
    static let targetTitle = "Target (demo)"
    static let targetText = "The demo needs no photo. Its reference is a drawn placeholder."

    let title: String
    let explanation: String
    let identifier: String
    let onOpenGround: () -> Void
    private let placeholder = DemoPlaceholder.makeImage()

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                Text(title)
                    .font(.title2.weight(.bold))
                    .accessibilityAddTraits(.isHeader)
                if let placeholder {
                    // Decorative: the caption below says what it is.
                    Image(decorative: placeholder, scale: 1)
                        .resizable()
                        .interpolation(.none)
                        .frame(width: 160, height: 160)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                }
                Text("Drawn placeholder, not a real person.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Text(explanation)
                    .font(.body)
                    .multilineTextAlignment(.center)
                Button("Open Ground", action: onOpenGround)
                    .buttonStyle(.borderedProminent)
                    .frame(minHeight: 44)
            }
            .padding(24)
            .frame(maxWidth: .infinity)
        }
        .accessibilityIdentifier(identifier)
    }
}
