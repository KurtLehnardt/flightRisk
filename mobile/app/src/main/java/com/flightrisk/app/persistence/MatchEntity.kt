package com.flightrisk.app.persistence

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
    tableName = "matches",
    foreignKeys = [
        ForeignKey(
            entity = SessionEntity::class,
            parentColumns = ["id"],
            childColumns = ["sessionId"],
            onDelete = ForeignKey.CASCADE,
        ),
    ],
    indices = [Index("sessionId")],
)
data class MatchEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val sessionId: String,
    val timestamp: String,
    val matchType: String,
    val reidScore: Double = 0.0,
    val faceScore: Double = 0.0,
    val combinedScore: Double = 0.0,
    val gemmaMatch: Boolean = false,
    val gemmaConfidence: String? = null,
    val reasoning: String? = null,
    val snapshotPath: String? = null,
    val cropPath: String? = null,
)
