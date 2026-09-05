package com.flightrisk.app.camera

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Matrix
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * CameraX [ImageAnalysis] implementation of [FrameSource].
 *
 * Captures preview frames via [ImageAnalysis.Analyzer], converts
 * YUV_420_888 to RGB [Bitmap], and applies rotation compensation
 * based on device orientation. The latest frame is held in a
 * thread-safe buffer.
 *
 * @param targetWidth Desired frame width (default 640 for YOLO input).
 * @param targetHeight Desired frame height (default 480 for YOLO input).
 */
class CameraXFrameSource(
    private val targetWidth: Int = 640,
    private val targetHeight: Int = 480,
) : FrameSource, ImageAnalysis.Analyzer {

    private val lock = ReentrantLock()
    private var latestFrame: Bitmap? = null
    private var frameCallback: ((Bitmap) -> Unit)? = null
    private var isRunning = false

    override fun start() {
        lock.withLock {
            isRunning = true
        }
    }

    override fun stop() {
        lock.withLock {
            isRunning = false
            latestFrame = null
        }
    }

    override fun getLatestFrame(): Bitmap? {
        lock.withLock {
            return latestFrame
        }
    }

    override fun setOnFrameCallback(callback: (Bitmap) -> Unit) {
        lock.withLock {
            frameCallback = callback
        }
    }

    /**
     * Called by CameraX for each preview frame.
     *
     * Converts the YUV_420_888 image to an RGB [Bitmap], applies
     * rotation correction, stores it in the thread-safe buffer,
     * and fires the frame callback if registered.
     */
    override fun analyze(imageProxy: ImageProxy) {
        if (!isRunning) {
            imageProxy.close()
            return
        }

        try {
            val bitmap = imageProxyToBitmap(imageProxy)
            if (bitmap != null) {
                val rotated = applyRotation(bitmap, imageProxy.imageInfo.rotationDegrees)
                val scaled = Bitmap.createScaledBitmap(rotated, targetWidth, targetHeight, true)

                // Recycle intermediates if they are distinct objects
                if (rotated !== bitmap) bitmap.recycle()
                if (scaled !== rotated) rotated.recycle()

                var callback: ((Bitmap) -> Unit)? = null
                lock.withLock {
                    latestFrame = scaled
                    callback = frameCallback
                }
                callback?.invoke(scaled)
            }
        } finally {
            imageProxy.close()
        }
    }

    /**
     * Convert an [ImageProxy] in YUV_420_888 format to an RGB [Bitmap].
     *
     * Uses [YuvImage] and JPEG compression as the conversion path,
     * which is the simplest approach that works reliably across
     * devices. For production, consider RenderScript or a native
     * YUV converter for better performance.
     */
    private fun imageProxyToBitmap(imageProxy: ImageProxy): Bitmap? {
        if (imageProxy.format != ImageFormat.YUV_420_888) return null

        val yBuffer = imageProxy.planes[0].buffer
        val uBuffer = imageProxy.planes[1].buffer
        val vBuffer = imageProxy.planes[2].buffer

        val ySize = yBuffer.remaining()
        val uSize = uBuffer.remaining()
        val vSize = vBuffer.remaining()

        // NV21 format: Y plane followed by interleaved VU
        val nv21 = ByteArray(ySize + uSize + vSize)
        yBuffer.get(nv21, 0, ySize)
        vBuffer.get(nv21, ySize, vSize)
        uBuffer.get(nv21, ySize + vSize, uSize)

        val yuvImage = YuvImage(
            nv21,
            ImageFormat.NV21,
            imageProxy.width,
            imageProxy.height,
            null,
        )

        val outputStream = ByteArrayOutputStream()
        yuvImage.compressToJpeg(
            Rect(0, 0, imageProxy.width, imageProxy.height),
            90,
            outputStream,
        )

        val jpegBytes = outputStream.toByteArray()
        return BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size)
    }

    /**
     * Apply rotation compensation to a bitmap.
     *
     * CameraX reports the rotation needed to orient the image
     * upright relative to the device's natural orientation.
     */
    private fun applyRotation(bitmap: Bitmap, rotationDegrees: Int): Bitmap {
        if (rotationDegrees == 0) return bitmap

        val matrix = Matrix().apply {
            postRotate(rotationDegrees.toFloat())
        }
        return Bitmap.createBitmap(
            bitmap, 0, 0,
            bitmap.width, bitmap.height,
            matrix, true,
        )
    }

    /**
     * Build an [ImageAnalysis] use case configured for this frame source.
     *
     * Bind the returned use case to a [LifecycleOwner] via CameraX's
     * [ProcessCameraProvider].
     */
    fun buildImageAnalysis(): ImageAnalysis {
        return ImageAnalysis.Builder()
            .setTargetResolution(android.util.Size(targetWidth, targetHeight))
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()
            .also { it.setAnalyzer(java.util.concurrent.Executors.newSingleThreadExecutor(), this) }
    }
}
