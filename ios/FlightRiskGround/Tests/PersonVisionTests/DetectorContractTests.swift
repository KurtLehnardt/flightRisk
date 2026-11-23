import XCTest
@testable import PersonVision

/// The detector's letterbox, decoding, NMS, box filter and crop rule on synthetic numbers.
final class DetectorContractTests: XCTestCase {
    // MARK: - Letterbox

    func testGeometryTruncatesAndPlacesAtTheTopLeft() {
        let g = LetterboxGeometry(frameWidth: 1920, frameHeight: 1080)
        XCTAssertEqual(g.ratio, 1.0 / 3.0)
        XCTAssertEqual(g.resizedWidth, 640)
        XCTAssertEqual(g.resizedHeight, 360)
        XCTAssertEqual(g.padLeft, 0)
        XCTAssertEqual(g.padTop, 0)
        // 1000 x 333: r = 0.64, 333 * 0.64 = 213.12 -> 213.
        let odd = LetterboxGeometry(frameWidth: 1000, frameHeight: 333)
        XCTAssertEqual(odd.resizedHeight, 213)
        XCTAssertEqual(odd.frameX(64), 100, accuracy: 1e-4)
    }

    func testCentredGeometryRoundsAndSplitsThePadding() {
        let g = LetterboxGeometry(frameWidth: 1920, frameHeight: 1080, placement: .centred)
        XCTAssertEqual(g.resizedWidth, 640)
        XCTAssertEqual(g.resizedHeight, 360)
        XCTAssertEqual(g.padLeft, 0)
        XCTAssertEqual(g.padTop, 140)
        XCTAssertEqual(g.frameY(140), 0, accuracy: 1e-4)
        XCTAssertEqual(g.frameY(500), 1080, accuracy: 1e-3)
    }

    func testTheCanvasIsPaddedWith114AndKeepsAnUnscaledFrame() {
        var pixels = [UInt8](repeating: 0, count: 640 * 320 * 3)
        for i in 0..<pixels.count { pixels[i] = UInt8(i % 251) }
        let frame = RGBImage(width: 640, height: 320, pixels: pixels)
        let (canvas, g) = Letterbox.make(frame)
        XCTAssertEqual(g.ratio, 1)
        XCTAssertEqual(canvas.width, 640)
        XCTAssertEqual(canvas.height, 640)
        // The top half is the frame, unchanged; the bottom half is padding.
        XCTAssertEqual(Array(canvas.pixels[0..<(640 * 320 * 3)]), pixels)
        XCTAssertTrue(canvas.pixels[(640 * 320 * 3)...].allSatisfy { $0 == 114 })
    }

    func testBilinearUpscaleOfAConstantImageIsConstant() {
        let (canvas, g) = Letterbox.make(RGBImage(width: 20, height: 10, red: 10, green: 200, blue: 77))
        XCTAssertEqual(g.resizedHeight, 320)
        XCTAssertEqual(canvas[x: 639, y: 319, c: 0], 10)
        XCTAssertEqual(canvas[x: 0, y: 0, c: 1], 200)
        XCTAssertEqual(canvas[x: 300, y: 100, c: 2], 77)
        XCTAssertEqual(canvas[x: 300, y: 320, c: 0], 114)
    }

    func testBilinearSamplesFollowOpenCVsPixelCentres() {
        // Upscale 2x: destination 0 maps to -0.25 (clamped to 0), 1 to 0.25, 2 to 0.75.
        let s = Letterbox.samples(source: 4, destination: 8)
        XCTAssertEqual(s[0].index, 0)
        XCTAssertEqual(s[0].weight, 0)
        XCTAssertEqual(s[1].index, 0)
        XCTAssertEqual(s[1].weight, 0.25)
        XCTAssertEqual(s[2].index, 0)
        XCTAssertEqual(s[2].weight, 0.75)
        XCTAssertEqual(s[7].index, 3)
        XCTAssertEqual(s[7].weight, 0)
    }

    // MARK: - Output rows

    func testAnchorsRunOverTheThreeStrides() {
        XCTAssertEqual(YOLOXContract.rowCount, 8400)
        XCTAssertTrue(YOLOXContract.anchor(row: 0)! == (0, 0, 8))
        XCTAssertTrue(YOLOXContract.anchor(row: 81)! == (1, 1, 8))
        XCTAssertTrue(YOLOXContract.anchor(row: 6399)! == (79, 79, 8))
        XCTAssertTrue(YOLOXContract.anchor(row: 6400)! == (0, 0, 16))
        XCTAssertTrue(YOLOXContract.anchor(row: 8000)! == (0, 0, 32))
        XCTAssertTrue(YOLOXContract.anchor(row: 8399)! == (19, 19, 32))
        XCTAssertNil(YOLOXContract.anchor(row: 8400))
    }

    /// An all-zero output with the given rows set: `(row, cx, cy, w, h, objectness, [class: score])`.
    func output(_ rows: [(Int, Float, Float, Float, Float, Float, [Int: Float])]) -> [Float] {
        var out = [Float](repeating: 0, count: YOLOXContract.rowCount * YOLOXContract.rowLength)
        for (row, cx, cy, w, h, obj, scores) in rows {
            let b = row * YOLOXContract.rowLength
            out[b] = cx; out[b + 1] = cy; out[b + 2] = w; out[b + 3] = h; out[b + 4] = obj
            for (c, s) in scores { out[b + 5 + c] = s }
        }
        return out
    }

