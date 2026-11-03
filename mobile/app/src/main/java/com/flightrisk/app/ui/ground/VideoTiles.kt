package com.flightrisk.app.ui.ground

import com.flightrisk.contract.Frame
import com.flightrisk.contract.FrameBox
import java.util.Base64
import kotlin.math.max
import kotlin.math.min

/**
 * The logic behind the Ground screen's video tiles. It mirrors the iOS app's `VideoTileLogic`
 * (docs/mobile-contract.md, "Limits both apps apply"): one tile per drone, at most 64 boxes per
 * tile, a frame older than 3 s shows NO VIDEO, and a frame that arrived before the link dropped is
 * marked "Not live". Everything here is plain Kotlin so it is tested on the JVM; the Compose code
 * in `VideoTilesSection` only draws what this produces.
 *
 * The phone only shows frames the ground sends. It never asks the ground to start or stop video
 * (`set_stream_video` is not in the phone's allow-list).
 */

/** A detection box in normalized frame coordinates, already clamped to 0..1 with left < right and top < bottom. */
data class VideoTileBox(
    val x1: Double,
    val y1: Double,
    val x2: Double,
    val y2: Double,
    val matched: Boolean,
    /** Present only when the ground sent a non-empty track id. */
    val trackId: String?,
)

/** How a tile is shown. */
enum class VideoTileMode {
    /** A frame newer than the staleness limit, ground state live. */
    LIVE,

    /** A frame newer than the staleness limit, but the ground state is stale (the link dropped). Tagged "Not live". */
    NOT_LIVE,

    /** No frame newer than the staleness limit: placeholder, no boxes, no image. */
    NO_VIDEO,
}

/** Everything one tile needs, computed from the latest frame of one drone. */
data class VideoTileModel(
    val id: String,
    val droneLabel: String,
    val mode: VideoTileMode,
    /** Base64 JPEG of a fresh frame; null for a detections-only frame or a stale tile. */
    val imageBase64: String?,
    val boxes: List<VideoTileBox>,
    /** Width over height of the source frame. */
    val aspect: Double,
    /** True when this fresh frame says the ground is not streaming video. Never true on a stale tile. */
    val videoOffAtGround: Boolean,
    /** Why a stale tile shows no video; null on a fresh tile. */
    val noVideoReason: String?,
    /** Boxes beyond [VideoTileLogic.MAX_BOXES_PER_TILE] that are not drawn. */
    val hiddenBoxCount: Int,
) {
    val accessibilityText: String
        get() = when (mode) {
            VideoTileMode.NO_VIDEO -> "$droneLabel video, no video, ${noVideoReason ?: "no recent frame"}"
            VideoTileMode.LIVE, VideoTileMode.NOT_LIVE -> {
                val total = boxes.size + hiddenBoxCount
                var detections = "$total detection${if (total == 1) "" else "s"}"
                val matches = boxes.count { it.matched }
                if (matches > 0) detections += ", $matches match${if (matches == 1) "" else "es"}"
                if (mode == VideoTileMode.NOT_LIVE) "$droneLabel video, not live, $detections" else "$droneLabel video, $detections"
            }
        }
}

/** The latest frame of a drone and when this phone received it (monotonic milliseconds). */
data class ReceivedFrame(val frame: Frame, val receivedAtMs: Long)

object VideoTileLogic {
    /** A tile whose newest frame is older than this shows NO VIDEO. */
    const val STALE_AFTER_MS = 3_000L

    /** The aspect ratio used when the frame carries no usable size. */
    const val DEFAULT_ASPECT = 4.0 / 3.0

    /** Boxes drawn per tile; more are only counted ("+N more"). */
    const val MAX_BOXES_PER_TILE = 64

    /** Tiles shown; the client keeps frames for fewer drones than this, so it is only a guard. */
    const val MAX_TILES = 16

    /** Longest base64 text decoded: the client already discards frames over 1 MiB, this is the same bound. */
    const val MAX_BASE64_CHARS = 1024 * 1024

    /** Images with more source pixels than this are refused (the edge sends 320x180; 4 MP is about 2000 x 2000). */
    const val MAX_SOURCE_PIXELS = 4_000_000

    /** The most pixels a decoded bitmap may have (about 4 MB as RGB_565), whatever the tile asks for. */
    const val MAX_DECODED_PIXELS = 2_000_000

    /** Upper bound on the long edge a tile may ask for. Subsampling is by powers of two, so the decoded edge can be up to twice the request. */
    const val MAX_DECODE_EDGE = 2048

    /** The least decoded long edge asked for. */
    const val MIN_DECODE_EDGE = 100

