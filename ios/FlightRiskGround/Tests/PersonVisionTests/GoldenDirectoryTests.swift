import CoreGraphics
import Foundation
import ImageIO
import XCTest
@testable import PersonVision

/// Checks against the full golden fixtures `make_models.py verify` writes (gitignored, never
/// committed). They are found at `$FLIGHTRISK_GOLDEN_DIR`, or else at `build/mobile_models/golden`
/// in this checkout; the tests are skipped when neither exists.
final class GoldenDirectoryTests: XCTestCase {
    static var goldenDirectory: URL? {
        if let env = ProcessInfo.processInfo.environment["FLIGHTRISK_GOLDEN_DIR"], !env.isEmpty {
            let url = URL(fileURLWithPath: env, isDirectory: true)
            return FileManager.default.fileExists(atPath: url.appendingPathComponent("golden.json").path) ? url : nil
        }
        // Tests/PersonVisionTests/<file> -> ios/FlightRiskGround -> ios -> the repository root.
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let url = root.appendingPathComponent("build/mobile_models/golden", isDirectory: true)
        return FileManager.default.fileExists(atPath: url.appendingPathComponent("golden.json").path) ? url : nil
    }

    func golden() throws -> URL {
        guard let dir = Self.goldenDirectory else {
            throw XCTSkip("no golden fixtures: run make_models.py verify, or set FLIGHTRISK_GOLDEN_DIR")
        }
        return dir
    }

    static func floats(_ url: URL) throws -> [Float] {
        let data = try Data(contentsOf: url)
        return data.withUnsafeBytes { raw in
            (0..<(data.count / 4)).map { Float(bitPattern: UInt32(littleEndian: raw.loadUnaligned(fromByteOffset: $0 * 4, as: UInt32.self))) }
        }
    }

    /// A PNG decoded with ImageIO into 8-bit RGB, without colour conversion.
    static func png(_ url: URL) throws -> RGBImage {
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        let w = image.width, h = image.height
        var rgba = [UInt8](repeating: 0, count: w * h * 4)
        let space = image.colorSpace ?? CGColorSpaceCreateDeviceRGB()
        try rgba.withUnsafeMutableBytes { buf in
            let ctx = try XCTUnwrap(CGContext(data: buf.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                              space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
            ctx.interpolationQuality = .none
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        }
        var rgb = [UInt8](repeating: 0, count: w * h * 3)
        for i in 0..<(w * h) {
            rgb[i * 3] = rgba[i * 4]
            rgb[i * 3 + 1] = rgba[i * 4 + 1]
            rgb[i * 3 + 2] = rgba[i * 4 + 2]
        }
        return RGBImage(width: w, height: h, pixels: rgb)
    }

    func testTheLetterboxIsWithinOneLevelOfUpstreams() throws {
        let dir = try golden()
        let frame = try Self.png(dir.appendingPathComponent("frame.png"))
        let expected = try Self.floats(dir.appendingPathComponent("detector_input.f32"))
        XCTAssertEqual(expected.count, 3 * 640 * 640)
        let (canvas, _) = Letterbox.make(frame)
        var worst: Float = 0
        var differing = 0
        let plane = 640 * 640
        for i in 0..<plane {
            // The tensor is BGR, channel-major; the canvas is RGB, interleaved.
            for (c, rgb) in [(0, 2), (1, 1), (2, 0)] {
                let d = abs(expected[c * plane + i] - Float(canvas.pixels[i * 3 + rgb]))
                worst = max(worst, d)
                if d > 0 { differing += 1 }
            }
        }
        // OpenCV rounds in fixed point (and its arm64 build takes another path again), so about a
        // tenth of the canvas values differ by one level; none may differ by more.
        XCTAssertLessThanOrEqual(worst, 1)
        XCTAssertLessThan(Double(differing) / Double(3 * plane), 0.15)
    }

    func testTheFullDetectorOutputDecodesToTheGoldenDetections() throws {
        let dir = try golden()
        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: dir.appendingPathComponent("golden.json"))) as? [String: Any]
        let dets = try XCTUnwrap(json?["detections"] as? [[String: Any]])
        let output = try Self.floats(dir.appendingPathComponent("detector_output.f32"))
        let got = YOLOXDecoder.decode(output, geometry: LetterboxGeometry(frameWidth: 800, frameHeight: 480),
                                      scoreThreshold: 0.25, classes: nil)
        XCTAssertEqual(got.count, dets.count)
        for (a, b) in zip(got, dets) {
            XCTAssertEqual(a.classIndex, b["class"] as? Int)
            XCTAssertEqual(a.score, Float(try XCTUnwrap(b["score"] as? Double)))
        }
    }

    func testTheCropAndEmbedderInputFromTheFrame() throws {
        let dir = try golden()
        let frame = try Self.png(dir.appendingPathComponent("frame.png"))
        let crop = try Self.png(dir.appendingPathComponent("crop.png"))
        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: dir.appendingPathComponent("golden.json"))) as? [String: Any]
        let px = try XCTUnwrap((json?["crop_box"] as? [String: Any])?["pixels"] as? [Int])
        XCTAssertEqual(frame.cropped(x1: px[0], y1: px[1], x2: px[2], y2: px[3]), crop)
        let expected = try Self.floats(dir.appendingPathComponent("embedder_input.f32"))
        XCTAssertEqual(CLIPPreprocess.input(crop), expected)
    }
}
