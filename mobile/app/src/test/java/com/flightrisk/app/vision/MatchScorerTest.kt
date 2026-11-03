package com.flightrisk.app.vision

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.app.llm.ReasoningResult
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlin.math.withSign
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for [MatchScorer].
 *
 * Validates weight redistribution, alert level classification,
 * numeric parity with the Python reference, and built-in signal
 * collision prevention.
 */
class MatchScorerTest {

    // -- Weight redistribution --

    @Test
    fun `score with all three signals uses declared weights`() {
        val scorer = MatchScorer(
            reidWeight = 0.35f,
            faceWeight = 0.40f,
            reasoningWeight = 0.25f,
        )
        val result = scorer.score(
            reidScore = 0.8f,
            faceScore = 0.7f,
            reasoningResult = ReasoningResult(isMatch = true, confidence = "high", reasoning = ""),
        )
        assertEquals(3, result.signalsUsed)
        // reid 0.8*0.35 + face 0.7*0.40 + reasoning 0.90*0.25 = 0.28+0.28+0.225 = 0.785
        assertEquals(0.785f, result.combinedScore, 0.01f)
        assertTrue(result.isMatch)
    }

    @Test
    fun `score redistributes weights when reasoning is null`() {
        val scorer = MatchScorer(
            reidWeight = 0.35f,
            faceWeight = 0.40f,
            reasoningWeight = 0.25f,
        )
        val result = scorer.score(
            reidScore = 0.8f,
            faceScore = 0.7f,
            reasoningResult = null,
        )
        assertEquals(2, result.signalsUsed)
        // With redistribution: reid_w = 0.35/(0.35+0.40) = 0.4667
        // face_w = 0.40/(0.35+0.40) = 0.5333
        // combined = 0.8*0.4667 + 0.7*0.5333 = 0.3733 + 0.3733 = 0.7466
        assertEquals(0.746f, result.combinedScore, 0.01f)
    }

    @Test
    fun `score with only reid signal redistributes all weight`() {
        val scorer = MatchScorer()
        val result = scorer.score(reidScore = 0.6f, faceScore = 0.0f)
        assertEquals(1, result.signalsUsed)
        // All weight goes to reid: 0.6 * 1.0 = 0.6
        assertEquals(0.6f, result.combinedScore, 0.01f)
    }

    @Test
    fun `score with no positive signals returns zero`() {
        val scorer = MatchScorer()
        val result = scorer.score(reidScore = 0.0f, faceScore = 0.0f)
        assertEquals(0, result.signalsUsed)
        assertEquals(0.0f, result.combinedScore, 0.001f)
        assertFalse(result.isMatch)
        assertEquals("none", result.confidenceLevel)
    }

    @Test
    fun `reasoning no-match is excluded as positive signal`() {
        val scorer = MatchScorer()
        // LLM says "no match" with high confidence -> reasoning_score = 0.10
        // But since isMatch=false, reasoning is not included as a positive signal
        val result = scorer.score(
            reidScore = 0.7f,
            faceScore = 0.6f,
            reasoningResult = ReasoningResult(isMatch = false, confidence = "high", reasoning = ""),
        )
        // reasoning excluded because isMatch=false
        assertEquals(2, result.signalsUsed)
    }

    // -- Alert level classification --

    @Test
    fun `alertLevel returns confirmed_match for high score and multiple signals`() {
        val scorer = MatchScorer()
        val result = ScoredResult(
            combinedScore = 0.75f,
            isMatch = true,
            confidenceLevel = "high",
            signalsUsed = 3,
        )
        assertEquals("confirmed_match", scorer.alertLevel(result))
    }

    @Test
    fun `alertLevel returns possible_match for medium confidence above threshold`() {
        val scorer = MatchScorer(matchThreshold = 0.45f)
        val result = ScoredResult(
            combinedScore = 0.50f,
            isMatch = true,
            confidenceLevel = "medium",
            signalsUsed = 1,
        )
        assertEquals("possible_match", scorer.alertLevel(result))
    }

