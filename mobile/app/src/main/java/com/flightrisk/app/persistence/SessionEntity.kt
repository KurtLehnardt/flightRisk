package com.flightrisk.app.persistence

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "sessions")
data class SessionEntity(
    @PrimaryKey val id: String,
    val startedAt: String,
    val endedAt: String? = null,
    val source: String? = null,
    val targetPhotoPath: String? = null,
    val targetDescription: String? = null,
    val totalFrames: Int = 0,
    val totalDetections: Int = 0,
    val totalMatches: Int = 0,
    val recordingPath: String? = null,
)
