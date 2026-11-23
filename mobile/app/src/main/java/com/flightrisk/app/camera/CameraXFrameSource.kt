package com.flightrisk.app.camera

import android.graphics.Bitmap
import android.graphics.Matrix
import android.util.Log
import android.util.Size
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * The phone camera as a [FrameSource]: a CameraX [ImageAnalysis] analyzer.
 *
 * The Search screen binds the use case from [buildImageAnalysis] together
 * with its preview in one `bindToLifecycle` call. CameraX delivers each frame
 * as RGBA_8888 ([ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888]);
 * [ImageProxy.toBitmap] turns it into an ARGB_8888 bitmap, honouring the row
 * stride and padding of the buffer, and the bitmap is rotated upright by
 * [ImageProxy.getImageInfo]'s rotation. The frame keeps the resolution CameraX
 * chose (about 1280 x 720), so the detector letterboxes it and the embedder's
 * crops are cut from it at full resolution.
 *
 * Frames are kept only while the source is started ([start] when a search
 * starts, [stop] when it ends); [takeNewFrame] hands each frame out once.
 */
class CameraXFrameSource : FrameSource, ImageAnalysis.Analyzer {

    private val lock = ReentrantLock()
    private var latestFrame: Bitmap? = null
    private var fresh = false
    private var frameCallback: ((Bitmap) -> Unit)? = null

    @Volatile
    private var isRunning = false

    private val executor: ExecutorService by lazy { Executors.newSingleThreadExecutor() }

    override fun start() {
        lock.withLock {
            isRunning = true
        }
    }

    override fun stop() {
        lock.withLock {
            isRunning = false
            latestFrame = null
            fresh = false
        }
    }

    /** Stops the source and its analysis thread. It cannot be used again. */
    fun release() {
        stop()
        executor.shutdown()
    }

    override fun getLatestFrame(): Bitmap? = lock.withLock { latestFrame }

    /** The latest frame if it has not been taken yet, else null, so each camera frame is processed once. */
    fun takeNewFrame(): Bitmap? {
        lock.withLock {
            if (!fresh) return null
            fresh = false
            return latestFrame
        }
    }

    override fun setOnFrameCallback(callback: (Bitmap) -> Unit) {
        lock.withLock {
            frameCallback = callback
        }
    }

    /** Called by CameraX on the analysis thread for each frame. */
    override fun analyze(image: ImageProxy) {
        try {
            if (!isRunning) return
            val upright = upright(image.toBitmap(), image.imageInfo.rotationDegrees)
            var callback: ((Bitmap) -> Unit)? = null
            lock.withLock {
                // A frame that arrives after stop() is dropped.
                if (!isRunning) return
                latestFrame = upright
                fresh = true
                callback = frameCallback
            }
            callback?.invoke(upright)
        } catch (e: Exception) {
            // toBitmap() refuses a format it cannot convert; that frame is skipped.
            Log.w(TAG, "Camera frame skipped: ${e.javaClass.simpleName}")
        } finally {
            image.close()
        }
    }

    /**
     * The [ImageAnalysis] use case for this source. [targetRotation] is the
     * display rotation (`Surface.ROTATION_*`) of the preview, when known, so
     * that the reported rotation turns frames the way the preview shows them.
     */
    fun buildImageAnalysis(targetRotation: Int? = null): ImageAnalysis {
        val selector = ResolutionSelector.Builder()
            .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
            .setResolutionStrategy(
                ResolutionStrategy(
                    Size(ANALYSIS_WIDTH, ANALYSIS_HEIGHT),
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER,
                ),
            )
            .build()
        return ImageAnalysis.Builder()
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setResolutionSelector(selector)
            .apply { if (targetRotation != null) setTargetRotation(targetRotation) }
            .build()
            .also { it.setAnalyzer(executor, this) }
    }

    companion object {
        private const val TAG = "CameraXFrameSource"
        const val ANALYSIS_WIDTH = 1280
        const val ANALYSIS_HEIGHT = 720

        /** [bitmap] turned upright by [rotationDegrees] (clockwise, as CameraX reports it). */
        fun upright(bitmap: Bitmap, rotationDegrees: Int): Bitmap {
            val degrees = FrameRotation.normalize(rotationDegrees)
            if (degrees == 0) return bitmap
            val matrix = Matrix().apply { postRotate(degrees.toFloat()) }
            val rotated = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
            if (rotated !== bitmap) bitmap.recycle()
            return rotated
        }
    }
}

/** Rotation arithmetic for camera frames, without Android types. */
object FrameRotation {
    /** [degrees] as 0, 90, 180 or 270; anything that is not a multiple of 90 is refused. */
    fun normalize(degrees: Int): Int {
        val d = ((degrees % 360) + 360) % 360
        require(d % 90 == 0) { "rotation must be a multiple of 90 degrees, got $degrees" }
        return d
    }
}
