import Foundation
import Network
import VideoToolbox
import CoreImage
import CoreVideo
import QuartzCore
import os.log

/// Receives and decodes the Tello's H264 video stream using VideoToolbox.
///
/// Port of Android `TelloFrameSource.kt` to iOS. The Tello streams H264 at
/// 960x720 over UDP port 11111. This class receives raw NAL units, reassembles
/// them across UDP packet boundaries, feeds them to a `VTDecompressionSession`
/// hardware decoder, and delivers decoded `CGImage` frames to the vision pipeline.
///
/// Frozen-frame detection mirrors the Python/Android implementations: a hash of
/// the center 8x8 pixel block is compared across consecutive frames, and
/// `onStreamFrozen` fires when the count exceeds the threshold.
final class TelloFrameSource: FrameSource {

    // MARK: - Constants

    private static let logger = Logger(subsystem: "com.flightrisk", category: "TelloFrameSource")
    private static let udpBufferSize = 2048
    private static let idleSleepNs: UInt64 = 5_000_000 // 5ms
    private static let telloVideoPort: UInt16 = 11111
    private static let telloHost = "192.168.10.1"

    // MARK: - Thread-safe frame storage

    private let lock = NSLock()
    private var latestFrame: CGImage?
    private var frameCallback: ((CGImage) -> Void)?

    // MARK: - State

    private var isRunning = false

    /// Set by DroneManager to handle frozen-stream recovery.
    var onStreamFrozen: (() -> Void)?

    // MARK: - Networking

    private var listener: NWListener?
    private let receiveQueue = DispatchQueue(label: "com.flightrisk.tello.receive", qos: .userInitiated)
    private let decodeQueue = DispatchQueue(label: "com.flightrisk.tello.decode", qos: .userInitiated)

    // MARK: - NAL reassembly

    private var nalAccumulator = Data(capacity: 65536)
    private let maxAccumulatorSize = 512 * 1024 // 512KB
    private var seenFirstStartCode = false
    private var nalQueue: [Data] = []
    private let nalQueueLock = NSLock()
    private let maxQueueSize = 120

    // MARK: - H264 decoder

    private var decompressionSession: VTDecompressionSession?
    private var formatDescription: CMVideoFormatDescription?
    private var currentSPS: Data?
    private var currentPPS: Data?
    private let ciContext = CIContext(options: [.useSoftwareRenderer: false])

    // MARK: - Frozen frame detection

    private var lastFrameHash: Int64 = 0
    private var frozenFrameCount = 0
    private let frozenFrameThreshold = 100

    // MARK: - NAL start code

    private static let nalStartCode: [UInt8] = [0x00, 0x00, 0x00, 0x01]

    // MARK: - FrameSource Protocol

    func start() {
        guard !isRunning else { return }

        do {
            let params = NWParameters.udp
            params.allowLocalEndpointReuse = true
            let listener = try NWListener(using: params, on: NWEndpoint.Port(rawValue: Self.telloVideoPort)!)
            self.listener = listener

            listener.newConnectionHandler = { [weak self] connection in
                self?.handleNewConnection(connection)
            }

            listener.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    Self.logger.info("Listening on UDP port \(Self.telloVideoPort)")
                case .failed(let error):
                    Self.logger.error("Listener failed: \(error.localizedDescription)")
                default:
                    break
                }
            }

            listener.start(queue: receiveQueue)
            isRunning = true

            // Start decode loop
            decodeQueue.async { [weak self] in
                self?.decodeLoop()
            }