    @Test
    fun `alertLevel returns weak_signal for score above half threshold`() {
        val scorer = MatchScorer(matchThreshold = 0.45f)
        val result = ScoredResult(
            combinedScore = 0.25f,
            isMatch = false,
            confidenceLevel = "low",
            signalsUsed = 1,
        )
        assertEquals("weak_signal", scorer.alertLevel(result))
    }

    @Test
    fun `alertLevel returns no_match for very low score`() {
        val scorer = MatchScorer(matchThreshold = 0.45f)
        val result = ScoredResult(
            combinedScore = 0.10f,
            isMatch = false,
            confidenceLevel = "low",
            signalsUsed = 1,
        )
        assertEquals("no_match", scorer.alertLevel(result))
    }

    // -- Numeric parity with Python scorer (10 test cases) --

    @Test
    fun `parity case 1 - high reid only`() {
        val scorer = MatchScorer()
        val result = scorer.score(reidScore = 0.9f)
        assertEquals(0.9f, result.combinedScore, 0.01f)
        assertTrue(result.isMatch)
    }

    @Test
    fun `parity case 2 - high face only`() {
        val scorer = MatchScorer()
        val result = scorer.score(faceScore = 0.85f)
        assertEquals(0.85f, result.combinedScore, 0.01f)
        assertTrue(result.isMatch)
    }

    @Test
    fun `parity case 3 - low reid and face`() {
        val scorer = MatchScorer()
        val result = scorer.score(reidScore = 0.2f, faceScore = 0.1f)
        // Redistributed: reid_w=0.4667, face_w=0.5333
        // combined = 0.2*0.4667 + 0.1*0.5333 = 0.0933 + 0.0533 = 0.1467
        assertEquals(0.146f, result.combinedScore, 0.01f)
        assertFalse(result.isMatch)
    }

    @Test
    fun `parity case 4 - reid face and high-match reasoning`() {
        val scorer = MatchScorer()
        val result = scorer.score(
            reidScore = 0.7f,
            faceScore = 0.8f,
            reasoningResult = ReasoningResult(isMatch = true, confidence = "high", reasoning = ""),
        )
        // 0.7*0.35 + 0.8*0.40 + 0.90*0.25 = 0.245+0.32+0.225 = 0.79
        assertEquals(0.79f, result.combinedScore, 0.01f)
    }

    @Test
    fun `parity case 5 - reid face and medium-match reasoning`() {
        val scorer = MatchScorer()
        val result = scorer.score(
            reidScore = 0.6f,
            faceScore = 0.5f,
            reasoningResult = ReasoningResult(isMatch = true, confidence = "medium", reasoning = ""),
        )
        // 0.6*0.35 + 0.5*0.40 + 0.65*0.25 = 0.21+0.20+0.1625 = 0.5725
        assertEquals(0.572f, result.combinedScore, 0.01f)
    }

    @Test
    fun `parity case 6 - reid face and low-match reasoning`() {
        val scorer = MatchScorer()
        val result = scorer.score(
            reidScore = 0.5f,
            faceScore = 0.4f,
            reasoningResult = ReasoningResult(isMatch = true, confidence = "low", reasoning = ""),
        )
        // 0.5*0.35 + 0.4*0.40 + 0.40*0.25 = 0.175+0.16+0.10 = 0.435
        assertEquals(0.435f, result.combinedScore, 0.01f)
    }

    @Test
    fun `parity case 7 - threshold boundary just below`() {
        val scorer = MatchScorer(matchThreshold = 0.45f)
        val result = scorer.score(reidScore = 0.44f)
        assertFalse(result.isMatch)
    }

    @Test
    fun `parity case 8 - threshold boundary at threshold`() {
        val scorer = MatchScorer(matchThreshold = 0.45f)
        val result = scorer.score(reidScore = 0.45f)
        assertTrue(result.isMatch)
    }

