package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject

/**
 * The confidence word of a reasoning verdict (`result.confidence` of `reasoning_result`, and
 * `gemma_confidence` of `match_alert`). The model's own words are `high`, `medium` and `low`; the
 * ground adds `pending` (a check is queued), `error` (the check failed), `skipped` (the alert was
 * answered before the check ran), `off` (reasoning was switched off) and `unavailable` (the model was
 * not loaded or the queue was full). `unknown` is what the ground writes when it could not read the
 * model's reply. Any other word is kept by name (docs/mobile-contract.md, "Unknown values are text"),
 * cut to [MAX_RAW_CHARS] characters, and is never shown as a verdict (see [VerdictKind.OTHER]).
 */
sealed class ReasoningConfidence(override val raw: String) : WireValue {
    data object High : ReasoningConfidence("high")
    data object Medium : ReasoningConfidence("medium")
    data object Low : ReasoningConfidence("low")
    data object Pending : ReasoningConfidence("pending")
    data object Error : ReasoningConfidence("error")
    data object Skipped : ReasoningConfidence("skipped")
    data object Off : ReasoningConfidence("off")
    data object Unavailable : ReasoningConfidence("unavailable")
    data class Unknown(val value: String) : ReasoningConfidence(value)

    companion object {
        /** The longest confidence word kept. A longer word from the ground is cut, so it cannot pile up in memory. */
        const val MAX_RAW_CHARS = 64

        fun of(raw: String): ReasoningConfidence = when (raw) {
            "high" -> High
            "medium" -> Medium
            "low" -> Low
            "pending" -> Pending
            "error" -> Error
            "skipped" -> Skipped
            "off" -> Off
            "unavailable" -> Unavailable
            else -> Unknown(cut(raw))
        }

        /** [raw] cut to [MAX_RAW_CHARS] characters, without splitting a surrogate pair. */
        private fun cut(raw: String): String {
            if (raw.length <= MAX_RAW_CHARS) return raw
            var end = MAX_RAW_CHARS
            if (Character.isHighSurrogate(raw[end - 1])) end--
            return raw.substring(0, end)
        }
    }
}

/**
 * The `reasoning_result` event (`reasoning_result.json`): the ground's AI check of a match, sent to
 * every client when the check finishes. Only a result with `type` `analyze` and an `alert_id` belongs
 * to a match alert; a `description` result (no `alert_id`) is the laptop's own text-description check
 * and a phone ignores it ([isAlertVerdict]).
 */
data class ReasoningResult(
    val alertId: String?,
    val trackId: String?,
    /** `analyze` (a photo check of an alert) or `description`; any other text is kept by name. */
    val type: String?,
    val match: Boolean?,
    val confidence: ReasoningConfidence?,
    /** The model's one-sentence reason, or the ground's own note. Untrusted text: shorten and clean it before showing. */
    val reasoning: String?,
) : ServerEvent {
    /** True when this result is the verdict of the match alert [alertId]. */
    val isAlertVerdict: Boolean get() = type == TYPE_ANALYZE && !alertId.isNullOrEmpty()

    /** The kind of verdict, or null when the result names no confidence. */
    val verdict: VerdictKind? get() = AlertVerdicts.kindOf(match, confidence)

    companion object {
        const val TYPE_ANALYZE = "analyze"
        const val TYPE_DESCRIPTION = "description"

        fun fromJson(o: JsonObject): ReasoningResult {
            val result = o.obj("result")
            return ReasoningResult(
                alertId = o.str("alert_id") ?: o.num("alert_id")?.let(::jsNumber),
                trackId = o.str("track_id") ?: o.num("track_id")?.let(::jsNumber),
                type = o.str("type"),
                match = result?.bool("match"),
                confidence = result?.str("confidence")?.let(ReasoningConfidence::of),
                reasoning = result?.str("reasoning"),
            )
        }
    }
}

