import CoreGraphics
import CoreML
import os
import Vision

/// InsightFace R18 face recognition using CoreML.
///
/// Loads an `InsightFaceR18.mlmodelc` CoreML model, preprocesses face crops
/// to 112x112 with [-1, 1] normalization, runs inference to produce 512-d
/// embeddings, and compares via cosine similarity.
///
/// Complements the primary ArcFace pipeline with a second face recognition
/// model for higher confidence when both agree.
///
/// Gracefully handles missing model files — marks itself unavailable
/// and logs a warning rather than crashing.
///
/// Registered as MatchScorer signal `insightFace`.
final class InsightFaceMatcher {

    private let logger = Logger(subsystem: "com.flightrisk", category: "insightFace")

    /// InsightFace R18 expects 112x112 aligned face input.
    private static let inputSize = 112

    private var mlModel: MLModel?
    private var targetEmbedding: [Float]?
    private let threshold: Float

    /// Whether the CoreML model is loaded and ready.
    private(set) var isAvailable: Bool = false

    /// Whether a target face embedding is currently set.
    var hasTarget: Bool { targetEmbedding != nil }

    /// Public match threshold.
    var matchThreshold: Float { threshold }

    /// - Parameter threshold: Cosine similarity threshold for a face match.
    init(threshold: Float = 0.40) {
        self.threshold = threshold
    }

    // MARK: - Model Loading

    /// Load the InsightFace R18 CoreML model from the app bundle.
    ///
    /// - Returns: `true` if the model loaded successfully.
    func loadModel() -> Bool {
        guard let modelURL = Bundle.main.url(forResource: "InsightFaceR18", withExtension: "mlmodelc") else {
            logger.warning("InsightFaceR18.mlmodelc not found in bundle -- signal unavailable")
            isAvailable = false
            return false
        }

        do {
            let config = MLModelConfiguration()
            config.computeUnits = .all
            mlModel = try MLModel(contentsOf: modelURL, configuration: config)
            isAvailable = true
            logger.info("InsightFace R18 model loaded successfully")
            return true
        } catch {
            logger.warning("Failed to load InsightFace R18 model: \(error.localizedDescription)")
            isAvailable = false
            return false
        }
    }

    // MARK: - Target Management

