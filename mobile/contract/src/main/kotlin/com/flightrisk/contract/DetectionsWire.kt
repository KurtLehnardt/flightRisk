package com.flightrisk.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.util.Base64

/**
 * One person detection in a phone edge's `detections` message
 * (`EdgeRunner.to_dict` in `flightrisk/edge.py`). Images are base64 JPEG text;
 * use [base64Jpeg] to build them from bytes.
 */
data class WireDetection(
    /** `[x1, y1, x2, y2]` in pixels of the camera frame. */
    val bbox: List<Double>,
    val confidence: Double,
    val reidEmbedding: List<Double>?,
    /** Sent only when the user turned face matching on. */
    val faceEmbedding: List<Double>?,
    /** Base64 JPEG of the person crop, or null. */
    val crop: String?,
) {
    init {
        require(bbox.size == 4) { "bbox must have four values, got ${bbox.size}" }
    }
}

/** The legacy `detections` message a phone edge sends to the ground's edge link. */
data class DetectionsMessage(
    /** Seconds since the Unix epoch. */
    val timestamp: Double,
    val frameId: Long,
    val frameWidth: Int,
    val frameHeight: Int,
    /** Base64 JPEG of the whole frame, or null. */
    val thumbnail: String?,
    val detections: List<WireDetection>,
)

/** Encodes and decodes the legacy `detections` message (docs/mobile-contract.md, "Phone as an edge"). */
object DetectionsWire {
    const val TYPE = "detections"

    /** `LEGACY_MAX_BYTES` in `flightrisk/fleet_protocol/constants.py`: 1 MiB of compact JSON. */
    const val MAX_BYTES = 1_048_576

    /** The keys of a message, exactly the ones `EdgeRunner.to_dict` writes for a phone. */
    val MESSAGE_KEYS: Set<String> =
        setOf("type", "timestamp", "frame_id", "frame_width", "frame_height", "thumbnail", "detections")

    /** The keys of a detection without face matching. */
    val DETECTION_KEYS: Set<String> = setOf("bbox", "confidence", "reid_embedding", "crop")

    /** Keys a phone edge never sends: it flies nothing and its detector is not a registered profile. */
    val NEVER_SENT_KEYS: Set<String> = setOf("drone_id", "detection_profile", "face_model")

    /** Base64 text of JPEG bytes, with the standard alphabet and padding (Python's `b64encode`). */
    fun base64Jpeg(jpeg: ByteArray): String = Base64.getEncoder().encodeToString(jpeg)

    /** The outcome of [encode]. */
    sealed interface EncodeResult {
        /**
         * The message fits. [json] is compact JSON of [bytes] UTF-8 bytes;
         * [droppedCrops] crops and, when [droppedThumbnail], the thumbnail
         * were removed to fit.
         */
        data class Ok(
            val json: String,
            val bytes: Int,
            val droppedCrops: Int,
            val droppedThumbnail: Boolean,
        ) : EncodeResult

        /** The message is over [MAX_BYTES] even without crops and thumbnail; do not send it. */
        data class TooLarge(val bytes: Int) : EncodeResult
    }

    /**
     * Encodes [message] as compact JSON for the edge link.
     *
     * - `face_embedding` is written only when [faceMatchingEnabled] is true.
     * - `thumbnail` is null when [streamVideo] is false (after the ground's
     *   `stream_video {enabled: false}`).
     * - `drone_id`, `detection_profile` and `face_model` are never written.
     * - Above [maxBytes], crops are dropped first (largest first), then the
     *   thumbnail; if it still does not fit, the result is [EncodeResult.TooLarge].
     */
    fun encode(
        message: DetectionsMessage,
        faceMatchingEnabled: Boolean = false,
        streamVideo: Boolean = true,
        maxBytes: Int = MAX_BYTES,
    ): EncodeResult {
        var thumbnail = if (streamVideo) message.thumbnail else null
        val crops = message.detections.map { it.crop }.toMutableList()

        var json = toJson(message, thumbnail, crops, faceMatchingEnabled).toString()
        var size = utf8Size(json)
        var droppedCrops = 0
        var droppedThumbnail = false

        if (size > maxBytes) {
            // Base64 needs no JSON escaping, so replacing "<crop>" by null
            // shrinks the text by exactly (length + 2 quotes) - 4 bytes.
            val order = crops.indices.filter { crops[it] != null }.sortedByDescending { crops[it]!!.length }
            for (i in order) {
                if (size <= maxBytes) break
                size -= crops[i]!!.length + 2 - 4
                crops[i] = null
                droppedCrops++
            }
            if (size > maxBytes && thumbnail != null) {
                size -= thumbnail.length + 2 - 4
                thumbnail = null
                droppedThumbnail = true
            }
            json = toJson(message, thumbnail, crops, faceMatchingEnabled).toString()
            size = utf8Size(json)
        }
        return if (size <= maxBytes) EncodeResult.Ok(json, size, droppedCrops, droppedThumbnail)
        else EncodeResult.TooLarge(size)
    }

