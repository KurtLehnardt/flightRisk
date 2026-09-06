package com.flightrisk.app.observability

import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import kotlin.math.roundToInt

class MetricsCollector {

    private val lock = ReentrantLock()

    private var startedAt: Long = System.currentTimeMillis()
    private var framesProcessed: Int = 0
    private var personsDetected: Int = 0
    private val matches = mutableMapOf("reid" to 0, "face" to 0, "description" to 0)
    private var scoreMin: Double = Double.MAX_VALUE
    private var scoreMax: Double = 0.0
    private var scoreSum: Double = 0.0
    private var scoreCount: Int = 0
    private var facesChecked: Int = 0
    private var facesFound: Int = 0
    private var reasoningCalls: Int = 0
    private var reasoningLatencyMs: Double = 0.0
    private var pipelineErrors: Int = 0

    fun reset() = lock.withLock {
        startedAt = System.currentTimeMillis()
        framesProcessed = 0
        personsDetected = 0
        matches.replaceAll { _, _ -> 0 }
        scoreMin = Double.MAX_VALUE
        scoreMax = 0.0
        scoreSum = 0.0
        scoreCount = 0
        facesChecked = 0
        facesFound = 0
        reasoningCalls = 0
        reasoningLatencyMs = 0.0
        pipelineErrors = 0
    }

    fun incFrames(n: Int = 1) = lock.withLock { framesProcessed += n }

    fun incPersons(n: Int) = lock.withLock { personsDetected += n }

    fun recordMatch(matchType: String, score: Double) = lock.withLock {
        matches[matchType] = (matches[matchType] ?: 0) + 1
        scoreCount++
        scoreSum += score
        if (score < scoreMin) scoreMin = score
        if (score > scoreMax) scoreMax = score
    }

    fun recordFaceCheck(found: Boolean) = lock.withLock {
        facesChecked++
        if (found) facesFound++
    }

    fun recordReasoning(durationMs: Double) = lock.withLock {
        reasoningCalls++
        reasoningLatencyMs += durationMs
    }

    fun recordError() = lock.withLock { pipelineErrors++ }

    fun snapshot(): Map<String, Any> = lock.withLock {
        val elapsed = (System.currentTimeMillis() - startedAt) / 1000.0
        val avgScore = if (scoreCount > 0) scoreSum / scoreCount else 0.0
        val faceRate = if (facesChecked > 0) facesFound.toDouble() / facesChecked * 100 else 0.0
        val avgReasoningMs = if (reasoningCalls > 0) reasoningLatencyMs / reasoningCalls else 0.0

        mapOf(
            "sessionElapsedS" to (elapsed * 10).roundToInt() / 10.0,
            "framesProcessed" to framesProcessed,
            "personsDetected" to personsDetected,
            "matches" to matches.toMap(),
            "matchesTotal" to matches.values.sum(),
            "scoreDistribution" to mapOf(
                "min" to if (scoreCount > 0) scoreMin else 0.0,
                "max" to scoreMax,
                "avg" to avgScore,
                "count" to scoreCount,
            ),
            "faceDetection" to mapOf(
                "checked" to facesChecked,
                "found" to facesFound,
                "ratePct" to (faceRate * 10).roundToInt() / 10.0,
            ),
            "reasoning" to mapOf(
                "calls" to reasoningCalls,
                "totalLatencyMs" to reasoningLatencyMs,
                "avgLatencyMs" to avgReasoningMs,
            ),
            "pipelineErrors" to pipelineErrors,
        )
    }
}
