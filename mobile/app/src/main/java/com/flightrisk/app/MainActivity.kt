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
import com.flightrisk.app.drone.DroneManager
import com.flightrisk.app.drone.FrameSourceMode
import com.flightrisk.app.drone.TelloState
import com.flightrisk.app.drone.TelloWifiChecker

import com.flightrisk.app.alert.AlertManager
import com.flightrisk.app.camera.CameraXFrameSource
import com.flightrisk.app.llm.LlmSelector
import com.flightrisk.app.location.LocationProvider
import com.flightrisk.app.pipeline.SearchPipeline
import com.flightrisk.app.vision.Detection
import com.flightrisk.app.vision.FaceRecognizer
import com.flightrisk.app.vision.PersonDetector
import com.flightrisk.app.vision.PersonReID

import com.flightrisk.app.ui.navigation.FlightRiskNavHost
import com.flightrisk.app.ui.onboarding.OnboardingScreen
import com.flightrisk.app.ui.onboarding.isOnboardingComplete
import com.flightrisk.app.ui.quality.QualityReport
import com.flightrisk.app.ui.search.SearchScreenState
import com.flightrisk.app.ui.settings.SettingsScreenState
import com.flightrisk.app.ui.theme.FlightRiskTheme
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

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

    // Drone state
    private var droneManager: DroneManager? = null
    private var currentDroneState by mutableStateOf<TelloState?>(null)
    private var frameSourceMode by mutableStateOf(FrameSourceMode.CAMERA)
    private var latestDroneFrame by mutableStateOf<Bitmap?>(null)
    private var droneStateJob: Job? = null
    private var droneAlertJob: Job? = null

    // AI pipeline state
    private var searchPipeline: SearchPipeline? = null
    private var personDetector: PersonDetector? = null
    private var personReID: PersonReID? = null
    private var faceRecognizer: FaceRecognizer? = null
    private var alertManager: AlertManager? = null
    private var pipelineEventJob: Job? = null
    private var pipelineSetupJob: Job? = null
    private var llmSelector: LlmSelector? = null
    private var locationProvider: LocationProvider? = null
    private var cameraFrameSource: CameraXFrameSource? = null


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

        // Initialize alert, LLM, and location subsystems
        alertManager = AlertManager(applicationContext)
        llmSelector = LlmSelector(applicationContext).also { it.startMonitoring() }
        locationProvider = LocationProvider(applicationContext)

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
                        droneState = currentDroneState,
                        frameSourceMode = frameSourceMode,
                        latestDroneFrame = latestDroneFrame,
                        onDismissDroneAlert = ::handleDismissDroneAlert,
                        onDroneConnect = ::handleDroneConnect,
                        onDroneDisconnect = ::handleDroneDisconnect,
                        onTakeoff = ::handleTakeoff,
                        onLand = ::handleLand,
                        onDroneMove = ::handleDroneMove,
                        onDroneRotate = ::handleDroneRotate,
                        onEmergencyStop = ::handleEmergencyStop,
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
        if (searchState.isSearching) return

        // Update UI immediately so button toggles without waiting for model load
        if (frameSourceMode == FrameSourceMode.DRONE) {
            searchState = searchState.copy(
                isSearching = true,
                frameWidth = 640,
                frameHeight = 480,
            )
        } else {
            searchState = searchState.copy(
                isSearching = true,
                frameWidth = 1920,
                frameHeight = 1080,
            )
        }

        val config = FlightRiskConfig.getInstance(this)
        val ctx = applicationContext
        val target = targetBitmap
        val currentFrameSourceMode = frameSourceMode

        // Load models and start pipeline off the main thread
        pipelineSetupJob = lifecycleScope.launch(Dispatchers.Default) {
            // --- Instantiate vision components (if not already created) ---

            if (personDetector == null) {
                try {
                    personDetector = PersonDetector(ctx)
                    Log.i(TAG, "PersonDetector loaded")
                } catch (e: Exception) {
                    Log.w(TAG, "PersonDetector failed to load (YOLO model missing?)", e)
                }
            }

            if (personReID == null) {
                try {
                    personReID = PersonReID(ctx)
                    Log.i(TAG, "PersonReID loaded")
                } catch (e: Exception) {
                    Log.w(TAG, "PersonReID failed to load (CLIP model missing?)", e)
                }
            }

            if (faceRecognizer == null) {
                try {
                    faceRecognizer = FaceRecognizer(ctx)
                    Log.i(TAG, "FaceRecognizer loaded")
                } catch (e: Exception) {
                    Log.w(TAG, "FaceRecognizer failed to load (SCRFD/ArcFace models missing?)", e)
                }
            }

            // --- Set target photo on vision components ---

            if (target != null) {
                try {
                    personReID?.setTarget(target)
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to set ReID target", e)
                }
                try {
                    faceRecognizer?.setTarget(target)
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to set face target", e)
                }
            }

            // --- Create camera frame source for camera mode ---

            if (currentFrameSourceMode == FrameSourceMode.CAMERA && cameraFrameSource == null) {
                cameraFrameSource = CameraXFrameSource(1920, 1080)
            }

            // --- Create SearchPipeline ---

            val am = alertManager ?: AlertManager(ctx).also { alertManager = it }
            val ls = llmSelector ?: LlmSelector(ctx).also {
                it.startMonitoring()
                llmSelector = it
            }
            val lp = locationProvider ?: LocationProvider(ctx).also {
                locationProvider = it
            }

            // Bail out if user stopped search while we were loading models
            if (!searchState.isSearching) return@launch

            val pipeline = SearchPipeline(config, ls, am, lp)
            searchPipeline = pipeline

            // Wire frame source: pull from drone or camera based on active mode
            pipeline.frameSource = SearchPipeline.FrameSource {
                if (frameSourceMode == FrameSourceMode.DRONE) {
                    droneManager?.frameSource?.getLatestFrame()
                } else {
                    cameraFrameSource?.getLatestFrame()
                }
            }

            // Wire detection callback (delegates to PersonDetector)
            val detector = personDetector
            if (detector != null) {
                pipeline.detectionCallback = object : SearchPipeline.DetectionCallback {
                    override fun detect(frame: Bitmap): List<Detection> {
                        return detector.detect(frame)
                    }

                    override fun annotate(
                        frame: Bitmap,
                        detections: List<Detection>,
                        matchIdx: Int?,
                    ): Bitmap {
                        return detector.annotate(frame, detections, matchIdx)
                    }
                }
            }

            // Wire ReID callback (delegates to PersonReID)
            val reid = personReID
            if (reid != null) {
                pipeline.reidCallback = object : SearchPipeline.ReidCallback {
                    override val matchThreshold: Float =
                        config.vision.reidThreshold.toFloat()

                    override fun findMatch(detections: List<Detection>): Pair<Int, Float>? {
                        val result = reid.findMatch(detections)
                        return if (result.first != null) {
                            Pair(result.first!!, result.second)
                        } else {
                            null
                        }
                    }

                    override fun compare(crop: Bitmap): Float {
                        return reid.compare(crop)
                    }
                }
            }

            // Wire face callback (delegates to FaceRecognizer)
            val face = faceRecognizer
            if (face != null) {
                pipeline.faceCallback = object : SearchPipeline.FaceCallback {
                    override val hasTarget: Boolean get() = face.hasTarget

                    override val matchThreshold: Float =
                        config.vision.faceMatchThreshold.toFloat()

                    override fun findMatch(detections: List<Detection>): Pair<Int, Float>? {
                        val result = face.findMatch(detections)
                        return if (result.first != null) {
                            Pair(result.first!!, result.second)
                        } else {
                            null
                        }
                    }

                    override fun compare(crop: Bitmap): Float {
                        return face.compare(crop)
                    }
                }
            }

            // Set target photo on pipeline for LLM reasoning
            pipeline.targetPhoto = target

            // --- Collect pipeline events into searchState ---

            pipelineEventJob = lifecycleScope.launch {
                pipeline.events.collect { event ->
                    when (event) {
                        is SearchPipeline.PipelineEvent.FrameProcessed -> {
                            searchState = searchState.copy(
                                fps = event.fps,
                                personsDetected = event.personsDetected,
                                cameraFrame = event.annotatedFrame,
                            )
                        }
                        is SearchPipeline.PipelineEvent.MatchAlert -> {
                            searchState = searchState.copy(
                                activeAlert = event.matchEntry,
                            )
                        }
                        is SearchPipeline.PipelineEvent.ConfidenceProgress -> {
                            searchState = searchState.copy(
                                confidenceFrames = event.framesMatched,
                                confidenceNeeded = event.framesNeeded,
                                highestMatchScore = event.avgScore,
                            )
                        }
                        is SearchPipeline.PipelineEvent.SearchComplete -> {
                            Log.i(
                                TAG,
                                "Search complete: reason=${event.reason}, " +
                                    "alertLevel=${event.alertLevel}",
                            )
                        }
                    }
                }
            }

            // Start the pipeline
            pipeline.start()

            // Take off and start search pattern if drone is connected
            if (currentFrameSourceMode == FrameSourceMode.DRONE) {
                val manager = droneManager
                if (manager != null) {
                    val alreadyFlying = manager.droneState.value.telemetry.isFlying
                    if (!alreadyFlying) {
                        manager.takeoff()
                        delay(3000)
                    }
                    manager.startSearchPattern()
                }
            }

            Log.i(TAG, "Search started, frameSource=$frameSourceMode")
        }
    }

    private fun handleStopSearch() {
        pipelineSetupJob?.cancel()
        pipelineSetupJob = null
        pipelineEventJob?.cancel()
        pipelineEventJob = null
        searchPipeline?.stop("user_stopped")
        searchPipeline = null
        droneManager?.stopSearchPattern()
        if (frameSourceMode == FrameSourceMode.DRONE) {
            lifecycleScope.launch { droneManager?.land() }
        }
        alertManager?.dismissAll()
        searchState = searchState.copy(isSearching = false, activeAlert = null)
        Log.i(TAG, "Search stopped")
    }

    private fun handleDismissAlert() {
        alertManager?.dismissAll()
        searchState = searchState.copy(activeAlert = null)
    }

    private fun handleDismissDroneAlert() {
        searchState = searchState.copy(droneAlert = null)
    }

    private fun handleNotMyChild() {
        val alert = searchState.activeAlert
        alertManager?.dismissAll()
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

    // ------------------------------------------------------------------
    // Drone callbacks
    // ------------------------------------------------------------------

    private fun handleDroneConnect() {
        if (droneManager != null) {
            Log.w(TAG, "Already connected or connecting, ignoring duplicate connect")
            return
        }
        val config = FlightRiskConfig.getInstance(this)
        val manager = DroneManager(applicationContext, config)
        droneManager = manager

        currentDroneState = null
        searchState = searchState.copy(droneConnectionMessage = null)

        // Collect drone state immediately so CONNECTING/ERROR states are visible
        droneStateJob = lifecycleScope.launch {
            manager.droneState.collect { state ->
                currentDroneState = state
            }
        }

        lifecycleScope.launch {
            val success = manager.connectAndStream()
            if (!success) {
                val wifiStatus = manager.wifiChecker.check()
                val droneError = manager.droneState.value.errorMessage
                val message = when {
                    wifiStatus !is TelloWifiChecker.WifiStatus.OnTelloWifi ->
                        manager.wifiChecker.getGuidanceMessage(wifiStatus)
                    droneError != null ->
                        droneError
                    else ->
                        "Connection failed. Make sure the Tello is powered on and try again."
                }
                Log.w(TAG, "Drone connection failed: $message")
                searchState = searchState.copy(droneConnectionMessage = message)
                droneStateJob?.cancel()
                droneStateJob = null
                droneManager = null
                return@launch
            }

            frameSourceMode = FrameSourceMode.DRONE
            searchState = searchState.copy(droneConnectionMessage = null)

            // Wire frame callback for Compose state updates
            manager.frameSource.setOnFrameCallback { bitmap ->
                lifecycleScope.launch(Dispatchers.Main.immediate) {
                    latestDroneFrame = bitmap
                }
            }

            // Collect alerts and route critical ones to the UI
            droneAlertJob = lifecycleScope.launch {
                manager.alerts.collect { alert ->
                    when (alert) {
                        is DroneManager.DroneAlert.ConnectionLost -> {
                            Log.e(TAG, "Drone alert: connection lost")
                            searchState = searchState.copy(
                                droneAlert = "Connection lost — check Tello WiFi",
                            )
                            // Clean up so user can reconnect (launch separately
                            // to avoid cancelling this collector mid-collect)
                            lifecycleScope.launch { cleanUpDroneConnection() }
                        }
                        is DroneManager.DroneAlert.BatteryCritical -> {
                            Log.e(TAG, "Drone alert: battery critical ${alert.percent}%")
                            searchState = searchState.copy(
                                droneAlert = "Battery critical: ${alert.percent}% — drone is auto-landing",
                            )
                        }
                        is DroneManager.DroneAlert.CrashDetected -> {
                            Log.e(TAG, "Drone alert: crash detected")
                            searchState = searchState.copy(
                                droneAlert = "Drone may have landed unexpectedly",
                            )
                        }
                        is DroneManager.DroneAlert.StreamFrozen -> {
                            Log.w(TAG, "Drone alert: stream frozen, recovering")
                        }
                    }
                }
            }

            Log.i(TAG, "Drone connected and streaming")
        }
    }

    private fun handleDroneDisconnect() {
        droneStateJob?.cancel()
        droneStateJob = null
        droneAlertJob?.cancel()
        droneAlertJob = null

        val manager = droneManager
        droneManager = null
        currentDroneState = null
        latestDroneFrame = null
        frameSourceMode = FrameSourceMode.CAMERA
        searchState = searchState.copy(droneConnectionMessage = null)

        lifecycleScope.launch {
            manager?.disconnect()
            Log.i(TAG, "Drone disconnected")
        }
    }

    private fun cleanUpDroneConnection() {
        droneStateJob?.cancel()
        droneStateJob = null
        droneAlertJob?.cancel()
        droneAlertJob = null

        val manager = droneManager
        droneManager = null
        currentDroneState = null
        latestDroneFrame = null
        frameSourceMode = FrameSourceMode.CAMERA

        lifecycleScope.launch {
            manager?.disconnect()
            Log.i(TAG, "Drone cleaned up after connection loss")
        }
    }

    private fun handleTakeoff() {
        lifecycleScope.launch { droneManager?.takeoff() }
    }

    private fun handleLand() {
        lifecycleScope.launch { droneManager?.land() }
    }

    private fun handleEmergencyStop() {
        lifecycleScope.launch { droneManager?.emergencyStop() }
    }

    private fun handleDroneMove(direction: String, distanceCm: Int) {
        lifecycleScope.launch { droneManager?.move(direction, distanceCm) }
    }

    private fun handleDroneRotate(degrees: Int) {
        lifecycleScope.launch { droneManager?.rotate(degrees) }
    }

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    override fun onPause() {
        super.onPause()
        droneManager?.onActivityPause()
    }

    override fun onDestroy() {
        // Clean up pipeline
        pipelineSetupJob?.cancel()
        pipelineSetupJob = null
        pipelineEventJob?.cancel()
        pipelineEventJob = null
        searchPipeline?.stop("activity_destroyed")
        searchPipeline = null

        // Clean up vision components
        try { personDetector?.close() } catch (e: Exception) {
            Log.w(TAG, "Error closing PersonDetector", e)
        }
        personDetector = null
        try { personReID?.close() } catch (e: Exception) {
            Log.w(TAG, "Error closing PersonReID", e)
        }
        personReID = null
        try { faceRecognizer?.close() } catch (e: Exception) {
            Log.w(TAG, "Error closing FaceRecognizer", e)
        }
        faceRecognizer = null

        // Clean up supporting components
        alertManager?.release()
        alertManager = null
        locationProvider?.stopUpdates()
        locationProvider = null
        cameraFrameSource?.stop()
        cameraFrameSource = null

        // Clean up drone
        droneManager?.onActivityDestroy()
        droneManager = null
        super.onDestroy()
    }
}
