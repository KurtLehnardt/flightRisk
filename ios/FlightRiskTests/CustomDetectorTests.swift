import CoreML
import PersonVision
import XCTest
@testable import FlightRisk

/// The optional bring-your-own detector: off by default, refused without the user's statement or
/// with a file that is not a usable model, kept only in app storage, and a fallback to YOLOX-S.
///
/// An end-to-end import runs only when `$FLIGHTRISK_TEST_CUSTOM_DETECTOR` (pass it to
/// `xcodebuild` as `TEST_RUNNER_FLIGHTRISK_TEST_CUSTOM_DETECTOR`) names a zipped `.mlpackage` or a
/// `.mlmodel` on the test machine. FlightRisk never ships or downloads one.
final class CustomDetectorTests: XCTestCase {
    var directory: URL!
    var suite: String!
    var defaults: UserDefaults!
    var store: CustomDetectorStore!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("custom-detector-tests-\(UUID().uuidString)", isDirectory: true)
        suite = "CustomDetectorTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
        store = CustomDetectorStore(directory: directory, defaults: defaults)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
        defaults.removePersistentDomain(forName: suite)
    }

    func scratchFile(_ name: String, bytes: Data) throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("cdt-\(UUID().uuidString)-\(name)")
        try bytes.write(to: url)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func assertImportFails(_ url: URL, attested: Bool = true, with expected: CustomModelError,
                           file: StaticString = #filePath, line: UInt = #line) async {
        do {
            try await store.importModel(from: url, attested: attested)
            XCTFail("the import should fail", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? CustomModelError, expected, file: file, line: line)
        }
        XCTAssertNil(store.info, file: file, line: line)
        XCTAssertFalse(store.isEnabled, file: file, line: line)
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.modelURL.path), file: file, line: line)
    }

    // MARK: - Off by default

    func testOffByDefaultAndCannotBeSwitchedOnWithoutAModel() {
        XCTAssertNil(store.info)
        XCTAssertFalse(store.isEnabled)
        store.isEnabled = true
        XCTAssertFalse(store.isEnabled)
        XCTAssertNil(store.makeBackend())
        XCTAssertFalse(defaults.bool(forKey: CustomDetectorStore.enabledKey))
    }

    func testTheDetectorFallsBackWhenNoCustomModelIsOn() {
        let detector = PersonDetector(modelName: "NoSuchDetectionModel", customStore: store)
        XCTAssertFalse(detector.isAvailable)
        XCTAssertFalse(detector.isUsingCustomModel)
        XCTAssertEqual(detector.unavailableReason, PersonDetector.missingModelReason())
    }

    // MARK: - Refusals

    func testTheStatementIsRequired() async throws {
        let url = try scratchFile("model.mlmodel", bytes: Data([1, 2, 3]))
        await assertImportFails(url, attested: false, with: .notAttested)
    }

    func testOnlyZipAndMLModelFiles() async throws {
        let url = try scratchFile("model.pt", bytes: Data([1, 2, 3]))
        await assertImportFails(url, with: .unsupportedFile)
    }

    func testFilesOver100MBAreRefusedBeforeReading() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("cdt-\(UUID().uuidString)-big.zip")
        XCTAssertTrue(FileManager.default.createFile(atPath: url.path, contents: nil))
        defer { try? FileManager.default.removeItem(at: url) }
        let handle = try FileHandle(forWritingTo: url)
        try handle.truncate(atOffset: 100_000_001) // sparse: no 100 MB is written
        try handle.close()
        await assertImportFails(url, with: .tooLarge(bytes: 100_000_001))
    }

    func testAnEmptyFileIsRefused() async throws {
        await assertImportFails(try scratchFile("model.mlmodel", bytes: Data()), with: .empty)
    }

    func testANonZipIsRefused() async throws {
        let url = try scratchFile("model.zip", bytes: Data(repeating: 7, count: 100))
        await assertImportFails(url, with: .archive(ZipExtractor.ZipError.notAZip.message))
    }

    func testAZipWithoutAPackageIsRefused() async throws {
        // An empty zip archive: just the end-of-central-directory record.
        var eocd = Data([0x50, 0x4B, 0x05, 0x06])
        eocd.append(Data(count: 18))
        await assertImportFails(try scratchFile("model.zip", bytes: eocd), with: .noPackageInArchive)
    }

    func testAFileThatIsNotACoreMLModelDoesNotCompile() async throws {
        let url = try scratchFile("model.mlmodel", bytes: Data("this is not a model".utf8))
        await assertImportFails(url, with: .compileFailed)
    }

    // MARK: - Interface reading

    func testTheInterfaceOfARealModelIsRead() throws {
        guard let url = BundledModels.url(YOLOXContract.modelName) else {
            throw XCTSkip("this build has no models")
        }
        let model = try MLModel(contentsOf: url)
        let interface = CustomDetectorStore.interface(of: model)
        XCTAssertEqual(interface.inputs, [.init(name: "images", kind: .image(width: 640, height: 640))])
        XCTAssertEqual(interface.outputs, [.init(name: "detections", shape: [1, 8400, 85])])
        XCTAssertEqual(interface.metadata["flightrisk.role"], "detector")
    }

    // MARK: - Text

    func testTheGateNamesTheLicenceAndAsksForAStatement() {
        XCTAssertTrue(CustomDetectorText.licence.contains("AGPL-3.0"))
        XCTAssertTrue(CustomDetectorText.licence.contains("Enterprise"))
        XCTAssertEqual(CustomDetectorText.ultralyticsLicensingURL.host(), "www.ultralytics.com")
        XCTAssertTrue(CustomDetectorText.attestation.hasPrefix("I confirm that I have the right to use this model"))
        XCTAssertTrue(CustomDetectorText.intro.contains("does not provide, host or download"))
        XCTAssertTrue(CustomDetectorText.requirements.contains("100 MB"))
        XCTAssertTrue(CustomDetectorText.sectionFooter.contains("Off by default"))
    }

    func testTheOnDeviceNoticeSaysThereIsNoFaceRecognition() {
        XCTAssertTrue(PrivacyNotice.onDeviceText.contains("does not use face recognition"))
        XCTAssertTrue(PrivacyNotice.onDeviceText.contains("appearance"))
    }

    // MARK: - End to end (only with a model on the test machine)

    func testImportingAUsersModelEndToEnd() async throws {
        guard let path = ProcessInfo.processInfo.environment["FLIGHTRISK_TEST_CUSTOM_DETECTOR"], !path.isEmpty else {
            throw XCTSkip("set FLIGHTRISK_TEST_CUSTOM_DETECTOR to a zipped .mlpackage or a .mlmodel to run this")
        }
        // Import a copy: the Settings screen deletes the picked file afterwards, the store does not.
        let source = URL(fileURLWithPath: path)
        let copy = FileManager.default.temporaryDirectory.appendingPathComponent("cdt-\(UUID().uuidString).\(source.pathExtension)")
        try FileManager.default.copyItem(at: source, to: copy)
        defer { try? FileManager.default.removeItem(at: copy) }

        let info = try await store.importModel(from: copy, attested: true)
        XCTAssertEqual(info.sha256, try FileDigest.sha256(of: source))
        XCTAssertGreaterThanOrEqual(info.testPictureScore, CustomModelValidator.personCheckThreshold)
        XCTAssertEqual(store.info, info)
        XCTAssertTrue(store.isEnabled)
        let values = try store.directory.resourceValues(forKeys: [.isExcludedFromBackupKey])
        XCTAssertEqual(values.isExcludedFromBackup, true)

        // With no bundled model, the user's model is the detector; switched off, detection is off.
        let detector = PersonDetector(modelName: "NoSuchDetectionModel", confidence: 0.25, customStore: store)
        XCTAssertTrue(detector.isAvailable)
        XCTAssertTrue(detector.isUsingCustomModel)
        XCTAssertNil(detector.unavailableReason)
        let picture = try XCTUnwrap(CustomDetectorStore.bundledCheckPicture())
        XCTAssertFalse(detector.persons(in: picture).isEmpty)
        store.isEnabled = false
        XCTAssertFalse(detector.isUsingCustomModel)
        XCTAssertFalse(detector.isAvailable)

        // With the bundled model, switching off goes back to YOLOX-S.
        if BundledModels.url(YOLOXContract.modelName) != nil {
            let both = PersonDetector(customStore: store)
            XCTAssertFalse(both.isUsingCustomModel)
            store.isEnabled = true
            XCTAssertTrue(both.isUsingCustomModel)
            store.isEnabled = false
            XCTAssertFalse(both.isUsingCustomModel)
            XCTAssertTrue(both.isAvailable)
        }

        store.remove()
        XCTAssertNil(store.info)
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.directory.path))
    }
}
