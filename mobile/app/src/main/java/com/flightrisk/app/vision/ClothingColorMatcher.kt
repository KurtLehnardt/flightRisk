package com.flightrisk.app.vision

import android.graphics.Bitmap
import android.graphics.Color
import android.util.Log
import kotlin.math.sqrt

/**
 * HSV color histogram matching for clothing appearance.
 *
 * Splits a person crop into upper body (top 40%) and lower body (bottom 40%)
 * regions, computes normalized HSV histograms for each, and compares them
 * against a target reference using histogram correlation. This provides a
 * lightweight, model-free appearance signal that complements learned ReID
 * embeddings.
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

    /** Precomputed target histograms (upper and lower body). */
    private var targetUpperHue: FloatArray? = null
    private var targetUpperSat: FloatArray? = null
    private var targetLowerHue: FloatArray? = null
    private var targetLowerSat: FloatArray? = null

    /** Whether a target has been set. */
    val hasTarget: Boolean
        get() = targetUpperHue != null

    /**
     * Set the reference image of the person to match clothing colors against.
     *
     * @param photo RGB [Bitmap] of the target person.
     */
    fun setTarget(photo: Bitmap) {
        val (upperHue, upperSat) = computeRegionHistograms(photo, UPPER_START, UPPER_END)
        val (lowerHue, lowerSat) = computeRegionHistograms(photo, LOWER_START, LOWER_END)
        targetUpperHue = upperHue
        targetUpperSat = upperSat
        targetLowerHue = lowerHue
        targetLowerSat = lowerSat
        Log.d(TAG, "Target clothing histograms set")
    }

    /** Clear the current target histograms. */
    fun clearTarget() {
        targetUpperHue = null
        targetUpperSat = null
        targetLowerHue = null
        targetLowerSat = null
    }

    /**
     * Compare a detected person crop's clothing colors against the target.
     *
     * @param crop RGB [Bitmap] of a detected person.
     * @return Correlation score (0-1). Higher = more similar clothing colors.
     *         Returns 0.0 if no target is set.
     */
    fun compare(crop: Bitmap): Float {
        val tUH = targetUpperHue ?: return 0.0f
        val tUS = targetUpperSat ?: return 0.0f
        val tLH = targetLowerHue ?: return 0.0f
        val tLS = targetLowerSat ?: return 0.0f

        return try {
            val (upperHue, upperSat) = computeRegionHistograms(crop, UPPER_START, UPPER_END)
            val (lowerHue, lowerSat) = computeRegionHistograms(crop, LOWER_START, LOWER_END)

            // Correlation for each histogram pair
            val upperHueCorr = histogramCorrelation(tUH, upperHue)
            val upperSatCorr = histogramCorrelation(tUS, upperSat)
            val lowerHueCorr = histogramCorrelation(tLH, lowerHue)
            val lowerSatCorr = histogramCorrelation(tLS, lowerSat)

            // Average across all four histograms, clamped to [0, 1]
            val avg = (upperHueCorr + upperSatCorr + lowerHueCorr + lowerSatCorr) / 4f
            avg.coerceIn(0f, 1f)
        } catch (e: Exception) {
            Log.w(TAG, "Clothing color comparison failed", e)
            0.0f
        }
    }

    /**
     * Compute normalized HSV histograms for a vertical region of the image.
     *
     * @param image Source bitmap.
     * @param startFraction Top of the region as a fraction of image height (0-1).
     * @param endFraction Bottom of the region as a fraction of image height (0-1).
     * @return Pair of (hue histogram, saturation histogram), both normalized.
     */
    private fun computeRegionHistograms(
        image: Bitmap,
        startFraction: Float,
        endFraction: Float,
    ): Pair<FloatArray, FloatArray> {
        val w = image.width
        val h = image.height
        val yStart = (h * startFraction).toInt().coerceAtLeast(0)
        val yEnd = (h * endFraction).toInt().coerceAtMost(h)

        val hueHist = FloatArray(HUE_BINS)
        val satHist = FloatArray(SAT_BINS)
        val hsv = FloatArray(3)
        var count = 0

        for (y in yStart until yEnd) {
            for (x in 0 until w) {
                val pixel = image.getPixel(x, y)
                val r = Color.red(pixel)
                val g = Color.green(pixel)
                val b = Color.blue(pixel)

                Color.RGBToHSV(r, g, b, hsv)

                // H is 0-360, S is 0-1, V is 0-1
                val hueBin = ((hsv[0] / 360f) * HUE_BINS).toInt().coerceIn(0, HUE_BINS - 1)
                val satBin = (hsv[1] * SAT_BINS).toInt().coerceIn(0, SAT_BINS - 1)

                hueHist[hueBin] += 1f
                satHist[satBin] += 1f
                count++
            }
        }

        // Normalize
        if (count > 0) {
            val countF = count.toFloat()
            for (i in hueHist.indices) hueHist[i] /= countF
            for (i in satHist.indices) satHist[i] /= countF
        }

        return Pair(hueHist, satHist)
    }

    /**
     * Compute Pearson correlation coefficient between two histograms.
     *
     * Returns a value in [-1, 1]; we clamp negatives to 0 since
     * anti-correlation is meaningless for clothing similarity.
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
