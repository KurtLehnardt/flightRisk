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
        XCTAssertFalse(claude.isAvailable, "The key must not open the backend without the opt-in")
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
        XCTAssertFalse(claude.isAvailable)
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
        XCTAssertFalse(claude.isAvailable)
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
        XCTAssertTrue(claude.isAvailable)

        settings.setCloudOptIn(false)
        XCTAssertNil(settings.storedProvider, "Opting out clears the stored cloud choice")
        await selector.refresh()
        status = await selector.status
        XCTAssertEqual(status.active, .off)
        XCTAssertEqual(status.selected, .off)
        XCTAssertFalse(status.isCloudActive)
        XCTAssertEqual(status.unavailable[.cloudClaude], ReasoningReason.cloudBlocked)
        XCTAssertFalse(claude.isAvailable)
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
            XCTFail("Contract fixture not found at \(fixture.path). The test must run from a repository checkout.")
            return
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
        XCTAssertTrue(message.contains("text description you entered"))
        XCTAssertTrue(message.contains("face"))
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

// MARK: - Review round: settings logic

extension ReasoningSettingsTests {

    // Consent decisions (the logic behind the picker and the toggle)

    func testPickingCloudBeforeOptInAsksForConsentAndStoresNothing() {
        XCTAssertEqual(settings.requestProvider(.cloudClaude), .askForConsent(selectCloud: true))
        XCTAssertNil(defaults.object(forKey: ReasoningSettings.providerKey))
        XCTAssertFalse(settings.cloudOptIn)
    }

    func testTurningToggleOnAsksForConsentWithoutSelectingCloud() {
        XCTAssertEqual(settings.requestCloudOptIn(true), .askForConsent(selectCloud: false))
        XCTAssertFalse(settings.cloudOptIn, "Nothing changes until the dialog is confirmed")
    }

    func testConfirmingConsentFromPickerOptsInAndSelectsCloud() {
        guard case .askForConsent(let selectCloud) = settings.requestProvider(.cloudClaude) else {
            return XCTFail("Expected a consent request")
        }
        settings.confirmCloudConsent(selectCloud: selectCloud)
        XCTAssertTrue(settings.cloudOptIn)
        XCTAssertEqual(settings.storedProvider, .cloudClaude)
    }

    func testConfirmingConsentFromToggleOptsInOnly() {
        settings.select(.localGemma)
        guard case .askForConsent(let selectCloud) = settings.requestCloudOptIn(true) else {
            return XCTFail("Expected a consent request")
        }
        settings.confirmCloudConsent(selectCloud: selectCloud)
        XCTAssertTrue(settings.cloudOptIn)
        XCTAssertEqual(settings.storedProvider, .localGemma)
    }

    func testAfterOptInPickingCloudAppliesWithoutAnotherDialog() {
        settings.confirmCloudConsent(selectCloud: false)
        XCTAssertEqual(settings.requestCloudOptIn(true), .applied)
        XCTAssertEqual(settings.requestProvider(.cloudClaude), .applied)
        XCTAssertEqual(settings.storedProvider, .cloudClaude)
    }

    func testTurningToggleOffAppliesAtOnceAndClearsCloudChoice() {
        settings.confirmCloudConsent(selectCloud: true)
        XCTAssertEqual(settings.requestCloudOptIn(false), .applied)
        XCTAssertFalse(settings.cloudOptIn)
        XCTAssertNil(settings.storedProvider)
    }

    func testPickingLocalOrOffAppliesAtOnce() {
        XCTAssertEqual(settings.requestProvider(.localGemma), .applied)
        XCTAssertEqual(settings.storedProvider, .localGemma)
        XCTAssertEqual(settings.requestProvider(.off), .applied)
        XCTAssertEqual(settings.storedProvider, .off)
    }

    // Opt-in flag parsing (NIT 9)

    func testOnlyAStoredBoolCountsAsOptIn() {
        for value in ["true", "YES", "1"] {
            defaults.set(value, forKey: ReasoningSettings.cloudOptInKey)
            XCTAssertFalse(settings.cloudOptIn, "String \(value) must not opt in")
        }
        defaults.set(true, forKey: ReasoningSettings.cloudOptInKey)
        XCTAssertTrue(settings.cloudOptIn)
    }

    // Key trimming (NIT 10)

    func testPastedKeyIsTrimmed() {
        XCTAssertEqual(ReasoningSettings.normalizedKey("  test-key\n"), "test-key")
        XCTAssertNil(ReasoningSettings.normalizedKey(" \n\t "))
    }

    // Legacy keys (LOW 7)

    func testLegacyGeminiAndOpenAIKeysAreDeletedOnce() {
        let store = FakeKeyStore(key: nil)
        _ = LlmSelector(settings: settings, keyStore: store)
        _ = LlmSelector(settings: settings, keyStore: store)
        XCTAssertEqual(store.legacyDeletes, 1)
        XCTAssertEqual(KeychainReasoningKeyStore.legacyProviders, ["cloud_gemini", "cloud_openai"])
    }

    // Locked Keychain (LOW 8)

