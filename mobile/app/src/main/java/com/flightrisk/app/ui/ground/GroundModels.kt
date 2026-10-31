package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.GroundSnapshot
import com.flightrisk.contract.ConnectionState
import com.flightrisk.contract.DroneBoard

/** The connection as the Ground screen shows it. */
sealed interface GroundLink {
    /** No address or no key saved yet. */
    data object NotConfigured : GroundLink

    /** The secure storage cannot be opened, so no key can be read. Fails safe: no connection. */
    data object StorageUnavailable : GroundLink

    /** Configured, but the user switched the link off. */
    data object Off : GroundLink

    /** The app is in the background; the link is closed until it returns. */
    data object Paused : GroundLink

    data class Connecting(val attempt: Int) : GroundLink
    data object Connected : GroundLink
    data class Reconnecting(val attempt: Int, val retryInSeconds: Double, val reason: String) : GroundLink

    /** The ground refused the key. Distinct from network loss; the client waits for the user. */
    data object WrongKey : GroundLink

    /** The address is not on a private network, so the client never connected. */
    data class AddressRefused(val detail: String) : GroundLink

    /** Idle: nothing is connecting and nothing is switched off (for example after the key was removed). */
    data object Stopped : GroundLink

    val isConnected: Boolean get() = this is Connected

    val text: String
        get() = when (this) {
            NotConfigured -> "Not configured: add the ground station address and key under Connection below."
            StorageUnavailable -> "${GroundText.SECURE_STORAGE_UNAVAILABLE}: this phone cannot read the saved key, so it does not connect to the ground."
            Off -> "Ground link off. Switch it on under Connection below."
            Paused -> "Not connected: the app was in the background."
            is Connecting -> if (attempt > 1) "Connecting (attempt $attempt)…" else "Connecting…"
            Connected -> "Connected"
            is Reconnecting -> "Reconnecting (attempt $attempt, next try in ${retryInSeconds.formatSeconds()} s): $reason"
            WrongKey -> "Wrong key: the ground station refused the API key. Check the key under Connection below."
            is AddressRefused -> "Address refused: $detail"
            Stopped -> "Not connected."
        }

    /** The short label of the link pill. */
    val pillText: String
        get() = when (this) {
            Connected -> "LIVE"
            is Connecting -> "CONNECTING"
            is Reconnecting -> "RECONNECTING"
            WrongKey -> "WRONG KEY"
            StorageUnavailable -> "NO STORAGE"
            is AddressRefused -> "REFUSED"
            NotConfigured -> "NOT SET UP"
            Off -> "OFF"
            Paused, Stopped -> "OFFLINE"
        }
}

private fun Double.formatSeconds(): String = if (this == Math.floor(this)) toLong().toString() else "%.1f".format(java.util.Locale.ROOT, this)

/** What a command is doing, from the tap to the ground's final answer. */
enum class CommandPhase {
    /** The tap was taken; the command is on its way to the connection. */
    SENDING,

    /** Handed to the connection. Not proof of delivery: the ground's answer is. */
    HANDED_OVER,

    /** The ground accepted it. When [CommandFeedback.awaitsFinalAnswer] is true the edge's final answer is still due. */
    ACCEPTED,

    /** No answer within the reply timeout. The operator may tap again. */
    NO_REPLY_YET,
    SUCCEEDED,

    /** LAND ALL reached only some drones: a warning that never fades. */
    PARTIAL,
    FAILED,
}

/** The visible result of one command, or of a failure the ground reported for a drone. */
data class CommandFeedback(
    /** Distinguishes taps, including two taps within the same instant. */
    val attempt: Int,
    val phase: CommandPhase,
    val text: String,
    val detail: String? = null,
    val startedAtMs: Long,
    val updatedAtMs: Long,
    val awaitsFinalAnswer: Boolean = true,
) {
    val isFailure: Boolean get() = phase == CommandPhase.FAILED

    /** Shown in the warning colour. */
    val isWarning: Boolean
        get() = phase == CommandPhase.FAILED || phase == CommandPhase.PARTIAL || phase == CommandPhase.NO_REPLY_YET

    /** Still waiting for the ground (or the edge) to answer. */
    val isWaiting: Boolean
        get() = when (phase) {
            CommandPhase.SENDING, CommandPhase.HANDED_OVER, CommandPhase.NO_REPLY_YET -> true
            CommandPhase.ACCEPTED -> awaitsFinalAnswer
            CommandPhase.SUCCEEDED, CommandPhase.PARTIAL, CommandPhase.FAILED -> false
        }

    /** A second tap is simply another command; nothing is retried automatically. */
    val offersRetry: Boolean
        get() = phase == CommandPhase.NO_REPLY_YET || phase == CommandPhase.FAILED || phase == CommandPhase.PARTIAL

    /** Failures and partial results stay until dismissed. */
    val canDismiss: Boolean get() = phase == CommandPhase.FAILED || phase == CommandPhase.PARTIAL
}

