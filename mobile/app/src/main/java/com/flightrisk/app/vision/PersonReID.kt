package com.flightrisk.app.vision

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import android.util.Log
import com.flightrisk.app.config.FlightRiskConfig
import com.flightrisk.app.model.OnnxModelLoader
import java.io.Closeable
import java.nio.FloatBuffer
import kotlin.math.sqrt

/**
 * Person Re-Identification using CLIP visual embeddings.
 *
 * Port of `flightrisk/vision/reid.py`. Loads a CLIP ViT-B/32 or
 * MobileCLIP ONNX model, extracts 512-d appearance embeddings from
 * person crops, and compares them against a target reference photo
 * via cosine similarity.
 *
 * @param context Android context for loading model from assets.
 * @param modelAsset ONNX model filename in assets (default "clip_visual.onnx").
 * @param matchThreshold Cosine similarity threshold for a positive match.
 */
class PersonReID(
    context: Context,
    modelAsset: String = "clip_visual.onnx",
    private val matchThreshold: Float = FlightRiskConfig.getInstance(context)
        .vision.reidThreshold.toFloat(),
) : Closeable {

    private val loader = OnnxModelLoader(context, modelAsset)
    private val environment: OrtEnvironment = loader.environment
    private var targetEmbedding: FloatArray? = null

    companion object {
        private const val TAG = "PersonReID"
        private const val INPUT_SIZE = 224
        private const val RESIZE_SIZE = 256

        // CLIP normalization constants (ImageNet-derived)
        private val MEAN = floatArrayOf(0.48145466f, 0.4578275f, 0.40821073f)
        private val STD = floatArrayOf(0.26862954f, 0.26130258f, 0.27577711f)
    }

    /**
     * Set the reference image of the person to find.
     *
     * Computes and stores the target embedding for later comparison.
     *
     * @param photo RGB [Bitmap] of the target person.
     */
    fun setTarget(photo: Bitmap) {
        targetEmbedding = extractEmbedding(photo)
        Log.d(TAG, "Target embedding set (${targetEmbedding?.size}-d)")
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
     * @param crop RGB [Bitmap] of a detected person.
     * @return Cosine similarity score (0-1). Higher = more similar.
     *         Returns 0.0 if no target is set.
     */
    fun compare(crop: Bitmap): Float {
        val target = targetEmbedding ?: return 0.0f
        val embedding = extractEmbedding(crop)
        val similarity = dotProduct(target, embedding)
        return maxOf(0f, similarity) // clamp to 0-1
    }

    /**
     * Find the best match among detected persons.
     *
     * @param detections List of [Detection] from [PersonDetector].
     * @return (index, score) of best match, or (null, 0.0) if no match
     *         exceeds the threshold.
     */
    fun findMatch(detections: List<Detection>): Pair<Int?, Float> {
        val target = targetEmbedding
        if (target == null || detections.isEmpty()) return Pair(null, 0.0f)

        var bestIdx: Int? = null
        var bestScore = 0.0f

        for ((i, det) in detections.withIndex()) {
            val score = compare(det.crop)
            if (score > bestScore) {
                bestScore = score
                bestIdx = i
            }
        }

        return if (bestScore >= matchThreshold) {
            Pair(bestIdx, bestScore)
        } else {
            Pair(null, bestScore)
        }
    }

    /**
     * Extract the raw appearance embedding for a person crop.
     *
     * Unlike [compare], this does not require a target to be set.
     * Used by callers that need the raw feature vector (e.g. to send
     * it to a ground station for remote matching).
     *
     * @param crop RGB [Bitmap] of a detected person.
     * @return Normalized 512-d feature vector, or null if extraction fails.
     */
    fun extractEmbeddingSafe(crop: Bitmap): FloatArray? {
        return try {
            extractEmbedding(crop)
        } catch (e: Exception) {
            Log.w(TAG, "ReID embedding extraction failed", e)
            null
        }
    }

    /**
     * Preprocess a person crop and extract its CLIP embedding.
     *
     * Pipeline: resize to 256 -> center-crop to 224x224 -> normalize
     * with CLIP means/stds -> NCHW -> ONNX inference -> L2-normalize.
     */
    private fun extractEmbedding(image: Bitmap): FloatArray {
        // Resize shorter side to 256, maintaining aspect ratio
        val (resizeW, resizeH) = if (image.width < image.height) {
            Pair(RESIZE_SIZE, (RESIZE_SIZE.toFloat() * image.height / image.width).toInt())
        } else {
            Pair((RESIZE_SIZE.toFloat() * image.width / image.height).toInt(), RESIZE_SIZE)
        }
        val resized = Bitmap.createScaledBitmap(image, resizeW, resizeH, true)

        // Center crop to 224x224
        val cropX = (resized.width - INPUT_SIZE) / 2
        val cropY = (resized.height - INPUT_SIZE) / 2
        val cropped = Bitmap.createBitmap(resized, cropX, cropY, INPUT_SIZE, INPUT_SIZE)
        if (resized !== image) resized.recycle()

        // Convert to NCHW float tensor with CLIP normalization
        val pixels = IntArray(INPUT_SIZE * INPUT_SIZE)
        cropped.getPixels(pixels, 0, INPUT_SIZE, 0, 0, INPUT_SIZE, INPUT_SIZE)
        cropped.recycle()

        val floatBuffer = FloatBuffer.allocate(1 * 3 * INPUT_SIZE * INPUT_SIZE)
        val channelSize = INPUT_SIZE * INPUT_SIZE

        for (i in pixels.indices) {
            val pixel = pixels[i]
            floatBuffer.put(i, (Color.red(pixel) / 255f - MEAN[0]) / STD[0])                    // R
            floatBuffer.put(channelSize + i, (Color.green(pixel) / 255f - MEAN[1]) / STD[1])     // G
            floatBuffer.put(2 * channelSize + i, (Color.blue(pixel) / 255f - MEAN[2]) / STD[2])  // B
        }

        val shape = longArrayOf(1, 3, INPUT_SIZE.toLong(), INPUT_SIZE.toLong())
        val tensor = OnnxTensor.createTensor(environment, floatBuffer, shape)

        val result = loader.runInference(tensor)
        tensor.close()

        // Extract embedding from output
        @Suppress("UNCHECKED_CAST")
        val outputArray = result[0].value as Array<FloatArray>
        val embedding = outputArray[0].clone()
        result.close()

        // L2 normalize
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

    /** Dot product of two vectors (cosine similarity for unit vectors). */
    private fun dotProduct(a: FloatArray, b: FloatArray): Float {
        var sum = 0f
        for (i in a.indices) sum += a[i] * b[i]
        return sum
    }

    override fun close() {
        loader.close()
    }
}
