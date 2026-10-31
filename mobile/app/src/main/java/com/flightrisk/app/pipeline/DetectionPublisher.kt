package com.flightrisk.app.pipeline

/**
 * One person found in a camera frame, as the pipeline hands it to a
 * [DetectionPublisher]. Images are produced on demand: [cropJpeg] is called
 * only while [DetectionPublisher.publish] runs, and only if the publisher
 * actually sends the frame, so a publisher that is busy costs no image work.
 */
class PublishedDetection(
    /** `[x1, y1, x2, y2]` in pixels of the camera frame. */
    val bbox: List<Double>,
    val confidence: Double,
    /** An appearance embedding, or null when the phone's model does not produce one. */
    val reidEmbedding: List<Double>? = null,
    /** A face embedding, or null. A publisher sends it only when face matching is on. */
    val faceEmbedding: List<Double>? = null,
    /** A small JPEG of the person, or null. */
    val cropJpeg: () -> ByteArray? = { null },
)

/** The detections of one camera frame. */
class PublishedFrame(
    val frameId: Long,
    /** Seconds since the Unix epoch. */
    val timestampSeconds: Double,
    val frameWidth: Int,
    val frameHeight: Int,
    /** A small JPEG of the whole frame, or null. Called only while [DetectionPublisher.publish] runs. */
    val thumbnailJpeg: () -> ByteArray? = { null },
    val detections: List<PublishedDetection>,
)

/**
 * Where the search pipeline hands each frame's detections. The default does
 * nothing. The pipeline calls [wantsFrame] first and builds a [PublishedFrame]
 * only when it returns true, then calls [publish] on the pipeline's thread.
 * Neither call may block, and neither may throw into the pipeline.
 */
interface DetectionPublisher {
    /** True when [publish] would accept a frame now (for example not busy and not over its rate). */
    fun wantsFrame(): Boolean

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
