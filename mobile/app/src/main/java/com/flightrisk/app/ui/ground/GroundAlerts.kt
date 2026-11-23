package com.flightrisk.app.ui.ground

import com.flightrisk.contract.AlertLevel
import com.flightrisk.contract.AlertUpgrade
import com.flightrisk.contract.AlertVerdicts
import com.flightrisk.contract.MatchAlert
import com.flightrisk.contract.MatchAlertResolved
import com.flightrisk.contract.ReasoningConfidence
import com.flightrisk.contract.ReasoningResult
import com.flightrisk.contract.VerdictKind

/**
 * Match alerts the ground reports, read only. The phone shows what the ground found and what the
 * operator answered; the operator answers on the laptop. `match_alert_response` is not a command the
 * phone can send (docs/mobile-contract.md), so there is nothing here that sends, confirms or dismisses.
 *
 * The ground sends `match_alert` when it finds a match, `match_alert_reminder` while the alert waits
 * for the operator, and `match_alert_resolved` once the operator confirmed or dismissed it. When the
 * ground runs an AI check of the match, `reasoning_result` carries its verdict and `alert_upgrade`
 * raises the alert's level. A level shown here never goes down.
 */
enum class AlertStatus {
    /** Reported in this connection and not answered as far as the phone has heard. */
    WAITING,

    /** The operator confirmed the match on the laptop. */
    CONFIRMED,

    /** The operator dismissed the alert on the laptop. */
    DISMISSED,

    /** The ground reported an answer this app does not know. */
    ANSWERED,

    /** Reported in an earlier connection and not repeated since: the phone cannot say whether it is answered. */
    EARLIER,
}

/** One row of the alert list. All text is from [GroundAlertsLog], already shortened and made safe to show. */
data class GroundAlertRow(
    /** The ground's alert id, shortened, or a position-based id for an alert without one. */
    val id: String,
    /** "Possible match", or the ground's own level text, shortened. */
    val levelText: String,
    /** "Track 8_7", or null when the ground named none. */
    val trackText: String?,
    val timeText: String,
    /** "Score 86% (ReID 86%, face 0%)". */
    val scoreText: String,
    val reasonText: String?,
    val status: AlertStatus,
    val statusText: String,
    /** True for a `match_alert_reminder`: the alert still waited for the operator when the ground sent it. */
    val isReminder: Boolean,
    val reminderText: String?,
    /** What the ground's AI check concluded, or null when the ground reported no check for this alert. */
    val verdict: VerdictKind? = null,
    /** "AI check: likely the same person (high confidence)", a neutral note for an error or a skipped check, or null. */
    val verdictText: String? = null,
    /** True for a note (running, error, skipped, not run) rather than a conclusion: drawn in a muted colour, never as an error. */
    val verdictIsNote: Boolean = false,
    /** The model's reason, short and cleaned, for the row. Never shown for an error or a skipped check. */
    val rationaleText: String? = null,
    /** The model's reason at full display length (at most [GroundText.MAX_SERVER_TEXT]) for the expanded detail. */
    val detailRationaleText: String? = null,
    /** "Confidence: high" for the expanded detail, or null. */
    val confidenceText: String? = null,
    /** "Raised to Strong match by the AI check.", or null when the check did not raise the level. */
    val levelRaisedText: String? = null,
) {
    val accessibilitySummary: String
        get() = buildList {
            add(levelText)
            trackText?.let(::add)
            add("at $timeText")
            add(scoreText)
            reasonText?.let(::add)
            verdictText?.let(::add)
            rationaleText?.let(::add)
            levelRaisedText?.let(::add)
            add(statusText)
            reminderText?.let(::add)
        }.joinToString(", ")

    /** True when the row has more to show when expanded. */
    val hasDetail: Boolean get() = verdictText != null
}

/** What the alert card draws. */
data class GroundAlertsUi(val rows: List<GroundAlertRow>, val notLiveNote: String?) {
    companion object {
        val EMPTY = GroundAlertsUi(emptyList(), null)
    }
}

