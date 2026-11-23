package com.flightrisk.contract

import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The AI check events (`reasoning_result.json`, `alert_upgrade.json`) and the verdict fields of
 * `match_alert` (docs/mobile-contract.md, "Events a phone must handle").
 */
class ReasoningVerdictTest {

    private fun result(case: String) = Fixtures.eventAs<ReasoningResult>("reasoning_result.json", case)

    private val awaiting = Fixtures.eventAs<MatchAlert>("match_alert.json", "edge_match_awaiting_reasoning")

    @Test
    fun theAlertAwaitingReasoningSaysSo() {
        assertEquals(ReasoningConfidence.Pending, awaiting.gemmaConfidence)
        assertNull(awaiting.gemmaMatch)
        assertEquals(AlertLevel.PossibleMatch, awaiting.alertLevel)
        assertEquals(VerdictKind.PENDING, AlertVerdicts.kindOf(awaiting.gemmaMatch, awaiting.gemmaConfidence))
    }

    @Test
    fun aMatchVerdictBelongsToTheAlertItWasQueuedFor() {
        val r = result("match")
        assertTrue(r.isAlertVerdict)
        assertEquals(awaiting.alertId, r.alertId)
        assertEquals("analyze", r.type)
        assertEquals(true, r.match)
        assertEquals(ReasoningConfidence.High, r.confidence)
        assertEquals(VerdictKind.MATCH, r.verdict)
        assertTrue(r.reasoning!!.isNotEmpty())
        assertTrue(AlertVerdicts.showsRationale(r.verdict))
    }

    @Test
    fun everyVerdictCaseMapsToItsKind() {
        val expected = mapOf(
            "match" to VerdictKind.MATCH,
            "no_match" to VerdictKind.NO_MATCH,
            "uncertain" to VerdictKind.UNCERTAIN,
            "error" to VerdictKind.ERROR,
            "error_with_detail" to VerdictKind.ERROR,
            "skipped" to VerdictKind.SKIPPED,
        )
        for ((case, kind) in expected) {
            val r = result(case)
            assertTrue(case, r.isAlertVerdict)
            assertEquals(case, kind, r.verdict)
        }
        // Error and skipped results are notes: their text is never shown as the model's reason.
        assertFalse(AlertVerdicts.showsRationale(result("error_with_detail").verdict))
        assertFalse(AlertVerdicts.showsRationale(result("skipped").verdict))
    }

    @Test
    fun aDescriptionCheckIsNotAnAlertVerdict() {
        val r = result("description_check")
        assertFalse(r.isAlertVerdict)
        assertNull(r.alertId)
        assertEquals("description", r.type)
    }

    @Test
    fun theUpgradeRaisesTheSameAlertToConfirmed() {
        val u = Fixtures.eventAs<AlertUpgrade>("alert_upgrade.json", "confirmed_match")
        assertEquals(awaiting.alertId, u.alertId)
        assertEquals(AlertLevel.ConfirmedMatch, u.newLevel)
        assertEquals(result("match").reasoning, u.reasoning)
    }

    @Test
    fun aReminderAfterTheCheckCarriesTheVerdict() {
        val r = Fixtures.eventAs<MatchAlert>("match_alert.json", "reminder_with_verdict")
        assertTrue(r.isReminder)
        assertEquals(awaiting.alertId, r.alertId)
        assertEquals(AlertLevel.ConfirmedMatch, r.alertLevel)
        assertEquals(true, r.gemmaMatch)
        assertEquals(ReasoningConfidence.High, r.gemmaConfidence)
        assertEquals(VerdictKind.MATCH, AlertVerdicts.kindOf(r.gemmaMatch, r.gemmaConfidence))
    }

    @Test
    fun aLevelNeverGoesDown() {
        val levels = listOf(null, AlertLevel.NoMatch, AlertLevel.WeakSignal, AlertLevel.PossibleMatch, AlertLevel.ConfirmedMatch, AlertLevel.Unknown("x"))
        for (current in levels) for (proposed in levels) {
            val shown = AlertVerdicts.raise(current, proposed)
            assertTrue("$current then $proposed gave $shown", AlertVerdicts.rank(shown) >= AlertVerdicts.rank(current))
            if (AlertVerdicts.rank(proposed) <= AlertVerdicts.rank(current)) assertEquals(current, shown)
        }
        assertEquals(AlertLevel.ConfirmedMatch, AlertVerdicts.raise(AlertLevel.PossibleMatch, AlertLevel.ConfirmedMatch))
        assertEquals(AlertLevel.ConfirmedMatch, AlertVerdicts.raise(AlertLevel.ConfirmedMatch, AlertLevel.PossibleMatch))
    }

    @Test
    fun confidenceWordsAreReadAndUnknownWordsKeptByName() {
        assertEquals(VerdictKind.NO_MATCH, AlertVerdicts.kindOf(false, ReasoningConfidence.of("medium")))
        assertEquals(VerdictKind.UNCERTAIN, AlertVerdicts.kindOf(true, ReasoningConfidence.of("unknown")))
        assertEquals(VerdictKind.UNCERTAIN, AlertVerdicts.kindOf(null, ReasoningConfidence.High))
        assertEquals(VerdictKind.NOT_RUN, AlertVerdicts.kindOf(false, ReasoningConfidence.of("off")))
        assertEquals(VerdictKind.NOT_RUN, AlertVerdicts.kindOf(false, ReasoningConfidence.of("unavailable")))
        assertEquals(VerdictKind.OTHER, AlertVerdicts.kindOf(true, ReasoningConfidence.of("very sure")))
        assertEquals("very sure", ReasoningConfidence.of("very sure").raw)
        assertNull(AlertVerdicts.kindOf(true, null))
        assertFalse(AlertVerdicts.isFinal(VerdictKind.PENDING))
        assertTrue(AlertVerdicts.isFinal(VerdictKind.ERROR))
    }

    @Test
    fun wrongTypesReadAsNotReported() {
        val r = ServerEvents.decode("reasoning_result", """{"alert_id":7,"type":"analyze","result":"yes"}""") as ReasoningResult
        assertEquals("7", r.alertId)
        assertNull(r.match)
        assertNull(r.confidence)
        assertNull(r.verdict)
        val u = ServerEvents.decode("alert_upgrade", """{"alert_id":"a","new_level":3}""") as AlertUpgrade
        assertNull(u.newLevel)
        assertTrue(ServerEvents.decode("reasoning_result", JsonPrimitive("x")) is ServerEvent.Malformed)
        assertTrue(ServerEvents.decode("alert_upgrade", "[[[") is ServerEvent.Malformed)
        assertTrue("reasoning_result" in ServerEvents.HANDLED && "alert_upgrade" in ServerEvents.HANDLED)
    }
}
