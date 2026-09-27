import Foundation
import Observation

/// Centralized configuration for the FlightRisk iOS app.
///
/// Port of `FlightRiskConfig.kt` (Android) and `flightrisk/config.py`
/// (`AmberConfig`) to Swift. Every default mirrors the value in the
/// Android/Python codebase so configs stay in sync across platforms.
///
/// On iOS, overrides come from `UserDefaults(suiteName: "flightrisk_config")`
/// rather than `SharedPreferences`.
@Observable
final class FlightRiskConfig {
    static let shared = FlightRiskConfig()

    var vision = VisionConfig()
    var reasoning = ReasoningConfig()
    var drone = DroneConfig()

    // MARK: - Vision

    /// Detection, ReID, scoring, and tracking parameters.
    /// Defaults match `flightrisk.config.VisionConfig` in Python exactly.
    struct VisionConfig {
        var detectorConfidence: Float = 0.4
        var reidThreshold: Float = 0.55
        var reidModel: String = "ViT-B-16"
        var faceDetSize: (Int, Int) = (640, 640)
        var faceMatchThreshold: Float = 0.45
        var scorerMatchThreshold: Float = 0.45
        var scorerReidWeight: Float = 0.35
        var scorerFaceWeight: Float = 0.40
        var scorerReasoningWeight: Float = 0.25
        var trackerIouThreshold: Float = 0.3
        var trackerMaxMissing: Int = 15
        var trackerScoreWindow: Int = 8
    }

    // MARK: - Reasoning

    /// Gemma reasoning worker: model, queueing, and timing parameters.
    /// Defaults match `flightrisk.config.ReasoningConfig` in Python exactly.
    struct ReasoningConfig {
        var model: String = "gemma4:latest"
        var queueMaxSize: Int = 10
        var alertCooldown: Double = 10.0
        var gemmaRateLimit: Double = 5.0
        var spatialGridSize: Int = 50
        var trackUpdateInterval: Double = 1.0
        var reasoningInterval: Double = 5.0
        var metricsInterval: Double = 10.0
        var corroborationThreshold: Int = 3
    }

    // MARK: - Drone

    /// Drone connection, telemetry, and safety thresholds.
    /// Defaults match `flightrisk.config.DroneConfig` in Python exactly.
    struct DroneConfig {
        var telloDefaultHost: String = "192.168.10.1"
        var telloCommandPort: UInt16 = 8889
        var telloVideoPort: UInt16 = 11111
        var telloStatePort: UInt16 = 8890
        var keepaliveIntervalSec: Int = 10
        var statePollingIntervalSec: Int = 2
        var commandTimeoutMs: Int = 7000
        var streamRecoveryThresholdMs: Int = 5000
        var batteryWarnThreshold: Int = 20
        var batteryCriticalThreshold: Int = 10
        var autoConnectInterval: Double = 5.0
    }

    // MARK: - Signal Configuration

    /// Per-signal enable/disable and weight configuration.
    /// Persisted via UserDefaults. Each signal has an enabled flag and a
    /// weight (0.0-1.0). Model-based signals default to disabled if the
    /// model is not bundled.
    var signalConfig = SignalConfig()

    struct SignalConfig {
        // -- Face Recognition signals --
        var faceEnabled: Bool = true
        var faceWeight: Float = 0.40
        var insightFaceEnabled: Bool = false  // disabled by default (model may not be bundled)
        var insightFaceWeight: Float = 0.35

        // -- Person Re-ID signals --
        var reidEnabled: Bool = true
        var reidWeight: Float = 0.35
        var osnetReidEnabled: Bool = false    // disabled by default (model may not be bundled)
        var osnetReidWeight: Float = 0.30

        // -- Appearance signals --
        var clothingColorEnabled: Bool = true
        var clothingColorWeight: Float = 0.15
        var heightRatioEnabled: Bool = true
        var heightRatioWeight: Float = 0.05

        // -- Other signals --
        var reasoningEnabled: Bool = true
        var reasoningWeight: Float = 0.25

        /// All signal names for iteration.
        static let allSignalNames: [String] = [
            "face", "insightFace",
            "reid", "osnetReid",
            "clothingColor", "heightRatio",
            "reasoning"
        ]

        /// Signal display names by key.
        static let displayNames: [String: String] = [
            "face": "ArcFace",
            "insightFace": "InsightFace R18",
            "reid": "CLIP ReID",
            "osnetReid": "OSNet ReID",
            "clothingColor": "Clothing Color",
            "heightRatio": "Height Ratio",
            "reasoning": "LLM Reasoning"
        ]

        /// Signal categories for UI grouping.
        static let categories: [(name: String, signals: [String])] = [
            ("Face Recognition", ["face", "insightFace"]),
            ("Person Re-ID", ["reid", "osnetReid"]),
            ("Appearance", ["clothingColor", "heightRatio"]),
            ("Other", ["reasoning"])
        ]

