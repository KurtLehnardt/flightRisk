import Foundation
import PersonVision
import XCTest
@testable import FlightRisk

/// The stored record's attestation, the all-or-nothing store, and the detector's promise not to
/// load a model when asked why detection is off.
final class CustomDetectorStoreTests: XCTestCase {
    var directory: URL!
    var suite: String!
    var defaults: UserDefaults!
    var compiled: URL!

    override func setUpWithError() throws {
        let fm = FileManager.default
        directory = fm.temporaryDirectory.appendingPathComponent("custom-store-tests-\(UUID().uuidString)", isDirectory: true)
        suite = "CustomDetectorStoreTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
        compiled = fm.temporaryDirectory.appendingPathComponent("fake-\(UUID().uuidString).mlmodelc", isDirectory: true)
        try fm.createDirectory(at: compiled, withIntermediateDirectories: true)
        try Data("weights".utf8).write(to: compiled.appendingPathComponent("weights.bin"))
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
        try? FileManager.default.removeItem(at: compiled)
        defaults.removePersistentDomain(forName: suite)
    }

    private func makeInfo(sha: String = "abc", version: Int = CustomDetectorInfo.currentAttestationVersion) -> CustomDetectorInfo {
        CustomDetectorInfo(sha256: sha, bytes: 7, inputName: "image", outputName: "out",
                           layout: .channelsFirst(classCount: 80), testPictureScore: 0.9,
                           importedAt: Date(timeIntervalSince1970: 1_700_000_000), attestationVersion: version)
    }

    private func encode(_ info: CustomDetectorInfo) throws -> Data {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return try encoder.encode(info)
    }

    func testOldInfoWithoutAttestationFieldsDecodes() throws {
        var object = try JSONSerialization.jsonObject(with: try encode(makeInfo())) as! [String: Any]
        object.removeValue(forKey: "attestationVersion")
        object.removeValue(forKey: "attestedAt")
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let info = try decoder.decode(CustomDetectorInfo.self, from: try JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(info.attestationVersion, CustomDetectorInfo.currentAttestationVersion)
        XCTAssertEqual(info.attestedAt, info.importedAt)
    }

    func testVersionMismatchNeedsReattest() throws {
        let store = CustomDetectorStore(directory: directory, defaults: defaults)
        try store.store(compiled: compiled, info: makeInfo(version: CustomDetectorInfo.currentAttestationVersion + 1))
        XCTAssertNil(store.info)
        XCTAssertTrue(store.needsReattest)
        store.isEnabled = true
        XCTAssertFalse(store.isEnabled)
    }

    func testMatchingVersionIsUsable() throws {
        let store = CustomDetectorStore(directory: directory, defaults: defaults)
        try store.store(compiled: compiled, info: makeInfo())
        XCTAssertNotNil(store.info)
        XCTAssertFalse(store.needsReattest)
    }

    func testFailedInfoWriteLeavesOldPairIntact() throws {
        let good = CustomDetectorStore(directory: directory, defaults: defaults)
        try good.store(compiled: compiled, info: makeInfo(sha: "old"))
        try Data("new weights".utf8).write(to: compiled.appendingPathComponent("weights.bin"))

        struct Boom: Error {}
        let failing = CustomDetectorStore(directory: directory, defaults: defaults, writeInfo: { _, _ in throw Boom() })
        XCTAssertThrowsError(try failing.store(compiled: compiled, info: makeInfo(sha: "new")))

        XCTAssertEqual(good.info?.sha256, "old")
        let weights = try Data(contentsOf: good.modelURL.appendingPathComponent("weights.bin"))
        XCTAssertEqual(String(decoding: weights, as: UTF8.self), "weights")
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: directory.path)
            .filter { $0.hasPrefix("staging-") || $0.hasPrefix("backup-") }
        XCTAssertEqual(leftovers, [])
    }

    func testUnavailableReasonDoesNotLoadAModel() throws {
        var loads = 0
        let store = CustomDetectorStore(directory: directory, defaults: defaults, loadBackend: { _, _ in
            loads += 1
            return nil
        })
        try store.store(compiled: compiled, info: makeInfo())
        store.isEnabled = true
        let detector = PersonDetector(modelName: "no-such-model-\(UUID().uuidString)", customStore: store)

        XCTAssertNil(detector.unavailableReason)
        _ = detector.isAvailable
        _ = detector.isUsingCustomModel
        XCTAssertEqual(loads, 0)
        XCTAssertFalse(detector.isUsingCustomModel)
    }

    func testUnavailableReasonWithoutAnyModelGivesBundledReason() {
        let store = CustomDetectorStore(directory: directory, defaults: defaults, loadBackend: { _, _ in nil })
        let detector = PersonDetector(modelName: "no-such-model-\(UUID().uuidString)", customStore: store)
        XCTAssertEqual(detector.unavailableReason, PersonDetector.missingModelReason())
    }
}
