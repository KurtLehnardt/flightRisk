package com.flightrisk.app.ground

import android.util.Log

/**
 * [GroundLog] to logcat under the tag `FlightRiskGround`. The client redacts
 * every message before it reaches this sink; this sink redacts once more with
 * [extraSecrets] (for example [SecretStore.valuesForRedaction]) so a secret
 * the client does not know about cannot reach logcat either.
 */
class AndroidGroundLog(private val extraSecrets: () -> List<String> = { emptyList() }) : GroundLog {
    override fun log(level: GroundLog.Level, message: String) {
        val priority = when (level) {
            GroundLog.Level.DEBUG -> Log.DEBUG
            GroundLog.Level.INFO -> Log.INFO
            GroundLog.Level.WARN -> Log.WARN
            GroundLog.Level.ERROR -> Log.ERROR
        }
        Log.println(priority, TAG, Redact.text(message, extraSecrets()))
    }

    companion object {
        const val TAG = "FlightRiskGround"
    }
}
