import SwiftUI

@main
struct FlightRiskApp: App {
    @State private var viewModel = SearchViewModel()
    @State private var gemmaModelManager = GemmaModelManager()
    private let config = FlightRiskConfig.shared

    var body: some Scene {
        WindowGroup {
            ContentView(
                viewModel: viewModel,
                cameraSession: .init(),
                config: config,
                droneState: nil,
                frameSourceMode: .camera,
                gemmaModelManager: gemmaModelManager
            )
        }
    }
}
