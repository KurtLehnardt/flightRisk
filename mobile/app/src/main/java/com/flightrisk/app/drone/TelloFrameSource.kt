package com.flightrisk.app.drone

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.media.Image
import android.media.MediaCodec
import android.media.MediaFormat
import android.util.Log
import com.flightrisk.app.camera.FrameSource
import com.flightrisk.app.config.DroneConfig
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.SocketTimeoutException
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * [FrameSource] that receives and decodes the Tello's H264 video stream.
 *
 * The Tello streams H264 at 960x720 over UDP port 11111. This class
 * receives raw NAL units, feeds them to a [MediaCodec] hardware decoder
 * in ByteBuffer output mode (for API 26 compatibility), converts the
 * decoded YUV frames to RGB [Bitmap], and downscales to the pipeline
 * target resolution (640x480 by default).
 *
 * Frozen-frame detection mirrors the Python implementation in
 * `flightrisk/drone/tello.py`: a hash of center pixels is compared
 * across consecutive frames, and [onStreamFrozen] fires when the
 * count exceeds [frozenFrameThreshold].
 *
 * @param config       Drone configuration (provides video port).
 * @param nativeWidth  Tello native stream width (960).
 * @param nativeHeight Tello native stream height (720).
 * @param targetWidth  Desired output width for the vision pipeline.
 * @param targetHeight Desired output height for the vision pipeline.
 */
