import XCTest
@testable import FlightRisk

final class FlightRiskConfigTests: XCTestCase {

    // MARK: - Default values

    func testDefaultVisionConfig() {
        let config = FlightRiskConfig()
        XCTAssertEqual(config.vision.detectorConfidence, 0.4)
        XCTAssertEqual(config.vision.reidThreshold, 0.55)
        XCTAssertEqual(config.vision.reidModel, "ViT-B-16")
        XCTAssertEqual(config.vision.faceDetSize.0, 640)
        XCTAssertEqual(config.vision.faceDetSize.1, 640)
        XCTAssertEqual(config.vision.faceMatchThreshold, 0.45)
        XCTAssertEqual(config.vision.scorerMatchThreshold, 0.45)
        XCTAssertEqual(config.vision.scorerReidWeight, 0.35)
        XCTAssertEqual(config.vision.scorerFaceWeight, 0.40)
        XCTAssertEqual(config.vision.scorerReasoningWeight, 0.25)
        XCTAssertEqual(config.vision.trackerIouThreshold, 0.3)
        XCTAssertEqual(config.vision.trackerMaxMissing, 15)
        XCTAssertEqual(config.vision.trackerScoreWindow, 8)
    }

    func testDefaultReasoningConfig() {
        let config = FlightRiskConfig()
        XCTAssertEqual(config.reasoning.model, "gemma4:latest")
        XCTAssertEqual(config.reasoning.queueMaxSize, 10)
        XCTAssertEqual(config.reasoning.alertCooldown, 10.0)
        XCTAssertEqual(config.reasoning.gemmaRateLimit, 5.0)
        XCTAssertEqual(config.reasoning.spatialGridSize, 50)
        XCTAssertEqual(config.reasoning.trackUpdateInterval, 1.0)
        XCTAssertEqual(config.reasoning.reasoningInterval, 5.0)
        XCTAssertEqual(config.reasoning.metricsInterval, 10.0)
        XCTAssertEqual(config.reasoning.corroborationThreshold, 3)
    }

    func testDefaultDroneConfig() {
        let config = FlightRiskConfig()
        XCTAssertEqual(config.drone.telloDefaultHost, "192.168.10.1")
        XCTAssertEqual(config.drone.telloCommandPort, 8889)
        XCTAssertEqual(config.drone.telloVideoPort, 11111)
        XCTAssertEqual(config.drone.telloStatePort, 8890)
        XCTAssertEqual(config.drone.keepaliveIntervalSec, 10)
        XCTAssertEqual(config.drone.statePollingIntervalSec, 2)
        XCTAssertEqual(config.drone.commandTimeoutMs, 7000)
        XCTAssertEqual(config.drone.streamRecoveryThresholdMs, 5000)
        XCTAssertEqual(config.drone.batteryWarnThreshold, 20)
        XCTAssertEqual(config.drone.batteryCriticalThreshold, 10)
        XCTAssertEqual(config.drone.autoConnectInterval, 5.0)
    }

    // MARK: - Singleton

    func testSharedSingletonReturnsSameInstance() {
        let a = FlightRiskConfig.shared
        let b = FlightRiskConfig.shared
        XCTAssertTrue(a === b)
    }

    // MARK: - Sensitivity presets

    func testApplyPresetMoreAlerts() {
        let config = FlightRiskConfig()
        config.applyPreset(.moreAlerts)
        XCTAssertEqual(config.vision.reidThreshold, 0.40)
        XCTAssertEqual(config.vision.scorerMatchThreshold, 0.35)
        XCTAssertEqual(config.vision.faceMatchThreshold, 0.30)
    }

    func testApplyPresetBalanced() {
        let config = FlightRiskConfig()
        // First apply a different preset to verify balanced restores defaults
        config.applyPreset(.moreAlerts)
        config.applyPreset(.balanced)
        XCTAssertEqual(config.vision.reidThreshold, 0.55)
        XCTAssertEqual(config.vision.scorerMatchThreshold, 0.45)
        XCTAssertEqual(config.vision.faceMatchThreshold, 0.45)
    }

    func testApplyPresetFewerAlerts() {
        let config = FlightRiskConfig()
        config.applyPreset(.fewerAlerts)
        XCTAssertEqual(config.vision.reidThreshold, 0.70)
        XCTAssertEqual(config.vision.scorerMatchThreshold, 0.60)
        XCTAssertEqual(config.vision.faceMatchThreshold, 0.55)
    }

    func testApplyPresetDoesNotAffectOtherSettings() {
        let config = FlightRiskConfig()
        let originalDetectorConfidence = config.vision.detectorConfidence
        let originalModel = config.reasoning.model
        config.applyPreset(.fewerAlerts)
        // These should be unchanged
        XCTAssertEqual(config.vision.detectorConfidence, originalDetectorConfidence)
        XCTAssertEqual(config.reasoning.model, originalModel)
    }

    // MARK: - SensitivityPreset values

    func testSensitivityPresetValues() {
        XCTAssertEqual(SensitivityPreset.moreAlerts.reidThreshold, 0.40)
        XCTAssertEqual(SensitivityPreset.balanced.reidThreshold, 0.55)
        XCTAssertEqual(SensitivityPreset.fewerAlerts.reidThreshold, 0.70)

        XCTAssertEqual(SensitivityPreset.moreAlerts.scorerMatchThreshold, 0.35)
        XCTAssertEqual(SensitivityPreset.balanced.scorerMatchThreshold, 0.45)
        XCTAssertEqual(SensitivityPreset.fewerAlerts.scorerMatchThreshold, 0.60)

        XCTAssertEqual(SensitivityPreset.moreAlerts.faceMatchThreshold, 0.30)
        XCTAssertEqual(SensitivityPreset.balanced.faceMatchThreshold, 0.45)
        XCTAssertEqual(SensitivityPreset.fewerAlerts.faceMatchThreshold, 0.55)
    }

    func testSensitivityPresetRawValues() {
        XCTAssertEqual(SensitivityPreset.moreAlerts.rawValue, "More Alerts")
        XCTAssertEqual(SensitivityPreset.balanced.rawValue, "Balanced")
        XCTAssertEqual(SensitivityPreset.fewerAlerts.rawValue, "Fewer Alerts")
    }

    func testSensitivityPresetCaseIterable() {
        let allCases = SensitivityPreset.allCases
        XCTAssertEqual(allCases.count, 3)
        XCTAssertTrue(allCases.contains(.moreAlerts))
        XCTAssertTrue(allCases.contains(.balanced))
        XCTAssertTrue(allCases.contains(.fewerAlerts))
    }

    // MARK: - VisionConfig defaults match scorer/tracker defaults

    func testVisionConfigMatchesScorerDefaults() {
        let config = FlightRiskConfig()
        let scorer = MatchScorer()
        XCTAssertEqual(config.vision.scorerReidWeight, scorer.reidWeight)
        XCTAssertEqual(config.vision.scorerFaceWeight, scorer.faceWeight)
        XCTAssertEqual(config.vision.scorerReasoningWeight, scorer.reasoningWeight)
        XCTAssertEqual(config.vision.scorerMatchThreshold, scorer.matchThreshold)
    }
}
