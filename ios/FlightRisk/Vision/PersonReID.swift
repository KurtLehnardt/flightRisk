import CoreGraphics
import CoreML
import Foundation
import os
import PersonVision

/// Appearance matching: the image tower of open_clip ViT-B-16 `laion2b_s34b_b88k` (MIT), the
/// ground's own model, converted and pinned by `scripts/mobile_models/make_models.py`
/// (`CLIPViTB16LAION.mlpackage`, 8-bit palettised by default).
///
/// The preprocessing is the ground's, bit for bit (`PersonVision.CLIPPreprocess`): shortest side
/// to 224 with Pillow's bicubic filter, the centre 224 x 224, `(v / 255 - mean) / std` with the
/// CLIP mean and standard deviation, RGB. The model returns a 512-value L2-normalised embedding.
/// The phone only compares embeddings it made itself (the target's and the detections'), so the
/// palettised model's small shared offset from PyTorch does not matter here.
///
/// Appearance only: no face is detected, recognised or compared.
final class PersonReID {

    private let logger = Logger(subsystem: "com.flightrisk", category: "reid")
    private let modelName: String
    private let bundle: Bundle
    private let lock = NSLock()
    private var model: MLModel?
    private var targetEmbedding: [Float]?
    private let threshold: Float

    /// Whether a target embedding is currently set.
    var hasTarget: Bool {
        lock.lock()
        defer { lock.unlock() }
        return targetEmbedding != nil
    }

    /// Whether the embedder model is loaded.
    var isAvailable: Bool {
        lock.lock()
        defer { lock.unlock() }
        return model != nil
    }

    /// The match threshold (cosine similarity).
    var matchThreshold: Float { threshold }

    /// - Parameters:
    ///   - threshold: Cosine similarity at or above which a detection matches.
    ///   - modelName: The compiled bundled model, without `.mlmodelc`.
    ///   - bundle: Where it is.
    init(threshold: Float = 0.55, modelName: String = CLIPContract.modelName, bundle: Bundle = .main) {
        self.threshold = threshold
        self.modelName = modelName
        self.bundle = bundle
    }

    // MARK: - Model Loading

    /// Load the embedder from the app bundle.
    ///
    /// - Returns: `true` when the model is loaded.
    @discardableResult
    func loadModel() -> Bool {
        guard let url = BundledModels.url(modelName, in: bundle) else {
            logger.warning("No appearance embedder model in this build; matching is off")
            return false
        }
        do {
            let config = MLModelConfiguration()
            config.computeUnits = BundledModels.computeUnits(for: "\(modelName).mlpackage", in: bundle)
            let loaded = try MLModel(contentsOf: url, configuration: config)
            lock.lock()
            model = loaded
            lock.unlock()
            return true
        } catch {
            logger.error("The appearance embedder model could not be loaded")
            return false
        }
    }

    // MARK: - Target Management

    /// Set the reference photo of the person to find. Like the ground
    /// (`flightrisk/vision/reid.py`), the whole photo is embedded.
    ///
    /// - Returns: `true` if the embedding was made.
    func setTarget(photo: CGImage) -> Bool {
        guard let embedding = extractEmbeddingSafe(crop: photo) else {
            logger.warning("Failed to extract the target embedding")
            return false
        }
        setTargetEmbedding(embedding)
        return true
    }

    /// Set the target embedding directly (for example one computed elsewhere).
    func setTargetEmbedding(_ embedding: [Float]) {
        lock.lock()
        targetEmbedding = Embedding.l2Normalized(embedding)
        lock.unlock()
    }

    /// Clear the current target embedding.
    func clearTarget() {
        lock.lock()
        targetEmbedding = nil
        lock.unlock()
    }

    // MARK: - Comparison

    /// Cosine similarity of a person crop with the target, clamped at 0; 0 without a target or
    /// when the crop cannot be embedded.
    func compare(crop: CGImage) -> Float {
        lock.lock()
        let target = targetEmbedding
        lock.unlock()
        guard let target, let embedding = extractEmbeddingSafe(crop: crop) else { return 0 }
        return max(0, Embedding.dot(target, embedding))
    }

    /// The best match among detections.
    ///
    /// - Returns: The best index (or `nil` when no detection reaches the threshold) and its score.
    func findMatch(detections: [Detection]) -> (index: Int?, score: Float) {
        guard hasTarget, !detections.isEmpty else { return (nil, 0) }
        var bestIdx: Int?
        var bestScore: Float = 0
        for (i, det) in detections.enumerated() {
            let score = compare(crop: det.crop)
            if score > bestScore {
                bestScore = score
                bestIdx = i
            }
        }
        return bestScore >= threshold ? (bestIdx, bestScore) : (nil, bestScore)
    }

    /// The L2-normalised 512-value embedding of an image, or `nil` when it cannot be made.
    func extractEmbeddingSafe(crop: CGImage) -> [Float]? {
        guard let rgb = ModelIO.rgb(crop) else { return nil }
        do {
            return try embedding(of: rgb)
        } catch {
            logger.warning("Appearance embedding failed")
            return nil
        }
    }

    /// The embedding of an RGB image, with the contract's preprocessing.
    func embedding(of image: RGBImage) throws -> [Float] {
        try embedding(input: CLIPPreprocess.input(image))
    }

    /// The embedding of an already preprocessed `[3, 224, 224]` tensor.
    func embedding(input tensor: [Float]) throws -> [Float] {
        lock.lock()
        let loaded = model
        lock.unlock()
        guard let loaded else { throw ReIDError.modelNotLoaded }
        let size = CLIPContract.inputSize
        let array = try ModelIO.multiArray(tensor, channels: 3, height: size, width: size)
        let out = try loaded.prediction(from: MLDictionaryFeatureProvider(
            dictionary: [CLIPContract.inputName: MLFeatureValue(multiArray: array)]
        ))
        guard let value = out.featureValue(for: CLIPContract.outputName)?.multiArrayValue else {
            throw ReIDError.invalidOutput
        }
        let embedding = ModelIO.floats(value)
        guard embedding.count == CLIPContract.embeddingCount, embedding.allSatisfy(\.isFinite) else {
            throw ReIDError.invalidOutput
        }
        return Embedding.l2Normalized(embedding)
    }

    // MARK: - Errors

    enum ReIDError: Error, LocalizedError {
        case modelNotLoaded
        case invalidOutput

        var errorDescription: String? {
            switch self {
            case .modelNotLoaded: return "The appearance embedder model is not loaded"
            case .invalidOutput: return "The appearance embedder produced an invalid output"
            }
        }
    }
}

// MARK: - PipelineReidCallback

extension PersonReID: PipelineReidCallback {}
