import CoreGraphics
import XCTest
@testable import FlightRisk

/// Tests for the cloud reasoning opt-in (R-21) and the provider picker
/// (R-22). No test touches the network or the real Keychain: keys come from
/// an in-memory store and the fake key is "test-key".
final class ReasoningSettingsTests: XCTestCase {

    private static let fakeKey = "test-key"

    private var suiteName: String!
    private var defaults: UserDefaults!
    private var settings: ReasoningSettings!

    override func setUp() {
        super.setUp()
        suiteName = "ReasoningSettingsTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        settings = ReasoningSettings(defaults: defaults)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        settings = nil
        super.tearDown()
    }

    // MARK: - Helpers

    private func makeSelector(key: String? = nil) -> (LlmSelector, FakeKeyStore) {
        let store = FakeKeyStore(key: key)
        return (LlmSelector(settings: settings, keyStore: store), store)
    }

    private func registerDefaults(
        on selector: LlmSelector,
        localAvailable: Bool
    ) async -> (FakeBackend, CloudClaudeLlmBackend) {
        let gemma = FakeBackend(name: "local_gemma", isAvailable: localAvailable)
        let claude = CloudClaudeLlmBackend()
        await selector.registerOnDeviceFirst(local: gemma, cloud: claude)
        return (gemma, claude)
    }

    // MARK: - Defaults (R-21)

    func testDefaultsAreCloudOffAndNoStoredProvider() {
        XCTAssertFalse(settings.cloudOptIn)
        XCTAssertNil(settings.storedProvider)
        XCTAssertEqual(settings.selectedProvider(localModelAvailable: true), .localGemma)
        XCTAssertEqual(settings.selectedProvider(localModelAvailable: false), .off)
    }

    func testDefaultProviderIsNeverCloud() {
        for local in [true, false] {
            XCTAssertFalse(ReasoningSettings.defaultProvider(localModelAvailable: local).isCloud)
        }
    }

    func testSettingsPersistUnderTheDocumentedKeys() {
        XCTAssertEqual(ReasoningSettings.suiteName, "flightrisk_config")
        XCTAssertEqual(ReasoningSettings.providerKey, "flightrisk_reasoning_provider")
        XCTAssertEqual(ReasoningSettings.cloudOptInKey, "flightrisk_allow_cloud_llm")

        settings.setCloudOptIn(true)
        XCTAssertTrue(settings.select(.cloudClaude))
        let reread = ReasoningSettings(defaults: defaults)
        XCTAssertTrue(reread.cloudOptIn)
        XCTAssertEqual(reread.storedProvider, .cloudClaude)
    }

    func testSavedKeyAloneNeverActivatesClaude() async {
        let (selector, _) = makeSelector(key: Self.fakeKey)
        let (_, claude) = await registerDefaults(on: selector, localAvailable: false)

        let status = await selector.status
        XCTAssertEqual(status.active, .off)
        XCTAssertFalse(status.isCloudActive)
        XCTAssertEqual(status.unavailable[.cloudClaude], ReasoningReason.cloudBlocked)
        XCTAssertEqual(claude.apiKey, "", "The key must not reach the backend without the opt-in")
        let active = await selector.getActiveBackend()
        XCTAssertEqual(active.name, "none")
    }

    func testSelectingCloudWithoutOptInIsRefused() {
        XCTAssertFalse(settings.select(.cloudClaude))
        XCTAssertNil(defaults.string(forKey: ReasoningSettings.providerKey))
    }

    // MARK: - Migration (R-21)

    func testLegacyStoredClaudeWithoutOptInResolvesToLocalOrOff() async {
        defaults.set("claude", forKey: ReasoningSettings.providerKey)
        XCTAssertNil(settings.storedProvider)
        XCTAssertEqual(settings.selectedProvider(localModelAvailable: true), .localGemma)
        XCTAssertEqual(settings.selectedProvider(localModelAvailable: false), .off)

        let (selector, _) = makeSelector(key: Self.fakeKey)
        let (_, claude) = await registerDefaults(on: selector, localAvailable: true)
        let status = await selector.status
        XCTAssertEqual(status.active, .localGemma)
        XCTAssertFalse(status.isCloudActive)
        XCTAssertEqual(claude.apiKey, "")
        // The selector's init migrated the value away; the opt-in stays off.
        XCTAssertNil(defaults.string(forKey: ReasoningSettings.providerKey))
        XCTAssertFalse(settings.cloudOptIn)
    }

