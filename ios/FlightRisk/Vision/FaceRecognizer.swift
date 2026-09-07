import CoreML
import Vision
import CoreGraphics
import os

/// Face recognition using SCRFD-500M detection + ArcFace MobileFaceNet embeddings via CoreML.
///
/// Port of `FaceRecognizer.kt` (Android). Uses a lightweight face detector
/// (SCRFD-500M) for face detection, then ArcFace (MobileFaceNet) for 512-d
/// face embedding extraction. Complements full-body ReID for higher confidence
/// matching, especially when clothing changes.
///
/// Both models are loaded from bundled `.mlmodelc` packages.
final class FaceRecognizer {

    private let logger = Logger(subsystem: "com.flightrisk", category: "face")

    private var scrfdModel: VNCoreMLModel?
    private var arcfaceModel: VNCoreMLModel?
    private var targetEmbedding: [Float]?
    private let threshold: Float

    private let detSize: (Int, Int)

    private static let arcfaceInputSize = 112
    private static let faceDetConfThreshold: Float = 0.5

    /// Whether a target face embedding is currently set.
    var hasTarget: Bool { targetEmbedding != nil }

    /// - Parameters:
    ///   - threshold: Cosine similarity threshold for a face match.
    ///   - detSize: Face detector input resolution (width, height).
    init(threshold: Float = 0.45, detSize: (Int, Int) = (640, 640)) {
        self.threshold = threshold
        self.detSize = detSize
    }

    // MARK: - Model Loading

    /// Load SCRFD and ArcFace CoreML models from the app bundle.
    ///
    /// - Returns: `true` if both models loaded successfully.
    func loadModels() -> Bool {
        scrfdModel = loadCoreMLModel(named: "SCRFDFaceDetector")
        arcfaceModel = loadCoreMLModel(named: "ArcFaceMobile")

        if scrfdModel == nil {
            logger.warning("Failed to load SCRFDFaceDetector model")
        }
        if arcfaceModel == nil {
            logger.warning("Failed to load ArcFaceMobile model")
        }

        let success = scrfdModel != nil && arcfaceModel != nil
        if success {
            logger.debug("Face recognition models loaded")
        }
        return success
    }

    // MARK: - Target Management

    /// Set the reference face from a photo of the target.
    ///
    /// Detects the largest face in the photo, extracts and stores its ArcFace embedding.
    ///
    /// - Parameter photo: Image containing the target's face.
    /// - Returns: `true` if a face was found and embedding set.
    func setTarget(photo: CGImage) -> Bool {
        guard let embedding = bestFaceEmbedding(image: photo) else {
            logger.warning("No face detected in reference photo")
            return false
        }
        targetEmbedding = embedding
        logger.debug("Target face embedding set (\(embedding.count)-d)")
        return true
    }

    /// Set the target embedding directly (e.g. restored from TargetStore).
    ///
    /// - Parameter embedding: Pre-computed 512-d L2-normalized face embedding.
    func setTargetEmbedding(_ embedding: [Float]) {
        targetEmbedding = embedding
        logger.debug("Target face embedding set from stored data (\(embedding.count)-d)")
    }

    /// Clear the current target embedding.
    func clearTarget() {
        targetEmbedding = nil
    }

    // MARK: - Comparison

    /// Compare a detected person crop's face against the target.
    ///
    /// - Parameter crop: Image of a detected person.
    /// - Returns: Cosine similarity 0-1, or 0.0 if no face found or no target set.
    func compare(crop: CGImage) -> Float {
        guard let target = targetEmbedding else { return 0.0 }

        guard let embedding = bestFaceEmbedding(image: crop) else { return 0.0 }

        let similarity = dotProduct(target, embedding)
        return max(0, similarity) // clamp to 0-1
    }

    /// Find the best face match among detected persons.
    ///
    /// - Parameter detections: List of ``Detection`` from the person detector.
    /// - Returns: Tuple of (best matching index or nil, best score).
    func findMatch(detections: [Detection]) -> (index: Int?, score: Float) {
        guard let _ = targetEmbedding, !detections.isEmpty else {
            return (nil, 0.0)
        }

        var bestIdx: Int?
        var bestScore: Float = 0.0

        for (i, det) in detections.enumerated() {
            let score = compare(crop: det.crop)
            if score > bestScore {
                bestScore = score
                bestIdx = i
            }
        }

        if bestScore >= threshold {
            return (bestIdx, bestScore)
        }
        return (nil, bestScore)
    }