    func testUnreadableKeyIsReportedSeparatelyFromMissingKey() async {
        settings.confirmCloudConsent(selectCloud: true)
        let store = FakeKeyStore(key: nil)
        store.lookup = .unreadable
        let selector = LlmSelector(settings: settings, keyStore: store)
        await selector.registerBackend(FakeBackend(name: "local_gemma", isAvailable: false))
        await selector.registerBackend(FakeBackend(name: "claude", isAvailable: true))
        var status = await selector.status
        XCTAssertEqual(status.unavailable[.cloudClaude], ReasoningReason.claudeKeyUnreadable)
        XCTAssertNotEqual(ReasoningReason.claudeKeyUnreadable, ReasoningReason.claudeKeyMissing)
        XCTAssertFalse(status.isCloudActive)

        store.lookup = .found("test-key")
        await selector.refresh()
        status = await selector.status
        XCTAssertTrue(status.isCloudActive)
        XCTAssertFalse(ClaudeKeyLookup.found("test-key").description.contains("test-key"))
    }

    // Notifications that cancel requests in flight

    func testOptOutKeyRemovalAndLocalSelectionPostTheGateNotification() {
        let store = FakeKeyStore(key: "test-key")
        settings.confirmCloudConsent(selectCloud: true)
        let counter = CallCounter()
        let observer = NotificationCenter.default.addObserver(
            forName: ReasoningSettings.cloudGateMayHaveClosed, object: nil, queue: nil
        ) { _ in _ = counter.next() }
        defer { NotificationCenter.default.removeObserver(observer) }

        settings.select(.localGemma)
        XCTAssertEqual(counter.count, 1)
        settings.removeClaudeKey(from: store)
        XCTAssertEqual(counter.count, 2)
        settings.setCloudOptIn(false)
        XCTAssertEqual(counter.count, 3)
        XCTAssertNil(store.key)
    }
}

// MARK: - Review round: the cloud backend's gate

/// The Claude backend with a stub `URLProtocol`: nothing reaches the network.
final class CloudClaudeGateTests: XCTestCase {

    private var suiteName: String!
    private var defaults: UserDefaults!
    private var settings: ReasoningSettings!
    private var store: FakeKeyStore!

    override func setUp() {
        super.setUp()
        suiteName = "CloudClaudeGateTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        settings = ReasoningSettings(defaults: defaults)
        store = FakeKeyStore(key: "test-key")
        settings.confirmCloudConsent(selectCloud: true)
        StubURLProtocol.reset()
    }

    override func tearDown() {
        StubURLProtocol.reset()
        defaults.removePersistentDomain(forName: suiteName)
        super.tearDown()
    }

    private func makeBackend(gate: CloudGate? = nil, timeout: TimeInterval = 5) -> CloudClaudeLlmBackend {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        return CloudClaudeLlmBackend(
            timeoutInterval: timeout,
            gate: gate ?? settings.cloudGate(keyStore: store),
            sessionConfiguration: config
        )
    }

    private func analyze(_ backend: CloudClaudeLlmBackend) async -> ReasoningResult {
        await backend.analyzeMatch(referenceImage: Self.image, candidateImage: Self.image, description: "test")
    }

    private static let image: CGImage = {
        let context = CGContext(
            data: nil, width: 4, height: 4, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )!
        context.setFillColor(red: 0.2, green: 0.4, blue: 0.6, alpha: 1)
        context.fill(CGRect(x: 0, y: 0, width: 4, height: 4))
        return context.makeImage()!
    }()

    /// Control: with the gate open the stub sees one request carrying the key,
    /// so the zero-request assertions below are meaningful.
    func testOpenGateSendsOneRequestWithTheKey() async {
        StubURLProtocol.mode = .respond
        let result = await analyze(makeBackend())
        XCTAssertEqual(StubURLProtocol.requests.count, 1)
        XCTAssertEqual(StubURLProtocol.requests.first?.value(forHTTPHeaderField: "x-api-key"), "test-key")
        XCTAssertTrue(result.isMatch)
    }

    func testOptOutWithoutRefreshStopsSendingAtOnce() async {
        let backend = makeBackend()
        let selector = LlmSelector(settings: settings, keyStore: store)
        await selector.registerOnDeviceFirst(local: FakeBackend(name: "local_gemma", isAvailable: false), cloud: backend)
        var active = await selector.getActiveBackend()
        XCTAssertEqual(active.name, "claude")

        settings.setCloudOptIn(false)  // no selector refresh
        XCTAssertFalse(backend.isAvailable)
        active = await selector.getActiveBackend()
        XCTAssertEqual(active.name, "none", "A queued request must not get the cloud backend")
        let result = await analyze(backend)
        XCTAssertEqual(result.confidence, "unavailable")
        XCTAssertEqual(StubURLProtocol.requests.count, 0)
    }

    func testSelectingLocalStopsCloudAtOnce() async {
        let backend = makeBackend()
        settings.select(.localGemma)
        XCTAssertFalse(backend.isAvailable)
        _ = await analyze(backend)
        XCTAssertEqual(StubURLProtocol.requests.count, 0)
    }