/** The alerts the ground reported, newest last, at most [MAX_ALERTS]. Not thread safe: use it from one thread. */
class GroundAlertsLog {
    private class Entry(var alert: MatchAlert, var answer: MatchAlertResolved.Action?, var answered: Boolean, var session: Int) {
        /** The highest level the ground reported for this alert. */
        var level: AlertLevel? = alert.alertLevel

        /** The level of the first report, so the row can say the AI check raised it. */
        val firstLevel: AlertLevel? = alert.alertLevel
        var raisedByCheck = false
        var verdictMatch: Boolean? = null
        var verdictConfidence: ReasoningConfidence? = null

        /** Already cleaned and cut to [GroundText.MAX_SERVER_TEXT]. */
        var verdictReasoning: String? = null

        val verdict: VerdictKind? get() = AlertVerdicts.kindOf(verdictMatch, verdictConfidence)

        fun setVerdict(match: Boolean?, confidence: ReasoningConfidence?, reasoning: String?) {
            val next = AlertVerdicts.kindOf(match, confidence) ?: return
            // A final verdict is never replaced by "running": a late reminder may still carry the pending copy.
            if (next == VerdictKind.PENDING && AlertVerdicts.isFinal(verdict)) return
            verdictMatch = match
            verdictConfidence = confidence
            verdictReasoning = if (AlertVerdicts.showsRationale(next)) GroundText.rationale(reasoning) else null
        }
    }

    private val entries = LinkedHashMap<String, Entry>()
    private var session = 0
    private var anonymous = 0

    /** A new connection to the ground started: older reports can no longer be called waiting. */
    fun onSessionStarted() {
        session++
    }

    /** Records `match_alert` or `match_alert_reminder`. A repeat of a known alert replaces it in place; an answer is kept. */
    fun onAlert(received: MatchAlert) {
        // Keep only what the rows show, already cut short: a snapshot or a very long text from the ground must not pile up.
        val alert = received.copy(
            snapshot = null,
            type = null,
            time = received.time?.let { GroundText.shorten(it, 40) },
            trackId = received.trackId?.let { GroundText.shortId(it) },
            reasoning = received.reasoning?.let { GroundText.shorten(it) },
            alertId = received.alertId?.let { GroundText.shortId(it) },
        )
        val key = received.alertId?.takeIf { it.isNotEmpty() && it.length <= MAX_ID_CHARS } ?: "n${anonymous++}"
        val existing = entries[key]
        if (existing != null) {
            existing.alert = alert
            existing.session = session
            // A repeat (a reminder after a reconnect) never lowers the level the phone already shows.
            val raised = AlertVerdicts.raise(existing.level, received.alertLevel)
            if (raised != existing.level) existing.level = raised
            existing.setVerdict(received.gemmaMatch, received.gemmaConfidence, received.reasoning)
            existing.raisedByCheck = existing.raisedByCheck ||
                (AlertVerdicts.rank(existing.level) > AlertVerdicts.rank(existing.firstLevel) && AlertVerdicts.isFinal(existing.verdict))
            return
        }
        val entry = Entry(alert, null, false, session)
        entry.setVerdict(received.gemmaMatch, received.gemmaConfidence, received.reasoning)
        entries[key] = entry
        while (entries.size > MAX_ALERTS) entries.remove(entries.keys.first())
    }

    /**
     * Records `reasoning_result`. Only the verdict of an alert this phone already shows is kept: a result
     * without an `alert_id`, with an id longer than [MAX_ID_CHARS], or of another `type` (the laptop's
     * description check) changes nothing.
     */
    fun onReasoningResult(result: ReasoningResult) {
        if (!result.isAlertVerdict) return
        val id = result.alertId?.takeIf { it.length <= MAX_ID_CHARS } ?: return
        val entry = entries[id] ?: return
        entry.setVerdict(result.match, result.confidence, result.reasoning)
    }

