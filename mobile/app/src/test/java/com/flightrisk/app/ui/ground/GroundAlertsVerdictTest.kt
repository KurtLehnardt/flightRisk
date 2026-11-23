package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.app.ground.eventually
import com.flightrisk.contract.AlertLevel
import com.flightrisk.contract.AlertUpgrade
import com.flightrisk.contract.MatchAlert
import com.flightrisk.contract.ReasoningConfidence
import com.flightrisk.contract.ReasoningResult
import com.flightrisk.contract.ServerEvents
import com.flightrisk.contract.VerdictKind
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The AI check verdict on the Ground screen's alert rows (`reasoning_result.json`, `alert_upgrade.json`):
 * the verdict, its confidence and a short, cleaned reason; neutral notes for an error or a skipped
 * check; and a level that only ever goes up.
 */
class GroundAlertsVerdictTest {

    @Suppress("UNCHECKED_CAST")
    private fun <T> fixture(file: String, case: String): T {
        val c = ContractFixtures.case(file, case)
        return ServerEvents.decode((c["event"] as JsonPrimitive).content, c["payload"]) as T
    }

    private val awaiting: MatchAlert = fixture("match_alert.json", "edge_match_awaiting_reasoning")
    private val upgrade: AlertUpgrade = fixture("alert_upgrade.json", "confirmed_match")

    private fun result(case: String): ReasoningResult = fixture("reasoning_result.json", case)

    /** The awaiting alert, re-keyed to the alert a verdict case was produced for. */
    private fun alertFor(r: ReasoningResult) = awaiting.copy(alertId = r.alertId)

    private fun log() = GroundAlertsLog().apply { onSessionStarted() }

    private fun GroundAlertsLog.row() = ui(stateStale = false).rows.single()

    @Test
    fun `an alert awaiting the check says the check is running instead of the awaiting text`() {
        val log = log()
        log.onAlert(awaiting)
        val row = log.row()
        assertEquals(VerdictKind.PENDING, row.verdict)
        assertEquals(GroundAlertsLog.VERDICT_PENDING, row.verdictText)
        assertTrue(row.verdictIsNote)
        assertNull(row.reasonText)
        assertNull(row.rationaleText)
        assertEquals("Possible match", row.levelText)
    }

    @Test
    fun `a match verdict and its upgrade show the verdict, the reason and the raised level`() {
        val log = log()
        log.onAlert(awaiting)
        val match = result("match")
        log.onReasoningResult(match)
        log.onAlertUpgrade(upgrade)
        val row = log.row()
        assertEquals(VerdictKind.MATCH, row.verdict)
        assertEquals("AI check: likely the same person (high confidence)", row.verdictText)
        assertFalse(row.verdictIsNote)
        assertEquals(match.reasoning, row.rationaleText)
        assertEquals(match.reasoning, row.detailRationaleText)
        assertEquals("Confidence: high", row.confidenceText)
        assertEquals("Strong match", row.levelText)
        assertEquals("Raised to Strong match by the AI check.", row.levelRaisedText)
        assertTrue(row.hasDetail)
        assertTrue(row.accessibilitySummary.contains("AI check: likely the same person"))
        assertEquals(AlertStatus.WAITING, row.status)
    }

    @Test
    fun `no match and uncertain verdicts read as such and keep the level`() {
        for ((case, text) in listOf(
            "no_match" to "AI check: likely not the same person (high confidence)",
            "uncertain" to "AI check: uncertain (low confidence)",
        )) {
            val log = log()
            val r = result(case)
            log.onAlert(alertFor(r))
            log.onReasoningResult(r)
            val row = log.row()
            assertEquals(case, text, row.verdictText)
            assertEquals(case, r.reasoning, row.rationaleText)
            assertEquals(case, "Possible match", row.levelText)
            assertNull(case, row.levelRaisedText)
        }
    }

    @Test
    fun `an error or a skipped check is a neutral note and never shows the ground's text`() {
        for ((case, text) in listOf(
            "error" to GroundAlertsLog.VERDICT_ERROR,
            "error_with_detail" to GroundAlertsLog.VERDICT_ERROR,
            "skipped" to GroundAlertsLog.VERDICT_SKIPPED,
        )) {
            val log = log()
            val r = result(case)
            log.onAlert(alertFor(r))
            log.onReasoningResult(r)
            val row = log.row()
            assertEquals(case, text, row.verdictText)
            assertTrue(case, row.verdictIsNote)
            assertNull(case, row.rationaleText)
            assertNull(case, row.detailRationaleText)
            assertNull(case, row.confidenceText)
            assertNull(case, row.reasonText)
            assertFalse(case, row.toString().contains("declined"))
            assertFalse(case, row.verdictText!!.contains("rror:"))
        }
    }

