package com.flightrisk.contract

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * A decoded Socket.IO event from the dashboard. Every model of an event a
 * phone handles implements this interface; [Ignored] covers event names a
 * phone does not use, and [Malformed] a known event whose payload is not a
 * JSON object. Decoding never throws.
 */
sealed interface ServerEvent {
    /** An event name the phone does not use (for example `fleet_telemetry`); ignore it without error. */
    data class Ignored(val name: String) : ServerEvent

    /** A known event whose payload is not an object. Kept so it can be logged by name. */
    data class Malformed(val name: String, val problem: String) : ServerEvent
}

/** Decodes dashboard events by name (docs/mobile-contract.md, "Events a phone must handle"). */
object ServerEvents {
    const val STATUS = "status"
    const val FLEET_STATUS = "fleet_status"
    const val FLEET_EVENT = "fleet_event"
    const val FLEET_PLAN = "fleet_plan"
    const val FLEET_DETECTION = "fleet_detection"
    const val FRAME = "frame"
    const val MATCH_ALERT = "match_alert"
    const val MATCH_ALERT_REMINDER = "match_alert_reminder"
    const val DETECTION_PROFILES = "detection_profiles"
    const val CEILING_SETTINGS = "ceiling_settings"
    const val REASONING_PROVIDER = "reasoning_provider"
    const val MANUAL_STATUS = "manual_status"
    const val MANUAL_ACK = "manual_ack"
    const val MANUAL_RESULT = "manual_result"
    const val LAND_ALL_ACK = "land_all_ack"
    const val COMMAND_ACK = "command_ack"
    const val STREAM_VIDEO_STATE = "stream_video_state"
    const val ERROR = "error"
    const val WARNING = "warning"

    /** Every event name this module decodes. */
    val HANDLED: Set<String> = setOf(
        STATUS, FLEET_STATUS, FLEET_EVENT, FLEET_PLAN, FLEET_DETECTION, FRAME, MATCH_ALERT,
        MATCH_ALERT_REMINDER, DETECTION_PROFILES, CEILING_SETTINGS, REASONING_PROVIDER, MANUAL_STATUS,
        MANUAL_ACK, MANUAL_RESULT, LAND_ALL_ACK, COMMAND_ACK, STREAM_VIDEO_STATE, ERROR, WARNING,
    )

    /** Decodes the payload of the event [name]. */
    fun decode(name: String, payload: JsonElement?): ServerEvent {
        if (name !in HANDLED) return ServerEvent.Ignored(name)
        val o = payload as? JsonObject
            ?: return ServerEvent.Malformed(name, "payload is not a JSON object")
        return decodeObject(name, o)
    }

    /** Decodes an event payload given as JSON text. */
    fun decode(name: String, payloadJson: String): ServerEvent {
        if (name !in HANDLED) return ServerEvent.Ignored(name)
        val element = parseJsonOrNull(payloadJson)
            ?: return ServerEvent.Malformed(name, "payload is not valid JSON")
        return decode(name, element)
    }

    private fun decodeObject(name: String, o: JsonObject): ServerEvent = when (name) {
        STATUS -> Status.fromJson(o)
        FLEET_STATUS -> FleetStatus.fromJson(o)
        FLEET_EVENT -> FleetEvent.fromJson(o)
        FLEET_PLAN -> FleetPlan.fromJson(o)
        FLEET_DETECTION -> FleetDetection.fromJson(o)
        FRAME -> Frame.fromJson(o)
        MATCH_ALERT -> MatchAlert.fromJson(o, isReminder = false)
        MATCH_ALERT_REMINDER -> MatchAlert.fromJson(o, isReminder = true)
        DETECTION_PROFILES -> DetectionProfiles.fromJson(o)
        CEILING_SETTINGS -> CeilingSettings.fromJson(o)
        REASONING_PROVIDER -> ReasoningProvider.fromJson(o)
        MANUAL_STATUS -> ManualStatus.fromJson(o)
        MANUAL_ACK -> ManualReply.fromJson(o, ManualReply.Kind.ACK)
        MANUAL_RESULT -> ManualReply.fromJson(o, ManualReply.Kind.RESULT)
        LAND_ALL_ACK -> LandAllAck.fromJson(o)
        COMMAND_ACK -> CommandAck.fromJson(o)
        STREAM_VIDEO_STATE -> StreamVideoState.fromJson(o)
        ERROR -> ServerMessage.fromJson(o, ServerMessage.Level.ERROR)
        WARNING -> ServerMessage.fromJson(o, ServerMessage.Level.WARNING)
        else -> ServerEvent.Ignored(name)
    }
}
