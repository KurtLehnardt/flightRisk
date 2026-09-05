package com.flightrisk.app.llm

import android.graphics.Bitmap

/**
 * Fallback LLM backend that returns "unavailable" for every request.
 *
 * Used when no real LLM backend is configured or reachable (e.g. the
 * device is on Tello WiFi with no internet, or no API key is set).
 * Always reports [isAvailable] = true so [LlmSelector] has a guaranteed
 * last-resort backend.
 */
class NoOpLlmBackend : LlmBackend {

    override val name: String = "none"

    override val isAvailable: Boolean = true

    override suspend fun analyzeMatch(
        referenceImage: Bitmap,
        candidateImage: Bitmap,
        description: String?,
    ): ReasoningResult = ReasoningResult(
        isMatch = false,
        confidence = "unavailable",
        reasoning = "LLM reasoning not available",
    )

    override suspend fun describeMatch(
        candidateImage: Bitmap,
        description: String,
    ): ReasoningResult = ReasoningResult(
        isMatch = false,
        confidence = "unavailable",
        reasoning = "LLM reasoning not available",
    )
}
