import CoreGraphics
import Foundation

/// The Search tab's view of the ground station link: a drone counts as connected only while the
/// live link is up, the state is fresh and a drone card reports a connected link. The demo's
/// simulated drones never count, so the demo cannot start a search.
@MainActor
final class GroundDroneLink: DroneLink {
    private let ground: GroundViewModel
    private var cachedImage: CGImage?
    private var cachedAt: Date?

    init(_ ground: GroundViewModel) {
        self.ground = ground
    }

    var hasConnectedDrone: Bool {
        guard ground.linkStatus == .connected, !ground.isStateStale, !ground.isDemoActive else { return false }
        return ground.reducer.cards.contains { $0.link == .connected }
    }

    func newestFrame() -> DroneFrame? {
        let connected = Set(ground.reducer.cards.filter { $0.link == .connected }.map(\.id))
        let newest = ground.latestFrameAt
            .filter { connected.contains($0.key) }
            .max { $0.value < $1.value }
        guard let newest, let base64 = ground.latestFrames[newest.key]?.image, !base64.isEmpty else { return nil }
        if cachedAt == newest.value, let cachedImage { return DroneFrame(image: cachedImage, arrivedAt: newest.value) }
        guard let image = VideoTileLogic.decodeImage(base64: base64, maxPixelSize: 640) else { return nil }
        cachedImage = image
        cachedAt = newest.value
        return DroneFrame(image: image, arrivedAt: newest.value)
    }
}
