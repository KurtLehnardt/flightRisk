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
        XCTAssertEqual(tile.accessibilityText, "drone-1 video, 2 detections")
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
        XCTAssertFalse(VideoTileLogic.isStale(arrivedAt: t0 + 10, now: t0), "future stamp is fresh")
        XCTAssertTrue(VideoTileLogic.isStale(arrivedAt: nil, now: t0))
    }

    func testStaleTileShowsNoVideoAndClearsBoxes() throws {
        let f = try frame("edge_video_with_match")
        let tile = try XCTUnwrap(tiles(["drone-1": f], at: ["drone-1": t0], now: t0 + 5, stream: false).first)
        XCTAssertEqual(tile.mode, .noVideo)
        XCTAssertTrue(tile.boxes.isEmpty)
        XCTAssertNil(tile.imageBase64)
        XCTAssertTrue(tile.videoOffAtGround)
        XCTAssertEqual(tile.accessibilityText, "drone-1 video, no video")
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
}
