package com.flightrisk.app

import android.graphics.Bitmap
import android.graphics.Color
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.MediumTest
import androidx.test.filters.SmallTest
import com.flightrisk.app.alert.AlertManager
import com.flightrisk.app.config.FlightRiskConfig
import com.flightrisk.app.config.SensitivityPreset
import com.flightrisk.app.config.VisionConfig
import com.flightrisk.app.vision.Detection
import com.flightrisk.app.vision.DetectionTracker
import com.flightrisk.app.vision.MatchScorer
import com.flightrisk.app.vision.ReasoningResult
import com.flightrisk.app.vision.ScoredResult
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * End-to-end instrumented tests for the FlightRisk vision pipeline.
 *
 * These tests validate the pipeline logic without ONNX models. The ONNX
 * inference layer is mocked -- we supply synthetic scores and verify that
 * the pipeline components (MatchScorer, DetectionTracker, AlertManager,
 * Config presets) compose correctly to produce the right match decisions,
 * alert levels, and temporal corroboration behavior.
 *
 * Real test images would be placed in `androidTest/assets/` and loaded
 * via [android.content.res.AssetManager] when ONNX models are available.
 */
@RunWith(AndroidJUnit4::class)
class PipelineE2ETest {

    // -- Helpers --

    /** Create a solid-color bitmap (stand-in for a real person crop). */
    private fun fakeBitmap(
        width: Int = 100,
        height: Int = 200,
        color: Int = Color.RED,
    ): Bitmap {
        val bmp = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        bmp.eraseColor(color)
        return bmp
    }

    /** Build a [Detection] with a fake crop and the given bbox/confidence. */
    private fun fakeDetection(
        bbox: IntArray = intArrayOf(10, 20, 110, 220),
        confidence: Float = 0.9f,
        color: Int = Color.RED,
    ): Detection {
        return Detection(
            bbox = bbox,
            confidence = confidence,
            crop = fakeBitmap(
                width = bbox[2] - bbox[0],
                height = bbox[3] - bbox[1],
                color = color,
            ),
        )
    }

    // Shared scorer (default weights)
    private lateinit var scorer: MatchScorer
    private lateinit var tracker: DetectionTracker

    @Before
    fun setUp() {
        scorer = MatchScorer(
            reidWeight = 0.35f,
            faceWeight = 0.40f,
            reasoningWeight = 0.25f,
            matchThreshold = 0.45f,
        )
        tracker = DetectionTracker(
            iouThreshold = 0.3f,
            maxMissing = 15,
            scoreWindow = 8,
        )
    }

    @After
    fun tearDown() {
        tracker.clear()
    }

    // ================================================================
    // Test 1: Matching pair — high ReID + face scores produce a match
    // ================================================================

    @SmallTest
    @Test
    fun matchingPair_producesPositiveMatch() {
        // Simulate high ReID + face similarity (as if ONNX models returned these)
        val reidScore = 0.75f
        val faceScore = 0.80f

        val result = scorer.score(reidScore = reidScore, faceScore = faceScore)

        assertTrue("Should be a match", result.isMatch)
        assertTrue(
            "Combined score should exceed match threshold (0.45)",
            result.combinedScore > 0.45f,
        )

        val alert = scorer.alertLevel(result)
        assertTrue(
            "Alert should be confirmed_match or possible_match",
            alert == "confirmed_match" || alert == "possible_match",
        )
    }

    // ================================================================
    // Test 2: Non-matching pair — low scores produce no match
    // ================================================================

    @SmallTest
    @Test
    fun nonMatchingPair_producesNoMatch() {
        // Simulate low ReID + face similarity
        val reidScore = 0.15f
        val faceScore = 0.10f

        val result = scorer.score(reidScore = reidScore, faceScore = faceScore)

        assertFalse("Should not be a match", result.isMatch)
        assertTrue(
            "Combined score should be below match threshold",
            result.combinedScore < 0.45f,
        )

        val alert = scorer.alertLevel(result)
        assertTrue(
            "Alert should be no_match or weak_signal",
            alert == "no_match" || alert == "weak_signal",
        )
    }

    // ================================================================
    // Test 3: No persons in frame — empty detections
    // ================================================================

