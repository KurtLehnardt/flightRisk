import Foundation
import XCTest
@testable import PersonVision

/// The golden numbers committed in `Fixtures/golden-subset.json`: a subset of what
/// `make_models.py verify` writes to `build/mobile_models/golden`, from a drawn frame (no
/// photograph). These tests need no model and no download.
final class GoldenSubsetTests: XCTestCase {
    struct Subset: Decodable {
        struct Frame: Decodable { let width: Int; let height: Int }
        struct Row: Decodable { let index: Int; let f32le_base64: String }
        struct Detection: Decodable { let box: [Double]; let score: Double; let `class`: Int }
        struct CropBox: Decodable { let box: [Double]; let pixels: [Int] }
        struct Crop: Decodable { let width: Int; let height: Int; let rgb_base64: String }
        struct EmbedderInput: Decodable { let sha256: String; let first_values: [Double]; let count: Int }
        let frame: Frame
        let letterbox_ratio: Double
        let score_threshold: Double
        let detector_rows: [Row]
        let detections: [Detection]
        let crop_box: CropBox
        let crop: Crop
        let embedder_input: EmbedderInput
    }

    static func loadSubset() throws -> Subset {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "golden-subset", withExtension: "json", subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Subset.self, from: Data(contentsOf: url))
    }

    /// The `[8400, 85]` output with only the committed rows filled in; every other row scores 0.
    static func detectorOutput(_ s: Subset) throws -> [Float] {
        var out = [Float](repeating: 0, count: YOLOXContract.rowCount * YOLOXContract.rowLength)
        for row in s.detector_rows {
            let data = try XCTUnwrap(Data(base64Encoded: row.f32le_base64))
            XCTAssertEqual(data.count, YOLOXContract.rowLength * 4)
            let values = data.withUnsafeBytes { raw in
                (0..<YOLOXContract.rowLength).map { Float(bitPattern: UInt32(littleEndian: raw.loadUnaligned(fromByteOffset: $0 * 4, as: UInt32.self))) }
            }
            out.replaceSubrange(row.index * YOLOXContract.rowLength ..< (row.index + 1) * YOLOXContract.rowLength, with: values)
        }
        return out
    }

    func testTheLetterboxGeometryOfTheGoldenFrame() throws {
        let s = try Self.loadSubset()
        let g = LetterboxGeometry(frameWidth: s.frame.width, frameHeight: s.frame.height)
        XCTAssertEqual(g.ratio, s.letterbox_ratio)
        XCTAssertEqual(g.resizedWidth, 640)
        XCTAssertEqual(g.resizedHeight, 384)
        XCTAssertEqual(g.padLeft, 0)
        XCTAssertEqual(g.padTop, 0)
    }

    func testDecodingReproducesUpstreamPostprocessExactly() throws {
        let s = try Self.loadSubset()
        let g = LetterboxGeometry(frameWidth: s.frame.width, frameHeight: s.frame.height)
        let got = YOLOXDecoder.decode(try Self.detectorOutput(s), geometry: g,
                                      scoreThreshold: Float(s.score_threshold), classes: nil)
        XCTAssertEqual(got.count, s.detections.count)
        for (a, b) in zip(got, s.detections) {
            XCTAssertEqual(a.classIndex, b.class)
            XCTAssertEqual(a.score, Float(b.score))
            XCTAssertEqual([a.x1, a.y1, a.x2, a.y2] as [Float], b.box.map { Float($0) })
        }
    }

    func testPersonsOnlyKeepsClassZeroInScoreOrder() throws {
        let s = try Self.loadSubset()
        let g = LetterboxGeometry(frameWidth: s.frame.width, frameHeight: s.frame.height)
        let persons = YOLOXDecoder.decode(try Self.detectorOutput(s), geometry: g, scoreThreshold: 0.25)
        let expected = s.detections.filter { $0.class == 0 }
        XCTAssertEqual(persons.count, expected.count)
        XCTAssertEqual(persons.map { $0.score }, expected.map { Float($0.score) })
        XCTAssertTrue(persons.allSatisfy { $0.classIndex == 0 })
    }

    func testTheCropRuleGivesTheGoldenPixels() throws {
        let s = try Self.loadSubset()
        let b = s.crop_box.box.map { Float($0) }
        let p = try XCTUnwrap(CropRule.pixels(x1: b[0], y1: b[1], x2: b[2], y2: b[3],
                                              frameWidth: s.frame.width, frameHeight: s.frame.height))
        XCTAssertEqual([p.x1, p.y1, p.x2, p.y2], s.crop_box.pixels)
        XCTAssertEqual(p.x2 - p.x1, s.crop.width)
        XCTAssertEqual(p.y2 - p.y1, s.crop.height)
    }

    func testTheEmbedderInputIsTheGroundsBitForBit() throws {
        let s = try Self.loadSubset()
        let bytes = try XCTUnwrap(Data(base64Encoded: s.crop.rgb_base64))
        let crop = RGBImage(width: s.crop.width, height: s.crop.height, pixels: [UInt8](bytes))
        let tensor = CLIPPreprocess.input(crop)
        XCTAssertEqual(tensor.count, s.embedder_input.count)
        XCTAssertEqual(Array(tensor.prefix(s.embedder_input.first_values.count)), s.embedder_input.first_values.map { Float($0) })
        var data = Data(capacity: tensor.count * 4)
        for v in tensor { withUnsafeBytes(of: v.bitPattern.littleEndian) { data.append(contentsOf: $0) } }
        XCTAssertEqual(FileDigest.sha256(of: data), s.embedder_input.sha256,
                       "the Swift resize, crop and normalisation must match open_clip's transform exactly")
    }
}
