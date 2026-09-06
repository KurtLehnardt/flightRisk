package com.flightrisk.app.canon

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import android.util.Log
import com.flightrisk.app.persistence.FlightRiskDatabase
import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.PrimaryKey
import androidx.room.Query
import java.io.ByteArrayOutputStream

@Entity(tableName = "target_versions")
data class TargetVersionEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val timestamp: Long = System.currentTimeMillis(),
    val operatorId: String = "default",
    val qualityScore: Double? = null,
    val imageB64: String,
    val isActive: Boolean = false,
)

@Dao
interface TargetVersionDao {
    @Insert
    suspend fun insert(version: TargetVersionEntity): Long

    @Query("UPDATE target_versions SET isActive = 0 WHERE isActive = 1")
    suspend fun deactivateAll()

    @Query("UPDATE target_versions SET isActive = 1 WHERE id = :versionId")
    suspend fun activate(versionId: Long)

    @Query("SELECT * FROM target_versions WHERE isActive = 1 LIMIT 1")
    suspend fun getActive(): TargetVersionEntity?

    @Query("SELECT id, timestamp, operatorId, qualityScore, isActive FROM target_versions ORDER BY id DESC LIMIT :limit")
    suspend fun getHistory(limit: Int = 20): List<TargetVersionSummary>

    @Query("SELECT * FROM target_versions WHERE id = :versionId")
    suspend fun getVersion(versionId: Long): TargetVersionEntity?

    @Query("SELECT id FROM target_versions WHERE isActive = 1 LIMIT 1")
    suspend fun getActiveVersionId(): Long?
}

data class TargetVersionSummary(
    val id: Long,
    val timestamp: Long,
    val operatorId: String,
    val qualityScore: Double?,
    val isActive: Boolean,
)

class TargetCanon(context: Context) {

    companion object {
        private const val TAG = "TargetCanon"
    }

    private val dao: TargetVersionDao

    init {
        val db = FlightRiskDatabase.getInstance(context)
        dao = db.targetVersionDao()
    }

    suspend fun setTarget(
        image: Bitmap,
        operatorId: String = "default",
        qualityScore: Double? = null,
    ): Long {
        val b64 = bitmapToBase64(image)
        dao.deactivateAll()
        val id = dao.insert(
            TargetVersionEntity(
                operatorId = operatorId,
                qualityScore = qualityScore,
                imageB64 = b64,
                isActive = true,
            )
        )
        Log.i(TAG, "Target set: version $id by $operatorId")
        return id
    }

    suspend fun getActive(): Pair<Long, Bitmap>? {
        val entity = dao.getActive() ?: return null
        val bitmap = base64ToBitmap(entity.imageB64) ?: return null
        return entity.id to bitmap
    }

    suspend fun getHistory(limit: Int = 20): List<TargetVersionSummary> =
        dao.getHistory(limit)

    suspend fun revertTo(versionId: Long): Bitmap? {
        val entity = dao.getVersion(versionId) ?: return null
        dao.deactivateAll()
        dao.activate(versionId)
        Log.i(TAG, "Reverted to version $versionId")
        return base64ToBitmap(entity.imageB64)
    }

    suspend fun activeVersionId(): Long? = dao.getActiveVersionId()

    private fun bitmapToBase64(bitmap: Bitmap): String {
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, 90, out)
        return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
    }

    private fun base64ToBitmap(b64: String): Bitmap? {
        return try {
            val bytes = Base64.decode(b64, Base64.NO_WRAP)
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to decode target image: ${e.message}")
            null
        }
    }
}