    @Test
    fun `the level never goes down`() {
        val log = log()
        log.onAlert(awaiting.copy(alertLevel = AlertLevel.ConfirmedMatch))
        log.onAlertUpgrade(upgrade.copy(newLevel = AlertLevel.PossibleMatch))
        assertEquals("Strong match", log.row().levelText)
        // A reminder that still carries a lower level does not lower it either.
        log.onAlert(awaiting.copy(isReminder = true, renotify = true, alertLevel = AlertLevel.WeakSignal))
        assertEquals("Strong match", log.row().levelText)
        assertNull(log.row().levelRaisedText)
        // An unknown level ranks lowest: it never replaces a known one.
        log.onAlertUpgrade(upgrade.copy(newLevel = AlertLevel.Unknown("certain")))
        assertEquals("Strong match", log.row().levelText)
    }

    @Test
    fun `a late reminder still saying pending does not undo a verdict`() {
        val log = log()
        log.onAlert(awaiting)
        log.onReasoningResult(result("match"))
        log.onAlert(awaiting.copy(isReminder = true, renotify = true))
        val row = log.row()
        assertEquals(VerdictKind.MATCH, row.verdict)
        assertEquals(result("match").reasoning, row.rationaleText)
    }

    @Test
    fun `a reminder after a reconnect brings the verdict with it`() {
        val log = log()
        log.onAlert(fixture("match_alert.json", "reminder_with_verdict"))
        val row = log.row()
        assertEquals(VerdictKind.MATCH, row.verdict)
        assertEquals("Strong match", row.levelText)
        assertEquals(result("match").reasoning, row.rationaleText)
    }

    @Test
    fun `results for other alerts, description checks and overlong ids change nothing`() {
        val log = log()
        log.onAlert(awaiting)
        val before = log.row()
        log.onReasoningResult(result("no_match")) // queued for another alert
        log.onReasoningResult(result("description_check"))
        log.onReasoningResult(result("match").copy(alertId = "x".repeat(GroundAlertsLog.MAX_ID_CHARS + 1)))
        log.onReasoningResult(result("match").copy(type = "description"))
        log.onAlertUpgrade(upgrade.copy(alertId = "someone-else"))
        log.onAlertUpgrade(upgrade.copy(alertId = null))
        assertEquals(before, log.row())
        assertEquals(1, log.size)
    }

    @Test
    fun `a hostile reason is cut, cleaned and carries no key`() {
        val log = log()
        log.onAlert(awaiting)
        val hostile = "Same jacket‮ evil ⁦x\nline2 sk-ant-api03-SECRETSECRET Bearer abc.def " + "y".repeat(5_000)
        log.onReasoningResult(result("match").copy(reasoning = hostile))
        val row = log.row()
        val all = listOf(row.rationaleText!!, row.detailRationaleText!!)
        assertTrue(row.rationaleText!!.length <= GroundAlertsLog.MAX_ROW_RATIONALE)
        assertTrue(row.detailRationaleText!!.length <= GroundText.MAX_SERVER_TEXT)
        for (text in all) {
            assertFalse(text.contains('‮'))
            assertFalse(text.contains('⁦'))
            assertFalse(text.contains('\n'))
            assertFalse(text.contains("SECRETSECRET"))
            assertFalse(text.contains("abc.def"))
            assertTrue(text.startsWith("Same jacket"))
        }
    }

    @Test
    fun `an unknown confidence word is shown by name`() {
        val log = log()
        log.onAlert(awaiting)
        log.onReasoningResult(result("match").copy(confidence = ReasoningConfidence.of("fairly sure")))
        val row = log.row()
        assertEquals(VerdictKind.OTHER, row.verdict)
        assertEquals("AI check: fairly sure", row.verdictText)
    }

    @Test
    fun `the verdict reaches the alert row through the real client and nothing is answered`() = harnessTest {
        val c = connectWithFleet()
        c.fixture("match_alert.json", "edge_match_awaiting_reasoning")
        eventually("alert shown") { ui.alerts.rows.isNotEmpty() }
        assertEquals(VerdictKind.PENDING, ui.alerts.rows.single().verdict)
        c.fixture("reasoning_result.json", "description_check")
        c.fixture("reasoning_result.json", "match")
        c.fixture("alert_upgrade.json", "confirmed_match")
        eventually("verdict shown") { ui.alerts.rows.single().verdict == VerdictKind.MATCH && ui.alerts.rows.single().levelText == "Strong match" }
        assertEquals("Raised to Strong match by the AI check.", ui.alerts.rows.single().levelRaisedText)
        val sent = c.eventFrames().joinToString("\n")
        assertFalse(sent.contains("match_alert_response"))
    }
}