class TelloFrameSource(
    private val config: DroneConfig,
    private val nativeWidth: Int = 960,
    private val nativeHeight: Int = 720,
    private val targetWidth: Int = 640,
    private val targetHeight: Int = 480,
) : FrameSource {

    companion object {
        private const val TAG = "TelloFrameSource"
        private const val UDP_BUFFER_SIZE = 2048
        private const val SOCKET_TIMEOUT_MS = 2000
        private const val DECODE_POLL_TIMEOUT_US = 10_000L // 10ms
        private const val IDLE_SLEEP_MS = 5L
    }

    // Thread-safe latest frame (same pattern as CameraXFrameSource)
    private val lock = ReentrantLock()
    private var latestFrame: Bitmap? = null
    private var frameCallback: ((Bitmap) -> Unit)? = null
    @Volatile
    private var isRunning = false

    /** Set by DroneManager to handle frozen-stream recovery. */
    var onStreamFrozen: (() -> Unit)? = null

    // H264 decoder
    private var codec: MediaCodec? = null

    // UDP receiver
    private var udpSocket: DatagramSocket? = null
    private var receiveThread: Thread? = null
    private var decodeThread: Thread? = null
    private val nalQueue = ConcurrentLinkedQueue<ByteArray>()

    // NAL reassembly across UDP packet boundaries
    private val maxAccumulatorSize = 512 * 1024 // 512KB
    private val nalAccumulator = ByteArrayOutputStream(65536)
    @Volatile
    private var seenFirstStartCode = false

    // Frozen frame tracking
    private var lastFrameHash: Long = 0
    private var frozenFrameCount = 0
    private val frozenFrameThreshold = 100

    // -- FrameSource interface ------------------------------------------------

    override fun start() {
        if (isRunning) return

        try {
            // UDP socket
            udpSocket = DatagramSocket(config.telloVideoPort).apply {
                soTimeout = SOCKET_TIMEOUT_MS
            }

            // MediaCodec H264 decoder in ByteBuffer output mode
            val format = MediaFormat.createVideoFormat(
                MediaFormat.MIMETYPE_VIDEO_AVC,
                nativeWidth,
                nativeHeight,
            )
            codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC).apply {
                configure(format, null, null, 0) // null surface = ByteBuffer mode
                start()
            }

            // All resources created successfully
            isRunning = true

            // Receive thread (daemon)
            receiveThread = Thread(::receiveLoop, "TelloFrameSource-recv").apply {
                isDaemon = true
                start()
            }

            // Decode thread (daemon)
            decodeThread = Thread(::decodeLoop, "TelloFrameSource-decode").apply {
                isDaemon = true
                start()
            }

            Log.i(TAG, "Started on UDP port ${config.telloVideoPort}")
        } catch (e: Exception) {
            // Cleanup any partially created resources
            try { codec?.stop() } catch (_: Exception) {}
            try { codec?.release() } catch (_: Exception) {}
            codec = null
            try { udpSocket?.close() } catch (_: Exception) {}
            udpSocket = null
            isRunning = false
            Log.e(TAG, "Failed to start: ${e.message}", e)
            throw e
        }
    }

    override fun stop() {
        if (!isRunning) return
        isRunning = false

        // Close socket to unblock receive thread
        try {
            udpSocket?.close()
        } catch (e: Exception) {
            Log.w(TAG, "Error closing UDP socket", e)
        }
        udpSocket = null

        // Join threads
        receiveThread?.join(2000)
        decodeThread?.join(2000)
        receiveThread = null
        decodeThread = null

        // Release MediaCodec
        try {
            codec?.stop()
            codec?.release()
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing MediaCodec", e)
        }
        codec = null

        // Clear state
        nalQueue.clear()
        nalAccumulator.reset()
        seenFirstStartCode = false
        lock.withLock {
            latestFrame = null
            frameCallback = null
        }
        lastFrameHash = 0
        frozenFrameCount = 0

        Log.i(TAG, "Stopped")
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

    // -- Internal threads -----------------------------------------------------

    /**
     * Receive UDP packets from the Tello and reassemble NAL units across
     * packet boundaries before enqueuing for decoding.
     *
     * H264 NAL units (especially IDR frames) can span multiple UDP packets.
     * We accumulate incoming bytes and split on NAL start codes
     * (0x00 0x00 0x00 0x01), emitting complete NAL units to the decode queue.
     */
    private fun receiveLoop() {
        val buffer = ByteArray(UDP_BUFFER_SIZE)
        val packet = DatagramPacket(buffer, buffer.size)

        while (isRunning) {
            try {
                packet.setLength(buffer.size) // Reset length before each receive
                udpSocket?.receive(packet) ?: break

                // Guard against unbounded accumulator growth
                if (nalAccumulator.size() > maxAccumulatorSize) {
                    Log.w(TAG, "NAL accumulator exceeded $maxAccumulatorSize bytes, resetting")
                    nalAccumulator.reset()
                    seenFirstStartCode = false
                }

                // Append received bytes to accumulator
                nalAccumulator.write(
                    packet.data, packet.offset, packet.length
                )

                // Scan accumulated buffer for NAL start codes
                val accumulated = nalAccumulator.toByteArray()
                val startCodes = findNalStartCodes(accumulated)

                if (startCodes.isEmpty()) continue // keep accumulating

                if (!seenFirstStartCode) {
                    seenFirstStartCode = true
                    // Data before first start code is a partial NAL — discard it
                }

                // Emit complete NALs (each bounded by two consecutive start codes)
                for (i in 0 until startCodes.size - 1) {
                    nalQueue.add(accumulated.copyOfRange(startCodes[i], startCodes[i + 1]))
                }

                // Keep the last (potentially incomplete) NAL in the accumulator
                nalAccumulator.reset()
                val lastStart = startCodes.last()
                nalAccumulator.write(accumulated, lastStart, accumulated.size - lastStart)
            } catch (_: SocketTimeoutException) {
                // Expected when no data arrives within timeout; loop continues
            } catch (_: IOException) {
                // Socket closed (normal shutdown) or network error
                if (isRunning) {
                    Log.w(TAG, "UDP receive error, exiting receive loop")
                }
                break
            }
        }
        Log.d(TAG, "Receive loop exited")
    }

    /**
     * Dequeue raw packets, extract NAL units, feed to MediaCodec,
     * and deliver decoded frames.
     */
    private fun decodeLoop() {
        val bufferInfo = MediaCodec.BufferInfo()

        while (isRunning) {
            val decoder = codec ?: break
            var didWork = false

            // --- Feed input ---
            val data = nalQueue.poll()
            if (data != null) {
                didWork = true
                feedNalUnits(decoder, data)
            }

            // --- Drain output ---
            try {
                val outputIndex = decoder.dequeueOutputBuffer(bufferInfo, DECODE_POLL_TIMEOUT_US)
                when {
                    outputIndex >= 0 -> {
                        didWork = true
                        try {
                            val outputFormat = decoder.outputFormat
                            // Prefer Image API (API 21+) for device-independent
                            // color format handling; fall back to raw ByteBuffer
                            val image = decoder.getOutputImage(outputIndex)
                            val bitmap = if (image != null) {
                                imageToBitmap(image)
                            } else {
                                val outputBuffer = decoder.getOutputBuffer(outputIndex)
                                if (outputBuffer != null) {
                                    outputBufferToBitmap(outputBuffer, outputFormat)
                                } else null
                            }
                            bitmap?.let { handleDecodedFrame(it) }
                        } catch (e: Exception) {
                            Log.e(TAG, "Frame conversion failed, skipping frame", e)
                        } finally {
                            decoder.releaseOutputBuffer(outputIndex, false)
                        }
                    }
                    outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                        Log.d(TAG, "Output format changed: ${decoder.outputFormat}")
                    }
                    // INFO_TRY_AGAIN_LATER or INFO_OUTPUT_BUFFERS_CHANGED: no-op
                }
            } catch (e: MediaCodec.CodecException) {
                Log.e(TAG, "MediaCodec error in decode loop", e)
            } catch (e: IllegalStateException) {
                Log.e(TAG, "MediaCodec illegal state", e)
                break
            }

            if (!didWork) {
                Thread.sleep(IDLE_SLEEP_MS)
            }
        }
        Log.d(TAG, "Decode loop exited")
    }

    // -- NAL unit handling ----------------------------------------------------

    /**
     * Find NAL start codes in [data] and feed each NAL unit to the decoder.
     *
     * The Tello sends raw H264 NAL units. Each unit is prefixed with a
     * four-byte start code (0x00 0x00 0x00 0x01). If no start code is
     * found, the entire packet is submitted as-is (some packets contain
     * a single NAL without a leading start code).
     */
    private fun feedNalUnits(decoder: MediaCodec, data: ByteArray) {
        val startCodes = findNalStartCodes(data)

        if (startCodes.isEmpty()) {
            // No start code found; submit the whole packet
            submitToDecoder(decoder, data)
            return
        }

        for (i in startCodes.indices) {
            val start = startCodes[i]
            val end = if (i + 1 < startCodes.size) startCodes[i + 1] else data.size
            val nalUnit = data.copyOfRange(start, end)
            submitToDecoder(decoder, nalUnit)
        }
    }

    /**
     * Locate all 4-byte NAL start codes (0x00 0x00 0x00 0x01) in [data].
     */
    private fun findNalStartCodes(data: ByteArray): List<Int> {
        val positions = mutableListOf<Int>()
        if (data.size < 4) return positions
        for (i in 0..data.size - 4) {
            if (data[i] == 0x00.toByte() &&
                data[i + 1] == 0x00.toByte() &&
                data[i + 2] == 0x00.toByte() &&
                data[i + 3] == 0x01.toByte()
            ) {
                positions.add(i)
            }
        }
        return positions
    }

    /**
     * Submit a single NAL unit to the decoder's input buffer.
     */
    private fun submitToDecoder(decoder: MediaCodec, nalUnit: ByteArray) {
        try {
            val inputIndex = decoder.dequeueInputBuffer(DECODE_POLL_TIMEOUT_US)
            if (inputIndex >= 0) {
                val inputBuffer = decoder.getInputBuffer(inputIndex) ?: return
                inputBuffer.clear()
                inputBuffer.put(nalUnit)
                decoder.queueInputBuffer(inputIndex, 0, nalUnit.size, 0, 0)
            }
        } catch (e: MediaCodec.CodecException) {
            Log.e(TAG, "Error submitting NAL unit to decoder", e)
        } catch (e: IllegalStateException) {
            Log.e(TAG, "Decoder not in executing state", e)
        }
    }

    // -- Frame conversion -----------------------------------------------------

    /**
     * Convert a decoded [Image] (from [MediaCodec.getOutputImage]) to an RGB
     * [Bitmap] and downscale to [targetWidth] x [targetHeight].
     *
     * The Image API normalizes plane access regardless of the underlying
     * color format (NV12, I420, etc.), avoiding hardcoded assumptions.
     */
    private fun imageToBitmap(image: Image): Bitmap? {
        val width = image.width
        val height = image.height
        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        val yBuffer = yPlane.buffer
        val uBuffer = uPlane.buffer
        val vBuffer = vPlane.buffer

        val yRowStride = yPlane.rowStride
        val uvRowStride = uPlane.rowStride
        val uvPixelStride = uPlane.pixelStride

        val nv21Size = width * height + width * (height / 2)
        val nv21 = ByteArray(nv21Size)

        // Copy Y plane row by row (row stride may exceed width)
        var pos = 0
        for (row in 0 until height) {
            yBuffer.position(row * yRowStride)
            yBuffer.get(nv21, pos, width)
            pos += width
        }

        // Build NV21 chroma: V then U interleaved
        val uvHeight = height / 2
        val uvWidth = width / 2
        for (row in 0 until uvHeight) {
            for (col in 0 until uvWidth) {
                val uvIndex = row * uvRowStride + col * uvPixelStride
                nv21[pos++] = vBuffer.get(uvIndex) // V first for NV21
                nv21[pos++] = uBuffer.get(uvIndex) // then U
            }
        }

        val yuvImage = YuvImage(nv21, ImageFormat.NV21, width, height, null)
        val outputStream = ByteArrayOutputStream()
        yuvImage.compressToJpeg(Rect(0, 0, width, height), 90, outputStream)

        val jpegBytes = outputStream.toByteArray()
        val fullBitmap = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size) ?: return null

        val scaled = Bitmap.createScaledBitmap(fullBitmap, targetWidth, targetHeight, true)
        if (scaled !== fullBitmap) {
            fullBitmap.recycle()
        }
        return scaled
    }

    /**
     * Convert a decoded YUV output buffer to an RGB [Bitmap] and downscale
     * to [targetWidth] x [targetHeight].
     *
     * Fallback path when [MediaCodec.getOutputImage] returns null.
     * Uses [YuvImage] + JPEG compression, matching the pattern in
     * [CameraXFrameSource.imageProxyToBitmap].
     */
    private fun outputBufferToBitmap(buffer: java.nio.ByteBuffer, format: MediaFormat): Bitmap? {
        val width = format.getInteger(MediaFormat.KEY_WIDTH)
        val height = format.getInteger(MediaFormat.KEY_HEIGHT)
        val stride = if (format.containsKey(MediaFormat.KEY_STRIDE)) {
            format.getInteger(MediaFormat.KEY_STRIDE)
        } else {
            width
        }

        // Account for slice height (may be larger than height on some decoders)
        val sliceHeight = if (format.containsKey(MediaFormat.KEY_SLICE_HEIGHT)) {
            format.getInteger(MediaFormat.KEY_SLICE_HEIGHT).let { if (it > 0) it else height }
        } else {
            height
        }

        // MediaCodec outputs NV12 (COLOR_FormatYUV420SemiPlanar) by default.
        // YuvImage expects NV21 (VU interleaved), so swap U and V bytes.
        // Y plane occupies stride * sliceHeight bytes (includes padding rows)
        val yBufferSize = stride * sliceHeight
        val uvBufferSize = stride * (sliceHeight / 2)

        // Ensure buffer has enough data
        val bufferTotalSize = yBufferSize + uvBufferSize
        if (buffer.remaining() < bufferTotalSize) {
            // Fall back to actual remaining size
            val available = buffer.remaining()
            if (available < width * height) return null
            return outputBufferToBitmapFallback(buffer, width, height)
        }

        // Output NV21 array sized for actual image dimensions (no padding)
        val nv21YSize = stride * height
        val nv21UvSize = stride * (height / 2)
        val nv21Total = nv21YSize + nv21UvSize
        val nv21 = ByteArray(nv21Total)

        // Copy Y rows (skip padding between sliceHeight and height)
        val fullBuffer = ByteArray(bufferTotalSize)
        buffer.get(fullBuffer, 0, bufferTotalSize)

        System.arraycopy(fullBuffer, 0, nv21, 0, nv21YSize)

        // Copy UV rows from offset yBufferSize (after Y plane including padding)
        val uvSrcOffset = yBufferSize
        val uvCopySize = nv21UvSize.coerceAtMost(uvBufferSize)
        System.arraycopy(fullBuffer, uvSrcOffset, nv21, nv21YSize, uvCopySize)

        // NV12 -> NV21: swap U/V pairs in the chroma plane
        for (i in nv21YSize until nv21Total - 1 step 2) {
            val temp = nv21[i]
            nv21[i] = nv21[i + 1]
            nv21[i + 1] = temp
        }

        val yuvImage = YuvImage(nv21, ImageFormat.NV21, stride, height, null)
        val outputStream = ByteArrayOutputStream()
        yuvImage.compressToJpeg(Rect(0, 0, width, height), 90, outputStream)

        val jpegBytes = outputStream.toByteArray()
        val fullBitmap = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size) ?: return null

        // Downscale to target resolution
        val scaled = Bitmap.createScaledBitmap(fullBitmap, targetWidth, targetHeight, true)
        if (scaled !== fullBitmap) {
            fullBitmap.recycle()
        }

        return scaled
    }

    /**
     * Fallback YUV-to-Bitmap conversion when the buffer size doesn't match
     * the expected stride-based layout.
     */
    private fun outputBufferToBitmapFallback(
        buffer: java.nio.ByteBuffer,
        width: Int,
        height: Int,
    ): Bitmap? {
        val ySize = width * height
        val uvSize = width * height / 2
        val totalSize = ySize + uvSize
        if (buffer.remaining() < totalSize) return null

        val nv21 = ByteArray(totalSize)
        buffer.get(nv21, 0, totalSize)

        // NV12 -> NV21: swap U/V
        for (i in ySize until totalSize - 1 step 2) {
            val temp = nv21[i]
            nv21[i] = nv21[i + 1]
            nv21[i + 1] = temp
        }

        val yuvImage = YuvImage(nv21, ImageFormat.NV21, width, height, null)
        val outputStream = ByteArrayOutputStream()
        yuvImage.compressToJpeg(Rect(0, 0, width, height), 90, outputStream)

        val jpegBytes = outputStream.toByteArray()
        val fullBitmap = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size) ?: return null

        val scaled = Bitmap.createScaledBitmap(fullBitmap, targetWidth, targetHeight, true)
        if (scaled !== fullBitmap) {
            fullBitmap.recycle()
        }

        return scaled
    }

    // -- Frozen frame detection -----------------------------------------------

    /**
     * Check for frozen frames and update [latestFrame] + invoke callback.
     */
    private fun handleDecodedFrame(bitmap: Bitmap) {
        val hash = computeFrameHash(bitmap)
        if (hash == lastFrameHash) {
            frozenFrameCount++
            if (frozenFrameCount >= frozenFrameThreshold) {
                Log.w(TAG, "Frozen frame detected ($frozenFrameCount identical)")
                onStreamFrozen?.invoke()
                frozenFrameCount = 0
                // Don't update latestFrame with a frozen frame
                bitmap.recycle()
                return
            }
        } else {
            frozenFrameCount = 0
        }
        lastFrameHash = hash

        var callback: ((Bitmap) -> Unit)? = null
        lock.withLock {
            latestFrame?.recycle()
            latestFrame = bitmap
            callback = frameCallback
        }
        // Give the callback a COPY so the consumer owns it independently.
        // TelloFrameSource owns latestFrame; the consumer owns the copy.
        val frameCopy = bitmap.copy(Bitmap.Config.ARGB_8888, false)
        callback?.invoke(frameCopy)
    }

    /**
     * Hash a small region of center pixels for frozen-frame detection.
     *
     * Mirrors the Python approach in `tello.py` which hashes a 10x10
     * block at the frame center.
     */
    private fun computeFrameHash(bitmap: Bitmap): Long {
        val blockSize = 8
        val cx = bitmap.width / 2
        val cy = bitmap.height / 2
        val left = (cx - blockSize / 2).coerceAtLeast(0)
        val top = (cy - blockSize / 2).coerceAtLeast(0)
        val right = (left + blockSize).coerceAtMost(bitmap.width)
        val bottom = (top + blockSize).coerceAtMost(bitmap.height)

        var hash = 17L
        for (y in top until bottom) {
            for (x in left until right) {
                hash = hash * 31 + bitmap.getPixel(x, y).toLong()
            }
        }
        return hash
    }
}
