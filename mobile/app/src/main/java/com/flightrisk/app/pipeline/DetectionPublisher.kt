package com.flightrisk.app.pipeline

/**
 * One person found in a camera frame, as the pipeline hands it to a
 * [DetectionPublisher]. Images are compressed and embeddings computed on
 * demand: [cropJpeg] and [reidEmbedding] are called on the publisher's worker,
 * not on the pipeline's thread, and only if the publisher actually encodes the
 * frame.
 */
class PublishedDetection(
    /** `[x1, y1, x2, y2]` in pixels of the camera frame. */
    val bbox: List<Double>,
    val confidence: Double,
    /**
     * Computes the appearance embedding, or returns null when there is none
     * (the app has no model for the ground yet, see `EmbeddingProvider`). Only
     * the detections the publisher asked for ([DetectionPublisher.maxEmbeddings])
     * have one. It may throw when the model fails; the publisher then sends null.
     * The vector is kept in memory only and never logged or stored.
     */
    val reidEmbedding: () -> List<Double>? = { null },
    /** A face embedding, or null. A publisher sends it only when face matching is on. */
    val faceEmbedding: List<Double>? = null,
    /** A small JPEG of the person, or null. Only the detections the publisher asked for have one. */
    val cropJpeg: () -> ByteArray? = { null },
)

/** The detections of one camera frame. */
class PublishedFrame(
    val frameId: Long,
    /** Seconds since the Unix epoch. */
    val timestampSeconds: Double,
    val frameWidth: Int,
    val frameHeight: Int,
    /** A small JPEG of the whole frame, or null. Called on the publisher's worker, only if the frame is encoded. */
    val thumbnailJpeg: () -> ByteArray? = { null },
    val detections: List<PublishedDetection>,
)

/**
 * Where the search pipeline hands each frame's detections. The default does
 * nothing. The pipeline calls [wantsFrame] first and builds a [PublishedFrame]
 * only when it returns true (copying only what [wantsThumbnail], [maxCrops]
 * and [maxEmbeddings] ask for), then calls [publish] on the pipeline's thread.
 * Neither call may block, compress images or run a model, and neither may
 * throw into the pipeline.
 */
interface DetectionPublisher {
    /** True when [publish] would accept a frame now (for example not busy and not over its rate). */
    fun wantsFrame(): Boolean

    /** True when the next frame's whole-camera-view picture would be sent (the ground asked for video). */
    fun wantsThumbnail(): Boolean = false

    /** How many person crops the next frame may carry (the most confident detections); 0 means no pictures of people. */
    fun maxCrops(): Int = 0

    /** How many appearance embeddings the next frame may carry (the most confident detections); 0 means none. */
    fun maxEmbeddings(): Int = 0

    /** Offers one frame. The publisher may drop it; it never queues without bound. */
    fun publish(frame: PublishedFrame)

    /** A search started. */
    fun onSearchStarted() {}

    /** The search stopped. */
    fun onSearchStopped() {}

    object NoOp : DetectionPublisher {
        override fun wantsFrame(): Boolean = false
        override fun publish(frame: PublishedFrame) {}
        override fun toString(): String = "DetectionPublisher.NoOp"
    }
}