    func testScoreIsObjectnessTimesTheBestClassAndBoxesAreDividedByTheRatio() {
        let out = output([(100, 100, 200, 20, 60, 0.9, [0: 0.8, 3: 0.1])])
        let g = LetterboxGeometry(frameWidth: 1280, frameHeight: 720) // r = 0.5
        let d = YOLOXDecoder.decode(out, geometry: g, scoreThreshold: 0.5)
        XCTAssertEqual(d.count, 1)
        XCTAssertEqual(d[0].score, Float(0.9) * Float(0.8))
        XCTAssertEqual(d[0].x1, 180)
        XCTAssertEqual(d[0].y1, 340)
        XCTAssertEqual(d[0].x2, 220)
        XCTAssertEqual(d[0].y2, 460)
    }

    func testARowWhoseBestClassIsNotPersonIsNotAPerson() {
        // Person scores 0.6 but "bicycle" scores 0.7: upstream assigns the row to class 1.
        let out = output([(5, 50, 50, 10, 30, 1.0, [0: 0.6, 1: 0.7])])
        let g = LetterboxGeometry(frameWidth: 640, frameHeight: 640)
        XCTAssertTrue(YOLOXDecoder.decode(out, geometry: g, scoreThreshold: 0.25).isEmpty)
        XCTAssertEqual(YOLOXDecoder.decode(out, geometry: g, scoreThreshold: 0.25, classes: nil).first?.classIndex, 1)
    }

    func testATieGoesToTheFirstClass() {
        let out = output([(5, 50, 50, 10, 30, 1.0, [0: 0.5, 2: 0.5])])
        let g = LetterboxGeometry(frameWidth: 640, frameHeight: 640)
        XCTAssertEqual(YOLOXDecoder.decode(out, geometry: g, scoreThreshold: 0.25).first?.classIndex, 0)
    }

    func testRowsUnderTheThresholdAreDropped() {
        let out = output([(5, 50, 50, 10, 30, 0.5, [0: 0.79])]) // 0.395
        let g = LetterboxGeometry(frameWidth: 640, frameHeight: 640)
        XCTAssertTrue(YOLOXDecoder.decode(out, geometry: g, scoreThreshold: 0.4).isEmpty)
        XCTAssertEqual(YOLOXDecoder.decode(out, geometry: g, scoreThreshold: 0.39).count, 1)
    }

    func testNMSKeepsTheBestOfOverlappingBoxesPerClass() {
        let out = output([
            (10, 100, 100, 40, 80, 1, [0: 0.9]),
            (11, 102, 101, 40, 80, 1, [0: 0.8]),   // IoU about 0.9 with the first: suppressed
            (12, 300, 300, 40, 80, 1, [0: 0.7]),   // far away: kept
            (13, 101, 100, 40, 80, 1, [2: 0.85]),  // another class: kept by per-class NMS
        ])
        let g = LetterboxGeometry(frameWidth: 640, frameHeight: 640)
        let all = YOLOXDecoder.decode(out, geometry: g, scoreThreshold: 0.25, classes: nil)
        XCTAssertEqual(all.map(\.score), [0.9, 0.85, 0.7])
        XCTAssertEqual(all.map(\.classIndex), [0, 2, 0])
        XCTAssertEqual(YOLOXDecoder.decode(out, geometry: g, scoreThreshold: 0.25).map(\.score), [0.9, 0.7])
    }

    func testNMSSuppressesOnlyAboveTheIoU() {
        // Two 10 x 10 boxes overlapping by half their width: IoU = 50 / 150 = 1/3.
        let a = DetectedBox(x1: 0, y1: 0, x2: 10, y2: 10, score: 0.9, classIndex: 0)
        let b = DetectedBox(x1: 5, y1: 0, x2: 15, y2: 10, score: 0.8, classIndex: 0)
        XCTAssertEqual(a.iou(b), 1.0 / 3.0, accuracy: 1e-12)
        XCTAssertEqual(NonMaxSuppression.singleClass([a, b], iou: 1.0 / 3.0).count, 2)
        XCTAssertEqual(NonMaxSuppression.singleClass([b, a], iou: 0.3), [a])
    }

    // MARK: - Box filter

    func testTheAerialFilterKeepsSmallPeopleTheOldFilterDropped() {
        // A person 24 pixels tall and 10 wide in a 1920 x 1080 frame (2.2% of the height).
        XCTAssertTrue(PersonBoxFilter.aerial.accepts(width: 10, height: 24, frameWidth: 1920, frameHeight: 1080))
        XCTAssertFalse(PersonBoxFilter.groundLevel.accepts(width: 10, height: 24, frameWidth: 1920, frameHeight: 1080))
        // Under 1% of the height is still dropped.
        XCTAssertFalse(PersonBoxFilter.aerial.accepts(width: 5, height: 10, frameWidth: 1920, frameHeight: 1080))
    }

