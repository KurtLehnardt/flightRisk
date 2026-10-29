import Foundation
import Observation

// MARK: - Provider

/// The reasoning providers the phone offers in Settings.
///
/// These mirror the Python providers in `flightrisk/reasoning/__init__.py`
/// (`gemma`, `claude`, `off`). The raw values are the strings persisted in
/// UserDefaults and must not change.
enum ReasoningProvider: String, CaseIterable, Identifiable, Sendable {
    case localGemma = "local_gemma"
    case cloudClaude = "cloud_claude"
    case off = "off"

    var id: String { rawValue }

    /// The label shown in the Settings picker.
    var displayName: String {
        switch self {
        case .localGemma: return "Local Gemma"
        case .cloudClaude: return "Cloud Claude"
        case .off: return "Off"
        }
    }

    /// True for providers that send images off the device.
    var isCloud: Bool { self == .cloudClaude }

    /// Map an ``LlmBackend/name`` to the provider it implements.
    ///
    /// Returns nil for the no-op backend and for unknown names, so an unknown
    /// backend can never be treated as the cloud provider by accident.
    init?(backendName: String) {
        switch backendName {
        case "local_gemma", "gemma": self = .localGemma
        case "claude", "cloud_claude": self = .cloudClaude
        default: return nil
        }
    }

    /// Parse a stored value, including the spellings used by earlier builds
    /// and by the Python side. Unknown values (for example the removed
    /// "cloud_gemini" and "cloud_openai" entries) return nil.
    init?(storedValue: String) {
        switch storedValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "local_gemma", "gemma", "ollama": self = .localGemma
        case "cloud_claude", "claude": self = .cloudClaude
        case "off", "none", "disabled": self = .off
        default: return nil
        }
    }
}

// MARK: - Reason texts

/// User-facing reasons why a provider cannot be used right now.
///
/// The cloud-blocked text mirrors the dashboard's text in
/// `flightrisk/reasoning/__init__.py` (`cloud_block_reason`) and the
/// `reasoning_provider` fixtures, with the phone's opt-in control named in
/// place of the ground's environment variable.
enum ReasoningReason {
    /// The shared first sentence and clause of the dashboard's cloud-blocked
    /// reason, up to the semicolon.
    static let cloudBlockedPrefix =
        "Cloud reasoning is not enabled. Claude would send person crops and the reference photo to Anthropic;"

    static let cloudBlocked = cloudBlockedPrefix + " turn on \"Allow cloud reasoning\" to opt in."
    static let claudeKeyMissing = "No Claude API key is saved on this device."
    static let offline = "No internet connection."
    static let claudeNotRegistered = "The Claude backend is not loaded."
    static let gemmaNotReady = "The on-device Gemma model is not downloaded and loaded yet."
}

// MARK: - Persisted settings

/// Persisted reasoning settings: the provider the user picked and the
/// explicit cloud opt-in.
///
/// Stored in `UserDefaults(suiteName: "flightrisk_config")`, the same suite
/// as ``FlightRiskConfig``. The opt-in flag is the phone's equivalent of the
/// ground's `FLIGHTRISK_ALLOW_CLOUD_LLM=1`.
///
/// Rules:
/// - Cloud is never the default. With nothing stored, the provider is Local
///   Gemma when the on-device model is available and Off otherwise.
/// - The opt-in flag is false unless the user confirmed the consent step.
/// - A stored cloud choice without the opt-in flag (for example a value left
///   by an earlier build) is ignored and removed by ``migrateLegacyValues()``.
/// - Turning the opt-in off clears a stored cloud choice.
/// - API keys never live here; they stay in the Keychain.
struct ReasoningSettings {
    static let suiteName = "flightrisk_config"
    static let providerKey = "flightrisk_reasoning_provider"
    static let cloudOptInKey = "flightrisk_allow_cloud_llm"

    let defaults: UserDefaults

    init(defaults: UserDefaults? = nil) {
        self.defaults = defaults ?? UserDefaults(suiteName: Self.suiteName) ?? .standard
    }

    /// True only when the user explicitly confirmed the cloud consent step.
    var cloudOptIn: Bool {
        defaults.bool(forKey: Self.cloudOptInKey)
    }

    /// The provider the user explicitly chose, or nil when nothing valid is
    /// stored. A stored cloud choice counts only while the opt-in is on.
    var storedProvider: ReasoningProvider? {
        guard let raw = defaults.string(forKey: Self.providerKey),
              let provider = ReasoningProvider(storedValue: raw) else { return nil }
        if provider.isCloud && !cloudOptIn { return nil }
        return provider
    }

    /// The default provider when the user has not chosen one: Local Gemma if
    /// the on-device model is available, otherwise Off. Never cloud.
    static func defaultProvider(localModelAvailable: Bool) -> ReasoningProvider {
        localModelAvailable ? .localGemma : .off
    }

