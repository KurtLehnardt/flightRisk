import CoreGraphics
import CoreML
import os
import Vision

/// OSNet-based person re-identification using CoreML.
///
/// Loads an `OSNetReID.mlmodelc` CoreML model, preprocesses person crops
/// to 256x128 with ImageNet normalization, runs inference to produce 512-d
/// embeddings, and compares via cosine similarity.
///
/// Gracefully handles missing model files — marks itself unavailable
/// and logs a warning rather than crashing.
///
/// Registered as MatchScorer signal `osnetReid`.
final class OSNetReIDMatcher {

    private let logger = Logger(subsystem: "com.flightrisk", category: "osnetReid")

    /// OSNet expects 256x128 input (height x width).
    private static let inputHeight = 256
    private static let inputWidth = 128

    /// ImageNet normalization constants.
    private static let mean: [Float] = [0.485, 0.456, 0.406]
    private static let std: [Float] = [0.229, 0.224, 0.225]

    private var mlModel: MLModel?
    private var targetEmbedding: [Float]?
    private let threshold: Float
    private var inputName: String = "input"

    /// Whether the CoreML model is loaded and ready.
    private(set) var isAvailable: Bool = false

    /// Whether a target embedding is currently set.
    var hasTarget: Bool { targetEmbedding != nil }

    /// Public match threshold.
    var matchThreshold: Float { threshold }

    /// - Parameter threshold: Cosine similarity threshold for a positive match.
    init(threshold: Float = 0.50) {
        self.threshold = threshold
    }

    // MARK: - Model Loading

    /// Load the OSNetReID CoreML model from the app bundle.
    ///
    /// - Returns: `true` if the model loaded successfully.
    func loadModel() -> Bool {
        guard let modelURL = Bundle.main.url(forResource: "OSNetReID", withExtension: "mlmodelc") else {
            logger.warning("OSNetReID.mlmodelc not found in bundle -- signal unavailable")
            isAvailable = false
            return false
        }

        do {
            let config = MLModelConfiguration()
            config.computeUnits = .all
            mlModel = try MLModel(contentsOf: modelURL, configuration: config)
            if let firstInput = mlModel?.modelDescription.inputDescriptionsByName.keys.first {
                inputName = firstInput
            }
            isAvailable = true
            logger.info("OSNetReID model loaded successfully")
            return true
        } catch {
            logger.warning("Failed to load OSNetReID model: \(error.localizedDescription)")
            isAvailable = false
            return false
        }
    }

    // MARK: - Target Management

    /// Set the reference person from a photo.
    ///
    /// - Parameter photo: CGImage of the target person.
    /// - Returns: `true` if the embedding was extracted successfully.
    func setTarget(photo: CGImage) -> Bool {
        guard isAvailable else {
            logger.warning("OSNet model not available; cannot set target")
            return false
        }

        guard let embedding = extractEmbedding(from: photo) else {
            logger.warning("Failed to extract OSNet target embedding")
            return false
        }
        targetEmbedding = embedding
        logger.debug("OSNet target embedding set (\(embedding.count)-d)")
        return true
    }

    /// Set the target embedding directly (restored from storage).
    func setTargetEmbedding(_ embedding: [Float]) {
        targetEmbedding = embedding
    }

    /// Clear the current target embedding.
    func clearTarget() {
        targetEmbedding = nil
    }

    // MARK: - Comparison

    /// Compare a detected person crop against the target.
    ///
    /// - Parameter crop: CGImage of a detected person.
    /// - Returns: Cosine similarity 0-1, or 0.0 if unavailable.
    func compare(crop: CGImage) -> Float {
        guard isAvailable, let target = targetEmbedding else { return 0.0 }
        guard let embedding = extractEmbedding(from: crop) else { return 0.0 }
        let similarity = dotProduct(target, embedding)
        return max(0.0, similarity)
    }

    // MARK: - Embedding Extraction

    /// Extract and L2-normalize an OSNet embedding from an image.
    private func extractEmbedding(from image: CGImage) -> [Float]? {
        guard let model = mlModel else { return nil }

        do {
            let inputArray = try preprocess(image: image)

            let inputFeature = try MLDictionaryFeatureProvider(
                dictionary: [self.inputName: MLFeatureValue(multiArray: inputArray)]
            )
            let prediction = try model.prediction(from: inputFeature)

            guard let outputFeature = prediction.featureNames.first,
                  let outputValue = prediction.featureValue(for: outputFeature),
                  let outputArray = outputValue.multiArrayValue else {
                logger.warning("OSNet model produced no output")
                return nil
            }

            let count = outputArray.count
            var embedding = [Float](repeating: 0, count: count)
            let ptr = outputArray.dataPointer.bindMemory(to: Float.self, capacity: count)
            for i in 0..<count {
                embedding[i] = ptr[i]
            }

            return l2Normalize(embedding)
        } catch {
            logger.warning("OSNet embedding extraction failed: \(error.localizedDescription)")
            return nil
        }
    }

    // MARK: - Preprocessing

    /// Preprocess image for OSNet: resize to 256x128, ImageNet normalize, NCHW layout.
    private func preprocess(image: CGImage) throws -> MLMultiArray {
        let width = Self.inputWidth
        let height = Self.inputHeight

        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(
                  data: nil,
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bytesPerRow: width * 4,
                  space: colorSpace,
                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
              ) else {
            throw OSNetError.preprocessingFailed
        }

        context.interpolationQuality = .high
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))

        guard let pixelData = context.data else {
            throw OSNetError.preprocessingFailed
        }
        let pixels = pixelData.bindMemory(to: UInt8.self, capacity: width * height * 4)

        // Build NCHW MLMultiArray: [1, 3, 256, 128]
        let inputArray = try MLMultiArray(
            shape: [1, 3, NSNumber(value: height), NSNumber(value: width)],
            dataType: .float32
        )
        let channelSize = width * height

        for i in 0..<channelSize {
            let offset = i * 4
            let r = Float(pixels[offset]) / 255.0
            let g = Float(pixels[offset + 1]) / 255.0
            let b = Float(pixels[offset + 2]) / 255.0

            inputArray[i] = NSNumber(value: (r - Self.mean[0]) / Self.std[0])
            inputArray[channelSize + i] = NSNumber(value: (g - Self.mean[1]) / Self.std[1])
            inputArray[2 * channelSize + i] = NSNumber(value: (b - Self.mean[2]) / Self.std[2])
        }

        return inputArray
    }

    // MARK: - Math Utilities

    private func l2Normalize(_ vec: [Float]) -> [Float] {
        var sumSq: Float = 0
        for v in vec { sumSq += v * v }
        let norm = sqrtf(sumSq)
        guard norm > 0 else { return vec }
        return vec.map { $0 / norm }
    }

    private func dotProduct(_ a: [Float], _ b: [Float]) -> Float {
        guard a.count == b.count else { return 0.0 }
        var sum: Float = 0
        for i in a.indices { sum += a[i] * b[i] }
        return sum
    }

    // MARK: - Errors

    private enum OSNetError: Error, LocalizedError {
        case preprocessingFailed

        var errorDescription: String? {
            switch self {
            case .preprocessingFailed: return "OSNet image preprocessing failed"
            }
        }
    }
}