    func testRequestInFlightIsCancelledOnOptOut() async {
        await assertInFlightRequestCancelled { self.settings.setCloudOptIn(false) }
    }

    func testRequestInFlightIsCancelledOnKeyRemoval() async {
        await assertInFlightRequestCancelled { self.settings.removeClaudeKey(from: self.store) }
    }

    private func assertInFlightRequestCancelled(
        _ revoke: @escaping () -> Void,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        StubURLProtocol.mode = .hang
        let started = expectation(description: "request started")
        StubURLProtocol.onStart = { started.fulfill() }
        let backend = makeBackend(timeout: 20)
        let call = Task { await self.analyze(backend) }
        await fulfillment(of: [started], timeout: 5)

        let begin = Date()
        revoke()
        let result = await call.value
        XCTAssertEqual(result.confidence, "unavailable", file: file, line: line)
        XCTAssertLessThan(Date().timeIntervalSince(begin), 5, "Cancelled, not timed out", file: file, line: line)
        XCTAssertEqual(StubURLProtocol.requests.count, 1, file: file, line: line)
        XCTAssertGreaterThanOrEqual(StubURLProtocol.stopCount, 1, file: file, line: line)
    }

    func testMissingKeyNeverProducesARequest() async {
        store.key = nil
        _ = await analyze(makeBackend())
        _ = await makeBackend().describeMatch(candidateImage: Self.image, description: "test")
        _ = await analyze(makeBackend(gate: { "" }))
        XCTAssertEqual(StubURLProtocol.requests.count, 0)
    }

    func testKeyRemovedBetweenChecksNeverSendsAnEmptyHeader() async {
        // The gate yields the key for the first check only, as if the key
        // were removed while the images were being encoded.
        let counter = CallCounter()
        let backend = makeBackend(gate: { counter.next() == 0 ? "test-key" : nil })
        let result = await analyze(backend)
        XCTAssertEqual(result.confidence, "unavailable")
        XCTAssertEqual(StubURLProtocol.requests.count, 0)
        XCTAssertGreaterThanOrEqual(counter.count, 2, "The gate must be checked again before sending")
    }

    func testOfflineBlocksSending() async {
        let backend = makeBackend()
        backend.setOnline(false)
        _ = await analyze(backend)
        XCTAssertEqual(StubURLProtocol.requests.count, 0)
    }
}

// MARK: - Test doubles

final class CallCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var calls = 0
    func next() -> Int { lock.withLock { defer { calls += 1 }; return calls } }
    var count: Int { lock.withLock { calls } }
}

/// Records requests instead of sending them.
final class StubURLProtocol: URLProtocol {
    enum Mode { case respond, hang }

    private static let lock = NSLock()
    private static var _mode: Mode = .respond
    private static var _requests: [URLRequest] = []
    private static var _stopCount = 0
    private static var _onStart: (() -> Void)?

    static var mode: Mode {
        get { lock.withLock { _mode } }
        set { lock.withLock { _mode = newValue } }
    }
    static var requests: [URLRequest] { lock.withLock { _requests } }
    static var stopCount: Int { lock.withLock { _stopCount } }
    static var onStart: (() -> Void)? {
        get { lock.withLock { _onStart } }
        set { lock.withLock { _onStart = newValue } }
    }

    static func reset() {
        lock.withLock {
            _mode = .respond
            _requests = []
            _stopCount = 0
            _onStart = nil
        }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let (mode, onStart) = Self.lock.withLock { () -> (Mode, (() -> Void)?) in
            Self._requests.append(request)
            return (Self._mode, Self._onStart)
        }
        onStart?()
        guard mode == .respond else { return }
        let body = #"{"content":[{"type":"text","text":"MATCH: yes\nCONFIDENCE: high\nREASONING: stub"}]}"#
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {
        Self.lock.withLock { Self._stopCount += 1 }
    }
}


final class FakeKeyStore: ReasoningKeyStore, @unchecked Sendable {
    private let lock = NSLock()
    private var _lookup: ClaudeKeyLookup
    private var _legacyDeletes = 0

    init(key: String?) { _lookup = key.map { .found($0) } ?? .missing }

    var key: String? {
        get { lock.withLock { _lookup.key } }
        set { lock.withLock { _lookup = newValue.map { .found($0) } ?? .missing } }
    }

    var lookup: ClaudeKeyLookup {
        get { lock.withLock { _lookup } }
        set { lock.withLock { _lookup = newValue } }
    }

    var legacyDeletes: Int { lock.withLock { _legacyDeletes } }

    func lookupClaudeApiKey() -> ClaudeKeyLookup { lookup }
    func saveClaudeApiKey(_ key: String) throws { self.key = key }
    func deleteClaudeApiKey() { key = nil }
    func deleteLegacyProviderKeys() { lock.withLock { _legacyDeletes += 1 } }
}

final class StatusRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var statuses: [ReasoningStatus] = []

    func append(_ status: ReasoningStatus) { lock.withLock { statuses.append(status) } }
    var all: [ReasoningStatus] { lock.withLock { statuses } }
    var last: ReasoningStatus? { all.last }
}

final class FakeBackend: LlmBackend {
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
