package com.flightrisk.app.transport

import android.graphics.Bitmap
import com.flightrisk.app.pipeline.PublishedDetection
import com.flightrisk.app.pipeline.PublishedFrame
import com.flightrisk.app.vision.Detection
import java.io.ByteArrayOutputStream

/**
 * Turns the pipeline's bitmaps into a [PublishedFrame]. This is the only
 * place the app compresses images for the ground station link.
 *
 * The pipeline thread only makes small scaled copies of what the publisher
 * asked for (the whole-view thumbnail when the ground asked for video, and
 * crops for the most confident people when crops are on); the JPEG
 * compression runs later, in the lambdas, on the publisher's worker. The
 * camera frame itself is not used after [publishedFrame] returns.
 *
 * The wire format (JSON keys, base64, the size limit) lives only in the
 * contract module's `DetectionsWire`.
 */
object BitmapFrames {
    /** Whole-frame thumbnail: at most this wide or tall, keeping the aspect ratio. */
    const val THUMBNAIL_MAX_WIDTH = 320
    const val THUMBNAIL_MAX_HEIGHT = 240
    const val THUMBNAIL_QUALITY = 60

    /** The longest side of a person crop, and its quality. Small on purpose. */
    const val CROP_MAX_SIDE = 160
    const val CROP_QUALITY = 70

    fun publishedFrame(
        frame: Bitmap,
        detections: List<Detection>,
        frameId: Long,
        timestampSeconds: Double,
        withThumbnail: Boolean,
        maxCrops: Int,
    ): PublishedFrame {
        val thumb = if (withThumbnail) scaledToFit(frame, THUMBNAIL_MAX_WIDTH, THUMBNAIL_MAX_HEIGHT) else null
        val cropOwners = detections.indices.sortedByDescending { detections[it].confidence }.take(maxCrops.coerceAtLeast(0)).toSet()
        return PublishedFrame(
            frameId = frameId,
            timestampSeconds = timestampSeconds,
            frameWidth = frame.width,
            frameHeight = frame.height,
            thumbnailJpeg = { thumb?.let { compress(it, THUMBNAIL_QUALITY) } },
            detections = detections.mapIndexed { i, d ->
                val crop = if (i in cropOwners) scaledToFit(d.crop, CROP_MAX_SIDE, CROP_MAX_SIDE) else null
                PublishedDetection(
                    bbox = d.bbox.map { it.toDouble() },
                    confidence = d.confidence.toDouble(),
                    cropJpeg = { crop?.let { compress(it, CROP_QUALITY) } },
                )
            },
        )
    }

    /** A copy no larger than the limits, keeping the aspect ratio, or null if it cannot be made. */
    private fun scaledToFit(source: Bitmap, maxWidth: Int, maxHeight: Int): Bitmap? =
        try {
            if (source.width <= 0 || source.height <= 0) {
                null
            } else {
                val scale = minOf(1.0, maxWidth.toDouble() / source.width, maxHeight.toDouble() / source.height)
                val w = maxOf(1, (source.width * scale).toInt())
                val h = maxOf(1, (source.height * scale).toInt())
                Bitmap.createScaledBitmap(source, w, h, true)
            }
        } catch (_: RuntimeException) {
            null
        }

    private fun compress(bitmap: Bitmap, quality: Int): ByteArray? =
        try {
            val out = ByteArrayOutputStream()
            if (bitmap.compress(Bitmap.CompressFormat.JPEG, quality, out)) out.toByteArray() else null
        } catch (_: RuntimeException) {
            null
        } finally {
            try {
                bitmap.recycle()
            } catch (_: RuntimeException) {
            }
        }
}
