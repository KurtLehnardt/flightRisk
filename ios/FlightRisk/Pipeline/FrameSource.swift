import CoreGraphics

/// Where the search pipeline gets its video frames. The only source in the app is the drone
/// video that arrives through the ground station (see ``DroneFrameSource``). The phone's own
/// camera is never a frame source.
protocol FrameSource: AnyObject {
    func start()
    func stop()
    func getLatestFrame() -> CGImage?
    func setOnFrameCallback(_ callback: @escaping (CGImage) -> Void)
}
