package com.flightrisk.app.vision

import android.content.Context
import android.util.Log
import com.flightrisk.app.byo.ByoDetectorStore
import com.flightrisk.app.config.FlightRiskConfig
import com.flightrisk.app.model.OnnxModelLoader

/**
 * Builds the person detector and the appearance embedder a search uses.
 *
 * The detector is YOLOX-S from the assets, unless the user imported their own
 * detector in Settings and it is on; if that one cannot be opened, YOLOX-S is
 * used. The embedder is always the open_clip ViT-B-16 model from the assets.
 * A missing model gives null: the search then runs without detection or
 * matching, as before, and the Search screen says the models are not loaded.
 */
object DetectorFactory {
    private const val TAG = "DetectorFactory"

    /** The detector a search should use now, as a key comparable with [PersonDetector.source]. */
    fun wantedSource(context: Context): String =
        ByoDetectorStore(context).current()?.let { byoSource(it.sha256) } ?: YOLOX_SOURCE

    const val YOLOX_SOURCE = "yolox-s"
    private fun byoSource(sha256: String) = "imported:${sha256.take(16)}"

    /** The detector, or null when no detector model can be opened. */
    fun createDetector(
        context: Context,
        config: FlightRiskConfig,
        sizeFilter: DetectionSizeFilter = DetectionSizeFilter.AERIAL,
    ): PersonDetector? {
        val threshold = config.vision.detectorConfidence.toFloat()
        val imported = ByoDetectorStore(context).current()
        if (imported != null) {
            try {
                val model = OnnxModelLoader.fromFile(ByoDetectorStore(context).modelFile)
                return PersonDetector(
                    DetectorEngine(model, UltralyticsContract(imported.layout), threshold, sizeFilter),
                    byoSource(imported.sha256),
                )
            } catch (e: Exception) {
                Log.w(TAG, "The imported detector could not be opened; using YOLOX-S (${e.javaClass.simpleName})")
            }
        }
        return try {
            val model = OnnxModelLoader.fromAsset(context, YoloxContract.ASSET)
            PersonDetector(DetectorEngine(model, YoloxContract, threshold, sizeFilter), YOLOX_SOURCE)
        } catch (e: Exception) {
            Log.w(TAG, "YOLOX-S is not installed or did not load (${e.javaClass.simpleName})")
            null
        }
    }

    /** The appearance embedder, or null when its model is missing or does not load. */
    fun createEmbedder(context: Context, config: FlightRiskConfig): PersonReID? =
        try {
            val model = OnnxModelLoader.fromAsset(context, ClipContract.ASSET)
            PersonReID(EmbedderEngine(model), config.vision.reidThreshold.toFloat())
        } catch (e: Exception) {
            Log.w(TAG, "The appearance embedder is not installed or did not load (${e.javaClass.simpleName})")
            null
        }
}
