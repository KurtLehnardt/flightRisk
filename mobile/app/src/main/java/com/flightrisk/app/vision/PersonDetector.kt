package com.flightrisk.app.vision

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.Log
import com.flightrisk.app.config.FlightRiskConfig
import com.flightrisk.app.model.OnnxModelLoader
import java.io.Closeable
import java.nio.FloatBuffer

/**
 * YOLO-based person detector using ONNX Runtime.
 *
 * Port of `flightrisk/vision/detector.py`. Loads a YOLO11n ONNX model
 * from assets, runs inference on camera frames, and returns bounding
 * boxes for detected persons (COCO class 0).
 *
 * @param context Android context for loading model from assets.
 * @param modelAsset ONNX model filename in assets (default "yolo11n.onnx").
 * @param confidence Minimum confidence threshold (default from config).
 * @param iouThreshold IoU threshold for NMS (default 0.45).
 */
class PersonDetector(
    context: Context,
    modelAsset: String = "yolo11n.onnx",
    private val confidence: Float = FlightRiskConfig.getInstance(context)
        .vision.detectorConfidence.toFloat(),
    private val iouThreshold: Float = 0.45f,
) : Closeable {

    private val loader = OnnxModelLoader(context, modelAsset)
    private val environment: OrtEnvironment = loader.environment

    companion object {
        private const val TAG = "PersonDetector"
        private const val INPUT_SIZE = 640
        private const val PERSON_CLASS_ID = 0
    }

    /**
     * Detect persons in a camera frame.
     *
     * @param frame RGB [Bitmap] from the camera.
     * @return List of [Detection] objects for each detected person.
     */
    fun detect(frame: Bitmap): List<Detection> {
        // Preprocessing: letterbox resize, normalize, NCHW
        val (inputTensor, scaleX, scaleY, padX, padY) = preprocess(frame)

        // Run inference
        val results = loader.runInference(inputTensor)
        inputTensor.close()

        // Parse YOLO output and apply NMS
        val detections = postprocess(results, frame, scaleX, scaleY, padX, padY)
        results.close()

        return detections
    }

    /**
     * Draw bounding boxes on a frame.
     *
     * @param frame The original frame.
     * @param detections Output from [detect].
     * @param matchIdx Index of the matched person (drawn in green, others in blue).
     * @return Annotated frame copy.
     */
    fun annotate(
        frame: Bitmap,
        detections: List<Detection>,
        matchIdx: Int? = null,
    ): Bitmap {
        val annotated = frame.copy(frame.config, true)
        val canvas = Canvas(annotated)

        val matchPaint = Paint().apply {
            color = Color.GREEN
            strokeWidth = 6f
            style = Paint.Style.STROKE
        }
        val defaultPaint = Paint().apply {
            color = Color.rgb(0, 180, 255)
            strokeWidth = 4f
            style = Paint.Style.STROKE
        }
        val textPaint = Paint().apply {
            color = Color.WHITE
            textSize = 36f
            isAntiAlias = true
        }

        for ((i, det) in detections.withIndex()) {
            val (x1, y1, x2, y2) = det.bbox
            val isMatch = matchIdx != null && i == matchIdx
            val paint = if (isMatch) matchPaint else defaultPaint

            canvas.drawRect(
                x1.toFloat(), y1.toFloat(),
                x2.toFloat(), y2.toFloat(),
                paint,
            )

            val label = if (isMatch) {
                "MATCH ${(det.confidence * 100).toInt()}%"
            } else {
                "${(det.confidence * 100).toInt()}%"
            }
            textPaint.color = if (isMatch) Color.GREEN else Color.rgb(0, 180, 255)
            canvas.drawText(label, x1.toFloat(), (y1 - 10).toFloat(), textPaint)
        }

        return annotated
    }

    /**
     * Letterbox-resize, normalize, and convert to NCHW float tensor.
     *
     * Returns the tensor and the transform parameters needed to map
     * output coordinates back to the original frame.
     */
    private fun preprocess(frame: Bitmap): PreprocessResult {
        val srcW = frame.width
        val srcH = frame.height

        // Compute letterbox scale (fit within INPUT_SIZE x INPUT_SIZE)
        val scale = minOf(
            INPUT_SIZE.toFloat() / srcW,
            INPUT_SIZE.toFloat() / srcH,
        )
        val newW = (srcW * scale).toInt()
        val newH = (srcH * scale).toInt()
        val padX = (INPUT_SIZE - newW) / 2
        val padY = (INPUT_SIZE - newH) / 2

        // Resize the frame
        val resized = Bitmap.createScaledBitmap(frame, newW, newH, true)

        // Create letterboxed bitmap with gray padding (114, 114, 114)
        val letterboxed = Bitmap.createBitmap(INPUT_SIZE, INPUT_SIZE, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(letterboxed)
        canvas.drawColor(Color.rgb(114, 114, 114))
        canvas.drawBitmap(resized, padX.toFloat(), padY.toFloat(), null)
        if (resized !== frame) resized.recycle()

        // Convert to NCHW float array, normalized to 0-1
        // Shape: [1, 3, 640, 640]
        val pixels = IntArray(INPUT_SIZE * INPUT_SIZE)
        letterboxed.getPixels(pixels, 0, INPUT_SIZE, 0, 0, INPUT_SIZE, INPUT_SIZE)
        letterboxed.recycle()

        val floatBuffer = FloatBuffer.allocate(1 * 3 * INPUT_SIZE * INPUT_SIZE)
        val channelSize = INPUT_SIZE * INPUT_SIZE

        // NCHW layout: R channel, then G channel, then B channel
        for (i in pixels.indices) {
            val pixel = pixels[i]
            floatBuffer.put(i, Color.red(pixel) / 255f)                    // R
            floatBuffer.put(channelSize + i, Color.green(pixel) / 255f)    // G
            floatBuffer.put(2 * channelSize + i, Color.blue(pixel) / 255f) // B
        }

        val shape = longArrayOf(1, 3, INPUT_SIZE.toLong(), INPUT_SIZE.toLong())
        val tensor = OnnxTensor.createTensor(environment, floatBuffer, shape)

        return PreprocessResult(tensor, scale, scale, padX, padY)
    }

    /**
     * Parse YOLO output tensor, apply confidence filtering and NMS.
     *
     * YOLO11 output shape is [1, 84, N] where 84 = 4 bbox coords + 80
     * class scores, and N = number of candidate detections.
     */
    private fun postprocess(
        results: ai.onnxruntime.OrtSession.Result,
        frame: Bitmap,
        scaleX: Float,
        scaleY: Float,
        padX: Int,
        padY: Int,
    ): List<Detection> {
        // Output tensor: shape [1, 84, N]
        @Suppress("UNCHECKED_CAST")
        val outputArray = results[0].value as Array<Array<FloatArray>>
        val output = outputArray[0] // [84, N]
        val numDetections = output[0].size

        data class RawDet(val x1: Int, val y1: Int, val x2: Int, val y2: Int, val conf: Float)
        val rawDetections = mutableListOf<RawDet>()

        for (i in 0 until numDetections) {
            // Person class score is at index 4 (first class after 4 bbox coords)
            val personConf = output[4 + PERSON_CLASS_ID][i]
            if (personConf < confidence) continue

            // Bbox: center_x, center_y, width, height (in letterboxed coords)
            val cx = output[0][i]
            val cy = output[1][i]
            val w = output[2][i]
            val h = output[3][i]

            // Convert to corner coords and undo letterbox transform
            val x1 = ((cx - w / 2 - padX) / scaleX).toInt().coerceAtLeast(0)
            val y1 = ((cy - h / 2 - padY) / scaleY).toInt().coerceAtLeast(0)
            val x2 = ((cx + w / 2 - padX) / scaleX).toInt().coerceAtMost(frame.width)
            val y2 = ((cy + h / 2 - padY) / scaleY).toInt().coerceAtMost(frame.height)

            if (x2 > x1 && y2 > y1) {
                rawDetections.add(RawDet(x1, y1, x2, y2, personConf))
            }
        }

        // Apply NMS
        val nmsIndices = applyNms(rawDetections.map {
            floatArrayOf(it.x1.toFloat(), it.y1.toFloat(), it.x2.toFloat(), it.y2.toFloat())
        }, rawDetections.map { it.conf })

        val detections = mutableListOf<Detection>()
        for (idx in nmsIndices) {
            val raw = rawDetections[idx]
            val crop = Bitmap.createBitmap(
                frame,
                raw.x1.coerceAtLeast(0),
                raw.y1.coerceAtLeast(0),
                (raw.x2 - raw.x1).coerceAtMost(frame.width - raw.x1),
                (raw.y2 - raw.y1).coerceAtMost(frame.height - raw.y1),
            )
            detections.add(
                Detection(
                    bbox = intArrayOf(raw.x1, raw.y1, raw.x2, raw.y2),
                    confidence = raw.conf,
                    crop = crop,
                )
            )
        }

        Log.d(TAG, "Detected ${detections.size} persons (${rawDetections.size} pre-NMS)")
        return detections
    }

    /**
     * Greedy NMS: suppress overlapping boxes by IoU.
     */
    private fun applyNms(
        boxes: List<FloatArray>,
        scores: List<Float>,
    ): List<Int> {
        if (boxes.isEmpty()) return emptyList()

        val indices = scores.indices.sortedByDescending { scores[it] }
        val keep = mutableListOf<Int>()
        val suppressed = BooleanArray(boxes.size)

        for (i in indices) {
            if (suppressed[i]) continue
            keep.add(i)
            for (j in indices) {
                if (suppressed[j] || i == j) continue
                if (computeIou(boxes[i], boxes[j]) > iouThreshold) {
                    suppressed[j] = true
                }
            }
        }

        return keep
    }

    /**
     * Compute IoU between two boxes [x1, y1, x2, y2].
     */
    private fun computeIou(box1: FloatArray, box2: FloatArray): Float {
        val x1 = maxOf(box1[0], box2[0])
        val y1 = maxOf(box1[1], box2[1])
        val x2 = minOf(box1[2], box2[2])
        val y2 = minOf(box1[3], box2[3])
        val intersection = maxOf(0f, x2 - x1) * maxOf(0f, y2 - y1)
        val area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
        val area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
        val union = area1 + area2 - intersection
        return if (union > 0f) intersection / union else 0f
    }

    override fun close() {
        loader.close()
    }

    private data class PreprocessResult(
        val tensor: OnnxTensor,
        val scaleX: Float,
        val scaleY: Float,
        val padX: Int,
        val padY: Int,
    )
}
