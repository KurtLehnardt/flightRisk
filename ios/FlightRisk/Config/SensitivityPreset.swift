import Foundation

/// Sensitivity presets that trade off alert volume vs. precision.
///
/// `.moreAlerts` lowers thresholds to catch more potential matches (higher
/// recall, more false positives). `.fewerAlerts` raises them for fewer,
/// higher-confidence alerts. `.balanced` matches the defaults.
///
/// Values are ported exactly from the Android `SensitivityPreset` enum.
enum SensitivityPreset: String, CaseIterable, Identifiable {
    case moreAlerts = "More Alerts"
    case balanced = "Balanced"
    case fewerAlerts = "Fewer Alerts"

    var id: String { rawValue }

    var reidThreshold: Float {
        switch self {
        case .moreAlerts: return 0.40
        case .balanced: return 0.55
        case .fewerAlerts: return 0.70
        }
    }

    var scorerMatchThreshold: Float {
        switch self {
        case .moreAlerts: return 0.35
        case .balanced: return 0.45
        case .fewerAlerts: return 0.60
        }
    }

    var faceMatchThreshold: Float {
        switch self {
        case .moreAlerts: return 0.30
        case .balanced: return 0.45
        case .fewerAlerts: return 0.55
        }
    }

    var description: String {
        switch self {
        case .moreAlerts:
            return "Lower thresholds for higher recall. More alerts, including potential false positives."
        case .balanced:
            return "Default thresholds balancing recall and precision."
        case .fewerAlerts:
            return "Higher thresholds for fewer, higher-confidence alerts."
        }
    }
}
