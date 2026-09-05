package com.flightrisk.app.vision

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * Stores target embeddings and photo for session persistence.
 *
 * Keeps the ReID and face embeddings in memory for fast access,
 * and serializes them to files in the app's internal storage for
 * crash recovery. Thread-safe via [ReentrantLock].
 */
class TargetStore(private val context: Context) {

    private val lock = ReentrantLock()

    /** ReID (CLIP) embedding for the target person. */
    var reidEmbedding: FloatArray? = null
        get() = lock.withLock { field }
        private set

    /** Face (ArcFace) embedding for the target person. */
    var faceEmbedding: FloatArray? = null
        get() = lock.withLock { field }
        private set

    /** Reference photo of the target person. */
    var targetBitmap: Bitmap? = null
        get() = lock.withLock { field }
        private set

    companion object {
        private const val TAG = "TargetStore"
        private const val REID_FILE = "target_reid_embedding.bin"
        private const val FACE_FILE = "target_face_embedding.bin"
        private const val BITMAP_FILE = "target_photo.png"
    }

    /**
     * Store a ReID embedding.
     *
     * @param embedding The normalized CLIP feature vector.
     * @param persist If true (default), also write to disk for crash recovery.
     */
    fun setReidEmbedding(embedding: FloatArray, persist: Boolean = true) {
        lock.withLock {
            reidEmbedding = embedding.clone()
        }
        if (persist) saveEmbedding(REID_FILE, embedding)
    }

    /**
     * Store a face embedding.
     *
     * @param embedding The normalized ArcFace feature vector.
     * @param persist If true (default), also write to disk for crash recovery.
     */
    fun setFaceEmbedding(embedding: FloatArray, persist: Boolean = true) {
        lock.withLock {
            faceEmbedding = embedding.clone()
        }
        if (persist) saveEmbedding(FACE_FILE, embedding)
    }

    /**
     * Store the target reference photo.
     *
     * @param bitmap The target person's photo.
     * @param persist If true (default), also write to disk for crash recovery.
     */
    fun setTargetBitmap(bitmap: Bitmap, persist: Boolean = true) {
        lock.withLock {
            targetBitmap = bitmap.copy(bitmap.config, false)
        }
        if (persist) saveBitmap(bitmap)
    }

    /**
     * Clear all stored data (both in memory and on disk).
     */
    fun clear() {
        lock.withLock {
            reidEmbedding = null
            faceEmbedding = null
            targetBitmap = null
        }
        deleteFile(REID_FILE)
        deleteFile(FACE_FILE)
        deleteFile(BITMAP_FILE)
    }

    /** Whether any target data is currently stored. */
    val hasTarget: Boolean
        get() = lock.withLock {
            reidEmbedding != null || faceEmbedding != null
        }

    /**
     * Restore persisted target data from disk.
     *
     * Call this on app startup to recover the target after a crash
     * or process death.
     *
     * @return true if any data was restored.
     */
    fun restore(): Boolean {
        var restored = false
        lock.withLock {
            loadEmbedding(REID_FILE)?.let {
                reidEmbedding = it
                restored = true
            }
            loadEmbedding(FACE_FILE)?.let {
                faceEmbedding = it
                restored = true
            }
            loadBitmap()?.let {
                targetBitmap = it
                restored = true
            }
        }
        if (restored) {
            Log.d(TAG, "Restored target data from disk")
        }
        return restored
    }

    // -- Serialization helpers --

    private fun saveEmbedding(filename: String, embedding: FloatArray) {
        try {
            val file = File(context.filesDir, filename)
            DataOutputStream(FileOutputStream(file)).use { out ->
                out.writeInt(embedding.size)
                for (v in embedding) out.writeFloat(v)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to persist embedding to $filename", e)
        }
    }

    private fun loadEmbedding(filename: String): FloatArray? {
        val file = File(context.filesDir, filename)
        if (!file.exists()) return null
        return try {
            DataInputStream(FileInputStream(file)).use { input ->
                val size = input.readInt()
                FloatArray(size) { input.readFloat() }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to load embedding from $filename", e)
            null
        }
    }

    private fun saveBitmap(bitmap: Bitmap) {
        try {
            val file = File(context.filesDir, BITMAP_FILE)
            FileOutputStream(file).use { out ->
                bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to persist target bitmap", e)
        }
    }

    private fun loadBitmap(): Bitmap? {
        val file = File(context.filesDir, BITMAP_FILE)
        if (!file.exists()) return null
        return try {
            BitmapFactory.decodeFile(file.absolutePath)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to load target bitmap", e)
            null
        }
    }

    private fun deleteFile(filename: String) {
        try {
            File(context.filesDir, filename).delete()
        } catch (e: Exception) {
            Log.w(TAG, "Failed to delete $filename", e)
        }
    }
}
