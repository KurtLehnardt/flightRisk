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
                    onPresetSelected: { _ in },
                    onThresholdChanged: { _, _ in },
                    onLlmBackendChanged: { _ in },
                    onApiKeyChanged: { _ in },
                    llmAvailable: false,
                    droneState: droneState,
                    frameSourceMode: frameSourceMode
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
