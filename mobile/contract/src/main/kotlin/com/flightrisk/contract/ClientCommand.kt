package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Everything a phone may emit to the dashboard, and nothing else
 * (docs/mobile-contract.md, "Events a phone may emit"; `client_emits.json`).
 *
 * The class is sealed, so no other module can add a command: the only way to
 * produce an emit is to construct one of these cases, and none of them names
 * a forbidden event (`plan_fleet`, `start_fleet`, `manual_command`,
 * `set_ceiling_settings` and the rest). A networking layer should accept a
 * [ClientCommand], never a free event name.
 */
sealed class ClientCommand(
    /** The Socket.IO event name. */
    val event: String,
) {
    /** The event's payload object, or null for an event sent without one. */
    abstract fun payload(): JsonObject?

    /** The payload as compact JSON text, or null for an event sent without one. */
    fun payloadJson(): String? = payload()?.toString()

    data object GetFleetStatus : ClientCommand("get_fleet_status") {
        override fun payload(): JsonObject? = null
    }

    data object GetDetectionProfiles : ClientCommand("get_detection_profiles") {
        override fun payload(): JsonObject? = null
    }

    data object GetCeilingSettings : ClientCommand("get_ceiling_settings") {
        override fun payload(): JsonObject? = null
    }

    data object GetReasoningProvider : ClientCommand("get_reasoning_provider") {
        override fun payload(): JsonObject? = null
    }

    data object GetManualStatus : ClientCommand("get_manual_status") {
        override fun payload(): JsonObject? = null
    }

    /** LAND ALL. Never gated by any drone or fleet state. */
    data object LandAll : ClientCommand("land_all") {
        override fun payload(): JsonObject? = null
    }

    /** Land one drone: `drone_command {command: "land", drone_id}`. Never gated by the drone's state. */
    data class LandDrone(val droneId: String) : ClientCommand("drone_command") {
        init {
            require(droneId.isNotBlank()) { "drone id must not be blank" }
        }

        override fun payload(): JsonObject = buildJsonObject {
            put("command", "land")
            put("drone_id", droneId)
        }
    }

    /**
     * Set one drone's detection profile. An empty [profile] makes the drone
     * follow the fleet default. Offer it only while `controls_enabled` is
     * true and the drone's `supported` is true.
     */
    data class SetDetectionProfile(val droneId: String, val profile: String) :
        ClientCommand("set_detection_profile") {
        init {
            require(droneId.isNotBlank()) { "drone id must not be blank" }
        }

        override fun payload(): JsonObject = buildJsonObject {
            put("drone_id", droneId)
            put("profile", profile)
        }
    }

    /** Set the fleet's default detection profile. An empty [profile] clears the default. */
    data class SetFleetDetectionProfile(val profile: String) : ClientCommand("set_fleet_detection_profile") {
        override fun payload(): JsonObject = buildJsonObject {
            put("profile", profile)
        }
    }

    /**
     * Resume the search of a held drone nobody controls. Offer it only when
     * the drone's `control_mode` is `"held"` and `fleet_status.manual_controlled`
     * is null (see [canResumeSearch]).
     */
    data class ManualResumeSearch(val droneId: String) : ClientCommand("manual_resume_search") {
        init {
            require(droneId.isNotBlank()) { "drone id must not be blank" }
        }

        override fun payload(): JsonObject = buildJsonObject {
            put("drone_id", droneId)
        }
    }

    companion object {
        /** The exact set of event names a phone may emit. */
        val ALLOWED_EVENTS: Set<String> = setOf(
            "get_fleet_status", "get_detection_profiles", "get_ceiling_settings", "get_reasoning_provider",
            "get_manual_status", "land_all", "drone_command", "set_detection_profile",
            "set_fleet_detection_profile", "manual_resume_search",
        )

        /**
         * The five state requests to send on every connect and every reconnect.
         * A getter, not a stored list: a companion field that referenced the
         * nested objects could be initialised while one of them is still null.
         */
        val STATE_REQUESTS: List<ClientCommand>
            get() = listOf(GetFleetStatus, GetDetectionProfiles, GetCeilingSettings, GetReasoningProvider, GetManualStatus)

        /** True when [event] is in the allow-list; a networking layer can assert this before emitting. */
        fun isAllowed(event: String): Boolean = event in ALLOWED_EVENTS

        /**
         * Whether Resume search may be offered for [drone]: its `control_mode`
         * is `"held"` and nobody controls a drone manually.
         */
        fun canResumeSearch(drone: ControlFields, manualControlled: String?): Boolean =
            drone.controlMode == ControlMode.Held && manualControlled == null
    }
}