    const val VIDEO_OFF_TEXT = "Video is off at the ground. Turn it on from the dashboard."
    const val NO_IMAGE_TEXT = "No image in this frame."
    const val UNREADABLE_TEXT = "Video frame could not be read."
    const val NOT_LIVE_TAG = "Not live"
    const val NO_VIDEO_TITLE = "NO VIDEO"

    /**
     * True when the frame arrived more than [STALE_AFTER_MS] ago. A receive time in the future (which a
     * monotonic clock never gives, but a test or a restored clock might) counts as stale too.
     */
    fun isStale(receivedAtMs: Long?, nowMs: Long): Boolean {
        if (receivedAtMs == null) return true
        val age = nowMs - receivedAtMs
        return age > STALE_AFTER_MS || age < -STALE_AFTER_MS
    }

    /** Width over height from the frame's size, or [DEFAULT_ASPECT] when either side is unusable. Kept within 1:4 to 4:1. */
    fun aspect(width: Int?, height: Int?): Double {
        if (width == null || height == null || width <= 0 || height <= 0) return DEFAULT_ASPECT
        return (width.toDouble() / height.toDouble()).coerceIn(0.25, 4.0)
    }

    /**
     * A box from `[x1, y1, x2, y2]` in 0..1. Coordinates outside 0..1 are clamped and swapped corners are
     * put in order. Returns null for the wrong count, a NaN or infinite value, or a box with no area left.
     */
    fun box(bboxNorm: List<Double>?, matched: Boolean = false, trackId: String? = null): VideoTileBox? {
        val b = bboxNorm ?: return null
        if (b.size != 4 || b.any { !it.isFinite() }) return null
        val c = b.map { it.coerceIn(0.0, 1.0) }
        val left = min(c[0], c[2])
        val right = max(c[0], c[2])
        val top = min(c[1], c[3])
        val bottom = max(c[1], c[3])
        if (right <= left || bottom <= top) return null
        val track = trackId?.let { GroundText.shortId(it).trim() }?.takeIf { it.isNotEmpty() }
        return VideoTileBox(left, top, right, bottom, matched, track)
    }

    /** At most [MAX_BOXES_PER_TILE] valid boxes, and how many raw entries were left unexamined. */
    fun cappedBoxes(frameBoxes: List<FrameBox>?): Pair<List<VideoTileBox>, Int> {
        val raw = frameBoxes ?: emptyList()
        val result = ArrayList<VideoTileBox>()
        var index = 0
        while (index < raw.size && result.size < MAX_BOXES_PER_TILE) {
            val b = raw[index]
            box(b.bboxNorm, b.matched ?: false, b.trackId)?.let(result::add)
            index++
        }
        return result to (raw.size - index)
    }

    fun noVideoReason(droneLabel: String, stateStale: Boolean): String =
        if (stateStale) "no frames from the ground" else "no recent frame from $droneLabel"

    /** Drone ids in natural order, so `drone-2` comes before `drone-10`. */
    val naturalOrder: Comparator<String> = Comparator { a, b ->
        val c = compareNatural(a, b)
        if (c != 0) c else a.compareTo(b)
    }

    private fun compareNatural(a: String, b: String): Int {
        var i = 0
        var j = 0
        while (i < a.length && j < b.length) {
            if (a[i].isDigit() && b[j].isDigit()) {
                var ei = i
                while (ei < a.length && a[ei].isDigit()) ei++
                var ej = j
                while (ej < b.length && b[ej].isDigit()) ej++
                val na = a.substring(i, ei).trimStart('0')
                val nb = b.substring(j, ej).trimStart('0')
                if (na.length != nb.length) return na.length.compareTo(nb.length)
                val c = na.compareTo(nb)
                if (c != 0) return c
                i = ei
                j = ej
            } else {
                val c = a[i].lowercaseChar().compareTo(b[j].lowercaseChar())
                if (c != 0) return c
                i++
                j++
            }
        }
        return (a.length - i).compareTo(b.length - j)
    }

