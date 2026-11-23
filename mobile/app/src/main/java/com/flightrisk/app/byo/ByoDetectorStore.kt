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
 * `res/xml/data_extraction_rules.xml`). It is off by default. The record keeps
 * the digest, the output layout, and which version of the attestation the user
 * ticked and when. Turning it off deletes the file and the record, so turning it
 * on again shows the licence notice and asks for the attestation again; so does
 * a change of the attestation's wording ([ATTESTATION_VERSION]).
 *
 * Reads touch the disk: call them off the main thread.
 *
 * @param filesDir The app's private files folder (`Context.getFilesDir()`).
 * @param prefs The `flightrisk_byo_detector` preferences.
 */
class ByoDetectorStore(private val filesDir: File, private val prefs: SharedPreferences) {

    constructor(context: Context) : this(
        context.applicationContext.filesDir,
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE),
    )

    /** The record of the imported model. */
    data class Imported(
        val sha256: String,
        val sizeBytes: Long,
        val layout: UltralyticsContract.Layout,
        val attestationVersion: Int = ATTESTATION_VERSION,
        val attestedAtMillis: Long = 0L,
    )

    val folder: File get() = File(filesDir, FOLDER)
    val modelFile: File get() = File(folder, MODEL_FILE)

    /** The imported model, when it is on, attested with the current wording, and its file is still there. */
    fun current(): Imported? {
        if (!prefs.getBoolean(KEY_ENABLED, false)) return null
        val version = prefs.getInt(KEY_ATTESTATION_VERSION, 0)
        if (version != ATTESTATION_VERSION) return null
        val sha = prefs.getString(KEY_SHA256, null) ?: return null
        val layout = prefs.getString(KEY_LAYOUT, null)
            ?.let { name -> UltralyticsContract.Layout.entries.firstOrNull { it.name == name } } ?: return null
        if (!modelFile.isFile) return null
        return Imported(sha, prefs.getLong(KEY_SIZE, modelFile.length()), layout, version, prefs.getLong(KEY_ATTESTED_AT, 0L))
    }

    /** Records a model [ByoDetectorImporter] has already moved into [modelFile]. */
    fun save(imported: Imported) {
        prefs.edit()
            .clear()
            .putBoolean(KEY_ENABLED, true)
            .putInt(KEY_ATTESTATION_VERSION, imported.attestationVersion)
            .putLong(KEY_ATTESTED_AT, imported.attestedAtMillis)
            .putString(KEY_SHA256, imported.sha256)
            .putLong(KEY_SIZE, imported.sizeBytes)
            .putString(KEY_LAYOUT, imported.layout.name)
            .commit()
    }

    /** True when the last search could not open the imported model and used YOLOX-S instead. */
    fun lastLoadFailed(): Boolean = prefs.getBoolean(KEY_LOAD_FAILED, false)

    /** Records whether the last attempt to open the imported model failed. */
    fun setLoadFailed(failed: Boolean) {
        if (lastLoadFailed() != failed) prefs.edit().putBoolean(KEY_LOAD_FAILED, failed).commit()
    }

    /** Turns it off: deletes the model file and the record. Searches then use YOLOX-S. */
    fun turnOff() {
        prefs.edit().clear().commit()
        folder.listFiles()?.forEach { it.delete() }
        folder.delete()
    }

    /**
     * Deletes copies left by an import that did not finish (the app was killed,
     * or a hostile model crashed it while it was being checked).
     */
    fun deleteStaleImports() {
        folder.listFiles { f -> f.name.startsWith(TEMP_PREFIX) }?.forEach { it.delete() }
    }

    /** A new, unused name for an import in progress. */
    fun newTempFile(): File = File(folder, "$TEMP_PREFIX${System.nanoTime()}.onnx")

    companion object {
        const val PREFS_NAME = "flightrisk_byo_detector"
        const val FOLDER = "byo_detector"
        const val MODEL_FILE = "detector.onnx"
        const val TEMP_PREFIX = ".import-"

        /** Raise when [ByoDetectorText.ATTESTATION] or the notice changes, so users attest again. */
        const val ATTESTATION_VERSION = 1

        private const val KEY_ENABLED = "enabled"
        private const val KEY_ATTESTATION_VERSION = "attestation_version"
        private const val KEY_ATTESTED_AT = "attested_at_ms"
        private const val KEY_SHA256 = "sha256"
        private const val KEY_SIZE = "size_bytes"
        private const val KEY_LAYOUT = "layout"
        private const val KEY_LOAD_FAILED = "load_failed"
    }
}