    func testTheAerialFilterKeepsAPersonLyingDown() {
        XCTAssertTrue(PersonBoxFilter.aerial.accepts(width: 90, height: 30, frameWidth: 1920, frameHeight: 1080))
        XCTAssertFalse(PersonBoxFilter.groundLevel.accepts(width: 90, height: 30, frameWidth: 1920, frameHeight: 1080))
        XCTAssertFalse(PersonBoxFilter.aerial.accepts(width: 100, height: 30, frameWidth: 1920, frameHeight: 1080))
        XCTAssertFalse(PersonBoxFilter.aerial.accepts(width: 4, height: 30, frameWidth: 1920, frameHeight: 1080))
    }

    func testTheFilterIsConfigurable() {
        var f = PersonBoxFilter.aerial
        f.minHeightRatio = 0.05
        XCTAssertFalse(f.accepts(width: 10, height: 24, frameWidth: 1920, frameHeight: 1080))
        XCTAssertFalse(PersonBoxFilter.aerial.accepts(width: 0, height: 24, frameWidth: 1920, frameHeight: 1080))
    }

    // MARK: - Crop rule

    func testTheCropRuleClipsThenTruncates() {
        let p = CropRule.pixels(x1: -3.7, y1: 10.9, x2: 50.99, y2: 500, frameWidth: 640, frameHeight: 480)
        XCTAssertEqual(p?.x1, 0)
        XCTAssertEqual(p?.y1, 10)
        XCTAssertEqual(p?.x2, 50)
        XCTAssertEqual(p?.y2, 480)
        XCTAssertNil(CropRule.pixels(x1: 10.2, y1: 10, x2: 10.9, y2: 20, frameWidth: 640, frameHeight: 480))
        XCTAssertNil(CropRule.pixels(x1: 700, y1: 10, x2: 800, y2: 20, frameWidth: 640, frameHeight: 480))
    }

    func testCroppingCopiesTheRightPixels() {
        var px = [UInt8](repeating: 0, count: 4 * 3 * 3)
        for i in 0..<px.count { px[i] = UInt8(i) }
        let image = RGBImage(width: 4, height: 3, pixels: px)
        let c = image.cropped(x1: 1, y1: 1, x2: 3, y2: 3)
        XCTAssertEqual(c?.width, 2)
        XCTAssertEqual(c?.pixels, [15, 16, 17, 18, 19, 20, 27, 28, 29, 30, 31, 32])
        XCTAssertNil(image.cropped(x1: 3, y1: 0, x2: 3, y2: 1))
    }

    // MARK: - Embedder preprocessing

    func testResizedSizeAndCentreCropOffsets() {
        XCTAssertTrue(CLIPPreprocess.resizedSize(width: 19, height: 49) == (224, 577))
        XCTAssertTrue(CLIPPreprocess.resizedSize(width: 300, height: 200) == (336, 224))
        // Surpluses of 1 and 3: halves go to the even neighbour (0 and 2).
        XCTAssertEqual(CLIPPreprocess.cropOffsets(width: 225, height: 224).left, 0)
        XCTAssertEqual(CLIPPreprocess.cropOffsets(width: 227, height: 224).left, 2)
        XCTAssertEqual(CLIPPreprocess.cropOffsets(width: 224, height: 577).top, 176)
    }

    func testBicubicKeepsAConstantImageConstant() {
        let up = PillowBicubic.resize(RGBImage(width: 7, height: 13, red: 40, green: 41, blue: 250), width: 224, height: 416)
        XCTAssertTrue(stride(from: 0, to: up.pixels.count, by: 3).allSatisfy {
            up.pixels[$0] == 40 && up.pixels[$0 + 1] == 41 && up.pixels[$0 + 2] == 250
        })
        let down = PillowBicubic.resize(RGBImage(width: 900, height: 500, red: 0, green: 255, blue: 9), width: 403, height: 224)
        XCTAssertEqual(down[x: 200, y: 100, c: 1], 255)
        XCTAssertEqual(down[x: 0, y: 0, c: 2], 9)
    }

    func testTheTensorIsChannelMajorAndNormalised() {
        let t = CLIPPreprocess.input(RGBImage(width: 224, height: 224, red: 255, green: 0, blue: 128))
        XCTAssertEqual(t.count, 3 * 224 * 224)
        XCTAssertEqual(t[0], (1 - 0.48145466) / 0.26862954, accuracy: 1e-6)
        XCTAssertEqual(t[224 * 224], (0 - 0.4578275) / 0.26130258, accuracy: 1e-6)
        XCTAssertEqual(t[2 * 224 * 224 + 5], (Float(128) / 255 - 0.40821073) / 0.27577711, accuracy: 1e-6)
    }

    func testEmbeddingHelpers() {
        XCTAssertEqual(Embedding.l2Normalized([3, 4]), [0.6, 0.8])
        XCTAssertEqual(Embedding.l2Normalized([0, 0]), [0, 0])
        XCTAssertEqual(Embedding.cosine([1, 0], [2, 0]), 1)
        XCTAssertEqual(Embedding.dot([1, 0], [1]), 0)
    }
}
