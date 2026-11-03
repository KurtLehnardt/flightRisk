import Foundation
import Observation
import Security

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
    static let claudeKeyUnreadable =
        "The saved Claude API key can't be read right now (the phone may be locked). It will be checked again when the phone is unlocked."
    static let offline = "No internet connection."
    static let claudeNotRegistered = "The Claude backend is not loaded."
    static let gemmaNotReady = "The on-device Gemma model is not downloaded and loaded yet."
}

// MARK: - Persisted settings

/// Persisted reasoning settings: the provider the user picked and the
/// explicit cloud opt-in.
///
/// The provider choice is stored in `UserDefaults(suiteName: "flightrisk_config")`,
/// the same suite as ``FlightRiskConfig``. The opt-in flag is the phone's
/// equivalent of the ground's `FLIGHTRISK_ALLOW_CLOUD_LLM=1`. It is consent
/// given on one phone, so it is kept in a ``CloudConsentStore`` file that
/// device backups exclude; `UserDefaults` is included in backups and must not
/// hold it. ``migrateLegacyValues(keyStore:)`` moves a value written by an
/// earlier build out of `UserDefaults`.
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
    /// The `UserDefaults` key that earlier builds used for the opt-in. It is
    /// read only by the migration and is removed once the value has moved.
    static let cloudOptInKey = "flightrisk_allow_cloud_llm"
    static let legacyKeysRemovedKey = "flightrisk_reasoning_legacy_keys_removed"

    /// Posted synchronously whenever a change may have closed the cloud
    /// gate (opt-out, selecting a non-cloud provider, removing the key).
    /// ``CloudClaudeLlmBackend`` observes it and cancels requests in flight.
    static let cloudGateMayHaveClosed = Notification.Name("FlightRiskCloudGateMayHaveClosed")

    let defaults: UserDefaults
    let consent: CloudConsentStore

    init(defaults: UserDefaults? = nil, consent: CloudConsentStore = .production) {
        self.defaults = defaults ?? UserDefaults(suiteName: Self.suiteName) ?? .standard
        self.consent = consent
    }

    /// True only when the user explicitly confirmed the cloud consent step on
    /// this phone. A new or restored phone has no consent file, so this is false.
    var cloudOptIn: Bool { consent.isGranted }

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
        if !provider.isCloud {
            Self.postGateMayHaveClosed()
        }
        return true
    }

    /// Record the user's answer to the cloud consent step.
    ///
    /// Call with `true` only after the user confirmed the dialog that names
    /// what is sent. Turning the opt-in off also clears a stored cloud
    /// choice, so the next resolution falls back to local or off.
    func setCloudOptIn(_ enabled: Bool) {
        // If the consent file cannot be written the opt-in stays off, so a
        // failure never leaves cloud use on without a recorded consent.
        consent.setGranted(enabled)
        // An explicit decision supersedes any legacy value still waiting in
        // UserDefaults, so a deferred migration can never turn consent back on.
        defaults.removeObject(forKey: Self.cloudOptInKey)
        if !enabled,
           let raw = defaults.string(forKey: Self.providerKey),
           ReasoningProvider(storedValue: raw)?.isCloud == true {
            defaults.removeObject(forKey: Self.providerKey)
        }
        if !enabled {
            Self.postGateMayHaveClosed()
        }
    }

    /// Delete the saved Claude key, then tell the cloud backend so it
    /// cancels any request in flight.
    func removeClaudeKey(from keyStore: ReasoningKeyStore) {
        keyStore.deleteClaudeApiKey()
        Self.postGateMayHaveClosed()
    }

    static func postGateMayHaveClosed() {
        NotificationCenter.default.post(name: cloudGateMayHaveClosed, object: nil)
    }

    /// The cloud gate for ``CloudClaudeLlmBackend``: it returns the Claude
    /// key only while the opt-in is on, Cloud Claude is the selected
    /// provider and a key can be read. It reads all three at call time.
    func cloudGate(keyStore: ReasoningKeyStore) -> CloudGate {
        let settings = self
        return {
            guard settings.cloudOptIn, settings.storedProvider == .cloudClaude else { return nil }
            return keyStore.claudeApiKey()
        }
    }

    /// Trim a pasted key. Returns nil when nothing is left.
    static func normalizedKey(_ input: String) -> String? {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    // MARK: Opt-in migration

    enum OptInMigration: Equatable {
        /// Nothing was left in `UserDefaults`, or it has been dealt with.
        case settled
        /// A legacy opt-in is still in `UserDefaults`; try again next launch.
        case deferred
    }

    /// Move an opt-in that an earlier build stored in `UserDefaults` (which
    /// device backups include) into the consent file (which they exclude).
    ///
    /// - A stored value that is not the Bool `true` was never an opt-in and is
    ///   just removed.
    /// - `true` is carried over only when a Claude key is saved on this
    ///   device. The key is `ThisDeviceOnly`, so it is absent on a phone
    ///   restored from a backup; there the stale flag is dropped and cloud use
    ///   stays off until the user confirms the consent dialog again.
    /// - The `UserDefaults` value is removed only after the consent file has
    ///   been written and read back as granted. If the write fails, or the key
    ///   cannot be read yet because the phone is locked, nothing is removed and
    ///   the migration runs again on the next launch. Until then
    ///   ``cloudOptIn`` is false.
    @discardableResult
    func migrateLegacyOptIn(keyStore: ReasoningKeyStore?) -> OptInMigration {
        guard let stored = defaults.object(forKey: Self.cloudOptInKey) else { return .settled }
        guard (stored as? Bool) == true else {
            defaults.removeObject(forKey: Self.cloudOptInKey)
            return .settled
        }
        if !consent.isGranted {
            guard let keyStore else { return .deferred }
            switch keyStore.lookupClaudeApiKey() {
            case .unreadable:
                return .deferred
            case .missing:
                break  // nothing to carry over; the user must opt in again
            case .found:
                consent.setGranted(true)
                guard consent.isGranted else { return .deferred }
            }
        }
        defaults.removeObject(forKey: Self.cloudOptInKey)
        return .settled
    }

    // MARK: Consent decisions

    /// What the Settings screen should do after a user action.
    enum ConsentDecision: Equatable {
        /// The change was applied and saved.
        case applied
        /// Show the consent dialog. On confirmation, call
        /// ``confirmCloudConsent(selectCloud:)`` with this value.
        case askForConsent(selectCloud: Bool)
    }

    /// The user picked `provider` in the picker. Picking Cloud Claude before
    /// opting in asks for consent instead of selecting it.
    func requestProvider(_ provider: ReasoningProvider) -> ConsentDecision {
        if provider.isCloud && !cloudOptIn {
            return .askForConsent(selectCloud: true)
        }
        select(provider)
        return .applied
    }

    /// The user flipped the "Allow cloud reasoning" toggle. Turning it on
    /// asks for consent; turning it off applies at once.
    func requestCloudOptIn(_ enabled: Bool) -> ConsentDecision {
        if enabled {
            return cloudOptIn ? .applied : .askForConsent(selectCloud: false)
        }
        setCloudOptIn(false)
        return .applied
    }

    /// The user confirmed the consent dialog.
    func confirmCloudConsent(selectCloud: Bool) {
        setCloudOptIn(true)
        if selectCloud {
            select(.cloudClaude)
        }
    }

    /// Clean up values written by earlier builds.
    ///
    /// Moves a cloud opt-in left in `UserDefaults` into the consent file (see
    /// ``migrateLegacyOptIn(keyStore:)``), then removes a stored provider that
    /// is unknown (the removed Gemini and OpenAI entries) or that is a cloud
    /// provider without the opt-in flag. It never sets the opt-in on its own
    /// and never selects a cloud provider.
    func migrateLegacyValues(keyStore: ReasoningKeyStore? = nil) {
        // Earlier builds offered Gemini and OpenAI with no backend behind
        // them. Delete any keys saved for them, once.
        if let keyStore, !defaults.bool(forKey: Self.legacyKeysRemovedKey) {
            keyStore.deleteLegacyProviderKeys()
            defaults.set(true, forKey: Self.legacyKeysRemovedKey)
        }
        // While an opt-in is waiting to move (for example the phone is
        // locked), the provider choice must not be judged against an opt-in
        // that has not arrived yet.
        if migrateLegacyOptIn(keyStore: keyStore) == .deferred { return }
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

// MARK: - Consent store

/// The cloud opt-in, kept in a small file that device backups exclude.
///
/// Why a file and not the Keychain: the flag is not a secret, and a plain file
/// in Application Support marked `isExcludedFromBackup` is the simplest
/// location that a backup and restore does not carry to another phone. A
/// Keychain item with a `ThisDeviceOnly` class would also work, but it adds
/// query and error handling code for no gain here. A restored or new phone has
/// no file, so cloud use defaults to off.
///
/// The file holds the text `granted` while the user has opted in and does not
/// exist otherwise. Any other content, or an unreadable file, counts as not
/// granted.
struct CloudConsentStore: Sendable {
    let fileURL: URL

    private static let grantedMarker = Data("granted".utf8)

    /// `Application Support/FlightRisk/cloud_consent`.
    static let production: CloudConsentStore = {
        let base = (try? FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true
        )) ?? FileManager.default.temporaryDirectory
        return CloudConsentStore(fileURL: base.appendingPathComponent("FlightRisk", isDirectory: true)
            .appendingPathComponent("cloud_consent"))
    }()

    var isGranted: Bool {
        (try? Data(contentsOf: fileURL)) == Self.grantedMarker
    }

    /// Record the user's answer. Returns true when the stored state now
    /// matches `granted`. On failure to grant, nothing is granted.
    @discardableResult
    func setGranted(_ granted: Bool) -> Bool {
        let fm = FileManager.default
        if !granted {
            if fm.fileExists(atPath: fileURL.path) {
                do { try fm.removeItem(at: fileURL) } catch {
                    // Could not delete: overwrite so the content no longer reads as granted.
                    try? Data("revoked".utf8).write(to: fileURL, options: .atomic)
                }
            }
            return !isGranted
        }
        do {
            let directory = fileURL.deletingLastPathComponent()
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
            var directoryURL = directory
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try directoryURL.setResourceValues(values)
            try Self.grantedMarker.write(to: fileURL, options: .atomic)
            var fileCopy = fileURL
            try fileCopy.setResourceValues(values)
        } catch {
            // Never leave a granted file that is not excluded from backup.
            try? fm.removeItem(at: fileURL)
            return false
        }
        return isGranted
    }

    /// True when the consent file exists and backups exclude it.
    var isExcludedFromBackup: Bool {
        (try? fileURL.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup) == true
    }
}

// MARK: - Key store

/// Returns the Claude API key only while cloud use is allowed right now.
typealias CloudGate = @Sendable () -> String?

/// The result of reading the Claude key, without exposing it in
/// descriptions.
enum ClaudeKeyLookup: Equatable, Sendable, CustomStringConvertible {
    case found(String)
    case missing
    /// The Keychain could not be read (for example while the phone is locked).
    case unreadable

    var key: String? {
        if case .found(let key) = self { return key }
        return nil
    }

    var description: String {
        switch self {
        case .found: return "found"
        case .missing: return "missing"
        case .unreadable: return "unreadable"
        }
    }
}

/// Access to the Claude API key.
///
/// The key itself is only handed to ``CloudClaudeLlmBackend`` through the
/// cloud gate. Implementations must never log the key.
protocol ReasoningKeyStore: Sendable {
    func lookupClaudeApiKey() -> ClaudeKeyLookup
    func saveClaudeApiKey(_ key: String) throws
    func deleteClaudeApiKey()
    /// Delete keys saved for the removed Gemini and OpenAI entries.
    func deleteLegacyProviderKeys()
}

extension ReasoningKeyStore {
    func claudeApiKey() -> String? { lookupClaudeApiKey().key }
}

/// The production key store, backed by the Keychain item that
/// ``KeychainHelper`` writes for the "cloud_claude" provider.
struct KeychainReasoningKeyStore: ReasoningKeyStore {
    static let claudeProvider = "cloud_claude"
    static let legacyProviders = ["cloud_gemini", "cloud_openai"]
    /// Must match `KeychainHelper.service` and its account naming.
    private static let service = "com.flightrisk.app"
    private static let claudeAccount = "flightrisk_api_key_cloud_claude"

    func lookupClaudeApiKey() -> ClaudeKeyLookup {
        // Read directly so a locked Keychain can be told apart from a
        // missing key; KeychainHelper.load returns nil for both.
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: Self.claudeAccount,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        switch status {
        case errSecSuccess:
            guard let data = result as? Data,
                  let key = String(data: data, encoding: .utf8),
                  !key.isEmpty else { return .missing }
            return .found(key)
        case errSecItemNotFound:
            return .missing
        default:
            return .unreadable
        }
    }

    func saveClaudeApiKey(_ key: String) throws {
        try KeychainHelper.saveApiKey(key, provider: Self.claudeProvider)
    }

    func deleteClaudeApiKey() {
        KeychainHelper.deleteApiKey(provider: Self.claudeProvider)
    }

    func deleteLegacyProviderKeys() {
        for provider in Self.legacyProviders {
            KeychainHelper.deleteApiKey(provider: provider)
        }
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
