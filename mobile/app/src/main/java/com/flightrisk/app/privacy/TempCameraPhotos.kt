package com.flightrisk.app.privacy

import java.io.File

/**
 * The temporary file a camera capture for the target photo is written to. It
 * holds a picture of a child, so it is deleted as soon as it has been decoded
 * and any left over from a crash are removed at app start.
 */
object TempCameraPhotos {
    const val PREFIX = "flightrisk_target_"
    const val SUFFIX = ".jpg"

    fun create(cacheDir: File): File = File.createTempFile(PREFIX, SUFFIX, cacheDir)

    fun isTempPhoto(file: File): Boolean =
        file.isFile && file.name.startsWith(PREFIX) && file.name.endsWith(SUFFIX)

    /** Deletes one capture file. Returns true if it is gone afterwards. */
    fun delete(file: File?): Boolean {
        if (file == null) return true
        return !file.exists() || file.delete()
    }

    /** Deletes every leftover capture file in [cacheDir] and returns how many. */
    fun sweep(cacheDir: File): Int {
        val files = cacheDir.listFiles() ?: return 0
        return files.count { isTempPhoto(it) && it.delete() }
    }
}
