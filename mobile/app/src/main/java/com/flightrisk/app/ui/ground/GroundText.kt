package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.DropReason
import com.flightrisk.contract.DisplayText
import com.flightrisk.contract.LandAllAck
import com.flightrisk.contract.ManualReply
import com.flightrisk.contract.ReasonCode

/**
 * The words of the Ground screen that the contract module does not own. They
 * mirror the iOS Ground tab (`GroundUIText`) so the two phones say the same
 * thing in the same situation. The contract's own texts (LAND ALL results,
 * drone link notices, fleet event banners) come from [DisplayText].
 */
object GroundText {
    const val PRIVACY_NOTE = "Connects only to the ground station address you enter."
    const val SECURE_STORAGE_UNAVAILABLE = "Secure storage unavailable on this device"

    private const val LAND_BY_OTHER_MEANS = DisplayText.LAND_NOW_BY_OTHER_MEANS

    const val LAND_ALL_HANDED_OVER =
        DisplayText.LAND_ALL_SENDING + " Handed to the connection; waiting for the ground's reply."
    const val LAND_ALL_NOT_SENT_LINK_OFF =
        "LAND ALL was NOT sent: this phone's ground link is off or not set up. $LAND_BY_OTHER_MEANS"
    const val LAND_ALL_NOT_SENT_STORAGE =
        "LAND ALL was NOT sent: secure storage is unavailable on this device, so this phone cannot connect to the ground. $LAND_BY_OTHER_MEANS"
    const val LAND_ALL_SEND_FAILED =
        "LAND ALL was NOT sent: the connection failed while sending. $LAND_BY_OTHER_MEANS"
    const val LAND_ALL_CONNECTION_LOST =
        "LAND ALL: the connection was lost before the ground answered. $LAND_BY_OTHER_MEANS"
    const val LAND_ALL_NO_REPLY_YET = "LAND ALL: no reply from the ground yet. Check the connection."

    const val STALE_AFTER_DISCONNECT = "Last values: not connected to the ground."
    const val STALE_REFRESHING = "Last values: waiting for a fresh fleet status from the ground."
    const val LAST_VALUE_SUFFIX = " (last value)"
    const val PARTIAL_RECORD = "Partial record: only manual-control data so far; waiting for the fleet status."

    const val ACTION_LAND = "Land"
    const val ACTION_RESUME = "Resume search"

    /**
     * The dashboard's partial-ack text (`ui.js`, `landAllAckFeedback`):
     * `reached_any` is true but at least one path reports 0 drones. Null when
     * no path reports 0.
     */
    fun landAllPartial(ack: LandAllAck): String? {
        if (ack.reachedAny != true) return null
        val paths = listOf("coordinator" to ack.coordinator, "transport" to ack.transport, "local" to ack.local)
        val none = paths.filter { it.second == 0 }.map { it.first }
        if (none.isEmpty()) return null
        val got = paths.mapNotNull { (name, count) -> count?.takeIf { it > 0 }?.let { "$name $it" } }
        return "LAND ALL reached only some drones. Reached: " + got.joinToString(", ") +
            ". Reached none: " + none.joinToString(", ") + ". Land the rest by hand."
    }

    fun droneNotSent(id: String, action: String, linkOff: Boolean): String {
        val why = if (linkOff) "this phone's ground link is off or not set up" else "this phone is not connected to the ground"
        return "$id: $action was NOT sent: $why. Land it with its own controls, or use the ground laptop."
    }

    fun droneSendFailed(id: String, action: String): String =
        "$id: $action was NOT sent: the connection failed while sending. Land it with its own controls, or use the ground laptop."

    fun droneNoUsableId(action: String): String =
        if (action == ACTION_LAND) {
            "Land was not sent: this drone has no usable id. Land it with its own controls."
        } else {
            "$action was not sent: this drone has no usable id."
        }

    fun droneSending(id: String, action: String): String = "$id: $action sending…"

    fun droneHandedOver(id: String, action: String): String =
        "$id: $action sending… Handed to the connection; waiting for the ground's reply."