    func testMigrationDropsLegacyCloudAndPhantomValuesAndNeverOptsIn() {
        for legacy in ["cloud_claude", "claude", "cloud_gemini", "cloud_openai", "nonsense"] {
            defaults.set(legacy, forKey: ReasoningSettings.providerKey)
            settings.migrateLegacyValues()
            XCTAssertNil(defaults.string(forKey: ReasoningSettings.providerKey), legacy)
            XCTAssertFalse(settings.cloudOptIn, legacy)
        }
    }

    func testMigrationNormalizesLocalSpellings() {
        defaults.set("gemma", forKey: ReasoningSettings.providerKey)
        settings.migrateLegacyValues()
        XCTAssertEqual(defaults.string(forKey: ReasoningSettings.providerKey), "local_gemma")

        defaults.set("none", forKey: ReasoningSettings.providerKey)
        settings.migrateLegacyValues()
        XCTAssertEqual(defaults.string(forKey: ReasoningSettings.providerKey), "off")
    }

    func testMigrationKeepsCloudChoiceWhenOptedIn() {
        settings.setCloudOptIn(true)
        defaults.set("cloud_claude", forKey: ReasoningSettings.providerKey)
        settings.migrateLegacyValues()
        XCTAssertEqual(settings.storedProvider, .cloudClaude)
    }

    // MARK: - Opt-in plus key (R-21)

    func testOptInPlusKeyMakesClaudeAvailableAndActiveWhenSelected() async {
        settings.setCloudOptIn(true)
        XCTAssertTrue(settings.select(.cloudClaude))
        let (selector, _) = makeSelector(key: Self.fakeKey)
        let (_, claude) = await registerDefaults(on: selector, localAvailable: true)

        let status = await selector.status
        XCTAssertNil(status.unavailable[.cloudClaude])
        XCTAssertEqual(status.active, .cloudClaude)
        XCTAssertTrue(status.isCloudActive)
        XCTAssertNil(status.notice)
        XCTAssertTrue(claude.isAvailable)
        let active = await selector.getActiveBackend()
        XCTAssertEqual(active.name, "claude")
    }

    func testOptInPlusKeyDoesNotSwitchAwayFromLocalSelection() async {
        settings.setCloudOptIn(true)
        let (selector, _) = makeSelector(key: Self.fakeKey)
        _ = await registerDefaults(on: selector, localAvailable: true)

        let status = await selector.status
        XCTAssertNil(status.unavailable[.cloudClaude], "Claude is available ...")
        XCTAssertEqual(status.active, .localGemma, "... but not chosen unless the user selects it")
        XCTAssertFalse(status.isCloudActive)
    }

    func testOptInWithoutKeyReportsMissingKeyAndFallsBackWithNotice() async {
        settings.setCloudOptIn(true)
        settings.select(.cloudClaude)
        let (selector, _) = makeSelector(key: nil)
        _ = await registerDefaults(on: selector, localAvailable: false)

        let status = await selector.status
        XCTAssertEqual(status.unavailable[.cloudClaude], ReasoningReason.claudeKeyMissing)
        XCTAssertEqual(status.active, .off)
        XCTAssertEqual(status.selected, .cloudClaude)
        XCTAssertNotNil(status.notice, "The user must be told why nothing is active")
    }

    // MARK: - Removing the key or the opt-in

    func testRemovingKeyFallsBackToLocalWithNotice() async {
        settings.setCloudOptIn(true)
        settings.select(.cloudClaude)
        let (selector, store) = makeSelector(key: Self.fakeKey)
        let (_, claude) = await registerDefaults(on: selector, localAvailable: true)
        var status = await selector.status
        XCTAssertTrue(status.isCloudActive)

        store.key = nil
        await selector.refresh()
        status = await selector.status
        XCTAssertEqual(status.active, .localGemma)
        XCTAssertFalse(status.isCloudActive)
        XCTAssertEqual(status.unavailable[.cloudClaude], ReasoningReason.claudeKeyMissing)
        XCTAssertEqual(claude.apiKey, "")
        let notice = try? XCTUnwrap(status.notice)
        XCTAssertTrue(notice?.contains("Using Local Gemma instead") == true)
    }

