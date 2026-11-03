package com.flightrisk.app.ui.ground

import com.flightrisk.contract.AlertLevel
import com.flightrisk.contract.MatchAlert
import com.flightrisk.contract.MatchAlertResolved

/**
 * Match alerts the ground reports, read only. The phone shows what the ground found and what the
 * operator answered; the operator answers on the laptop. `match_alert_response` is not a command the
 * phone can send (docs/mobile-contract.md), so there is nothing here that sends, confirms or dismisses.
 *
 * The ground sends `match_alert` when it finds a match, `match_alert_reminder` while the alert waits
 * for the operator, and `match_alert_resolved` once the operator confirmed or dismissed it.
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
) {
    val accessibilitySummary: String
        get() = buildList {
            add(levelText)
            trackText?.let(::add)
            add("at $timeText")
            add(scoreText)
            reasonText?.let(::add)
            add(statusText)
            reminderText?.let(::add)
        }.joinToString(", ")
}

/** What the alert card draws. */
data class GroundAlertsUi(val rows: List<GroundAlertRow>, val notLiveNote: String?) {
    companion object {
        val EMPTY = GroundAlertsUi(emptyList(), null)
    }
}

/** The alerts the ground reported, newest last, at most [MAX_ALERTS]. Not thread safe: use it from one thread. */
class GroundAlertsLog {
    private class Entry(var alert: MatchAlert, var answer: MatchAlertResolved.Action?, var answered: Boolean, var session: Int)

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
            return
        }
        entries[key] = Entry(alert, null, false, session)
        while (entries.size > MAX_ALERTS) entries.remove(entries.keys.first())
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
        val reason = a.reasoning?.takeIf { it.isNotEmpty() }?.let { GroundText.shorten(it) }
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
            levelText = level(a.alertLevel),
            trackText = track,
            timeText = time,
            scoreText = "Score ${percent(a.score)} (ReID ${percent(a.reidScore)}, face ${percent(a.faceScore)})",
            reasonText = reason,
            status = status,
            statusText = statusText(status),
            isReminder = a.isReminder || a.renotify == true,
            reminderText = if (a.isReminder || a.renotify == true) reminderTag(time) else null,
        )
    }

    companion object {
        const val MAX_ALERTS = 100

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
