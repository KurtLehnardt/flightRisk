package com.flightrisk.app

import android.app.Application

/**
 * Application class for FlightRisk.
 *
 * Handles app-wide initialization: config loading, ONNX Runtime session
 * setup, and any dependency injection in future phases.
 */
class FlightRiskApp : Application() {

    override fun onCreate() {
        super.onCreate()
        // Future: initialize ONNX Runtime, set up DI, etc.
    }
}
