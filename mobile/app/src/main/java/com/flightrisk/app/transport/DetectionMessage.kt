package com.flightrisk.app.transport

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream

data class DetectionData(
    val bbox: IntArray,
    val confidence: Float,
    val reidEmbedding: FloatArray? = null,
    val faceEmbedding: FloatArray? = null,
    val cropJpeg: ByteArray? = null,
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is DetectionData) return false
        return bbox.contentEquals(other.bbox) && confidence == other.confidence
    }

    override fun hashCode(): Int = bbox.contentHashCode() * 31 + confidence.hashCode()
}

data class DetectionMessage(
    val timestamp: Double,
    val frameId: Int,
    val frameWidth: Int = 0,
    val frameHeight: Int = 0,
    val thumbnailJpeg: ByteArray? = null,
    val detections: List<DetectionData> = emptyList(),
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("type", "detections")
        put("timestamp", timestamp)
        put("frame_id", frameId)
        put("frame_width", frameWidth)
        put("frame_height", frameHeight)
        put("thumbnail", thumbnailJpeg?.let { Base64.encodeToString(it, Base64.NO_WRAP) })
        put("detections", JSONArray().apply {
            for (det in detections) {
                put(JSONObject().apply {
                    put("bbox", JSONArray().apply { for (v in det.bbox) put(v) })
                    put("confidence", det.confidence.toDouble())
                    det.reidEmbedding?.let { emb ->
                        put("reid_embedding", JSONArray().apply { for (v in emb) put(v.toDouble()) })
                    }
                    det.faceEmbedding?.let { emb ->
                        put("face_embedding", JSONArray().apply { for (v in emb) put(v.toDouble()) })
                    }
                    det.cropJpeg?.let { crop ->
                        put("crop", Base64.encodeToString(crop, Base64.NO_WRAP))
                    }
                })
            }
        })
    }

    companion object {
        fun fromJson(json: JSONObject): DetectionMessage {
            val detArray = json.optJSONArray("detections") ?: JSONArray()
            val detections = (0 until detArray.length()).map { i ->
                val d = detArray.getJSONObject(i)
                val bboxArr = d.getJSONArray("bbox")
                val bbox = IntArray(bboxArr.length()) { bboxArr.getInt(it) }

                val reidArr = d.optJSONArray("reid_embedding")
                val reidEmb = reidArr?.let { arr ->
                    FloatArray(arr.length()) { arr.getDouble(it).toFloat() }
                }

                val faceArr = d.optJSONArray("face_embedding")
                val faceEmb = faceArr?.let { arr ->
                    FloatArray(arr.length()) { arr.getDouble(it).toFloat() }
                }

                val cropB64 = d.optString("crop", "")
                val cropJpeg = cropB64.takeIf { it.isNotEmpty() }?.let {
                    Base64.decode(it, Base64.NO_WRAP)
                }

                DetectionData(
                    bbox = bbox,
                    confidence = d.getDouble("confidence").toFloat(),
                    reidEmbedding = reidEmb,
                    faceEmbedding = faceEmb,
                    cropJpeg = cropJpeg,
                )
            }

            val thumbB64 = json.optString("thumbnail", "")
            val thumbJpeg = thumbB64.takeIf { it.isNotEmpty() }?.let {
                Base64.decode(it, Base64.NO_WRAP)
            }

            return DetectionMessage(
                timestamp = json.getDouble("timestamp"),
                frameId = json.getInt("frame_id"),
                frameWidth = json.optInt("frame_width", 0),
                frameHeight = json.optInt("frame_height", 0),
                thumbnailJpeg = thumbJpeg,
                detections = detections,
            )
        }

        fun thumbnailFromBitmap(bitmap: Bitmap, quality: Int = 60): ByteArray {
            val scaled = Bitmap.createScaledBitmap(bitmap, 320, 180, true)
            val out = ByteArrayOutputStream()
            scaled.compress(Bitmap.CompressFormat.JPEG, quality, out)
            if (scaled !== bitmap) scaled.recycle()
            return out.toByteArray()
        }

        fun cropToJpeg(bitmap: Bitmap, quality: Int = 80): ByteArray {
            val out = ByteArrayOutputStream()
            bitmap.compress(Bitmap.CompressFormat.JPEG, quality, out)
            return out.toByteArray()
        }

        fun jpegToBitmap(jpeg: ByteArray): Bitmap? =
            BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
    }
}
