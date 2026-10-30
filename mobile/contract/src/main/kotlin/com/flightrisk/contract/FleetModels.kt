package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject

/**
 * The `status` event, sent once per connect (`status.json`).
 *
 * `fleet.drones` is a count here, unlike `fleet_status.drones`, which is a map.
 */
data class Status(
    val connected: Boolean?,
    val source: String?,
    val appearanceOnly: Boolean?,
    val tiled: Boolean?,
    val fleet: FleetSummary?,
) : ServerEvent {
    /** True when the ground runs a fleet (`fleet.drones > 0`). */
    val isFleetMode: Boolean get() = (fleet?.drones ?: 0) > 0

    companion object {
        fun fromJson(o: JsonObject): Status = Status(
            connected = o.bool("connected"),
            source = o.str("source"),
            appearanceOnly = o.bool("appearance_only"),
            tiled = o.bool("tiled"),
            fleet = o.obj("fleet")?.let(FleetSummary::fromJson),
        )
    }
}

/** `status.fleet`: the number of configured drones, the network mode and the clock-sync flag. */
data class FleetSummary(
    val drones: Int?,
    val net: String?,
    val synced: Boolean?,
) {
    companion object {
        fun fromJson(o: JsonObject): FleetSummary = FleetSummary(
            drones = o.int("drones"),
            net = o.str("net"),
            synced = o.bool("synced"),
        )
    }
}

/**
 * The `fleet_status` event (`fleet_status.json`). It has two shapes:
 * [Fleet] when the ground runs a fleet, and [Legacy] (`{drones, count}`) for a
 * ground without one. The fleet shape always carries `manual_controlled`
 * (`flightrisk/dashboard/app.py`, `status_payload`), and the dashboard uses
 * that key to recognise it, so this decoder does the same.
 */
sealed interface FleetStatus : ServerEvent {
    /** Drone ids in display order (sorted). */
    val droneIds: List<String>

    data class Fleet(
        /** Keyed by drone id, sorted by id. */
        val drones: Map<String, FleetDrone>,
        val net: String?,
        val synced: Boolean?,
        /** The drone the laptop controls manually, or null. */
        val manualControlled: String?,
    ) : FleetStatus {
        override val droneIds: List<String> get() = drones.keys.toList()
    }

    data class Legacy(
        /** Keyed by drone id, sorted by id. */
        val drones: Map<String, LegacyDrone>,
        val count: Int?,
    ) : FleetStatus {
        override val droneIds: List<String> get() = drones.keys.toList()
    }

    companion object {
        fun fromJson(o: JsonObject): FleetStatus =
            if (o.containsKey("manual_controlled") || !o.containsKey("count")) {
                Fleet(
                    drones = o.keyedObjects("drones", FleetDrone::fromJson) ?: emptyMap(),
                    net = o.str("net"),
                    synced = o.bool("synced"),
                    manualControlled = o.str("manual_controlled"),
                )
            } else {
                Legacy(
                    drones = o.keyedObjects("drones", LegacyDrone::fromJson) ?: emptyMap(),
                    count = o.int("count"),
                )
            }
    }
}

/**
 * The fields the control-mode display reads, shared by a fleet drone and a
 * `manual_status` entry.
 */
interface ControlFields {
    val controlMode: ControlMode?
    val edgeControlMode: ControlMode?
    val orphanLandInS: Double?
    val isFlying: Boolean?
}

/**
 * One drone of a fleet `fleet_status` (`flightrisk/drone/coordinator.py` status
 * plus the link and manual-control fields the dashboard adds). Every field is
 * optional: null means "not reported" and is shown as a dash, never as zero.
 */
