package com.flightrisk.app.camera

/**
 * Hands camera frames to the search pipeline, converting a frame only when the
 * pipeline wants one. No Android types, so it is unit-tested.
 *
 * The analyzer calls [offer] for every frame the camera delivers. The frame is
 * always closed (so CameraX can deliver the next one), but it is converted
 * ([F] to [T], for example `ImageProxy` to an upright `Bitmap`) only while the
 * source is started, the pipeline has asked for a frame, and none is waiting.
 * Otherwise it is closed unconverted, which costs nothing. The pipeline calls
 * [take]: it gets the waiting frame once, or null, and in that case the next
 * frame the camera delivers is converted for it. A frame converted while (or
 * after) the source is stopped is dropped.
 */
class FrameHandOff<F, T : Any> {
    private val lock = Any()
    private var running = false
    private var wanted = false
    private var ready: T? = null

    /** The last frame converted, whether or not it was taken; null after [stop]. */
    @Volatile
    var latest: T? = null
        private set

    /** Starts keeping frames; the first frame delivered is converted. */
    fun start() = synchronized(lock) {
        running = true
        wanted = true
        ready = null
    }

    /** Stops keeping frames and drops the waiting one. */
    fun stop() = synchronized(lock) {
        running = false
        wanted = false
        ready = null
        latest = null
    }

    /**
     * One frame from the camera. Converts it with [convert] if the pipeline
     * wants a frame, and closes it with [close] on every path, also when
     * [convert] throws. Returns the converted frame, or null when it was not
     * converted or could not be.
     */
    fun offer(frame: F, close: (F) -> Unit, convert: (F) -> T): T? {
        try {
            val need = synchronized(lock) { running && wanted && ready == null }
            if (!need) return null
            val value = try {
                convert(frame)
            } catch (e: Exception) {
                // The pipeline still wants a frame; the next one is tried.
                return null
            }
            synchronized(lock) {
                if (!running) return null
                ready = value
                wanted = false
                latest = value
            }
            return value
        } finally {
            close(frame)
        }
    }

    /** The waiting frame, handed out once; or null, and then the next frame delivered is converted. */
    fun take(): T? = synchronized(lock) {
        val value = ready
        if (value != null) {
            ready = null
        } else if (running) {
            wanted = true
        }
        value
    }
}
