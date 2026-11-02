import CoreGraphics
import Foundation
import GroundProtocol
import UIKit
import XCTest
@testable import FlightRisk

/// Tests of the pure helpers behind the Ground tab's video tiles, using the contract fixtures.
final class VideoTileDecodeTests: XCTestCase {
    private func frame(_ name: String, file: StaticString = #filePath, line: UInt = #line) throws -> FramePayload {
        let json = try ContractFixtures.payloadJSON("frame.json", name, testFile: file, line: line)
        return try JSONDecoder().decode(FramePayload.self, from: Data(json.utf8))
    }

    private let t0 = Date(timeIntervalSince1970: 1_000_000)

    private func tiles(_ frames: [String: FramePayload], at: [String: Date], now: Date,
                       stream: Bool? = true, stale: Bool = false) -> [VideoTileModel] {
        VideoTileLogic.makeTiles(frames: frames, arrivedAt: at, now: now, streamVideoEnabled: stream, stateStale: stale)
    }

    // MARK: Normal and detections-only frames

    func testNormalFrameDecodesImageAndBoxes() throws {
        let f = try frame("edge_video_with_match")
        let tile = try XCTUnwrap(tiles(["drone-1": f], at: ["drone-1": t0], now: t0 + 1).first)
        XCTAssertEqual(tile.mode, .live)
        XCTAssertEqual(tile.boxes.count, 2)
        XCTAssertEqual(tile.boxes.filter(\.matched).count, 1)
        XCTAssertNil(tile.boxes[0].trackId)
        XCTAssertEqual(tile.aspect, 960.0 / 720.0, accuracy: 1e-9)
        XCTAssertFalse(tile.videoOffAtGround)
        XCTAssertEqual(tile.accessibilityText, "drone-1 video, 2 detections, 1 match")
        let image = try XCTUnwrap(VideoTileLogic.decodeImage(base64: try XCTUnwrap(tile.imageBase64), maxPixelSize: 400))
        XCTAssertEqual(image.width, 8)
        XCTAssertEqual(image.height, 8)
    }

    func testDetectionsOnlyFrameDrawsBoxesOverPlaceholder() throws {
        let f = try frame("edge_detections_only")
        let tile = try XCTUnwrap(tiles(["drone-2": f], at: ["drone-2": t0], now: t0, stream: false).first)
        XCTAssertNil(tile.imageBase64)
        XCTAssertEqual(tile.boxes.count, 1)
        XCTAssertTrue(tile.videoOffAtGround)
        XCTAssertEqual(tile.mode, .live)
        XCTAssertEqual(tile.accessibilityText, "drone-2 video, 1 detection")
        XCTAssertEqual(VideoTileLogic.videoOffText, "Video is off at the ground. Turn it on from the dashboard.")
    }

    func testTileIsOnePerDroneInStableNaturalOrder() throws {
        let f = try frame("edge_video_with_match")
        let ids = ["drone-10", "drone-2", "drone-1"]
        let frames = Dictionary(uniqueKeysWithValues: ids.map { ($0, f) })
        let at = Dictionary(uniqueKeysWithValues: ids.map { ($0, t0) })
        XCTAssertEqual(tiles(frames, at: at, now: t0).map(\.id), ["drone-1", "drone-2", "drone-10"])
    }

    func testTrackIdShownOnlyWhenPresent() {
        XCTAssertNil(VideoTileLogic.box(bboxNorm: [0, 0, 1, 1], trackId: nil)?.trackId)
        XCTAssertNil(VideoTileLogic.box(bboxNorm: [0, 0, 1, 1], trackId: "  ")?.trackId)
        XCTAssertEqual(VideoTileLogic.box(bboxNorm: [0, 0, 1, 1], trackId: "t-7")?.trackId, "t-7")
    }

    // MARK: Corrupt and oversized images

