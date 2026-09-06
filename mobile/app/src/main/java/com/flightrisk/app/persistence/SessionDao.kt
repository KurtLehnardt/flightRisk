package com.flightrisk.app.persistence

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query

@Dao
interface SessionDao {

    @Insert
    suspend fun insert(session: SessionEntity)

    @Query(
        """UPDATE sessions SET endedAt = :endedAt, totalFrames = :totalFrames,
           totalDetections = :totalDetections, totalMatches = :totalMatches,
           recordingPath = :recordingPath WHERE id = :sessionId"""
    )
    suspend fun endSession(
        sessionId: String,
        endedAt: String,
        totalFrames: Int,
        totalDetections: Int,
        totalMatches: Int,
        recordingPath: String?,
    )

    @Query("SELECT * FROM sessions WHERE id = :sessionId")
    suspend fun getSession(sessionId: String): SessionEntity?

    @Query("SELECT * FROM sessions ORDER BY startedAt DESC LIMIT :limit")
    suspend fun getRecentSessions(limit: Int = 20): List<SessionEntity>

    @Insert
    suspend fun insertMatch(match: MatchEntity): Long

    @Query(
        """UPDATE matches SET gemmaMatch = :gemmaMatch, gemmaConfidence = :gemmaConfidence,
           reasoning = :reasoning WHERE id = :matchId"""
    )
    suspend fun updateMatchReasoning(
        matchId: Long,
        gemmaMatch: Boolean,
        gemmaConfidence: String?,
        reasoning: String?,
    )

    @Query("SELECT * FROM matches WHERE sessionId = :sessionId ORDER BY timestamp ASC")
    suspend fun getSessionMatches(sessionId: String): List<MatchEntity>

    @Insert
    suspend fun insertFeedback(feedback: MatchFeedbackEntity)

    @Query("SELECT COUNT(*) FROM matches")
    suspend fun getTotalMatchCount(): Int

    @Query(
        """SELECT matchType, COUNT(*) as cnt, AVG(reidScore) as avgReid,
           AVG(faceScore) as avgFace, AVG(combinedScore) as avgCombined
           FROM matches GROUP BY matchType"""
    )
    suspend fun getMatchStatsByType(): List<MatchTypeStats>

    @Query("SELECT COUNT(*) FROM match_feedback WHERE feedback = 'confirmed'")
    suspend fun getConfirmedCount(): Int

    @Query("SELECT COUNT(*) FROM match_feedback WHERE feedback = 'rejected'")
    suspend fun getRejectedCount(): Int
}

data class MatchTypeStats(
    val matchType: String,
    val cnt: Int,
    val avgReid: Double?,
    val avgFace: Double?,
    val avgCombined: Double?,
)