        /// Get enabled state for a signal by name.
        func isEnabled(_ name: String) -> Bool {
            switch name {
            case "face": return faceEnabled
            case "insightFace": return insightFaceEnabled
            case "reid": return reidEnabled
            case "osnetReid": return osnetReidEnabled
            case "clothingColor": return clothingColorEnabled
            case "heightRatio": return heightRatioEnabled
            case "reasoning": return reasoningEnabled
            default: return false
            }
        }

        /// Get weight for a signal by name.
        func weight(_ name: String) -> Float {
            switch name {
            case "face": return faceWeight
            case "insightFace": return insightFaceWeight
            case "reid": return reidWeight
            case "osnetReid": return osnetReidWeight
            case "clothingColor": return clothingColorWeight
            case "heightRatio": return heightRatioWeight
            case "reasoning": return reasoningWeight
            default: return 0.0
            }
        }

        /// Set enabled state for a signal by name.
        mutating func setEnabled(_ name: String, enabled: Bool) {
            switch name {
            case "face": faceEnabled = enabled
            case "insightFace": insightFaceEnabled = enabled
            case "reid": reidEnabled = enabled
            case "osnetReid": osnetReidEnabled = enabled
            case "clothingColor": clothingColorEnabled = enabled
            case "heightRatio": heightRatioEnabled = enabled
            case "reasoning": reasoningEnabled = enabled
            default: break
            }
        }

        /// Set weight for a signal by name.
        mutating func setWeight(_ name: String, weight: Float) {
            switch name {
            case "face": faceWeight = weight
            case "insightFace": insightFaceWeight = weight
            case "reid": reidWeight = weight
            case "osnetReid": osnetReidWeight = weight
            case "clothingColor": clothingColorWeight = weight
            case "heightRatio": heightRatioWeight = weight
            case "reasoning": reasoningWeight = weight
            default: break
            }
        }
    }

    // MARK: - Presets

    /// Apply a sensitivity preset, adjusting thresholds to trade off
    /// between alert volume and precision.
    func applyPreset(_ preset: SensitivityPreset) {
        vision.reidThreshold = preset.reidThreshold
        vision.scorerMatchThreshold = preset.scorerMatchThreshold
        vision.faceMatchThreshold = preset.faceMatchThreshold
    }

    // MARK: - UserDefaults Loading

    private static let suiteName = "flightrisk_config"

    /// Load overrides from UserDefaults. Only a curated subset of the
    /// most commonly-tuned values is exposed, matching the Android
    /// `fromPreferences` pattern.
    func loadOverrides() {
        guard let defaults = UserDefaults(suiteName: Self.suiteName) else { return }

        if let confidence = defaults.object(forKey: "FLIGHTRISK_DETECTOR_CONFIDENCE") as? Float {
            vision.detectorConfidence = confidence
        }
        if let reidThreshold = defaults.object(forKey: "FLIGHTRISK_REID_THRESHOLD") as? Float {
            vision.reidThreshold = reidThreshold
        }
        if let scorerThreshold = defaults.object(forKey: "FLIGHTRISK_SCORER_THRESHOLD") as? Float {
            vision.scorerMatchThreshold = scorerThreshold
        }
        if let model = defaults.string(forKey: "FLIGHTRISK_GEMMA_MODEL") {
            reasoning.model = model
        }
        if let cooldown = defaults.object(forKey: "FLIGHTRISK_ALERT_COOLDOWN") as? Double {
            reasoning.alertCooldown = cooldown
        }
        if let queueSize = defaults.object(forKey: "FLIGHTRISK_QUEUE_SIZE") as? Int {
            reasoning.queueMaxSize = queueSize
        }

        // Signal enabled/weight overrides
        for name in SignalConfig.allSignalNames {
            if let enabled = defaults.object(forKey: "FLIGHTRISK_SIGNAL_\(name)_ENABLED") as? Bool {
                signalConfig.setEnabled(name, enabled: enabled)
            }
            if let weight = defaults.object(forKey: "FLIGHTRISK_SIGNAL_\(name)_WEIGHT") as? Float {
                signalConfig.setWeight(name, weight: weight)
            }
        }
    }

    /// Save signal configuration to UserDefaults.
    func saveSignalConfig() {
        guard let defaults = UserDefaults(suiteName: Self.suiteName) else { return }
        for name in SignalConfig.allSignalNames {
            defaults.set(signalConfig.isEnabled(name), forKey: "FLIGHTRISK_SIGNAL_\(name)_ENABLED")
            defaults.set(signalConfig.weight(name), forKey: "FLIGHTRISK_SIGNAL_\(name)_WEIGHT")
        }
    }
}
