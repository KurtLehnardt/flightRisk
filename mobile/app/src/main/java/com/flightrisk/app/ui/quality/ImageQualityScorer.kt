package com.flightrisk.app.ui.quality

import android.graphics.Bitmap
import android.graphics.Color
import android.media.FaceDetector
import kotlin.math.sqrt

/**
 * Quality report for a target reference photo.
 *
 * Port of `flightrisk/vision/quality.py` to Kotlin using Android's
 * native Bitmap API (no OpenCV dependency). Quality checks include
 * blur detection, brightness/contrast analysis, resolution validation,
 * and face presence detection.
 *
 * @property overallScore Quality score 0.0-1.0.
 * @property grade Letter grade A-F derived from [overallScore].
 * @property issues List of identified quality issues.
 * @property suggestions Actionable suggestions to improve photo quality.
 */
data class QualityReport(
    val overallScore: Float,
    val grade: String,
    val issues: List<String>,
    val suggestions: List<String>,
) {
    /**
     * Whether the photo quality is acceptable for use as a target.
     * Grade C or above is acceptable.
     */
    val isAcceptable: Boolean
        get() = grade in listOf("A", "B", "C")
}

/**
 * Scores the quality of a target reference photo for person ReID and
 * face matching.
 *
 * This is a Bitmap-based port of `flightrisk/vision/quality.py`. Since
 * OpenCV is not available on Android without a native library, blur
 * detection uses a simplified Laplacian-like edge variance metric
 * computed from pixel luminance differences.
 *
 * ## Scoring breakdown (each 0.0-1.0, averaged):
 * - **Blur**: variance of luminance gradients (Laplacian approximation)
 * - **Brightness**: penalizes too dark (mean < 50) or too bright (> 200)
 * - **Contrast**: standard deviation of pixel luminance
 * - **Resolution**: penalizes images below 200x200
 * - **Face**: bonus if Android FaceDetector finds at least one face
 */
object ImageQualityScorer {

    /** Minimum acceptable resolution in either dimension. */
    private const val MIN_RESOLUTION = 200

    /** Blur variance threshold below which the image is considered blurry. */
    private const val BLUR_THRESHOLD = 100f

    /**
     * Analyze a bitmap and return a [QualityReport].
     *
     * @param bitmap The target photo to analyze.
     * @return Quality report with score, grade, issues, and suggestions.
     */
    fun analyze(bitmap: Bitmap): QualityReport {
        val issues = mutableListOf<String>()
        val suggestions = mutableListOf<String>()

        val blurScore = analyzeBlur(bitmap)
        val brightnessScore = analyzeBrightness(bitmap)
        val contrastScore = analyzeContrast(bitmap)
        val resolutionScore = analyzeResolution(bitmap)
        val faceScore = analyzeFacePresence(bitmap)

        if (blurScore < 0.5f) {
            issues.add("Too blurry")
            suggestions.add("Use a sharper photo with the subject in focus")
        }
        if (brightnessScore < 0.5f) {
            issues.add("Poor lighting")
            suggestions.add("Use a photo taken in good lighting conditions")
        }
        if (contrastScore < 0.5f) {
            issues.add("Low contrast")
            suggestions.add("Use a photo with clear distinction between subject and background")
        }
        if (resolutionScore < 0.5f) {
            issues.add("Low resolution")
            suggestions.add("Use a higher-resolution photo (at least 200x200 pixels)")
        }
        if (faceScore < 0.5f) {
            issues.add("Face not detected")
            suggestions.add("Use a photo where the face is clearly visible and facing the camera")
        }

        // Weighted average: face detection is most important for matching
        val overallScore = (
            blurScore * 0.20f +
                brightnessScore * 0.15f +
                contrastScore * 0.15f +
                resolutionScore * 0.15f +
                faceScore * 0.35f
            )

        val grade = when {
            overallScore >= 0.85f -> "A"
            overallScore >= 0.70f -> "B"
            overallScore >= 0.50f -> "C"
            overallScore >= 0.30f -> "D"
            else -> "F"
        }

        return QualityReport(
            overallScore = overallScore,
            grade = grade,
            issues = issues,
            suggestions = suggestions,
        )
    }

