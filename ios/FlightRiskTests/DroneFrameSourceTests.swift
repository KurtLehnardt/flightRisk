import CoreGraphics
import XCTest
@testable import FlightRisk

/// A drone frame is processed once and only while it is fresh. A repeated or stalled frame must
/// never add a ReID score, because three scores confirm a match.
@MainActor
final class DroneFrameSourceTests: XCTestCase {

    private static func makeImage() -> CGImage {
        let ctx = CGContext(data: nil, width: 8, height: 8, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(),
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        return ctx.makeImage()!
    }

    private final class StubLink: DroneLink {
        var hasConnectedDrone = true
        var frame: DroneFrame?
        func newestFrame() -> DroneFrame? { frame }
    }

    private final class Clock: @unchecked Sendable {
        private let lock = NSLock()
        private var current: Date
        init(_ start: Date) { current = start }
        var now: Date { lock.withLock { current } }
        func advance(_ seconds: TimeInterval) { lock.withLock { current = current.addingTimeInterval(seconds) } }
    }

    private final class CountingDetector: PipelineDetectionCallback, @unchecked Sendable {
        private let lock = NSLock()
        private var calls = 0
        var callCount: Int { lock.withLock { calls } }
        func detect(frame: CGImage) -> [Detection] {
            lock.withLock { calls += 1 }
            return []
        }
        func annotate(frame: CGImage, detections: [Detection], matchIdx: Int?) -> CGImage? { nil }
    }

    func testSameFrameIsReturnedOnlyOnce() {
        let link = StubLink()
        let clock = Clock(Date())
        let source = DroneFrameSource(link: link, now: { clock.now })
        link.frame = DroneFrame(image: Self.makeImage(), arrivedAt: clock.now)
        source.pollOnce()
        XCTAssertNotNil(source.getLatestFrame())
        for _ in 0..<5 {
            source.pollOnce()
            XCTAssertNil(source.getLatestFrame(), "a frame already handed out is not offered again")
        }
        clock.advance(0.5)
        link.frame = DroneFrame(image: Self.makeImage(), arrivedAt: clock.now)
        source.pollOnce()
        XCTAssertNotNil(source.getLatestFrame(), "a new frame is offered")
    }

    func testStaleFrameReturnsNil() {
        let link = StubLink()
        let clock = Clock(Date())
        let source = DroneFrameSource(link: link, maxFrameAge: 2, now: { clock.now })
        link.frame = DroneFrame(image: Self.makeImage(), arrivedAt: clock.now)
        source.pollOnce()
        clock.advance(2.5)
        XCTAssertNil(source.getLatestFrame())
    }

    func testDisconnectClearsTheLatestFrame() {
        let link = StubLink()
        let clock = Clock(Date())
        let source = DroneFrameSource(link: link, now: { clock.now })
        link.frame = DroneFrame(image: Self.makeImage(), arrivedAt: clock.now)
        source.pollOnce()
        link.hasConnectedDrone = false
        source.pollOnce()
        XCTAssertNil(source.getLatestFrame())
    }

    func testPipelineScoresOneFrameOnceEvenWhenPolledRepeatedly() async throws {
        let link = StubLink()
        link.frame = DroneFrame(image: Self.makeImage(), arrivedAt: Date())
        let source = DroneFrameSource(link: link)
        source.pollOnce()
        let pipeline = SearchPipeline(config: FlightRiskConfig.shared, llmSelector: LlmSelector(),
                                      alertManager: AlertManager(), locationProvider: LocationProvider())
        let detector = CountingDetector()
        await pipeline.wireCallbacks(detection: detector, reid: NoopReid())
        await pipeline.setFrameSource(PollingSource(source))
        await pipeline.start()
        try await Task.sleep(for: .milliseconds(500))
        await pipeline.stop()
        XCTAssertEqual(detector.callCount, 1, "one frame is processed once, however long the loop spins")
    }

    /// Keeps the pipeline's loop spinning over the same stored frame, as a stalled feed would.
    private final class PollingSource: FrameSource, @unchecked Sendable {
        private let inner: DroneFrameSource
        init(_ inner: DroneFrameSource) { self.inner = inner }
        func start() {}
        func stop() {}
        func getLatestFrame() -> CGImage? { inner.getLatestFrame() }
        func setOnFrameCallback(_ callback: @escaping (CGImage) -> Void) {}
    }

    private final class NoopReid: PipelineReidCallback, @unchecked Sendable {
        var matchThreshold: Float { 0.5 }
        func setTarget(photo: CGImage) -> Bool { true }
        func findMatch(detections: [Detection]) -> (index: Int?, score: Float) { (nil, 0) }
        func compare(crop: CGImage) -> Float { 0 }
    }
}
