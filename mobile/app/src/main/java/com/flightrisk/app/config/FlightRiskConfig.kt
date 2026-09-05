package com.flightrisk.app.config

import android.content.Context
import android.content.SharedPreferences

/**
 * Centralized configuration for the FlightRisk mobile app.
 *
 * Port of `flightrisk/config.py` (`AmberConfig`) to Kotlin. Every default
 * below mirrors the value that was hardcoded in the Python codebase, so the
 * mobile and server configs stay in sync.
 *
 * On mobile, overrides come from [SharedPreferences] rather than environment
 * variables. The [fromPreferences] factory reads `FLIGHTRISK_*` preference
 * keys with the same semantics as `AmberConfig.from_env`.
 */
data class FlightRiskConfig(
    val vision: VisionConfig = VisionConfig(),
    val reasoning: ReasoningConfig = ReasoningConfig(),
    val drone: DroneConfig = DroneConfig(),
) {

    companion object {
        @Volatile
        private var instance: FlightRiskConfig? = null

        private const val PREFS_NAME = "flightrisk_config"

        /**
         * Return the app-wide [FlightRiskConfig] singleton.
         *
         * Loads from [SharedPreferences] on first call; subsequent calls
         * return the cached instance. Thread-safe via double-checked locking.
         */
        fun getInstance(context: Context): FlightRiskConfig {
            return instance ?: synchronized(this) {
                instance ?: fromPreferences(
                    context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                ).also { instance = it }
            }
        }

        /**
         * Build a config with values overridden from [SharedPreferences].
         *
         * Only a curated subset of the most commonly-tuned values is
         * exposed via preferences, matching the Python `AmberConfig.from_env`
         * pattern.
         */
        fun fromPreferences(prefs: SharedPreferences): FlightRiskConfig {
            val config = FlightRiskConfig()
            return config.copy(
                vision = config.vision.copy(
                    detectorModel = prefs.getString("FLIGHTRISK_DETECTOR_MODEL", null)
                        ?: config.vision.detectorModel,
                    detectorConfidence = prefs.getFloat(
                        "FLIGHTRISK_DETECTOR_CONFIDENCE",
                        config.vision.detectorConfidence.toFloat()
                    ).toDouble(),
                    reidThreshold = prefs.getFloat(
                        "FLIGHTRISK_REID_THRESHOLD",
                        config.vision.reidThreshold.toFloat()
                    ).toDouble(),
                    scorerMatchThreshold = prefs.getFloat(
                        "FLIGHTRISK_SCORER_THRESHOLD",
                        config.vision.scorerMatchThreshold.toFloat()
                    ).toDouble(),
                ),
                reasoning = config.reasoning.copy(
                    model = prefs.getString("FLIGHTRISK_GEMMA_MODEL", null)
                        ?: config.reasoning.model,
                    apiKey = prefs.getString("FLIGHTRISK_API_KEY", null)
                        ?: config.reasoning.apiKey,
                    alertCooldown = prefs.getFloat(
                        "FLIGHTRISK_ALERT_COOLDOWN",
                        config.reasoning.alertCooldown.toFloat()
                    ).toDouble(),
                    queueMaxSize = prefs.getInt(
                        "FLIGHTRISK_QUEUE_SIZE",
                        config.reasoning.queueMaxSize
                    ),
                ),
            )
        }

        /**
         * Build a config from a [SensitivityPreset].
         *
         * Adjusts thresholds to trade off between alert volume and
         * precision. [SensitivityPreset.BALANCED] produces the same
         * values as the defaults.
         */
        fun fromPreset(preset: SensitivityPreset): FlightRiskConfig {
            val config = FlightRiskConfig()
            return config.copy(
                vision = config.vision.copy(
                    reidThreshold = preset.reidThreshold,
                    scorerMatchThreshold = preset.scorerMatchThreshold,
                    faceMatchThreshold = preset.faceMatchThreshold,
                )
            )
        }

        /**
         * Clear the cached singleton so the next [getInstance] reloads.
         *
         * Mainly for tests that need to change preferences mid-run and
         * observe a fresh config.
         */
        fun resetInstance() {
            instance = null
        }
    }
}

/**
 * Detection, ReID, scoring, and tracking parameters.
 *
 * Defaults match `flightrisk.config.VisionConfig` in Python exactly.
 */
data class VisionConfig(
    val detectorModel: String = "yolo11n.pt",
    val detectorConfidence: Double = 0.4,
    val reidThreshold: Double = 0.55,
    val reidModel: String = "ViT-B-16",
    val faceDetSize: Pair<Int, Int> = Pair(640, 640),
    val faceMatchThreshold: Double = 0.45,
    val scorerMatchThreshold: Double = 0.45,
    val scorerReidWeight: Double = 0.35,
    val scorerFaceWeight: Double = 0.40,
    val scorerReasoningWeight: Double = 0.25,
    val trackerIouThreshold: Double = 0.3,
    val trackerMaxMissing: Int = 15,
    val trackerScoreWindow: Int = 8,
)

/**
 * Gemma reasoning worker: model, queueing, and timing parameters.
 *
 * Defaults match `flightrisk.config.ReasoningConfig` in Python exactly.
 */
data class ReasoningConfig(
    val model: String = "gemma4:latest",
    val apiKey: String? = null,
    val queueMaxSize: Int = 10,
    val alertCooldown: Double = 10.0,
    val gemmaRateLimit: Double = 5.0,
    val spatialGridSize: Int = 50,
    val trackUpdateInterval: Double = 1.0,
    val reasoningInterval: Double = 5.0,
    val metricsInterval: Double = 10.0,
    val corroborationThreshold: Int = 3,
)

/**
 * Drone connection, telemetry, and safety thresholds.
 *
 * Defaults match `flightrisk.config.DroneConfig` in Python exactly.
 */
data class DroneConfig(
    val autoConnectInterval: Double = 5.0,
    val batteryWarnThreshold: Int = 20,
    val batteryCriticalThreshold: Int = 10,
    val telloDefaultHost: String = "192.168.10.1",
    val mavlinkDefaultAddress: String = "udp://:14540",
    val mavlinkCmdTimeout: Double = 30.0,
)

/**
 * Sensitivity presets that trade off alert volume vs. precision.
 *
 * [MORE_ALERTS] lowers thresholds to catch more potential matches (higher
 * recall, more false positives). [FEWER_ALERTS] raises them for fewer,
 * higher-confidence alerts. [BALANCED] matches the defaults.
 */
enum class SensitivityPreset(
    val reidThreshold: Double,
    val scorerMatchThreshold: Double,
    val faceMatchThreshold: Double,
) {
    MORE_ALERTS(
        reidThreshold = 0.40,
        scorerMatchThreshold = 0.35,
        faceMatchThreshold = 0.30,
    ),
    BALANCED(
        reidThreshold = 0.55,
        scorerMatchThreshold = 0.45,
        faceMatchThreshold = 0.45,
    ),
    FEWER_ALERTS(
        reidThreshold = 0.70,
        scorerMatchThreshold = 0.60,
        faceMatchThreshold = 0.55,
    ),
}
