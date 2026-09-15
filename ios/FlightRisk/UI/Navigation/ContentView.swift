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

    var body: some View {
        if onboardingComplete {
            TabView(selection: $selectedTab) {
                SearchView(viewModel: viewModel, cameraSession: cameraSession)
                    .tabItem {
                        Label("Search", systemImage: "magnifyingglass")
                    }
                    .tag(0)
                TargetPickerView { image, report in
                    viewModel.targetPhoto = image
                    viewModel.targetReport = report
                    selectedTab = 0
                }
                    .tabItem {
                        Label("Target", systemImage: "person.crop.circle")
                    }
                    .tag(1)
                SettingsView(
                    config: config,
                    onPresetSelected: { preset in
                        // Config presets are read-only from FlightRiskConfig.shared
                    },
                    onThresholdChanged: { name, value in
                        // Threshold changes are handled by FlightRiskConfig
                    },
                    onLlmBackendChanged: { backend in
                        // Backend changes handled by LlmSelector via notification
                    },
                    onApiKeyChanged: { apiKey in
                        // API key saved to Keychain by SettingsView itself
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
            OnboardingView()
        }
    }
}
