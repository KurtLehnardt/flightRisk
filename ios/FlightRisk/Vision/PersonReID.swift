import CoreGraphics
import CoreML
import Vision
import os

/// Person re-identification using CLIP visual embeddings via CoreML.
///
/// Port of Android `PersonReID.kt`. Loads a CLIP ViT-B/32 CoreML model,
/// extracts 512-d appearance embeddings from person crops, and compares
/// them against a target reference photo via cosine similarity.
///
/// Preprocessing follows the standard CLIP pipeline:
/// 1. Resize shorter side to 256 maintaining aspect ratio
/// 2. Center crop to 224x224
/// 3. Normalize with CLIP means/stds
/// 4. NCHW layout as MLMultiArray input
/// 5. L2-normalize the output embedding
final class PersonReID {

    private let logger = Logger(subsystem: "com.flightrisk", category: "reid")
    private var mlModel: MLModel?
    private var model: VNCoreMLModel?
    private var targetEmbedding: [Float]?
    private let threshold: Float

    private static let inputSize = 224
    private static let resizeSize = 256

    /// CLIP normalization constants (ImageNet-derived).
    private static let mean: [Float] = [0.48145466, 0.4578275, 0.40821073]
    private static let std: [Float] = [0.26862954, 0.26130258, 0.27577711]

    /// Whether a target embedding is currently set.
    var hasTarget: Bool { targetEmbedding != nil }

    /// Create a PersonReID instance.
    ///
    /// - Parameter threshold: Cosine similarity threshold for a positive match.
    init(threshold: Float = 0.55) {
        self.threshold = threshold
    }

    // MARK: - Model Loading

    /// Load the CLIPVisual CoreML model.
    ///
    /// - Returns: `true` if the model loaded successfully.
    func loadModel() -> Bool {
        do {
            let config = MLModelConfiguration()
            config.computeUnits = .all

            guard let modelURL = Bundle.main.url(forResource: "CLIPVisual", withExtension: "mlmodelc") else {
                logger.error("CLIPVisual.mlmodelc not found in bundle")
                return false
            }

            let loaded = try MLModel(contentsOf: modelURL, configuration: config)
            mlModel = loaded
            model = try VNCoreMLModel(for: loaded)
            logger.info("CLIPVisual model loaded successfully")
            return true
        } catch {
            logger.error("Failed to load CLIPVisual model: \(error.localizedDescription)")
            return false
        }
    }

    // MARK: - Target Management

    /// Set the reference image of the person to find.
    ///
    /// Computes and stores the target embedding for later comparison.
    ///
    /// - Parameter photo: CGImage of the target person.
    /// - Returns: `true` if the embedding was successfully extracted.
    func setTarget(photo: CGImage) -> Bool {
        guard let embedding = extractEmbedding(from: photo) else {
            logger.warning("Failed to extract target embedding")
            return false
        }
        targetEmbedding = embedding
        logger.debug("Target embedding set (\(embedding.count)-d)")
        return true
    }

    /// Set the target embedding directly (e.g. restored from TargetStore).
    ///
    /// - Parameter embedding: Pre-computed L2-normalized CLIP embedding.
    func setTargetEmbedding(_ embedding: [Float]) {
        targetEmbedding = embedding
        logger.debug("Target embedding set from stored data (\(embedding.count)-d)")
    }

    /// Clear the current target embedding.
    func clearTarget() {
        targetEmbedding = nil
    }

    // MARK: - Comparison

    /// Compare a detected person crop against the target.
    ///
    /// - Parameter crop: CGImage of a detected person.
    /// - Returns: Cosine similarity score (0-1). Returns 0.0 if no target
    ///   is set or embedding extraction fails.
    func compare(crop: CGImage) -> Float {
        guard let target = targetEmbedding else { return 0.0 }
        guard let embedding = extractEmbedding(from: crop) else { return 0.0 }
        let similarity = dotProduct(target, embedding)
        return max(0.0, similarity)
    }