    func testRemovingOptInFallsBackAndClearsKeyFromBackend() async {
        settings.setCloudOptIn(true)
        settings.select(.cloudClaude)
        let (selector, _) = makeSelector(key: Self.fakeKey)
        let (_, claude) = await registerDefaults(on: selector, localAvailable: false)
        var status = await selector.status
        XCTAssertTrue(status.isCloudActive)
        XCTAssertFalse(claude.apiKey.isEmpty)

        settings.setCloudOptIn(false)
        XCTAssertNil(settings.storedProvider, "Opting out clears the stored cloud choice")
        await selector.refresh()
        status = await selector.status
        XCTAssertEqual(status.active, .off)
        XCTAssertEqual(status.selected, .off)
        XCTAssertFalse(status.isCloudActive)
        XCTAssertEqual(status.unavailable[.cloudClaude], ReasoningReason.cloudBlocked)
        XCTAssertEqual(claude.apiKey, "")
    }

    func testCloudSelectedButBlockedWithNoLocalModelTellsTheUser() async {
        settings.setCloudOptIn(true)
        settings.select(.cloudClaude)
        let (selector, _) = makeSelector(key: Self.fakeKey)
        _ = await registerDefaults(on: selector, localAvailable: false)
        await selector.handleConnectivityChange(hasInternet: false)

        let status = await selector.status
        XCTAssertEqual(status.active, .off)
        XCTAssertEqual(status.unavailable[.cloudClaude], ReasoningReason.offline)
        XCTAssertTrue(status.notice?.contains("Reasoning is off") == true)
    }

    func testLocalSelectedButNotReadyTellsTheUser() async {
        settings.select(.localGemma)
        let (selector, _) = makeSelector()
        _ = await registerDefaults(on: selector, localAvailable: false)
        let status = await selector.status
        XCTAssertEqual(status.active, .off)
        XCTAssertNotNil(status.notice)
    }

    // MARK: - Registration order

    func testRegistrationOrderIsOnDeviceFirst() async {
        let (selector, _) = makeSelector()
        _ = await registerDefaults(on: selector, localAvailable: true)
        let names = await selector.registeredBackendNames
        XCTAssertEqual(names, ["local_gemma", "claude"])
    }

    func testCloudIsNeverAFallbackEvenWhenRegisteredFirst() async {
        settings.setCloudOptIn(true)
        settings.select(.localGemma)
        let (selector, _) = makeSelector(key: Self.fakeKey)
        await selector.registerBackend(FakeBackend(name: "claude", isAvailable: true))
        await selector.registerBackend(FakeBackend(name: "local_gemma", isAvailable: false))

        let status = await selector.status
        XCTAssertEqual(status.active, .off)
        XCTAssertFalse(status.isCloudActive)
        let active = await selector.getActiveBackend()
        XCTAssertEqual(active.name, "none")
    }

    // MARK: - Reason text

    func testCloudBlockedReasonMirrorsTheDashboard() {
        XCTAssertTrue(ReasoningReason.cloudBlocked.hasPrefix(ReasoningReason.cloudBlockedPrefix))
        XCTAssertTrue(ReasoningReason.cloudBlocked.contains("person crops and the reference photo to Anthropic"))
    }

    /// Compare against the shared contract fixture when the repository is
    /// on disk (it always is for simulator runs from a checkout).
    func testCloudBlockedReasonMatchesTheContractFixture() throws {
        let fixture = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // FlightRiskTests
            .deletingLastPathComponent()  // ios
            .deletingLastPathComponent()  // repository root
            .appendingPathComponent("tests/fixtures/mobile_contract/reasoning_provider.json")
        guard let data = try? Data(contentsOf: fixture) else {
            throw XCTSkip("Contract fixture not reachable from the test host")
        }
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let cases = try XCTUnwrap(json["cases"] as? [String: Any])
        let blocked = try XCTUnwrap(cases["local_gemma_cloud_blocked"] as? [String: Any])
        let payload = try XCTUnwrap(blocked["payload"] as? [String: Any])
        let unavailable = try XCTUnwrap(payload["unavailable"] as? [String: String])
        let dashboardReason = try XCTUnwrap(unavailable["claude"])
        XCTAssertTrue(dashboardReason.hasPrefix(ReasoningReason.cloudBlockedPrefix))
    }

