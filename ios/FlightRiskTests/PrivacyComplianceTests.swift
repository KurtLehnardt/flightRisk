import XCTest
@testable import FlightRisk

/// Tests for the privacy surfaces (R-24, R-25): the permission strings and
/// App Transport Security setting in Info.plist, and the first-launch notice
/// acknowledgement keys. No test touches the network.
final class PrivacyComplianceTests: XCTestCase {

    // MARK: - Helpers

    /// `ios/` of the checkout, found from this file's path.
    private var iosDirectory: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
    }

    private func sourcePlist() throws -> [String: Any] {
        let url = iosDirectory.appendingPathComponent("FlightRisk/Info.plist")
        let data = try Data(contentsOf: url)
        let object = try PropertyListSerialization.propertyList(from: data, format: nil)
        return try XCTUnwrap(object as? [String: Any])
    }

    private func builtPlist() -> [String: Any] {
        Bundle.main.infoDictionary ?? [:]
    }

    private var suiteName: String!
    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "PrivacyComplianceTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        super.tearDown()
    }

    // MARK: - Info.plist (R-25)

    private func assertPlistRules(_ plist: [String: Any], file: StaticString = #filePath, line: UInt = #line) throws {
        for key in ["NSCameraUsageDescription", "NSLocationWhenInUseUsageDescription",
                    "NSPhotoLibraryUsageDescription", "NSLocalNetworkUsageDescription"] {
            let text = try XCTUnwrap(plist[key] as? String, "\(key) missing", file: file, line: line)
            XCTAssertFalse(text.isEmpty, file: file, line: line)
        }
        let local = try XCTUnwrap(plist["NSLocalNetworkUsageDescription"] as? String, file: file, line: line)
        XCTAssertFalse(local.localizedCaseInsensitiveContains("Tello"), file: file, line: line)
        XCTAssertTrue(local.localizedCaseInsensitiveContains("ground station"), file: file, line: line)

        let ats = try XCTUnwrap(plist["NSAppTransportSecurity"] as? [String: Any], file: file, line: line)
        XCTAssertEqual(ats["NSAllowsLocalNetworking"] as? Bool, true, file: file, line: line)
        XCTAssertNil(ats["NSAllowsArbitraryLoads"], file: file, line: line)
        XCTAssertNil(ats["NSAllowsArbitraryLoadsInWebContent"], file: file, line: line)
        XCTAssertNil(ats["NSExceptionDomains"], file: file, line: line)
        XCTAssertEqual(Set(ats.keys), ["NSAllowsLocalNetworking"], file: file, line: line)
        // No Bonjour browsing is used, so no service list is declared.
        XCTAssertNil(plist["NSBonjourServices"], file: file, line: line)
    }

    func testSourceInfoPlistHasExpectedKeys() throws {
        try assertPlistRules(sourcePlist())
    }

    func testBuiltBundleInfoPlistHasExpectedKeys() throws {
        let plist = builtPlist()
        try XCTSkipIf(plist["NSCameraUsageDescription"] == nil, "tests are not running inside the app bundle")
        try assertPlistRules(plist)
    }

    func testProjectYmlAgreesWithInfoPlist() throws {
        let yml = try String(contentsOf: iosDirectory.appendingPathComponent("project.yml"), encoding: .utf8)
        let plist = try sourcePlist()
        for key in ["NSCameraUsageDescription", "NSLocationWhenInUseUsageDescription",
                    "NSPhotoLibraryUsageDescription", "NSLocalNetworkUsageDescription"] {
            let text = try XCTUnwrap(plist[key] as? String)
            XCTAssertTrue(yml.contains("\(key): \"\(text)\""), "project.yml differs for \(key)")
        }
        XCTAssertTrue(yml.contains("NSAllowsLocalNetworking: true"))
        XCTAssertFalse(yml.contains("NSAllowsArbitraryLoads"))
        XCTAssertFalse(yml.localizedCaseInsensitiveContains("Tello"))
    }

    // MARK: - Notice keys (R-24)

    func testKeyNames() {
        XCTAssertEqual(PrivacyNotice.acknowledgedKey, "flightrisk_privacy_notice_acknowledged")
        XCTAssertEqual(PrivacyNotice.legacyOnboardingCompleteKey, "flightrisk_onboarding_complete")
    }

    func testNeitherKeyShowsNotice() {
        XCTAssertFalse(PrivacyNotice.isAcknowledged(defaults: defaults))
        PrivacyNotice.migrateLegacyAcknowledgement(defaults: defaults)
        XCTAssertFalse(PrivacyNotice.isAcknowledged(defaults: defaults))
        XCTAssertNil(defaults.object(forKey: PrivacyNotice.acknowledgedKey))
    }

    func testOldKeyOnlyCountsAndIsCopiedWithoutLoss() {
        defaults.set(true, forKey: PrivacyNotice.legacyOnboardingCompleteKey)
        XCTAssertTrue(PrivacyNotice.isAcknowledged(defaults: defaults))
        PrivacyNotice.migrateLegacyAcknowledgement(defaults: defaults)
        XCTAssertTrue(defaults.bool(forKey: PrivacyNotice.acknowledgedKey))
        XCTAssertTrue(defaults.bool(forKey: PrivacyNotice.legacyOnboardingCompleteKey))
    }

    func testReadingMigratesOldKeyOnce() {
        defaults.set(true, forKey: PrivacyNotice.legacyOnboardingCompleteKey)
        XCTAssertNil(defaults.object(forKey: PrivacyNotice.acknowledgedKey))
        XCTAssertTrue(PrivacyNotice.isAcknowledged(defaults: defaults))
        XCTAssertTrue(defaults.bool(forKey: PrivacyNotice.acknowledgedKey))
    }

    func testNewKeyOnlyCounts() {
        defaults.set(true, forKey: PrivacyNotice.acknowledgedKey)
        XCTAssertTrue(PrivacyNotice.isAcknowledged(defaults: defaults))
        PrivacyNotice.migrateLegacyAcknowledgement(defaults: defaults)
        XCTAssertFalse(defaults.bool(forKey: PrivacyNotice.legacyOnboardingCompleteKey))
    }

    func testAcknowledgeWritesNewKeyOnly() {
        PrivacyNotice.acknowledge(defaults: defaults)
        XCTAssertTrue(defaults.bool(forKey: PrivacyNotice.acknowledgedKey))
        XCTAssertFalse(defaults.bool(forKey: PrivacyNotice.legacyOnboardingCompleteKey))
    }

    // MARK: - Notice text

    func testNoticeTextMatchesBehaviour() {
        let ground = PrivacyNotice.groundLinkText
        XCTAssertFalse(ground.localizedCaseInsensitiveContains("Tello"))
        XCTAssertTrue(ground.contains("not encrypted"))
        XCTAssertTrue(ground.contains("Keychain"))
        XCTAssertTrue(ground.contains("refuses a ws:// or http:// address unless it is on your local network"))
        XCTAssertTrue(ground.contains("does not pin certificates"))
        XCTAssertFalse(ground.contains("does not limit"))
        XCTAssertTrue(ground.contains("wss://"))
        XCTAssertTrue(ground.contains("resume-search"))
        XCTAssertFalse(ground.contains("over your local network"))

        let cloud = PrivacyNotice.cloudText
        XCTAssertTrue(cloud.contains("Anthropic"))
        XCTAssertTrue(cloud.contains("off"))
        XCTAssertTrue(cloud.contains("reference photo"))
        XCTAssertTrue(cloud.contains("description"))
        XCTAssertFalse(cloud.contains("OpenAI"))
        XCTAssertFalse(cloud.contains("Gemini"))
        XCTAssertEqual(PrivacyNotice.policyURL.scheme, "https")
    }
}