data class FleetDrone(
    val id: String,
    val droneId: String?,
    val connected: Boolean?,
    val lost: Boolean?,
    val linkAgeS: Double?,
    val battery: Double?,
    val heightCm: Double?,
    val altCm: Double?,
    val headingDeg: Double?,
    override val isFlying: Boolean?,
    val laneIndex: Int?,
    val lanesTotal: Int?,
    val acked: Boolean?,
    val relXCm: Double?,
    val relYCm: Double?,
    val lat: Double?,
    val lon: Double?,
    val droneLinkOk: Boolean?,
    val droneLinkDetail: String?,
    val synced: Boolean?,
    val offsetMs: Double?,
    val rttMs: Double?,
    val skewMs: Double?,
    override val controlMode: ControlMode?,
    override val edgeControlMode: ControlMode?,
    val manualSupported: Boolean?,
    val manualLast: ManualLast?,
    override val orphanLandInS: Double?,
) : ControlFields {
    /**
     * This entry with the keys a `manual_status` entry carries laid over it,
     * as the dashboard's `Object.assign` does. Only [DroneStateReducer] calls
     * it, so that events are applied in arrival order (newest wins).
     */
    internal fun overlaidWith(manual: ManualDroneStatus): FleetDrone {
        val keys = manual.carriedKeys
        return copy(
            controlMode = if (ManualDroneStatus.KEY_CONTROL_MODE in keys) manual.controlMode else controlMode,
            edgeControlMode = if (ManualDroneStatus.KEY_EDGE_CONTROL_MODE in keys) manual.edgeControlMode else edgeControlMode,
            manualSupported = if (ManualDroneStatus.KEY_MANUAL_SUPPORTED in keys) manual.manualSupported else manualSupported,
            manualLast = if (ManualDroneStatus.KEY_MANUAL_LAST in keys) manual.manualLast else manualLast,
            orphanLandInS = if (ManualDroneStatus.KEY_ORPHAN_LAND_IN_S in keys) manual.orphanLandInS else orphanLandInS,
        )
    }

    companion object {
        fun fromJson(id: String, o: JsonObject): FleetDrone = FleetDrone(
            id = id,
            droneId = o.str("drone_id"),
            connected = o.bool("connected"),
            lost = o.bool("lost"),
            linkAgeS = o.num("link_age_s"),
            battery = o.num("battery"),
            heightCm = o.num("height_cm"),
            altCm = o.num("alt_cm"),
            headingDeg = o.num("heading_deg"),
            isFlying = o.bool("is_flying"),
            laneIndex = o.int("lane_index"),
            lanesTotal = o.int("lanes_total"),
            acked = o.bool("acked"),
            relXCm = o.num("rel_x_cm"),
            relYCm = o.num("rel_y_cm"),
            lat = o.num("lat"),
            lon = o.num("lon"),
            droneLinkOk = o.bool("drone_link_ok"),
            droneLinkDetail = o.str("drone_link_detail"),
            synced = o.bool("synced"),
            offsetMs = o.num("offset_ms"),
            rttMs = o.num("rtt_ms"),
            skewMs = o.num("skew_ms"),
            controlMode = o.str("control_mode")?.let(ControlMode::of),
            edgeControlMode = o.str("edge_control_mode")?.let(ControlMode::of),
            manualSupported = o.bool("manual_supported"),
            manualLast = o.obj("manual_last")?.let(ManualLast::fromJson),
            orphanLandInS = o.num("orphan_land_in_s"),
        )
    }
}

/**
 * One drone of a ground without a fleet (legacy `fleet_status`). It has no
 * `connected` or `lost`, so its connection state is unknown.
 */
data class LegacyDrone(
    val id: String,
    val battery: Double?,
    /** Centimetres, 0 when unknown. */
    val height: Double?,
    val temperature: Double?,
    val flightTime: Double?,
    val isFlying: Boolean?,
    val isConnected: Boolean?,
) {
    companion object {
        fun fromJson(id: String, o: JsonObject): LegacyDrone = LegacyDrone(
            id = id,
            battery = o.num("battery"),
            height = o.num("height"),
            temperature = o.num("temperature"),
            flightTime = o.num("flight_time"),
            isFlying = o.bool("is_flying"),
            isConnected = o.bool("is_connected"),
        )
    }
}

/** The last manual action on a drone (`manual_last`), or null when there was none. */
data class ManualLast(
    val action: ManualAction?,
    val ok: Boolean?,
    val reason: ReasonCode?,
    val detail: String?,
) {
    companion object {
        fun fromJson(o: JsonObject): ManualLast = ManualLast(
            action = o.str("action")?.let(ManualAction::of),
            ok = o.bool("ok"),
            reason = o.str("reason")?.let(::ReasonCode),
            detail = o.str("detail"),
        )
    }
}
