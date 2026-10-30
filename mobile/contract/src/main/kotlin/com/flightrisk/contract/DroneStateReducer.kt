package com.flightrisk.contract

/**
 * One drone as the phone currently knows it, built by [DroneStateReducer].
 */
sealed interface DroneRecord {
    val id: String

    /** The connection state of this record (`connState`). */
    val connection: ConnectionState

    /** The control-mode display of this record (`modeText`). */
    fun controlMode(manualControlled: String?): ControlModeDisplay

    /**
     * A drone of a fleet ground: the latest `fleet_status` entry with any
     * later `manual_status` keys laid over it. [partial] is true when only a
     * `manual_status` entry has been seen for the drone (no `fleet_status`
     * entry yet), so its connection, battery and position are unknown.
     */
    data class Fleet(val drone: FleetDrone, val partial: Boolean) : DroneRecord {
        override val id: String get() = drone.id
        override val connection: ConnectionState get() = ConnectionState.of(drone)
        override fun controlMode(manualControlled: String?): ControlModeDisplay =
            ControlModeDisplay.of(drone.id, drone, manualControlled)
    }

    /** A drone of a ground without a fleet (legacy `fleet_status`). */
    data class Legacy(val drone: LegacyDrone) : DroneRecord {
        override val id: String get() = drone.id
        override val connection: ConnectionState get() = ConnectionState.of(drone)
        override fun controlMode(manualControlled: String?): ControlModeDisplay = ControlModeDisplay.of(drone)
    }
}

/**
 * What the phone knows about every drone, as [DroneStateReducer] built it.
 *
 * @property drones Records keyed by drone id, sorted by id (the display order).
 * @property manualControlled The drone the laptop controls manually, or null
 *   (an empty string on the wire also means nobody).
 * @property stale True after [DroneStateReducer.disconnected] until the next
 *   `fleet_status`: the cards then show the last values received.
 */
data class DroneBoard(
    val drones: Map<String, DroneRecord>,
    val manualControlled: String?,
    val stale: Boolean,
) {
    /** Drone ids in display order. */
    val droneIds: List<String> get() = drones.keys.toList()

    fun connection(id: String): ConnectionState = drones[id]?.connection ?: ConnectionState.UNKNOWN

    fun controlMode(id: String): ControlModeDisplay? = drones[id]?.controlMode(manualControlled)

    /** Whether Resume search may be offered for the drone [id]. */
    fun canResumeSearch(id: String): Boolean {
        val record = drones[id] as? DroneRecord.Fleet ?: return false
        return ClientCommand.canResumeSearch(record.drone, manualControlled)
    }

    companion object {
        val EMPTY: DroneBoard = DroneBoard(emptyMap(), null, stale = false)
    }
}

/**
 * Builds the drone cards from `fleet_status` and `manual_status` strictly in
 * the order the events arrive, the way the dashboard does (`ui.js`,
 * `socket.on('fleet_status')` and `socket.on('manual_status')`): the newest
 * event wins.
 *
 * - A `fleet_status` replaces each drone's whole record (any earlier
 *   `manual_status` overlay is dropped), removes drones it does not list, and
 *   sets [DroneBoard.manualControlled] from `manual_controlled` when the key
 *   is present (the fleet shape always carries it).
 * - A `manual_status` replaces only the keys it carries on each listed
 *   drone's record and sets [DroneBoard.manualControlled] from `controlled`.
 *   A drone it lists that has no record yet gets a partial record
 *   ([DroneRecord.Fleet.partial]) until the next `fleet_status`.
 * - Every other event leaves the board unchanged.
 *
 * The UI must feed every `fleet_status` and `manual_status` through
 * [reduce] in arrival order and derive the cards from the resulting board.
 * Merging a `fleet_status` entry and a `manual_status` entry any other way
 * (for example always laying the last `manual_status` over the latest
 * `fleet_status`) shows a held drone as normal or a stale countdown, because
 * the ground sends `fleet_status` with fresh control fields on every tick and
 * `manual_status` only when manual control changes.
 */
object DroneStateReducer {
    /** The board before any event. */
    val INITIAL: DroneBoard = DroneBoard.EMPTY

    /** Applies one decoded server event to [board] and returns the new board. Never throws. */
    fun reduce(board: DroneBoard, event: ServerEvent): DroneBoard = when (event) {
        is FleetStatus.Fleet -> DroneBoard(
            drones = event.drones.mapValuesTo(sortedMapOf()) { (_, d) -> DroneRecord.Fleet(d, partial = false) },
            manualControlled = event.manualControlled.nullIfEmpty(),
            stale = false,
        )
        is FleetStatus.Legacy -> DroneBoard(
            drones = event.drones.mapValuesTo(sortedMapOf()) { (_, d) -> DroneRecord.Legacy(d) },
            // A legacy fleet_status carries no manual_controlled; the dashboard keeps its value.
            manualControlled = board.manualControlled,
            stale = false,
        )
        is ManualStatus -> applyManual(board, event)
        else -> board
    }

    /** Applies events in order. */
    fun reduceAll(board: DroneBoard, events: Iterable<ServerEvent>): DroneBoard =
        events.fold(board) { b, e -> reduce(b, e) }

    /**
     * The socket dropped: keep the cards but mark them stale until the next
     * `fleet_status` (docs/mobile-contract.md: never keep a card from before a
     * disconnect without marking it stale).
     */
    fun disconnected(board: DroneBoard): DroneBoard = board.copy(stale = true)

    private fun applyManual(board: DroneBoard, event: ManualStatus): DroneBoard {
        val drones = sortedMapOf<String, DroneRecord>()
        drones.putAll(board.drones)
        for ((id, manual) in event.drones) {
            drones[id] = when (val existing = drones[id]) {
                is DroneRecord.Fleet -> existing.copy(drone = existing.drone.overlaidWith(manual))
                // A ground without a fleet has no manual control; keep its record.
                is DroneRecord.Legacy -> existing
                null -> DroneRecord.Fleet(emptyFleetDrone(id).overlaidWith(manual), partial = true)
            }
        }
        return board.copy(drones = drones, manualControlled = event.controlled.nullIfEmpty())
    }

    private fun String?.nullIfEmpty(): String? = this?.takeIf { it.isNotEmpty() }

    private fun emptyFleetDrone(id: String) = FleetDrone(
        id = id, droneId = null, connected = null, lost = null, linkAgeS = null, battery = null,
        heightCm = null, altCm = null, headingDeg = null, isFlying = null, laneIndex = null, lanesTotal = null,
        acked = null, relXCm = null, relYCm = null, lat = null, lon = null, droneLinkOk = null,
        droneLinkDetail = null, synced = null, offsetMs = null, rttMs = null, skewMs = null, controlMode = null,
        edgeControlMode = null, manualSupported = null, manualLast = null, orphanLandInS = null,
    )
}
