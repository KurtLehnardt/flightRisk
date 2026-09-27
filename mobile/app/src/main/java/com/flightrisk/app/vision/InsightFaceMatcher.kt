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
 * Face recognition using InsightFace R18 embeddings via TFLite.
 *
 * Loads an InsightFace ResNet-18 TFLite model from assets and produces
 * 512-d face embeddings. InsightFace R18 is a higher-quality face
 * recognition model than MobileFaceNet, providing better discrimination
 * at the cost of slightly more compute.
 *
 * Gracefully handles a missing model file by reporting [isAvailable] = false
 * and returning 0.0 for all comparisons. Face detection (to locate and crop
 * the face) is delegated to the caller or to [FaceRecognizer]'s detection
 * pipeline.
 *
 * Signal name: `insightface`
 * Default weight: 0.35
 *
 * @param context Android context for loading model from assets.
 * @param modelAsset TFLite model filename in assets (default "insightface_r18.tflite").
 */
class InsightFaceMatcher(
    context: Context,
    private val modelAsset: String = "insightface_r18.tflite",
) : Closeable {

    companion object {
        private const val TAG = "InsightFaceMatcher"

        /** InsightFace R18 input: 112x112 aligned face. */
        private const val INPUT_SIZE = 112

        /** Output embedding dimension. */
        private const val EMBEDDING_DIM = 512
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
            Log.i(TAG, "InsightFace R18 model loaded successfully from $modelAsset")
        } catch (e: Exception) {
            Log.w(TAG, "InsightFace R18 model not available: ${e.message}")
            isAvailable = false
        }
    }

    /** Whether a target face embedding is currently set. */
    val hasTarget: Boolean
        get() = targetEmbedding != null

    /**
     * Set the reference face from a photo of the target.
     *
     * The input should be a face crop (ideally already aligned). If the
     * photo is a full person image, the caller should detect and crop the
     * face first.
     *
     * @param faceCrop RGB [Bitmap] of the target's face (or full photo
     *   from which the largest face region will be center-cropped).
     */
    fun setTarget(faceCrop: Bitmap) {
        if (!isAvailable) return
        targetEmbedding = extractEmbedding(faceCrop)
        if (targetEmbedding != null) {
            Log.d(TAG, "Target InsightFace embedding set (${targetEmbedding?.size}-d)")
        }
    }

    /** Clear the current target embedding. */
    fun clearTarget() {
        targetEmbedding = null
    }

    /**
     * Compare a face crop against the target.
     *
     * @param faceCrop RGB [Bitmap] of a detected face (aligned to ~112x112).
     * @return Cosine similarity score (0-1). Higher = more similar.
     *         Returns 0.0 if model not available or no target set.
     */
    fun compare(faceCrop: Bitmap): Float {
        if (!isAvailable) return 0.0f
        val target = targetEmbedding ?: return 0.0f

        return try {
            val embedding = extractEmbedding(faceCrop) ?: return 0.0f
            val similarity = dotProduct(target, embedding)
            maxOf(0f, similarity) // clamp to [0, 1]
        } catch (e: Exception) {
            Log.w(TAG, "InsightFace comparison failed", e)
            0.0f
        }
    }

    /**
     * Extract the raw face embedding.
     *
     * @param faceCrop RGB [Bitmap] of a face.
     * @return Normalized 512-d embedding, or null if inference fails.
     */
    fun extractEmbeddingSafe(faceCrop: Bitmap): FloatArray? {
        if (!isAvailable) return null
        return try {
            extractEmbedding(faceCrop)
        } catch (e: Exception) {
            Log.w(TAG, "InsightFace embedding extraction failed", e)
            null
        }
    }

    /**
     * Preprocess a face image and extract its InsightFace embedding.
     *
     * Pipeline: resize to 112x112 -> normalize to [-1, 1]
     * -> NHWC float buffer -> TFLite inference -> L2-normalize.
     */
    private fun extractEmbedding(image: Bitmap): FloatArray? {
        val interp = interpreter ?: return null

        // Resize to 112x112
        val aligned = Bitmap.createScaledBitmap(image, INPUT_SIZE, INPUT_SIZE, true)

        // Convert to NHWC float buffer, normalized to [-1, 1]
        val inputBuffer = ByteBuffer.allocateDirect(
            1 * INPUT_SIZE * INPUT_SIZE * 3 * 4
        ).apply {
            order(ByteOrder.nativeOrder())
        }

        for (y in 0 until INPUT_SIZE) {
            for (x in 0 until INPUT_SIZE) {
                val pixel = aligned.getPixel(x, y)
                // InsightFace normalization: (pixel - 127.5) / 127.5 -> [-1, 1]
                inputBuffer.putFloat((Color.red(pixel) - 127.5f) / 127.5f)
                inputBuffer.putFloat((Color.green(pixel) - 127.5f) / 127.5f)
                inputBuffer.putFloat((Color.blue(pixel) - 127.5f) / 127.5f)
            }
        }

        if (aligned !== image) aligned.recycle()
        inputBuffer.rewind()

        // Output: [1, 512]
        val outputBuffer = ByteBuffer.allocateDirect(1 * EMBEDDING_DIM * 4).apply {
            order(ByteOrder.nativeOrder())
        }

        try {
            interp.run(inputBuffer, outputBuffer)
        } catch (e: Exception) {
            Log.w(TAG, "InsightFace inference failed", e)
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
