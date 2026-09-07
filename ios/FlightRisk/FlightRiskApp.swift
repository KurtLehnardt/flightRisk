import SwiftUI

@main
struct FlightRiskApp: App {
    @State private var viewModel = SearchViewModel()
    private let config = FlightRiskConfig.shared

    var body: some Scene {
        WindowGroup {
            ContentView(
                viewModel: viewModel,
                cameraSession: .init(),
                config: config,
                droneState: nil,
                frameSourceMode: .camera
            )
        }
    }
}
