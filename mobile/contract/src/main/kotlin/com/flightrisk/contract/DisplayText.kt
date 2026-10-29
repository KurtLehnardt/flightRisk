package com.flightrisk.contract

import kotlin.math.floor

/**
 * Plain text for status chips, banners and refusals, worded exactly as the
 * dashboard (`flightrisk/dashboard/static/ui/ui.js`) and the spec
 * (docs/mobile-contract.md) word them. Pure functions: no Android classes.
 */
object DisplayText {
    /** Shown for a value that was not reported. Never show zero for it. */
    const val DASH = "--"

    /** `land_all_ack` with `reached_any == false` (the ground also broadcasts it as `error`). */
    const val LAND_ALL_REACHED_NO_DRONE = "LAND ALL reached no drone. Land every drone with its local control now."

    /** The dashboard's banner after LAND ALL is emitted, and for a `land_all` fleet event. */
    const val LAND_ALL_SENT = "LAND ALL sent"

    /** Shown while LAND ALL waits for its ack for more than [LAND_ALL_NO_REPLY_AFTER_MS]. */
    const val LAND_ALL_NO_REPLY_YET = "LAND ALL: no reply yet"

    /** After this long without `land_all_ack`, show [LAND_ALL_NO_REPLY_YET] (docs/mobile-contract.md, rule 7). */
    const val LAND_ALL_NO_REPLY_AFTER_MS = 5_000L

    /**
     * A LAND ALL tap while the socket is not connected: the command was not
     * sent and is never queued (docs/mobile-contract.md, safety rule 2).
     */
    const val LAND_ALL_NOT_SENT =
        "LAND ALL was not sent: not connected to the ground station. " +
            "Use the laptop's LAND ALL (Shift+L) or the drones' own controls."

    /** The fallback when a drone link is down and the ground gave no detail. */
    const val DRONE_LINK_DEFAULT_DETAIL = "the edge cannot reach the drone"

    /**
     * "Drone link down: <detail>. Land still works." when `drone_link_ok` is
     * false, and null when it is true or absent (`ui.js`, `manualDisabledReason`).
     */
    fun droneLinkDown(droneLinkOk: Boolean?, droneLinkDetail: String?): String? {
        if (droneLinkOk != false) return null
        val detail = droneLinkDetail?.takeIf { it.isNotEmpty() } ?: DRONE_LINK_DEFAULT_DETAIL
        return "Drone link down: $detail. Land still works."
    }

    fun droneLinkDown(drone: FleetDrone): String? = droneLinkDown(drone.droneLinkOk, drone.droneLinkDetail)

    /** "auto-landing in N s", the countdown part of the HELD badge. */
    fun autoLandingIn(seconds: Double): String = "auto-landing in ${jsNumber(seconds)} s"

    /**
     * The local countdown between updates: [orphanLandInS] received at
     * [receivedAtMs], seen at [nowMs], in whole seconds and never below zero.
     * Replace it with each new value from `fleet_status` or `manual_status`.
     */
    fun orphanCountdown(orphanLandInS: Double, receivedAtMs: Long, nowMs: Long): Double {
        val elapsed = floor((nowMs - receivedAtMs).coerceAtLeast(0L) / 1000.0)
        return (orphanLandInS - elapsed).coerceAtLeast(0.0)
    }