    @Test
    fun `parity case 9 - custom weights`() {
        val scorer = MatchScorer(
            reidWeight = 0.5f,
            faceWeight = 0.3f,
            reasoningWeight = 0.2f,
        )
        val result = scorer.score(reidScore = 0.8f, faceScore = 0.6f)
        // Redistributed: reid_w=0.5/0.8=0.625, face_w=0.3/0.8=0.375
        // combined = 0.8*0.625 + 0.6*0.375 = 0.5+0.225 = 0.725
        assertEquals(0.725f, result.combinedScore, 0.01f)
    }

    @Test
    fun `parity case 10 - extra signal registered`() {
        val scorer = MatchScorer()
        scorer.registerSignal("thermal", 0.15f)
        val result = scorer.score(
            reidScore = 0.7f,
            faceScore = 0.6f,
            extraSignals = mapOf("thermal" to 0.8f),
        )
        // total weight = 0.35+0.40+0.15 = 0.90
        // reid: 0.7*(0.35/0.90) = 0.7*0.3889 = 0.2722
        // face: 0.6*(0.40/0.90) = 0.6*0.4444 = 0.2667
        // thermal: 0.8*(0.15/0.90) = 0.8*0.1667 = 0.1333
        // combined = 0.6722
        assertEquals(0.672f, result.combinedScore, 0.01f)
    }

    // -- BUILTIN_SIGNALS collision prevention --