    /** The message as a JSON object, before any size cap. */
    fun toJsonObject(
        message: DetectionsMessage,
        faceMatchingEnabled: Boolean = false,
        streamVideo: Boolean = true,
    ): JsonObject = toJson(
        message,
        if (streamVideo) message.thumbnail else null,
        message.detections.map { it.crop },
        faceMatchingEnabled,
    )

    private fun toJson(
        message: DetectionsMessage,
        thumbnail: String?,
        crops: List<String?>,
        faceMatchingEnabled: Boolean,
    ): JsonObject = buildJsonObject {
        put("type", TYPE)
        put("timestamp", wireNumber(message.timestamp))
        put("frame_id", message.frameId)
        put("frame_width", message.frameWidth)
        put("frame_height", message.frameHeight)
        put("thumbnail", thumbnail?.let(::JsonPrimitive) ?: JsonNull)
        put(
            "detections",
            buildJsonArray {
                message.detections.forEachIndexed { i, d ->
                    add(
                        buildJsonObject {
                            put("bbox", numberArray(d.bbox))
                            put("confidence", wireNumber(d.confidence))
                            put("reid_embedding", d.reidEmbedding?.let(::numberArray) ?: JsonNull)
                            if (faceMatchingEnabled) {
                                put("face_embedding", d.faceEmbedding?.let(::numberArray) ?: JsonNull)
                            }
                            put("crop", crops[i]?.let(::JsonPrimitive) ?: JsonNull)
                        },
                    )
                }
            },
        )
    }

    /**
     * Decodes a `detections` message the way `EdgeRunner.from_dict` reads it.
     * Returns null when it is not a `detections` message or lacks a required
     * field (`timestamp`, `frame_id`, or a detection's `bbox` or `confidence`).
     */
    fun decode(o: JsonObject): DetectionsMessage? {
        if (o.str("type") != TYPE) return null
        val timestamp = o.num("timestamp") ?: return null
        val frameId = o.long("frame_id") ?: return null
        val detections = mutableListOf<WireDetection>()
        for (element in o.arr("detections") ?: JsonArray(emptyList())) {
            val d = element.asObject() ?: return null
            val bbox = d.numbers("bbox")?.takeIf { it.size == 4 } ?: return null
            val confidence = d.num("confidence") ?: return null
            detections += WireDetection(
                bbox = bbox,
                confidence = confidence,
                reidEmbedding = d.numbers("reid_embedding"),
                faceEmbedding = d.numbers("face_embedding"),
                crop = d.str("crop")?.takeIf { it.isNotEmpty() },
            )
        }
        return DetectionsMessage(
            timestamp = timestamp,
            frameId = frameId,
            frameWidth = o.int("frame_width") ?: 0,
            frameHeight = o.int("frame_height") ?: 0,
            thumbnail = o.str("thumbnail")?.takeIf { it.isNotEmpty() },
            detections = detections,
        )
    }

    /** Decodes a `detections` message from JSON text, or returns null. */
    fun decode(text: String): DetectionsMessage? = parseJsonOrNull(text).asObject()?.let(::decode)

    private fun utf8Size(text: String): Int = text.toByteArray(Charsets.UTF_8).size

    private fun numberArray(values: List<Double>): JsonArray = buildJsonArray {
        values.forEach { add(wireNumber(it)) }
    }

    /** Whole values are written without a fraction (`512`, not `512.0`), as Python writes ints. */
    private fun wireNumber(value: Double): JsonElement {
        require(value.isFinite()) { "JSON cannot carry $value" }
        return if (value == Math.floor(value) && Math.abs(value) < 9.007199254740991E15) {
            JsonPrimitive(value.toLong())
        } else {
            JsonPrimitive(value)
        }
    }
}
