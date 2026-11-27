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
    func newestFrame() -> CGImage?
}

/// A ``FrameSource`` fed by drone video only. It polls the ``DroneLink`` while the search runs
/// and keeps the newest frame for the pipeline. It never opens a camera.
final class DroneFrameSource: FrameSource, @unchecked Sendable {
    private let link: any DroneLink
    private let pollInterval: Duration
    private let lock = NSLock()
    private var latest: CGImage?
    private var callback: ((CGImage) -> Void)?
    private var poller: Task<Void, Never>?

    init(link: any DroneLink, pollInterval: Duration = .milliseconds(200)) {
        self.link = link
        self.pollInterval = pollInterval
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
    }

    func getLatestFrame() -> CGImage? {
        lock.lock(); defer { lock.unlock() }
        return latest
    }

    func setOnFrameCallback(_ callback: @escaping (CGImage) -> Void) {
        lock.lock(); defer { lock.unlock() }
        self.callback = callback
    }

    @MainActor
    private func pollOnce() {
        guard link.hasConnectedDrone, let image = link.newestFrame() else { return }
        lock.lock()
        latest = image
        let cb = callback
        lock.unlock()
        cb?(image)
    }
}