    @SmallTest
    @Test
    fun noPersonsInFrame_producesEmptyDetections() {
        // Simulate PersonDetector returning empty list (no persons found)
        val detections = emptyList<Detection>()

        assertTrue("Detections should be empty", detections.isEmpty())

        // Pipeline should not attempt scoring with empty detections
        // DetectionTracker should return empty when given no detections
        val tracked = tracker.update(detections)
        assertTrue("Tracked list should be empty for no detections", tracked.isEmpty())
    }

    // ================================================================
    // Test 4: Multiple persons, one match — correct index identified
    // ================================================================

    @MediumTest
    @Test
    fun multiplePersons_matchesCorrectIndex() {
        // Three detections with different simulated scores
        val scores = listOf(
            Pair(0.20f, 0.15f),  // person 0: low match
            Pair(0.78f, 0.82f),  // person 1: high match (target)
            Pair(0.10f, 0.05f),  // person 2: no match
        )

        // Find the best match (simulating what ReID.findMatch does)
        var bestIdx: Int? = null
        var bestCombined = 0f

        for ((i, pair) in scores.withIndex()) {
            val result = scorer.score(reidScore = pair.first, faceScore = pair.second)
            if (result.combinedScore > bestCombined) {
                bestCombined = result.combinedScore
                bestIdx = i
            }
        }

        assertEquals("Best match should be person at index 1", 1, bestIdx)

        val bestResult = scorer.score(
            reidScore = scores[1].first,
            faceScore = scores[1].second,
        )
        assertTrue("Best person should be a match", bestResult.isMatch)

        // Verify non-matching persons are not matches
        val person0 = scorer.score(
            reidScore = scores[0].first,
            faceScore = scores[0].second,
        )
        assertFalse("Person 0 should not be a match", person0.isMatch)

        val person2 = scorer.score(
            reidScore = scores[2].first,
            faceScore = scores[2].second,
        )
        assertFalse("Person 2 should not be a match", person2.isMatch)
    }

    // ================================================================
    // Test 5: MatchScorer weight redistribution
    // ================================================================

    @SmallTest
    @Test
    fun scorer_redistributesWeights_reidOnly() {
        // Score with only ReID (face=0, no reasoning)
        val result = scorer.score(reidScore = 0.7f, faceScore = 0.0f)

        assertEquals("Only 1 signal should be used", 1, result.signalsUsed)
        // With weight redistribution, all weight goes to ReID
        // normalized weight = 0.35/0.35 = 1.0, so score = 0.7 * 1.0 = 0.7
        assertEquals(0.7f, result.combinedScore, 0.01f)
        assertTrue("0.7 should exceed threshold 0.45", result.isMatch)
    }

    @SmallTest
    @Test
    fun scorer_redistributesWeights_reidPlusFace_noReasoning() {
        // Score with ReID + face (no reasoning)
        val result = scorer.score(reidScore = 0.6f, faceScore = 0.5f)

        assertEquals("2 signals should be used", 2, result.signalsUsed)
        // reid weight redistributed: 0.35/(0.35+0.40) = 0.4667
        // face weight redistributed: 0.40/(0.35+0.40) = 0.5333
        // combined = 0.6*0.4667 + 0.5*0.5333 = 0.2800 + 0.2666 = 0.5466
        assertEquals(0.546f, result.combinedScore, 0.01f)
        assertTrue("Combined 0.546 should exceed threshold 0.45", result.isMatch)

        // Verify reasoning weight was redistributed (not lost)
        // total active weight should normalize to 1.0 implicitly
    }

    @SmallTest
    @Test
    fun scorer_redistributesWeights_allThreeSignals() {
        val reasoning = ReasoningResult(
            isMatch = true,
            confidence = "high",
            reasoning = "Clothing matches description",
        )
        val result = scorer.score(
            reidScore = 0.6f,
            faceScore = 0.5f,
            reasoningResult = reasoning,
        )

        assertEquals("3 signals should be used", 3, result.signalsUsed)
        // No redistribution needed -- all three active
        // reid: 0.6*0.35 + face: 0.5*0.40 + reasoning: 0.90*0.25
        // = 0.21 + 0.20 + 0.225 = 0.635
        assertEquals(0.635f, result.combinedScore, 0.01f)
    }