    // -------------------------------------------------------------------
    // Blur detection (Laplacian approximation)
    // -------------------------------------------------------------------

    /**
     * Estimate image sharpness using a simplified Laplacian filter.
     *
     * Computes the variance of luminance second-derivatives by comparing
     * each interior pixel with its horizontal and vertical neighbors.
     * Higher variance indicates more edges (sharper image).
     *
     * @return Score 0.0-1.0 where 1.0 is sharp.
     */
    private fun analyzeBlur(bitmap: Bitmap): Float {
        // Downscale for performance (we only need the variance)
        val scaled = scaledBitmap(bitmap, 200)
        val w = scaled.width
        val h = scaled.height

        if (w < 3 || h < 3) return 0f

        val pixels = IntArray(w * h)
        scaled.getPixels(pixels, 0, w, 0, 0, w, h)

        var sum = 0.0
        var sumSq = 0.0
        var count = 0

        for (y in 1 until h - 1) {
            for (x in 1 until w - 1) {
                val idx = y * w + x
                val center = luminance(pixels[idx])
                val left = luminance(pixels[idx - 1])
                val right = luminance(pixels[idx + 1])
                val top = luminance(pixels[(y - 1) * w + x])
                val bottom = luminance(pixels[(y + 1) * w + x])

                // Laplacian: sum of neighbors minus 4*center
                val laplacian = (left + right + top + bottom - 4.0 * center)
                sum += laplacian
                sumSq += laplacian * laplacian
                count++
            }
        }

        if (count == 0) return 0f

        val mean = sum / count
        val variance = (sumSq / count) - (mean * mean)

        // Map variance to 0-1 score with BLUR_THRESHOLD as midpoint
        return (variance.toFloat() / (variance.toFloat() + BLUR_THRESHOLD)).coerceIn(0f, 1f)
    }

    // -------------------------------------------------------------------
    // Brightness analysis
    // -------------------------------------------------------------------

    /**
     * Score image brightness. Penalizes very dark (<50 mean luminance)
     * and very bright (>200 mean luminance) images.
     *
     * @return Score 0.0-1.0 where 1.0 is ideal brightness.
     */
    private fun analyzeBrightness(bitmap: Bitmap): Float {
        val scaled = scaledBitmap(bitmap, 100)
        val pixels = IntArray(scaled.width * scaled.height)
        scaled.getPixels(pixels, 0, scaled.width, 0, 0, scaled.width, scaled.height)

        val meanLum = pixels.map { luminance(it) }.average().toFloat()

        return when {
            meanLum < 30f -> 0.1f
            meanLum < 50f -> 0.3f + (meanLum - 30f) / 20f * 0.2f
            meanLum > 230f -> 0.1f
            meanLum > 200f -> 0.3f + (230f - meanLum) / 30f * 0.2f
            else -> {
                // Ideal range 50-200: score based on distance from midpoint
                val ideal = 125f
                val dist = kotlin.math.abs(meanLum - ideal) / 75f
                1f - dist * 0.3f
            }
        }
    }

    // -------------------------------------------------------------------
    // Contrast analysis
    // -------------------------------------------------------------------

