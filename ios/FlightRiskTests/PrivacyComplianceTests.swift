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

    // MARK: - Privacy policy address

    func testPolicyURLComesFromInfoPlistWithASafeDefault() throws {
        let hosted = "https://example.com/flightrisk/privacy"
        XCTAssertEqual(PrivacyNotice.policyURL(fromInfoValue: hosted).absoluteString, hosted)
        XCTAssertEqual(PrivacyNotice.policyURL(fromInfoValue: "  \(hosted)\n").absoluteString, hosted)
        for bad: Any? in [nil, "", "   ", "$(FLIGHTRISK_PRIVACY_POLICY_URL)", "http://example.com/privacy",
                          "javascript:alert(1)", "https://", 42] {
            XCTAssertEqual(PrivacyNotice.policyURL(fromInfoValue: bad), PrivacyNotice.defaultPolicyURL, "\(String(describing: bad))")
        }
        let plist = try sourcePlist()
        XCTAssertEqual(plist[PrivacyNotice.policyURLInfoKey] as? String, "$(FLIGHTRISK_PRIVACY_POLICY_URL)")
        let yml = try String(contentsOf: iosDirectory.appendingPathComponent("project.yml"), encoding: .utf8)
        XCTAssertTrue(yml.contains("FLIGHTRISK_PRIVACY_POLICY_URL: \(PrivacyNotice.defaultPolicyURL.absoluteString)"),
                      "project.yml and the code agree on the default policy address")
    }

    func testBuiltBundleHasTheExpandedPolicyURL() throws {
        let value = builtPlist()[PrivacyNotice.policyURLInfoKey] as? String
        try XCTSkipIf(value == nil, "tests are not running inside the app bundle")
        XCTAssertFalse(value!.contains("$("), "the build setting was expanded")
        XCTAssertEqual(PrivacyNotice.policyURL.scheme, "https")
        // A value the policy parser rejects would silently become the default, so the expanded
        // value must be exactly what the policy address resolves to.
        XCTAssertEqual(PrivacyNotice.policyURL.absoluteString, value!.trimmingCharacters(in: .whitespacesAndNewlines),
                       "the configured policy address was rejected and replaced by the default")
    }

    func testSettingsLinksToThePrivacyPolicy() throws {
        let source = try String(contentsOf: iosDirectory.appendingPathComponent("FlightRisk/UI/Settings/SettingsView.swift"), encoding: .utf8)
        XCTAssertTrue(source.contains("Link(\"Privacy Policy\", destination: PrivacyNotice.policyURL)"),
                      "the policy must stay reachable from Settings after onboarding")
    }

    // MARK: - Store readiness

    func testExportComplianceKeyDeclaresNoNonExemptEncryption() throws {
        XCTAssertEqual(try sourcePlist()["ITSAppUsesNonExemptEncryption"] as? Bool, false)
        let yml = try String(contentsOf: iosDirectory.appendingPathComponent("project.yml"), encoding: .utf8)
        XCTAssertTrue(yml.contains("ITSAppUsesNonExemptEncryption: false"))
    }

    func testPrivacyManifestDeclaresTheRequiredReasonsAndNoTracking() throws {
        let url = iosDirectory.appendingPathComponent("FlightRisk/PrivacyInfo.xcprivacy")
        let data = try Data(contentsOf: url)
        let manifest = try XCTUnwrap(try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
        XCTAssertEqual(manifest["NSPrivacyTracking"] as? Bool, false)
        XCTAssertEqual((manifest["NSPrivacyTrackingDomains"] as? [String])?.isEmpty, true)

        let accessed = try XCTUnwrap(manifest["NSPrivacyAccessedAPITypes"] as? [[String: Any]])
        var reasons: [String: [String]] = [:]
        for entry in accessed {
            let type = try XCTUnwrap(entry["NSPrivacyAccessedAPIType"] as? String)
            reasons[type] = try XCTUnwrap(entry["NSPrivacyAccessedAPITypeReasons"] as? [String])
        }
        XCTAssertEqual(reasons, [
            "NSPrivacyAccessedAPICategoryUserDefaults": ["CA92.1"],
            "NSPrivacyAccessedAPICategoryDiskSpace": ["E174.1"],
            "NSPrivacyAccessedAPICategoryFileTimestamp": ["C617.1"],
            "NSPrivacyAccessedAPICategorySystemBootTime": ["35F9.1"],
        ])

        let collected = try XCTUnwrap(manifest["NSPrivacyCollectedDataTypes"] as? [[String: Any]])
        let types = Set(collected.compactMap { $0["NSPrivacyCollectedDataType"] as? String })
        XCTAssertEqual(types, ["NSPrivacyCollectedDataTypePhotosorVideos", "NSPrivacyCollectedDataTypeOtherUserContent"])
        for entry in collected {
            XCTAssertEqual(entry["NSPrivacyCollectedDataTypeLinked"] as? Bool, true)
            XCTAssertEqual(entry["NSPrivacyCollectedDataTypeTracking"] as? Bool, false)
            XCTAssertEqual(entry["NSPrivacyCollectedDataTypePurposes"] as? [String], ["NSPrivacyCollectedDataTypePurposeAppFunctionality"])
        }

        let bundled = Bundle.main.url(forResource: "PrivacyInfo", withExtension: "xcprivacy")
        if Bundle.main.infoDictionary?["NSCameraUsageDescription"] != nil {
            XCTAssertNotNil(bundled, "the privacy manifest is copied into the app bundle")
        }
    }

    func testTheAppIsIPhoneOnly() throws {
        let yml = try String(contentsOf: iosDirectory.appendingPathComponent("project.yml"), encoding: .utf8)
        let project = try String(contentsOf: iosDirectory.appendingPathComponent("FlightRisk.xcodeproj/project.pbxproj"), encoding: .utf8)
        XCTAssertFalse(yml.contains("TARGETED_DEVICE_FAMILY: \"1,2\""))
        XCTAssertFalse(project.contains("TARGETED_DEVICE_FAMILY = \"1,2\""))
        XCTAssertTrue(project.contains("TARGETED_DEVICE_FAMILY = 1;"))
        XCTAssertFalse(project.contains("DEVELOPMENT_TEAM ="), "the team comes from Config/Signing.xcconfig, never the project")
    }

    // MARK: - Missing detection model

    func testAMissingDetectionModelIsReportedNotSilent() throws {
        let detector = PersonDetector(modelName: "NoSuchDetectionModel")
        XCTAssertFalse(detector.isAvailable)
        let reason = try XCTUnwrap(detector.unavailableReason)
        XCTAssertEqual(reason, PersonDetector.missingModelReason())
        XCTAssertTrue(reason.contains("Person detection model not installed"))
        XCTAssertTrue(reason.contains("does not include the person detection model"))
        XCTAssertFalse(reason.contains("YOLO"), "no internal model name in text shown to the user")
        XCTAssertTrue(reason.contains("no one will be found"))
    }
}
