package com.flightrisk.app

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.flightrisk.app.ui.ground.GroundConsoleViewModel
import com.flightrisk.app.demo.DemoArt
import com.flightrisk.app.demo.DemoImages
import com.flightrisk.app.demo.DemoText
import com.flightrisk.app.ui.demo.DemoFrame
import com.flightrisk.app.ui.demo.DemoBlockedDialog
import com.flightrisk.app.ui.demo.DemoDisconnectDialog
import com.flightrisk.app.ui.search.BoundingBox
import androidx.compose.runtime.collectAsState
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import com.flightrisk.app.config.FlightRiskConfig
import com.flightrisk.app.config.SensitivityPreset
import com.flightrisk.app.drone.DroneManager
import com.flightrisk.app.drone.FrameSourceMode
import com.flightrisk.app.drone.PatternType
import com.flightrisk.app.drone.TelloState
import com.flightrisk.app.drone.TelloWifiChecker

import com.flightrisk.app.alert.AlertManager
import com.flightrisk.app.camera.CameraXFrameSource
import com.flightrisk.app.config.AppSecrets
import com.flightrisk.app.llm.CloudClaudeLlmBackend
import com.flightrisk.app.llm.CloudReasoningGate
import com.flightrisk.app.llm.LocalLlmBackend
import com.flightrisk.app.llm.LlmSelector
import com.flightrisk.app.llm.ReasoningAction
import com.flightrisk.app.llm.ReasoningController
import com.flightrisk.app.llm.ReasoningProvider
import com.flightrisk.app.llm.ReasoningSettings
import com.flightrisk.app.llm.SecretStoreClaudeKeySource
import com.flightrisk.app.llm.local.GemmaMediaPipeProvider
import com.flightrisk.app.llm.local.LocalLlmProvider
import com.flightrisk.app.llm.local.ModelState
import com.flightrisk.app.location.LocationProvider
import com.flightrisk.app.persistence.SessionRepository
import com.flightrisk.app.privacy.PrivacyNoticeGate
import com.flightrisk.app.privacy.PrivacyPrefs
import com.flightrisk.app.privacy.RecordingPolicy
import com.flightrisk.app.privacy.SharedPrefsPrivacyPrefs
import com.flightrisk.app.ui.privacy.PrivacyNoticeScreen
import com.flightrisk.app.pipeline.SearchPipeline
import com.flightrisk.app.transport.GroundLink
import com.flightrisk.app.recording.SessionRecorder
import com.flightrisk.app.vision.Detection
import com.flightrisk.app.vision.FaceRecognizer
import com.flightrisk.app.vision.PersonDetector
import com.flightrisk.app.vision.PersonReID

