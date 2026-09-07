import CoreGraphics

/// Result of an LLM reasoning call.
///
/// The structured format mirrors the Python agent's parsed response:
/// `MATCH: yes/no`, `CONFIDENCE: high/medium/low`, `REASONING: ...`.
struct ReasoningResult {
    /// Whether the LLM believes the candidate matches.
    let isMatch: Bool
    /// Confidence level: "high", "medium", "low", "unavailable", "error", or "unknown".
    let confidence: String
    /// Free-text explanation from the LLM.
    let reasoning: String
}

/// Interface for LLM reasoning backends.
///
/// Ports the Python `FlightRiskAgent` abstraction to a pluggable backend
/// system. On mobile, the primary implementation is ``CloudClaudeLlmBackend``
/// (Anthropic Messages API). When the device is offline (e.g. on Tello WiFi),
/// the system falls back to ``NoOpLlmBackend``.
protocol LlmBackend {
    /// Human-readable backend name (e.g. "claude", "none").
    var name: String { get }

    /// Whether this backend can currently serve requests.
    var isAvailable: Bool { get }

    /// Compare a reference image of the missing person against a candidate
    /// detection from the drone camera.
    ///
    /// Mirrors `FlightRiskAgent.analyze_match()` from the Python codebase.
    ///
    /// - Parameters:
    ///   - referenceImage: The target/reference photo.
    ///   - candidateImage: A cropped detection from the drone feed.
    ///   - description: Optional text description to augment the comparison.
    /// - Returns: ``ReasoningResult`` with match verdict, confidence, and reasoning.
    func analyzeMatch(
        referenceImage: CGImage,
        candidateImage: CGImage,
        description: String?
    ) async -> ReasoningResult

    /// Check whether a candidate detection matches a text description of
    /// the missing person (used when no reference photo is available).
    ///
    /// Mirrors `FlightRiskAgent.match_description()` from the Python codebase.
    ///
    /// - Parameters:
    ///   - candidateImage: A cropped detection from the drone feed.
    ///   - description: Text description of the person to find.
    /// - Returns: ``ReasoningResult`` with match verdict, confidence, and reasoning.
    func describeMatch(
        candidateImage: CGImage,
        description: String
    ) async -> ReasoningResult
}
