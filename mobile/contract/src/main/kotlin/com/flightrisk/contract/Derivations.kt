package com.flightrisk.contract

/**
 * A drone's connection state, copied from the dashboard's `connState`
 * (`flightrisk/dashboard/static/ui/ui.js`; docs/mobile-contract.md, "Derived
 * state"). First match wins:
 *
 * 1. `lost == true` -> [LOST]
 * 2. `connected == false` -> [DOWN]
 * 3. `connected == true` -> [CONNECTED]
 * 4. otherwise -> [UNKNOWN], shown as `--` (every drone of a ground without a fleet)
 */
enum class ConnectionState(
    /** The text on the drone card, as on the dashboard. */
    val label: String,
) {
    CONNECTED("CONNECTED"),
    DOWN("DOWN"),
    LOST("LOST"),
    UNKNOWN("--");

    /** DOWN and LOST cards show their values as the last ones received. */
    val isStale: Boolean get() = this == DOWN || this == LOST

    companion object {
        /** The note the dashboard puts on a DOWN or LOST card. */
        const val STALE_NOTE = "No live link: values are the last ones received"

        fun of(lost: Boolean?, connected: Boolean?): ConnectionState = when {
            lost == true -> LOST
            connected == false -> DOWN
            connected == true -> CONNECTED
            else -> UNKNOWN
        }

        fun of(drone: FleetDrone): ConnectionState = of(drone.lost, drone.connected)

        /** A drone of a ground without a fleet reports neither `lost` nor `connected`. */
        @Suppress("UNUSED_PARAMETER")
        fun of(drone: LegacyDrone): ConnectionState = UNKNOWN
    }
}

/**
 * A drone's control mode as the dashboard shows it, copied from `modeText`
 * (`ui.js`; docs/mobile-contract.md, "Control mode"). First match wins:
 *
 * 1. `orphan_land_in_s` is a number -> [Held] with the countdown:
 *    "HELD: auto-landing in N s, no controller"
 * 2. the drone is `manual_controlled` -> [Manual] (the laptop controls it; a
 *    phone never does, so it shows "MANUAL")
 * 3. `control_mode == "held"` or `edge_control_mode == "held"` -> [Held]:
 *    "HELD: hovering, search paused"
 * 4. `control_mode == "manual"` -> [Manual]: "MANUAL"
 * 5. otherwise [Autonomous]: "Flying" when `is_flying == true`, "On the
 *    ground" when it is false, and no text when it is unknown.
 */
sealed interface ControlModeDisplay {
    /** The badge text; empty when there is nothing to show. */
    val text: String

    /** Held. [orphanLandInS] is the ground's countdown to an automatic landing, or null when there is none. */
    data class Held(val orphanLandInS: Double?) : ControlModeDisplay {
        override val text: String
            get() = if (orphanLandInS != null) {
                "HELD: auto-landing in ${jsNumber(orphanLandInS)} s, no controller"
            } else {
                "HELD: hovering, search paused"
            }
    }

    /** Under manual control of the laptop's operator. */
    data object Manual : ControlModeDisplay {
        override val text: String get() = "MANUAL"
    }

    /** Flying its plan or idle. [isFlying] is null when the ground did not report it. */
    data class Autonomous(val isFlying: Boolean?) : ControlModeDisplay {
        override val text: String
            get() = when (isFlying) {
                true -> "Flying"
                false -> "On the ground"
                null -> ""
            }
    }

    companion object {
        /**
         * The display for [drone] (with id [droneId]) when [manualControlled]
         * is the drone the laptop controls (`fleet_status.manual_controlled` or
         * `manual_status.controlled`), or null.
         */
        fun of(droneId: String?, drone: ControlFields, manualControlled: String?): ControlModeDisplay {
            val landIn = drone.orphanLandInS
            if (landIn != null) return Held(landIn)
            if (!droneId.isNullOrEmpty() && droneId == manualControlled) return Manual
            if (drone.controlMode == ControlMode.Held || drone.edgeControlMode == ControlMode.Held) return Held(null)
            if (drone.controlMode == ControlMode.Manual) return Manual
            return Autonomous(drone.isFlying)
        }

        /** The display for a fleet drone, keyed by its id in `fleet_status.drones`. */
        fun of(drone: FleetDrone, manualControlled: String?): ControlModeDisplay =
            of(drone.id, drone, manualControlled)

        /**
         * The display for a fleet drone with its `manual_status` entry merged
         * over it, as the dashboard merges the two events.
         */
        fun of(drone: FleetDrone, manual: ManualDroneStatus?, manualControlled: String?): ControlModeDisplay =
            of(drone.id, if (manual != null) drone.mergedWith(manual) else drone, manualControlled)
    }
}
