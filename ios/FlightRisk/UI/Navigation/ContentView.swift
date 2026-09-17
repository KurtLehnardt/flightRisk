import AVFoundation
import SwiftUI

struct ContentView: View {
    @AppStorage("flightrisk_onboarding_complete") private var onboardingComplete = false
    @State private var selectedTab = 0

    let viewModel: SearchViewModel
    let cameraSession: AVCaptureSession
    let config: FlightRiskConfig
    let droneState: TelloState?
    let frameSourceMode: FrameSourceMode
    let gemmaModelManager: (any GemmaModelManaging)?
    let pipeline: SearchPipeline?
    let llmSelector: LlmSelector?

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
                    llmAvailable: viewModel.activeBackendName != nil && viewModel.activeBackendName != "none",
                    droneState: droneState,
                    frameSourceMode: frameSourceMode,
                    modelManager: gemmaModelManager
                )
                    .tabItem {
                        Label("Settings", systemImage: "gearshape")
                    }
                    .tag(2)
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
