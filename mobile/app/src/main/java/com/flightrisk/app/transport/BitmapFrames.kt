package com.flightrisk.app.transport

import android.graphics.Bitmap
import com.flightrisk.app.pipeline.PublishedDetection
import com.flightrisk.app.pipeline.PublishedFrame
import com.flightrisk.app.vision.Detection
import com.flightrisk.app.vision.EdgeEmbeddings
import com.flightrisk.app.vision.EmbeddingProvider
import com.flightrisk.app.vision.NoEmbeddingProvider
import java.io.ByteArrayOutputStream

/**
 * Turns the pipeline's bitmaps into a [PublishedFrame]. This is the only
 * place the app compresses images for the ground station link.
 *
 * The pipeline thread only makes small scaled copies of what the publisher
 * asked for (the whole-view thumbnail when the ground asked for video, crops
 * for the most confident people when crops are on, and inputs for the
 * appearance model when the app has one the ground can compare); the JPEG
 * compression and the model run later, in the lambdas, on the publisher's
 * worker. The camera frame and the detection crops themselves are not used
 * after [publishedFrame] returns, and no lambda recycles them: every copy is
 * a new bitmap.
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

    /** The longest side of the copy handed to the appearance model, which resizes it to 224 pixels anyway. */
    const val EMBEDDING_INPUT_MAX_SIDE = 512

    fun publishedFrame(
        frame: Bitmap,
        detections: List<Detection>,
        frameId: Long,
        timestampSeconds: Double,
        withThumbnail: Boolean,
        maxCrops: Int,
        maxEmbeddings: Int = 0,
        embeddingProvider: EmbeddingProvider<Bitmap> = NoEmbeddingProvider,
    ): PublishedFrame {
        val thumb = if (withThumbnail) scaledToFit(frame, THUMBNAIL_MAX_WIDTH, THUMBNAIL_MAX_HEIGHT) else null
        val cropOwners = detections.indices.sortedByDescending { detections[it].confidence }.take(maxCrops.coerceAtLeast(0)).toSet()
        val embeddings = EdgeEmbeddings.lazyEmbeddings(
            detections = detections,
            confidence = { it.confidence.toDouble() },
            max = maxEmbeddings,
            provider = embeddingProvider,
            prepare = { scaledToFit(it.crop, EMBEDDING_INPUT_MAX_SIDE, EMBEDDING_INPUT_MAX_SIDE) },
            release = ::recycleQuietly,
        )
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
                    reidEmbedding = embeddings[i],
                    cropJpeg = { crop?.let { compress(it, CROP_QUALITY) } },
                )
            },
        )
    }

    /**
     * A new bitmap no larger than the limits, keeping the aspect ratio, or null
     * if it cannot be made. It is never [source] itself (`createScaledBitmap`
     * returns the source when no scaling is needed), because the caller
     * recycles it while the pipeline may still use the source.
     */
    private fun scaledToFit(source: Bitmap, maxWidth: Int, maxHeight: Int): Bitmap? =
        try {
            if (source.width <= 0 || source.height <= 0) {
                null
            } else {
                val scale = minOf(1.0, maxWidth.toDouble() / source.width, maxHeight.toDouble() / source.height)
                val w = maxOf(1, (source.width * scale).toInt())
                val h = maxOf(1, (source.height * scale).toInt())
                val scaled = Bitmap.createScaledBitmap(source, w, h, true)
                if (scaled === source) source.copy(source.config ?: Bitmap.Config.ARGB_8888, false) else scaled
            }
        } catch (_: RuntimeException) {
            null
        }

    private fun recycleQuietly(bitmap: Bitmap) {
        try {
            bitmap.recycle()
        } catch (_: RuntimeException) {
        }
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
