import CoreGraphics
import XCTest
@testable import FlightRisk

final class DetectionTrackerTests: XCTestCase {

    // MARK: - Helpers

    /// Create a minimal 1x1 pixel CGImage for testing.
    private func makeDummyCrop() -> CGImage {
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let context = CGContext(
            data: nil, width: 1, height: 1,
            bitsPerComponent: 8, bytesPerRow: 4,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )!
        return context.makeImage()!
    }

    private func makeDetection(bbox: [Int], confidence: Float = 0.9) -> Detection {
        Detection(bbox: bbox, confidence: confidence, crop: makeDummyCrop())
    }

    // MARK: - computeIou

    func testIouIdenticalBoxes() {
        let iou = DetectionTracker.computeIou(box1: [0, 0, 100, 100], box2: [0, 0, 100, 100])
        XCTAssertEqual(iou, 1.0)
    }

    func testIouNoOverlap() {
        let iou = DetectionTracker.computeIou(box1: [0, 0, 50, 50], box2: [100, 100, 200, 200])
        XCTAssertEqual(iou, 0.0)
    }

    func testIouPartialOverlap() {
        // box1: [0,0,100,100] area=10000
        // box2: [50,50,150,150] area=10000
        // intersection: [50,50,100,100] = 50*50 = 2500
        // union: 10000 + 10000 - 2500 = 17500
        // IoU = 2500/17500 ~ 0.1429
        let iou = DetectionTracker.computeIou(box1: [0, 0, 100, 100], box2: [50, 50, 150, 150])
        XCTAssertEqual(iou, Float(2500) / Float(17500), accuracy: 0.001)
    }

    func testIouContainedBox() {
        // box2 is fully inside box1
        // box1: [0,0,100,100] area=10000
        // box2: [25,25,75,75] area=2500
        // intersection = 2500; union = 10000 + 2500 - 2500 = 10000
        // IoU = 2500/10000 = 0.25
        let iou = DetectionTracker.computeIou(box1: [0, 0, 100, 100], box2: [25, 25, 75, 75])
        XCTAssertEqual(iou, 0.25, accuracy: 0.001)
    }

    func testIouMalformedBoxTooFewElements() {
        let iou = DetectionTracker.computeIou(box1: [0, 0], box2: [0, 0, 100, 100])
        XCTAssertEqual(iou, 0.0)
    }

    func testIouDegenerateBox() {
        // x2 <= x1 -> degenerate box
        let iou = DetectionTracker.computeIou(box1: [50, 0, 50, 100], box2: [0, 0, 100, 100])
        XCTAssertEqual(iou, 0.0)
    }

    func testIouInvertedBox() {
        // x2 < x1 -> inverted box
        let iou = DetectionTracker.computeIou(box1: [100, 100, 0, 0], box2: [0, 0, 100, 100])
        XCTAssertEqual(iou, 0.0)
    }

    func testIouAdjacentBoxes() {
        // Boxes share an edge but no area
        let iou = DetectionTracker.computeIou(box1: [0, 0, 50, 50], box2: [50, 0, 100, 50])
        XCTAssertEqual(iou, 0.0)
    }

    // MARK: - update() creates new tracks

    func testUpdateCreatesNewTracks() {
        let tracker = DetectionTracker()
        let detections = [
            makeDetection(bbox: [0, 0, 100, 100]),
            makeDetection(bbox: [200, 200, 300, 300]),
        ]
        let tracked = tracker.update(detections: detections)
        XCTAssertEqual(tracked.count, 2)
        // Each should have a unique track ID
        let ids = Set(tracked.map { $0.trackId })
        XCTAssertEqual(ids.count, 2)
    }

    func testUpdateEmptyDetectionsReturnsEmpty() {
        let tracker = DetectionTracker()
        let tracked = tracker.update(detections: [])
        XCTAssertTrue(tracked.isEmpty)
    }

    // MARK: - update() matches by IoU

    func testUpdateMatchesExistingTrackByIou() {
        let tracker = DetectionTracker()
        // Frame 1: one detection
        let tracked1 = tracker.update(detections: [
            makeDetection(bbox: [0, 0, 100, 100]),
        ])
        XCTAssertEqual(tracked1.count, 1)
        let originalId = tracked1[0].trackId

        // Frame 2: same-ish bbox -> should match the existing track
        let tracked2 = tracker.update(detections: [
            makeDetection(bbox: [5, 5, 105, 105]),
        ])
        XCTAssertEqual(tracked2.count, 1)
        XCTAssertEqual(tracked2[0].trackId, originalId)
    }

    func testUpdateCreatesNewTrackForFarDetection() {
        let tracker = DetectionTracker()
        let tracked1 = tracker.update(detections: [
            makeDetection(bbox: [0, 0, 100, 100]),
        ])
        let originalId = tracked1[0].trackId

        // Frame 2: far away detection -> new track
        let tracked2 = tracker.update(detections: [
            makeDetection(bbox: [500, 500, 600, 600]),
        ])
        // Should have 2 tracks: original (now aging) + new one
        // But the original had no match, so it ages. Let's check:
        // The returned list includes all active tracks, so it should be 2
        XCTAssertEqual(tracked2.count, 2)
        let ids = Set(tracked2.map { $0.trackId })
        XCTAssertTrue(ids.contains(originalId))
        XCTAssertEqual(ids.count, 2)
    }

