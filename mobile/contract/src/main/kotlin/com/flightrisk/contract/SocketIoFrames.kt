package com.flightrisk.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** Engine.IO protocol 4 packet types (`socketio_frames.json`, `engineio_type`). */
enum class EngineIoType(val code: Char) {
    OPEN('0'), CLOSE('1'), PING('2'), PONG('3'), MESSAGE('4'), UPGRADE('5'), NOOP('6');

    companion object {
        fun of(code: Char): EngineIoType? = entries.firstOrNull { it.code == code }
    }
}

/** Socket.IO protocol 5 packet types (`socketio_frames.json`, `socketio_type`). */
enum class SocketIoType(val code: Char) {
    CONNECT('0'), DISCONNECT('1'), EVENT('2'), ACK('3'), CONNECT_ERROR('4'), BINARY_EVENT('5'), BINARY_ACK('6');

    companion object {
        fun of(code: Char): SocketIoType? = entries.firstOrNull { it.code == code }
    }
}

/**
 * One decoded Engine.IO text frame. For a [EngineIoType.MESSAGE] frame,
 * [socketIo] holds the Socket.IO packet inside it.
 */
data class EngineIoFrame(
    val type: EngineIoType,
    /** The JSON after the type character (the open packet), or null. */
    val data: JsonElement?,
    val socketIo: SocketIoPacket?,
) {
    /** Never prints the payload, which may hold the dashboard key on a CONNECT. */
    override fun toString(): String = "EngineIoFrame(type=$type, socketIo=$socketIo)"
}

/** One Socket.IO packet in the default namespace or [namespace]. */
data class SocketIoPacket(
    val type: SocketIoType,
    val namespace: String,
    val ackId: Long?,
    val data: JsonElement?,
) {
    /** For an EVENT, the event name; otherwise null. */
    val eventName: String? get() = if (type == SocketIoType.EVENT) (data as? JsonArray)?.firstOrNull().stringValue() else null

    /** For an EVENT, its single payload (the dashboard always sends exactly one), or null. */
    val eventPayload: JsonElement? get() = if (type == SocketIoType.EVENT) (data as? JsonArray)?.getOrNull(1) else null

    /** Never prints the data, which holds the dashboard key on a client CONNECT. */
    override fun toString(): String =
        "SocketIoPacket(type=$type, namespace=$namespace, ackId=$ackId, event=$eventName)"
}

/**
 * Encodes and decodes the Engine.IO 4 / Socket.IO 5 text frames the dashboard
 * speaks (Flask-SocketIO 5.6.1 on python-socketio 5.16.3; `socketio_frames.json`).
 * Only text frames are handled: the dashboard sends no binary packets.
 */
object SocketIoFrames {
    /** Engine.IO pong, the answer to every ping `2`. */
    const val PONG = "3"

    /** Socket.IO disconnect of the default namespace. */
    const val DISCONNECT = "41"

    /** Engine.IO close. */
    const val CLOSE = "1"

    /** Decodes one text frame, or returns null when it is not a valid frame. Never throws. */
    fun parse(frame: String): EngineIoFrame? {
        if (frame.isEmpty()) return null
        val type = EngineIoType.of(frame[0]) ?: return null
        val rest = frame.substring(1)
        if (type != EngineIoType.MESSAGE) {
            val data = if (rest.isEmpty()) null else parseJsonOrNull(rest) ?: return null
            return EngineIoFrame(type, data, null)
        }
        val packet = parseSocketIo(rest) ?: return null
        return EngineIoFrame(type, null, packet)
    }

    private fun parseSocketIo(text: String): SocketIoPacket? {
        if (text.isEmpty()) return null
        val type = SocketIoType.of(text[0]) ?: return null
        var i = 1
        var namespace = "/"
        if (i < text.length && text[i] == '/') {
            val comma = text.indexOf(',', i)
            if (comma < 0) {
                namespace = text.substring(i)
                i = text.length
            } else {
                namespace = text.substring(i, comma)
                i = comma + 1
            }
        }
        val digitsStart = i
        while (i < text.length && text[i].isDigit()) i++
        val ackId = if (i > digitsStart) text.substring(digitsStart, i).toLongOrNull() ?: return null else null
        val data = if (i < text.length) parseJsonOrNull(text.substring(i)) ?: return null else null
        return SocketIoPacket(type, namespace, ackId, data)
    }

    /**
     * The namespace connect. With [apiKey] it is `40{"api_key":"<key>"}`;
     * without, `40`. The result carries the dashboard key: send it on the
     * socket and never log it.
     */
    fun connect(apiKey: String?): String =
        if (apiKey == null) {
            "40"
        } else {
            "40" + buildJsonObject { put("api_key", apiKey) }.toString()
        }

    /**
     * An EVENT frame for [command]: `42["name"]` or `42["name",payload]`, with
     * an ack id when [ackId] is given. Only a [ClientCommand] can be encoded,
     * so the emit allow-list holds on the wire too.
     */
    fun event(command: ClientCommand, ackId: Long? = null): String {
        val array = buildJsonArray {
            add(JsonPrimitive(command.event))
            command.payload()?.let { add(it) }
        }
        return "42" + (ackId?.toString() ?: "") + array.toString()
    }
}
