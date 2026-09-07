import CoreGraphics

/// Fallback LLM backend that returns "unavailable" for every request.
///
/// Used when no real LLM backend is configured or reachable (e.g. the
/// device is on Tello WiFi with no internet, or no API key is set).
/// Always reports ``isAvailable`` as `true` so ``LlmSelector`` has a
/// guaranteed last-resort backend.
final class NoOpLlmBackend: LlmBackend {

    let name: String = "none"

    let isAvailable: Bool = true

    func analyzeMatch(
        referenceImage: CGImage,
        candidateImage: CGImage,
        description: String?
    ) async -> ReasoningResult {
        ReasoningResult(
            isMatch: false,
            confidence: "unavailable",
            reasoning: "LLM reasoning not available"
        )
    }

    func describeMatch(
        candidateImage: CGImage,
        description: String
    ) async -> ReasoningResult {
        ReasoningResult(
            isMatch: false,
            confidence: "unavailable",
            reasoning: "LLM reasoning not available"
        )
    }
}
