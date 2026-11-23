import AVFoundation
import SwiftUI

struct ContentView: View {
    @AppStorage("flightrisk_onboarding_complete") private var onboardingComplete = false
    @State private var selectedTab = 0
    @Environment(\.scenePhase) private var scenePhase

    let viewModel: SearchViewModel
    let cameraSession: AVCaptureSession
    let config: FlightRiskConfig
    let gemmaModelManager: (any GemmaModelManaging)?
    let pipeline: SearchPipeline?
    let llmSelector: LlmSelector?

    /// The app's one ground view model (`GroundViewModel.live`), which the Ground tab, the scene
    /// hook and the Settings section all use. A computed property, not a stored one: a stored
    /// property would create the singleton, and start its link, while onboarding is showing.
    private var groundViewModel: GroundViewModel { .live }

    var body: some View {
        if onboardingComplete {
            TabView(selection: $selectedTab) {
                SearchView(viewModel: viewModel, cameraSession: cameraSession)
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
                GroundView(viewModel: groundViewModel)
                    .tabItem {
                        Label("Ground", systemImage: "antenna.radiowaves.left.and.right")
                    }
                    .tag(3)
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
            OnboardingView { [pipeline] image, report in
                viewModel.targetPhoto = image
                viewModel.targetReport = report
                if let pipeline {
                    Task { await pipeline.setTargetPhoto(image) }
                }
            }
        }
    }
}
