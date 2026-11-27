import CoreGraphics
import XCTest
@testable import FlightRisk

/// A search runs only with a connected drone. Without one, "Start search" refuses with one short
/// message, nothing starts, and no video source is ever opened. The phone's own camera is not a
/// source at all.
@MainActor
final class SearchNeedsDroneTests: XCTestCase {

    private final class FakeLink: DroneLink {
        var hasConnectedDrone: Bool
        init(connected: Bool) { hasConnectedDrone = connected }
        func newestFrame() -> CGImage? { nil }
    }

    private final class SpyFrameSource: FrameSource, @unchecked Sendable {
        private let lock = NSLock()
        private var starts = 0
        var startCount: Int { lock.withLock { starts } }
        func start() { lock.withLock { starts += 1 } }
        func stop() {}
        func getLatestFrame() -> CGImage? { nil }
        func setOnFrameCallback(_ callback: @escaping (CGImage) -> Void) {}
    }

    private func makeModel(link: (any DroneLink)?) async -> (SearchViewModel, SearchPipeline, SpyFrameSource) {
        let pipeline = SearchPipeline(
            config: FlightRiskConfig.shared,
            llmSelector: LlmSelector(),
            alertManager: AlertManager(),
            locationProvider: LocationProvider()
        )
        let spy = SpyFrameSource()
        await pipeline.setFrameSource(spy)
        let model = SearchViewModel()
        model.droneLink = link
        model.observe(pipeline: pipeline)
        return (model, pipeline, spy)
    }

    func testStartSearchWithNoDroneRefusesAndStaysIdle() async {
        let (model, pipeline, spy) = await makeModel(link: FakeLink(connected: false))
        model.startSearch()
        try? await Task.sleep(for: .milliseconds(200))
        XCTAssertFalse(model.isSearching)
        XCTAssertEqual(model.statusMessage, "Connect a drone first.")
        let running = await pipeline.isRunning
        XCTAssertFalse(running)
        XCTAssertEqual(spy.startCount, 0, "no video source is opened without a drone")
    }

    func testStartSearchWithNoLinkAtAllRefuses() async {
        let (model, _, spy) = await makeModel(link: nil)
        model.startSearch()
        XCTAssertFalse(model.isSearching)
        XCTAssertEqual(model.statusMessage, SearchViewModel.connectDroneFirst)
        XCTAssertEqual(spy.startCount, 0)
    }

    func testStartSearchWithADroneStartsAndClearsTheMessage() async {
        let link = FakeLink(connected: false)
        let (model, pipeline, spy) = await makeModel(link: link)
        model.startSearch()
        XCTAssertNotNil(model.statusMessage)
        link.hasConnectedDrone = true
        model.startSearch()
        XCTAssertTrue(model.isSearching)
        XCTAssertNil(model.statusMessage)
        try? await Task.sleep(for: .milliseconds(200))
        let running = await pipeline.isRunning
        XCTAssertTrue(running)
        XCTAssertEqual(spy.startCount, 1)
        model.stopSearch()
    }

    func testLosingTheDroneStopsARunningSearch() async {
        let link = FakeLink(connected: true)
        let (model, _, _) = await makeModel(link: link)
        model.startSearch()
        XCTAssertTrue(model.isSearching)
        link.hasConnectedDrone = false
        model.droneDidDisconnect()
        XCTAssertFalse(model.isSearching)
        XCTAssertEqual(model.statusMessage, SearchViewModel.droneDisconnected)
    }

    func testDroneDisconnectWhileIdleChangesNothing() async {
        let (model, _, _) = await makeModel(link: FakeLink(connected: false))
        model.droneDidDisconnect()
        XCTAssertNil(model.statusMessage)
    }

    /// The phone camera is gone from the app: no source file opens a capture session. Taking the
    /// target photo uses the system picker, which is not a frame source.
    func testNoSourceFileOpensACaptureSession() throws {
        let appDir = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("FlightRisk")
        let files = try XCTUnwrap(FileManager.default.enumerator(at: appDir, includingPropertiesForKeys: nil))
        var checked = 0
        for case let url as URL in files where url.pathExtension == "swift" {
            let text = try String(contentsOf: url, encoding: .utf8)
            checked += 1
            XCTAssertFalse(text.contains("AVCaptureSession"), "\(url.lastPathComponent) opens a capture session")
            XCTAssertFalse(text.contains("AVCaptureDevice"), "\(url.lastPathComponent) opens a capture device")
        }
        XCTAssertGreaterThan(checked, 20)
    }
}