    // MARK: - Picker (R-22)

    func testPickerOffersExactlyLocalGemmaCloudClaudeAndOff() {
        XCTAssertEqual(ReasoningProvider.allCases, [.localGemma, .cloudClaude, .off])
        XCTAssertEqual(ReasoningProvider.allCases.map(\.displayName), ["Local Gemma", "Cloud Claude", "Off"])
        XCTAssertNil(ReasoningProvider(storedValue: "cloud_gemini"))
        XCTAssertNil(ReasoningProvider(storedValue: "cloud_openai"))
    }

    func testConsentMessageNamesWhatIsSentAndHowToStop() {
        let message = SettingsView.cloudConsentMessage
        XCTAssertTrue(message.contains("cropped image of each detected person"))
        XCTAssertTrue(message.contains("reference photo"))
        XCTAssertTrue(message.contains("Anthropic"))
        XCTAssertTrue(message.contains("turn this off at any time"))
    }

    // MARK: - Cloud-active indicator

    @MainActor
    func testReasoningActivityPublishesCloudActive() {
        let activity = ReasoningActivity()
        XCTAssertFalse(activity.isCloudActive)
        var status = ReasoningStatus.initial
        status.active = .cloudClaude
        status.selected = .cloudClaude
        status.cloudOptIn = true
        activity.update(status)
        XCTAssertTrue(activity.isCloudActive)
        status.active = .localGemma
        activity.update(status)
        XCTAssertFalse(activity.isCloudActive)
    }

    func testStatusHandlerReceivesCloudActiveChanges() async {
        settings.setCloudOptIn(true)
        settings.select(.cloudClaude)
        let (selector, store) = makeSelector(key: Self.fakeKey)
        let recorder = StatusRecorder()
        await selector.setStatusHandler { recorder.append($0) }
        _ = await registerDefaults(on: selector, localAvailable: false)
        XCTAssertEqual(recorder.last?.isCloudActive, true)

        store.key = nil
        await selector.refresh()
        XCTAssertEqual(recorder.last?.isCloudActive, false)
    }

    // MARK: - No key material

    func testNoKeyMaterialInStatusOrDescriptions() async {
        settings.setCloudOptIn(true)
        settings.select(.cloudClaude)
        let (selector, _) = makeSelector(key: Self.fakeKey)
        let recorder = StatusRecorder()
        await selector.setStatusHandler { recorder.append($0) }
        _ = await registerDefaults(on: selector, localAvailable: true)

        let status = await selector.status
        XCTAssertTrue(status.isCloudActive)
        var texts = [status.description, String(describing: status), String(reflecting: status)]
        texts += recorder.all.map(\.description)
        texts += status.unavailable.values
        texts.append(status.notice ?? "")
        texts.append(String(describing: settings.defaults.dictionaryRepresentation()))
        for text in texts {
            XCTAssertFalse(text.contains(Self.fakeKey), "Key material leaked into: \(text)")
        }
    }
}

// MARK: - Test doubles

private final class FakeKeyStore: ReasoningKeyStore, @unchecked Sendable {
    private let lock = NSLock()
    private var _key: String?

    init(key: String?) { _key = key }

    var key: String? {
        get { lock.withLock { _key } }
        set { lock.withLock { _key = newValue } }
    }

    func claudeApiKey() -> String? { key }
}

private final class StatusRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var statuses: [ReasoningStatus] = []

    func append(_ status: ReasoningStatus) { lock.withLock { statuses.append(status) } }
    var all: [ReasoningStatus] { lock.withLock { statuses } }
    var last: ReasoningStatus? { all.last }
}

private final class FakeBackend: LlmBackend {
    let name: String
    let isAvailable: Bool

    init(name: String, isAvailable: Bool) {
        self.name = name
        self.isAvailable = isAvailable
    }

    func analyzeMatch(referenceImage: CGImage, candidateImage: CGImage, description: String?) async -> ReasoningResult {
        ReasoningResult(isMatch: false, confidence: "unavailable", reasoning: "fake")
    }

    func describeMatch(candidateImage: CGImage, description: String) async -> ReasoningResult {
        ReasoningResult(isMatch: false, confidence: "unavailable", reasoning: "fake")
    }
}