    /// The provider the user has selected, falling back to the default.
    func selectedProvider(localModelAvailable: Bool) -> ReasoningProvider {
        storedProvider ?? Self.defaultProvider(localModelAvailable: localModelAvailable)
    }

    /// Persist the user's choice.
    ///
    /// Selecting Cloud Claude is refused (returns false, nothing stored)
    /// unless the opt-in is already on, so the consent step can never be
    /// skipped.
    @discardableResult
    func select(_ provider: ReasoningProvider) -> Bool {
        if provider.isCloud && !cloudOptIn { return false }
        defaults.set(provider.rawValue, forKey: Self.providerKey)
        return true
    }

    /// Record the user's answer to the cloud consent step.
    ///
    /// Call with `true` only after the user confirmed the dialog that names
    /// what is sent. Turning the opt-in off also clears a stored cloud
    /// choice, so the next resolution falls back to local or off.
    func setCloudOptIn(_ enabled: Bool) {
        defaults.set(enabled, forKey: Self.cloudOptInKey)
        if !enabled,
           let raw = defaults.string(forKey: Self.providerKey),
           ReasoningProvider(storedValue: raw)?.isCloud == true {
            defaults.removeObject(forKey: Self.providerKey)
        }
    }

    /// Clean up values written by earlier builds.
    ///
    /// Removes a stored provider that is unknown (the removed Gemini and
    /// OpenAI entries) or that is a cloud provider without the opt-in flag.
    /// It never sets the opt-in flag and never selects a cloud provider.
    func migrateLegacyValues() {
        guard let raw = defaults.string(forKey: Self.providerKey) else { return }
        guard let provider = ReasoningProvider(storedValue: raw) else {
            defaults.removeObject(forKey: Self.providerKey)
            return
        }
        if provider.isCloud && !cloudOptIn {
            defaults.removeObject(forKey: Self.providerKey)
        } else if raw != provider.rawValue {
            defaults.set(provider.rawValue, forKey: Self.providerKey)
        }
    }
}

// MARK: - Key store

/// Read access to the Claude API key.
///
/// The key itself is only handed to ``CloudClaudeLlmBackend`` by
/// ``LlmSelector`` while the cloud opt-in is on. Implementations must never
/// log the key.
protocol ReasoningKeyStore: Sendable {
    func claudeApiKey() -> String?
}

/// The production key store, backed by ``KeychainHelper``.
struct KeychainReasoningKeyStore: ReasoningKeyStore {
    static let claudeProvider = "cloud_claude"

    func claudeApiKey() -> String? {
        guard let key = KeychainHelper.loadApiKey(provider: Self.claudeProvider),
              !key.isEmpty else { return nil }
        return key
    }
}

// MARK: - Resolved status

/// The resolved reasoning state: what the user selected, what is actually
/// active, and why anything is unavailable.
///
/// Contains no key material, so its description is safe to show or log.
struct ReasoningStatus: Equatable, Sendable, CustomStringConvertible {
    /// The provider the user selected (or the default).
    var selected: ReasoningProvider
    /// The provider actually answering reasoning requests (`.off` for none).
    var active: ReasoningProvider
    /// Whether the user has opted in to cloud reasoning.
    var cloudOptIn: Bool
    /// Providers that cannot be used right now, with the reason to show.
    var unavailable: [ReasoningProvider: String]
    /// Set when the selected provider could not be used and the app fell
    /// back to another one, so the user is told instead of left guessing.
    var notice: String?

    /// True whenever cloud reasoning is the active backend. Views show a
    /// "Cloud active" indicator while this is true.
    var isCloudActive: Bool { active.isCloud }

    static let initial = ReasoningStatus(
        selected: .off,
        active: .off,
        cloudOptIn: false,
        unavailable: [:],
        notice: nil
    )

    var description: String {
        let reasons = unavailable
            .sorted { $0.key.rawValue < $1.key.rawValue }
            .map { "\($0.key.rawValue): \($0.value)" }
            .joined(separator: "; ")
        return "ReasoningStatus(selected: \(selected.rawValue), active: \(active.rawValue), "
            + "cloudOptIn: \(cloudOptIn), cloudActive: \(isCloudActive), "
            + "unavailable: [\(reasons)], notice: \(notice ?? "none"))"
    }
}

// MARK: - Observable status for views

/// The current reasoning status, published for SwiftUI views.
///
/// ``LlmSelector`` pushes every change here (wired in `FlightRiskApp`). Any
/// view can read ``isCloudActive`` to show the "Cloud active" indicator.
@MainActor @Observable
final class ReasoningActivity {
    static let shared = ReasoningActivity()

    private(set) var status: ReasoningStatus = .initial

    /// True whenever cloud reasoning is the active backend.
    var isCloudActive: Bool { status.isCloudActive }

    func update(_ status: ReasoningStatus) {
        if self.status != status {
            self.status = status
        }
    }
}
