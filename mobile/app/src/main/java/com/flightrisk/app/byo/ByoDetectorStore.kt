package com.flightrisk.app.byo

import android.content.Context
import android.content.SharedPreferences
import com.flightrisk.app.vision.UltralyticsContract
import java.io.File

/**
 * Where an imported detector lives and whether it is on.
 *
 * The model file is `files/byo_detector/detector.onnx` in the app's private
 * storage and its record is the `flightrisk_byo_detector` preferences; both are
 * excluded from backup and device transfer (`res/xml/backup_rules.xml` and
 * `res/xml/data_extraction_rules.xml`). It is off by default. Turning it off
 * deletes the file and the record, so turning it on again shows the licence
 * notice and asks for the attestation again.
 */
class ByoDetectorStore(context: Context) {
    private val appContext = context.applicationContext
    private val prefs: SharedPreferences = appContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /** The record of the imported model; null when none is on. */
    data class Imported(
        val sha256: String,
        val sizeBytes: Long,
        val layout: UltralyticsContract.Layout,
    )

    val folder: File get() = File(appContext.filesDir, FOLDER)
    val modelFile: File get() = File(folder, MODEL_FILE)

    /** The imported model, when it is on and its file is still there. */
    fun current(): Imported? {
        if (!prefs.getBoolean(KEY_ENABLED, false) || !prefs.getBoolean(KEY_ATTESTED, false)) return null
        val sha = prefs.getString(KEY_SHA256, null) ?: return null
        val layout = prefs.getString(KEY_LAYOUT, null)
            ?.let { name -> UltralyticsContract.Layout.entries.firstOrNull { it.name == name } } ?: return null
        if (!modelFile.isFile) return null
        return Imported(sha, prefs.getLong(KEY_SIZE, modelFile.length()), layout)
    }

    /** Records a model [ByoDetectorImporter] has already moved into [modelFile]. */
    fun save(imported: Imported) {
        prefs.edit()
            .putBoolean(KEY_ENABLED, true)
            .putBoolean(KEY_ATTESTED, true)
            .putString(KEY_SHA256, imported.sha256)
            .putLong(KEY_SIZE, imported.sizeBytes)
            .putString(KEY_LAYOUT, imported.layout.name)
            .commit()
    }

    /** Turns it off: deletes the model file and the record. Searches then use YOLOX-S. */
    fun turnOff() {
        prefs.edit().clear().commit()
        folder.listFiles()?.forEach { it.delete() }
        folder.delete()
    }

    companion object {
        const val PREFS_NAME = "flightrisk_byo_detector"
        const val FOLDER = "byo_detector"
        const val MODEL_FILE = "detector.onnx"
        private const val KEY_ENABLED = "enabled"
        private const val KEY_ATTESTED = "attested"
        private const val KEY_SHA256 = "sha256"
        private const val KEY_SIZE = "size_bytes"
        private const val KEY_LAYOUT = "layout"
    }
}
