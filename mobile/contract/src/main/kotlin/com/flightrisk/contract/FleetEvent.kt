package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject

/**
 * The `fleet_event` event (`fleet_event.json`): one case per kind the ground
 * sends today, and [Unknown] for any other kind, which is shown by name.
 *
 * The kind is read from `event`, falling back to `type` and then `kind`, as the
 * dashboard does (`ui.js`, `socket.on('fleet_event')`).
 */
sealed interface FleetEvent : ServerEvent {
    /** The kind exactly as it arrived on the wire. */
    val kind: String

    /** The human-readable explanation, when the ground sent one. */
    val detail: String?

    data class DroneLost(val droneId: String?, override val detail: String?) : FleetEvent {
        override val kind: String get() = DRONE_LOST
    }

    /** LAND ALL was sent. `detail` is "sent to all" or "failed: <ids>". */
    data class LandAll(override val detail: String?) : FleetEvent {
        override val kind: String get() = LAND_ALL

        /** True when the ground could not reach a link: `detail` starts with "failed". */
        val failed: Boolean get() = detail?.startsWith("failed") == true
    }

    data class PlanInvalidated(override val detail: String?) : FleetEvent {
        override val kind: String get() = PLAN_INVALIDATED
    }

    data class PlanRefused(override val detail: String?) : FleetEvent {
        override val kind: String get() = PLAN_REFUSED
    }

    data class ProfileSwitchPending(
        val droneIds: List<String>,
        override val detail: String?,
    ) : FleetEvent {
        override val kind: String get() = PROFILE_SWITCH_PENDING
    }

    /**
     * Lanes of a lost drone were re-tasked. `to` counts the lanes appended to
     * each accepting drone, `lanes` lists the accepted lanes and `awaiting` the
     * lanes still waiting for an answer.
     */
    data class Retask(
        val droneId: String?,
        val to: Map<String, Int>,
        val lanes: List<RetaskLane>,
        val awaiting: List<RetaskLane>?,
        override val detail: String?,
    ) : FleetEvent {
        override val kind: String get() = RETASK
    }

    /** Lanes of a lost drone that no drone took; each lane carries its `reason`. */
    data class RetaskFailed(
        val droneId: String?,
        val lanes: List<RetaskLane>,
        override val detail: String?,
    ) : FleetEvent {
        override val kind: String get() = RETASK_FAILED
    }

    data class Start(override val detail: String?) : FleetEvent {
        override val kind: String get() = START
    }

    data class StartRefused(override val detail: String?) : FleetEvent {
        override val kind: String get() = START_REFUSED
    }

    /** A kind this app does not know; shown by name, never dropped. */
    data class Unknown(
        override val kind: String,
        val droneId: String?,
        override val detail: String?,
    ) : FleetEvent

    companion object {
        const val DRONE_LOST = "drone_lost"
        const val LAND_ALL = "land_all"
        const val PLAN_INVALIDATED = "plan_invalidated"
        const val PLAN_REFUSED = "plan_refused"
        const val PROFILE_SWITCH_PENDING = "profile_switch_pending"
        const val RETASK = "retask"
        const val RETASK_FAILED = "retask_failed"
        const val START = "start"
        const val START_REFUSED = "start_refused"

        /** The nine kinds the ground sends today. */
        val KNOWN_KINDS: Set<String> = setOf(
            DRONE_LOST, LAND_ALL, PLAN_INVALIDATED, PLAN_REFUSED, PROFILE_SWITCH_PENDING,
            RETASK, RETASK_FAILED, START, START_REFUSED,
        )

        fun fromJson(o: JsonObject): FleetEvent {
            val kind = o.str("event") ?: o.str("type") ?: o.str("kind") ?: ""
            val detail = o.str("detail")
            return when (kind) {
                DRONE_LOST -> DroneLost(o.str("drone_id"), detail)
                LAND_ALL -> LandAll(detail)
                PLAN_INVALIDATED -> PlanInvalidated(detail)
                PLAN_REFUSED -> PlanRefused(detail)
                PROFILE_SWITCH_PENDING -> ProfileSwitchPending(o.strings("drone_ids") ?: emptyList(), detail)
                RETASK -> Retask(
                    droneId = o.str("drone_id"),
                    to = o.obj("to")?.let { to ->
                        to.entries.mapNotNull { (id, n) -> n.intValue()?.let { id to it } }.toMap(sortedMapOf())
                    } ?: emptyMap(),
                    lanes = o.objects("lanes", RetaskLane::fromJson) ?: emptyList(),
                    awaiting = o.objects("awaiting", RetaskLane::fromJson),
                    detail = detail,
                )
                RETASK_FAILED -> RetaskFailed(
                    droneId = o.str("drone_id"),
                    lanes = o.objects("lanes", RetaskLane::fromJson) ?: emptyList(),
                    detail = detail,
                )
                START -> Start(detail)
                START_REFUSED -> StartRefused(detail)
                else -> Unknown(kind, o.str("drone_id"), detail)
            }
        }
    }
}

/**
 * One lane in a retask event or in a plan's `uncovered` list: the lane's index
 * in its drone's plan, its direction and length, and its start and end in the
 * plan frame.
 */
data class RetaskLane(
    val index: Int?,
    val direction: LaneDirection?,
    val distanceCm: Double?,
    val xCm: Double?,
    val yCm: Double?,
    val endXCm: Double?,
    val endYCm: Double?,
    /** The drone that took or awaits the lane (retask), or the lane's owner (plan). */
    val droneId: String?,
    /** Why nobody took the lane (`retask_failed` and plan `uncovered` only). */
    val reason: String?,
) {
    companion object {
        fun fromJson(o: JsonObject): RetaskLane = RetaskLane(
            index = o.int("index"),
            direction = o.str("direction")?.let(LaneDirection::of),
            distanceCm = o.num("distance_cm"),
            xCm = o.num("x_cm"),
            yCm = o.num("y_cm"),
            endXCm = o.num("end_x_cm"),
            endYCm = o.num("end_y_cm"),
            droneId = o.str("drone_id"),
            reason = o.str("reason"),
        )
    }
}
