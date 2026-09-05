package com.flightrisk.app.vision

/**
 * Multi-signal weighted confidence scorer.
 *
 * Port of `flightrisk/vision/scorer.py`. Combines ReID body similarity,
 * face recognition, and LLM reasoning into a single weighted confidence
 * score. Dynamic weight redistribution when signals are missing ensures
 * the available signals are always normalized to sum to 1.0.
 *
 * New signals (thermal, gait, etc.) can be added via [registerSignal]
 * without touching this class's internals.
 *
 * @param reidWeight Weight for full-body ReID similarity.
 * @param faceWeight Weight for face recognition score.
 * @param reasoningWeight Weight for LLM reasoning confidence.
 * @param matchThreshold Combined score threshold for a positive match.
 */
class MatchScorer(
    private val reidWeight: Float = 0.35f,
    private val faceWeight: Float = 0.40f,
    private val reasoningWeight: Float = 0.25f,
    private val matchThreshold: Float = 0.45f,
) {

    companion object {
        /**
         * Names populated internally via [score]'s named parameters.
         * These may not be used as [registerSignal] names -- doing so
         * would silently shadow the named-param value.
         */
        val BUILTIN_SIGNALS = setOf("reid", "face", "reasoning")
    }

    /**
     * Signal registry: name -> weight. Built-in signals are seeded here
     * so they flow through the same weighting/redistribution logic as
     * anything added via [registerSignal].
     */
    private val signals: MutableMap<String, Float> = mutableMapOf(
        "reid" to reidWeight,
        "face" to faceWeight,
        "reasoning" to reasoningWeight,
    )

    /**
     * Register a new named signal and its weight for use in [score].
     *
     * Once registered, pass the signal's numeric score (0-1) via the
     * [extraSignals] map parameter of [score].
     *
     * Not thread-safe: call only during initialization, before the
     * scorer is shared across concurrent code paths.
     *
     * @throws IllegalArgumentException if [name] collides with a built-in signal.
     */
    fun registerSignal(name: String, weight: Float) {
        require(name !in BUILTIN_SIGNALS) {
            "Cannot override built-in signal '$name'"
        }
        signals[name] = weight
    }

    /**
     * Compute a combined match score.
     *
     * @param reidScore Cosine similarity from ReID (0-1).
     * @param faceScore Cosine similarity from face recognition (0-1).
     * @param reasoningResult Result from the LLM reasoning worker, or null.
     * @param extraSignals Additional signals registered via [registerSignal],
     *   as name -> score (0-1) pairs.
     * @return [ScoredResult] with the combined score and metadata.
     */
    fun score(
        reidScore: Float = 0.0f,
        faceScore: Float = 0.0f,
        reasoningResult: ReasoningResult? = null,
        extraSignals: Map<String, Float> = emptyMap(),
    ): ScoredResult {
        val reasoningScore = reasoningToScore(reasoningResult)

        // Assemble raw scores for every signal supplied this call.
        val rawScores = mutableMapOf(
            "reid" to reidScore,
            "face" to faceScore,
        )

        // Reasoning is excluded unless the LLM actually reported a match --
        // a confident "no match" shouldn't be treated as a positive signal.
        if (reasoningScore > 0f && (reasoningResult == null || reasoningResult.isMatch)) {
            rawScores["reasoning"] = reasoningScore
        }

        for ((name, value) in extraSignals) {
            require(name !in BUILTIN_SIGNALS) {
                "Cannot override built-in signal '$name' via extraSignals; " +
                    "use the named parameter instead (reidScore, faceScore, or reasoningResult)"
            }
            require(name in signals) {
                "Unknown signal '$name' -- register it first with registerSignal(\"$name\", weight = ...)"
            }
            rawScores[name] = value
        }

        // Compute active weights (redistribute when a signal is missing)
        val activeWeights = mutableMapOf<String, Float>()
        var totalWeight = 0f
        for ((name, value) in rawScores) {
            if (value > 0f) {
                val w = signals[name] ?: continue
                activeWeights[name] = w
                totalWeight += w
            }
        }

        // If no signals have positive scores, return zero
        if (totalWeight == 0f) {
            return ScoredResult(
                combinedScore = 0.0f,
                isMatch = false,
                confidenceLevel = "none",
                signalsUsed = 0,
            )
        }

        // Normalize weights to sum to 1.0 (redistribute missing signal weight)
        val normWeights = activeWeights.mapValues { it.value / totalWeight }

        // Weighted sum
        var combined = 0f
        for ((name, w) in normWeights) {
            combined += rawScores[name]!! * w
        }
        combined = (combined * 1000).toInt() / 1000f // round to 3 decimal places

        // Determine confidence level
        val numSignals = activeWeights.size
        val confidenceLevel = when {
            combined >= 0.65f && numSignals >= 2 -> "high"
            combined >= 0.40f || (combined >= 0.35f && numSignals >= 2) -> "medium"
            else -> "low"
        }

        return ScoredResult(
            combinedScore = combined,
            isMatch = combined >= matchThreshold,
            confidenceLevel = confidenceLevel,
            signalsUsed = numSignals,
        )
    }

    /**
     * Determine alert level from a score result.
     *
     * @return "confirmed_match", "possible_match", "weak_signal", or "no_match".
     */
    fun alertLevel(scored: ScoredResult): String {
        val score = scored.combinedScore
        val signals = scored.signalsUsed
        val conf = scored.confidenceLevel

        return when {
            score >= 0.65f && signals >= 2 && conf == "high" -> "confirmed_match"
            score >= matchThreshold && conf in listOf("medium", "high") -> "possible_match"
            score >= matchThreshold * 0.5f -> "weak_signal"
            else -> "no_match"
        }
    }

    /**
     * Convert an LLM reasoning result to a numeric score.
     *
     * A "no match" returns a dampened score (not zero) to avoid
     * vetoing reliable signals when the LLM misjudges low-quality
     * drone footage.
     */
    internal fun reasoningToScore(result: ReasoningResult?): Float {
        if (result == null) return 0.0f

        val confidence = result.confidence.lowercase()
        return if (result.isMatch) {
            when (confidence) {
                "high" -> 0.90f
                "medium" -> 0.65f
                "low" -> 0.40f
                else -> 0.50f
            }
        } else {
            when (confidence) {
                "high" -> 0.10f
                "medium" -> 0.20f
                "low" -> 0.30f
                else -> 0.30f
            }
        }
    }
}
