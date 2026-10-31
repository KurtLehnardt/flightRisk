package com.flightrisk.app.ground

import android.util.Log

/**
 * [GroundLog] to logcat under the tag `FlightRiskGround`. The client redacts
 * every message with all its secrets and only then truncates it, so give the
 * client the same extra secrets (`GroundConsoleClient(extraSecrets = ...)`)
 * that this sink gets: a secret first seen here could already have been cut
 * to an unrecognisable prefix. This sink redacts once more with
 * [extraSecrets] as a second line of defence.
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