    /** The banner for a fleet event, exactly as the dashboard's `showFleetBanner` text, or null for none. */
    fun fleetBanner(event: FleetEvent): String? {
        val detail = event.detail?.takeIf { it.isNotEmpty() }
        return when (event) {
            is FleetEvent.RetaskFailed ->
                "RETASK FAILED: " + (detail ?: event.droneId?.takeIf { it.isNotEmpty() } ?: "")
            is FleetEvent.Retask -> "RETASK:" + who(event.droneId) + (detail?.let { " $it" } ?: "")
            is FleetEvent.DroneLost -> "DRONE LOST:" + who(event.droneId)
            is FleetEvent.LandAll -> LAND_ALL_SENT
            is FleetEvent.ProfileSwitchPending -> "WARNING: " + (detail ?: "detection profile switch pending")
            is FleetEvent.PlanInvalidated -> "PLAN EDITED: press Plan search again"
            is FleetEvent.Start -> event.kind
            is FleetEvent.PlanRefused -> "PLAN REFUSED"
            is FleetEvent.StartRefused -> event.kind
            is FleetEvent.Unknown -> event.kind.takeIf { it.isNotEmpty() }
        }
    }

    /** True for the fleet events styled as refusals (`plan_refused`, `start_refused`). */
    fun isRefusal(event: FleetEvent): Boolean = event is FleetEvent.PlanRefused || event is FleetEvent.StartRefused

    /**
     * The refusal message the dashboard shows under the fleet controls:
     * "Plan refused: <detail>" or "Start refused: <detail>" ("unknown" when
     * no detail came), or null for other events.
     */
    fun refusal(event: FleetEvent): String? {
        val detail = event.detail?.takeIf { it.isNotEmpty() } ?: "unknown"
        return when (event) {
            is FleetEvent.PlanRefused -> "Plan refused: $detail"
            is FleetEvent.StartRefused -> "Start refused: $detail"
            else -> null
        }
    }

    /**
     * The result of LAND ALL: [LAND_ALL_REACHED_NO_DRONE] when no path
     * reached a drone, otherwise the counts per path that exists, for example
     * "LAND ALL reached: coordinator 2, transport 2".
     */
    fun landAllResult(ack: LandAllAck): String {
        if (ack.failed) return LAND_ALL_REACHED_NO_DRONE
        val parts = listOfNotNull(
            ack.coordinator?.let { "coordinator $it" },
            ack.transport?.let { "transport $it" },
            ack.local?.let { "local $it" },
        )
        return "LAND ALL reached: " + parts.joinToString(", ")
    }

    /**
     * One line of the dashboard's "NOT SEARCHED" list for a lane of
     * [lostId]: uncovered lanes give their reason, awaiting lanes the drone
     * they wait for (`ui.js`, `renderUncovered`).
     */
    fun uncoveredLane(lostId: String, lane: RetaskLane, uncovered: Boolean): String {
        val where = "(" + fmt(lane.xCm) + ", " + fmt(lane.yCm) + ") to (" +
            fmt(lane.endXCm) + ", " + fmt(lane.endYCm) + ") cm"
        val why = if (uncovered) {
            lane.reason?.takeIf { it.isNotEmpty() } ?: "no drone can take it"
        } else {
            "awaiting " + (lane.droneId?.takeIf { it.isNotEmpty() } ?: "a free drone")
        }
        return lostId + " lane " + (lane.index?.toString() ?: "undefined") + " " +
            (lane.direction?.raw ?: "undefined") + " " + fmt(lane.distanceCm) + " cm " + where + ": " + why
    }

    /** The header of the "NOT SEARCHED" list (`ui.js`, `renderUncovered`). */
    fun notSearchedHeader(uncoveredCount: Int, awaitingCount: Int): String =
        "NOT SEARCHED: " + uncoveredCount + " lane" + (if (uncoveredCount == 1) "" else "s") +
            " uncovered, " + awaitingCount + " awaiting a drone"

    /** The dashboard's `fmt`: `--` for null, otherwise rounded to one decimal place. */
    fun fmt(value: Double?): String {
        if (value == null) return DASH
        return jsNumber((value * 10).roundHalfUp() / 10.0)
    }

    /** JavaScript's `Math.round`: halves round towards positive infinity. */
    private fun Double.roundHalfUp(): Double = floor(this + 0.5)

    private fun who(droneId: String?): String = droneId?.takeIf { it.isNotEmpty() }?.let { " $it" } ?: ""
}
