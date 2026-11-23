import CoreGraphics
import CoreML
import ImageIO
import PersonVision
import XCTest
@testable import FlightRisk

/// The on-device models in the app: the Core ML glue, the missing-model behaviour, and, when a
/// build includes the models (`make_models.py install`), their parity with PyTorch on the golden
/// fixtures.
///
/// The golden fixtures are gitignored. They are found at `$FLIGHTRISK_GOLDEN_DIR` (pass it to
/// `xcodebuild` as `TEST_RUNNER_FLIGHTRISK_GOLDEN_DIR`), or else at `build/mobile_models/golden`
/// in this checkout. Model tests are skipped when the build has no models or there are no
/// fixtures.
final class OnDeviceModelTests: XCTestCase {

    // MARK: - Fixtures

    static var goldenDirectory: URL? {
        if let env = ProcessInfo.processInfo.environment["FLIGHTRISK_GOLDEN_DIR"], !env.isEmpty {
            let url = URL(fileURLWithPath: env, isDirectory: true)
            return FileManager.default.fileExists(atPath: url.appendingPathComponent("golden.json").path) ? url : nil
        }
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let url = root.appendingPathComponent("build/mobile_models/golden", isDirectory: true)
        return FileManager.default.fileExists(atPath: url.appendingPathComponent("golden.json").path) ? url : nil
    }

    func golden() throws -> URL {
        guard let dir = Self.goldenDirectory else { throw XCTSkip("no golden fixtures (run make_models.py verify)") }
        return dir
    }

    func requireModels() throws {
        guard BundledModels.url(YOLOXContract.modelName) != nil, BundledModels.url(CLIPContract.modelName) != nil else {
            throw XCTSkip("this build has no models (install them with make_models.py install)")
        }
    }

    static func floats(_ url: URL) throws -> [Float] {
        let data = try Data(contentsOf: url)
        return data.withUnsafeBytes { raw in
            (0..<(data.count / 4)).map { Float(bitPattern: UInt32(littleEndian: raw.loadUnaligned(fromByteOffset: $0 * 4, as: UInt32.self))) }
        }
    }

    static func png(_ url: URL) throws -> RGBImage {
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        return try XCTUnwrap(ModelIO.rgb(image))
    }

