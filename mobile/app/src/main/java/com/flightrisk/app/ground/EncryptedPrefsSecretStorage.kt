package com.flightrisk.app.ground

import android.content.Context
import android.content.SharedPreferences
import com.flightrisk.app.config.FlightRiskConfig

/**
 * [SecretStoragePort] over the app's existing EncryptedSharedPreferences file
 * (`flightrisk_secure_prefs`, AES256-SIV keys and AES256-GCM values under an
 * Android Keystore master key), so secrets already saved by older versions
 * stay readable. The preferences are opened on first use, because creating
 * the master key touches the Keystore; call it off the main thread.
 */
class EncryptedPrefsSecretStorage(context: Context) : SecretStoragePort {
    private val appContext = context.applicationContext

    private val prefs: SharedPreferences by lazy { FlightRiskConfig.getEncryptedPrefs(appContext) }

    override fun read(key: String): String? = prefs.getString(key, null)

    override fun write(changes: Map<String, String?>): Boolean {
        val editor = prefs.edit()
        for ((key, value) in changes) {
            if (value == null) editor.remove(key) else editor.putString(key, value)
        }
        return editor.commit()
    }

    override fun toString(): String = "EncryptedPrefsSecretStorage"
}
