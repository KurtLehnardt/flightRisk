import AVFoundation
import SwiftUI

struct ContentView: View {
    @AppStorage("flightrisk_onboarding_complete") private var onboardingComplete = false

    let viewModel: SearchViewModel
    let cameraSession: AVCaptureSession
    let config: FlightRiskConfig
    let droneState: TelloState?
    let frameSourceMode: FrameSourceMode

    var body: some View {
        if onboardingComplete {
            TabView {
                SearchView(viewModel: viewModel, cameraSession: cameraSession)
                    .tabItem {
                        Label("Search", systemImage: "magnifyingglass")
                    }
                TargetPickerView(onPhotoSelected: { _, _ in })
                    .tabItem {
                        Label("Target", systemImage: "person.crop.circle")
                    }
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
            }
        } else {
            OnboardingView()
        }
    }
}