    /// Set the reference face from a photo of the target.
    ///
    /// Detects the face region using Vision, crops and aligns it,
    /// then extracts the InsightFace embedding.
    ///
    /// - Parameter photo: Image containing the target's face.
    /// - Returns: `true` if a face was found and embedding set.
    func setTarget(photo: CGImage) -> Bool {
        guard isAvailable else {
            logger.warning("InsightFace model not available; cannot set target")
            return false
        }

        guard let embedding = extractFaceEmbedding(image: photo) else {
            logger.warning("No face detected or embedding extraction failed for target")
            return false
        }
        targetEmbedding = embedding
        logger.debug("InsightFace target embedding set (\(embedding.count)-d)")
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

    /// Compare a detected person crop's face against the target.
    ///
    /// - Parameter crop: Image of a detected person.
    /// - Returns: Cosine similarity 0-1, or 0.0 if unavailable or no face found.
    func compare(crop: CGImage) -> Float {
        guard isAvailable, let target = targetEmbedding else { return 0.0 }
        guard let embedding = extractFaceEmbedding(image: crop) else { return 0.0 }
        let similarity = dotProduct(target, embedding)
        return max(0.0, similarity)
    }

    // MARK: - Face Detection + Embedding

    /// Detect the best face in an image and extract its InsightFace embedding.
    private func extractFaceEmbedding(image: CGImage) -> [Float]? {
        // Use Vision's built-in face detector to find faces
        let faceRequest = VNDetectFaceRectanglesRequest()
        let handler = VNImageRequestHandler(cgImage: image, options: [:])

        do {
            try handler.perform([faceRequest])
        } catch {
            logger.warning("Vision face detection failed: \(error.localizedDescription)")
            return nil
        }

        guard let faces = faceRequest.results, !faces.isEmpty else {
            return nil
        }

        // Pick the largest face
        let bestFace = faces.max(by: {
            $0.boundingBox.width * $0.boundingBox.height <
            $1.boundingBox.width * $1.boundingBox.height
        })
        guard let face = bestFace else { return nil }

        // Convert Vision normalized coordinates to pixel coordinates
        let bbox = face.boundingBox
        let imgW = CGFloat(image.width)
        let imgH = CGFloat(image.height)

        // Vision uses bottom-left origin, normalized [0,1]
        let x1 = Int(bbox.origin.x * imgW)
        let y1 = Int((1.0 - bbox.origin.y - bbox.height) * imgH)
        let x2 = Int((bbox.origin.x + bbox.width) * imgW)
        let y2 = Int((1.0 - bbox.origin.y) * imgH)

        let cropRect = CGRect(
            x: max(0, x1), y: max(0, y1),
            width: min(x2 - x1, image.width - max(0, x1)),
            height: min(y2 - y1, image.height - max(0, y1))
        )
        guard cropRect.width > 0, cropRect.height > 0,
              let faceCrop = image.cropping(to: cropRect) else {
            return nil
        }

        // Resize to 112x112 and extract embedding
        guard let aligned = resizeFace(faceCrop) else { return nil }
        return insightFaceEmbedding(alignedFace: aligned)
    }

    /// Resize a face crop to 112x112.
    private func resizeFace(_ image: CGImage) -> CGImage? {
        let size = Self.inputSize
        guard let context = CGContext(
            data: nil,
            width: size,
            height: size,
            bitsPerComponent: 8,
            bytesPerRow: size * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else {
            return nil
        }
        context.interpolationQuality = .high
        context.draw(image, in: CGRect(x: 0, y: 0, width: size, height: size))
        return context.makeImage()
    }

    /// Extract InsightFace embedding from a 112x112 aligned face.
    private func insightFaceEmbedding(alignedFace: CGImage) -> [Float]? {
        guard let model = mlModel else { return nil }

        let size = Self.inputSize

        do {
            // Build NCHW MLMultiArray with [-1, 1] normalization
            guard let context = CGContext(
                data: nil,
                width: size,
                height: size,
                bitsPerComponent: 8,
                bytesPerRow: size * 4,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
            ) else {
                return nil
            }
            context.draw(alignedFace, in: CGRect(x: 0, y: 0, width: size, height: size))
            guard let data = context.data else { return nil }
            let pixels = data.bindMemory(to: UInt8.self, capacity: size * size * 4)

            let inputArray = try MLMultiArray(
                shape: [1, 3, NSNumber(value: size), NSNumber(value: size)],
                dataType: .float32
            )
            let channelSize = size * size

            for i in 0..<channelSize {
                let offset = i * 4
                let r = Float(pixels[offset]) / 127.5 - 1.0   // normalize to [-1, 1]
                let g = Float(pixels[offset + 1]) / 127.5 - 1.0
                let b = Float(pixels[offset + 2]) / 127.5 - 1.0

                inputArray[i] = NSNumber(value: r)
                inputArray[channelSize + i] = NSNumber(value: g)
                inputArray[2 * channelSize + i] = NSNumber(value: b)
            }

            // Run inference
            let inputFeature = try MLDictionaryFeatureProvider(
                dictionary: ["input": MLFeatureValue(multiArray: inputArray)]
            )
            let prediction = try model.prediction(from: inputFeature)

            guard let outputFeature = prediction.featureNames.first,
                  let outputValue = prediction.featureValue(for: outputFeature),
                  let outputArray = outputValue.multiArrayValue else {
                logger.warning("InsightFace model produced no output")
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
            logger.warning("InsightFace embedding extraction failed: \(error.localizedDescription)")
            return nil
        }
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
}
