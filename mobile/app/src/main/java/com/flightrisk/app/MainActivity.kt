package com.flightrisk.app

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import com.flightrisk.app.config.FlightRiskConfig
import com.flightrisk.app.config.SensitivityPreset
import com.flightrisk.app.ui.navigation.FlightRiskNavHost
import com.flightrisk.app.ui.onboarding.OnboardingScreen
import com.flightrisk.app.ui.onboarding.isOnboardingComplete
import com.flightrisk.app.ui.quality.QualityReport
import com.flightrisk.app.ui.search.SearchScreenState
import com.flightrisk.app.ui.settings.SettingsScreenState
import com.flightrisk.app.ui.theme.FlightRiskTheme

/**
 * Entry point activity for FlightRisk.
 *
 * On launch, checks whether onboarding is complete:
 * - If not: shows the [OnboardingScreen] flow
 * - If yes: shows the [FlightRiskNavHost] with bottom navigation
 *
 * Requests camera and location permissions on launch.
 */
class MainActivity : ComponentActivity() {

    companion object {
        private const val TAG = "MainActivity"
    }

    // ------------------------------------------------------------------
    // UI state
    // ------------------------------------------------------------------

    private var onboardingComplete by mutableStateOf(false)
    private var searchState by mutableStateOf(SearchScreenState())
    private var settingsState by mutableStateOf(SettingsScreenState())
    private var targetBitmap by mutableStateOf<Bitmap?>(null)
    private var targetQualityReport by mutableStateOf<QualityReport?>(null)

    // ------------------------------------------------------------------
    // Permission launcher
    // ------------------------------------------------------------------

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        val cameraGranted = permissions[Manifest.permission.CAMERA] == true
        val locationGranted = permissions[Manifest.permission.ACCESS_FINE_LOCATION] == true

        Log.i(TAG, "Permissions: camera=$cameraGranted, location=$locationGranted")

        if (!cameraGranted) {
            Log.w(TAG, "Camera permission denied -- search will not work")
        }
    }

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        onboardingComplete = isOnboardingComplete(this)

        // Load initial settings from config
        val config = FlightRiskConfig.getInstance(this)
        val savedApiKey = config.reasoning.apiKey ?: ""
        settingsState = SettingsScreenState(
            activePreset = SensitivityPreset.BALANCED,
            reidThreshold = config.vision.reidThreshold.toFloat(),
            faceThreshold = config.vision.faceMatchThreshold.toFloat(),
            scorerThreshold = config.vision.scorerMatchThreshold.toFloat(),
            llmApiKey = savedApiKey,
            llmAvailable = savedApiKey.isNotBlank(),
        )

        // Request permissions
        requestPermissionsIfNeeded()

        setContent {
            FlightRiskTheme {
                if (!onboardingComplete) {
                    OnboardingScreen(
                        onComplete = { bitmap ->
                            targetBitmap = bitmap
                            onboardingComplete = true
                        },
                    )
                } else {
                    FlightRiskNavHost(
                        searchState = searchState,
                        settingsState = settingsState,
                        targetBitmap = targetBitmap,
                        targetQualityReport = targetQualityReport,
                        onStartSearch = ::handleStartSearch,
                        onStopSearch = ::handleStopSearch,
                        onDismissAlert = ::handleDismissAlert,
                        onNotMyChild = ::handleNotMyChild,
                        onPhotoSelected = ::handlePhotoSelected,
                        onPresetSelected = ::handlePresetSelected,
                        onThresholdChanged = ::handleThresholdChanged,
                        onLlmBackendChanged = ::handleLlmBackendChanged,
                        onApiKeyChanged = ::handleApiKeyChanged,
                    )
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // Permission handling
    // ------------------------------------------------------------------

    private fun requestPermissionsIfNeeded() {
        val permissionsToRequest = mutableListOf<String>()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) {
            permissionsToRequest.add(Manifest.permission.CAMERA)
        }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED
        ) {
            permissionsToRequest.add(Manifest.permission.ACCESS_FINE_LOCATION)
        }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION)
            != PackageManager.PERMISSION_GRANTED
        ) {
            permissionsToRequest.add(Manifest.permission.ACCESS_COARSE_LOCATION)
        }

        if (permissionsToRequest.isNotEmpty()) {
            permissionLauncher.launch(permissionsToRequest.toTypedArray())
        }
    }

    // ------------------------------------------------------------------
    // Search callbacks
    // ------------------------------------------------------------------

    private fun handleStartSearch() {
        searchState = searchState.copy(isSearching = true)
        Log.i(TAG, "Search started")
        // Future: initialize and start SearchPipeline
    }

    private fun handleStopSearch() {
        searchState = searchState.copy(isSearching = false, activeAlert = null)
        Log.i(TAG, "Search stopped")
        // Future: stop SearchPipeline
    }

    private fun handleDismissAlert() {
        searchState = searchState.copy(activeAlert = null)
    }

    private fun handleNotMyChild() {
        val alert = searchState.activeAlert
        Log.i(TAG, "Not my child: track=${alert?.trackId}")
        searchState = searchState.copy(activeAlert = null)
        // Future: record negative feedback for this track
    }

    // ------------------------------------------------------------------
    // Target photo callback
    // ------------------------------------------------------------------

    private fun handlePhotoSelected(bitmap: Bitmap, report: QualityReport) {
        targetBitmap = bitmap
        targetQualityReport = report
        Log.i(TAG, "Target photo selected: grade=${report.grade}, score=${report.overallScore}")
    }

    // ------------------------------------------------------------------
    // Settings callbacks
    // ------------------------------------------------------------------

    private fun handlePresetSelected(preset: SensitivityPreset) {
        settingsState = settingsState.copy(
            activePreset = preset,
            reidThreshold = preset.reidThreshold.toFloat(),
            faceThreshold = preset.faceMatchThreshold.toFloat(),
            scorerThreshold = preset.scorerMatchThreshold.toFloat(),
        )
        Log.i(TAG, "Preset selected: $preset")
        // Future: update FlightRiskConfig and restart pipeline
    }

    private fun handleThresholdChanged(name: String, value: Float) {
        settingsState = when (name) {
            "reid" -> settingsState.copy(activePreset = null, reidThreshold = value)
            "face" -> settingsState.copy(activePreset = null, faceThreshold = value)
            "scorer" -> settingsState.copy(activePreset = null, scorerThreshold = value)
            else -> settingsState
        }
        Log.d(TAG, "Threshold changed: $name=$value")
    }

    private fun handleLlmBackendChanged(backend: String) {
        settingsState = settingsState.copy(
            llmBackend = backend,
            llmAvailable = when (backend) {
                "cloud_claude" -> settingsState.llmApiKey.isNotBlank()
                else -> false
            },
        )
        Log.i(TAG, "LLM backend changed: $backend")
    }

    private fun handleApiKeyChanged(apiKey: String) {
        getSharedPreferences("flightrisk_config", MODE_PRIVATE)
            .edit()
            .putString("FLIGHTRISK_API_KEY", apiKey)
            .apply()
        settingsState = settingsState.copy(
            llmApiKey = apiKey,
            llmAvailable = apiKey.isNotBlank() && settingsState.llmBackend == "cloud_claude",
        )
    }
}
