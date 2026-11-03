package com.flightrisk.contract

import kotlin.math.abs
import kotlin.math.sqrt

/**
 * The appearance (ReID) embedding a phone edge may put in `reid_embedding`,
 * as the ground compares it (`flightrisk/vision/reid_spec.py`, published in
 * `edge_edge_to_ground.json` under `_meta.reid_embedding`).
 *
 * The ground compares each detection's vector with the target embedding it
 * made itself from the reference photo, by cosine similarity. That number
 * means something only when both vectors come from the same model and the
 * same weights. A vector of the right length from another model (for example
 * OpenAI CLIP ViT-B/32) still gives a number, but a meaningless one, so a
 * phone must send only vectors whose model is [MODEL_ID].
 *
 * Embeddings describe a person's appearance and are treated like biometric
 * data: they are kept in memory only, never logged and never stored.
 */
object ReidEmbedding {
    const val LIBRARY = "open_clip"
    const val MODEL = "ViT-B-16"
    const val PRETRAINED = "laion2b_s34b_b88k"

    /** The model an embedding provider must name to have its vectors sent. */
    const val MODEL_ID = "$LIBRARY:$MODEL:$PRETRAINED"

    /** Number of values in one embedding. */
    const val DIMENSION = 512

    /** Preprocessing of the model input: shortest side resized to this (bicubic), then a center crop of this square. */
    const val INPUT_SIZE = 224
    val MEAN: List<Double> = listOf(0.48145466, 0.4578275, 0.40821073)
    val STD: List<Double> = listOf(0.26862954, 0.26130258, 0.27577711)

    /** How far from 1 the L2 norm of a sent vector may be (float32 rounding is far smaller). */
    const val NORM_TOLERANCE = 1e-3

    /**
     * Why [values] is not a valid wire embedding, or null when it is: exactly
     * [DIMENSION] finite values with an L2 norm within [NORM_TOLERANCE] of 1.
     * The text names only sizes, never a value.
     */
    fun problem(values: List<Double>?): String? {
        if (values == null) return "no embedding"
        if (values.size != DIMENSION) return "expected $DIMENSION values, got ${values.size}"
        if (values.any { !it.isFinite() }) return "a value is not a finite number"
        val norm = sqrt(values.sumOf { it * it })
        if (abs(norm - 1.0) > NORM_TOLERANCE) return "the vector does not have unit length"
        return null
    }

    /**
     * The wire form of one raw model output: L2-normalized and rounded to
     * float32, as `PersonReID.extract_embedding(...).tolist()` gives it in
     * Python. Throws [IllegalArgumentException] (with a message that names
     * no value) when [raw] has the wrong length, holds a value that is not
     * finite, or is all zeros.
     */
    fun toWire(raw: FloatArray): List<Double> {
        require(raw.size == DIMENSION) { "expected $DIMENSION values, got ${raw.size}" }
        var sumSq = 0.0
        for (v in raw) {
            require(v.isFinite()) { "a value is not a finite number" }
            sumSq += v.toDouble() * v.toDouble()
        }
        val norm = sqrt(sumSq)
        require(norm > 0.0 && norm.isFinite()) { "the vector has no length" }
        val out = List(DIMENSION) { i -> (raw[i].toDouble() / norm).toFloat().toDouble() }
        problem(out)?.let { throw IllegalArgumentException(it) }
        return out
    }
}
