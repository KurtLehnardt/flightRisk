package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject

/** The `manual_status` event (`manual_status.json`). */
data class ManualStatus(
    /** The drone the laptop controls manually, or null. */
    val controlled: String?,
    /** Keyed by drone id, sorted by id. */
    val drones: Map<String, ManualDroneStatus>,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): ManualStatus = ManualStatus(
            controlled = o.str("controlled"),
            drones = o.keyedObjects("drones", ManualDroneStatus::fromJson) ?: emptyMap(),
        )
    }
}

/** One drone of `manual_status`. It does not carry `is_flying`. */
data class ManualDroneStatus(
    val id: String,
    override val controlMode: ControlMode?,
    override val edgeControlMode: ControlMode?,
    val manualSupported: Boolean?,
    val manualLast: ManualLast?,
    override val orphanLandInS: Double?,
) : ControlFields {
    override val isFlying: Boolean? get() = null

    companion object {
        fun fromJson(id: String, o: JsonObject): ManualDroneStatus = ManualDroneStatus(
            id = id,
            controlMode = o.str("control_mode")?.let(ControlMode::of),
            edgeControlMode = o.str("edge_control_mode")?.let(ControlMode::of),
            manualSupported = o.bool("manual_supported"),
            manualLast = o.obj("manual_last")?.let(ManualLast::fromJson),
            orphanLandInS = o.num("orphan_land_in_s"),
        )
    }
}

/**
 * The `manual_ack` and `manual_result` events (`manual_ack.json`,
 * `manual_result.json`): the ground's answer to a per-drone land or a resume,
 * and the edge's later answer (or `reason: "no_reply"` after 4 seconds).
 */
data class ManualReply(
    val kind: Kind,
    val droneId: String?,
    val action: ManualAction?,
    val ok: Boolean?,
    val reason: ReasonCode?,
    val detail: String?,
    /** Sent on a successful `resume_search` result. */
    val controlMode: ControlMode?,
) : ServerEvent {
    enum class Kind { ACK, RESULT }

    companion object {
        fun fromJson(o: JsonObject, kind: Kind): ManualReply = ManualReply(
            kind = kind,
            droneId = o.str("drone_id"),
            action = o.str("action")?.let(ManualAction::of),
            ok = o.bool("ok"),
            reason = o.str("reason")?.let(::ReasonCode),
            detail = o.str("detail"),
            controlMode = o.str("control_mode")?.let(ControlMode::of),
        )
    }
}

/**
 * The `land_all_ack` event (`land_all_ack.json`): how many drones each LAND
 * ALL path reached (null when that path does not exist on this ground) and
 * whether any path reached a drone. The payload is flat.
 */
data class LandAllAck(
    val coordinator: Int?,
    val transport: Int?,
    val local: Int?,
    val reachedAny: Boolean?,
) : ServerEvent {
    /** `reached_any == false` is a failure; an absent flag is not treated as success either. */
    val failed: Boolean get() = reachedAny != true

    companion object {
        fun fromJson(o: JsonObject): LandAllAck = LandAllAck(
            coordinator = o.int("coordinator"),
            transport = o.int("transport"),
            local = o.int("local"),
            reachedAny = o.bool("reached_any"),
        )
    }
}

/** The `command_ack` event: the answer to `drone_command` on a ground without a fleet. */
data class CommandAck(val command: String?) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): CommandAck = CommandAck(o.str("command"))
    }
}

/** The `stream_video_state` event: whether the ground streams video. */
data class StreamVideoState(val enabled: Boolean?) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): StreamVideoState = StreamVideoState(o.bool("enabled"))
    }
}

/** The `error` and `warning` events: a message to show as a toast, never a disconnect. */
data class ServerMessage(val level: Level, val message: String?) : ServerEvent {
    enum class Level { ERROR, WARNING }

    companion object {
        fun fromJson(o: JsonObject, level: Level): ServerMessage = ServerMessage(level, o.str("message"))
    }
}
