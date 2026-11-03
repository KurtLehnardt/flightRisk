import Foundation

/// Multi-signal weighted confidence scorer.
///
/// Port of Android `MatchScorer.kt` / Python `flightrisk/vision/scorer.py`. Arithmetic is in Double, like Python,
/// and the combined score is rounded like Python; `tests/fixtures/mobile_contract/match_score_vectors.json`
/// holds the cases all three implementations must reproduce.
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
    private(set) var reidWeight: Double = 0.35
    /// Weight for face recognition score.
    private(set) var faceWeight: Double = 0.40
    /// Weight for LLM reasoning confidence.
    private(set) var reasoningWeight: Double = 0.25
    /// Combined score threshold for a positive match.
    private(set) var matchThreshold: Double = 0.45

    // MARK: - Signal registry

    /// Signal registry: name -> weight. Built-in signals are seeded here
    /// so they flow through the same weighting/redistribution logic as
    /// anything added via ``registerSignal(name:weight:)``.
    private var signals: [String: Double]

    // MARK: - Init

    init(
        reidWeight: Double = 0.35,
        faceWeight: Double = 0.40,
        reasoningWeight: Double = 0.25,
        matchThreshold: Double = 0.45
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
    mutating func registerSignal(name: String, weight: Double) {
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
        reidScore: Double = 0.0,
        faceScore: Double = 0.0,
        reasoningResult: ReasoningResult? = nil,
        extraSignalScores: [String: Double] = [:]
    ) -> ScoredResult {
        let reasoningScore = Self.reasoningToScore(result: reasoningResult)

        // Assemble raw scores for every signal supplied this call.
        var rawScores: [String: Double] = [
            "reid": reidScore,
            "face": faceScore,
        ]

        // Reasoning is always included when positive — "no match" results
        // use dampened scores (0.10-0.30) to avoid vetoing reliable signals.
        if reasoningScore > 0 {
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

        // Every floating-point accumulation below walks the signals in this
        // fixed order. Dictionary iteration order is randomized per process,
        // and Double addition is not associative, so iterating a dictionary
        // directly made the rounded score vary between runs.
        let orderedNames = Self.canonicalOrder(of: rawScores.keys)

        // Compute active weights (redistribute when a signal is missing)
        var activeWeights: [String: Double] = [:]
        var totalWeight: Double = 0
        for name in orderedNames {
            let value = rawScores[name]!
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
        var normWeights: [String: Double] = [:]
        for name in orderedNames {
            if let w = activeWeights[name] {
                normWeights[name] = w / totalWeight
            }
        }

        // Weighted sum, accumulated in canonical signal order.
        var combined: Double = 0
        for name in orderedNames {
            if let w = normWeights[name] {
                combined += rawScores[name]! * w
            }
        }
        // Round to 3 decimal places exactly as Python's round(x, 3) does.
        combined = Self.roundToThreePlaces(combined)

        // Count only signals with meaningfully positive scores for confidence
        // level checks. Dampened "no match" reasoning (scores < 0.3) should
        // drag down the combined score but not count as a positive signal
        // that could promote confidence to "high".
        let positiveSignalCount = rawScores.filter { $0.value >= 0.3 }.count

        // Determine confidence level using positive signals only
        let confidenceLevel: String
        if combined >= 0.65 && positiveSignalCount >= 2 {
            confidenceLevel = "high"
        } else if combined >= 0.40 || (combined >= 0.35 && positiveSignalCount >= 2) {
            confidenceLevel = "medium"
        } else {
            confidenceLevel = "low"
        }

        return ScoredResult(
            combinedScore: combined,
            isMatch: combined >= matchThreshold,
            confidenceLevel: confidenceLevel,
            signalsUsed: activeWeights.count  // total for transparency
        )
    }

    /// A Float setting (the config stores Float) as the Double with the same decimal spelling, so
    /// 0.35 as a Float becomes the Double 0.35 that Python's config holds, not 0.3499999940395355.
    static func decimal(_ value: Float) -> Double {
        Double("\(value)") ?? Double(value)
    }

    /// Python's `round(x, 3)`: the exact binary value, rounded to the nearest thousandth with ties to
    /// even, keeping the sign of zero. NaN and the infinities come back unchanged. Truncating
    /// (`Int(x * 1000) / 1000`) is wrong: it differs from Python for most inputs.
    ///
    /// `String(format:)` formats the exact value of the double, as Python's `round` does, so the two
    /// agree on every input, including values such as 0.0625 that sit exactly on a tie.
    static func roundToThreePlaces(_ x: Double) -> Double {
        guard x.isFinite else { return x }
        return Double(String(format: "%.3f", x)) ?? x
    }

    /// Fixed signal order used for all floating-point accumulation: the
    /// built-in signals in documented order (reid, face, reasoning), followed
    /// by any registered extra signals sorted by name.
    private static func canonicalOrder<S: Sequence>(of names: S) -> [String] where S.Element == String {
        let builtinOrder = ["reid", "face", "reasoning"]
        let present = Set(names)
        let builtins = builtinOrder.filter { present.contains($0) }
        let extras = present.subtracting(builtinOrder).sorted()
        return builtins + extras
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
    static func reasoningToScore(result: ReasoningResult?) -> Double {
        guard let result = result else { return 0.0 }

        let confidence = result.confidence.lowercased()
        let score: Double
        if result.isMatch {
            switch confidence {
            case "high": score = 0.90
            case "medium": score = 0.65
            case "low": score = 0.40
            default: score = 0.50
            }
        } else {
            switch confidence {
            case "high": score = 0.10
            case "medium": score = 0.20
            case "low": score = 0.30
            default: score = 0.30
            }
        }

        return score * Double(result.confidenceDiscount)
    }
}
