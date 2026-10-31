package com.flightrisk.app

import android.app.Application
import com.flightrisk.app.privacy.TempCameraPhotos

/**
 * Application class for FlightRisk.
 *
 * Handles app-wide initialization: config loading, ONNX Runtime session
 * setup, and any dependency injection in future phases.
 */
class FlightRiskApp : Application() {

    override fun onCreate() {
        super.onCreate()
        // A camera capture of a target photo can remain in the cache if the
        // process died before it was decoded. Delete it at process start.
        TempCameraPhotos.sweep(cacheDir)
        // Future: initialize ONNX Runtime, set up DI, etc.
    }
}
