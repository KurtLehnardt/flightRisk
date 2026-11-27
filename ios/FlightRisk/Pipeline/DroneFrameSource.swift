import CoreGraphics
import Foundation

/// What the Search tab needs to know about the drone link: whether a drone is connected, and
/// the newest video frame a connected drone sent.
@MainActor
protocol DroneLink: AnyObject {
    /// True only while the ground station link is live and at least one drone is connected.
    /// The demo's simulated drones do not count.
    var hasConnectedDrone: Bool { get }
    /// The newest decoded frame of a connected drone, or nil when there is none yet.
    func newestFrame() -> DroneFrame?
}

/// A decoded drone video frame and the moment it arrived. Two frames with the same arrival time
/// are the same frame.
struct DroneFrame {
    let image: CGImage
    let arrivedAt: Date
}

/// A ``FrameSource`` fed by drone video only. It polls the ``DroneLink`` while the search runs
/// and keeps the newest frame for the pipeline. It never opens a camera.
///
/// Each frame is handed to the pipeline at most once, and a frame older than `maxFrameAge` is
/// never handed out. Without both rules the pipeline would score one frame again and again, and
/// a stalled feed would keep confirming a match from the last picture it saw.
final class DroneFrameSource: FrameSource, @unchecked Sendable {
    private let link: any DroneLink
    private let pollInterval: Duration
    private let lock = NSLock()
    private let maxFrameAge: TimeInterval
    private let now: @Sendable () -> Date
    private var latest: DroneFrame?
    private var lastServedAt: Date?
    private var callback: ((CGImage) -> Void)?
    private var poller: Task<Void, Never>?

    init(link: any DroneLink,
         pollInterval: Duration = .milliseconds(200),
         maxFrameAge: TimeInterval = 2,
         now: @escaping @Sendable () -> Date = { Date() }) {
        self.link = link
        self.pollInterval = pollInterval
        self.maxFrameAge = maxFrameAge
        self.now = now
    }

    func start() {
        lock.lock(); defer { lock.unlock() }
        guard poller == nil else { return }
        poller = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.pollOnce()
                try? await Task.sleep(for: self.pollInterval)
            }
        }
    }

    func stop() {
        lock.lock(); defer { lock.unlock() }
        poller?.cancel()
        poller = nil
        latest = nil
        lastServedAt = nil
    }

    /// Returns the newest frame once. A frame already returned, or one older than `maxFrameAge`,
    /// gives nil.
    func getLatestFrame() -> CGImage? {
        lock.lock(); defer { lock.unlock() }
        guard let frame = latest else { return nil }
        if let served = lastServedAt, frame.arrivedAt <= served { return nil }
        guard now().timeIntervalSince(frame.arrivedAt) <= maxFrameAge else { return nil }
        lastServedAt = frame.arrivedAt
        return frame.image
    }

    func setOnFrameCallback(_ callback: @escaping (CGImage) -> Void) {
        lock.lock(); defer { lock.unlock() }
        self.callback = callback
    }

    @MainActor
    func pollOnce() {
        guard link.hasConnectedDrone else {
            lock.lock(); latest = nil; lock.unlock()
            return
        }
        guard let frame = link.newestFrame() else { return }
        lock.lock()
        if let current = latest, frame.arrivedAt <= current.arrivedAt {
            lock.unlock()
            return
        }
        latest = frame
        let cb = callback
        lock.unlock()
        cb?(frame.image)
    }
}