    /// Extract the raw face embedding for a person crop.
    ///
    /// Used by ``TargetStore`` for persistence.
    ///
    /// - Parameter crop: Image of a detected person.
    /// - Returns: Normalized 512-d face embedding, or nil if no face found.
    func extractEmbedding(crop: CGImage) -> [Float]? {
        return bestFaceEmbedding(image: crop)
    }

    // MARK: - Pipeline Internals

    /// Detect faces in the image, pick the largest, and return its ArcFace embedding.
    ///
    /// Pipeline: face detection (SCRFD) -> crop largest face -> resize to 112x112
    /// -> ArcFace normalization -> CoreML inference -> L2-normalize.
    private func bestFaceEmbedding(image: CGImage) -> [Float]? {
        let faces = detectFaces(image: image)
        guard !faces.isEmpty else { return nil }

        // Pick the largest face by bounding box area
        guard let best = faces.max(by: { area($0) < area($1) }) else { return nil }

        // Crop face from original image
        let x1 = max(best.x1, 0)
        let y1 = max(best.y1, 0)
        let x2 = min(best.x2, image.width)
        let y2 = min(best.y2, image.height)
        let w = x2 - x1
        let h = y2 - y1
        guard w > 0, h > 0 else { return nil }

        let cropRect = CGRect(x: x1, y: y1, width: w, height: h)
        guard let faceCrop = image.cropping(to: cropRect) else { return nil }

        // Resize to 112x112 for ArcFace
        let size = Self.arcfaceInputSize
        guard let aligned = resizeImage(faceCrop, to: CGSize(width: size, height: size)) else {
            return nil
        }

        // Extract embedding and L2-normalize
        guard let raw = arcfaceEmbedding(alignedFace: aligned) else { return nil }
        return l2Normalize(raw)
    }

    // MARK: - Face Detection (SCRFD)

    /// Bounding box for a detected face in pixel coordinates.
    private struct FaceBox {
        let x1: Int
        let y1: Int
        let x2: Int
        let y2: Int
    }

    /// Run SCRFD face detection on the image.
    ///
    /// - Parameter image: Source image.
    /// - Returns: Face bounding boxes in original image coordinates.
    private func detectFaces(image: CGImage) -> [FaceBox] {
        guard let model = scrfdModel else { return [] }

        let detW = detSize.0
        let detH = detSize.1
        let scaleX = Float(image.width) / Float(detW)
        let scaleY = Float(image.height) / Float(detH)

        // Resize image to detector input size
        guard let resized = resizeImage(image, to: CGSize(width: detW, height: detH)) else {
            return []
        }

        // Build pixel buffer with SCRFD normalization: (pixel - 127.5) / 128
        guard let pixelBuffer = createPixelBuffer(
            from: resized,
            width: detW,
            height: detH,
            mean: 127.5,
            scale: 128.0
        ) else {
            logger.warning("Failed to create SCRFD pixel buffer")
            return []
        }

        // Run inference
        let request = VNCoreMLRequest(model: model)
        request.imageCropAndScaleOption = .scaleFill

        let handler = VNImageRequestHandler(cvPixelBuffer: pixelBuffer, options: [:])
        do {
            try handler.perform([request])
        } catch {
            logger.warning("SCRFD inference failed: \(error.localizedDescription)")
            return []
        }

        // Parse results
        var faces: [FaceBox] = []

        if let results = request.results as? [VNCoreMLFeatureValueObservation] {
            // SCRFD outputs multi-array with bounding boxes and scores
            faces = parseSCRFDOutput(results, scaleX: scaleX, scaleY: scaleY,
                                     imgWidth: image.width, imgHeight: image.height)
        } else if let results = request.results as? [VNRecognizedObjectObservation] {
            // If the model outputs recognized objects directly
            for obs in results where obs.confidence >= Self.faceDetConfThreshold {
                let box = obs.boundingBox
                // Vision coordinates are normalized bottom-left origin
                let x1 = Int(box.origin.x * CGFloat(image.width))
                let y1 = Int((1.0 - box.origin.y - box.height) * CGFloat(image.height))
                let x2 = Int((box.origin.x + box.width) * CGFloat(image.width))
                let y2 = Int((1.0 - box.origin.y) * CGFloat(image.height))
                if x2 > x1 && y2 > y1 {
                    faces.append(FaceBox(
                        x1: max(x1, 0), y1: max(y1, 0),
                        x2: min(x2, image.width), y2: min(y2, image.height)
                    ))
                }
            }
        }

        return faces
    }

