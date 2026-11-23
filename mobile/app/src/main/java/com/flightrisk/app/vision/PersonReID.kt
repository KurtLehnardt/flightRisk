package com.flightrisk.app.vision

import android.graphics.Bitmap
import android.util.Log
import java.io.Closeable

/**
 * Appearance (person re-identification) matching on the phone.
 *
 * The embedder is the image tower of open_clip ViT-B-16 `laion2b_s34b_b88k`
 * (MIT), the ground's own model, installed into the assets by
 * `scripts/mobile_models/make_models.py install` and run with the exact
 * preprocessing of [ClipContract]. It makes a 512-value, L2-normalised
 * embedding of the target photo and of each detected person's crop, and
 * compares them by cosine similarity. No face model is used: matching is by
 * appearance (clothing, colours, build) only.
 *
 * The embeddings stay in memory and are never logged or stored.
 *
 * @param engine The embedder without Android types.
 * @param matchThreshold Cosine similarity at or above which a crop matches.
 * @param selection Which detections are compared: tall enough, and the most
 *   confident few ([MatchSelection]).
 */
class PersonReID(
    private val engine: EmbedderEngine,
    private val matchThreshold: Float,
    private val selection: MatchSelection = MatchSelection(minCropHeightPx = 40, maxPerFrame = 5),
) : Closeable {

    @Volatile
    private var targetEmbedding: FloatArray? = null

    companion object {
        private const val TAG = "PersonReID"
    }

    /**
     * Set the reference image of the person to find.
     *
     * @param photo The target photo (or a crop of the target person in it).
     */
    fun setTarget(photo: Bitmap) {
        targetEmbedding = extractEmbedding(photo)
        Log.d(TAG, "Target set")
    }

    /** Clear the current target embedding. */
    fun clearTarget() {
        targetEmbedding = null
    }

    /** Whether a target embedding is currently set. */
    val hasTarget: Boolean
        get() = targetEmbedding != null

    /**
     * Compare a detected person crop against the target.
     *
     * @return Cosine similarity clamped to 0..1, or 0 when no target is set.
     */
    fun compare(crop: Bitmap): Float {
        val target = targetEmbedding ?: return 0.0f
        val embedding = extractEmbedding(crop)
        var dot = 0.0
        for (i in target.indices) dot += target[i].toDouble() * embedding[i]
        return dot.toFloat().coerceIn(0f, 1f)
    }

    /**
     * Find the best match among detected persons. Only the detections
     * [selection] allows are embedded and compared; the others get no score.
     *
     * @return (index, score) of the best match, or (null, best score) if no
     *   compared crop reaches the threshold.
     */
    fun findMatch(detections: List<Detection>): Pair<Int?, Float> {
        if (targetEmbedding == null || detections.isEmpty()) return Pair(null, 0.0f)

        var bestIdx: Int? = null
        var bestScore = 0.0f
        val compared = selection.eligible(detections.map { it.bbox[3] - it.bbox[1] }, detections.map { it.confidence })
        for (i in compared) {
            val score = compare(detections[i].crop)
            if (score > bestScore) {
                bestScore = score
                bestIdx = i
            }
        }
        return if (bestScore >= matchThreshold) Pair(bestIdx, bestScore) else Pair(null, bestScore)
    }

    private fun extractEmbedding(image: Bitmap): FloatArray {
        val w = image.width
        val h = image.height
        val argb = IntArray(w * h)
        image.getPixels(argb, 0, w, 0, 0, w, h)
        return engine.embed(argb, w, h)
    }

    override fun close() {
        engine.close()
    }
}
