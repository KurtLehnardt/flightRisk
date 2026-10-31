package com.flightrisk.app.transport

import android.graphics.Bitmap
import com.flightrisk.app.pipeline.PublishedDetection
import com.flightrisk.app.pipeline.PublishedFrame
import com.flightrisk.app.vision.Detection
import java.io.ByteArrayOutputStream

/**
 * Turns the pipeline's bitmaps into a [PublishedFrame]. This is the only
 * place the app compresses images for the ground station link. It builds the
 * JPEG bytes lazily, so a frame the publisher drops costs no image work, and
 * it holds no reference to the bitmaps after the publish call returns.
 *
 * The wire format itself (JSON keys, base64, the size limit) lives only in
 * the contract module's `DetectionsWire`.
 */
object BitmapFrames {
    /** Whole-frame thumbnail size and quality. */
    const val THUMBNAIL_WIDTH = 320
    const val THUMBNAIL_HEIGHT = 180
    const val THUMBNAIL_QUALITY = 60

    /** The longest side of a person crop, and its quality. Small on purpose. */
    const val CROP_MAX_SIDE = 160
    const val CROP_QUALITY = 70

    fun publishedFrame(
        frame: Bitmap,
        detections: List<Detection>,
        frameId: Long,
        timestampSeconds: Double,
    ): PublishedFrame =
        PublishedFrame(
            frameId = frameId,
            timestampSeconds = timestampSeconds,
            frameWidth = frame.width,
            frameHeight = frame.height,
            thumbnailJpeg = { jpeg(frame, THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT, THUMBNAIL_QUALITY) },
            detections = detections.map { d ->
                PublishedDetection(
                    bbox = d.bbox.map { it.toDouble() },
                    confidence = d.confidence.toDouble(),
                    cropJpeg = { scaledCrop(d.crop) },
                )
            },
        )

    private fun scaledCrop(crop: Bitmap): ByteArray? {
        val longest = maxOf(crop.width, crop.height)
        if (longest <= 0) return null
        val scale = minOf(1.0, CROP_MAX_SIDE.toDouble() / longest)
        val w = maxOf(1, (crop.width * scale).toInt())
        val h = maxOf(1, (crop.height * scale).toInt())
        return jpeg(crop, w, h, CROP_QUALITY)
    }

    private fun jpeg(source: Bitmap, width: Int, height: Int, quality: Int): ByteArray? =
        try {
            val scaled = if (source.width == width && source.height == height) source else Bitmap.createScaledBitmap(source, width, height, true)
            val out = ByteArrayOutputStream()
            val ok = scaled.compress(Bitmap.CompressFormat.JPEG, quality, out)
            if (scaled !== source) scaled.recycle()
            if (ok) out.toByteArray() else null
        } catch (_: RuntimeException) {
            null
        }
}
