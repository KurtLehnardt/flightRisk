package com.flightrisk.app.drone

import android.content.Context
import android.graphics.Bitmap
import android.util.Log
import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import java.nio.FloatBuffer

/**
 * Monocular depth-based obstacle avoidance using MiDaS Small.
 *
 * Ported from flightrisk/drone/obstacle.py — divides the depth map
 * into a 3×3 grid, checks the middle row for obstacles, and returns
 * an evasive action when the center cell depth is below a safe threshold.
 */
class ObstacleGuard(
    context: Context,
    private val minSafeDepth: Float = 0.35f,
) {

    companion object {
        private const val TAG = "ObstacleGuard"
        private const val MODEL_FILE = "midas_small.onnx"
        private const val INPUT_SIZE = 256
        private val MEAN = floatArrayOf(0.485f, 0.456f, 0.406f)
        private val STD = floatArrayOf(0.229f, 0.224f, 0.225f)
    }

    data class CheckResult(
        val safe: Boolean,
        val centerDepth: Float,
        val leftDepth: Float,
        val rightDepth: Float,
        val action: String,
        val confidence: Float,
    )

    private val env = OrtEnvironment.getEnvironment()
    private var session: OrtSession? = null

    init {
        try {
            val modelBytes = context.assets.open(MODEL_FILE).readBytes()
            session = env.createSession(modelBytes)
            Log.i(TAG, "MiDaS Small loaded (${modelBytes.size / 1024 / 1024} MB)")
        } catch (e: Exception) {
            Log.w(TAG, "MiDaS model not available: ${e.message}")
        }
    }

    val isAvailable: Boolean get() = session != null

    /**
     * Analyze a video frame for obstacles. Returns a [CheckResult] with
     * the recommended action.
     *
     * Matches the Python ObstacleGuard.check_path() logic:
     * - Resize frame to 256×256
     * - Run MiDaS inference to get depth map
     * - Normalize depth to 0..1
     * - Divide into 3×3 grid, check middle row
     * - If center < minSafeDepth → obstacle ahead
     */
    fun checkPath(frame: Bitmap): CheckResult {
        val sess = session ?: return CheckResult(
            safe = true, centerDepth = 1f, leftDepth = 1f,
            rightDepth = 1f, action = "clear", confidence = 0f,
        )

        val input = preprocessFrame(frame)
        val inputTensor = OnnxTensor.createTensor(env, input, longArrayOf(1, 3, INPUT_SIZE.toLong(), INPUT_SIZE.toLong()))

        val results = sess.run(mapOf("0" to inputTensor))
        val outputTensor = results[0] as OnnxTensor
        val depthMap = (outputTensor.value as Array<*>)[0] as FloatArray

        inputTensor.close()
        results.close()

        return analyzeDepthMap(depthMap)
    }

    private fun preprocessFrame(bitmap: Bitmap): FloatBuffer {
        val resized = Bitmap.createScaledBitmap(bitmap, INPUT_SIZE, INPUT_SIZE, true)
        val pixels = IntArray(INPUT_SIZE * INPUT_SIZE)
        resized.getPixels(pixels, 0, INPUT_SIZE, 0, 0, INPUT_SIZE, INPUT_SIZE)
        if (resized !== bitmap) resized.recycle()

        val buffer = FloatBuffer.allocate(3 * INPUT_SIZE * INPUT_SIZE)
        val channelSize = INPUT_SIZE * INPUT_SIZE

        for (i in pixels.indices) {
            val pixel = pixels[i]
            val r = ((pixel shr 16 and 0xFF) / 255f - MEAN[0]) / STD[0]
            val g = ((pixel shr 8 and 0xFF) / 255f - MEAN[1]) / STD[1]
            val b = ((pixel and 0xFF) / 255f - MEAN[2]) / STD[2]
            buffer.put(i, r)
            buffer.put(channelSize + i, g)
            buffer.put(2 * channelSize + i, b)
        }
        buffer.rewind()
        return buffer
    }

    private fun analyzeDepthMap(depthMap: FloatArray): CheckResult {
        // Normalize depth to 0..1
        var minVal = Float.MAX_VALUE
        var maxVal = Float.MIN_VALUE
        for (v in depthMap) {
            if (v < minVal) minVal = v
            if (v > maxVal) maxVal = v
        }
        val range = maxVal - minVal
        if (range < 1e-6f) {
            return CheckResult(
                safe = true, centerDepth = 1f, leftDepth = 1f,
                rightDepth = 1f, action = "clear", confidence = 0f,
            )
        }

        val normalized = FloatArray(depthMap.size) { (depthMap[it] - minVal) / range }

        // Divide into 3×3 grid, check middle row (row index 1)
        val gridH = INPUT_SIZE / 3
        val gridW = INPUT_SIZE / 3
        val midRowStart = gridH  // row 1 starts at gridH

        fun regionMean(colStart: Int, colEnd: Int): Float {
            var sum = 0f
            var count = 0
            for (r in midRowStart until midRowStart + gridH) {
                for (c in colStart until colEnd) {
                    sum += normalized[r * INPUT_SIZE + c]
                    count++
                }
            }
            return if (count > 0) sum / count else 1f
        }

        val leftDepth = regionMean(0, gridW)
        val centerDepth = regionMean(gridW, 2 * gridW)
        val rightDepth = regionMean(2 * gridW, INPUT_SIZE)

        val safe = centerDepth >= minSafeDepth

        val action = if (safe) {
            "clear"
        } else {
            when {
                leftDepth > rightDepth && leftDepth > minSafeDepth -> "go_left"
                rightDepth > leftDepth && rightDepth > minSafeDepth -> "go_right"
                else -> "reverse"
            }
        }

        val confidence = if (safe) centerDepth else 1f - centerDepth

        return CheckResult(
            safe = safe,
            centerDepth = centerDepth,
            leftDepth = leftDepth,
            rightDepth = rightDepth,
            action = action,
            confidence = confidence,
        )
    }

    fun close() {
        session?.close()
        session = null
    }
}
