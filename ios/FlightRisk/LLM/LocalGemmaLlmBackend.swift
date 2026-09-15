import CoreGraphics
import Foundation
import os

/// Error types for local Gemma model operations.
enum GemmaError: Error, LocalizedError {
    case inferenceTimeout
    case modelNotAvailable
    case insufficientDiskSpace(available: UInt64, required: UInt64)
    case cellularDownloadBlocked

    var errorDescription: String? {
        switch self {
        case .inferenceTimeout:
            return "Local inference timed out after 30 seconds"
        case .modelNotAvailable:
            return "Local Gemma model is not available"
        case .insufficientDiskSpace(let avail, let req):
            return "Insufficient disk space: \(avail / 1_000_000_000)GB available, \(req / 1_000_000_000)GB required"
        case .cellularDownloadBlocked:
            return "Model download blocked on cellular network"
        }
    }
}

/// LLM backend that runs Gemma 2 locally via MLX for on-device
/// visual reasoning.
///
/// Mirrors the ``CloudClaudeLlmBackend`` prompt structure but runs
/// inference entirely on-device. Images are described via
/// ``ImageDescriptionExtractor`` (Vision framework) and compared as
/// text since Gemma 2 is a text-only model.
///
/// A confidence discount of 0.7 is applied to all results because
/// the smaller local model is less reliable than cloud Claude.
final class LocalGemmaLlmBackend: LlmBackend {

    // MARK: - Constants

    private static let confidenceDiscount: Float = 0.7
    private static let inferenceTimeout: TimeInterval = 30
    private static let maxTokens = 256

    // MARK: - Properties

    let name = "local_gemma"

    /// Cached synchronous availability state. Updated via
    /// ``refreshAvailability()`` since ``LlmBackend/isAvailable``
    /// is a synchronous property and the underlying actor state
    /// requires `await`.
    private(set) var isAvailable: Bool = false

    private let modelManager: any GemmaModelManaging
    private let logger = Logger(subsystem: "com.flightrisk.app", category: "LocalGemmaLlmBackend")

    // MARK: - Init

    /// - Parameter modelManager: The model manager actor that owns the
    ///   Gemma model lifecycle.
    init(modelManager: any GemmaModelManaging) {
        self.modelManager = modelManager
        // Kick off initial availability check
        Task { [weak self] in
            await self?.refreshAvailability()
        }
    }

    // MARK: - Availability

    /// Update the cached ``isAvailable`` from the model manager's
    /// actor-isolated state.
    func refreshAvailability() async {
        isAvailable = await modelManager.isReady
    }

    // MARK: - LlmBackend

    func analyzeMatch(
        referenceImage: CGImage,
        candidateImage: CGImage,
        description: String?
    ) async -> ReasoningResult {
        // Auto-load if downloaded but not yet loaded
        if await !modelManager.isReady {
            let currentState = await modelManager.state
            if currentState == .downloaded {
                logger.info("Model downloaded but not loaded — auto-loading")
                do {
                    try await modelManager.loadModel()
                } catch {
                    return errorResult("Auto-load failed: \(error.localizedDescription)")
                }
            } else {
                return errorResult("Model not available (state: \(currentState))")
            }
        }

        // Extract text descriptions from both images
        let refDescription = await ImageDescriptionExtractor.describe(referenceImage)
        let candDescription = await ImageDescriptionExtractor.describe(candidateImage)

        // Build comparison prompt with Gemma turn markers
        var prompt = """
            <start_of_turn>user
            You are helping find a missing person.

            Reference person description:
            \(refDescription)

            Candidate person description:
            \(candDescription)
            """

        if let description, !description.isEmpty {
            prompt += "\nAdditional context: \(description)"
        }

        prompt += """


            Compare these two people. Consider clothing color/type, hair, build, and distinguishing features.

            Respond in this exact format:
            MATCH: yes or no
            CONFIDENCE: high, medium, or low
            REASONING: one sentence explaining why
            <end_of_turn>
            <start_of_turn>model

            """

        return await runInference(prompt: prompt)
    }

    func describeMatch(
        candidateImage: CGImage,
        description: String
    ) async -> ReasoningResult {
        // Auto-load if downloaded but not yet loaded
        if await !modelManager.isReady {
            let currentState = await modelManager.state
            if currentState == .downloaded {
                logger.info("Model downloaded but not loaded — auto-loading")
                do {
                    try await modelManager.loadModel()
                } catch {
                    return errorResult("Auto-load failed: \(error.localizedDescription)")
                }
            } else {
                return errorResult("Model not available (state: \(currentState))")
            }
        }

        // Extract text description of candidate image
        let candDescription = await ImageDescriptionExtractor.describe(candidateImage)

        let prompt = """
            <start_of_turn>user
            You are helping find a missing person.
            The person's description: \(description)

            A drone camera detected a person. Here is a description of the detected person:
            \(candDescription)

            Does this person match the description above?
            Consider: clothing color/type, hair color/style, approximate age, build, backpack/accessories, and any distinguishing features.

            Respond in this exact format:
            MATCH: yes or no
            CONFIDENCE: high, medium, or low
            REASONING: one sentence explaining why
            <end_of_turn>
            <start_of_turn>model

            """

        return await runInference(prompt: prompt)
    }

    // MARK: - Inference

    /// Run model inference with a timeout, parse the result, and
    /// refresh cached availability.
    private func runInference(prompt: String) async -> ReasoningResult {
        do {
            let rawText: String = try await withTimeout(Self.inferenceTimeout) { [modelManager] in
                try await modelManager.generate(prompt: prompt, maxTokens: Self.maxTokens)
            }

            // Strip Gemma turn markers from response
            let cleaned = rawText
                .replacingOccurrences(of: "<end_of_turn>", with: "")
                .replacingOccurrences(of: "<start_of_turn>", with: "")
                .trimmingCharacters(in: .whitespacesAndNewlines)

            await refreshAvailability()

            return ReasoningResult.parse(cleaned, confidenceDiscount: Self.confidenceDiscount)
        } catch {
            logger.error("Local inference failed: \(error.localizedDescription)")
            await refreshAvailability()
            return errorResult("Local inference failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Timeout

    /// Race an async operation against a deadline, cancelling the
    /// slower task.
    private func withTimeout<T>(
        _ seconds: TimeInterval,
        _ operation: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        try await withThrowingTaskGroup(of: T.self) { group in
            group.addTask {
                try await operation()
            }
            group.addTask {
                try await Task.sleep(for: .seconds(seconds))
                throw GemmaError.inferenceTimeout
            }
            let result = try await group.next()!
            group.cancelAll()
            return result
        }
    }

    // MARK: - Helpers

    /// Build a failure ``ReasoningResult`` with the confidence discount
    /// applied.
    private func errorResult(_ message: String) -> ReasoningResult {
        ReasoningResult(
            isMatch: false,
            confidence: "error",
            reasoning: message,
            confidenceDiscount: Self.confidenceDiscount
        )
    }
}
