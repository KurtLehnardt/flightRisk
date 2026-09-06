package com.flightrisk.app.persistence

import android.content.Context
import android.graphics.Bitmap
import android.util.Log
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

class SessionRepository(context: Context) {

    companion object {
        private const val TAG = "SessionRepository"
    }

    private val dao = FlightRiskDatabase.getInstance(context).sessionDao()
    private val snapshotDir = File(context.filesDir, "snapshots").also { it.mkdirs() }

    private val dateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)

    suspend fun createSession(
        source: String,
        targetPhotoPath: String? = null,
        targetDescription: String? = null,
    ): String {
        val sessionId = UUID.randomUUID().toString()
        val entity = SessionEntity(
            id = sessionId,
            startedAt = dateFormat.format(Date()),
            source = source,
            targetPhotoPath = targetPhotoPath,
            targetDescription = targetDescription,
        )
        dao.insert(entity)
        Log.d(TAG, "Session created: $sessionId")
        return sessionId
    }

    suspend fun endSession(
        sessionId: String,
        totalFrames: Int = 0,
        totalDetections: Int = 0,
        totalMatches: Int = 0,
        recordingPath: String? = null,
    ) {
        dao.endSession(
            sessionId = sessionId,
            endedAt = dateFormat.format(Date()),
            totalFrames = totalFrames,
            totalDetections = totalDetections,
            totalMatches = totalMatches,
            recordingPath = recordingPath,
        )
        Log.d(TAG, "Session ended: $sessionId")
    }

    suspend fun getSession(sessionId: String): SessionEntity? =
        dao.getSession(sessionId)

    suspend fun getRecentSessions(limit: Int = 20): List<SessionEntity> =
        dao.getRecentSessions(limit)

    suspend fun addMatch(
        sessionId: String,
        matchType: String,
        reidScore: Double = 0.0,
        faceScore: Double = 0.0,
        combinedScore: Double = 0.0,
        gemmaMatch: Boolean = false,
        gemmaConfidence: String? = null,
        reasoning: String? = null,
        snapshot: Bitmap? = null,
        crop: Bitmap? = null,
    ): Long {
        val snapshotPath = snapshot?.let { saveBitmap(it, "snap") }
        val cropPath = crop?.let { saveBitmap(it, "crop") }

        val entity = MatchEntity(
            sessionId = sessionId,
            timestamp = dateFormat.format(Date()),
            matchType = matchType,
            reidScore = reidScore,
            faceScore = faceScore,
            combinedScore = combinedScore,
            gemmaMatch = gemmaMatch,
            gemmaConfidence = gemmaConfidence,
            reasoning = reasoning,
            snapshotPath = snapshotPath,
            cropPath = cropPath,
        )
        return dao.insertMatch(entity)
    }

    suspend fun updateMatchReasoning(
        matchId: Long,
        gemmaMatch: Boolean,
        gemmaConfidence: String?,
        reasoning: String?,
    ) {
        dao.updateMatchReasoning(matchId, gemmaMatch, gemmaConfidence, reasoning)
    }

    suspend fun getSessionMatches(sessionId: String): List<MatchEntity> =
        dao.getSessionMatches(sessionId)

    suspend fun addFeedback(
        matchId: Long,
        sessionId: String,
        feedback: String,
        notes: String? = null,
    ) {
        dao.insertFeedback(
            MatchFeedbackEntity(
                matchId = matchId,
                sessionId = sessionId,
                feedback = feedback,
                timestamp = dateFormat.format(Date()),
                notes = notes,
            )
        )
    }

    suspend fun getMatchStats(): Map<String, Any> {
        val total = dao.getTotalMatchCount()
        val byType = dao.getMatchStatsByType().associate { stat ->
            stat.matchType to mapOf(
                "count" to stat.cnt,
                "avgReid" to (stat.avgReid ?: 0.0),
                "avgFace" to (stat.avgFace ?: 0.0),
                "avgCombined" to (stat.avgCombined ?: 0.0),
            )
        }
        return mapOf("totalMatches" to total, "byType" to byType)
    }

    suspend fun getFeedbackStats(): Map<String, Any> {
        val confirmed = dao.getConfirmedCount()
        val rejected = dao.getRejectedCount()
        val total = confirmed + rejected
        val rate = if (total > 0) confirmed.toDouble() / total else 0.0
        return mapOf(
            "totalConfirmed" to confirmed,
            "totalRejected" to rejected,
            "confirmationRate" to rate,
        )
    }

    private fun saveBitmap(bitmap: Bitmap, prefix: String): String {
        val ts = System.currentTimeMillis()
        val file = File(snapshotDir, "${prefix}_$ts.jpg")
        try {
            FileOutputStream(file).use { out ->
                bitmap.compress(Bitmap.CompressFormat.JPEG, 85, out)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to save bitmap: ${e.message}")
        }
        return file.absolutePath
    }
}