    // ================================================================
    // Test 6: DetectionTracker temporal corroboration
    // ================================================================

    @MediumTest
    @Test
    fun tracker_createsTrackAndAccumulatesScores() {
        // Frame 1: new detection
        val det1 = fakeDetection(bbox = intArrayOf(100, 100, 200, 300), confidence = 0.85f)
        val tracked1 = tracker.update(listOf(det1))
        assertEquals("Should have 1 tracked detection", 1, tracked1.size)
        val trackId = tracked1[0].trackId

        // Add a ReID score for frame 1
        tracker.addScores(trackId, reidScore = 0.65f)

        // Frame 2: same position (high IoU) -> matched to same track
        val det2 = fakeDetection(bbox = intArrayOf(105, 102, 205, 302), confidence = 0.88f)
        val tracked2 = tracker.update(listOf(det2))
        assertEquals("Should still have 1 tracked detection", 1, tracked2.size)
        assertEquals("Track ID should be stable", trackId, tracked2[0].trackId)

        tracker.addScores(trackId, reidScore = 0.70f)

        // Frame 3: slightly shifted (still overlapping)
        val det3 = fakeDetection(bbox = intArrayOf(108, 105, 208, 305), confidence = 0.90f)
        val tracked3 = tracker.update(listOf(det3))
        assertEquals("Should still have 1 tracked detection", 1, tracked3.size)
        assertEquals("Track ID should be stable", trackId, tracked3[0].trackId)

        tracker.addScores(trackId, reidScore = 0.72f)

        // Verify track summary
        val summary = tracker.getTrack(trackId)
        assertNotNull("Track summary should exist", summary)
        summary!!

        assertEquals("Should have 3 ReID scores", 3, summary.reidScores.size)

        val expectedAvg = (0.65f + 0.70f + 0.72f) / 3f
        assertEquals(
            "Average ReID score should match",
            expectedAvg,
            summary.avgReidScore,
            0.01f,
        )
    }

    @MediumTest
    @Test
    fun tracker_removesExpiredTracks() {
        val det = fakeDetection(bbox = intArrayOf(50, 50, 150, 250))
        val tracked = tracker.update(listOf(det))
        val trackId = tracked[0].trackId

        // Simulate maxMissing+1 frames with no matching detection
        for (i in 0..15) {
            tracker.update(emptyList())
        }

        val summary = tracker.getTrack(trackId)
        assertNull("Track should be expired and removed", summary)
    }

    // ================================================================
    // Test 7: AlertManager tiering
    // ================================================================

    @MediumTest
    @Test
    fun alertManager_confirmedMatch_allActions() {
        val context = androidx.test.platform.app.InstrumentationRegistry
            .getInstrumentation().targetContext

        val alertManager = AlertManager(context, cooldownMs = 10_000L)

        try {
            val actions = alertManager.fireAlert(
                AlertManager.CONFIRMED_MATCH,
                "track_1",
            )
            assertNotNull("Confirmed match should fire", actions)
            actions!!
            assertTrue("Audio should be active", actions.audio)
            assertTrue("Haptic should be active", actions.haptic)
            assertTrue("Visual should be active", actions.visual)
        } finally {
            alertManager.release()
        }
    }

    @MediumTest
    @Test
    fun alertManager_possibleMatch_hapticAndVisualOnly() {
        val context = androidx.test.platform.app.InstrumentationRegistry
            .getInstrumentation().targetContext

        val alertManager = AlertManager(context, cooldownMs = 10_000L)

        try {
            val actions = alertManager.fireAlert(
                AlertManager.POSSIBLE_MATCH,
                "track_2",
            )
            assertNotNull("Possible match should fire", actions)
            actions!!
            assertFalse("Audio should NOT be active", actions.audio)
            assertTrue("Haptic should be active", actions.haptic)
            assertTrue("Visual should be active", actions.visual)
        } finally {
            alertManager.release()
        }
    }

    @SmallTest
    @Test
    fun alertManager_weakSignal_visualOnly() {
        val context = androidx.test.platform.app.InstrumentationRegistry
            .getInstrumentation().targetContext

        val alertManager = AlertManager(context, cooldownMs = 10_000L)

        try {
            val actions = alertManager.fireAlert(
                AlertManager.WEAK_SIGNAL,
                "track_3",
            )
            assertNotNull("Weak signal should fire", actions)
            actions!!
            assertFalse("Audio should NOT be active", actions.audio)
            assertFalse("Haptic should NOT be active", actions.haptic)
            assertTrue("Visual should be active", actions.visual)
        } finally {
            alertManager.release()
        }
    }

