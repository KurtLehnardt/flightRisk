package com.flightrisk.app.camera

import android.graphics.Bitmap

/**
 * Interface for video frame providers.
 *
 * Implementations deliver RGB [Bitmap] frames from a camera, file, or
 * test fixture. The vision pipeline consumes frames through this
 * abstraction so the detector/ReID/face modules stay decoupled from
 * any particular camera API.
 */
interface FrameSource {

    /** Start producing frames. */
    fun start()

    /** Stop producing frames and release resources. */
    fun stop()

    /**
     * Return the most recent frame, or null if no frame is available yet.
     *
     * Thread-safe: may be called from any thread.
     */
    fun getLatestFrame(): Bitmap?

    /**
     * Register a callback invoked each time a new frame arrives.
     *
     * Only one callback is active at a time; calling this again replaces
     * the previous callback.
     */
    fun setOnFrameCallback(callback: (Bitmap) -> Unit)
}
