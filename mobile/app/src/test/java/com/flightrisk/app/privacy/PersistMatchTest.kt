package com.flightrisk.app.privacy

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PersistMatchTest {
    private class RecordingSink : MatchSink<String> {
        val snapshots = mutableListOf<String?>()
        override suspend fun addMatch(
            sessionId: String,
            matchType: String,
            reidScore: Double,
            faceScore: Double,
            combinedScore: Double,
            snapshot: String?,
        ): Long {
            snapshots.add(snapshot)
            return snapshots.size.toLong()
        }
    }

    @Test fun noPictureIsPassedForSavingWhenRecordingIsOff() = runBlocking {
        val sink = RecordingSink()
        persistMatch(sink, false, "s", "full", 0.5, 0.5, 0.5, "crop")
        assertEquals(1, sink.snapshots.size)
        assertNull(sink.snapshots[0])
    }

    @Test fun pictureIsPassedForSavingWhenRecordingIsOn() = runBlocking {
        val sink = RecordingSink()
        persistMatch(sink, true, "s", "full", 0.5, 0.5, 0.5, "crop")
        assertEquals(listOf<String?>("crop"), sink.snapshots)
    }

    @Test fun matchIsStillSavedWithoutPictureWhenRecordingIsOff() = runBlocking {
        val sink = RecordingSink()
        val id = persistMatch(sink, false, "s", "full", 0.5, 0.5, 0.5, null)
        assertEquals(1L, id)
    }
}
