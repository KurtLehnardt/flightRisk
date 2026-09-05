package com.flightrisk.app.drone

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
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

    // Frozen frame tracking
    private var lastFrameHash: Long = 0
    private var frozenFrameCount = 0
    private val frozenFrameThreshold = 100

    // -- FrameSource interface ------------------------------------------------

    override fun start() {
        if (isRunning) return
        isRunning = true

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
        lock.withLock {
            latestFrame = null
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
     * Receive UDP packets from the Tello and enqueue raw data for decoding.
     */
    private fun receiveLoop() {
        val buffer = ByteArray(UDP_BUFFER_SIZE)
        val packet = DatagramPacket(buffer, buffer.size)

        while (isRunning) {
            try {
                udpSocket?.receive(packet) ?: break
                // Copy received bytes and enqueue
                val data = packet.data.copyOfRange(packet.offset, packet.offset + packet.length)
                nalQueue.add(data)
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
                            val outputBuffer = decoder.getOutputBuffer(outputIndex)
                            val outputFormat = decoder.outputFormat
                            if (outputBuffer != null) {
                                val bitmap = outputBufferToBitmap(outputBuffer, outputFormat)
                                if (bitmap != null) {
                                    handleDecodedFrame(bitmap)
                                }
                            }
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
     * Convert a decoded YUV output buffer to an RGB [Bitmap] and downscale
     * to [targetWidth] x [targetHeight].
     *
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

        // MediaCodec outputs NV12 (COLOR_FormatYUV420SemiPlanar) by default.
        // YuvImage expects NV21 (VU interleaved), so swap U and V bytes.
        val ySize = stride * height
        val uvSize = stride * height / 2

        // Ensure buffer has enough data
        val totalSize = ySize + uvSize
        if (buffer.remaining() < totalSize) {
            // Fall back to actual remaining size
            val available = buffer.remaining()
            if (available < width * height) return null
            return outputBufferToBitmapFallback(buffer, width, height)
        }

        val nv21 = ByteArray(totalSize)
        buffer.position(buffer.position()) // ensure position is at start of data
        buffer.get(nv21, 0, totalSize)

        // NV12 -> NV21: swap U/V pairs in the chroma plane
        for (i in ySize until totalSize - 1 step 2) {
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
            latestFrame = bitmap
            callback = frameCallback
        }
        callback?.invoke(bitmap)
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
