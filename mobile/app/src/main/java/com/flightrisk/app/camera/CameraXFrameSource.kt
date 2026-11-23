package com.flightrisk.app.camera

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Matrix
import android.util.Log
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * The phone camera as a [FrameSource] for a search.
 *
 * For the whole search, [bind] binds a [Preview] and an [ImageAnalysis] use
 * case together, in one `bindToLifecycle` call, to the Activity (not to the
 * Search screen), so detection keeps running when the person opens another
 * tab; the Search screen only shows [preview] while it is visible. Both use
 * cases ask for the same 16:9 aspect ratio, so the preview shows what is
 * analysed. Frames arrive as RGBA_8888 and [FrameHandOff] converts one
 * ([ImageProxy.toBitmap], which honours the row stride and padding, then
 * turned upright by the reported rotation) only when the pipeline asks for it
 * with [takeNewFrame]; every other frame is closed unconverted.
 */
class CameraXFrameSource : FrameSource, ImageAnalysis.Analyzer {

    /** What the camera is doing, for the Search screen. */
    enum class Status { IDLE, STARTING, RUNNING, NO_PERMISSION, UNAVAILABLE }

    private val handOff = FrameHandOff<ImageProxy, Bitmap>()
    private var frameCallback: ((Bitmap) -> Unit)? = null
    private val executor: ExecutorService by lazy { Executors.newSingleThreadExecutor() }

    private var provider: ProcessCameraProvider? = null
    private var boundCases: List<androidx.camera.core.UseCase> = emptyList()
    private var bindGeneration = 0

    /** The preview of the running search, for the Search screen to show; null when none is bound. */
    var preview by mutableStateOf<Preview?>(null)
        private set

    var status by mutableStateOf(Status.IDLE)
        private set

    override fun start() = handOff.start()

    override fun stop() = handOff.stop()

    /** Stops the source and its analysis thread. It cannot be used again. */
    fun release() {
        stop()
        unbind()
        executor.shutdown()
    }

    override fun getLatestFrame(): Bitmap? = handOff.latest

    /** The next camera frame, each frame handed out once, or null when none is ready yet. */
    fun takeNewFrame(): Bitmap? = handOff.take()

    override fun setOnFrameCallback(callback: (Bitmap) -> Unit) {
        frameCallback = callback
    }

    /** Called by CameraX on the analysis thread for each frame. Every frame is closed. */
    override fun analyze(image: ImageProxy) {
        val frame = handOff.offer(image, close = { it.close() }) { upright(it.toBitmap(), it.imageInfo.rotationDegrees) }
        if (frame != null) frameCallback?.invoke(frame)
    }

    /**
     * Binds the preview and the frame analysis to [owner] (the Activity) for a
     * search. [displayRotation] is the display's `Surface.ROTATION_*`. Call on
     * the main thread. Use cases bound by an earlier bind are unbound first,
     * so a second bind never leaves two sets bound.
     */
    fun bind(context: Context, owner: LifecycleOwner, displayRotation: Int) {
        releaseBoundCases()
        preview = null
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            status = Status.NO_PERMISSION
            return
        }
        status = Status.STARTING
        val generation = ++bindGeneration
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            // unbind() (or another bind) came first: do nothing.
            if (generation != bindGeneration) return@addListener
            val p = try {
                future.get()
            } catch (e: Exception) {
                Log.e(TAG, "Camera provider unavailable", e)
                status = Status.UNAVAILABLE
                return@addListener
            }
            provider = p
            // The back camera is preferred; a device with only a front camera uses that.
            val selector = listOf(CameraSelector.DEFAULT_BACK_CAMERA, CameraSelector.DEFAULT_FRONT_CAMERA)
                .firstOrNull { s -> runCatching { p.hasCamera(s) }.getOrDefault(false) }
            if (selector == null) {
                status = Status.UNAVAILABLE
                return@addListener
            }
            val previewCase = Preview.Builder()
                .setResolutionSelector(ResolutionSelector.Builder().setAspectRatioStrategy(ASPECT).build())
                .setTargetRotation(displayRotation)
                .build()
            val analysisCase = buildImageAnalysis(displayRotation)
            try {
                // Nothing this source bound before stays bound next to the new use cases.
                releaseBoundCases()
                p.bindToLifecycle(owner, selector, previewCase, analysisCase)
                boundCases = listOf(previewCase, analysisCase)
                preview = previewCase
                status = Status.RUNNING
            } catch (e: IllegalArgumentException) {
                // No camera matches the selector (it can disappear, for example an external one).
                Log.e(TAG, "Camera bind failed: no matching camera", e)
                status = Status.UNAVAILABLE
            } catch (e: IllegalStateException) {
                Log.e(TAG, "Camera bind failed: illegal state", e)
                status = Status.UNAVAILABLE
            } catch (e: SecurityException) {
                Log.e(TAG, "Camera bind failed: security exception", e)
                status = Status.NO_PERMISSION
            }
        }, ContextCompat.getMainExecutor(context))
    }

    /** Unbinds the search's use cases. Call on the main thread. */
    fun unbind() {
        bindGeneration++
        releaseBoundCases()
        preview = null
        status = Status.IDLE
    }

    /** Unbinds the use cases this source bound, if any, and stops their frame analysis. Main thread. */
    private fun releaseBoundCases() {
        val cases = boundCases
        boundCases = emptyList()
        if (cases.isEmpty()) return
        try {
            provider?.unbind(*cases.toTypedArray())
        } catch (e: Exception) {
            Log.w(TAG, "Camera cleanup failed", e)
        }
        cases.filterIsInstance<ImageAnalysis>().forEach { it.clearAnalyzer() }
    }

    private fun buildImageAnalysis(targetRotation: Int): ImageAnalysis {
        val selector = ResolutionSelector.Builder()
            .setAspectRatioStrategy(ASPECT)
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
            .setTargetRotation(targetRotation)
            .build()
            .also { it.setAnalyzer(executor, this) }
    }

    companion object {
        private const val TAG = "CameraXFrameSource"
        const val ANALYSIS_WIDTH = 1280
        const val ANALYSIS_HEIGHT = 720

        /** The preview and the analysis use the same aspect ratio, so the preview shows what is searched. */
        private val ASPECT = AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY

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
