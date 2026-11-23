package com.flightrisk.app.vision

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import java.io.Closeable

/**
 * Finds people in camera frames with an on-device ONNX detector.
 *
 * The default is YOLOX-S ([YoloxContract], Apache-2.0), installed into the
 * assets by `scripts/mobile_models/make_models.py install`. A detector the user
 * imported in Settings uses [UltralyticsContract] instead
 * ([com.flightrisk.app.byo.ByoDetector]). Either way the detection is the
 * model's person box in frame pixels, clipped and truncated by [CropRule], and
 * its crop is cut from the full-resolution frame with no margin, as the ground
 * does.
 *
 * @param engine The detector without Android types (model, contract, threshold, size filter).
 * @param source What the engine runs, for logs and for telling detectors apart.
 */
class PersonDetector(
    private val engine: DetectorEngine,
    val source: String,
) : Closeable {

    private var pixels = IntArray(0)

    /**
     * Detect persons in a camera frame.
     *
     * @param frame The frame, upright, in any bitmap format `getPixels` reads.
     * @return One [Detection] per kept person, highest score first.
     */
    @Synchronized
    fun detect(frame: Bitmap): List<Detection> {
        val w = frame.width
        val h = frame.height
        // The pixel buffer is reused from frame to frame (the pipeline calls this from one thread).
        if (pixels.size != w * h) pixels = IntArray(w * h)
        frame.getPixels(pixels, 0, w, 0, 0, w, h)
        return engine.detect(pixels, w, h).map { person ->
            val (x1, y1, x2, y2) = person.pixels
            Detection(
                bbox = person.pixels.copyOf(),
                confidence = person.box.score,
                crop = Bitmap.createBitmap(frame, x1, y1, x2 - x1, y2 - y1),
            )
        }
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
        // Bitmap.config is nullable from API 35 (for example a bitmap whose format has no
        // Config); draw on an ARGB_8888 copy in that case.
        val annotated = frame.copy(frame.config ?: Bitmap.Config.ARGB_8888, true)
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

            canvas.drawRect(x1.toFloat(), y1.toFloat(), x2.toFloat(), y2.toFloat(), paint)

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

    override fun close() {
        engine.close()
    }

    override fun toString(): String = "PersonDetector($source)"
}
