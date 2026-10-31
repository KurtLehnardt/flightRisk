package com.flightrisk.app.privacy

import java.io.File

/** The saved session rows (sessions, matches and match feedback). */
interface SavedRows {
    /** Deletes every row and returns how many sessions were removed. */
    suspend fun deleteAllRows(): Int
}

/**
 * Deletes everything FlightRisk saved about searches: the database rows and
 * the files in the recordings and snapshots folders. The folders themselves
 * are kept.
 */
class SavedDataEraser(
    private val rows: SavedRows,
    private val directories: List<File>,
) {
    data class Result(val rows: Int, val files: Int, val failedFiles: Int)

    suspend fun eraseAll(): Result {
        val sessions = rows.deleteAllRows()
        var deleted = 0
        var failed = 0
        for (dir in directories) {
            val (d, f) = deleteContents(dir)
            deleted += d
            failed += f
        }
        return Result(sessions, deleted, failed)
    }

    companion object {
        /** Deletes every file below [dir], keeping [dir]. Returns (deleted, failed). */
        fun deleteContents(dir: File): Pair<Int, Int> {
            var deleted = 0
            var failed = 0
            val children = dir.listFiles() ?: return 0 to 0
            for (child in children) {
                if (child.isDirectory) {
                    val (d, f) = deleteContents(child)
                    deleted += d
                    failed += f
                    if (!child.delete()) failed++
                } else if (child.delete()) {
                    deleted++
                } else {
                    failed++
                }
            }
            return deleted to failed
        }
    }
}
