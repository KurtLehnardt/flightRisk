package com.flightrisk.app.alert

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.RingtoneManager
import android.media.MediaPlayer
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import java.util.concurrent.ConcurrentHashMap

/**
 * Tiered alert manager for match events.
 *
 * Mirrors the Python `alerts.py` alert-throttle logic with Android-native
 * audio, haptic, and visual notification actions:
 *
 * | Alert Level       | Audio | Haptic | Visual |
 * |-------------------|-------|--------|--------|
 * | confirmed_match   |   X   |   X    |   X    |
 * | possible_match    |       |   X    |   X    |
 * | weak_signal       |       |        |   X    |
 *
 * A per-track cooldown (default 10 seconds, matching Python's
 * `ALERT_COOLDOWN`) prevents repeated alerts for the same spatial
 * track key within the cooldown window.
 *
 * @param context Android application context.
 * @param cooldownMs Cooldown in milliseconds between alerts for the
 *   same track key. Default 10_000 (10 seconds).
 */
class AlertManager(
    private val context: Context,
    private val cooldownMs: Long = 10_000L,
) {

    companion object {
        private const val TAG = "AlertManager"

        /** Alert level constants matching the Python pipeline. */
        const val CONFIRMED_MATCH = "confirmed_match"
        const val POSSIBLE_MATCH = "possible_match"
        const val WEAK_SIGNAL = "weak_signal"
        const val NO_MATCH = "no_match"
    }

    /** Tracks the last alert time per track key (for cooldown). */
    private val lastAlertTime = ConcurrentHashMap<String, Long>()

    /** Active audio player (one at a time). */
    private var activePlayer: MediaPlayer? = null
    private val playerLock = Any()

    /** Result of a [fireAlert] call, indicating which actions were taken. */
    data class AlertActions(
        val audio: Boolean,
        val haptic: Boolean,
        val visual: Boolean,
    )

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    /**
     * Fire an alert for the given level and track key.
     *
     * Respects the per-track cooldown: if the same [trackKey] was
     * alerted within [cooldownMs], this is a no-op and returns null.
     *
     * @param alertLevel One of [CONFIRMED_MATCH], [POSSIBLE_MATCH],
     *   [WEAK_SIGNAL], or [NO_MATCH].
     * @param trackKey Spatial track identifier (grid-cell key).
     * @return [AlertActions] describing what was triggered, or null
     *   if suppressed by cooldown.
     */
    fun fireAlert(alertLevel: String, trackKey: String): AlertActions? {
        if (alertLevel == NO_MATCH) return null

        val now = System.currentTimeMillis()
        val last = lastAlertTime[trackKey]
        if (last != null && (now - last) < cooldownMs) {
            Log.d(TAG, "Alert for $trackKey suppressed (cooldown)")
            return null
        }

        lastAlertTime[trackKey] = now
        Log.i(TAG, "Firing alert: level=$alertLevel track=$trackKey")

        return when (alertLevel) {
            CONFIRMED_MATCH -> {
                playAlarmTone()
                vibratePattern(
                    longArrayOf(0, 500, 200, 500, 200, 500),
                    repeat = 0,
                )
                AlertActions(audio = true, haptic = true, visual = true)
            }
            POSSIBLE_MATCH -> {
                vibrateBurst()
                AlertActions(audio = false, haptic = true, visual = true)
            }
            WEAK_SIGNAL -> {
                AlertActions(audio = false, haptic = false, visual = true)
            }
            else -> null
        }
    }

    /**
     * Dismiss alerts for a specific track key.
     * Stops audio and haptic feedback if active.
     */
    fun dismiss(trackKey: String) {
        lastAlertTime.remove(trackKey)
        stopAudio()
        cancelVibration()
        Log.d(TAG, "Dismissed alert for $trackKey")
    }

    /**
     * Dismiss all active alerts. Stops all audio and haptic feedback
     * and clears the cooldown tracker.
     */
    fun dismissAll() {
        lastAlertTime.clear()
        stopAudio()
        cancelVibration()
        Log.d(TAG, "All alerts dismissed")
    }

    /**
     * Check whether a track key is currently within its cooldown window.
     */
    fun isWithinCooldown(trackKey: String): Boolean {
        val last = lastAlertTime[trackKey] ?: return false
        return (System.currentTimeMillis() - last) < cooldownMs
    }

    /**
     * Release all resources. Call when the alert manager is no longer
     * needed (e.g. Activity.onDestroy).
     */
    fun release() {
        dismissAll()
        synchronized(playerLock) {
            activePlayer?.release()
            activePlayer = null
        }
    }

    // ------------------------------------------------------------------
    // Audio
    // ------------------------------------------------------------------

    /**
     * Play a repeating alarm tone at max alarm-stream volume.
     */
    private fun playAlarmTone() {
        synchronized(playerLock) {
            // Stop any existing playback
            activePlayer?.let {
                it.stop()
                it.release()
            }

            try {
                val alarmUri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                    ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)

                val player = MediaPlayer().apply {
                    setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_ALARM)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                            .build()
                    )
                    setDataSource(context, alarmUri)
                    isLooping = true
                    prepare()
                    start()
                }
                activePlayer = player

                // Ensure alarm stream is at max volume
                val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
                audioManager?.let {
                    val maxVol = it.getStreamMaxVolume(AudioManager.STREAM_ALARM)
                    it.setStreamVolume(AudioManager.STREAM_ALARM, maxVol, 0)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to play alarm tone", e)
            }
        }
    }

    /**
     * Stop any active audio playback.
     */
    private fun stopAudio() {
        synchronized(playerLock) {
            activePlayer?.let {
                try {
                    if (it.isPlaying) it.stop()
                    it.release()
                } catch (e: Exception) {
                    Log.w(TAG, "Error stopping audio", e)
                }
            }
            activePlayer = null
        }
    }

    // ------------------------------------------------------------------
    // Haptic
    // ------------------------------------------------------------------

    /**
     * Vibrate with a repeating pattern (for confirmed match).
     *
     * @param pattern Timing pattern: [delay, vibrate, sleep, vibrate, ...].
     * @param repeat Index to repeat from (-1 = no repeat, 0 = repeat all).
     */
    private fun vibratePattern(pattern: LongArray, repeat: Int) {
        val vibrator = getVibrator() ?: return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator.vibrate(
                    VibrationEffect.createWaveform(pattern, repeat)
                )
            } else {
                @Suppress("DEPRECATION")
                vibrator.vibrate(pattern, repeat)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Vibration failed", e)
        }
    }

    /**
     * Single vibration burst (for possible match).
     */
    private fun vibrateBurst() {
        val vibrator = getVibrator() ?: return
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator.vibrate(
                    VibrationEffect.createOneShot(500, VibrationEffect.DEFAULT_AMPLITUDE)
                )
            } else {
                @Suppress("DEPRECATION")
                vibrator.vibrate(500)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Vibration failed", e)
        }
    }

    /**
     * Cancel any active vibration.
     */
    private fun cancelVibration() {
        getVibrator()?.cancel()
    }

    /**
     * Get the system [Vibrator] service, handling API level differences.
     */
    private fun getVibrator(): Vibrator? {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val manager = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
            manager?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
        }
    }
}