/**
 * The `alert_upgrade` event (`alert_upgrade.json`): the AI check raised a match alert's level. The
 * ground sends it only when the level rose, and a phone applies it only when [newLevel] ranks above
 * the level it shows ([AlertVerdicts.raise]): a level never goes down.
 */
data class AlertUpgrade(
    val alertId: String?,
    val trackId: String?,
    val newLevel: AlertLevel?,
    val reasoning: String?,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): AlertUpgrade = AlertUpgrade(
            alertId = o.str("alert_id") ?: o.num("alert_id")?.let(::jsNumber),
            trackId = o.str("track_id") ?: o.num("track_id")?.let(::jsNumber),
            newLevel = o.str("new_level")?.let(AlertLevel::of),
            reasoning = o.str("reasoning"),
        )
    }
}

/** What an AI check concluded, in the words a phone shows. */
enum class VerdictKind {
    /** The check is queued or running. */
    PENDING,

    /** Likely the same person (high or medium confidence). */
    MATCH,

    /** Likely not the same person (high or medium confidence). */
    NO_MATCH,

    /** Low confidence, or a reply the ground could not read. */
    UNCERTAIN,

    /** The check failed. Shown as a neutral note, never as an error. */
    ERROR,

    /** The alert was answered before the check ran. */
    SKIPPED,

    /** Reasoning was off or not available, so no check ran. */
    NOT_RUN,

    /**
     * A confidence word this app does not know. It is not a verdict: a phone shows the neutral note
     * "AI check: unclear", never the word itself and never the model's reason.
     */
    OTHER,
}

/** Verdict and level rules shared by the alert list and its tests. */
object AlertVerdicts {
    /** `flightrisk/dashboard/alerts.py` `_LEVEL_RANK` and `ui.js` `LEVEL_RANK`; an unknown level ranks 0. */
    fun rank(level: AlertLevel?): Int = when (level) {
        AlertLevel.ConfirmedMatch -> 3
        AlertLevel.PossibleMatch -> 2
        AlertLevel.WeakSignal -> 1
        AlertLevel.NoMatch, is AlertLevel.Unknown, null -> 0
    }

    /** The level to show after [proposed] arrives for an alert showing [current]: never lower than [current]. */
    fun raise(current: AlertLevel?, proposed: AlertLevel?): AlertLevel? =
        if (proposed != null && rank(proposed) > rank(current)) proposed else current

    /** The kind of verdict for a match flag and confidence word, or null when there is no confidence word. */
    fun kindOf(match: Boolean?, confidence: ReasoningConfidence?): VerdictKind? = when (confidence) {
        null -> null
        ReasoningConfidence.Pending -> VerdictKind.PENDING
        ReasoningConfidence.Error -> VerdictKind.ERROR
        ReasoningConfidence.Skipped -> VerdictKind.SKIPPED
        ReasoningConfidence.Off, ReasoningConfidence.Unavailable -> VerdictKind.NOT_RUN
        ReasoningConfidence.High, ReasoningConfidence.Medium -> when (match) {
            true -> VerdictKind.MATCH
            false -> VerdictKind.NO_MATCH
            null -> VerdictKind.UNCERTAIN
        }
        ReasoningConfidence.Low -> VerdictKind.UNCERTAIN
        is ReasoningConfidence.Unknown -> if (confidence.value == "unknown") VerdictKind.UNCERTAIN else VerdictKind.OTHER
    }

    /**
     * Whether the model's reason may be shown with this verdict. Error and skipped notes never show the
     * ground's text, and neither does an unknown confidence word ([VerdictKind.OTHER]), whose reply the
     * app cannot read as a verdict.
     */
    fun showsRationale(kind: VerdictKind?): Boolean =
        kind == VerdictKind.MATCH || kind == VerdictKind.NO_MATCH || kind == VerdictKind.UNCERTAIN

    /** True for a verdict that ends the check. A pending state never replaces a final one. */
    fun isFinal(kind: VerdictKind?): Boolean = kind != null && kind != VerdictKind.PENDING
}