    fun droneNoReplyYet(id: String, action: String): String =
        "$id: $action: no reply from the ground yet. Check the connection."

    fun droneNoFinalAnswer(id: String, action: String): String =
        if (action == ACTION_LAND) {
            "$id: no final answer from the drone's edge. Land it with its own controls."
        } else {
            "$id: $action: no final answer from the drone's edge."
        }

    fun droneConnectionLost(id: String, action: String): String =
        if (action == ACTION_LAND) {
            "$id: connection lost before the answer; land it with its own controls."
        } else {
            "$id: $action: connection lost before the answer."
        }

    fun legacyLandAccepted(id: String): String = "$id: Land accepted by the ground (no confirmation from the drone)."

    fun landAccepted(id: String): String = "$id: Land accepted by the ground; waiting for the drone's edge."
    fun resumeAccepted(id: String): String = "$id: Resume search accepted by the ground; waiting for the drone's edge."
    fun landAcknowledged(id: String): String = "$id: Land acknowledged by the drone's edge."
    fun searchResumed(id: String): String = "$id: search resumed."

    /** A refused or failed manual reply whose action the phone did not send (an orphan auto-land, for example). */
    fun droneActionFailed(id: String, action: String, why: String): String =
        "$id: $action failed ($why). Land it with its own controls."

    /** " (12 s ago)" for an age of at least one second, else "". */
    fun age(seconds: Double): String {
        if (!seconds.isFinite() || seconds < 1) return ""
        return if (seconds < 120) " (${seconds.toInt()} s ago)" else " (${(seconds / 60).toInt()} min ago)"
    }

    /** The refusal line of a manual reply: "<drone>: <why>", with the ground's `detail` when it sent one. */
    fun manualWhy(reply: ManualReply): String {
        val detail = reply.detail?.takeIf { it.isNotEmpty() }
        if (detail != null) return detail
        return reply.reason?.let { nackReasonText(it) } ?: "refused"
    }

    /** Short readable text for a refusal reason. Unknown reasons are shown by name. */
    fun nackReasonText(reason: ReasonCode): String = when (reason.raw) {
        "altitude" -> "altitude not allowed"
        "battery" -> "battery too low"
        "busy" -> "drone is busy"
        "ceiling" -> "above the ceiling"
        "drone_unreachable" -> "drone unreachable"
        "floor" -> "below the floor"
        "geofence" -> "outside the geofence"
        "invalid" -> "invalid request"
        "landing" -> "drone is landing"
        "no_valid_plan" -> "no valid plan"
        "not_authority" -> "this ground is not the authority"
        "not_flying" -> "drone is not flying"
        "not_manual" -> "drone is not under manual control"
        "other_drone" -> "conflicts with another drone"
        "plan_mismatch" -> "plan mismatch"
        "slot_band" -> "outside the altitude slot"
        "stale_epoch" -> "stale command epoch"
        "stale_plan" -> "stale plan"
        "unsupported" -> "not supported by this edge"
        "no_reply" -> "no reply from the edge"
        else -> reason.raw
    }

    /** The reason a connection ended, in words (already redacted by the client). */
    fun dropReason(reason: DropReason): String = when (reason) {
        DropReason.HandshakeTimeout -> "the handshake timed out"
        DropReason.PingTimeout -> "the ground stopped answering"
        is DropReason.ServerClosed -> "the ground closed the connection" + reason.detail.suffixed()
        DropReason.ServerDisconnected -> "the ground disconnected this phone"
        is DropReason.MessageTooLarge -> "a message was larger than ${reason.limitBytes} bytes"
        is DropReason.EarlyEventOverflow -> "too many events arrived before the connection was ready"
        is DropReason.ConsumerStalled -> "the app fell behind the ground's events"
        is DropReason.ProtocolError -> "protocol error" + reason.detail.suffixed()
        is DropReason.TransportError -> "network error" + reason.detail.suffixed()
        is DropReason.InternalError -> "internal error" + reason.detail.suffixed()
    }

    private fun String.suffixed(): String = if (isBlank()) "" else " ($this)"
}
