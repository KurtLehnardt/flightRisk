package com.flightrisk.app.privacy

import java.io.File
import java.nio.file.Files
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class SavedDataEraserTest {
    private lateinit var root: File
    private lateinit var recordings: File
    private lateinit var snapshots: File

    private class FakeRows(var sessions: Int) : SavedRows {
        var deleteCalls = 0
        override suspend fun deleteAllRows(): Int {
            deleteCalls++
            val n = sessions
            sessions = 0
            return n
        }
    }

    @Before fun setUp() {
        root = Files.createTempDirectory("fr-erase").toFile()
        recordings = File(root, "recordings").apply { mkdirs() }
        snapshots = File(root, "snapshots").apply { mkdirs() }
    }

    @After fun tearDown() {
        root.deleteRecursively()
    }

    @Test fun deletesRowsAndAllFilesButKeepsTheFolders() = runBlocking {
        File(recordings, "session_1.mp4").writeText("v")
        File(recordings, "session_2.mp4").writeText("v")
        File(snapshots, "snap_1.jpg").writeText("i")
        File(snapshots, "crop_1.jpg").writeText("i")
        File(snapshots, "nested").apply { mkdirs() }.let { File(it, "x.jpg").writeText("i") }
        val rows = FakeRows(3)

        val result = SavedDataEraser(rows, listOf(recordings, snapshots)).eraseAll()

        assertEquals(3, result.rows)
        assertEquals(5, result.files)
        assertEquals(0, result.failedFiles)
        assertEquals(1, rows.deleteCalls)
        assertTrue(recordings.isDirectory)
        assertTrue(snapshots.isDirectory)
        assertEquals(0, recordings.listFiles()!!.size)
        assertEquals(0, snapshots.listFiles()!!.size)
    }

    @Test fun doesNotTouchFilesOutsideTheListedFolders() = runBlocking {
        val other = File(root, "keep.txt").apply { writeText("keep") }
        File(recordings, "a.mp4").writeText("v")
        SavedDataEraser(FakeRows(0), listOf(recordings, snapshots)).eraseAll()
        assertTrue(other.exists())
    }

    @Test fun missingFolderIsNotAnError() = runBlocking {
        val result = SavedDataEraser(FakeRows(1), listOf(File(root, "nope"))).eraseAll()
        assertEquals(1, result.rows)
        assertEquals(0, result.files)
        assertEquals(0, result.failedFiles)
    }

    @Test fun emptyStoreGivesZeroResult() = runBlocking {
        val result = SavedDataEraser(FakeRows(0), listOf(recordings, snapshots)).eraseAll()
        assertEquals(SavedDataEraser.Result(0, 0, 0), result)
    }

    @Test fun daoDeletesCoverEveryTableOfTheSessionData() {
        val dao = File("src/main/java/com/flightrisk/app/persistence/SessionDao.kt").readText()
        assertTrue(dao.contains("DELETE FROM match_feedback"))
        assertTrue(dao.contains("DELETE FROM matches"))
        assertTrue(dao.contains("DELETE FROM sessions"))
        val repo = File("src/main/java/com/flightrisk/app/persistence/SessionRepository.kt").readText()
        assertTrue(repo.contains("withTransaction"))
    }
}
