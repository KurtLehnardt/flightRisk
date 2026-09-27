package com.flightrisk.app.vision

import android.util.Log
import kotlin.math.abs
import kotlin.math.max

/**
 * Bounding box height-to-width ratio matcher.
 *
 * Compares the aspect ratio (height / width) of a target person's bounding
 * box against detection bounding boxes. People of similar build and posture
 * will have similar bbox aspect ratios, providing a lightweight geometric
 * signal that requires no model inference.
 *
 * Signal name: `height_ratio`
 * Default weight: 0.05
 */
class HeightRatioMatcher {

    companion object {
        private const val TAG = "HeightRatioMatcher"
    }

    /** Precomputed target aspect ratio (height / width). */
    private var targetRatio: Float? = null

    /** Whether a target ratio has been set. */
    val hasTarget: Boolean
        get() = targetRatio != null

    /**
     * Set the target aspect ratio from a reference bounding box.
     *
     * @param bbox Bounding box as [x1, y1, x2, y2] pixel coordinates.
     */
    fun setTarget(bbox: IntArray) {
        val w = (bbox[2] - bbox[0]).toFloat()
        val h = (bbox[3] - bbox[1]).toFloat()
        if (w > 0f && h > 0f) {
            targetRatio = h / w
            Log.d(TAG, "Target height ratio set: ${targetRatio}")
        }
    }

    /**
     * Set the target aspect ratio from the full target photo dimensions.
     * Assumes the photo is a tightly-cropped person.
     *
     * @param width Photo width in pixels.
     * @param height Photo height in pixels.
     */
    fun setTargetFromDimensions(width: Int, height: Int) {
        if (width > 0 && height > 0) {
            targetRatio = height.toFloat() / width.toFloat()
            Log.d(TAG, "Target height ratio set from dimensions: ${targetRatio}")
        }
    }

    /** Clear the current target ratio. */
    fun clearTarget() {
        targetRatio = null
    }

    /**
     * Compare a detection bounding box's aspect ratio against the target.
     *
     * Score = 1.0 - |target_ratio - detection_ratio| / max(target_ratio, detection_ratio)
     * Clamped to [0, 1].
     *
     * @param bbox Detection bounding box as [x1, y1, x2, y2].
     * @return Similarity score (0-1). Higher = more similar aspect ratio.
     *         Returns 0.0 if no target is set.
     */
    fun compare(bbox: IntArray): Float {
        val target = targetRatio ?: return 0.0f

        val w = (bbox[2] - bbox[0]).toFloat()
        val h = (bbox[3] - bbox[1]).toFloat()
        if (w <= 0f || h <= 0f) return 0.0f

        val detectionRatio = h / w
        val maxRatio = max(target, detectionRatio)
        if (maxRatio <= 0f) return 0.0f

        val score = 1.0f - abs(target - detectionRatio) / maxRatio
        return score.coerceIn(0f, 1f)
    }
}
