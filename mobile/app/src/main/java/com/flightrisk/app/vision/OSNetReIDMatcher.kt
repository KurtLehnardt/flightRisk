package com.flightrisk.app.vision

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import android.util.Log
import java.io.Closeable
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.sqrt

/**
 * Person Re-Identification using OSNet (Omni-Scale Network).
 *
 * Loads an OSNet x1.0 TFLite model from assets and produces 512-d person
 * appearance embeddings. OSNet is specifically designed for person ReID
 * (unlike CLIP which is general-purpose), so it typically outperforms
 * CLIP on re-identification tasks.
 *
 * Gracefully handles a missing model file by reporting [isAvailable] = false
 * and returning 0.0 for all comparisons.
 *
 * Signal name: `osnet_reid`
 * Default weight: 0.30
 *
 * @param context Android context for loading model from assets.
 * @param modelAsset TFLite model filename in assets (default "osnet_x1_0.tflite").
 */
class OSNetReIDMatcher(
    context: Context,
    private val modelAsset: String = "osnet_x1_0.tflite",
) : Closeable {

    companion object {
        private const val TAG = "OSNetReIDMatcher"

        /** OSNet input: 256x128 (height x width). */
        private const val INPUT_HEIGHT = 256
        private const val INPUT_WIDTH = 128

        /** OSNet output embedding dimension. */
        private const val EMBEDDING_DIM = 512

        /** ImageNet normalization constants. */
        private val MEAN = floatArrayOf(0.485f, 0.456f, 0.406f)
        private val STD = floatArrayOf(0.229f, 0.224f, 0.225f)
    }

    /** Whether the TFLite model was successfully loaded. */
    var isAvailable: Boolean = false
        private set

    private var interpreter: org.tensorflow.lite.Interpreter? = null
    private var targetEmbedding: FloatArray? = null

    init {
        try {
            val modelBuffer = context.assets.open(modelAsset).use { input ->
                val bytes = input.readBytes()
                ByteBuffer.allocateDirect(bytes.size).apply {
                    order(ByteOrder.nativeOrder())
                    put(bytes)
                    rewind()
                }
            }
            interpreter = org.tensorflow.lite.Interpreter(modelBuffer)
            isAvailable = true
            Log.i(TAG, "OSNet model loaded successfully from $modelAsset")
        } catch (e: Exception) {
            Log.w(TAG, "OSNet model not available: ${e.message}")
            isAvailable = false
        }
    }

    /** Whether a target embedding is currently set. */
    val hasTarget: Boolean
        get() = targetEmbedding != null

    /**
     * Set the reference image of the person to find.
     *
     * @param photo RGB [Bitmap] of the target person.
     */
    fun setTarget(photo: Bitmap) {
        if (!isAvailable) return
        targetEmbedding = extractEmbedding(photo)
        if (targetEmbedding != null) {
            Log.d(TAG, "Target OSNet embedding set (${targetEmbedding?.size}-d)")
        }
    }

    /** Clear the current target embedding. */
    fun clearTarget() {
        targetEmbedding = null
    }

    /**
     * Compare a detected person crop against the target.
     *
     * @param crop RGB [Bitmap] of a detected person.
     * @return Cosine similarity score (0-1). Higher = more similar.
     *         Returns 0.0 if model not available or no target set.
     */
    fun compare(crop: Bitmap): Float {
        if (!isAvailable) return 0.0f
        val target = targetEmbedding ?: return 0.0f

        return try {
            val embedding = extractEmbedding(crop) ?: return 0.0f
            val similarity = dotProduct(target, embedding)
            maxOf(0f, similarity) // clamp to [0, 1]
        } catch (e: Exception) {
            Log.w(TAG, "OSNet comparison failed", e)
            0.0f
        }
    }

    /**
     * Preprocess a person crop and extract its OSNet embedding.
     *
     * Pipeline: resize to 256x128 -> normalize with ImageNet mean/std
     * -> NCHW float buffer -> TFLite inference -> L2-normalize.
     */
    private fun extractEmbedding(image: Bitmap): FloatArray? {
        val interp = interpreter ?: return null

        // Resize to 128x256 (width x height)
        val resized = Bitmap.createScaledBitmap(image, INPUT_WIDTH, INPUT_HEIGHT, true)

        // Convert to NHWC float buffer with ImageNet normalization
        val inputBuffer = ByteBuffer.allocateDirect(
            1 * INPUT_HEIGHT * INPUT_WIDTH * 3 * 4
        ).apply {
            order(ByteOrder.nativeOrder())
        }

        for (y in 0 until INPUT_HEIGHT) {
            for (x in 0 until INPUT_WIDTH) {
                val pixel = resized.getPixel(x, y)
                inputBuffer.putFloat((Color.red(pixel) / 255f - MEAN[0]) / STD[0])
                inputBuffer.putFloat((Color.green(pixel) / 255f - MEAN[1]) / STD[1])
                inputBuffer.putFloat((Color.blue(pixel) / 255f - MEAN[2]) / STD[2])
            }
        }

        if (resized !== image) resized.recycle()
        inputBuffer.rewind()

        // Output: [1, 512]
        val outputBuffer = ByteBuffer.allocateDirect(1 * EMBEDDING_DIM * 4).apply {
            order(ByteOrder.nativeOrder())
        }

        try {
            interp.run(inputBuffer, outputBuffer)
        } catch (e: Exception) {
            Log.w(TAG, "OSNet inference failed", e)
            return null
        }

        outputBuffer.rewind()
        val embedding = FloatArray(EMBEDDING_DIM) { outputBuffer.float }

        return l2Normalize(embedding)
    }

    /** L2-normalize a vector in-place and return it. */
    private fun l2Normalize(vec: FloatArray): FloatArray {
        var sumSq = 0f
        for (v in vec) sumSq += v * v
        val norm = sqrt(sumSq)
        if (norm > 0f) {
            for (i in vec.indices) vec[i] /= norm
        }
        return vec
    }

    /** Dot product of two unit vectors (cosine similarity). */
    private fun dotProduct(a: FloatArray, b: FloatArray): Float {
        var sum = 0f
        for (i in a.indices) sum += a[i] * b[i]
        return sum
    }

    override fun close() {
        interpreter?.close()
        interpreter = null
    }
}