    /**
     * One tile per drone id in natural order. [streamVideoEnabled] is the ground's last `stream_video_state`;
     * it is not trusted while the ground state is stale.
     */
    fun makeTiles(
        frames: Map<String, ReceivedFrame>,
        nowMs: Long,
        streamVideoEnabled: Boolean?,
        stateStale: Boolean,
    ): List<VideoTileModel> = frames.keys.sortedWith(naturalOrder).take(MAX_TILES).mapNotNull { id ->
        val received = frames[id] ?: return@mapNotNull null
        val frame = received.frame
        val label = if (id.isEmpty()) "unknown drone" else GroundText.shortId(id)
        val aspect = aspect(frame.frameWidth, frame.frameHeight)
        if (isStale(received.receivedAtMs, nowMs)) {
            VideoTileModel(
                id = id, droneLabel = label, mode = VideoTileMode.NO_VIDEO, imageBase64 = null, boxes = emptyList(),
                aspect = aspect, videoOffAtGround = false, noVideoReason = noVideoReason(label, stateStale), hiddenBoxCount = 0,
            )
        } else {
            val (boxes, hidden) = cappedBoxes(frame.boxes)
            val videoOff = frame.streamVideo == false || (!stateStale && streamVideoEnabled == false)
            VideoTileModel(
                id = id, droneLabel = label, mode = if (stateStale) VideoTileMode.NOT_LIVE else VideoTileMode.LIVE,
                imageBase64 = frame.image?.takeIf { it.isNotEmpty() }, boxes = boxes, aspect = aspect,
                videoOffAtGround = videoOff, noVideoReason = null, hiddenBoxCount = hidden,
            )
        }
    }

    /** One column for a single drone and on a narrow upright phone; two for two or more drones on a wide or landscape screen. */
    fun columns(count: Int, widthDp: Float, heightDp: Float): Int =
        if (count >= 2 && (widthDp >= 600f || heightDp < 480f)) 2 else 1

    /** The pixel size to decode for a tile this many pixels wide, rounded up to 50 and bounded. */
    fun targetPixelSize(widthPx: Float): Int {
        if (!widthPx.isFinite() || widthPx <= 0f) return 600
        val raw = Math.ceil(widthPx / 50.0) * 50.0
        return raw.coerceIn(MIN_DECODE_EDGE.toDouble(), MAX_DECODE_EDGE.toDouble()).toInt()
    }

    /**
     * The area a decoded image occupies inside a tile, fitted whole and centered like the dashboard's
     * `getVideoRect`. Returns left, top, width, height. Without a usable image size it is the whole tile.
     */
    fun imageRect(imageWidth: Int?, imageHeight: Int?, tileWidth: Float, tileHeight: Float): FloatArray {
        if (imageWidth == null || imageHeight == null || imageWidth <= 0 || imageHeight <= 0 || tileWidth <= 0f || tileHeight <= 0f) {
            return floatArrayOf(0f, 0f, max(tileWidth, 0f), max(tileHeight, 0f))
        }
        val scale = min(tileWidth / imageWidth, tileHeight / imageHeight)
        val w = imageWidth * scale
        val h = imageHeight * scale
        return floatArrayOf((tileWidth - w) / 2f, (tileHeight - h) / 2f, w, h)
    }

    // ---------------------------------------------------------------- decode guard

    /** The result of checking a frame's image text before it is handed to the platform decoder. */
    sealed interface Guarded {
        class Ok(val jpeg: ByteArray) : Guarded
        data object Refused : Guarded
    }

    /**
     * Checks and decodes base64 text from the ground into JPEG bytes: not empty, no longer than
     * [MAX_BASE64_CHARS], valid base64 (line breaks are ignored) and starting with the JPEG signature.
     * Never throws; whatever is wrong gives [Guarded.Refused]. Hostile text costs at most one pass.
     */
    fun guard(base64: String?): Guarded {
        if (base64.isNullOrEmpty() || base64.length > MAX_BASE64_CHARS) return Guarded.Refused
        val bytes = try {
            Base64.getMimeDecoder().decode(base64)
        } catch (_: IllegalArgumentException) {
            return Guarded.Refused
        }
        val isJpeg = bytes.size > 4 &&
            bytes[0] == 0xFF.toByte() && bytes[1] == 0xD8.toByte() && bytes[2] == 0xFF.toByte()
        return if (isJpeg) Guarded.Ok(bytes) else Guarded.Refused
    }

    /** True when the source size is usable and not larger than [MAX_SOURCE_PIXELS]. */
    fun sourceSizeAllowed(width: Int, height: Int): Boolean =
        width > 0 && height > 0 && width.toLong() * height.toLong() <= MAX_SOURCE_PIXELS

    /**
     * The power-of-two subsampling for a decode that keeps the long edge at least [targetEdge] (never
     * upscaling) and the decoded area at most [MAX_DECODED_PIXELS], so the full-size bitmap is never held.
     * The area limit wins over the target. The target is bounded by [MAX_DECODE_EDGE].
     */
    fun sampleSize(width: Int, height: Int, targetEdge: Int): Int {
        val target = targetEdge.coerceIn(1, MAX_DECODE_EDGE)
        var sample = 1
        var w = max(width, 1)
        var h = max(height, 1)
        while (max(w, h) / 2 >= target || w.toLong() * h > MAX_DECODED_PIXELS) {
            sample *= 2
            w = max(w / 2, 1)
            h = max(h / 2, 1)
            if (sample >= 1 shl 14) break
        }
        return sample
    }
}
