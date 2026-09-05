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
 * Face recognition using ArcFace embeddings.
 *
 * Port of `flightrisk/vision/face.py`. Uses a lightweight face detector
 * (SCRFD-500M) for face detection/alignment, then ArcFace (MobileFaceNet)
 * for 512-d face embedding extraction. Complements full-body ReID for
 * higher confidence matching, especially when clothing changes.
 *
 * Both models are loaded from ONNX assets.
 *
 * @param context Android context for loading models from assets.
 * @param faceDetAsset ONNX model filename for face detection (default "scrfd_500m.onnx").
 * @param faceRecAsset ONNX model filename for face recognition (default "arcface_mobilefacenet.onnx").
 * @param matchThreshold Cosine similarity threshold for a face match.
 * @param detSize Detection input size (width, height).
 */
class FaceRecognizer(
    context: Context,
    faceDetAsset: String = "scrfd_500m.onnx",
    faceRecAsset: String = "arcface_mobilefacenet.onnx",
    private val matchThreshold: Float = FlightRiskConfig.getInstance(context)
        .vision.faceMatchThreshold.toFloat(),
    private val detSize: Pair<Int, Int> = FlightRiskConfig.getInstance(context)
        .vision.faceDetSize,
) : Closeable {

    private val detLoader = OnnxModelLoader(context, faceDetAsset)
    private val recLoader = OnnxModelLoader(context, faceRecAsset)
    private val environment: OrtEnvironment = detLoader.environment
    private var targetEmbedding: FloatArray? = null

    companion object {
        private const val TAG = "FaceRecognizer"
        private const val ARCFACE_INPUT_SIZE = 112
        private const val FACE_DET_CONF_THRESHOLD = 0.5f
    }

    /** Whether a target face embedding is currently set. */
    val hasTarget: Boolean
        get() = targetEmbedding != null

    /**
     * Set the reference face from a photo of the target.
     *
     * Detects the largest face, extracts and stores its embedding.
     *
     * @param photo RGB [Bitmap] containing the target's face.
     * @return true if a face was found and embedding set, false otherwise.
     */
    fun setTarget(photo: Bitmap): Boolean {
        return try {
            val emb = bestFaceEmbedding(photo)
            if (emb != null) {
                targetEmbedding = emb
                Log.d(TAG, "Target face embedding set (${emb.size}-d)")
                true
            } else {
                Log.w(TAG, "No face detected in reference photo")
                false
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to set target face", e)
            false
        }
    }

    /** Clear the current target embedding. */
    fun clearTarget() {
        targetEmbedding = null
    }

    /**
     * Compare a detected person crop's face against the target.
     *
     * @param crop RGB [Bitmap] of a detected person.
     * @return Cosine similarity (0-1), or 0.0 if no face found or no target set.
     */
    fun compare(crop: Bitmap): Float {
        val target = targetEmbedding ?: return 0.0f
        return try {
            val emb = bestFaceEmbedding(crop) ?: return 0.0f
            val similarity = dotProduct(target, emb)
            maxOf(0f, similarity) // clamp to 0-1
        } catch (e: Exception) {
            Log.w(TAG, "Face comparison failed", e)
            0.0f
        }
    }

    /**
     * Find the best face match among detected persons.
     *
     * @param detections List of [Detection] from [PersonDetector].
     * @return (index, score) of best face match, or (null, 0.0) if none.
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
     * Extract the raw face embedding for a person crop.
     *
     * @param crop RGB [Bitmap] of a detected person.
     * @return Normalized 512-d face embedding, or null if no face found.
     */
    fun extractEmbedding(crop: Bitmap): FloatArray? {
        return try {
            bestFaceEmbedding(crop)
        } catch (e: Exception) {
            Log.w(TAG, "Face embedding extraction failed", e)
            null
        }
    }

    /**
     * Detect faces in the image, pick the largest, and return its embedding.
     *
     * Pipeline: face detection (SCRFD) -> crop largest face -> resize to
     * 112x112 -> ArcFace normalization -> ONNX inference -> L2-normalize.
     */
    private fun bestFaceEmbedding(image: Bitmap): FloatArray? {
        val faces = detectFaces(image)
        if (faces.isEmpty()) return null

        // Pick the largest face by bounding box area
        val best = faces.maxByOrNull { (it[2] - it[0]) * (it[3] - it[1]) } ?: return null

        // Crop and align face to 112x112 for ArcFace
        val x1 = best[0].coerceAtLeast(0)
        val y1 = best[1].coerceAtLeast(0)
        val x2 = best[2].coerceAtMost(image.width)
        val y2 = best[3].coerceAtMost(image.height)
        val w = x2 - x1
        val h = y2 - y1
        if (w <= 0 || h <= 0) return null

        val faceCrop = Bitmap.createBitmap(image, x1, y1, w, h)
        val aligned = Bitmap.createScaledBitmap(faceCrop, ARCFACE_INPUT_SIZE, ARCFACE_INPUT_SIZE, true)
        if (faceCrop !== image) faceCrop.recycle()

        // Extract embedding via ArcFace
        val embedding = arcfaceEmbedding(aligned)
        aligned.recycle()

        return l2Normalize(embedding)
    }

    /**
     * Run face detection on the image.
     *
     * Uses SCRFD-500M ONNX model to detect face bounding boxes.
     *
     * @return List of face bounding boxes as [x1, y1, x2, y2] int arrays.
     */
    private fun detectFaces(image: Bitmap): List<IntArray> {
        val detW = detSize.first
        val detH = detSize.second

        val resized = Bitmap.createScaledBitmap(image, detW, detH, true)
        val scaleX = image.width.toFloat() / detW
        val scaleY = image.height.toFloat() / detH

        // Convert to NCHW normalized float tensor
        val pixels = IntArray(detW * detH)
        resized.getPixels(pixels, 0, detW, 0, 0, detW, detH)
        resized.recycle()

        val floatBuffer = FloatBuffer.allocate(1 * 3 * detW * detH)
        val channelSize = detW * detH

        for (i in pixels.indices) {
            val pixel = pixels[i]
            // SCRFD uses simple mean subtraction (127.5) and scale (1/128)
            floatBuffer.put(i, (Color.red(pixel) - 127.5f) / 128f)
            floatBuffer.put(channelSize + i, (Color.green(pixel) - 127.5f) / 128f)
            floatBuffer.put(2 * channelSize + i, (Color.blue(pixel) - 127.5f) / 128f)
        }

        val shape = longArrayOf(1, 3, detH.toLong(), detW.toLong())
        val tensor = OnnxTensor.createTensor(environment, floatBuffer, shape)

        val result = detLoader.runInference(tensor)
        tensor.close()

        // Parse SCRFD output: boxes and scores
        // SCRFD outputs vary by stride; we parse generically
        val faces = mutableListOf<IntArray>()
        try {
            // Try to parse as [1, N, 5] format (boxes with scores)
            @Suppress("UNCHECKED_CAST")
            val boxes = result[0].value as Array<Array<FloatArray>>
            val detections = boxes[0]
            for (det in detections) {
                if (det.size >= 5 && det[4] >= FACE_DET_CONF_THRESHOLD) {
                    val x1 = (det[0] * scaleX).toInt().coerceAtLeast(0)
                    val y1 = (det[1] * scaleY).toInt().coerceAtLeast(0)
                    val x2 = (det[2] * scaleX).toInt().coerceAtMost(image.width)
                    val y2 = (det[3] * scaleY).toInt().coerceAtMost(image.height)
                    if (x2 > x1 && y2 > y1) {
                        faces.add(intArrayOf(x1, y1, x2, y2))
                    }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Face detection output parsing failed", e)
        }

        result.close()
        return faces
    }

    /**
     * Extract ArcFace embedding from a 112x112 aligned face image.
     */
    private fun arcfaceEmbedding(alignedFace: Bitmap): FloatArray {
        val size = ARCFACE_INPUT_SIZE
        val pixels = IntArray(size * size)
        alignedFace.getPixels(pixels, 0, size, 0, 0, size, size)

        val floatBuffer = FloatBuffer.allocate(1 * 3 * size * size)
        val channelSize = size * size

        for (i in pixels.indices) {
            val pixel = pixels[i]
            // ArcFace normalization: (pixel - 127.5) / 127.5 -> range [-1, 1]
            floatBuffer.put(i, (Color.red(pixel) - 127.5f) / 127.5f)
            floatBuffer.put(channelSize + i, (Color.green(pixel) - 127.5f) / 127.5f)
            floatBuffer.put(2 * channelSize + i, (Color.blue(pixel) - 127.5f) / 127.5f)
        }

        val shape = longArrayOf(1, 3, size.toLong(), size.toLong())
        val tensor = OnnxTensor.createTensor(environment, floatBuffer, shape)

        val result = recLoader.runInference(tensor)
        tensor.close()

        @Suppress("UNCHECKED_CAST")
        val outputArray = result[0].value as Array<FloatArray>
        val embedding = outputArray[0].clone()
        result.close()

        return embedding
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
        detLoader.close()
        recLoader.close()
    }
}
