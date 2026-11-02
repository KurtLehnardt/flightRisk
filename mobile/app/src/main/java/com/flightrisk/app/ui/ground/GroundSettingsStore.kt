package com.flightrisk.app.ui.ground

import android.content.Context

/**
 * The non-secret Ground connection settings: the ground address (`host:port`
 * as [com.flightrisk.app.ground.GroundEndpoint.hostHeader] writes it) and
 * whether the link is switched on. The dashboard API key is never kept here;
 * it lives only in [com.flightrisk.app.ground.SecretStore].
 */
interface GroundSettingsStore {
    var address: String?
    var enabled: Boolean
}

/** [GroundSettingsStore] over a plain SharedPreferences file. */
class SharedPrefsGroundSettings(context: Context) : GroundSettingsStore {
    private val prefs = context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    override var address: String?
        get() = prefs.getString(KEY_ADDRESS, null)?.takeIf { it.isNotEmpty() }
        set(value) {
            prefs.edit().apply { if (value.isNullOrEmpty()) remove(KEY_ADDRESS) else putString(KEY_ADDRESS, value) }.apply()
        }

    override var enabled: Boolean
        get() = prefs.getBoolean(KEY_ENABLED, false)
        set(value) {
            prefs.edit().putBoolean(KEY_ENABLED, value).apply()
        }

    private companion object {
        const val FILE = "flightrisk_ground_settings"
        const val KEY_ADDRESS = "ground_address"
        const val KEY_ENABLED = "ground_enabled"
    }
}

/** An in-memory [GroundSettingsStore] for tests. */
class MemoryGroundSettings(override var address: String? = null, override var enabled: Boolean = false) : GroundSettingsStore
