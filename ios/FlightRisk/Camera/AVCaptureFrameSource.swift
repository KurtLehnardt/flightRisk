import AVFoundation
import CoreImage
import CoreGraphics
import os

final class AVCaptureFrameSource: NSObject, FrameSource, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let captureSession = AVCaptureSession()
    private let sessionQueue = DispatchQueue(label: "com.flightrisk.camera.session")
    private let outputQueue = DispatchQueue(label: "com.flightrisk.camera.output")
    private let ciContext = CIContext()
    private let logger = Logger(subsystem: "com.flightrisk", category: "camera")

    private var latestFrame: CGImage?
    private let frameLock = NSLock()
    private var onFrameCallback: ((CGImage) -> Void)?

    var session: AVCaptureSession { captureSession }

    override init() {
        super.init()
        configureCaptureSession()
    }

    private func configureCaptureSession() {
        captureSession.beginConfiguration()
        captureSession.sessionPreset = .high

        guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            logger.error("Back camera not available")
            captureSession.commitConfiguration()
            return
        }

        do {
            let input = try AVCaptureDeviceInput(device: camera)
            if captureSession.canAddInput(input) {
                captureSession.addInput(input)
            } else {
                logger.error("Cannot add camera input to session")
                captureSession.commitConfiguration()
                return
            }
        } catch {
            logger.error("Failed to create camera input: \(error.localizedDescription)")
            captureSession.commitConfiguration()
            return
        }

        let output = AVCaptureVideoDataOutput()
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: outputQueue)

        if captureSession.canAddOutput(output) {
            captureSession.addOutput(output)
            if let connection = output.connection(with: .video) {
                connection.videoRotationAngle = 90
            }
        } else {
            logger.error("Cannot add video output to session")
        }

        captureSession.commitConfiguration()
    }

    func start() {
        sessionQueue.async { [weak self] in
            guard let self = self else { return }
            AVCaptureDevice.requestAccess(for: .video) { granted in
                if granted {
                    self.sessionQueue.async {
                        if !self.captureSession.isRunning {
                            self.captureSession.startRunning()
                            self.logger.info("Capture session started")
                        }
                    }
                } else {
                    self.logger.warning("Camera access denied")
                }
            }
        }
    }

    func stop() {
        sessionQueue.async { [weak self] in
            guard let self = self else { return }
            if self.captureSession.isRunning {
                self.captureSession.stopRunning()
                self.logger.info("Capture session stopped")
            }
        }
    }

    func getLatestFrame() -> CGImage? {
        frameLock.lock()
        defer { frameLock.unlock() }
        return latestFrame
    }

    func setOnFrameCallback(_ callback: @escaping (CGImage) -> Void) {
        frameLock.lock()
        onFrameCallback = callback
        frameLock.unlock()
    }

    // MARK: - AVCaptureVideoDataOutputSampleBufferDelegate

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            return
        }

        let ciImage = CIImage(cvPixelBuffer: pixelBuffer)
        guard let cgImage = ciContext.createCGImage(ciImage, from: ciImage.extent) else {
            return
        }

        frameLock.lock()
        latestFrame = cgImage
        let callback = onFrameCallback
        frameLock.unlock()

        callback?(cgImage)
    }
}
