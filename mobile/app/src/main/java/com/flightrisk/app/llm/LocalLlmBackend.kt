package com.flightrisk.app.llm

import android.graphics.Bitmap
import android.util.Log
import com.flightrisk.app.llm.local.LocalLlmProvider
import com.flightrisk.app.llm.local.ModelState
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.withTimeout

/**
 * [LlmBackend] implementation that delegates to any [LocalLlmProvider].
 *
 * Formats prompts in the same MATCH / CONFIDENCE / REASONING structure
 * as [CloudClaudeLlmBackend], but runs inference on-device. Since local
 * models are smaller and less reliable, a confidence discount is applied
 * to all results.
 *
 * The provider is injected at construction time and can be any
 * [LocalLlmProvider] implementation — MediaPipe, llama.cpp, ONNX, etc.
 *
 * Because local models are text-only, images are described via a simple
 * pixel-based description (color histogram summary). For full multimodal
 * support, swap in a vision-language model provider.
 *
 * @param provider The on-device model provider.
 * @param confidenceDiscount Multiplier applied to confidence scores
 *   (default 0.7 = 30% reduction vs. cloud models).
 */
class LocalLlmBackend(
    private val provider: LocalLlmProvider,
    private val confidenceDiscount: Float = 0.7f,
) : LlmBackend {

    companion object {
        private const val TAG = "LocalLlmBackend"
        private const val INFERENCE_TIMEOUT_MS = 30_000L
        private const val MAX_TOKENS = 256
    }

    override val name: String = "local_${provider.modelInfo.id}"

    override val isAvailable: Boolean
        get() = provider.isReady

    override suspend fun analyzeMatch(
        referenceImage: Bitmap,
        candidateImage: Bitmap,
        description: String?,
    ): ReasoningResult {
        if (!tryAutoLoad()) {
            return errorResult("Local model not available")
        }

        val refDesc = describeBitmap(referenceImage, "reference person")
        val candDesc = describeBitmap(candidateImage, "detected person")

        val prompt = buildString {
            append("<start_of_turn>user\n")
            append("You are helping find a missing person.\n\n")
            append("Reference person description:\n$refDesc\n\n")
            append("Candidate person description:\n$candDesc\n\n")
            if (!description.isNullOrBlank()) {
                append("Additional context: $description\n\n")
            }
            append("Compare these two people. Consider clothing color/type, ")
            append("hair, build, and distinguishing features.\n\n")
            append("Respond in this exact format:\n")
            append("MATCH: yes or no\n")
            append("CONFIDENCE: high, medium, or low\n")
            append("REASONING: one sentence explaining why\n")
            append("<end_of_turn>\n")
            append("<start_of_turn>model\n")
        }

        return runInference(prompt)
    }

    override suspend fun describeMatch(
        candidateImage: Bitmap,
        description: String,
    ): ReasoningResult {
        if (!tryAutoLoad()) {
            return errorResult("Local model not available")
        }

        val candDesc = describeBitmap(candidateImage, "detected person")

        val prompt = buildString {
            append("<start_of_turn>user\n")
            append("You are helping find a missing person.\n")
            append("The person's description: $description\n\n")
            append("A drone camera detected a person. ")
            append("Description of the detected person:\n$candDesc\n\n")
            append("Does this person match the description above?\n")
            append("Consider: clothing color/type, hair color/style, ")
            append("approximate age, build, backpack/accessories, ")
            append("and any distinguishing features.\n\n")
            append("Respond in this exact format:\n")
            append("MATCH: yes or no\n")
            append("CONFIDENCE: high, medium, or low\n")
            append("REASONING: one sentence explaining why\n")
            append("<end_of_turn>\n")
            append("<start_of_turn>model\n")
        }

        return runInference(prompt)
    }

    private suspend fun tryAutoLoad(): Boolean {
        if (provider.isReady) return true
        val current = provider.state.value
        if (current is ModelState.Downloaded) {
            return try {
                provider.load()
                provider.isReady
            } catch (e: Exception) {
                Log.w(TAG, "Auto-load failed: ${e.message}")
                false
            }
        }
        return false
    }

    private suspend fun runInference(prompt: String): ReasoningResult {
        return try {
            val rawText = withTimeout(INFERENCE_TIMEOUT_MS) {
                provider.generate(prompt, MAX_TOKENS)
            }

            val cleaned = rawText
                .replace("<end_of_turn>", "")
                .replace("<start_of_turn>", "")
                .trim()

            parseAndDiscount(cleaned)
        } catch (e: TimeoutCancellationException) {
            Log.w(TAG, "Inference timed out")
            errorResult("Local inference timed out")
        } catch (e: Exception) {
            Log.e(TAG, "Inference failed", e)
            errorResult("Local inference failed: ${e.message}")
        }
    }

    private fun parseAndDiscount(text: String): ReasoningResult {
        var isMatch = false
        var confidence = "unknown"
        var reasoning = text

        for (line in text.split("\n")) {
            val upper = line.trim().uppercase()
            when {
                upper.startsWith("MATCH:") -> isMatch = "YES" in upper
                upper.startsWith("CONFIDENCE:") ->
                    confidence = upper.substringAfter(":").trim().lowercase()
                upper.startsWith("REASONING:") ->
                    reasoning = line.trim().substringAfter(":").trim()
            }
        }

        return ReasoningResult(
            isMatch = isMatch,
            confidence = discountConfidence(confidence),
            reasoning = reasoning,
        )
    }

    private fun discountConfidence(confidence: String): String {
        if (confidenceDiscount >= 1.0f) return confidence
        return when (confidence) {
            "high" -> "medium"
            "medium" -> "low"
            else -> confidence
        }
    }

    /**
     * Extract a text description from a bitmap for text-only models.
     *
     * Computes dominant colors from a downsampled grid. This is a
     * lightweight fallback — vision-language model providers can
     * override with actual image understanding.
     */
    private fun describeBitmap(bitmap: Bitmap, label: String): String {
        val w = bitmap.width
        val h = bitmap.height
        if (w == 0 || h == 0) return "$label: empty image"

        val sampleSize = 8
        val stepX = maxOf(1, w / sampleSize)
        val stepY = maxOf(1, h / sampleSize)

        var totalR = 0L; var totalG = 0L; var totalB = 0L
        var count = 0

        for (y in 0 until h step stepY) {
            for (x in 0 until w step stepX) {
                val pixel = bitmap.getPixel(x, y)
                totalR += android.graphics.Color.red(pixel)
                totalG += android.graphics.Color.green(pixel)
                totalB += android.graphics.Color.blue(pixel)
                count++
            }
        }

        if (count == 0) return "$label: could not analyze image"

        val avgR = (totalR / count).toInt()
        val avgG = (totalG / count).toInt()
        val avgB = (totalB / count).toInt()

        val dominantColor = when {
            avgR > 180 && avgG < 100 && avgB < 100 -> "red"
            avgR < 100 && avgG > 180 && avgB < 100 -> "green"
            avgR < 100 && avgG < 100 && avgB > 180 -> "blue"
            avgR > 180 && avgG > 180 && avgB < 100 -> "yellow"
            avgR > 180 && avgG < 100 && avgB > 180 -> "purple"
            avgR < 100 && avgG > 180 && avgB > 180 -> "cyan"
            avgR > 200 && avgG > 200 && avgB > 200 -> "white/light"
            avgR < 60 && avgG < 60 && avgB < 60 -> "black/dark"
            avgR > 150 && avgG > 150 && avgB > 150 -> "gray/light"
            else -> "mixed colors (R=$avgR, G=$avgG, B=$avgB)"
        }

        val aspectRatio = h.toFloat() / w
        val build = when {
            aspectRatio > 3.0f -> "very tall and narrow"
            aspectRatio > 2.0f -> "tall"
            aspectRatio > 1.5f -> "average proportions"
            else -> "wide/stocky"
        }

        return "$label: ${w}x${h} image, dominant color is $dominantColor, $build build"
    }

    private fun errorResult(message: String) = ReasoningResult(
        isMatch = false,
        confidence = "error",
        reasoning = message,
    )
}
