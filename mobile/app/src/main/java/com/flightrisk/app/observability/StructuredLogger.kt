package com.flightrisk.app.observability

import android.util.Log
import org.json.JSONObject

class StructuredLogger(private val component: String = "flightrisk") {

    private fun emit(level: String, event: String, extras: Map<String, Any?> = emptyMap()) {
        val json = JSONObject().apply {
            put("ts", System.currentTimeMillis())
            put("level", level)
            put("component", component)
            put("event", event)
            for ((k, v) in extras) {
                put(k, v ?: JSONObject.NULL)
            }
        }
        val msg = json.toString()
        when (level) {
            "error" -> Log.e("FR.$component", msg)
            "warning" -> Log.w("FR.$component", msg)
            "debug" -> Log.d("FR.$component", msg)
            else -> Log.i("FR.$component", msg)
        }
    }

    fun info(event: String, vararg extras: Pair<String, Any?>) =
        emit("info", event, extras.toMap())

    fun warning(event: String, vararg extras: Pair<String, Any?>) =
        emit("warning", event, extras.toMap())

    fun error(event: String, vararg extras: Pair<String, Any?>) =
        emit("error", event, extras.toMap())

    fun debug(event: String, vararg extras: Pair<String, Any?>) =
        emit("debug", event, extras.toMap())

    fun detection(count: Int, frameId: Int? = null, vararg extras: Pair<String, Any?>) =
        emit("info", "detection", mapOf("count" to count, "frameId" to frameId) + extras.toMap())

    fun match(score: Float, matchType: String, vararg extras: Pair<String, Any?>) =
        emit("info", "match", mapOf("score" to score, "matchType" to matchType) + extras.toMap())

    fun faceResult(success: Boolean, score: Float = 0f, vararg extras: Pair<String, Any?>) =
        emit("info", "face_result", mapOf("success" to success, "score" to score) + extras.toMap())

    fun reasoning(durationMs: Float, vararg extras: Pair<String, Any?>) =
        emit("info", "reasoning", mapOf("durationMs" to durationMs) + extras.toMap())

    fun scoring(combined: Float, reid: Float = 0f, face: Float = 0f, vararg extras: Pair<String, Any?>) =
        emit("info", "scoring", mapOf("combined" to combined, "reid" to reid, "face" to face) + extras.toMap())

    fun droneCommand(command: String, vararg extras: Pair<String, Any?>) =
        emit("info", "drone_command", mapOf("command" to command) + extras.toMap())

    fun battery(level: Int, isFlying: Boolean = false, warnThreshold: Int = 20, criticalThreshold: Int = 10) {
        val lvl = when {
            level <= criticalThreshold -> "error"
            level <= warnThreshold -> "warning"
            else -> "info"
        }
        emit(lvl, "battery", mapOf("batteryLevel" to level, "isFlying" to isFlying))
    }
}