    /**
     * Score image contrast using the standard deviation of luminance.
     *
     * @return Score 0.0-1.0 where 1.0 is good contrast.
     */
    private fun analyzeContrast(bitmap: Bitmap): Float {
        val scaled = scaledBitmap(bitmap, 100)
        val pixels = IntArray(scaled.width * scaled.height)
        scaled.getPixels(pixels, 0, scaled.width, 0, 0, scaled.width, scaled.height)

        val luminances = pixels.map { luminance(it).toDouble() }
        val mean = luminances.average()
        val variance = luminances.map { (it - mean) * (it - mean) }.average()
        val stdDev = sqrt(variance).toFloat()

        // stdDev < 20 is very flat; > 60 is good contrast
        return when {
            stdDev < 10f -> 0.1f
            stdDev < 20f -> 0.3f
            stdDev < 40f -> 0.5f + (stdDev - 20f) / 20f * 0.3f
            else -> (0.8f + (stdDev - 40f) / 40f * 0.2f).coerceAtMost(1f)
        }
    }

    // -------------------------------------------------------------------
    // Resolution check
    // -------------------------------------------------------------------

    /**
     * Score image resolution. Full score for 200x200+, degraded below.
     *
     * @return Score 0.0-1.0 where 1.0 meets minimum resolution.
     */
    private fun analyzeResolution(bitmap: Bitmap): Float {
        val minDim = minOf(bitmap.width, bitmap.height)
        return when {
            minDim >= MIN_RESOLUTION -> 1f
            minDim >= 100 -> 0.5f + (minDim - 100f) / 100f * 0.5f
            minDim >= 50 -> 0.2f + (minDim - 50f) / 50f * 0.3f
            else -> 0.1f
        }
    }

    // -------------------------------------------------------------------
    // Face presence check
    // -------------------------------------------------------------------

    /**
     * Check for face presence using Android's built-in [FaceDetector].
     *
     * Note: [FaceDetector] requires [Bitmap.Config.RGB_565] format.
     *
     * @return 1.0 if at least one face is detected, 0.0 otherwise.
     */
    private fun analyzeFacePresence(bitmap: Bitmap): Float {
        return try {
            val scaled = scaledBitmap(bitmap, 480)
            // FaceDetector requires even width
            val evenW = scaled.width and 0x7FFFFFFE
            val evenH = scaled.height and 0x7FFFFFFE
            if (evenW < 2 || evenH < 2) return 0f

            val cropped = if (evenW != scaled.width || evenH != scaled.height) {
                Bitmap.createBitmap(scaled, 0, 0, evenW, evenH)
            } else {
                scaled
            }

            val rgb565 = if (cropped.config != Bitmap.Config.RGB_565) {
                cropped.copy(Bitmap.Config.RGB_565, false) ?: return 0f
            } else {
                cropped
            }

            val detector = FaceDetector(rgb565.width, rgb565.height, 1)
            val faces = arrayOfNulls<FaceDetector.Face>(1)
            val count = detector.findFaces(rgb565, faces)

            if (rgb565 !== cropped) rgb565.recycle()
            if (cropped !== scaled) cropped.recycle()
            if (scaled !== bitmap) scaled.recycle()

            if (count > 0) 1f else 0f
        } catch (e: Exception) {
            0f
        }
    }

    // -------------------------------------------------------------------
    // Utilities
    // -------------------------------------------------------------------

    /**
     * Extract luminance (0-255) from an ARGB pixel value.
     */
    private fun luminance(pixel: Int): Float {
        val r = Color.red(pixel)
        val g = Color.green(pixel)
        val b = Color.blue(pixel)
        return 0.299f * r + 0.587f * g + 0.114f * b
    }

    /**
     * Scale a bitmap down so its longest side is at most [maxSize].
     * Returns the original bitmap if it is already small enough.
     */
    private fun scaledBitmap(bitmap: Bitmap, maxSize: Int): Bitmap {
        val maxDim = maxOf(bitmap.width, bitmap.height)
        if (maxDim <= maxSize) return bitmap

        val scale = maxSize.toFloat() / maxDim
        val newW = (bitmap.width * scale).toInt().coerceAtLeast(1)
        val newH = (bitmap.height * scale).toInt().coerceAtLeast(1)
        return Bitmap.createScaledBitmap(bitmap, newW, newH, true)
    }
}
