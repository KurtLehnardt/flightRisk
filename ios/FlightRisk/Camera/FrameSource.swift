import CoreGraphics

protocol FrameSource: AnyObject {
    func start()
    func stop()
    func getLatestFrame() -> CGImage?
    func setOnFrameCallback(_ callback: @escaping (CGImage) -> Void)
}
