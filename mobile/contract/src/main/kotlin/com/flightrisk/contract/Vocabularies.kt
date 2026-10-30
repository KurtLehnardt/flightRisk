package com.flightrisk.contract

/**
 * A closed wire vocabulary with an open fallback: every known value is an
 * object, and a value this app does not know decodes to an `Unknown(raw)` case
 * that keeps the text so it can be shown by name (docs/mobile-contract.md,
 * "Unknown values are text"). Decoding a vocabulary never throws.
 */
interface WireValue {
    /** The exact string on the wire. */
    val raw: String
}

/** `control_mode` and `edge_control_mode` of a drone (`nack_reasons.json`, case `control_modes`). */
sealed class ControlMode(override val raw: String) : WireValue {
    data object Autonomous : ControlMode("autonomous")
    data object Held : ControlMode("held")
    data object Manual : ControlMode("manual")
    data class Unknown(val value: String) : ControlMode(value)

    companion object {
        /** Every value this app knows, in the order of `nack_reasons.json`. */
        fun known(): List<ControlMode> = listOf(Autonomous, Held, Manual)

        fun of(raw: String): ControlMode = when (raw) {
            "autonomous" -> Autonomous
            "held" -> Held
            "manual" -> Manual
            else -> Unknown(raw)
        }
    }
}

/** The ground's reasoning provider (`reasoning_provider.provider`). */
sealed class ReasoningProviderKind(override val raw: String) : WireValue {
    data object Gemma : ReasoningProviderKind("gemma")
    data object Claude : ReasoningProviderKind("claude")
    data object Off : ReasoningProviderKind("off")
    data class Unknown(val value: String) : ReasoningProviderKind(value)

    companion object {
        fun of(raw: String): ReasoningProviderKind = when (raw) {
            "gemma" -> Gemma
            "claude" -> Claude
            "off" -> Off
            else -> Unknown(raw)
        }
    }
}

/** `match_alert.alert_level` (`flightrisk/vision/scorer.py`, `alert_level`). */
sealed class AlertLevel(override val raw: String) : WireValue {
    data object ConfirmedMatch : AlertLevel("confirmed_match")
    data object PossibleMatch : AlertLevel("possible_match")
    data object WeakSignal : AlertLevel("weak_signal")
    data object NoMatch : AlertLevel("no_match")
    data class Unknown(val value: String) : AlertLevel(value)

    companion object {
        fun of(raw: String): AlertLevel = when (raw) {
            "confirmed_match" -> ConfirmedMatch
            "possible_match" -> PossibleMatch
            "weak_signal" -> WeakSignal
            "no_match" -> NoMatch
            else -> Unknown(raw)
        }
    }
}

/** A lane's `direction` in the plan frame (`flightrisk/drone/coordinator.py`, `_DIRECTION_VECTORS`). */
sealed class LaneDirection(override val raw: String) : WireValue {
    data object Forward : LaneDirection("forward")
    data object Back : LaneDirection("back")
    data object Right : LaneDirection("right")
    data object Left : LaneDirection("left")
    data class Unknown(val value: String) : LaneDirection(value)

    companion object {
        fun of(raw: String): LaneDirection = when (raw) {
            "forward" -> Forward
            "back" -> Back
            "right" -> Right
            "left" -> Left
            else -> Unknown(raw)
        }
    }
}

/** `manual_ack.action` and `manual_result.action` (`flightrisk/dashboard/manual_control.py`). */
sealed class ManualAction(override val raw: String) : WireValue {
    data object Land : ManualAction("land")
    data object ResumeSearch : ManualAction("resume_search")
    data object TakeControl : ManualAction("take_control")
    data class Unknown(val value: String) : ManualAction(value)

    companion object {
        fun of(raw: String): ManualAction = when (raw) {
            "land" -> Land
            "resume_search" -> ResumeSearch
            "take_control" -> TakeControl
            // Manual flight commands (takeoff, hover, moves) are laptop-only
            // and are shown by name.
            else -> Unknown(raw)
        }
    }
}

/**
 * A refusal, nack or fallback reason code (`reason`, `fallback_reason`,
 * `refusal.reason`). The reason vocabularies are large and grow with the fleet
 * protocol, so a reason is kept as its text with a [known] check against
 * [NackReasons] instead of one case per value. It is never an error.
 */
@JvmInline
value class ReasonCode(val raw: String) {
    /** True when the code is in one of today's vocabularies. */
    val known: Boolean get() = raw in NackReasons.ALL_REASONS

    override fun toString(): String = raw
}

/**
 * Today's reason and mode vocabularies from the fleet protocol constants,
 * copied from `tests/fixtures/mobile_contract/nack_reasons.json`. A contract
 * test fails when the fixture gains a value that is missing here.
 */
object NackReasons {
    val ASSIGN_LANES_V1: Set<String> = setOf("altitude", "battery", "geofence", "invalid", "other_drone")

    val FENCED_COMMAND_V2: Set<String> = setOf(
        "altitude", "battery", "busy", "drone_unreachable", "geofence", "invalid", "landing",
        "no_valid_plan", "not_authority", "not_flying", "other_drone", "plan_mismatch",
        "stale_epoch", "stale_plan",
    )

    val SET_DETECTION_PROFILE: Set<String> =
        setOf("busy", "in_flight", "invalid", "load_failed", "unavailable", "unknown_profile")

    val PROFILE_FALLBACK: Set<String> = setOf("weights_missing")

    val SET_CEILING: Set<String> = setOf("above_ceiling", "in_flight", "invalid", "not_authority", "stale_epoch")

    val MANUAL_COMMAND: Set<String> = setOf(
        "altitude", "battery", "busy", "ceiling", "drone_unreachable", "floor", "geofence", "invalid",
        "landing", "no_valid_plan", "not_authority", "not_flying", "not_manual", "other_drone",
        "plan_mismatch", "slot_band", "stale_epoch", "stale_plan", "unsupported",
    )

    val CONTROL_MODES: Set<String> = setOf("autonomous", "held", "manual")

    val HOLD_REASONS: Set<String> = setOf("alert", "handover", "operator")

    val EDGE_STATES: Set<String> = setOf("flying_lanes", "holding", "hovering", "idle", "landing", "taking_off")

    /**
     * Reasons the dashboard itself adds outside the fleet protocol constants:
     * `no_reply` is the ground's own timeout for a manual command
     * (`manual_result`, `MANUAL_REPLY_TIMEOUT_S`).
     */
    val DASHBOARD_REASONS: Set<String> = setOf("no_reply")

    /** The reason vocabularies by the case name used in `nack_reasons.json`. */
    val BY_FIXTURE_CASE: Map<String, Set<String>> = mapOf(
        "assign_lanes_v1" to ASSIGN_LANES_V1,
        "fenced_command_v2" to FENCED_COMMAND_V2,
        "set_detection_profile" to SET_DETECTION_PROFILE,
        "profile_fallback" to PROFILE_FALLBACK,
        "set_ceiling" to SET_CEILING,
        "manual_command" to MANUAL_COMMAND,
        "control_modes" to CONTROL_MODES,
        "hold_reasons" to HOLD_REASONS,
        "edge_states" to EDGE_STATES,
    )

    /** Every reason code known today (control modes and edge states are not reasons). */
    val ALL_REASONS: Set<String> =
        ASSIGN_LANES_V1 + FENCED_COMMAND_V2 + SET_DETECTION_PROFILE + PROFILE_FALLBACK +
            SET_CEILING + MANUAL_COMMAND + HOLD_REASONS + DASHBOARD_REASONS
}