    /**
     * Records `alert_upgrade`. The level is raised only when the new level ranks above the one shown
     * ([AlertVerdicts.raise]); an upgrade can never lower it. An upgrade for an alert the phone never saw
     * is ignored.
     */
    fun onAlertUpgrade(upgrade: AlertUpgrade) {
        val id = upgrade.alertId?.takeIf { it.isNotEmpty() && it.length <= MAX_ID_CHARS } ?: return
        val entry = entries[id] ?: return
        val raised = AlertVerdicts.raise(entry.level, upgrade.newLevel)
        if (raised == entry.level) return
        entry.level = raised
        entry.raisedByCheck = true
        if (entry.verdictReasoning == null && entry.verdict == null) {
            entry.verdictReasoning = GroundText.rationale(upgrade.reasoning)
        }
    }

    /**
     * Records `match_alert_resolved`. Only an alert this phone has already shown is marked; an answer for an
     * alert the phone never saw is ignored, as is an event without `ok` true, one without an `alert_id` or with one
     * longer than [MAX_ID_CHARS]. The first answer for an alert wins.
     */
    fun onResolved(resolved: MatchAlertResolved) {
        if (resolved.ok != true) return
        val id = resolved.alertId?.takeIf { it.isNotEmpty() && it.length <= MAX_ID_CHARS } ?: return
        val entry = entries[id] ?: return
        if (entry.answered) return
        entry.answered = true
        entry.answer = resolved.action
    }

    fun clear() {
        entries.clear()
        anonymous = 0
    }

    val size: Int get() = entries.size

    /** Newest first. [stateStale] adds the "Not live" note. */
    fun ui(stateStale: Boolean): GroundAlertsUi {
        if (entries.isEmpty()) return GroundAlertsUi.EMPTY
        val rows = entries.entries.toList().asReversed().map { (key, e) -> row(key, e, stateStale) }
        return GroundAlertsUi(rows, MissionFormat.notLiveNote(stateStale))
    }

    private fun row(key: String, e: Entry, stateStale: Boolean): GroundAlertRow {
        val a = e.alert
        val track = a.trackId?.takeIf { it.isNotEmpty() }?.let { "Track " + GroundText.shortId(it) }
        val time = a.time?.takeIf { it.isNotEmpty() }?.let { GroundText.shorten(it, 40) } ?: "--"
        val verdict = e.verdict
        // With an AI check the verdict lines replace the alert's own text ("Awaiting … reasoning…").
        val reason = if (verdict != null) null else a.reasoning?.takeIf { it.isNotEmpty() }?.let { GroundText.shorten(it) }
        val rationale = if (AlertVerdicts.showsRationale(verdict)) e.verdictReasoning else null
        val status = when {
            e.answered -> when (e.answer) {
                MatchAlertResolved.Action.CONFIRM -> AlertStatus.CONFIRMED
                MatchAlertResolved.Action.DISMISS -> AlertStatus.DISMISSED
                null -> AlertStatus.ANSWERED
            }
            e.session == session && !stateStale -> AlertStatus.WAITING
            else -> AlertStatus.EARLIER
        }
        return GroundAlertRow(
            id = GroundText.shortId(key),
            levelText = level(e.level),
            trackText = track,
            timeText = time,
            scoreText = "Score ${percent(a.score)} (ReID ${percent(a.reidScore)}, face ${percent(a.faceScore)})",
            reasonText = reason,
            status = status,
            statusText = statusText(status),
            isReminder = a.isReminder || a.renotify == true,
            reminderText = if (a.isReminder || a.renotify == true) reminderTag(time) else null,
            verdict = verdict,
            verdictText = verdictText(verdict, e.verdictConfidence),
            verdictIsNote = verdict != null && !AlertVerdicts.showsRationale(verdict),
            rationaleText = rationale?.let { GroundText.shorten(it, MAX_ROW_RATIONALE) },
            detailRationaleText = rationale,
            confidenceText = if (AlertVerdicts.showsRationale(verdict)) e.verdictConfidence?.let { "Confidence: " + GroundText.shorten(it.raw, 40) } else null,
            levelRaisedText = if (e.raisedByCheck && e.level != null) "Raised to ${level(e.level)} by the AI check." else null,
        )
    }

