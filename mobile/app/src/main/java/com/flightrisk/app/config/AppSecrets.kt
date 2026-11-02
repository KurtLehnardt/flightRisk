package com.flightrisk.app.config

import android.content.Context
import com.flightrisk.app.ground.SecretStore

/**
 * Holds a value created at most once per process, under a lock. A creation
 * that throws stores nothing, so the next call tries again.
 */
class SingleInstance<T : Any> {
    private val lock = Any()

    @Volatile
    private var value: T? = null

    fun get(create: () -> T): T = value ?: synchronized(lock) { value ?: create().also { value = it } }

    override fun toString(): String = "SingleInstance(created=${value != null})"
}

/**
 * The app's one [SecretStore]. Every screen uses this instance, so the
 * encrypted preferences (and, on a fresh install, the Keystore master key and
 * the Tink keyset) are created by one caller only. Two stores opening the file
 * at the same time on a fresh install can each create a keyset, and values
 * written with the lost keyset later read as "Secure storage unavailable".
 */
object AppSecrets {
    private val store = SingleInstance<SecretStore>()

    fun store(context: Context): SecretStore = store.get { SecretStore(context.applicationContext) }
}
