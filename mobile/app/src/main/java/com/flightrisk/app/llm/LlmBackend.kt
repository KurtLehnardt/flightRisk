package com.flightrisk.app.llm

import android.graphics.Bitmap

/**
 * Interface for LLM reasoning backends.
 *
 * Ports the Python `FlightRiskAgent` abstraction to a pluggable backend
 * system. On mobile, the primary implementation is [CloudClaudeLlmBackend]
 * (Anthropic Messages API). When the device is offline (e.g. on Tello WiFi),
 * the system falls back to [NoOpLlmBackend].
 */
interface LlmBackend {
    /** Human-readable backend name (e.g. "claude", "none"). */
    val name: String

    /** Whether this backend can currently serve requests. */
    val isAvailable: Boolean

    /**
     * Compare a reference image of the missing person against a candidate
     * detection from the drone camera.
     *
     * Mirrors `FlightRiskAgent.analyze_match()` from the Python codebase.
     *
     * @param referenceImage The target/reference photo.
     * @param candidateImage A cropped detection from the drone feed.
     * @param description Optional text description to augment the comparison.
     * @return [ReasoningResult] with match verdict, confidence, and reasoning.
     */
    suspend fun analyzeMatch(
        referenceImage: Bitmap,
        candidateImage: Bitmap,
        description: String? = null,
    ): ReasoningResult

    /**
     * Check whether a candidate detection matches a text description of
     * the missing person (used when no reference photo is available).
     *
     * Mirrors `FlightRiskAgent.match_description()` from the Python codebase.
     *
     * @param candidateImage A cropped detection from the drone feed.
     * @param description Text description of the person to find.
     * @return [ReasoningResult] with match verdict, confidence, and reasoning.
     */
    suspend fun describeMatch(
        candidateImage: Bitmap,
        description: String,
    ): ReasoningResult
}

/**
 * Result of an LLM reasoning call.
 *
 * The structured format mirrors the Python agent's parsed response:
 * `MATCH: yes/no`, `CONFIDENCE: high/medium/low`, `REASONING: ...`.
 *
 * @property isMatch Whether the LLM believes the candidate matches.
 * @property confidence Confidence level: "high", "medium", "low",
 *   "unavailable", "error", or "unknown".
 * @property reasoning Free-text explanation from the LLM.
 */
data class ReasoningResult(
    val isMatch: Boolean,
    val confidence: String,
    val reasoning: String,
)