    @SmallTest
    @Test
    fun alertManager_noMatch_doesNotFire() {
        val context = androidx.test.platform.app.InstrumentationRegistry
            .getInstrumentation().targetContext

        val alertManager = AlertManager(context, cooldownMs = 10_000L)

        try {
            val actions = alertManager.fireAlert(AlertManager.NO_MATCH, "track_4")
            assertNull("no_match should not fire any alert", actions)
        } finally {
            alertManager.release()
        }
    }

    @MediumTest
    @Test
    fun alertManager_cooldownPreventsDuplicateAlert() {
        val context = androidx.test.platform.app.InstrumentationRegistry
            .getInstrumentation().targetContext

        // Use a long cooldown to ensure the second fire is within it
        val alertManager = AlertManager(context, cooldownMs = 10_000L)

        try {
            val first = alertManager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_5")
            assertNotNull("First alert should fire", first)

            // Immediately fire again -- should be suppressed by cooldown
            val second = alertManager.fireAlert(AlertManager.CONFIRMED_MATCH, "track_5")
            assertNull("Second alert within cooldown should be suppressed", second)

            // Verify cooldown status
            assertTrue(
                "Track should be within cooldown",
                alertManager.isWithinCooldown("track_5"),
            )
        } finally {
            alertManager.release()
        }
    }

    // ================================================================
    // Test 8: Config sensitivity presets
    // ================================================================

    @SmallTest
    @Test
    fun config_moreAlertsPreset_lowersThresholds() {
        val config = FlightRiskConfig.fromPreset(SensitivityPreset.MORE_ALERTS)

        assertEquals(0.40, config.vision.reidThreshold, 0.001)
        assertEquals(0.35, config.vision.scorerMatchThreshold, 0.001)
        assertEquals(0.30, config.vision.faceMatchThreshold, 0.001)
    }

    @SmallTest
    @Test
    fun config_balancedPreset_matchesDefaults() {
        val config = FlightRiskConfig.fromPreset(SensitivityPreset.BALANCED)
        val defaults = FlightRiskConfig()

        assertEquals(
            "BALANCED reidThreshold should match default",
            defaults.vision.reidThreshold,
            config.vision.reidThreshold,
            0.001,
        )
        assertEquals(
            "BALANCED scorerMatchThreshold should match default",
            defaults.vision.scorerMatchThreshold,
            config.vision.scorerMatchThreshold,
            0.001,
        )
        assertEquals(
            "BALANCED faceMatchThreshold should match default",
            defaults.vision.faceMatchThreshold,
            config.vision.faceMatchThreshold,
            0.001,
        )
    }

    @SmallTest
    @Test
    fun config_fewerAlertsPreset_raisesThresholds() {
        val config = FlightRiskConfig.fromPreset(SensitivityPreset.FEWER_ALERTS)

        assertEquals(0.70, config.vision.reidThreshold, 0.001)
        assertEquals(0.60, config.vision.scorerMatchThreshold, 0.001)
        assertEquals(0.55, config.vision.faceMatchThreshold, 0.001)
    }

    @SmallTest
    @Test
    fun config_presetOrdering_moreAlerts_lowerThan_balanced_lowerThan_fewerAlerts() {
        val more = FlightRiskConfig.fromPreset(SensitivityPreset.MORE_ALERTS)
        val balanced = FlightRiskConfig.fromPreset(SensitivityPreset.BALANCED)
        val fewer = FlightRiskConfig.fromPreset(SensitivityPreset.FEWER_ALERTS)

        assertTrue(
            "MORE_ALERTS thresholds should be lower than BALANCED",
            more.vision.scorerMatchThreshold < balanced.vision.scorerMatchThreshold,
        )
        assertTrue(
            "BALANCED thresholds should be lower than FEWER_ALERTS",
            balanced.vision.scorerMatchThreshold < fewer.vision.scorerMatchThreshold,
        )
    }
}
