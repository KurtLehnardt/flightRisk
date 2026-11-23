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
 * detector in Settings and it is on. If the imported one cannot be opened,
 * YOLOX-S is used and [Choice.IMPORTED_FAILED] says so, for the Search screen
 * and Settings. The embedder is always the open_clip ViT-B-16 model from the
 * assets. A missing model gives null: the search then runs without detection
 * or matching, and the Search screen says the models are not loaded.
 *
 * Nothing that identifies an imported model is logged.
 */
object DetectorFactory {
    private const val TAG = "DetectorFactory"

    /** Which detector a search got. */
    enum class Choice {
        /** YOLOX-S, the built-in detector. */
        YOLOX,

        /** The detector the user imported. */
        IMPORTED,

        /** The imported detector is on but could not be opened, so YOLOX-S is used. */
        IMPORTED_FAILED,

        /** No detector could be opened (the models are not installed). */
        NONE,
    }

    /** A detector (or none) and how it was chosen. */
    data class Selection<M>(val detector: M?, val choice: Choice)

    /**
     * The choice itself, without Android: try the imported detector when
     * [importedOn], fall back to YOLOX-S when that fails, and give none when
     * YOLOX-S fails too.
     */
    fun <M> select(importedOn: Boolean, openImported: () -> M, openYolox: () -> M): Selection<M> {
        var importedFailed = false
        if (importedOn) {
            try {
                return Selection(openImported(), Choice.IMPORTED)
            } catch (e: Exception) {
                importedFailed = true
            } catch (e: OutOfMemoryError) {
                importedFailed = true
            }
        }
        return try {
            Selection(openYolox(), if (importedFailed) Choice.IMPORTED_FAILED else Choice.YOLOX)
        } catch (e: Exception) {
            Selection(null, Choice.NONE)
        }
    }

    /**
     * A key for the detector a search should use now. When it is the same as
     * the key the current detector was built for, the detector is kept (also
     * when that build fell back to YOLOX-S, so YOLOX-S is not reloaded for every
     * search). It contains the imported model's digest: never log it.
     */
    fun wantedKey(context: Context): String =
        ByoDetectorStore(context).current()?.let { "imported:${it.sha256}" } ?: "yolox-s"

    /** The detector for a search, and how it was chosen. Records an imported detector's failure for Settings. */
    fun createDetector(context: Context, config: FlightRiskConfig): Selection<PersonDetector> {
        val threshold = config.vision.detectorConfidence.toFloat()
        val sizeFilter = DetectionSizeFilter.from(config.phoneVision)
        val store = ByoDetectorStore(context)
        val imported = store.current()
        val selection = select(
            importedOn = imported != null,
            openImported = {
                val model = OnnxModelLoader.fromImportedFile(store.modelFile)
                PersonDetector(DetectorEngine(model, UltralyticsContract(imported!!.layout), threshold, sizeFilter), "imported")
            },
            openYolox = {
                val model = OnnxModelLoader.fromAsset(context, YoloxContract.ASSET)
                PersonDetector(DetectorEngine(model, YoloxContract, threshold, sizeFilter), "yolox-s")
            },
        )
        if (imported != null) store.setLoadFailed(selection.choice != Choice.IMPORTED)
        when (selection.choice) {
            Choice.IMPORTED_FAILED -> Log.w(TAG, "The imported detector could not be opened; using YOLOX-S")
            Choice.NONE -> Log.w(TAG, "No detector could be opened (models not installed?)")
            else -> Unit
        }
        return selection
    }

    /** The appearance embedder, or null when its model is missing or does not load. */
    fun createEmbedder(context: Context, config: FlightRiskConfig): PersonReID? =
        try {
            val model = OnnxModelLoader.fromAsset(context, ClipContract.ASSET)
            PersonReID(EmbedderEngine(model), config.vision.reidThreshold.toFloat(), MatchSelection.from(config.phoneVision))
        } catch (e: Exception) {
            Log.w(TAG, "The appearance embedder is not installed or did not load (${e.javaClass.simpleName})")
            null
        }
}