    /// Parse SCRFD multi-array output into face bounding boxes.
    private func parseSCRFDOutput(
        _ observations: [VNCoreMLFeatureValueObservation],
        scaleX: Float, scaleY: Float,
        imgWidth: Int, imgHeight: Int
    ) -> [FaceBox] {
        var faces: [FaceBox] = []

        // SCRFD outputs: look for the multi-array containing [N, 5+] detections
        // where each row is [x1, y1, x2, y2, score, ...]
        for obs in observations {
            guard let multiArray = obs.featureValue.multiArrayValue else { continue }
            let shape = multiArray.shape.map { $0.intValue }

            // Expect shape [1, N, 5+] or [N, 5+]
            let rows: Int
            let cols: Int
            let batchOffset: Int

            if shape.count == 3 && shape[2] >= 5 {
                rows = shape[1]
                cols = shape[2]
                batchOffset = 0
            } else if shape.count == 2 && shape[1] >= 5 {
                rows = shape[0]
                cols = shape[1]
                batchOffset = 0
            } else {
                continue
            }

            let ptr = multiArray.dataPointer.bindMemory(to: Float.self, capacity: rows * cols)
            for r in 0..<rows {
                let base = (batchOffset * rows + r) * cols
                let score = ptr[base + 4]
                guard score >= Self.faceDetConfThreshold else { continue }

                let x1 = Int((ptr[base + 0] * scaleX).rounded()).clamped(to: 0...imgWidth)
                let y1 = Int((ptr[base + 1] * scaleY).rounded()).clamped(to: 0...imgHeight)
                let x2 = Int((ptr[base + 2] * scaleX).rounded()).clamped(to: 0...imgWidth)
                let y2 = Int((ptr[base + 3] * scaleY).rounded()).clamped(to: 0...imgHeight)

                if x2 > x1 && y2 > y1 {
                    faces.append(FaceBox(x1: x1, y1: y1, x2: x2, y2: y2))
                }
            }
        }

        return faces
    }

    // MARK: - ArcFace Embedding

    /// Extract ArcFace embedding from a 112x112 aligned face image.
    private func arcfaceEmbedding(alignedFace: CGImage) -> [Float]? {
        guard let model = arcfaceModel else { return nil }

        let size = Self.arcfaceInputSize

        // Build pixel buffer with ArcFace normalization: (pixel - 127.5) / 127.5 -> [-1, 1]
        guard let pixelBuffer = createPixelBuffer(
            from: alignedFace,
            width: size,
            height: size,
            mean: 127.5,
            scale: 127.5
        ) else {
            logger.warning("Failed to create ArcFace pixel buffer")
            return nil
        }

        let request = VNCoreMLRequest(model: model)
        request.imageCropAndScaleOption = .scaleFill

        let handler = VNImageRequestHandler(cvPixelBuffer: pixelBuffer, options: [:])
        do {
            try handler.perform([request])
        } catch {
            logger.warning("ArcFace inference failed: \(error.localizedDescription)")
            return nil
        }

        // Extract embedding from output
        guard let results = request.results as? [VNCoreMLFeatureValueObservation],
              let firstResult = results.first,
              let multiArray = firstResult.featureValue.multiArrayValue else {
            logger.warning("ArcFace produced no output")
            return nil
        }

        let count = multiArray.count
        let ptr = multiArray.dataPointer.bindMemory(to: Float.self, capacity: count)
        var embedding = [Float](repeating: 0, count: count)
        for i in 0..<count {
            embedding[i] = ptr[i]
        }

        return embedding
    }

    // MARK: - Image Utilities