            Self.logger.info("TelloFrameSource started")
        } catch {
            Self.logger.error("Failed to start listener: \(error.localizedDescription)")
        }
    }

    deinit {
        if let session = decompressionSession {
            VTDecompressionSessionInvalidate(session)
        }
        listener?.cancel()
    }

    func stop() {
        guard isRunning else { return }
        isRunning = false

        listener?.cancel()
        listener = nil

        // Tear down decoder
        if let session = decompressionSession {
            VTDecompressionSessionInvalidate(session)
            decompressionSession = nil
        }
        formatDescription = nil
        currentSPS = nil
        currentPPS = nil

        lock.lock()
        latestFrame = nil
        frameCallback = nil
        lock.unlock()

        nalQueueLock.lock()
        nalQueue.removeAll()
        nalQueueLock.unlock()

        nalAccumulator.removeAll(keepingCapacity: true)
        seenFirstStartCode = false
        lastFrameHash = 0
        frozenFrameCount = 0

        Self.logger.info("TelloFrameSource stopped")
    }

    func getLatestFrame() -> CGImage? {
        lock.lock()
        defer { lock.unlock() }
        return latestFrame
    }

    func setOnFrameCallback(_ callback: @escaping (CGImage) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        frameCallback = callback
    }

    // MARK: - UDP Receive

    /// Handle a new inbound UDP connection from the Tello.
    private func handleNewConnection(_ connection: NWConnection) {
        // Validate sender address -- only accept from 192.168.10.1
        if case let .hostPort(host, _) = connection.currentPath?.remoteEndpoint {
            let hostStr: String
            switch host {
            case .ipv4(let addr):
                hostStr = "\(addr)"
            case .ipv6(let addr):
                hostStr = "\(addr)"
            case .name(let name, _):
                hostStr = name
            @unknown default:
                hostStr = ""
            }
            if !hostStr.contains(Self.telloHost) {
                Self.logger.warning("Rejected connection from unexpected host: \(hostStr)")
                connection.cancel()
                return
            }
        }

        connection.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                self?.receiveData(on: connection)
            case .failed(let error):
                Self.logger.warning("Connection failed: \(error.localizedDescription)")
            case .cancelled:
                break
            default:
                break
            }
        }
        connection.start(queue: receiveQueue)
    }

    /// Continuously receive UDP data from a connection.
    private func receiveData(on connection: NWConnection) {
        guard isRunning else { return }

        connection.receiveMessage { [weak self] data, _, _, error in
            guard let self, self.isRunning else { return }

            if let error {
                Self.logger.warning("Receive error: \(error.localizedDescription)")
                return
            }

            if let data {
                self.processReceivedData(data)
            }

            // Schedule next receive
            self.receiveData(on: connection)
        }
    }

    /// Process received UDP data: accumulate and extract NAL units.
    ///
    /// H264 NAL units (especially IDR frames) can span multiple UDP packets.
    /// We accumulate incoming bytes and split on NAL start codes
    /// (0x00 0x00 0x00 0x01), emitting complete NAL units to the decode queue.
    private func processReceivedData(_ data: Data) {
        // Guard against unbounded accumulator growth
        if nalAccumulator.count > maxAccumulatorSize {
            Self.logger.warning("NAL accumulator exceeded \(self.maxAccumulatorSize) bytes, resetting")
            nalAccumulator.removeAll(keepingCapacity: true)
            seenFirstStartCode = false
        }

        nalAccumulator.append(data)

        // Scan for NAL start codes
        let startCodes = findNalStartCodes(in: nalAccumulator)
        if startCodes.isEmpty { return }

        if !seenFirstStartCode {
            seenFirstStartCode = true
        }

        // Emit complete NALs (each bounded by two consecutive start codes)
        for i in 0 ..< startCodes.count - 1 {
            let nalData = nalAccumulator[startCodes[i] ..< startCodes[i + 1]]

            nalQueueLock.lock()
            // Drop oldest if queue is full
            while nalQueue.count > maxQueueSize {
                nalQueue.removeFirst()
            }
            nalQueue.append(Data(nalData))
            nalQueueLock.unlock()
        }

        // Keep the last (potentially incomplete) NAL in the accumulator
        let lastStart = startCodes.last!
        let remaining = Data(nalAccumulator[lastStart...])
        nalAccumulator.removeAll(keepingCapacity: true)
        nalAccumulator.append(remaining)
    }

    // MARK: - NAL Start Code Scanner

    /// Locate all 4-byte NAL start codes (0x00 0x00 0x00 0x01) in data.
    private func findNalStartCodes(in data: Data) -> [Int] {
        var positions: [Int] = []
        guard data.count >= 4 else { return positions }

        data.withUnsafeBytes { buffer in
            guard let ptr = buffer.baseAddress?.assumingMemoryBound(to: UInt8.self) else { return }
            let count = buffer.count
            for i in 0 ... count - 4 {
                if ptr[i] == 0x00 && ptr[i + 1] == 0x00 && ptr[i + 2] == 0x00 && ptr[i + 3] == 0x01 {
                    positions.append(i)
                }
            }
        }
        return positions
    }

    // MARK: - Decode Loop

    /// Dequeue NAL units and feed to VTDecompressionSession.
    private func decodeLoop() {
        while isRunning {
            var nalData: Data?

            nalQueueLock.lock()
            if !nalQueue.isEmpty {
                nalData = nalQueue.removeFirst()
            }
            nalQueueLock.unlock()

            if let nalData {
                processNalUnit(nalData)
            } else {
                // No work -- sleep briefly
                Thread.sleep(forTimeInterval: 0.005)
            }
        }
        Self.logger.debug("Decode loop exited")
    }

    /// Process a single NAL unit (with start code prefix).
    ///
    /// SPS and PPS NAL units are used to create or recreate the decoder.
    /// All other NAL types are sent to the decoder for frame output.
    private func processNalUnit(_ nalData: Data) {
        // NAL data includes the 4-byte start code prefix; the NAL type
        // is in the 5th byte (first byte after start code), masked with 0x1F.
        guard nalData.count > 4 else { return }
        let nalType = nalData[nalData.startIndex + 4] & 0x1F

        switch nalType {
        case 7: // SPS
            let spsPayload = nalData[(nalData.startIndex + 4)...]
            if currentSPS == nil || currentSPS != Data(spsPayload) {
                currentSPS = Data(spsPayload)
                Self.logger.debug("SPS updated (\(spsPayload.count) bytes)")
                recreateDecoderIfNeeded()
            }

        case 8: // PPS
            let ppsPayload = nalData[(nalData.startIndex + 4)...]
            if currentPPS == nil || currentPPS != Data(ppsPayload) {
                currentPPS = Data(ppsPayload)
                Self.logger.debug("PPS updated (\(ppsPayload.count) bytes)")
                recreateDecoderIfNeeded()
            }

        default:
            // Non-parameter NAL -- decode it
            decodeNalUnit(nalData)
        }
    }

    // MARK: - VTDecompressionSession Management

    /// Create or recreate the VTDecompressionSession when SPS/PPS change.
    private func recreateDecoderIfNeeded() {
        guard let sps = currentSPS, let pps = currentPPS else { return }

        // Invalidate existing session
        if let session = decompressionSession {
            VTDecompressionSessionInvalidate(session)
            decompressionSession = nil
            formatDescription = nil
        }

        // Build CMVideoFormatDescription from SPS and PPS
        var newFormat: CMVideoFormatDescription?

        // Keep pointers alive for the duration of the C call
        sps.withUnsafeBytes { spsBuffer in
            pps.withUnsafeBytes { ppsBuffer in
                let spsPtr = spsBuffer.baseAddress!.assumingMemoryBound(to: UInt8.self)
                let ppsPtr = ppsBuffer.baseAddress!.assumingMemoryBound(to: UInt8.self)
                var pointers: [UnsafePointer<UInt8>] = [spsPtr, ppsPtr]
                var sizes: [Int] = [sps.count, pps.count]

                let status = CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    allocator: kCFAllocatorDefault,
                    parameterSetCount: 2,
                    parameterSetPointers: &pointers,
                    parameterSetSizes: &sizes,
                    nalUnitHeaderLength: 4,
                    formatDescriptionOut: &newFormat
                )

                if status != noErr {
                    Self.logger.error("Failed to create format description: \(status)")
                    return
                }
            }
        }

        guard let format = newFormat else { return }
        formatDescription = format

        // Create decompression session
        let outputAttributes: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ]

        var callbackRecord = VTDecompressionOutputCallbackRecord(
            decompressionOutputCallback: Self.decompressionCallback,
            decompressionOutputRefCon: Unmanaged.passUnretained(self).toOpaque()
        )

        var session: VTDecompressionSession?
        let status = VTDecompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            formatDescription: format,
            decoderSpecification: nil,
            imageBufferAttributes: outputAttributes as CFDictionary,
            outputCallback: &callbackRecord,
            decompressionSessionOut: &session
        )

        if status != noErr {
            Self.logger.error("Failed to create decompression session: \(status)")
            return
        }

        decompressionSession = session
        Self.logger.info("VTDecompressionSession created")
    }

    // MARK: - NAL Decoding

    /// Wrap a NAL unit in CMBlockBuffer / CMSampleBuffer and submit to the decoder.
    private func decodeNalUnit(_ nalData: Data) {
        guard let session = decompressionSession, let format = formatDescription else { return }

        // Replace 4-byte start code with 4-byte length prefix (big-endian)
        // for Annex B -> AVCC conversion.
        let payloadLength = nalData.count - 4
        guard payloadLength > 0 else { return }

        var avccData = Data(count: nalData.count)
        let bigEndianLength = UInt32(payloadLength).bigEndian
        withUnsafeBytes(of: bigEndianLength) { lengthBytes in
            avccData.replaceSubrange(0 ..< 4, with: lengthBytes)
        }
        avccData.replaceSubrange(4 ..< nalData.count, with: nalData[(nalData.startIndex + 4)...])

        // Create CMBlockBuffer
        var blockBuffer: CMBlockBuffer?
        avccData.withUnsafeMutableBytes { rawBuffer in
            guard let baseAddress = rawBuffer.baseAddress else { return }
            let length = rawBuffer.count

            // Allocate a copy so CMBlockBuffer owns the memory
            let copy = UnsafeMutableRawPointer.allocate(byteCount: length, alignment: 1)
            copy.copyMemory(from: baseAddress, byteCount: length)

            let deallocator = kCFAllocatorDefault
            CMBlockBufferCreateWithMemoryBlock(
                allocator: kCFAllocatorDefault,
                memoryBlock: copy,
                blockLength: length,
                blockAllocator: deallocator,
                customBlockSource: nil,
                offsetToData: 0,
                dataLength: length,
                flags: 0,
                blockBufferOut: &blockBuffer
            )
        }

        guard let block = blockBuffer else { return }

        // Create CMSampleBuffer
        var sampleBuffer: CMSampleBuffer?
        var sampleSize = avccData.count
        var timing = CMSampleTimingInfo(
            duration: CMTime.invalid,
            presentationTimeStamp: CMTime(value: CMTimeValue(CACurrentMediaTime() * 1000), timescale: 1000),
            decodeTimeStamp: CMTime.invalid
        )

        let sampleStatus = CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault,
            dataBuffer: block,
            formatDescription: format,
            sampleCount: 1,
            sampleTimingEntryCount: 1,
            sampleTimingArray: &timing,
            sampleSizeEntryCount: 1,
            sampleSizeArray: &sampleSize,
            sampleBufferOut: &sampleBuffer
        )

        guard sampleStatus == noErr, let sample = sampleBuffer else {
            Self.logger.warning("Failed to create sample buffer: \(sampleStatus)")
            return
        }

        // Submit for decoding
        let decodeFlags = VTDecodeFrameFlags._EnableAsynchronousDecompression
        var infoFlags = VTDecodeInfoFlags()

        let decodeStatus = VTDecompressionSessionDecodeFrame(
            session,
            sampleBuffer: sample,
            flags: decodeFlags,
            frameRefcon: nil,
            infoFlagsOut: &infoFlags
        )

        if decodeStatus != noErr {
            Self.logger.debug("Decode frame error: \(decodeStatus)")
        }
    }

    // MARK: - Decode Callback

    /// VTDecompressionSession output callback (C function pointer).
    private static let decompressionCallback: VTDecompressionOutputCallback = {
        (refCon, _, status, _, imageBuffer, _, _) in

        guard status == noErr, let imageBuffer else { return }

        let source = Unmanaged<TelloFrameSource>.fromOpaque(refCon!).takeUnretainedValue()
        source.handleDecodedPixelBuffer(imageBuffer)
    }

    /// Convert a decoded CVPixelBuffer to CGImage and deliver.
    private func handleDecodedPixelBuffer(_ pixelBuffer: CVPixelBuffer) {
        let ciImage = CIImage(cvPixelBuffer: pixelBuffer)
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)

        guard let cgImage = ciContext.createCGImage(ciImage, from: CGRect(x: 0, y: 0, width: width, height: height)) else {
            Self.logger.warning("Failed to create CGImage from pixel buffer")
            return
        }

        handleDecodedFrame(cgImage)
    }

    // MARK: - Frozen Frame Detection

    /// Check for frozen frames, update latestFrame, and invoke callback.
    private func handleDecodedFrame(_ image: CGImage) {
        let hash = computeFrameHash(image)

        var frozen = false
        lock.lock()
        if hash == lastFrameHash {
            frozenFrameCount += 1
            if frozenFrameCount >= frozenFrameThreshold {
                frozen = true
                frozenFrameCount = 0
            }
        } else {
            frozenFrameCount = 0
        }
        lastFrameHash = hash
        latestFrame = image
        let callback = frameCallback
        lock.unlock()

        if frozen {
            Self.logger.warning("Frozen frame detected")
            onStreamFrozen?()
            return
        }

        callback?(image)
    }

    /// Hash the center 8x8 pixel block of a frame for frozen-frame detection.
    ///
    /// Mirrors the Android `computeFrameHash` and Python `tello.py` approach.
    private func computeFrameHash(_ image: CGImage) -> Int64 {
        let blockSize = 8
        let width = image.width
        let height = image.height
        let cx = width / 2
        let cy = height / 2
        let left = max(cx - blockSize / 2, 0)
        let top = max(cy - blockSize / 2, 0)
        let right = min(left + blockSize, width)
        let bottom = min(top + blockSize, height)

        let cropRect = CGRect(x: left, y: top, width: right - left, height: bottom - top)
        guard let cropped = image.cropping(to: cropRect) else { return 0 }

        let cropW = cropped.width
        let cropH = cropped.height
        let bytesPerPixel = 4
        let bytesPerRow = cropW * bytesPerPixel
        let totalBytes = bytesPerRow * cropH

        var pixelData = [UInt8](repeating: 0, count: totalBytes)

        guard let context = CGContext(
            data: &pixelData,
            width: cropW,
            height: cropH,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return 0 }

        context.draw(cropped, in: CGRect(x: 0, y: 0, width: cropW, height: cropH))

        var hash: Int64 = 17
        for i in stride(from: 0, to: totalBytes, by: bytesPerPixel) {
            // Combine R, G, B into a single value (skip alpha)
            let pixel = Int64(pixelData[i]) << 16 | Int64(pixelData[i + 1]) << 8 | Int64(pixelData[i + 2])
            hash = hash &* 31 &+ pixel
        }
        return hash
    }
}
