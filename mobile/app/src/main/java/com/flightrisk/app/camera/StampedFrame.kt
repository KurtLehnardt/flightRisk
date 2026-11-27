package com.flightrisk.app.camera

/**
 * A frame with the identity the search pipeline needs to score it only once.
 *
 * @property sequence Increases by one for every frame the source receives. Two frames with the
 *   same sequence are the same picture.
 * @property arrivedAtMs When the source received the frame, on the clock the source was given.
 */
data class StampedFrame<T>(val frame: T, val sequence: Long, val arrivedAtMs: Long)

/**
 * Holds the newest frame of a video source together with its sequence number and arrival time.
 *
 * A frame older than [maxAgeMs] is never handed out: a stalled feed returns null instead of the
 * last picture it happened to receive.
 */
class LatestFrameSlot<T : Any>(
    private val clockMs: () -> Long,
    private val maxAgeMs: Long = DEFAULT_MAX_AGE_MS,
) {
    private var current: StampedFrame<T>? = null
    private var nextSequence = 0L

    /** Store a newly arrived frame. Returns the frame it replaced so the caller can release it. */
    @Synchronized
    fun put(frame: T): T? {
        val previous = current?.frame
        current = StampedFrame(frame, nextSequence++, clockMs())
        return previous
    }

    /** The newest frame, or null when there is none or it is older than the age limit. */
    @Synchronized
    fun latest(): StampedFrame<T>? {
        val held = current ?: return null
        return if (clockMs() - held.arrivedAtMs > maxAgeMs) null else held
    }

    /** Drop the held frame. Returns it so the caller can release it. Sequence numbers keep counting. */
    @Synchronized
    fun clear(): T? {
        val previous = current?.frame
        current = null
        return previous
    }

    companion object {
        /** A frame older than this is stale and is not returned. */
        const val DEFAULT_MAX_AGE_MS = 2_000L
    }
}

/** Lets each sequence number through once, so a repeated frame is not scored again. */
class FrameDeduper {
    private var lastSequence: Long? = null

    /** True the first time [sequence] is seen, false for a repeat of the previous frame. */
    @Synchronized
    fun isNew(sequence: Long): Boolean {
        if (sequence == lastSequence) return false
        lastSequence = sequence
        return true
    }
}
