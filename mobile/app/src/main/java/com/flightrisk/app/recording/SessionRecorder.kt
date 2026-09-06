package com.flightrisk.app.recording

import android.graphics.Bitmap
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

class SessionRecorder(
    private val recordingsDir: File,
    private val fps: Int = 15,
    private val width: Int = 960,
    private val height: Int = 720,
    private val bitRate: Int = 2_000_000,
) {
    companion object {
        private const val TAG = "SessionRecorder"
        private const val MIME_TYPE = MediaFormat.MIMETYPE_VIDEO_AVC
        private const val I_FRAME_INTERVAL = 5
        private const val TIMEOUT_US = 10_000L
    }

    private val lock = ReentrantLock()

    @Volatile
    var isRecording: Boolean = false
        private set

    @Volatile
    var frameCount: Int = 0
        private set

    private var encoder: MediaCodec? = null
    private var muxer: MediaMuxer? = null
    private var trackIndex: Int = -1
    private var muxerStarted: Boolean = false
    private var filePath: String? = null

    init {
        recordingsDir.mkdirs()
    }

    fun start(filename: String? = null): String {
        if (isRecording) return filePath ?: ""

        val name = filename ?: run {
            val ts = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
            "session_$ts.mp4"
        }
        val file = File(recordingsDir, name)
        filePath = file.absolutePath

        val format = MediaFormat.createVideoFormat(MIME_TYPE, width, height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
            setInteger(MediaFormat.KEY_BIT_RATE, bitRate)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, I_FRAME_INTERVAL)
        }

        try {
            encoder = MediaCodec.createEncoderByType(MIME_TYPE).also {
                it.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                it.start()
            }
            muxer = MediaMuxer(file.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
            trackIndex = -1
            muxerStarted = false
            frameCount = 0
            isRecording = true
            Log.i(TAG, "Recording started: $filePath")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording: ${e.message}")
            releaseCodec()
        }

        return filePath ?: ""
    }

    fun writeFrame(bitmap: Bitmap) {
        if (!isRecording) return

        lock.withLock {
            val enc = encoder ?: return

            val inputIndex = enc.dequeueInputBuffer(TIMEOUT_US)
            if (inputIndex < 0) return

            val inputBuffer = enc.getInputBuffer(inputIndex) ?: return
            inputBuffer.clear()

            val scaled = Bitmap.createScaledBitmap(bitmap, width, height, true)
            val yuvData = bitmapToNv12(scaled)
            if (scaled !== bitmap) scaled.recycle()

            inputBuffer.put(yuvData)

            val pts = frameCount * 1_000_000L / fps
            enc.queueInputBuffer(inputIndex, 0, yuvData.size, pts, 0)
            frameCount++

            drainEncoder(false)
        }
    }

    fun stop(): String? {
        if (!isRecording) return null

        lock.withLock {
            isRecording = false

            try {
                val enc = encoder
                if (enc != null) {
                    val inputIndex = enc.dequeueInputBuffer(TIMEOUT_US)
                    if (inputIndex >= 0) {
                        enc.queueInputBuffer(inputIndex, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                    }
                    drainEncoder(true)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Error signaling EOS: ${e.message}")
            }

            releaseCodec()

            val duration = if (fps > 0) frameCount.toFloat() / fps else 0f
            Log.i(TAG, "Recording saved: $filePath ($frameCount frames, ${"%.1f".format(duration)}s)")
        }

        return filePath
    }

    private fun drainEncoder(endOfStream: Boolean) {
        val enc = encoder ?: return
        val bufferInfo = MediaCodec.BufferInfo()

        while (true) {
            val outputIndex = enc.dequeueOutputBuffer(bufferInfo, TIMEOUT_US)

            when {
                outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    val mux = muxer ?: return
                    trackIndex = mux.addTrack(enc.outputFormat)
                    mux.start()
                    muxerStarted = true
                }
                outputIndex >= 0 -> {
                    val outputBuffer = enc.getOutputBuffer(outputIndex) ?: continue

                    if (bufferInfo.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                        bufferInfo.size = 0
                    }

                    if (bufferInfo.size > 0 && muxerStarted) {
                        outputBuffer.position(bufferInfo.offset)
                        outputBuffer.limit(bufferInfo.offset + bufferInfo.size)
                        muxer?.writeSampleData(trackIndex, outputBuffer, bufferInfo)
                    }

                    enc.releaseOutputBuffer(outputIndex, false)

                    if (bufferInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
                }
                else -> {
                    if (!endOfStream) return
                }
            }
        }
    }

    private fun releaseCodec() {
        try { encoder?.stop() } catch (_: Exception) {}
        try { encoder?.release() } catch (_: Exception) {}
        encoder = null

        try {
            if (muxerStarted) muxer?.stop()
        } catch (_: Exception) {}
        try { muxer?.release() } catch (_: Exception) {}
        muxer = null
        muxerStarted = false
    }

    private fun bitmapToNv12(bitmap: Bitmap): ByteArray {
        val w = bitmap.width
        val h = bitmap.height
        val pixels = IntArray(w * h)
        bitmap.getPixels(pixels, 0, w, 0, 0, w, h)

        val ySize = w * h
        val uvSize = w * h / 2
        val nv12 = ByteArray(ySize + uvSize)

        var yIndex = 0
        var uvIndex = ySize

        for (row in 0 until h) {
            for (col in 0 until w) {
                val argb = pixels[row * w + col]
                val r = (argb shr 16) and 0xFF
                val g = (argb shr 8) and 0xFF
                val b = argb and 0xFF

                val y = ((66 * r + 129 * g + 25 * b + 128) shr 8) + 16
                nv12[yIndex++] = y.coerceIn(0, 255).toByte()

                if (row % 2 == 0 && col % 2 == 0 && uvIndex < nv12.size - 1) {
                    val u = ((-38 * r - 74 * g + 112 * b + 128) shr 8) + 128
                    val v = ((112 * r - 94 * g - 18 * b + 128) shr 8) + 128
                    nv12[uvIndex++] = u.coerceIn(0, 255).toByte()
                    nv12[uvIndex++] = v.coerceIn(0, 255).toByte()
                }
            }
        }
        return nv12
    }
}
