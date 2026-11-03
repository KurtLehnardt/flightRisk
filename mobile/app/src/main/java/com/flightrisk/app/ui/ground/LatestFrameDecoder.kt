package com.flightrisk.app.ui.ground

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** What a tile's decoder holds: the latest decoded image, or that the last frame could not be read. */
data class DecodeState<T : Any>(val image: T? = null, val failed: Boolean = false)

/**
 * Decodes the frames of one tile, one at a time, off the calling thread. While a decode runs, newer
 * requests replace each other, so only the latest waiting frame is decoded and the rest are dropped
 * (dropping frames under load is the point: video is never queued). Only the latest decoded image is
 * kept. [submit], [clear] and [close] must be called from one thread (the main thread); [decode] runs on
 * [workDispatcher] and must stop early when its `cancelled` argument returns true.
 *
 * An image that was decoded but never shown (because a newer request or [clear] arrived meanwhile) is
 * passed to [release] straight away. A shown image is only dropped, never recycled, because the
 * renderer may still be drawing it; the garbage collector frees it with its native memory.
 */
class LatestFrameDecoder<T : Any>(
    private val scope: CoroutineScope,
    private val workDispatcher: CoroutineDispatcher,
    private val decode: (base64: String, targetEdge: Int, cancelled: () -> Boolean) -> T?,
    private val release: (T) -> Unit = {},
) {
    private class Request(val base64: String, val targetEdge: Int)

    private val _state = MutableStateFlow(DecodeState<T>())
    val state: StateFlow<DecodeState<T>> = _state.asStateFlow()

    /** How many decodes have started; for tests. */
    var decodesStarted = 0
        private set

    private var pending: Request? = null
    private var worker: Job? = null
    private var generation = 0
    private var closed = false

    /** Asks for this frame to be decoded; null clears the image and abandons queued work. */
    fun submit(base64: String?, targetEdge: Int) {
        if (closed) return
        if (base64 == null) {
            clear()
            return
        }
        pending = Request(base64, targetEdge)
        if (worker != null) return
        val mine = generation
        worker = scope.launch { drain(mine) }
    }

    /** Drops queued work, cancels a running decode and clears the image. */
    fun clear() {
        generation++
        pending = null
        worker?.cancel()
        worker = null
        _state.value = DecodeState()
    }

    /** [clear]s and refuses further requests. */
    fun close() {
        clear()
        closed = true
    }

    private suspend fun drain(mine: Int) {
        while (mine == generation) {
            val next = pending ?: break
            pending = null
            decodesStarted++
            val result = withContext(workDispatcher) { decode(next.base64, next.targetEdge) { !isActive } }
            if (mine != generation) {
                result?.let(release)
                return
            }
            _state.value = DecodeState(result, failed = result == null)
        }
        if (mine == generation) worker = null
    }
}

/**
 * The Android decoder for a tile's JPEG: refuses hostile input (see [VideoTileLogic.guard]), reads the
 * size first, refuses huge images, and decodes with a power-of-two subsample into RGB_565 so the
 * full-size bitmap is never held. Returns null for anything it cannot read. Never throws.
 */
internal fun decodeFrameBitmap(base64: String, targetEdge: Int, cancelled: () -> Boolean): Bitmap? {
    if (cancelled()) return null
    val jpeg = (VideoTileLogic.guard(base64) as? VideoTileLogic.Guarded.Ok)?.jpeg ?: return null
    return try {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, bounds)
        if (!VideoTileLogic.sourceSizeAllowed(bounds.outWidth, bounds.outHeight) || cancelled()) return null
        val options = BitmapFactory.Options().apply {
            inSampleSize = VideoTileLogic.sampleSize(bounds.outWidth, bounds.outHeight, targetEdge)
            inPreferredConfig = Bitmap.Config.RGB_565
        }
        val bitmap = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, options)
        if (bitmap != null && cancelled()) {
            bitmap.recycle() // Never shown, so nothing else holds it.
            null
        } else {
            bitmap
        }
    } catch (_: Exception) {
        null
    } catch (_: OutOfMemoryError) {
        null
    }
}
