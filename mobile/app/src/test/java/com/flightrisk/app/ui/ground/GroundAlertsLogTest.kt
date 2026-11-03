package com.flightrisk.app.ui.ground

import com.flightrisk.contract.AlertLevel
import com.flightrisk.contract.MatchAlert
import com.flightrisk.contract.MatchAlertResolved
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class GroundAlertsLogTest {

    private fun alert(
        id: String? = "a1",
        reminder: Boolean = false,
        level: AlertLevel? = AlertLevel.PossibleMatch,
        score: Double? = 0.86,
        reasoning: String? = null,
        time: String? = "12:34:56",
        track: String? = "8_7",
    ) = MatchAlert(reminder, id, time, score, 0.0, 0.86, level, "SNAPSHOT", track, "edge", reasoning, if (reminder) true else null, if (reminder) 0 else null)

    private fun resolved(id: String? = "a1", action: MatchAlertResolved.Action? = MatchAlertResolved.Action.DISMISS, ok: Boolean? = true) =
        MatchAlertResolved(id, action, action?.wire, ok, "searching")

    @Test
    fun `a first alert is waiting and shows the ground's own numbers`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        log.onAlert(alert())
        val ui = log.ui(stateStale = false)
        val row = ui.rows.single()
        assertEquals("Possible match", row.levelText)
        assertEquals("Track 8_7", row.trackText)
        assertEquals("12:34:56", row.timeText)
        assertEquals("Score 86% (ReID 86%, face 0%)", row.scoreText)
        assertEquals(AlertStatus.WAITING, row.status)
        assertNull(ui.notLiveNote)
        assertEquals("Possible match, Track 8_7, at 12:34:56, Score 86% (ReID 86%, face 0%), Waiting for an answer on the laptop.", row.accessibilitySummary)
    }

    @Test
    fun `rows are newest first and a repeat replaces the row in place`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        log.onAlert(alert("a1"))
        log.onAlert(alert("a2", time = "13:00:00"))
        log.onAlert(alert("a1", reminder = true))
        val rows = log.ui(false).rows
        assertEquals(listOf("a2", "a1"), rows.map { it.id })
        assertTrue(rows[1].isReminder)
        assertEquals(GroundAlertsLog.reminderTag("12:34:56"), rows[1].reminderText)
        assertFalse(rows[0].isReminder)
    }

    @Test
    fun `an answer marks the row, only ok true is an answer, and the first answer wins`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        log.onAlert(alert("a1"))
        log.onAlert(alert("a2"))
        log.onResolved(resolved("a1", MatchAlertResolved.Action.CONFIRM))
        log.onResolved(resolved("a2", MatchAlertResolved.Action.DISMISS, ok = false))
        log.onResolved(resolved("a2", MatchAlertResolved.Action.DISMISS, ok = null))
        log.onResolved(resolved("a1", MatchAlertResolved.Action.DISMISS))
        val byId = log.ui(false).rows.associateBy { it.id }
        assertEquals(AlertStatus.CONFIRMED, byId.getValue("a1").status)
        assertEquals(GroundAlertsLog.CONFIRMED_TEXT, byId.getValue("a1").statusText)
        assertEquals(AlertStatus.WAITING, byId.getValue("a2").status)
        log.onResolved(resolved("a2", null))
        assertEquals(AlertStatus.ANSWERED, log.ui(false).rows.first { it.id == "a2" }.status)
    }

    @Test
    fun `an answer for an unknown or missing alert id changes nothing`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        log.onAlert(alert("a1"))
        log.onResolved(resolved("zzz"))
        log.onResolved(resolved("x".repeat(129)))
        log.onResolved(resolved(null))
        log.onResolved(resolved(""))
        assertEquals(AlertStatus.WAITING, log.ui(false).rows.single().status)
        assertEquals(1, log.size)
    }

    @Test
    fun `an alert from an earlier session or from a stale state is not called waiting`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        log.onAlert(alert("a1"))
        assertEquals(AlertStatus.EARLIER, log.ui(stateStale = true).rows.single().status)
        assertEquals(MissionFormat.NOT_LIVE_VALUES, log.ui(stateStale = true).notLiveNote)
        log.onSessionStarted()
        assertEquals(AlertStatus.EARLIER, log.ui(stateStale = false).rows.single().status)
        log.onAlert(alert("a1", reminder = true))
        assertEquals(AlertStatus.WAITING, log.ui(stateStale = false).rows.single().status)
        // An answer stays an answer through a new session.
        log.onResolved(resolved("a1"))
        log.onSessionStarted()
        assertEquals(AlertStatus.DISMISSED, log.ui(false).rows.single().status)
    }

    @Test
    fun `at most one hundred alerts are kept, oldest dropped first`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        repeat(150) { log.onAlert(alert("a$it")) }
        assertEquals(100, log.size)
        val ids = log.ui(false).rows.map { it.id }
        assertEquals("a149", ids.first())
        assertEquals("a50", ids.last())
    }

    @Test
    fun `alerts without an id are kept apart`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        log.onAlert(alert(null))
        log.onAlert(alert(null))
        assertEquals(2, log.size)
        assertEquals(2, log.ui(false).rows.map { it.id }.toSet().size)
    }

    @Test
    fun `text and numbers from the ground are made safe`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        log.onAlert(alert("a1", level = AlertLevel.Unknown("x".repeat(500)), score = Double.NaN, reasoning = "r".repeat(5000), time = "t".repeat(500), track = "k".repeat(500)))
        val row = log.ui(false).rows.single()
        assertTrue(row.levelText.length <= 40)
        assertTrue(row.reasonText!!.length <= GroundText.MAX_SERVER_TEXT)
        assertTrue(row.timeText.length <= 40)
        assertTrue(row.trackText!!.length <= "Track ".length + GroundText.MAX_ID_TEXT)
        assertEquals("Score -- (ReID 86%, face 0%)", row.scoreText)
        assertEquals("Match alert", GroundAlertsLog.level(null))
        assertEquals("--", GroundAlertsLog.percent(Double.POSITIVE_INFINITY))
        assertEquals("0%", GroundAlertsLog.percent(-3.0))
        assertEquals("100%", GroundAlertsLog.percent(7.0))
        assertEquals("86%", GroundAlertsLog.percent(0.855))
    }

    @Test
    fun `clear forgets everything`() {
        val log = GroundAlertsLog().apply { onSessionStarted() }
        log.onAlert(alert())
        log.clear()
        assertTrue(log.ui(false).rows.isEmpty())
        assertEquals(0, log.size)
    }
}