import com.flightrisk.app.ui.navigation.FlightRiskNavHost
import com.flightrisk.app.ui.onboarding.OnboardingScreen
import com.flightrisk.app.ui.onboarding.isOnboardingComplete
import com.flightrisk.app.ui.quality.QualityReport
import com.flightrisk.app.ui.search.SearchScreenState
import com.flightrisk.app.ui.search.searchReadiness
import com.flightrisk.app.ui.settings.SettingsScreenState
import com.flightrisk.app.ui.theme.FlightRiskTheme
import androidx.lifecycle.lifecycleScope
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
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
        private const val MAX_DRONE_CONNECT_ATTEMPTS = 3
        private const val DRONE_RETRY_DELAY_MS = 2000L
        private const val DEMO_FPS = 12f
        private const val DEMO_SCORE = 0.41f
    }

    // ------------------------------------------------------------------
    // UI state
    // ------------------------------------------------------------------

    private var onboardingComplete by mutableStateOf(false)
    private lateinit var privacyPrefs: PrivacyPrefs
    private var noticeAcknowledged by mutableStateOf(false)
    private var searchState by mutableStateOf(SearchScreenState())
    private var settingsState by mutableStateOf(SettingsScreenState())
    private var targetBitmap by mutableStateOf<Bitmap?>(null)
    private var targetQualityReport by mutableStateOf<QualityReport?>(null)

    // Demo mode. The ground ViewModel owns it; this activity follows it for the Search and Target tabs.
    // The real target is kept aside while the demo shows its drawn placeholder and comes back after.
    private var demoActive by mutableStateOf(false)
    private var demoDialog by mutableStateOf<DemoDialog?>(null)
    private var realTargetBitmap: Bitmap? = null
    private var realTargetQualityReport: QualityReport? = null

    private sealed interface DemoDialog {
        data class ConfirmDisconnect(val host: String?) : DemoDialog
        data class Blocked(val text: String) : DemoDialog
    }

    /**
     * Whether the device has a camera the search preview can use. The preview binds the
     * back or the front camera only, so a device whose only camera is an external one
     * (FEATURE_CAMERA_EXTERNAL, which FEATURE_CAMERA_ANY also reports) counts as having
     * none. The manifest does not require a camera, so the app also installs on devices
     * without one, which can search with a drone.
     */
    private val deviceHasCamera: Boolean by lazy {
        packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA) ||
            packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA_FRONT)
    }

    // Ground console (U3). One block: the ViewModel is owned by the activity, so it outlives tab
    // switches, and it follows the activity between foreground and background. It connects only
    // when the user saved an address and a key and switched the link on.
    private val groundConsoleViewModel: GroundConsoleViewModel by viewModels {
        GroundConsoleViewModel.factory(applicationContext)
    }

    init {
        lifecycle.addObserver(
            LifecycleEventObserver { _, event ->
                when (event) {
                    Lifecycle.Event.ON_START -> groundConsoleViewModel.onForeground()
                    Lifecycle.Event.ON_STOP -> if (!isChangingConfigurations) groundConsoleViewModel.onBackground()
                    else -> Unit
                }
            },
        )
    }

    // Drone state
    private var droneManager: DroneManager? = null
    private var currentDroneState by mutableStateOf<TelloState?>(null)
    private var frameSourceMode by mutableStateOf(FrameSourceMode.CAMERA)
    private var latestDroneFrame by mutableStateOf<Bitmap?>(null)
    private var droneStateJob: Job? = null
    private var droneAlertJob: Job? = null
    private var selectedSearchPattern by mutableStateOf(PatternType.EXPANDING_SQUARE)

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
    private var sessionRepository: SessionRepository? = null
    private var sessionRecorder: SessionRecorder? = null

    // Local LLM state
    private var localLlmProvider: LocalLlmProvider? = null
    private var localLlmBackend: LocalLlmBackend? = null
    private var localModelStateJob: Job? = null

    // Reasoning (U5): cloud is opt-in, off by default; the Claude key lives only in SecretStore.
    private var cloudLlmBackend: CloudClaudeLlmBackend? = null
    private var reasoningController: ReasoningController? = null
    private var reasoningStateJob: Job? = null


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

        privacyPrefs = SharedPrefsPrivacyPrefs(applicationContext)
        noticeAcknowledged = !PrivacyNoticeGate.shouldShowNotice(privacyPrefs)
        onboardingComplete = isOnboardingComplete(this)
        searchState = searchState.copy(hasCamera = deviceHasCamera)

        // Load initial settings from config. No secret is read here.
        val config = FlightRiskConfig.getInstance(this)
        settingsState = SettingsScreenState(
            activePreset = SensitivityPreset.BALANCED,
            reidThreshold = config.vision.reidThreshold.toFloat(),
            faceThreshold = config.vision.faceMatchThreshold.toFloat(),
            scorerThreshold = config.vision.scorerMatchThreshold.toFloat(),
        )

        // Initialize alert and location subsystems
        alertManager = AlertManager(applicationContext)
        locationProvider = LocationProvider(applicationContext)

        // Reasoning. Cloud Claude is used only while the saved opt-in is on,
        // Cloud Claude is selected and a key is saved in SecretStore; the cloud
        // backend checks that itself on every call (CloudReasoningGate). The
        // on-device backend is registered first, and the cloud backend is never
        // a fallback.
        val provider = GemmaMediaPipeProvider(applicationContext)
        localLlmProvider = provider
        val localBackend = LocalLlmBackend(provider).also { localLlmBackend = it }
        val reasoningSettings = ReasoningSettings(applicationContext)
        val secretStore = AppSecrets.store(applicationContext)
        val cloudGate = CloudReasoningGate(reasoningSettings, SecretStoreClaudeKeySource(secretStore))
        val cloudBackend = CloudClaudeLlmBackend.forDevice(applicationContext, cloudGate).also { cloudLlmBackend = it }
        val selector = LlmSelector(
            settings = reasoningSettings,
            gate = cloudGate,
            localModelUsable = {
                val state = provider.state.value
                state is ModelState.Ready || state is ModelState.Downloaded
            },
            isOnline = { CloudClaudeLlmBackend.hasValidatedInternet(applicationContext) },
            context = applicationContext,
        )
        selector.registerOnDeviceFirst(local = localBackend, cloud = cloudBackend)
        selector.startMonitoring()
        llmSelector = selector
        val controller = ReasoningController(reasoningSettings, cloudGate, selector, lifecycleScope)
        reasoningController = controller

        // The Claude key moves once from FLIGHTRISK_API_KEY to FLIGHTRISK_CLAUDE_API_KEY
        // (write new, then delete old), off the main thread. It never turns cloud on.
        lifecycleScope.launch {
            controller.start { reasoningSettings.migrateLegacyKeyOnce(secretStore) }
        }

        // Settings and the Search banner follow the resolved reasoning state.
        reasoningStateJob = lifecycleScope.launch {
            controller.ui.collect { ui ->
                settingsState = settingsState.copy(reasoning = ui)
                searchState = searchState.copy(
                    cloudReasoningActive = ui.isCloudActive,
                    reasoningNotice = ui.status.notice,
                )
            }
        }

        // Observe local model state for settings UI and re-resolve the active backend.
        localModelStateJob = lifecycleScope.launch {
            provider.state.collect { modelState ->
                settingsState = settingsState.copy(
                    localModelState = modelState,
                    localModelName = provider.modelInfo.displayName,
                    localModelSizeMb = (provider.modelInfo.sizeBytes / 1_000_000).toInt(),
                )
                selector.refresh()
            }
        }

        // Demo mode: the Search and Target tabs follow the ground console's demo state.
        lifecycleScope.launch {
            groundConsoleViewModel.uiState.map { it.demo }.distinctUntilChanged().collect { applyDemo(it) }
        }
        lifecycleScope.launch {
            // A refusal (the real ground needs the operator, or the mission could not be loaded) or a request
            // to confirm the link switch-off: shown as a dialog, never dropped silently.
            groundConsoleViewModel.uiState.map { it.demoNotice }.distinctUntilChanged().collect { notice ->
                when (notice) {
                    is GroundConsoleViewModel.DemoNotice.ConfirmDisconnect -> demoDialog = DemoDialog.ConfirmDisconnect(notice.host)
                    is GroundConsoleViewModel.DemoNotice.Blocked -> demoDialog = DemoDialog.Blocked(notice.text)
                    null -> Unit
                }
                if (notice != null) groundConsoleViewModel.dismissDemoNotice()
            }
        }

        // Ground station link (phone as an edge). Off until the user turns it on in
        // Settings; it connects only while a search runs and the app is in view.
        lifecycle.addObserver(GroundLink.controller(applicationContext).lifecycleObserver)

        // The camera and location prompts wait for the privacy notice. When the
        // notice is not acknowledged yet, they are requested after "I Understand".
        if (PrivacyNoticeGate.mayRequestPermissions(privacyPrefs)) {
            requestPermissionsIfNeeded()
        }

        setContent {
            FlightRiskTheme {
                val groundUi by groundConsoleViewModel.uiState.collectAsState()
                // The demo banner sits above every screen and pushes it down, so it never covers LAND ALL.
                DemoFrame(demo = groundUi.demo) {
                    if (!noticeAcknowledged) {
                        PrivacyNoticeScreen(
                            onAccept = {
                                PrivacyNoticeGate.acknowledge(privacyPrefs)
                                noticeAcknowledged = true
                                requestPermissionsIfNeeded()
                            },
                        )
                    } else if (!onboardingComplete && !groundUi.demo) {
                        OnboardingScreen(
                            onComplete = { bitmap ->
                                targetBitmap = bitmap
                                onboardingComplete = true
                            },
                            // "Set up a search later": the tabs open with no target photo, and
                            // Search shows "No search target yet" until one is chosen.
                            onSkip = { onboardingComplete = true },
                            onTryDemo = ::requestDemo,
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
                            onReasoningAction = ::handleReasoningAction,
                            onLocalModelDownload = ::handleLocalModelDownload,
                            onLocalModelDelete = ::handleLocalModelDelete,
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
                            selectedSearchPattern = selectedSearchPattern,
                            onSearchPatternChanged = { selectedSearchPattern = it },
                            demoActive = groundUi.demo,
                            onDemoToggle = { on -> if (on) requestDemo() else groundConsoleViewModel.exitDemo() },
                        )
                    }
                }
                when (val dialog = demoDialog) {
                    is DemoDialog.ConfirmDisconnect -> DemoDisconnectDialog(
                        host = dialog.host,
                        onConfirm = {
                            demoDialog = null
                            // Checked again: the ground ViewModel re-checks its own refusals before it starts.
                            if (demoBlockedHere()) {
                                demoDialog = DemoDialog.Blocked(DemoText.BLOCKED_BY_SEARCH)
                            } else {
                                groundConsoleViewModel.enterDemo(disconnectConfirmed = true)
                            }
                        },
                        onDismiss = { demoDialog = null },
                    )
                    is DemoDialog.Blocked -> DemoBlockedDialog(dialog.text, onDismiss = { demoDialog = null })
                    null -> Unit
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // Demo mode
    // ------------------------------------------------------------------

    /**
     * "Try demo" on the first onboarding screen and the Settings switch. Demo mode never starts while a
     * search runs or a drone is connected here. The ground ViewModel decides the rest: it refuses while the
     * real ground's state needs the operator, and it asks (through its demo notice) before it switches a
     * real ground link off.
     */
    private fun requestDemo() {
        if (demoBlockedHere()) {
            demoDialog = DemoDialog.Blocked(DemoText.BLOCKED_BY_SEARCH)
            return
        }
        groundConsoleViewModel.enterDemo(disconnectConfirmed = false)
    }

    /** A search runs or a Tello drone is connected on the Search tab. */
    private fun demoBlockedHere(): Boolean = searchState.isSearching || droneManager != null

    /** Follows the demo switch: the drawn placeholder target in, the real one out, and back again. */
    private fun applyDemo(on: Boolean) {
        if (on == demoActive) return
        demoActive = on
        if (on) {
            realTargetBitmap = targetBitmap
            realTargetQualityReport = targetQualityReport
            targetBitmap = DemoImages.target()
            targetQualityReport = null
            searchState = searchState.copy(
                isSearching = false, demoMode = true, boxes = emptyList(), cameraFrame = null, activeAlert = null,
                fps = 0f, personsDetected = 0, highestMatchScore = 0f, droneConnectionMessage = null,
            )
        } else {
            // Nothing the demo showed stays: its target, its scene and its messages go.
            targetBitmap = realTargetBitmap
            targetQualityReport = realTargetQualityReport
            realTargetBitmap = null
            realTargetQualityReport = null
            searchState = searchState.copy(
                isSearching = false, demoMode = false, boxes = emptyList(), cameraFrame = null, activeAlert = null,
                fps = 0f, personsDetected = 0, highestMatchScore = 0f, droneConnectionMessage = null,
            )
        }
    }

    /** The demo's search: a drawn scene with one detection. No camera, model, drone or network is used. */
    private fun startDemoSearch() {
        searchState = searchState.copy(
            isSearching = true,
            fps = DEMO_FPS,
            personsDetected = 1,
            highestMatchScore = DEMO_SCORE,
            cameraFrame = DemoImages.scene(),
            frameWidth = DemoArt.SCENE_WIDTH,
            frameHeight = DemoArt.SCENE_HEIGHT,
            boxes = listOf(BoundingBox(DemoArt.SCENE_FIGURE_BOX.copyOf(), trackId = 1, isMatch = false)),
        )
    }

    private fun demoDroneCommand() {
        searchState = searchState.copy(droneConnectionMessage = DemoText.DRONE_NOT_SENT)
    }

    // ------------------------------------------------------------------
    // Permission handling
    // ------------------------------------------------------------------

    private fun requestPermissionsIfNeeded() {
        val permissionsToRequest = mutableListOf<String>()

        // A device without a camera is not asked for the camera permission.
        if (deviceHasCamera &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
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
        if (demoActive) return startDemoSearch()
        // Without a camera (and no drone) there is nothing to search with. The Search
        // screen disables Start and says why; this also covers the drone flight
        // controls, which have their own Start button. A search without a target
        // photo may start: it detects people (and sends them to the ground station
        // when that link is on) but cannot match anyone, which the screen says.
        val readiness = searchReadiness(
            hasTarget = targetBitmap != null,
            hasCamera = deviceHasCamera,
            frameSourceMode = frameSourceMode,
        )
        if (!readiness.canStart) {
            Log.i(TAG, "Search not started: $readiness")
            return
        }

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
                } catch (e: LinkageError) {
                    // The native runtime did not load (UnsatisfiedLinkError, or
                    // ExceptionInInitializerError from the library's static init). Search
                    // goes on without this model, as when the model file is missing.
                    Log.w(TAG, "PersonDetector: native library did not load", e)
                }
            }

            if (personReID == null) {
                try {
                    personReID = PersonReID(ctx)
                    Log.i(TAG, "PersonReID loaded")
                } catch (e: Exception) {
                    Log.w(TAG, "PersonReID failed to load (CLIP model missing?)", e)
                } catch (e: LinkageError) {
                    // The native runtime did not load (UnsatisfiedLinkError, or
                    // ExceptionInInitializerError from the library's static init). Search
                    // goes on without this model, as when the model file is missing.
                    Log.w(TAG, "PersonReID: native library did not load", e)
                }
            }

            if (faceRecognizer == null) {
                try {
                    faceRecognizer = FaceRecognizer(ctx)
                    Log.i(TAG, "FaceRecognizer loaded")
                } catch (e: Exception) {
                    Log.w(TAG, "FaceRecognizer failed to load (SCRFD/ArcFace models missing?)", e)
                } catch (e: LinkageError) {
                    // The native runtime did not load (UnsatisfiedLinkError, or
                    // ExceptionInInitializerError from the library's static init). Search
                    // goes on without this model, as when the model file is missing.
                    Log.w(TAG, "FaceRecognizer: native library did not load", e)
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
            val ls = llmSelector ?: return@launch
            val lp = locationProvider ?: LocationProvider(ctx).also {
                locationProvider = it
            }

            // Bail out if user stopped search while we were loading models
            if (!searchState.isSearching) return@launch

            val repo = sessionRepository ?: SessionRepository(ctx).also { sessionRepository = it }
            val recordingsDir = java.io.File(ctx.filesDir, "recordings")
            // Video is recorded only if the person turned on "Record search video".
            val recorder = if (RecordingPolicy.shouldRecord(privacyPrefs)) {
                SessionRecorder(recordingsDir)
            } else {
                null
            }
            sessionRecorder = recorder

            val pipeline = SearchPipeline(config, ls, am, lp, repo, recorder)
            searchPipeline = pipeline
            pipeline.detectionPublisher = GroundLink.controller(ctx).detectionPublisher

            // Adapt scorer weights based on active LLM backend
            // Wait (briefly) for the saved-key check after start-up, so the weights
            // follow the backend that will actually answer.
            val reasoning = ls.awaitKeyChecked()
            pipeline.adaptWeightsForBackend(reasoning.active == ReasoningProvider.LOCAL)

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
                            // Pause drone search and hover when match detected
                            if (frameSourceMode == FrameSourceMode.DRONE) {
                                droneManager?.pauseSearchForMatch()
                            }
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

            // The user may have stopped the search while the models loaded; a cancelled
            // setup must not start the pipeline (it would turn the ground link on).
            ensureActive()

            // Start the pipeline
            pipeline.start()

            // Auto-takeoff and start search pattern if drone is connected
            if (currentFrameSourceMode == FrameSourceMode.DRONE) {
                val manager = droneManager
                if (manager != null) {
                    try {
                        val alreadyFlying = manager.droneState.value.telemetry.isFlying
                        if (!alreadyFlying) {
                            Log.i(TAG, "Auto-takeoff: sending takeoff command")
                            val success = manager.takeoff()
                            if (success) {
                                Log.i(TAG, "Auto-takeoff: success, waiting for stabilization")
                                delay(3000)
                            } else {
                                Log.e(TAG, "Auto-takeoff: takeoff command failed")
                            }
                        } else {
                            Log.i(TAG, "Auto-takeoff: drone already flying")
                        }
                        manager.startSearchPattern(selectedSearchPattern)
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: Exception) {
                        Log.e(TAG, "Auto-takeoff/search failed: ${e.message}", e)
                    }
                } else {
                    Log.w(TAG, "Auto-takeoff: droneManager is null")
                }
            }

            Log.i(TAG, "Search started, frameSource=$frameSourceMode")
        }
    }

    private fun handleStopSearch() {
        if (demoActive) {
            searchState = searchState.copy(isSearching = false, boxes = emptyList(), cameraFrame = null, fps = 0f, personsDetected = 0, highestMatchScore = 0f)
            return
        }
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
        searchPipeline?.resumeAfterMatch()
        droneManager?.resumeSearch()
    }

    private fun handleDismissDroneAlert() {
        searchState = searchState.copy(droneAlert = null)
    }

    private fun handleNotMyChild() {
        val alert = searchState.activeAlert
        alertManager?.dismissAll()
        val trackKey = alert?.trackId
        Log.i(TAG, "Not my child: track=$trackKey")
        if (trackKey != null) {
            searchPipeline?.suppressTrack(trackKey)
        }
        searchState = searchState.copy(activeAlert = null)
        searchPipeline?.resumeAfterMatch()
        droneManager?.resumeSearch()
    }

    // ------------------------------------------------------------------
    // Target photo callback
    // ------------------------------------------------------------------

    private fun handlePhotoSelected(bitmap: Bitmap, report: QualityReport) {
        // The Target tab offers no picker in demo mode; a late result must not replace the drawn placeholder.
        if (demoActive) {
            Log.i(TAG, "Target photo ignored: demo mode is on")
            return
        }
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

    /**
     * Reasoning actions from Settings. The consent decisions, the opt-in and
     * the key (SecretStore, off the main thread) are handled by
     * [ReasoningController]; the selector refresh it ends with updates the
     * "Cloud active" indicator and the fallback notice.
     */
    private fun handleReasoningAction(action: ReasoningAction) {
        reasoningController?.onAction(action)
        Log.i(TAG, "Reasoning action: ${action.javaClass.simpleName}")
    }

    // ------------------------------------------------------------------
    // Local model callbacks
    // ------------------------------------------------------------------

    private fun handleLocalModelDownload() {
        val provider = localLlmProvider ?: return
        lifecycleScope.launch {
            try {
                provider.download { progress ->
                    settingsState = settingsState.copy(
                        localModelState = ModelState.Downloading(progress),
                    )
                }
            } catch (e: Exception) {
                Log.e(TAG, "Local model download failed", e)
            }
        }
    }

    private fun handleLocalModelDelete() {
        val provider = localLlmProvider ?: return
        lifecycleScope.launch {
            try {
                provider.delete()
            } catch (e: Exception) {
                Log.e(TAG, "Local model delete failed", e)
            }
        }
    }

    // ------------------------------------------------------------------
    // Drone callbacks
    // ------------------------------------------------------------------

    private fun handleDroneConnect() {
        if (demoActive) return demoDroneCommand()
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
            var success = false
            for (attempt in 1..MAX_DRONE_CONNECT_ATTEMPTS) {
                Log.i(TAG, "Drone connect attempt $attempt/$MAX_DRONE_CONNECT_ATTEMPTS")
                success = manager.connectAndStream()
                if (success) break
                if (attempt < MAX_DRONE_CONNECT_ATTEMPTS) {
                    Log.i(TAG, "Retrying drone connection in ${DRONE_RETRY_DELAY_MS}ms...")
                    delay(DRONE_RETRY_DELAY_MS)
                    if (droneManager == null) return@launch
                }
            }
            if (!success) {
                val wifiStatus = manager.wifiChecker.check()
                val droneError = manager.droneState.value.errorMessage
                val message = when {
                    wifiStatus !is TelloWifiChecker.WifiStatus.OnTelloWifi ->
                        manager.wifiChecker.getGuidanceMessage(wifiStatus)
                    droneError != null ->
                        droneError
                    else ->
                        "Connection failed after $MAX_DRONE_CONNECT_ATTEMPTS attempts. " +
                            "Make sure the Tello is powered on and try again."
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
                        is DroneManager.DroneAlert.BatteryWarning -> {
                            Log.w(TAG, "Drone alert: battery warning ${alert.percent}%")
                            searchState = searchState.copy(
                                droneAlert = "Battery low: ${alert.percent}% — consider landing soon",
                            )
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
                        is DroneManager.DroneAlert.ObstacleDetected -> {
                            Log.w(TAG, "Drone alert: obstacle, action=${alert.action}, depth=${alert.centerDepth}")
                        }
                        is DroneManager.DroneAlert.MatchPause -> {
                            Log.i(TAG, "Drone alert: search paused for match inspection")
                        }
                    }
                }
            }

            Log.i(TAG, "Drone connected and streaming")
        }
    }

    private fun handleDroneDisconnect() {
        if (demoActive && droneManager == null) return demoDroneCommand()
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

    // In demo mode no drone is connected here (demo mode refuses to start while one is), so these only
    // say that nothing was sent. A real drone's Land is never gated by demo mode.
    private fun handleTakeoff() {
        if (demoActive) return demoDroneCommand()
        lifecycleScope.launch { droneManager?.takeoff() }
    }

    private fun handleLand() {
        if (demoActive && droneManager == null) return demoDroneCommand()
        lifecycleScope.launch { droneManager?.land() }
    }

    private fun handleEmergencyStop() {
        if (demoActive && droneManager == null) return demoDroneCommand()
        lifecycleScope.launch { droneManager?.emergencyStop() }
    }

    private fun handleDroneMove(direction: String, distanceCm: Int) {
        if (demoActive) return demoDroneCommand()
        lifecycleScope.launch { droneManager?.move(direction, distanceCm) }
    }

    private fun handleDroneRotate(degrees: Int) {
        if (demoActive) return demoDroneCommand()
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

        // Clean up reasoning: cancel any cloud request still in flight.
        reasoningStateJob?.cancel()
        reasoningStateJob = null
        cloudLlmBackend?.close()
        cloudLlmBackend = null
        llmSelector?.stopMonitoring()
        reasoningController = null

        // Clean up local LLM
        localModelStateJob?.cancel()
        localModelStateJob = null
        lifecycleScope.launch { localLlmProvider?.unload() }
        localLlmProvider = null
        localLlmBackend = null

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
