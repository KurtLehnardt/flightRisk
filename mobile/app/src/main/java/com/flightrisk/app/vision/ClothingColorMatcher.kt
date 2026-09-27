package com.flightrisk.app.vision

import android.graphics.Bitmap
import android.graphics.Color
import android.util.Log
import kotlin.math.sqrt

/**
 * HSV color histogram matching for clothing appearance.
 *
 * Splits a person crop into upper body (top 40%) and lower body (bottom 40%)
 * regions, computes normalized joint hue x saturation (H x S) histograms for
 * each, and compares them against a target reference using histogram
 * correlation. This provides a lightweight, model-free appearance signal
 * that complements learned ReID embeddings.
 *
 * Mirrors the Python/iOS implementation, which uses a single joint 2D
 * histogram (not separate 1D hue and saturation histograms) so that
 * hue/saturation combinations are compared jointly rather than marginally.
 *
 * Signal name: `clothing_color`
 * Default weight: 0.15
 */
class ClothingColorMatcher {

    companion object {
        private const val TAG = "ClothingColorMatcher"

        /** Number of hue bins (0-360 mapped to 0-29). */
        const val HUE_BINS = 30

        /** Number of saturation bins (0-1 mapped to 0-31). */
        const val SAT_BINS = 32

        /** Upper body: top 40% of the crop. */
        private const val UPPER_START = 0.0f
        private const val UPPER_END = 0.4f

        /** Lower body: bottom 40% of the crop. */
        private const val LOWER_START = 0.6f
        private const val LOWER_END = 1.0f
    }

    /** Precomputed target joint H x S histograms (upper and lower body). */
    private var targetUpperHist: FloatArray? = null
    private var targetLowerHist: FloatArray? = null

    /** Whether a target has been set. */
    val hasTarget: Boolean
        get() = targetUpperHist != null

    /**
     * Set the reference image of the person to match clothing colors against.
     *
     * @param photo RGB [Bitmap] of the target person.
     */
    fun setTarget(photo: Bitmap) {
        targetUpperHist = computeRegionHistogram(photo, UPPER_START, UPPER_END)
        targetLowerHist = computeRegionHistogram(photo, LOWER_START, LOWER_END)
        Log.d(TAG, "Target clothing histograms set")
    }

    /** Clear the current target histograms. */
    fun clearTarget() {
        targetUpperHist = null
        targetLowerHist = null
    }

    /**
     * Compare a detected person crop's clothing colors against the target.
     *
     * @param crop RGB [Bitmap] of a detected person.
     * @return Correlation score (0-1). Higher = more similar clothing colors.
     *         Returns 0.0 if no target is set.
     */
    fun compare(crop: Bitmap): Float {
        val tUpper = targetUpperHist ?: return 0.0f
        val tLower = targetLowerHist ?: return 0.0f

        return try {
            val upperHist = computeRegionHistogram(crop, UPPER_START, UPPER_END)
            val lowerHist = computeRegionHistogram(crop, LOWER_START, LOWER_END)

            // Correlation for each region's joint histogram
            val upperCorr = histogramCorrelation(tUpper, upperHist)
            val lowerCorr = histogramCorrelation(tLower, lowerHist)

            // Average across both regions, clamped to [0, 1]
            val avg = (upperCorr + lowerCorr) / 2f
            avg.coerceIn(0f, 1f)
        } catch (e: Exception) {
            Log.w(TAG, "Clothing color comparison failed", e)
            0.0f
        }
    }

    /**
     * Compute a normalized joint hue x saturation histogram for a vertical
     * region of the image.
     *
     * @param image Source bitmap.
     * @param startFraction Top of the region as a fraction of image height (0-1).
     * @param endFraction Bottom of the region as a fraction of image height (0-1).
     * @return Normalized joint H x S histogram of size [HUE_BINS] * [SAT_BINS].
     */
    private fun computeRegionHistogram(
        image: Bitmap,
        startFraction: Float,
        endFraction: Float,
    ): FloatArray {
        val totalBins = HUE_BINS * SAT_BINS
        val histogram = FloatArray(totalBins)
        val hsv = FloatArray(3)
        var count = 0
        val w = image.width
        val h = image.height
        val yStart = (h * startFraction).toInt().coerceAtLeast(0)
        val yEnd = (h * endFraction).toInt().coerceAtMost(h)

        for (y in yStart until yEnd) {
            for (x in 0 until w) {
                val pixel = image.getPixel(x, y)
                Color.RGBToHSV(Color.red(pixel), Color.green(pixel), Color.blue(pixel), hsv)
                val hueBin = ((hsv[0] / 360f) * HUE_BINS).toInt().coerceIn(0, HUE_BINS - 1)
                val satBin = (hsv[1] * SAT_BINS).toInt().coerceIn(0, SAT_BINS - 1)
                histogram[hueBin * SAT_BINS + satBin] += 1f
                count++
            }
        }
        if (count > 0) {
            val countF = count.toFloat()
            for (i in histogram.indices) histogram[i] /= countF
        }
        return histogram
    }

    /**
     * Compute Pearson correlation coefficient between two histograms.
     *
     * Returns a value in [-1, 1]; we clamp negatives to 0 since
     * anti-correlation is meaningless for clothing similarity (matches
     * Python's `max(0, correlation)`).
     */
    private fun histogramCorrelation(a: FloatArray, b: FloatArray): Float {
        val n = a.size
        if (n == 0) return 0f

        var meanA = 0f
        var meanB = 0f
        for (i in 0 until n) {
            meanA += a[i]
            meanB += b[i]
        }
        meanA /= n
        meanB /= n

        var cov = 0f
        var varA = 0f
        var varB = 0f
        for (i in 0 until n) {
            val da = a[i] - meanA
            val db = b[i] - meanB
            cov += da * db
            varA += da * da
            varB += db * db
        }

        val denom = sqrt(varA) * sqrt(varB)
        if (denom < 1e-10f) return 0f

        return (cov / denom).coerceAtLeast(0f)
    }
}
