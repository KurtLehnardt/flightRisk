package com.flightrisk.app.vision

import com.flightrisk.app.llm.ReasoningResult
import kotlin.math.withSign

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

        /** The decimal number a float prints as, as a double: `0.35f` becomes 0.35, not 0.3499999940395355. */
        internal fun decimal(value: Float): Double = value.toString().toDouble()

        /**
         * `round(value, 3)` as Python does it: the exact binary value of the double, rounded half to
         * even. A value that is not finite (NaN, infinity) is returned unchanged, as Python does.
         */
        internal fun roundHalfEven3(value: Double): Double =
            if (value.isNaN() || value.isInfinite()) {
                value
            } else {
                // A result that rounds to zero keeps the sign of the input, as Python's round() does (-0.0004 gives -0.0).
                val rounded = java.math.BigDecimal(value).setScale(3, java.math.RoundingMode.HALF_EVEN).toDouble()
                if (rounded == 0.0) 0.0.withSign(value) else rounded
            }
    }

    /**
     * Signal registry: name -> weight. Built-in signals are seeded here
     * so they flow through the same weighting/redistribution logic as
     * anything added via [registerSignal].
     */
    private val signals: MutableMap<String, Double> = mutableMapOf(
        "reid" to decimal(reidWeight),
        "face" to decimal(faceWeight),
        "reasoning" to decimal(reasoningWeight),
    )

    private val threshold: Double = decimal(matchThreshold)

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
        signals[name] = decimal(weight)
    }

    /**
     * Update the weight of any registered signal (including built-ins).
     *
     * Use this for runtime weight adjustment — e.g. reducing reasoning
     * weight when the active LLM backend is a slow local model.
     *
     * @throws IllegalArgumentException if [name] is not registered.
     */
    fun updateWeight(name: String, weight: Float) {
        require(name in signals) { "Unknown signal '$name'" }
        signals[name] = decimal(weight)
    }

    /**
     * Compute a combined match score.
     *
     * The arithmetic is done in double precision and rounded exactly like the Python scorer
     * (`round(x, 3)`, half to even on the exact binary value), so the same inputs give the same
     * score on the ground and on the phone. A [Float] argument is read as the decimal number it
     * prints as (`0.35f` is 0.35), because that is the number the caller meant.
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
    ): ScoredResult = scoreExact(
        reidScore = decimal(reidScore),
        faceScore = decimal(faceScore),
        reasoningResult = reasoningResult,
        extraSignals = extraSignals.mapValues { decimal(it.value) },
    )

    /** Same as [score] with double precision inputs, for callers and tests that hold a [Double]. */
    fun scoreExact(
        reidScore: Double = 0.0,
        faceScore: Double = 0.0,
        reasoningResult: ReasoningResult? = null,
        extraSignals: Map<String, Double> = emptyMap(),
    ): ScoredResult {
        val reasoningScore = decimal(reasoningToScore(reasoningResult))

        // Assemble raw scores for every signal supplied this call, in the order Python uses.
        val rawScores = linkedMapOf(
            "reid" to reidScore,
            "face" to faceScore,
        )

        // Reasoning is excluded unless the LLM actually reported a match --
        // a confident "no match" shouldn't be treated as a positive signal.
        if (reasoningScore > 0.0 && (reasoningResult == null || reasoningResult.isMatch)) {
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

        // Compute active weights (redistribute when a signal is missing). A NaN score is not
        // greater than zero, so it is left out, as in Python.
        val activeWeights = linkedMapOf<String, Double>()
        var totalWeight = 0.0
        for ((name, value) in rawScores) {
            if (value > 0.0) {
                val w = signals[name] ?: continue
                activeWeights[name] = w
                totalWeight += w
            }
        }

        // If no signals have positive scores, return zero
        if (totalWeight == 0.0) {
            return ScoredResult(
                combinedScore = 0.0f,
                isMatch = false,
                confidenceLevel = "none",
                signalsUsed = 0,
                combinedScoreExact = 0.0,
            )
        }

        // Weighted sum of the normalized weights (they sum to 1.0), in insertion order.
        var combined = 0.0
        for ((name, weight) in activeWeights) {
            combined += rawScores.getValue(name) * (weight / totalWeight)
        }
        combined = roundHalfEven3(combined)

        // Determine confidence level
        val numSignals = activeWeights.size
        val confidenceLevel = when {
            combined >= 0.65 && numSignals >= 2 -> "high"
            combined >= 0.40 || (combined >= 0.35 && numSignals >= 2) -> "medium"
            else -> "low"
        }

        return ScoredResult(
            combinedScore = combined.toFloat(),
            isMatch = combined >= threshold,
            confidenceLevel = confidenceLevel,
            signalsUsed = numSignals,
            combinedScoreExact = combined,
        )
    }

    /**
     * Determine alert level from a score result.
     *
     * @return "confirmed_match", "possible_match", "weak_signal", or "no_match".
     */
    fun alertLevel(scored: ScoredResult): String {
        val score = scored.combinedScoreExact
        val signals = scored.signalsUsed
        val conf = scored.confidenceLevel

        return when {
            score >= 0.65 && signals >= 2 && conf == "high" -> "confirmed_match"
            score >= threshold && conf in listOf("medium", "high") -> "possible_match"
            score >= threshold * 0.5 -> "weak_signal"
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
