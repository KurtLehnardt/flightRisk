package com.flightrisk.app.config

import android.content.Context
import android.content.SharedPreferences

/**
 * Per-signal matching algorithm configuration.
 *
 * Each signal in the scoring pipeline has an [enabled] toggle and a
 * [weight] (0.0-1.0). The UI allows users to enable/disable individual
 * signals and adjust their relative weights. Persisted via SharedPreferences.
 *
 * @property enabled Whether this signal is active in the scoring pipeline.
 * @property weight Relative weight for this signal (0.0-1.0).
 * @property status Runtime status of the underlying model/algorithm.
 */
data class SignalConfig(
    val enabled: Boolean,
    val weight: Float,
    val status: SignalStatus = SignalStatus.AVAILABLE,
)

/**
 * Runtime status of a matching signal.
 */
enum class SignalStatus {
    /** Model loaded and ready. */
    AVAILABLE,
    /** Model file not found in assets. */
    NOT_INSTALLED,
    /** Signal is enabled and actively scoring. */
    ACTIVE,
}

/**
 * Categories for grouping signals in the settings UI.
 */
enum class SignalCategory(val displayName: String) {
    FACE_RECOGNITION("Face Recognition"),
    PERSON_REID("Person Re-ID"),
    APPEARANCE("Appearance"),
    OTHER("Other"),
}

/**
 * Metadata for a matching signal, used by the settings UI.
 *
 * @property key SharedPreferences key prefix (e.g. "reid").
 * @property displayName Human-readable name.
 * @property description Brief explanation of the signal.
 * @property category UI grouping category.
 * @property defaultEnabled Default enabled state.
 * @property defaultWeight Default weight.
 */
data class SignalMetadata(
    val key: String,
    val displayName: String,
    val description: String,
    val category: SignalCategory,
    val defaultEnabled: Boolean,
    val defaultWeight: Float,
)

/**
 * Central registry of all matching signals and their configurations.
 *
 * Manages persistence via SharedPreferences and provides the signal
 * list for the settings UI and the scoring pipeline.
 */
object MatchingAlgorithmConfig {

    private const val PREFS_NAME = "flightrisk_matching"

    /**
     * All supported matching signals, in display order.
     */
    val SIGNALS = listOf(
        // Face Recognition
        SignalMetadata(
            key = "face",
            displayName = "ArcFace (MobileFaceNet)",
            description = "Face embeddings via SCRFD detection + ArcFace recognition. Works even when clothing changes.",
            category = SignalCategory.FACE_RECOGNITION,
            defaultEnabled = true,
            defaultWeight = 0.40f,
        ),
        SignalMetadata(
            key = "insightface",
            displayName = "InsightFace R18",
            description = "Higher-quality face embeddings via InsightFace ResNet-18. Requires insightface_r18.tflite model.",
            category = SignalCategory.FACE_RECOGNITION,
            defaultEnabled = true,
            defaultWeight = 0.35f,
        ),
        // Person Re-ID
        SignalMetadata(
            key = "reid",
            displayName = "CLIP ReID",
            description = "Full-body appearance matching using CLIP visual embeddings. General-purpose re-identification.",
            category = SignalCategory.PERSON_REID,
            defaultEnabled = true,
            defaultWeight = 0.35f,
        ),
        SignalMetadata(
            key = "osnet_reid",
            displayName = "OSNet ReID",
            description = "Dedicated person re-identification model. More accurate than CLIP for person matching. Requires osnet_x1_0.tflite model.",
            category = SignalCategory.PERSON_REID,
            defaultEnabled = true,
            defaultWeight = 0.30f,
        ),
        // Appearance
        SignalMetadata(
            key = "clothing_color",
            displayName = "Clothing Color",
            description = "HSV color histogram matching for upper and lower body clothing. No model required.",
            category = SignalCategory.APPEARANCE,
            defaultEnabled = true,
            defaultWeight = 0.15f,
        ),
        // Other
        SignalMetadata(
            key = "height_ratio",
            displayName = "Height Ratio",
            description = "Bounding box aspect ratio comparison. Lightweight geometric signal, no model required.",
            category = SignalCategory.OTHER,
            defaultEnabled = true,
            defaultWeight = 0.05f,
        ),
        SignalMetadata(
            key = "reasoning",
            displayName = "LLM Reasoning",
            description = "Visual reasoning via Claude or other LLM. Provides semantic understanding of matches.",
            category = SignalCategory.OTHER,
            defaultEnabled = true,
            defaultWeight = 0.25f,
        ),
    )

    /**
     * Load signal configurations from SharedPreferences.
     *
     * @param context Android context.
     * @return Map of signal key -> [SignalConfig].
     */
    fun loadConfigs(context: Context): Map<String, SignalConfig> {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return loadConfigs(prefs)
    }

    /**
     * Load signal configurations from the given SharedPreferences.
     *
     * @param prefs SharedPreferences instance.
     * @return Map of signal key -> [SignalConfig].
     */
    fun loadConfigs(prefs: SharedPreferences): Map<String, SignalConfig> {
        val configs = mutableMapOf<String, SignalConfig>()
        for (signal in SIGNALS) {
            val enabled = prefs.getBoolean("signal_${signal.key}_enabled", signal.defaultEnabled)
            val weight = prefs.getFloat("signal_${signal.key}_weight", signal.defaultWeight)
            configs[signal.key] = SignalConfig(enabled = enabled, weight = weight)
        }
        return configs
    }

    /**
     * Save a signal's enabled state to SharedPreferences.
     */
    fun saveEnabled(context: Context, signalKey: String, enabled: Boolean) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putBoolean("signal_${signalKey}_enabled", enabled).apply()
    }

    /**
     * Save a signal's weight to SharedPreferences.
     */
    fun saveWeight(context: Context, signalKey: String, weight: Float) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().putFloat("signal_${signalKey}_weight", weight).apply()
    }

    /**
     * Reset all signal configs to defaults.
     */
    fun resetToDefaults(context: Context) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().clear().apply()
    }

    /**
     * Get the metadata for a signal by key.
     */
    fun getMetadata(key: String): SignalMetadata? {
        return SIGNALS.find { it.key == key }
    }
}
