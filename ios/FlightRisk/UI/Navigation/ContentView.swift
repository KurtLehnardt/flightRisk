import AVFoundation
import GroundProtocol
import SwiftUI

struct ContentView: View {
    @AppStorage("flightrisk_onboarding_complete") private var onboardingComplete = false
    @State private var selectedTab = 0
    @State private var confirmingDemo = false

    let viewModel: SearchViewModel
    let cameraSession: AVCaptureSession
    let config: FlightRiskConfig
    let gemmaModelManager: (any GemmaModelManaging)?
    let pipeline: SearchPipeline?
    let llmSelector: LlmSelector?

    /// The demo shows the tabs during onboarding without completing it. It is read through
    /// `DemoModeState`, so the onboarding screens do not create the ground view model; only
    /// "Try the demo" does.
    private var demo: DemoModeState { .shared }

    var body: some View {
        if onboardingComplete || demo.isActive {
            TabView(selection: $selectedTab) {
                searchTab
                    .tabItem {
                        Label("Search", systemImage: "magnifyingglass")
                    }
                    .tag(0)
                TargetPickerView { [pipeline] image, report in
                    viewModel.targetPhoto = image
                    viewModel.targetReport = report
                    Task { await pipeline?.setTargetPhoto(image) }
                    selectedTab = 0
                }
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
                GroundView(viewModel: .live)
                    .tabItem {
                        Label("Ground", systemImage: "antenna.radiowaves.left.and.right")
                    }
                    .tag(3)
            }
            .safeAreaInset(edge: .top, spacing: 0) {
                if demo.isActive {
                    DemoBanner { GroundViewModel.live.stopDemo() }
                }
            }
            .onChange(of: demo.isActive) { _, active in
                guard active else { return }
                // The demo replaces the camera search; a running search stops.
                if viewModel.isSearching { viewModel.stopSearch(reason: "demo_started") }
                selectedTab = 3
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
                    selectedTab = 3
                    DemoStartConfirmation.request(.live, confirm: { confirmingDemo = true })
                }
            )
            .modifier(DemoStartConfirmation(isPresented: $confirmingDemo) { GroundViewModel.live.startDemo() })
        }
    }

    /// The camera search, or during the demo a screen with a drawn placeholder and no camera.
    @ViewBuilder
    private var searchTab: some View {
        if demo.isActive {
            DemoSearchView { selectedTab = 3 }
        } else {
            SearchView(viewModel: viewModel, cameraSession: cameraSession)
        }
    }
}

// MARK: - Demo mode

/// Shown above every tab while the demo runs. It cannot be dismissed; "Exit demo" ends the demo.
struct DemoBanner: View {
    let onExit: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "play.rectangle.fill")
                .accessibilityHidden(true)
            Text(DemoUIText.banner)
                .font(.subheadline.weight(.heavy))
                .accessibilityIdentifier("demo.banner")
            Spacer(minLength: 8)
            Button("Exit demo", action: onExit)
                .font(.subheadline.weight(.semibold))
                .buttonStyle(.bordered)
                .tint(.white)
                .accessibilityIdentifier("demo.exit")
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
        .background(Color.purple)
    }
}

/// The Search tab during the demo: no camera and no photo. The reference is a drawn
/// placeholder generated on the phone (`DemoPlaceholder`), never a photo of a real person.
struct DemoSearchView: View {
    let onOpenGround: () -> Void
    private let placeholder = DemoPlaceholder.makeImage()

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                Text("Search (demo)")
                    .font(.title2.weight(.bold))
                    .accessibilityAddTraits(.isHeader)
                if let placeholder {
                    Image(decorative: placeholder, scale: 1)
                        .resizable()
                        .interpolation(.none)
                        .frame(width: 160, height: 160)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .accessibilityLabel("Demo reference: a drawn figure, not a photo")
                }
                Text("Demo reference: a drawn placeholder, not a photo of a real person.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Text("Outside the demo, this tab uses the camera to look for the person in your reference photo, on this phone. The demo uses no camera and no photo. Open the Ground tab to watch the simulated drones, the obstacle cut, the match alert and its AI verdict.")
                    .font(.body)
                    .multilineTextAlignment(.center)
                Button("Open the Ground tab", action: onOpenGround)
                    .buttonStyle(.borderedProminent)
            }
            .padding(24)
            .frame(maxWidth: .infinity)
        }
    }
}