    companion object {
        const val MAX_ALERTS = 100

        /** The longest model reason a collapsed row shows; the expanded detail shows up to [GroundText.MAX_SERVER_TEXT]. */
        const val MAX_ROW_RATIONALE = 140

        const val VERDICT_PENDING = "AI check running…"
        const val VERDICT_MATCH = "AI check: likely the same person"
        const val VERDICT_NO_MATCH = "AI check: likely not the same person"
        const val VERDICT_UNCERTAIN = "AI check: uncertain"
        const val VERDICT_ERROR = "AI check could not run for this alert. The scores above are unchanged."
        const val VERDICT_SKIPPED = "AI check skipped: the alert was answered first."
        const val VERDICT_NOT_RUN = "AI check not run for this alert."

        /** The verdict line of a row, or null without a verdict. Errors and skipped checks read as neutral notes. */
        fun verdictText(kind: VerdictKind?, confidence: ReasoningConfidence?): String? {
            val suffix = when (confidence) {
                ReasoningConfidence.High -> " (high confidence)"
                ReasoningConfidence.Medium -> " (medium confidence)"
                ReasoningConfidence.Low -> " (low confidence)"
                else -> ""
            }
            return when (kind) {
                null -> null
                VerdictKind.PENDING -> VERDICT_PENDING
                VerdictKind.MATCH -> VERDICT_MATCH + suffix
                VerdictKind.NO_MATCH -> VERDICT_NO_MATCH + suffix
                VerdictKind.UNCERTAIN -> VERDICT_UNCERTAIN + suffix
                VerdictKind.ERROR -> VERDICT_ERROR
                VerdictKind.SKIPPED -> VERDICT_SKIPPED
                VerdictKind.NOT_RUN -> VERDICT_NOT_RUN
                VerdictKind.OTHER -> "AI check: " + GroundText.shorten(confidence?.raw, 40)
            }
        }

        /** An alert id longer than this is not used as a key (the iOS app ignores such an answer too). */
        const val MAX_ID_CHARS = 128
        const val NOTE = "Reported by the ground. Answer match alerts on the laptop."

        const val WAITING_TEXT = "Waiting for an answer on the laptop."
        const val CONFIRMED_TEXT = "Confirmed on the laptop."
        const val DISMISSED_TEXT = "Dismissed on the laptop."
        const val ANSWERED_TEXT = "Answered on the laptop."
        const val EARLIER_TEXT = "Reported earlier. The ground has not repeated it in this connection, so the phone cannot say whether it was answered."

        fun statusText(status: AlertStatus): String = when (status) {
            AlertStatus.WAITING -> WAITING_TEXT
            AlertStatus.CONFIRMED -> CONFIRMED_TEXT
            AlertStatus.DISMISSED -> DISMISSED_TEXT
            AlertStatus.ANSWERED -> ANSWERED_TEXT
            AlertStatus.EARLIER -> EARLIER_TEXT
        }

        /** What the ground said when it sent a reminder, and when. It is a statement about the past, not the alert's present state. */
        fun reminderTag(time: String): String =
            "Reminder at $time: the ground said this alert was waiting for the operator."

        fun level(level: AlertLevel?): String = when (level) {
            AlertLevel.ConfirmedMatch -> "Strong match"
            AlertLevel.PossibleMatch -> "Possible match"
            AlertLevel.WeakSignal -> "Weak signal"
            AlertLevel.NoMatch -> "No match"
            is AlertLevel.Unknown -> GroundText.shorten(level.value, 40)
            null -> "Match alert"
        }

        /** A score from 0 to 1 as a percent; "--" for a value that is absent or not a number. */
        fun percent(v: Double?): String {
            if (v == null || !v.isFinite()) return "--"
            return "${Math.round(v.coerceIn(0.0, 1.0) * 100)}%"
        }
    }
}
