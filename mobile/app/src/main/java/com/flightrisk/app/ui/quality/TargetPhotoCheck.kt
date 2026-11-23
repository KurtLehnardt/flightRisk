package com.flightrisk.app.ui.quality

import android.content.Context
import android.graphics.Bitmap
import android.util.Log
import com.flightrisk.app.model.OnnxModelLoader
import com.flightrisk.app.vision.DetectionSizeFilter
import com.flightrisk.app.vision.DetectorEngine
import com.flightrisk.app.vision.FrameBox
import com.flightrisk.app.vision.YoloxContract

/**
 * Checks a target photo on the phone: the image measures of
 * [ImageQualityScorer] and, with YOLOX-S (the built-in detector, whatever
 * detector searches use), whether it shows one whole person
 * ([PersonVisibility]). Runs the detector: call it off the main thread.
 */
object TargetPhotoCheck {
    private const val TAG = "TargetPhotoCheck"

    /** The person check does not need a large photo; it is scaled to this longest side first. */
    private const val MAX_SIDE = 1280

    fun analyze(context: Context, bitmap: Bitmap): QualityReport =
        ImageQualityScorer.analyze(bitmap, persons(context, bitmap))

    /** The people YOLOX-S finds in [bitmap], in its pixels; null when the detector is not available. */
    fun persons(context: Context, bitmap: Bitmap): List<FrameBox>? {
        val longest = maxOf(bitmap.width, bitmap.height)
        val scale = if (longest > MAX_SIDE) MAX_SIDE.toFloat() / longest else 1f
        val small = if (scale < 1f) {
            Bitmap.createScaledBitmap(bitmap, (bitmap.width * scale).toInt().coerceAtLeast(1), (bitmap.height * scale).toInt().coerceAtLeast(1), true)
        } else {
            bitmap
        }
        return try {
            val model = OnnxModelLoader.fromAsset(context, YoloxContract.ASSET)
            DetectorEngine(model, YoloxContract, PersonVisibility.MIN_SCORE, DetectionSizeFilter.NONE).use { engine ->
                val w = small.width
                val h = small.height
                val argb = IntArray(w * h)
                small.getPixels(argb, 0, w, 0, 0, w, h)
                engine.detect(argb, w, h).map { p ->
                    FrameBox(p.box.x1 / scale, p.box.y1 / scale, p.box.x2 / scale, p.box.y2 / scale, p.box.score, p.box.classId)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Person check unavailable (${e.javaClass.simpleName})")
            null
        } catch (e: OutOfMemoryError) {
            Log.w(TAG, "Person check unavailable (out of memory)")
            null
        } finally {
            if (small !== bitmap) small.recycle()
        }
    }
}