/** A `fleet_event` banner or a refused plan or start. */
data class GroundBanner(val id: Long, val text: String, val isFailure: Boolean, val receivedAtMs: Long)

/** An `error` or `warning` from the ground. Errors stay until dismissed; warnings fade. */
data class GroundToast(val id: Long, val text: String, val isError: Boolean, val receivedAtMs: Long)

/** Everything one drone status card shows. */
data class DroneCardUi(
    val id: String,
    val connection: ConnectionState,
    /** "CONNECTED", or "LAST: CONNECTED" while the whole state is stale. */
    val linkChipText: String,
    /** True when the link chip must not use its live colour (the state is stale). */
    val linkChipGreyed: Boolean,
    /** "MANUAL", "HELD" or null. */
    val controlChip: String?,
    /** The control text with the HELD countdown counted down locally; empty for none. */
    val controlText: String,
    val isHeldWithCountdown: Boolean,
    /** "87%", "87% (last value)" or "--". */
    val batteryText: String,
    /** "100 cm", "100 cm (last value)" or "--". */
    val altitudeText: String,
    /** "2/5", or null when the drone reports no lane. */
    val laneText: String?,
    /** "Drone link down: <detail>. Land still works.", or null. */
    val linkNotice: String?,
    val isPartial: Boolean,
    /** True when the whole state is stale. */
    val isStateStale: Boolean,
    /** True when the values are not live: the whole state is stale or the drone is DOWN or LOST. */
    val isValuesStale: Boolean,
    val staleNote: String?,
    val canResume: Boolean,
    val landFeedback: CommandFeedback?,
    val resumeFeedback: CommandFeedback?,
    /** A failure the ground reported for this drone (for example a failed orphan auto-land). */
    val alert: CommandFeedback?,
    val landFeedbackText: String?,
    val resumeFeedbackText: String?,
    val alertText: String?,
) {
    /** The id as shown: cut short when the ground sent a very long one. Commands still carry the real [id]. */
    val displayId: String get() = GroundText.shortId(id)

    /** "Land again" once a land failed or got no answer. */
    val landTitle: String get() = if (landFeedback?.offersRetry == true) "Land again" else "Land"
}

/** Whether a dashboard key is saved. Never the key itself. */
enum class KeyState { UNKNOWN, SAVED, NONE, UNAVAILABLE }

/** The connection card: address, key state, link switch and test result. Holds no secret. */
data class ConnectionCardState(
    /** The saved address as `host:port`, or null. */
    val savedAddress: String? = null,
    val addressError: String? = null,
    val keyState: KeyState = KeyState.UNKNOWN,
    val enabled: Boolean = false,
    val message: String? = null,
    val testing: Boolean = false,
    val testResult: String? = null,
) {
    val isConfigured: Boolean get() = savedAddress != null && keyState == KeyState.SAVED
}

/** Everything the Ground screen draws. */
data class GroundUiState(
    val link: GroundLink = GroundLink.NotConfigured,
    /** True while the drone state is from before a disconnect, a reconnect or a gap in events. */
    val isStateStale: Boolean = false,
    val staleNote: String? = null,
    val cards: List<DroneCardUi> = emptyList(),
    val readout: String = "0 drones, 0 connected",
    val landAll: CommandFeedback? = null,
    val landAllText: String? = null,
    val banners: List<GroundBanner> = emptyList(),
    val toasts: List<GroundToast> = emptyList(),
    val appearanceOnly: Boolean = false,
    val connection: ConnectionCardState = ConnectionCardState(),
    /** The board the cards were built from (stale-marked when it is not live). For later units. */
    val board: DroneBoard = DroneBoard.EMPTY,
    /** The latest whole-state payloads of the ground (plan, profiles, ceiling, reasoning). For later units. */
    val snapshot: GroundSnapshot = GroundSnapshot(),
)
