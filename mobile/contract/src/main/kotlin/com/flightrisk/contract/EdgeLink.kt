package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * A message from the ground to a phone acting as an edge
 * (`edge_ground_to_edge.json`). A phone edge acts on [StreamVideo] only;
 * everything else is ignored without closing the connection.
 */
sealed interface GroundToEdgeMessage {
    /** The `type` exactly as it arrived, or an empty string when there was none. */
    val type: String

    /** Turn the thumbnail on or off. [enabled] is null when the ground sent no boolean. */
    data class StreamVideo(val enabled: Boolean?) : GroundToEdgeMessage {
        override val type: String get() = EdgeLink.STREAM_VIDEO
    }

    /** The ground's search target. A phone keeps its own target and ignores it. */
    data class SetTarget(val reidEmbedding: List<Double>?, val faceEmbedding: List<Double>?) : GroundToEdgeMessage {
        override val type: String get() = EdgeLink.SET_TARGET
    }

    /** Fleet-protocol traffic, a flight command, or an unknown type: ignored. */
    data class Ignored(override val type: String) : GroundToEdgeMessage

    /** Text that is not a JSON object: ignored too (the link stays open). */
    data class Malformed(val problem: String) : GroundToEdgeMessage {
        override val type: String get() = ""
    }
}

/** What a phone edge does with a message from the ground. */
sealed interface EdgeAction {
    /** Send `thumbnail` again ([enabled] true) or send `thumbnail: null` ([enabled] false). */
    data class SetThumbnail(val enabled: Boolean) : EdgeAction

    /** Do nothing and keep the connection open. */
    data object Ignore : EdgeAction
}

/**
 * Wire rules of the edge link for a phone edge (docs/mobile-contract.md,
 * "Phone as an edge"; `docs/fleet-protocol-v2.md`). A phone edge speaks
 * protocol v1: it never sends `hello`, `telemetry`, `ack`, `nack` or `pong`,
 * never nacks, and treats every type it does not use as a no-op.
 */
object EdgeLink {
    const val AUTH = "auth"
    const val STREAM_VIDEO = "stream_video"
    const val SET_TARGET = "set_target"

    /** The WebSocket close code the ground uses for a wrong or missing edge token: fix the token, do not retry. */
    const val CLOSE_UNAUTHORIZED = 4001

    /** Fleet-protocol housekeeping a phone edge ignores. */
    val IGNORED_PROTOCOL_TYPES: Set<String> = setOf("hello", "heartbeat", "clock", "ping")

    /** The only message types a phone edge ever sends. */
    val OUTBOUND_TYPES: Set<String> = setOf(AUTH, DetectionsWire.TYPE)

    /** Types a phone edge must never send (fleet-edge replies). */
    val NEVER_SENT_TYPES: Set<String> = setOf("telemetry", "ack", "nack", "hello", "pong")

    /** Parses one text frame from the ground. Never throws. */
    fun parse(text: String): GroundToEdgeMessage {
        val o = parseJsonOrNull(text).asObject()
            ?: return GroundToEdgeMessage.Malformed("not a JSON object")
        return parse(o)
    }

    /** Parses one message object from the ground. Never throws. */
    fun parse(o: JsonObject): GroundToEdgeMessage = when (val type = o.str("type") ?: "") {
        STREAM_VIDEO -> GroundToEdgeMessage.StreamVideo(o.bool("enabled"))
        SET_TARGET -> GroundToEdgeMessage.SetTarget(o.numbers("reid_embedding"), o.numbers("face_embedding"))
        else -> GroundToEdgeMessage.Ignored(type)
    }

    /**
     * The edge-link type filter: [EdgeAction.SetThumbnail] for a
     * `stream_video` with a boolean `enabled`, and [EdgeAction.Ignore] for
     * everything else (`hello`, `heartbeat`, `clock`, `ping`, `set_target`,
     * `land`, `emergency_land_all`, unknown types, malformed text). Nothing
     * here closes the connection.
     */
    fun actionFor(message: GroundToEdgeMessage): EdgeAction = when (message) {
        is GroundToEdgeMessage.StreamVideo -> message.enabled?.let { EdgeAction.SetThumbnail(it) } ?: EdgeAction.Ignore
        is GroundToEdgeMessage.SetTarget -> EdgeAction.Ignore
        is GroundToEdgeMessage.Ignored -> EdgeAction.Ignore
        is GroundToEdgeMessage.Malformed -> EdgeAction.Ignore
    }

    /** [parse] then [actionFor]. */
    fun actionFor(text: String): EdgeAction = actionFor(parse(text))

    /**
     * The first message on the edge link: `{"type": "auth", "token": ...}`.
     * The result carries the edge token: send it on the socket and never log it.
     */
    fun authMessage(token: String): String {
        require(token.isNotEmpty()) { "edge token must not be empty" }
        return buildJsonObject {
            put("type", AUTH)
            put("token", token)
        }.toString()
    }
}

/**
 * A fleet edge's reply to the ground (`telemetry`, `ack`, `nack`, `pong`).
 * A phone edge never sends these; the model exists so the reference samples in
 * `edge_edge_to_ground.json` can be read and their reason codes checked.
 */
data class EdgeReply(
    val type: String,
    val droneId: String?,
    /** The command answered (`for`). */
    val forCommand: String?,
    val cmdId: String?,
    val result: String?,
    val reason: ReasonCode?,
    val detail: String?,
) {
    companion object {
        fun fromJson(o: JsonObject): EdgeReply = EdgeReply(
            type = o.str("type") ?: "",
            droneId = o.str("drone_id"),
            forCommand = o.str("for"),
            cmdId = o.str("cmd_id"),
            result = o.str("result"),
            reason = o.str("reason")?.let(::ReasonCode),
            detail = o.str("detail"),
        )
    }
}
