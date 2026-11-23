import Foundation
import XCTest
@testable import PersonVision

/// Where the Swift resamplers are exact: Pillow 13's bicubic resize always (both pass orders), and
/// the detector letterbox at the frame sizes where OpenCV's bilinear resize is a plain pick or a
/// 2 x 2 mean.
final class ResampleExactnessTests: XCTestCase {
    struct Fixture: Decodable {
        struct Case: Decodable {
            let width: Int
            let height: Int
            let resized_width: Int
            let resized_height: Int
            let vertical_first: Bool
            let resized_sha256: String
            let crop_sha256: String
        }
        let pillow: String
        let cases: [Case]
    }

    /// The fixture's pattern: `((x * 31) ^ (y * 17) ^ (c * 101) ^ ((x * y) >> 3)) & 255`.
    static func pattern(width: Int, height: Int) -> RGBImage {
        var px = [UInt8](repeating: 0, count: width * height * 3)
        for y in 0..<height {
            for x in 0..<width {
                for c in 0..<3 {
                    px[(y * width + x) * 3 + c] = UInt8(((x * 31) ^ (y * 17) ^ (c * 101) ^ ((x * y) >> 3)) & 255)
                }
            }
        }
        return RGBImage(width: width, height: height, pixels: px)
    }

    func testBicubicMatchesPillow13InBothPassOrders() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "pillow-bicubic", withExtension: "json", subdirectory: "Fixtures"))
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
        XCTAssertTrue(fixture.pillow.hasPrefix("13."))
        XCTAssertTrue(fixture.cases.contains { $0.vertical_first }, "the fixture covers the vertical-first order")
        XCTAssertTrue(fixture.cases.contains { !$0.vertical_first })
        for c in fixture.cases {
            let image = Self.pattern(width: c.width, height: c.height)
            let size = CLIPPreprocess.resizedSize(width: c.width, height: c.height)
            XCTAssertEqual(size.width, c.resized_width)
            XCTAssertEqual(size.height, c.resized_height)
            XCTAssertEqual(PillowBicubic.verticalFirst(fromWidth: c.width, fromHeight: c.height,
                                                       toWidth: size.width, toHeight: size.height), c.vertical_first)
            let full = PillowBicubic.resize(image, width: size.width, height: size.height)
            XCTAssertEqual(FileDigest.sha256(of: Data(full.pixels)), c.resized_sha256, "\(c.width) x \(c.height)")
            let crop = CLIPPreprocess.resizeAndCrop(image)
            XCTAssertEqual(FileDigest.sha256(of: Data(crop.pixels)), c.crop_sha256, "centre crop of \(c.width) x \(c.height)")
        }
    }

    func testTheWindowIsTheSameAsCroppingTheFullResize() {
        let image = Self.pattern(width: 300, height: 900)
        let full = PillowBicubic.resize(image, width: 224, height: 672)
        let window = PillowBicubic.resize(image, width: 224, height: 672, columns: 13..<200, rows: 300..<417)
        XCTAssertEqual(window, full.cropped(x1: 13, y1: 300, x2: 200, y2: 417))
        let wide = Self.pattern(width: 900, height: 300)
        let fullWide = PillowBicubic.resize(wide, width: 672, height: 224)
        XCTAssertEqual(PillowBicubic.resize(wide, width: 672, height: 224, columns: 224..<448, rows: 0..<224),
                       fullWide.cropped(x1: 224, y1: 0, x2: 448, y2: 224))
    }

    // MARK: - Letterbox

    /// A frame of seeded noise, so every rounding shows.
    static func noise(width: Int, height: Int, seed: UInt64) -> RGBImage {
        var state = seed
        var px = [UInt8](repeating: 0, count: width * height * 3)
        for i in 0..<px.count {
            state = state &* 6364136223846793005 &+ 1442695040888963407
            px[i] = UInt8(truncatingIfNeeded: state >> 56)
        }
        return RGBImage(width: width, height: height, pixels: px)
    }

    func testTheLetterboxIsExactAtOneToOne() {
        let frame = Self.noise(width: 640, height: 480, seed: 1)
        let (canvas, _) = Letterbox.make(frame)
        XCTAssertEqual(canvas.cropped(x1: 0, y1: 0, x2: 640, y2: 480), frame)
    }

    /// 1280 x 720 (and 3840 x 2160) to 640 x 360: OpenCV's resize halves both sides with the
    /// rounded mean of each 2 x 2 block.
    func testTheLetterboxIsExactWhenTheFrameHalves() {
        let frame = Self.noise(width: 1280, height: 720, seed: 2)
        let (canvas, g) = Letterbox.make(frame)
        XCTAssertEqual(g.resizedHeight, 360)
        var mismatches = 0
        for y in 0..<360 {
            for x in 0..<640 {
                for c in 0..<3 {
                    let sum = Int(frame[x: 2 * x, y: 2 * y, c: c]) + Int(frame[x: 2 * x + 1, y: 2 * y, c: c])
                        + Int(frame[x: 2 * x, y: 2 * y + 1, c: c]) + Int(frame[x: 2 * x + 1, y: 2 * y + 1, c: c])
                    if Int(canvas[x: x, y: y, c: c]) != (sum + 2) >> 2 { mismatches += 1 }
                }
            }
        }
        XCTAssertEqual(mismatches, 0)
    }

    /// 1920 x 1080 to 640 x 360 (and the portrait 1080 x 1920): each output is source pixel
    /// `3 * x + 1`, `3 * y + 1`, with OpenCV's weights 1 and 0.
    func testTheLetterboxIsExactAtAThird() {
        for (w, h) in [(1920, 1080), (1080, 1920)] {
            let frame = Self.noise(width: w, height: h, seed: 3)
            let (canvas, g) = Letterbox.make(frame)
            var mismatches = 0
            for y in 0..<g.resizedHeight {
                for x in 0..<g.resizedWidth {
                    for c in 0..<3 where canvas[x: x, y: y, c: c] != frame[x: 3 * x + 1, y: 3 * y + 1, c: c] {
                        mismatches += 1
                    }
                }
            }
            XCTAssertEqual(mismatches, 0, "\(w) x \(h)")
        }
    }
}