    /// Resize a CGImage to the given size using CoreGraphics.
    private func resizeImage(_ image: CGImage, to size: CGSize) -> CGImage? {
        let width = Int(size.width)
        let height = Int(size.height)

        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else {
            return nil
        }

        context.interpolationQuality = .high
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return context.makeImage()
    }

    /// Create a CVPixelBuffer from a CGImage with the given normalization.
    ///
    /// Normalizes each pixel channel as `(value - mean) / scale`.
    ///
    /// - Parameters:
    ///   - image: Source image (should already be resized to target dimensions).
    ///   - width: Target width in pixels.
    ///   - height: Target height in pixels.
    ///   - mean: Subtracted from each pixel channel value.
    ///   - scale: Divisor applied after mean subtraction.
    /// - Returns: A pixel buffer suitable for CoreML inference, or nil on failure.
    private func createPixelBuffer(
        from image: CGImage,
        width: Int,
        height: Int,
        mean: Float,
        scale: Float
    ) -> CVPixelBuffer? {
        // Draw image into a bitmap context to get raw pixel data
        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else {
            return nil
        }

        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        guard let data = context.data else { return nil }

        let pixelData = data.bindMemory(to: UInt8.self, capacity: width * height * 4)

        // Create pixel buffer
        var pixelBuffer: CVPixelBuffer?
        let status = CVPixelBufferCreate(
            kCFAllocatorDefault,
            width, height,
            kCVPixelFormatType_32BGRA,
            nil,
            &pixelBuffer
        )
        guard status == kCVReturnSuccess, let buffer = pixelBuffer else { return nil }

        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }

        guard let baseAddress = CVPixelBufferGetBaseAddress(buffer) else { return nil }
        let bytesPerRow = CVPixelBufferGetBytesPerRow(buffer)
        let destData = baseAddress.bindMemory(to: UInt8.self, capacity: bytesPerRow * height)

        // Copy pixel data (RGBA from context -> BGRA for CVPixelBuffer)
        // Use bytesPerRow for destination stride since hardware may add padding
        for y in 0..<height {
            let destRowStart = y * bytesPerRow
            let srcRowStart = y * width * 4
            for x in 0..<width {
                let srcOffset = srcRowStart + x * 4
                let destOffset = destRowStart + x * 4
                let r = pixelData[srcOffset]
                let g = pixelData[srcOffset + 1]
                let b = pixelData[srcOffset + 2]

                destData[destOffset] = b     // B
                destData[destOffset + 1] = g // G
                destData[destOffset + 2] = r // R
                destData[destOffset + 3] = 255 // A
            }
        }

        return buffer
    }

    // MARK: - Model Loading Helper

    /// Load a CoreML model by name from the app bundle.
    private func loadCoreMLModel(named name: String) -> VNCoreMLModel? {
        guard let url = Bundle.main.url(forResource: name, withExtension: "mlmodelc") else {
            logger.warning("CoreML model not found: \(name).mlmodelc")
            return nil
        }

        do {
            let config = MLModelConfiguration()
            config.computeUnits = .all // Use Neural Engine when available
            let mlModel = try MLModel(contentsOf: url, configuration: config)
            return try VNCoreMLModel(for: mlModel)
        } catch {
            logger.warning("Failed to load CoreML model \(name): \(error.localizedDescription)")
            return nil
        }
    }

    // MARK: - Math Utilities

    /// L2-normalize a vector.
    private func l2Normalize(_ vec: [Float]) -> [Float] {
        var sumSq: Float = 0
        for v in vec { sumSq += v * v }
        let norm = sqrtf(sumSq)
        guard norm > 0 else { return vec }
        return vec.map { $0 / norm }
    }

    /// Dot product of two vectors (cosine similarity for unit vectors).
    private func dotProduct(_ a: [Float], _ b: [Float]) -> Float {
        var sum: Float = 0
        let count = min(a.count, b.count)
        for i in 0..<count {
            sum += a[i] * b[i]
        }
        return sum
    }

    /// Area of a face bounding box.
    private func area(_ box: FaceBox) -> Int {
        (box.x2 - box.x1) * (box.y2 - box.y1)
    }
}

// MARK: - Int Clamping

private extension Int {
    func clamped(to range: ClosedRange<Int>) -> Int {
        Swift.min(Swift.max(self, range.lowerBound), range.upperBound)
    }
}