    @Test(expected = IllegalArgumentException::class)
    fun `registerSignal rejects reid name`() {
        val scorer = MatchScorer()
        scorer.registerSignal("reid", 0.5f)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `registerSignal rejects face name`() {
        val scorer = MatchScorer()
        scorer.registerSignal("face", 0.5f)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `registerSignal rejects reasoning name`() {
        val scorer = MatchScorer()
        scorer.registerSignal("reasoning", 0.5f)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `extraSignals rejects built-in name override`() {
        val scorer = MatchScorer()
        scorer.score(
            reidScore = 0.5f,
            extraSignals = mapOf("reid" to 0.9f),
        )
    }

    @Test(expected = IllegalArgumentException::class)
    fun `extraSignals rejects unregistered signal`() {
        val scorer = MatchScorer()
        scorer.score(
            reidScore = 0.5f,
            extraSignals = mapOf("unknown" to 0.5f),
        )
    }

    // -- Reasoning-to-score conversion --

    @Test
    fun `reasoningToScore returns correct values for match`() {
        val scorer = MatchScorer()
        assertEquals(0.90f, scorer.reasoningToScore(
            ReasoningResult(isMatch = true, confidence = "high", reasoning = "")
        ), 0.001f)
        assertEquals(0.65f, scorer.reasoningToScore(
            ReasoningResult(isMatch = true, confidence = "medium", reasoning = "")
        ), 0.001f)
        assertEquals(0.40f, scorer.reasoningToScore(
            ReasoningResult(isMatch = true, confidence = "low", reasoning = "")
        ), 0.001f)
        assertEquals(0.50f, scorer.reasoningToScore(
            ReasoningResult(isMatch = true, confidence = "unknown", reasoning = "")
        ), 0.001f)
    }

    @Test
    fun `reasoningToScore returns dampened values for no-match`() {
        val scorer = MatchScorer()
        assertEquals(0.10f, scorer.reasoningToScore(
            ReasoningResult(isMatch = false, confidence = "high", reasoning = "")
        ), 0.001f)
        assertEquals(0.20f, scorer.reasoningToScore(
            ReasoningResult(isMatch = false, confidence = "medium", reasoning = "")
        ), 0.001f)
        assertEquals(0.30f, scorer.reasoningToScore(
            ReasoningResult(isMatch = false, confidence = "low", reasoning = "")
        ), 0.001f)
    }

    @Test
    fun `reasoningToScore returns zero for null`() {
        val scorer = MatchScorer()
        assertEquals(0.0f, scorer.reasoningToScore(null), 0.001f)
    }

    // -- Edge cases --

    @Test
    fun `score with only face signal redistributes all weight`() {
        val scorer = MatchScorer()
        val result = scorer.score(reidScore = 0.0f, faceScore = 0.75f)
        assertEquals(1, result.signalsUsed)
        assertEquals(0.75f, result.combinedScore, 0.01f)
    }

    @Test
    fun `alertLevel boundary - score exactly at 0_65 with 2 signals is confirmed`() {
        val scorer = MatchScorer()
        val result = ScoredResult(
            combinedScore = 0.65f,
            isMatch = true,
            confidenceLevel = "high",
            signalsUsed = 2,
        )
        assertEquals("confirmed_match", scorer.alertLevel(result))
    }

    @Test
    fun `alertLevel boundary - score 0_65 with 1 signal is not confirmed`() {
        val scorer = MatchScorer()
        val result = ScoredResult(
            combinedScore = 0.65f,
            isMatch = true,
            confidenceLevel = "high",
            signalsUsed = 1,
        )
        assertEquals("possible_match", scorer.alertLevel(result))
    }

    @Test
    fun `alertLevel with medium confidence below threshold is weak_signal`() {
        val scorer = MatchScorer(matchThreshold = 0.45f)
        val result = ScoredResult(
            combinedScore = 0.36f,
            isMatch = false,
            confidenceLevel = "medium",
            signalsUsed = 2,
        )
        // 0.36 < 0.45 threshold, so not possible_match; 0.36 >= 0.225 (half threshold)
        assertEquals("weak_signal", scorer.alertLevel(result))
    }

    @Test
    fun `score all signals at 1_0 gives maximum combined`() {
        val scorer = MatchScorer()
        val result = scorer.score(
            reidScore = 1.0f,
            faceScore = 1.0f,
            reasoningResult = ReasoningResult(isMatch = true, confidence = "high", reasoning = ""),
        )
        // 1.0*0.35 + 1.0*0.40 + 0.90*0.25 = 0.975
        assertEquals(0.975f, result.combinedScore, 0.01f)
        assertEquals("high", result.confidenceLevel)
    }

    @Test
    fun `multiple extra signals combine correctly`() {
        val scorer = MatchScorer()
        scorer.registerSignal("thermal", 0.10f)
        scorer.registerSignal("gait", 0.10f)
        val result = scorer.score(
            reidScore = 0.6f,
            faceScore = 0.5f,
            extraSignals = mapOf("thermal" to 0.9f, "gait" to 0.7f),
        )
        assertEquals(4, result.signalsUsed)
        assertTrue(result.combinedScore > 0.5f)
    }

    /**
     * Documents that SearchPipeline.frameLoop bypasses MatchScorer
     * and uses inline weighted averaging. This test captures what
     * the pipeline ACTUALLY computes vs what MatchScorer would compute,
     * demonstrating the divergence.
     */
    @Test
    fun `pipeline inline scoring diverges from MatchScorer`() {
        val reidWeight = 0.35f
        val faceWeight = 0.40f
        val reidScore = 0.7f
        val faceScore = 0.6f

        // Pipeline inline logic (SearchPipeline.kt:422-424):
        val totalWeight = reidWeight + faceWeight
        val pipelineScore = (reidScore * reidWeight + faceScore * faceWeight) / totalWeight

        // MatchScorer logic:
        val scorer = MatchScorer(reidWeight = reidWeight, faceWeight = faceWeight)
        val scorerResult = scorer.score(reidScore = reidScore, faceScore = faceScore)

        // They should match for the 2-signal case
        assertEquals(
            "Pipeline and MatchScorer should agree on 2-signal scoring",
            pipelineScore, scorerResult.combinedScore, 0.01f,
        )

        // But pipeline cannot incorporate reasoning - it hard-codes
        // totalWeight = reidWeight + faceWeight, never including reasoning
        val withReasoning = scorer.score(
            reidScore = reidScore,
            faceScore = faceScore,
            reasoningResult = ReasoningResult(isMatch = true, confidence = "high", reasoning = ""),
        )
        assertTrue(
            "MatchScorer with reasoning gives different score than pipeline inline",
            withReasoning.combinedScore != pipelineScore,
        )
    }

    // -- Rounding parity with the Python scorer (shared vectors) --

    private fun number(element: kotlinx.serialization.json.JsonElement?): Double {
        val primitive = element as JsonPrimitive
        return if (primitive.isString) {
            when (primitive.content) {
                "nan" -> Double.NaN
                "inf" -> Double.POSITIVE_INFINITY
                "-inf" -> Double.NEGATIVE_INFINITY
                else -> error("unexpected special value ${primitive.content}")
            }
        } else {
            primitive.doubleOrNull ?: error("not a number: $primitive")
        }
    }

    /** A weight the Float based constructor reads back as exactly the decimal the vector wrote. */
    private fun weight(element: kotlinx.serialization.json.JsonElement?): Float {
        val d = number(element)
        val f = d.toFloat()
        assertEquals("the vector weight $d must survive the Float API", d, f.toString().toDouble(), 0.0)
        return f
    }

    @Test
    fun `every shared vector gives exactly the Python score`() {
        val cases = ContractFixtures.cases("match_score_vectors.json")
        assertTrue("expected the full vector set", cases.size >= 80)
        var negativeZero = 0
        for ((name, case) in cases) {
            val payload = case["payload"] as JsonObject
            val config = payload["scorer"] as JsonObject
            val scorer = MatchScorer(
                reidWeight = weight(config["reid_weight"]),
                faceWeight = weight(config["face_weight"]),
                reasoningWeight = weight(config["reasoning_weight"]),
                matchThreshold = weight(config["match_threshold"]),
            )
            for ((extra, w) in payload["extra_signal_weights"] as JsonObject) scorer.registerSignal(extra, weight(w))
            val signals = (payload["signals"] as JsonObject).mapValues { number(it.value) }
            val result = scorer.scoreExact(
                reidScore = signals["reid"] ?: 0.0,
                faceScore = signals["face"] ?: 0.0,
                extraSignals = signals - "reid" - "face",
            )
            val expected = payload["expected"] as JsonObject
            val want = number(expected["combined_score"])
            val got = result.combinedScoreExact
            if (want.isNaN()) {
                assertTrue("$name: expected NaN", got.isNaN())
            } else {
                // Exact equality on purpose, and the sign bit too: -0.0 is not 0.0 for Python's round().
                assertEquals("$name: combined score", want, got, 0.0)
                assertEquals("$name: sign of the combined score", 1.0.withSign(want), 1.0.withSign(got), 0.0)
                if (want == 0.0 && 1.0.withSign(want) < 0) negativeZero++
            }
            assertEquals("$name: is_match", (expected["is_match"] as JsonPrimitive).booleanOrNull, result.isMatch)
            assertEquals("$name: confidence", (expected["confidence_level"] as JsonPrimitive).content, result.confidenceLevel)
            assertEquals("$name: signals", (expected["signals_used"] as JsonPrimitive).intOrNull, result.signalsUsed)
        }
        assertTrue("the vectors must include a negative zero result", negativeZero > 0)
    }

    @Test
    fun `rounding is half to even on the exact binary value not truncation`() {
        val scorer = MatchScorer()
        assertEquals(0.062, scorer.scoreExact(reidScore = 0.0625).combinedScoreExact, 0.0)
        assertEquals(0.188, scorer.scoreExact(reidScore = 0.1875).combinedScoreExact, 0.0)
        assertEquals(0.001, scorer.scoreExact(reidScore = 0.0005).combinedScoreExact, 0.0)
        // 0.8571 truncated is 0.857 and rounded is 0.857; 0.6666 truncated is 0.666 and rounded is 0.667.
        assertEquals(0.667, scorer.scoreExact(reidScore = 0.6666).combinedScoreExact, 0.0)
    }

    @Test
    fun `non finite scores follow Python`() {
        val scorer = MatchScorer()
        assertEquals(0.0f, scorer.scoreExact(reidScore = Double.NaN).combinedScore, 0f)
        assertEquals("none", scorer.scoreExact(reidScore = Double.NaN).confidenceLevel)
        assertTrue(scorer.scoreExact(reidScore = Double.POSITIVE_INFINITY).combinedScoreExact.isInfinite())
    }
}
