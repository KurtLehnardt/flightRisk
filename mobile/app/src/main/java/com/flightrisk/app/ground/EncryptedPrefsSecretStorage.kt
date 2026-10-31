package com.flightrisk.app.ground

import android.content.Context
import android.content.SharedPreferences
import com.flightrisk.app.config.FlightRiskConfig

/**
 * [SecretStoragePort] over the app's existing EncryptedSharedPreferences file
 * (`flightrisk_secure_prefs`, AES256-SIV keys and AES256-GCM values under an
 * Android Keystore master key), so secrets already saved by older versions
 * stay readable. The file is excluded from backups (`res/xml/backup_rules.xml`
 * and `data_extraction_rules.xml`), because a restored copy cannot be
 * decrypted without the original device's Keystore key.
 *
 * Nothing here throws: opening, decrypting or committing can fail (a
 * corrupted file, a Keystore error), and every failure reads as null, writes
 * as false and shows in [availability]. The preferences are opened on first
 * use, which touches the Keystore: [SecretStore] keeps that off the main thread.
 */
class EncryptedPrefsSecretStorage(private val open: () -> SharedPreferences) : SecretStoragePort {

    /** The app's secure preferences file. */
    constructor(context: Context) : this(appSecurePrefs(context.applicationContext))

    @Volatile private var failure: String? = null

    private val prefs: SharedPreferences? by lazy {
        try {
            open()
        } catch (e: Throwable) {
            if (e is VirtualMachineError) throw e
            failure = "could not open the secure storage (${e.javaClass.simpleName})"
            null
        }
    }

    override fun read(key: String): String? =
        try {
            prefs?.getString(key, null)
        } catch (e: Exception) {
            failure = "could not read the secure storage (${e.javaClass.simpleName})"
            null
        }

    override fun write(changes: Map<String, String?>): Boolean {
        val p = prefs ?: return false
        return try {
            val editor = p.edit()
            for ((key, value) in changes) {
                if (value == null) editor.remove(key) else editor.putString(key, value)
            }
            editor.commit()
        } catch (e: Exception) {
            failure = "could not write the secure storage (${e.javaClass.simpleName})"
            false
        }
    }

    override fun availability(): SecretAvailability {
        prefs
        return failure?.let { SecretAvailability.Unavailable(it) } ?: SecretAvailability.Available
    }

    override fun toString(): String = "EncryptedPrefsSecretStorage"
}

private fun appSecurePrefs(app: Context): () -> SharedPreferences = { FlightRiskConfig.getEncryptedPrefs(app) }
