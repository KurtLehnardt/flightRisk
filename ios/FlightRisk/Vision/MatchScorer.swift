import Foundation

/// Multi-signal weighted confidence scorer.
///
/// Port of Android `MatchScorer.kt` / Python `flightrisk/vision/scorer.py`.
/// Combines ReID body similarity, face recognition, and LLM reasoning into
/// a single weighted confidence score. Dynamic weight redistribution when
/// signals are missing ensures available signals always normalize to 1.0.
///
/// New signals (thermal, gait, etc.) can be added via ``registerSignal(name:weight:)``
/// without touching this struct's internals.
struct MatchScorer {
    // MARK: - Built-in signal names

    /// Names reserved for built-in signals. These cannot be used with
    /// ``registerSignal(name:weight:)`` -- doing so would silently shadow
    /// the named-parameter value.
    static let builtinSignals: Set<String> = ["reid", "face", "reasoning"]

    // MARK: - Default weights

    /// Weight for full-body ReID similarity.
    private(set) var reidWeight: Float = 0.35
    /// Weight for face recognition score.
    private(set) var faceWeight: Float = 0.40
    /// Weight for LLM reasoning confidence.
    private(set) var reasoningWeight: Float = 0.25
    /// Combined score threshold for a positive match.
    private(set) var matchThreshold: Float = 0.45

    // MARK: - Signal registry

    /// Signal registry: name -> weight. Built-in signals are seeded here
    /// so they flow through the same weighting/redistribution logic as
    /// anything added via ``registerSignal(name:weight:)``.
    private var signals: [String: Float]

    // MARK: - Init

    init(
        reidWeight: Float = 0.35,
        faceWeight: Float = 0.40,
        reasoningWeight: Float = 0.25,
        matchThreshold: Float = 0.45
    ) {
        self.reidWeight = reidWeight
        self.faceWeight = faceWeight
        self.reasoningWeight = reasoningWeight
        self.matchThreshold = matchThreshold
        self.signals = [
            "reid": reidWeight,
            "face": faceWeight,
            "reasoning": reasoningWeight,
        ]
    }

    // MARK: - Public API

    /// Register a new named signal and its weight for use in ``score(...)``.
    ///
    /// Once registered, pass the signal's numeric score (0-1) via the
    /// `extraSignalScores` parameter keyed by this name.
    ///
    /// Not thread-safe: call only during initialization, before the
    /// scorer is shared across concurrent code paths.
    ///
    /// - Precondition: `name` must not collide with a built-in signal.
    mutating func registerSignal(name: String, weight: Float) {
        precondition(
            !Self.builtinSignals.contains(name),
            "Cannot override built-in signal '\(name)'"
        )
        signals[name] = weight
    }

    /// Compute a combined match score.
    ///
    /// - Parameters:
    ///   - reidScore: Cosine similarity from ReID (0-1).
    ///   - faceScore: Cosine similarity from face recognition (0-1).
    ///   - reasoningResult: Result from the LLM reasoning worker, or nil.
    ///   - extraSignalScores: Additional signals registered via
    ///     ``registerSignal(name:weight:)``, as name -> score (0-1) pairs.
    /// - Returns: ``ScoredResult`` with the combined score and metadata.
    func score(
        reidScore: Float = 0.0,
        faceScore: Float = 0.0,
        reasoningResult: ReasoningResult? = nil,
        extraSignalScores: [String: Float] = [:]
    ) -> ScoredResult {
        let reasoningScore = Self.reasoningToScore(result: reasoningResult)

        // Assemble raw scores for every signal supplied this call.
        var rawScores: [String: Float] = [
            "reid": reidScore,
            "face": faceScore,
        ]

        // Reasoning is excluded unless the LLM actually reported a match --
        // a confident "no match" shouldn't be treated as a positive signal.
        if reasoningScore > 0, reasoningResult == nil || reasoningResult!.isMatch {
            rawScores["reasoning"] = reasoningScore
        }

        for (name, value) in extraSignalScores {
            precondition(
                !Self.builtinSignals.contains(name),
                "Cannot override built-in signal '\(name)' via extraSignalScores; "
                + "use the named parameter instead (reidScore, faceScore, or reasoningResult)"
            )
            precondition(
                signals[name] != nil,
                "Unknown signal '\(name)' -- register it first with registerSignal(name: \"\(name)\", weight: ...)"
            )
            rawScores[name] = value
        }

        // Compute active weights (redistribute when a signal is missing)
        var activeWeights: [String: Float] = [:]
        var totalWeight: Float = 0
        for (name, value) in rawScores {
            if value > 0 {
                guard let w = signals[name] else { continue }
                activeWeights[name] = w
                totalWeight += w
            }
        }

        // If no signals have positive scores, return zero
        if totalWeight == 0 {
            return ScoredResult(
                combinedScore: 0.0,
                isMatch: false,
                confidenceLevel: "none",
                signalsUsed: 0
            )
        }

        // Normalize weights to sum to 1.0 (redistribute missing signal weight)
        var normWeights: [String: Float] = [:]
        for (name, w) in activeWeights {
            normWeights[name] = w / totalWeight
        }

        // Weighted sum
        var combined: Float = 0
        for (name, w) in normWeights {
            combined += rawScores[name]! * w
        }
        // Round to 3 decimal places
        combined = Float(Int(combined * 1000)) / 1000.0

        // Determine confidence level
        let numSignals = activeWeights.count
        let confidenceLevel: String
        if combined >= 0.65 && numSignals >= 2 {
            confidenceLevel = "high"
        } else if combined >= 0.40 || (combined >= 0.35 && numSignals >= 2) {
            confidenceLevel = "medium"
        } else {
            confidenceLevel = "low"
        }

        return ScoredResult(
            combinedScore: combined,
            isMatch: combined >= matchThreshold,
            confidenceLevel: confidenceLevel,
            signalsUsed: numSignals
        )
    }

    /// Determine alert level from a score result.
    ///
    /// - Returns: `"confirmed_match"`, `"possible_match"`, `"weak_signal"`, or `"no_match"`.
    func alertLevel(_ result: ScoredResult) -> String {
        let score = result.combinedScore
        let signals = result.signalsUsed
        let conf = result.confidenceLevel

        if score >= 0.65 && signals >= 2 && conf == "high" {
            return "confirmed_match"
        } else if score >= matchThreshold && (conf == "medium" || conf == "high") {
            return "possible_match"
        } else if score >= matchThreshold * 0.5 {
            return "weak_signal"
        } else {
            return "no_match"
        }
    }

    /// Convert an LLM reasoning result to a numeric score.
    ///
    /// A "no match" returns a dampened score (not zero) to avoid
    /// vetoing reliable signals when the LLM misjudges low-quality
    /// drone footage.
    static func reasoningToScore(result: ReasoningResult?) -> Float {
        guard let result = result else { return 0.0 }

        let confidence = result.confidence.lowercased()
        if result.isMatch {
            switch confidence {
            case "high": return 0.90
            case "medium": return 0.65
            case "low": return 0.40
            default: return 0.50
            }
        } else {
            switch confidence {
            case "high": return 0.10
            case "medium": return 0.20
            case "low": return 0.30
            default: return 0.30
            }
        }
    }
}