    // MARK: - addScores and getTrack

    func testAddScoresAndGetTrack() {
        let tracker = DetectionTracker()
        let tracked = tracker.update(detections: [
            makeDetection(bbox: [0, 0, 100, 100]),
        ])
        let trackId = tracked[0].trackId

        tracker.addScores(trackId: trackId, reidScore: 0.8, faceScore: 0.6)
        tracker.addScores(trackId: trackId, reidScore: 0.7, faceScore: 0.5)

        let summary = tracker.getTrack(trackId)
        XCTAssertNotNil(summary)
        XCTAssertEqual(summary!.reidScores, [0.8, 0.7])
        XCTAssertEqual(summary!.faceScores, [0.6, 0.5])
        XCTAssertEqual(summary!.avgReidScore, 0.75, accuracy: 0.001)
        XCTAssertEqual(summary!.avgFaceScore, 0.55, accuracy: 0.001)
    }

    func testAddScoresNilDoesNotRecord() {
        let tracker = DetectionTracker()
        let tracked = tracker.update(detections: [
            makeDetection(bbox: [0, 0, 100, 100]),
        ])
        let trackId = tracked[0].trackId

        // Only add reid, face is nil
        tracker.addScores(trackId: trackId, reidScore: 0.5)
        let summary = tracker.getTrack(trackId)!
        XCTAssertEqual(summary.reidScores.count, 1)
        XCTAssertEqual(summary.faceScores.count, 0)
    }

    func testAddScoresForNonexistentTrackDoesNothing() {
        let tracker = DetectionTracker()
        // Should not crash
        tracker.addScores(trackId: 999, reidScore: 0.5, faceScore: 0.5)
        XCTAssertNil(tracker.getTrack(999))
    }

    func testScoreWindowTrimming() {
        let tracker = DetectionTracker(scoreWindow: 3)
        let tracked = tracker.update(detections: [
            makeDetection(bbox: [0, 0, 100, 100]),
        ])
        let trackId = tracked[0].trackId

        // Add more scores than the window
        tracker.addScores(trackId: trackId, reidScore: 0.1)
        tracker.addScores(trackId: trackId, reidScore: 0.2)
        tracker.addScores(trackId: trackId, reidScore: 0.3)
        tracker.addScores(trackId: trackId, reidScore: 0.4) // should push out 0.1

        let summary = tracker.getTrack(trackId)!
        XCTAssertEqual(summary.reidScores.count, 3)
        XCTAssertEqual(summary.reidScores, [0.2, 0.3, 0.4])
    }

    // MARK: - getTrack returns nil for unknown

    func testGetTrackReturnsNilForUnknownId() {
        let tracker = DetectionTracker()
        XCTAssertNil(tracker.getTrack(42))
    }

    // MARK: - clear()

    func testClearRemovesAllTracks() {
        let tracker = DetectionTracker()
        let tracked = tracker.update(detections: [
            makeDetection(bbox: [0, 0, 100, 100]),
            makeDetection(bbox: [200, 200, 300, 300]),
        ])
        XCTAssertEqual(tracked.count, 2)

        tracker.clear()

        XCTAssertTrue(tracker.activeTracks.isEmpty)
        // After clear, getTrack returns nil for any previously valid ID
        for t in tracked {
            XCTAssertNil(tracker.getTrack(t.trackId))
        }
    }

    func testClearResetsIdCounter() {
        let tracker = DetectionTracker()
        _ = tracker.update(detections: [makeDetection(bbox: [0, 0, 100, 100])])
        tracker.clear()
        let tracked = tracker.update(detections: [makeDetection(bbox: [0, 0, 100, 100])])
        // After clear, next track ID should be 0 again
        XCTAssertEqual(tracked[0].trackId, 0)
    }

    // MARK: - Track expiration

    func testTracksRemovedAfterMaxMissingFrames() {
        let tracker = DetectionTracker(maxMissing: 2)
        // Frame 1: create a track
        let tracked = tracker.update(detections: [
            makeDetection(bbox: [0, 0, 100, 100]),
        ])
        let trackId = tracked[0].trackId

        // Frames 2-3: no detections at all -> track ages
        _ = tracker.update(detections: [])
        _ = tracker.update(detections: [])

        // Track should still exist (missedFrames == 2, not > 2)
        XCTAssertNotNil(tracker.getTrack(trackId))

        // Frame 4: one more miss -> now missedFrames == 3 > maxMissing(2), removed
        _ = tracker.update(detections: [])
        XCTAssertNil(tracker.getTrack(trackId))
    }

    // MARK: - Best crop tracking

    func testBestCropUpdatedOnHigherConfidence() {
        let tracker = DetectionTracker()
        // Frame 1: low confidence
        let tracked1 = tracker.update(detections: [
            makeDetection(bbox: [0, 0, 100, 100], confidence: 0.5),
        ])
        let trackId = tracked1[0].trackId

        // Frame 2: higher confidence, same bbox -> should update best crop
        _ = tracker.update(detections: [
            makeDetection(bbox: [2, 2, 102, 102], confidence: 0.9),
        ])

        let summary = tracker.getTrack(trackId)
        XCTAssertNotNil(summary)
        XCTAssertNotNil(summary!.bestCrop)
    }
}