    static func goldenJSON(_ dir: URL) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: dir.appendingPathComponent("golden.json"))) as? [String: Any])
    }

    static func cgImage(_ image: RGBImage) throws -> CGImage {
        var rgba = [UInt8](repeating: 255, count: image.width * image.height * 4)
        for i in 0..<(image.width * image.height) {
            rgba[i * 4] = image.pixels[i * 3]
            rgba[i * 4 + 1] = image.pixels[i * 3 + 1]
            rgba[i * 4 + 2] = image.pixels[i * 3 + 2]
        }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(rgba) as CFData))
        return try XCTUnwrap(CGImage(
            width: image.width, height: image.height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: image.width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent
        ))
    }

    // MARK: - Core ML glue (no model needed)

    func testRGBExtractionIsExact() throws {
        var px = [UInt8](repeating: 0, count: 37 * 23 * 3)
        for i in 0..<px.count { px[i] = UInt8((i * 31) % 256) }
        let image = RGBImage(width: 37, height: 23, pixels: px)
        XCTAssertEqual(ModelIO.rgb(try Self.cgImage(image)), image)
    }

    func testThePixelBufferIsBGRA() throws {
        var reuse: CVPixelBuffer?
        let pb = try ModelIO.pixelBuffer(RGBImage(width: 3, height: 2, red: 10, green: 20, blue: 30), reuse: &reuse)
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
        let base = try XCTUnwrap(CVPixelBufferGetBaseAddress(pb)).assumingMemoryBound(to: UInt8.self)
        let row = CVPixelBufferGetBytesPerRow(pb)
        XCTAssertEqual(CVPixelBufferGetPixelFormatType(pb), kCVPixelFormatType_32BGRA)
        XCTAssertEqual([base[row + 4], base[row + 5], base[row + 6], base[row + 7]], [30, 20, 10, 255])
        let again = try ModelIO.pixelBuffer(RGBImage(width: 3, height: 2, red: 1, green: 2, blue: 3), reuse: &reuse)
        XCTAssertTrue(again === pb, "a buffer of the same size is reused")
    }

    func testFloatsFollowTheShapeWhateverTheStrides() throws {
        // A [2, 3] array stored transposed (strides [1, 2]).
        let storage = UnsafeMutablePointer<Float>.allocate(capacity: 6)
        defer { storage.deallocate() }
        for (i, v) in [Float(0), 3, 1, 4, 2, 5].enumerated() { storage[i] = v }
        let array = try MLMultiArray(dataPointer: storage, shape: [2, 3], dataType: .float32, strides: [1, 2])
        XCTAssertEqual(ModelIO.floats(array), [0, 1, 2, 3, 4, 5])
        let doubles = try MLMultiArray(shape: [2], dataType: .double)
        doubles[0] = 1.5
        doubles[1] = -2
        XCTAssertEqual(ModelIO.floats(doubles), [1.5, -2])
        let tensor = try ModelIO.multiArray([1, 2, 3, 4, 5, 6, 7, 8], channels: 2, height: 2, width: 2)
        XCTAssertEqual(tensor.shape, [1, 2, 2, 2])
        XCTAssertEqual(ModelIO.floats(tensor), [1, 2, 3, 4, 5, 6, 7, 8])
    }

    func testAMissingEmbedderIsReportedAndMatchesNothing() throws {
        let reid = PersonReID(modelName: "NoSuchEmbedderModel")
        XCTAssertFalse(reid.loadModel())
        XCTAssertFalse(reid.isAvailable)
        let crop = try Self.cgImage(RGBImage(width: 8, height: 16, red: 1, green: 2, blue: 3))
        XCTAssertFalse(reid.setTarget(photo: crop))
        XCTAssertEqual(reid.compare(crop: crop), 0)
    }

    func testTheCheckPictureIsBundled() throws {
        let picture = try XCTUnwrap(CustomDetectorStore.bundledCheckPicture())
        XCTAssertEqual(picture.width, 640)
        XCTAssertEqual(picture.height, 640)
    }

    func testNoFaceModelIsLoadedOrNamed() throws {
        // The old face, YOLO and OSNet model names must not be looked up any more.
        let sources = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("FlightRisk", isDirectory: true)
        let files = try XCTUnwrap(FileManager.default.enumerator(at: sources, includingPropertiesForKeys: nil))
        var checked = 0
        for case let url as URL in files where url.pathExtension == "swift" {
            let text = try String(contentsOf: url, encoding: .utf8)
            for name in ["SCRFD", "ArcFace", "InsightFace", "OSNet", "YOLOPersonDetector", "CLIPVisual",
                         "VNDetectFaceRectanglesRequest", "VNDetectFaceLandmarksRequest", "CIDetectorTypeFace"] {
                XCTAssertFalse(text.contains(name), "\(url.lastPathComponent) names \(name)")
            }
            checked += 1
        }
        XCTAssertGreaterThan(checked, 50)
    }

    // MARK: - Bundled models (skipped without them)

    func testTheBundledModelsCarryTheirNotices() throws {
        try requireModels()
        let notices = try XCTUnwrap(BundledModels.notices())
        XCTAssertTrue(notices.contains("Apache License"))
        XCTAssertTrue(notices.contains("MIT"))
        XCTAssertEqual(BundledModels.computeUnits(for: "YOLOXs.mlpackage"), .all)
    }

    func testTheDetectorMatchesPyTorchOnTheGoldenInput() throws {
        try requireModels()
        let dir = try golden()
        // The golden input is a whole-number BGR tensor, so a pixel buffer holds it exactly.
        let input = try Self.floats(dir.appendingPathComponent("detector_input.f32"))
        let plane = 640 * 640
        var canvas = [UInt8](repeating: 0, count: plane * 3)
        for i in 0..<plane {
            canvas[i * 3] = UInt8(input[2 * plane + i])
            canvas[i * 3 + 1] = UInt8(input[plane + i])
            canvas[i * 3 + 2] = UInt8(input[i])
        }
        let backend = try YOLOXBackend(url: XCTUnwrap(BundledModels.url(YOLOXContract.modelName)), computeUnits: .all)
        let got = try backend.rawOutput(canvas: RGBImage(width: 640, height: 640, pixels: canvas))
        let want = try Self.floats(dir.appendingPathComponent("detector_output.f32"))
        XCTAssertEqual(got.count, want.count)
        var scoreDiff: Float = 0
        var boxDiff: Float = 0
        let n = YOLOXContract.rowLength
        for row in 0..<YOLOXContract.rowCount {
            let b = row * n
            var best: Float = 0
            for c in 0..<YOLOXContract.classCount {
                scoreDiff = max(scoreDiff, abs(got[b + 4] * got[b + 5 + c] - want[b + 4] * want[b + 5 + c]))
                best = max(best, want[b + 4] * want[b + 5 + c])
            }
            if best >= 0.05 {
                for k in 0..<4 { boxDiff = max(boxDiff, abs(got[b + k] - want[b + k])) }
            }
        }
        // The fp32 detector's gate in make_models.py verify, on every compute unit.
        XCTAssertLessThanOrEqual(scoreDiff, 1e-3)
        XCTAssertLessThanOrEqual(boxDiff, 0.05)
    }

    func testTheDetectorFindsTheGoldenPeopleFromTheFrame() throws {
        try requireModels()
        let dir = try golden()
        let frame = try Self.png(dir.appendingPathComponent("frame.png"))
        let open = PersonBoxFilter(minHeightRatio: 0, minAreaRatio: 0, minAspectRatio: 0, maxAspectRatio: 100)
        let detector = PersonDetector(confidence: 0.25, boxFilter: open)
        XCTAssertTrue(detector.isAvailable)
        XCTAssertNil(detector.unavailableReason)
        let got = detector.persons(in: frame)
        let want = try XCTUnwrap(Self.goldenJSON(dir)["detections"] as? [[String: Any]]).filter { $0["class"] as? Int == 0 }
        XCTAssertEqual(got.count, want.count)
        for w in want {
            let box = try XCTUnwrap(w["box"] as? [Double]).map { Float($0) }
            let ref = DetectedBox(x1: box[0], y1: box[1], x2: box[2], y2: box[3], score: Float(try XCTUnwrap(w["score"] as? Double)), classIndex: 0)
            let match = try XCTUnwrap(got.max { $0.iou(ref) < $1.iou(ref) })
            // The canvas differs from OpenCV's by at most one level (the model itself matches
            // PyTorch to 1e-3 on the exact input, above). On the smallest drawn figures, about 30
            // pixels tall, that moves a score by up to about 0.03.
            XCTAssertGreaterThanOrEqual(match.iou(ref), 0.95)
            XCTAssertEqual(match.score, ref.score, accuracy: 0.05)
        }
    }

    func testTheEmbedderMatchesPyTorchOnTheGoldenCrop() throws {
        try requireModels()
        let dir = try golden()
        let reid = PersonReID()
        XCTAssertTrue(reid.loadModel())
        let want = try Self.floats(dir.appendingPathComponent("embedding.f32"))
        let fromInput = try reid.embedding(input: Self.floats(dir.appendingPathComponent("embedder_input.f32")))
        let fromCrop = try reid.embedding(of: Self.png(dir.appendingPathComponent("crop.png")))
        // The palettised embedder's gate in make_models.py verify is a cosine of 0.99.
        XCTAssertGreaterThanOrEqual(Embedding.cosine(fromInput, want), 0.99)
        XCTAssertGreaterThanOrEqual(Embedding.cosine(fromCrop, want), 0.99)
        XCTAssertEqual(Embedding.dot(fromInput, fromCrop), 1, accuracy: 1e-4, "the Swift preprocessing is the ground's")
        XCTAssertEqual(Embedding.dot(fromCrop, fromCrop), 1, accuracy: 1e-4)
    }

    func testMatchingAFrameAgainstItsOwnCrop() throws {
        try requireModels()
        let dir = try golden()
        let reid = PersonReID()
        XCTAssertTrue(reid.loadModel())
        let frame = try Self.cgImage(Self.png(dir.appendingPathComponent("frame.png")))
        let detector = PersonDetector(confidence: 0.25)
        let detections = detector.detect(frame: frame)
        XCTAssertFalse(detections.isEmpty, "the aerial filter keeps the drawn people")
        XCTAssertTrue(reid.setTarget(photo: detections[0].crop))
        let match = reid.findMatch(detections: detections)
        XCTAssertEqual(match.index, 0)
        XCTAssertEqual(match.score, 1, accuracy: 1e-3)
    }

    func testYOLOXFindsAPersonInTheCheckPicture() throws {
        try requireModels()
        let backend = try YOLOXBackend(url: XCTUnwrap(BundledModels.url(YOLOXContract.modelName)), computeUnits: .all)
        let picture = try XCTUnwrap(CustomDetectorStore.bundledCheckPicture())
        let persons = try backend.persons(in: picture, scoreThreshold: CustomModelValidator.personCheckThreshold)
        XCTAssertNoThrow(try CustomModelValidator.checkPersonFound(persons))
    }
}
