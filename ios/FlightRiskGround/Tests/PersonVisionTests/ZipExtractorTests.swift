import Foundation
import XCTest
@testable import PersonVision

/// The zip reader for imported models: what it unpacks and what it refuses.
final class ZipExtractorTests: XCTestCase {
    var scratch: URL!

    override func setUpWithError() throws {
        scratch = FileManager.default.temporaryDirectory.appendingPathComponent("zip-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: scratch)
    }

    /// A zip of stored (uncompressed) entries, built by hand so that each field can be set.
    struct Item {
        var name: String
        var data: Data = Data()
        var flags: UInt16 = 0
        var method: UInt16 = 0
        var madeBy: UInt16 = 0x031E // Unix, version 3.0
        var mode: UInt32 = 0o100644
        var crc: UInt32? = nil
    }

    static func zip(_ items: [Item]) -> Data {
        var out = Data()
        var central = Data()
        func le16(_ v: UInt16, _ d: inout Data) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func le32(_ v: UInt32, _ d: inout Data) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        for item in items {
            let name = Data(item.name.utf8)
            let crc = item.crc ?? CRC32.checksum(item.data)
            let offset = UInt32(out.count)
            le32(0x0403_4B50, &out); le16(20, &out); le16(item.flags, &out); le16(item.method, &out)
            le16(0, &out); le16(0, &out); le32(crc, &out)
            le32(UInt32(item.data.count), &out); le32(UInt32(item.data.count), &out)
            le16(UInt16(name.count), &out); le16(0, &out)
            out.append(name); out.append(item.data)
            le32(0x0201_4B50, &central); le16(item.madeBy, &central); le16(20, &central); le16(item.flags, &central)
            le16(item.method, &central); le16(0, &central); le16(0, &central); le32(crc, &central)
            le32(UInt32(item.data.count), &central); le32(UInt32(item.data.count), &central)
            le16(UInt16(name.count), &central); le16(0, &central); le16(0, &central); le16(0, &central); le16(0, &central)
            le32(item.mode << 16, &central); le32(offset, &central)
            central.append(name)
        }
        let cdOffset = UInt32(out.count)
        out.append(central)
        le32(0x0605_4B50, &out); le16(0, &out); le16(0, &out)
        le16(UInt16(items.count), &out); le16(UInt16(items.count), &out)
        le32(UInt32(central.count), &out); le32(cdOffset, &out); le16(0, &out)
        return out
    }

    func testUnpacksAPackageAndSkipsTheFindersFiles() throws {
        let data = Self.zip([
            Item(name: "Model.mlpackage/", mode: 0o040755),
            Item(name: "Model.mlpackage/Manifest.json", data: Data("{}".utf8)),
            Item(name: "Model.mlpackage/Data/com.apple.CoreML/weights/weight.bin", data: Data([1, 2, 3, 4])),
            Item(name: "__MACOSX/Model.mlpackage/._Manifest.json", data: Data([9])),
            Item(name: "Model.mlpackage/.DS_Store", data: Data([9])),
        ])
        let written = try ZipExtractor.extract(data, to: scratch)
        XCTAssertEqual(written, ["Model.mlpackage/Manifest.json", "Model.mlpackage/Data/com.apple.CoreML/weights/weight.bin"])
        let weights = try Data(contentsOf: scratch.appendingPathComponent("Model.mlpackage/Data/com.apple.CoreML/weights/weight.bin"))
        XCTAssertEqual(weights, Data([1, 2, 3, 4]))
        XCTAssertFalse(FileManager.default.fileExists(atPath: scratch.appendingPathComponent("__MACOSX").path))
    }

    func testRefusesPathsOutsideTheArchive() {
        for name in ["../evil", "a/../../evil", "/etc/evil", "a\\b", "a//b", "./a"] {
            XCTAssertThrowsError(try ZipExtractor.entries(of: Self.zip([Item(name: name, data: Data([1]))])), name) {
                XCTAssertEqual($0 as? ZipExtractor.ZipError, .unsafePath, name)
            }
        }
    }

    func testRefusesSymbolicLinks() {
        let link = Item(name: "Model.mlpackage/link", data: Data("/etc/passwd".utf8), mode: 0o120777)
        XCTAssertThrowsError(try ZipExtractor.entries(of: Self.zip([link]))) {
            XCTAssertEqual($0 as? ZipExtractor.ZipError, .unsafePath)
        }
    }

    func testRefusesEncryptionAndUnknownMethods() {
        XCTAssertThrowsError(try ZipExtractor.entries(of: Self.zip([Item(name: "a", data: Data([1]), flags: 1)]))) {
            XCTAssertEqual($0 as? ZipExtractor.ZipError, .unsupported("encryption"))
        }
        XCTAssertThrowsError(try ZipExtractor.entries(of: Self.zip([Item(name: "a", data: Data([1]), method: 12)]))) {
            XCTAssertEqual($0 as? ZipExtractor.ZipError, .unsupported("compression method 12"))
        }
    }

    func testRefusesMoreThanTheSizeLimitBeforeUnpacking() {
        let data = Self.zip([Item(name: "a", data: Data(count: 600)), Item(name: "b", data: Data(count: 600))])
        XCTAssertThrowsError(try ZipExtractor.extract(data, to: scratch, limits: .init(maxTotalBytes: 1000))) {
            XCTAssertEqual($0 as? ZipExtractor.ZipError, .tooLarge(1200))
        }
        XCTAssertTrue((try? FileManager.default.contentsOfDirectory(atPath: scratch.path))?.isEmpty ?? false)
        XCTAssertThrowsError(try ZipExtractor.entries(of: data, limits: .init(maxEntries: 1))) {
            XCTAssertEqual($0 as? ZipExtractor.ZipError, .tooManyEntries)
        }
    }

    func testRefusesABadChecksumAndNonZips() {
        let bad = Self.zip([Item(name: "a", data: Data([1, 2, 3]), crc: 42)])
        XCTAssertThrowsError(try ZipExtractor.extract(bad, to: scratch)) {
            XCTAssertEqual($0 as? ZipExtractor.ZipError, .corrupt("checksum"))
        }
        XCTAssertThrowsError(try ZipExtractor.entries(of: Data("not a zip at all, not a zip at all".utf8))) {
            XCTAssertEqual($0 as? ZipExtractor.ZipError, .notAZip)
        }
    }

    /// A folder zipped by macOS's own tool (deflate, data descriptors), as the Finder's Compress does.
    func testUnpacksADittoArchiveWithDeflate() throws {
        let ditto = URL(fileURLWithPath: "/usr/bin/ditto")
        guard FileManager.default.isExecutableFile(atPath: ditto.path) else { throw XCTSkip("no ditto") }
        let src = scratch.appendingPathComponent("src/Detector.mlpackage", isDirectory: true)
        try FileManager.default.createDirectory(at: src.appendingPathComponent("Data"), withIntermediateDirectories: true)
        let payload = Data((0..<200_000).map { UInt8(truncatingIfNeeded: $0 * 7 % 13) })
        try payload.write(to: src.appendingPathComponent("Data/weight.bin"))
        try Data("{\"fileFormatVersion\": \"1.0.0\"}".utf8).write(to: src.appendingPathComponent("Manifest.json"))
        let zipURL = scratch.appendingPathComponent("Detector.zip")
        let p = Process()
        p.executableURL = ditto
        p.arguments = ["-c", "-k", "--sequesterRsrc", "--keepParent", src.path, zipURL.path]
        try p.run()
        p.waitUntilExit()
        XCTAssertEqual(p.terminationStatus, 0)
        let out = scratch.appendingPathComponent("out", isDirectory: true)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        let entries = try ZipExtractor.entries(of: Data(contentsOf: zipURL))
        XCTAssertTrue(entries.contains { $0.method == 8 }, "ditto deflates")
        try ZipExtractor.extract(Data(contentsOf: zipURL), to: out)
        XCTAssertEqual(try Data(contentsOf: out.appendingPathComponent("Detector.mlpackage/Data/weight.bin")), payload)
    }

    func testCRC32AndSHA256KnownValues() {
        XCTAssertEqual(CRC32.checksum(Data("123456789".utf8)), 0xCBF4_3926)
        XCTAssertEqual(FileDigest.sha256(of: Data("abc".utf8)), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    }
}