    func testCorruptBase64AndJpegDoNotCrash() {
        XCTAssertNil(VideoTileLogic.decodeImage(base64: "", maxPixelSize: 200))
        XCTAssertNil(VideoTileLogic.decodeImage(base64: "%%% not base64 %%%", maxPixelSize: 200))
        XCTAssertNil(VideoTileLogic.decodeImage(base64: Data("not a jpeg at all".utf8).base64EncodedString(), maxPixelSize: 200))
        XCTAssertNil(VideoTileLogic.decodeImage(base64: Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00]).base64EncodedString(), maxPixelSize: 200))
        let tooLong = String(repeating: "A", count: VideoTileLogic.maxBase64Characters + 4)
        XCTAssertNil(VideoTileLogic.decodeImage(base64: tooLong, maxPixelSize: 200))
    }

    func testTruncatedFixtureJpegDoesNotCrash() throws {
        let f = try frame("edge_video_with_match")
        let text = try XCTUnwrap(f.image)
        let half = String(text.prefix(text.count / 2))
        _ = VideoTileLogic.decodeImage(base64: half, maxPixelSize: 200)
    }

    func testOversizedImageIsDownsampled() throws {
        let size = CGSize(width: 4000, height: 3000)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let data = UIGraphicsImageRenderer(size: size, format: format).jpegData(withCompressionQuality: 0.5) { ctx in
            UIColor.red.setFill()
            ctx.fill(CGRect(origin: .zero, size: size))
        }
        let image = try XCTUnwrap(VideoTileLogic.decodeImage(base64: data.base64EncodedString(), maxPixelSize: 200))
        XCTAssertLessThanOrEqual(max(image.width, image.height), 200)
        XCTAssertLessThanOrEqual(image.width * image.height, 200 * 200)
        XCTAssertGreaterThan(image.width, image.height)
        // A request above the hard cap is still bounded.
        let big = try XCTUnwrap(VideoTileLogic.decodeImage(base64: data.base64EncodedString(), maxPixelSize: 100_000))
        XCTAssertLessThanOrEqual(max(big.width, big.height), VideoTileLogic.maxDecodePixels)
    }

    func testTargetPixelSizeIsBounded() {
        XCTAssertEqual(VideoTileLogic.targetPixelSize(pointWidth: 0, scale: 3), 600)
        XCTAssertEqual(VideoTileLogic.targetPixelSize(pointWidth: .nan, scale: 3), 600)
        XCTAssertEqual(VideoTileLogic.targetPixelSize(pointWidth: 10_000, scale: 3), VideoTileLogic.maxDecodePixels)
        XCTAssertEqual(VideoTileLogic.targetPixelSize(pointWidth: 361, scale: 3), 1100)
    }

    // MARK: Boxes

    func testBoxesOutsideUnitRangeAreClamped() throws {
        let box = try XCTUnwrap(VideoTileLogic.box(bboxNorm: [-0.5, -1, 1.5, 0.5]))
        XCTAssertEqual([box.x1, box.y1, box.x2, box.y2], [0, 0, 1, 0.5])
        XCTAssertNil(VideoTileLogic.box(bboxNorm: [1.2, 0.1, 1.8, 0.4]), "fully outside has no area")
        let swapped = try XCTUnwrap(VideoTileLogic.box(bboxNorm: [0.8, 0.9, 0.2, 0.1]))
        XCTAssertEqual([swapped.x1, swapped.y1, swapped.x2, swapped.y2], [0.2, 0.1, 0.8, 0.9])
    }

    func testNonFiniteAndMalformedBoxesAreDropped() {
        XCTAssertNil(VideoTileLogic.box(bboxNorm: [.nan, 0, 1, 1]))
        XCTAssertNil(VideoTileLogic.box(bboxNorm: [0, 0, .infinity, 1]))
        XCTAssertNil(VideoTileLogic.box(bboxNorm: [0, 0, 1]))
        XCTAssertNil(VideoTileLogic.box(bboxNorm: nil))
        XCTAssertNil(VideoTileLogic.box(bboxNorm: [0.5, 0.5, 0.5, 0.9]))
    }

    func testBoxRectScalesToTile() throws {
        let f = try frame("edge_video_with_match")
        let box = try XCTUnwrap(VideoTileLogic.boxes(from: f.boxes).first)
        let rect = box.rect(in: CGSize(width: 960, height: 720))
        XCTAssertEqual(rect.minX, 400, accuracy: 1e-6)
        XCTAssertEqual(rect.minY, 300, accuracy: 1e-6)
        XCTAssertEqual(rect.width, 60, accuracy: 1e-6)
        XCTAssertEqual(rect.height, 120, accuracy: 1e-6)
    }

    // MARK: Staleness

    func testStaleDecisionWithInjectedClock() {
        XCTAssertFalse(VideoTileLogic.isStale(arrivedAt: t0, now: t0))
        XCTAssertFalse(VideoTileLogic.isStale(arrivedAt: t0, now: t0 + 3))
        XCTAssertTrue(VideoTileLogic.isStale(arrivedAt: t0, now: t0 + 3.01))
        XCTAssertFalse(VideoTileLogic.isStale(arrivedAt: t0 + 2, now: t0), "small jitter is fresh")
        XCTAssertTrue(VideoTileLogic.isStale(arrivedAt: t0 + 3600, now: t0), "clock moved back an hour")
        XCTAssertTrue(VideoTileLogic.isStale(arrivedAt: nil, now: t0))
    }

    func testStaleTileShowsNoVideoAndClearsBoxes() throws {
        let f = try frame("edge_video_with_match")
        let tile = try XCTUnwrap(tiles(["drone-1": f], at: ["drone-1": t0], now: t0 + 5, stream: false).first)
        XCTAssertEqual(tile.mode, .noVideo)
        XCTAssertTrue(tile.boxes.isEmpty)
        XCTAssertNil(tile.imageBase64)
        XCTAssertFalse(tile.videoOffAtGround, "a stale tile cannot say video is off")
        XCTAssertEqual(tile.noVideoReason, "no recent frame from drone-1")
        XCTAssertEqual(tile.accessibilityText, "drone-1 video, no video, no recent frame from drone-1")
    }

    func testStaleGroundStateLabelsLastFrame() throws {
        let f = try frame("edge_video_with_match")
        let tile = try XCTUnwrap(tiles(["drone-1": f], at: ["drone-1": t0], now: t0 + 1, stale: true).first)
        XCTAssertEqual(tile.mode, .lastFrame)
        XCTAssertEqual(tile.boxes.count, 2)
    }

    // MARK: Layout

    func testLayoutColumns() {
        XCTAssertEqual(VideoTileLogic.columns(count: 1, regularWidth: true, compactHeight: false), 1)
        XCTAssertEqual(VideoTileLogic.columns(count: 3, regularWidth: false, compactHeight: false), 1)
        XCTAssertEqual(VideoTileLogic.columns(count: 2, regularWidth: false, compactHeight: true), 2)
        XCTAssertEqual(VideoTileLogic.columns(count: 2, regularWidth: true, compactHeight: false), 2)
        XCTAssertEqual(VideoTileLogic.aspect(width: 0, height: 720), VideoTileLogic.defaultAspect)
        XCTAssertEqual(VideoTileLogic.aspect(width: .nan, height: 720), VideoTileLogic.defaultAspect)
    }

    // MARK: Review round

    private func jpeg(_ w: Int, _ h: Int) -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: w, height: h), format: format).jpegData(withCompressionQuality: 0.5) { ctx in
            UIColor.blue.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
        }
    }

    func testBoxLandsOnImagePixelsForSixteenByNineImageInFourByThreeTile() throws {
        // The edge sends a 320x180 thumbnail while frame_width/height say 960x720.
        let image = CGSize(width: 320, height: 180)
        let tile = CGSize(width: 480, height: 360)
        let area = VideoTileLogic.imageRect(imageSize: image, in: tile)
        XCTAssertEqual(area, CGRect(x: 0, y: 45, width: 480, height: 270))
        let box = try XCTUnwrap(VideoTileLogic.box(bboxNorm: [0.5, 0.5, 0.75, 1.0]))
        let rect = box.rect(in: area.size).offsetBy(dx: area.minX, dy: area.minY)
        XCTAssertEqual(rect, CGRect(x: 240, y: 180, width: 120, height: 135))
        // In image pixels the same box is x 160...240, y 90...180: the lower middle of the image.
        let k = image.width / area.width
        XCTAssertEqual((rect.minX - area.minX) * k, 160, accuracy: 1e-9)
        XCTAssertEqual((rect.maxY - area.minY) * k, 180, accuracy: 1e-9)
        // A taller image is pillarboxed, never cropped.
        let tall = VideoTileLogic.imageRect(imageSize: CGSize(width: 90, height: 160), in: tile)
        XCTAssertEqual(tall.height, 360, accuracy: 1e-9)
        XCTAssertGreaterThan(tall.minX, 0)
        XCTAssertEqual(VideoTileLogic.imageRect(imageSize: nil, in: tile), CGRect(origin: .zero, size: tile))
    }

    func testVideoOffTextRules() throws {
        let off = try frame("edge_detections_only")
        let video = try frame("edge_video_with_match")
        func tile(_ f: FramePayload, now: Date, stream: Bool?, stale: Bool) -> VideoTileModel {
            tiles(["d": f], at: ["d": t0], now: now, stream: stream, stale: stale)[0]
        }
        // Fresh detections-only frame: video is off.
        XCTAssertTrue(tile(off, now: t0, stream: true, stale: false).videoOffAtGround)
        // Fresh frame with an image while the remembered flag says off and the ground is live.
        XCTAssertTrue(tile(video, now: t0, stream: false, stale: false).videoOffAtGround)
        // Same flag but the ground state is stale: the remembered flag is not trusted.
        XCTAssertFalse(tile(video, now: t0, stream: false, stale: true).videoOffAtGround)
        XCTAssertFalse(tile(video, now: t0, stream: true, stale: false).videoOffAtGround)
        // Stale tiles never say video is off; the reason depends on the link.
        let linkDown = tile(off, now: t0 + 9, stream: false, stale: true)
        XCTAssertEqual(linkDown.mode, .noVideo)
        XCTAssertFalse(linkDown.videoOffAtGround)
        XCTAssertEqual(linkDown.noVideoReason, "no frames from the ground")
        let silentEdge = tile(off, now: t0 + 9, stream: false, stale: false)
        XCTAssertFalse(silentEdge.videoOffAtGround)
        XCTAssertEqual(silentEdge.noVideoReason, "no recent frame from d")
    }

    func testBoxCapAndTieBreakAndMatchCount() throws {
        let one = #"{"bbox_norm":[0.1,0.1,0.2,0.2],"matched":true}"#
        let json = #"{"drone_id":"d","stream_video":true,"boxes":["# + Array(repeating: one, count: 100).joined(separator: ",") + "]}"
        let f = try JSONDecoder().decode(FramePayload.self, from: Data(json.utf8))
        let tile = try XCTUnwrap(tiles(["d": f], at: ["d": t0], now: t0).first)
        XCTAssertEqual(tile.boxes.count, VideoTileLogic.maxBoxesPerTile)
        XCTAssertEqual(tile.hiddenBoxCount, 36)
        XCTAssertEqual(tile.accessibilityText, "d video, 100 detections, 64 matches")
        let both = try frame("edge_video_with_match")
        XCTAssertEqual(tiles(["drone-1": both, "Drone-1": both], at: ["drone-1": t0, "Drone-1": t0], now: t0).map(\.id), ["drone-1", "Drone-1"])
    }

    func testPngAndOversizedSourcesAreRefused() throws {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let png = UIGraphicsImageRenderer(size: CGSize(width: 64, height: 64), format: format).pngData { ctx in
            UIColor.green.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
        }
        XCTAssertNil(VideoTileLogic.decodeImage(base64: png.base64EncodedString(), maxPixelSize: 100))
        let big = jpeg(4500, 4000)
        XCTAssertNil(VideoTileLogic.decodeImage(base64: big.base64EncodedString(), maxPixelSize: 100), "18 MP is over the cap")
        XCTAssertNotNil(VideoTileLogic.decodeImage(base64: jpeg(320, 180).base64EncodedString(), maxPixelSize: 100))
        XCTAssertNil(VideoTileLogic.decodeImage(base64: jpeg(320, 180).base64EncodedString(), maxPixelSize: 100, isCancelled: { true }))
    }

    @MainActor
    func testBurstOfFramesStartsAtMostTwoDecodes() async throws {
        let counter = Counter()
        let decoder = VideoTileDecoder(decode: { text, _, _ in
            counter.bump()
            Thread.sleep(forTimeInterval: 0.02)
            return text == "last" ? VideoTileLogic.decodeImage(base64: Data("x".utf8).base64EncodedString(), maxPixelSize: 10) : nil
        })
        for i in 0..<100 { decoder.submit(base64: i == 99 ? "last" : "f\(i)", pixels: 100) }
        try await Task.sleep(for: .milliseconds(400))
        XCTAssertLessThanOrEqual(counter.value, 2)
        XCTAssertLessThanOrEqual(decoder.decodesStarted, 2)
        decoder.submit(base64: nil, pixels: 100)
        XCTAssertNil(decoder.image)
    }

    @MainActor
    func testDecoderKeepsLatestImageAndSurfacesFailure() async throws {
        let good = jpeg(32, 18).base64EncodedString()
        let decoder = VideoTileDecoder()
        decoder.submit(base64: good, pixels: 100)
        for _ in 0..<100 where decoder.image == nil { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertEqual(decoder.image?.width, 32)
        decoder.submit(base64: "corrupt", pixels: 100)
        for _ in 0..<100 where !decoder.failed { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertTrue(decoder.failed)
        XCTAssertNil(decoder.image)
    }
}

private final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    func bump() { lock.withLock { n += 1 } }
    var value: Int { lock.withLock { n } }
}