    /// Find the best match among detected persons.
    ///
    /// - Parameter detections: Array of `Detection` from `PersonDetector`.
    /// - Returns: Tuple of (best matching index or nil, best score).
    ///   Index is nil if no detection exceeds the threshold.
    func findMatch(detections: [Detection]) -> (index: Int?, score: Float) {
        guard targetEmbedding != nil, !detections.isEmpty else {
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

    /// Extract the raw appearance embedding for a person crop.
    ///
    /// Unlike `compare`, this does not require a target to be set.
    /// Used by callers that need the raw feature vector (e.g. for
    /// TargetStore persistence).
    ///
    /// - Parameter crop: CGImage of a detected person.
    /// - Returns: L2-normalized 512-d embedding, or nil if extraction fails.
    func extractEmbeddingSafe(crop: CGImage) -> [Float]? {
        do {
            return try extractEmbeddingThrowing(from: crop)
        } catch {
            logger.warning("ReID embedding extraction failed: \(error.localizedDescription)")
            return nil
        }
    }

    // MARK: - Private: Embedding Extraction

    /// Extract and L2-normalize a CLIP embedding from an image.
    private func extractEmbedding(from image: CGImage) -> [Float]? {
        return try? extractEmbeddingThrowing(from: image)
    }

    /// Throwing variant of embedding extraction for error propagation.
    private func extractEmbeddingThrowing(from image: CGImage) throws -> [Float] {
        guard let cachedModel = mlModel else {
            throw ReIDError.modelNotLoaded
        }

        // Preprocess: resize + center crop + normalize -> MLMultiArray
        let inputArray = try preprocess(image: image)

        // Run inference via cached CoreML model
        let inputFeature = try MLDictionaryFeatureProvider(
            dictionary: ["input": MLFeatureValue(multiArray: inputArray)]
        )
        let prediction = try cachedModel.prediction(from: inputFeature)

        // Extract the output embedding
        guard let outputFeature = prediction.featureNames.first,
              let outputValue = prediction.featureValue(for: outputFeature),
              let outputArray = outputValue.multiArrayValue else {
            throw ReIDError.invalidOutput
        }

        // Convert MLMultiArray to [Float] and L2-normalize
        guard outputArray.dataType == .float32 else {
            logger.warning("Unexpected MLMultiArray dataType: \(outputArray.dataType.rawValue), expected Float32")
            throw ReIDError.modelNotLoaded
        }
        let count = outputArray.count
        var embedding = [Float](repeating: 0, count: count)
        let ptr = outputArray.dataPointer.bindMemory(to: Float.self, capacity: count)
        for i in 0..<count {
            embedding[i] = ptr[i]
        }

        return l2Normalize(embedding)
    }

    // MARK: - Private: Preprocessing

    /// CLIP preprocessing: resize shorter side to 256, center crop 224x224,
    /// normalize with CLIP means/stds, produce NCHW MLMultiArray.
    private func preprocess(image: CGImage) throws -> MLMultiArray {
        let srcW = image.width
        let srcH = image.height

        // Resize shorter side to 256, maintaining aspect ratio
        let resizeW: Int
        let resizeH: Int
        if srcW < srcH {
            resizeW = Self.resizeSize
            resizeH = Int(Float(Self.resizeSize) * Float(srcH) / Float(srcW))
        } else {
            resizeW = Int(Float(Self.resizeSize) * Float(srcW) / Float(srcH))
            resizeH = Self.resizeSize
        }

        // Render resized image into a bitmap context
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
            throw ReIDError.preprocessingFailed
        }

        guard let resizedContext = CGContext(
            data: nil,
            width: resizeW,
            height: resizeH,
            bitsPerComponent: 8,
            bytesPerRow: resizeW * 4,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            throw ReIDError.preprocessingFailed
        }

        resizedContext.interpolationQuality = .high
        resizedContext.draw(image, in: CGRect(x: 0, y: 0, width: resizeW, height: resizeH))

        guard let resizedImage = resizedContext.makeImage() else {
            throw ReIDError.preprocessingFailed
        }

        // Center crop to 224x224
        let cropX = (resizeW - Self.inputSize) / 2
        let cropY = (resizeH - Self.inputSize) / 2
        let cropRect = CGRect(x: cropX, y: cropY, width: Self.inputSize, height: Self.inputSize)

        guard let croppedImage = resizedImage.cropping(to: cropRect) else {
            throw ReIDError.preprocessingFailed
        }

        // Extract pixel data from cropped image
        guard let croppedContext = CGContext(
            data: nil,
            width: Self.inputSize,
            height: Self.inputSize,
            bitsPerComponent: 8,
            bytesPerRow: Self.inputSize * 4,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            throw ReIDError.preprocessingFailed
        }

        croppedContext.draw(croppedImage, in: CGRect(x: 0, y: 0, width: Self.inputSize, height: Self.inputSize))

        guard let pixelData = croppedContext.data else {
            throw ReIDError.preprocessingFailed
        }

        let pixels = pixelData.bindMemory(to: UInt8.self, capacity: Self.inputSize * Self.inputSize * 4)

        // Build NCHW MLMultiArray: [1, 3, 224, 224]
        let inputArray = try MLMultiArray(shape: [1, 3, NSNumber(value: Self.inputSize), NSNumber(value: Self.inputSize)], dataType: .float32)
        let channelSize = Self.inputSize * Self.inputSize

        for i in 0..<channelSize {
            let pixelOffset = i * 4  // RGBA, 4 bytes per pixel
            let r = Float(pixels[pixelOffset]) / 255.0
            let g = Float(pixels[pixelOffset + 1]) / 255.0
            let b = Float(pixels[pixelOffset + 2]) / 255.0

            // NCHW: channel 0 = R, channel 1 = G, channel 2 = B
            inputArray[i] = NSNumber(value: (r - Self.mean[0]) / Self.std[0])
            inputArray[channelSize + i] = NSNumber(value: (g - Self.mean[1]) / Self.std[1])
            inputArray[2 * channelSize + i] = NSNumber(value: (b - Self.mean[2]) / Self.std[2])
        }

        return inputArray
    }

    // MARK: - Private: Math Utilities

    /// L2-normalize a vector.
    private func l2Normalize(_ vec: [Float]) -> [Float] {
        var sumSq: Float = 0.0
        for v in vec { sumSq += v * v }
        let norm = sqrtf(sumSq)
        guard norm > 0 else { return vec }
        return vec.map { $0 / norm }
    }

    /// Dot product of two vectors (cosine similarity for unit vectors).
    private func dotProduct(_ a: [Float], _ b: [Float]) -> Float {
        guard a.count == b.count else { return 0.0 }
        var sum: Float = 0.0
        for i in a.indices { sum += a[i] * b[i] }
        return sum
    }

    // MARK: - Errors

    private enum ReIDError: Error, LocalizedError {
        case modelNotLoaded
        case preprocessingFailed
        case invalidOutput

        var errorDescription: String? {
            switch self {
            case .modelNotLoaded: return "CLIPVisual model not loaded"
            case .preprocessingFailed: return "Image preprocessing failed"
            case .invalidOutput: return "Model produced invalid output"
            }
        }
    }
}
