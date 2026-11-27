import SwiftUI
import XCTest
@testable import FlightRisk

/// Onboarding must not create the live ground view model: creating it starts the lifecycle
/// observers, a one second ticker and the connection. No other test in this target may touch
/// `GroundViewModel.live` before this one runs.
@MainActor
final class OnboardingGroundTests: XCTestCase {
    func testBuildingContentViewDuringOnboardingDoesNotCreateLiveGroundModel() {
        let key = "flightrisk_onboarding_complete"
        let defaults = UserDefaults.standard
        let previous = defaults.object(forKey: key)
        defaults.set(false, forKey: key)
        defer {
            if let previous { defaults.set(previous, forKey: key) } else { defaults.removeObject(forKey: key) }
        }

        let view = ContentView(
            viewModel: SearchViewModel(),
            config: FlightRiskConfig.shared,
            gemmaModelManager: nil,
            pipeline: nil,
            llmSelector: nil
        )
        _ = view.body

        XCTAssertFalse(GroundViewModel.liveWasCreated)
    }
}
