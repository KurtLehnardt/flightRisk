import SwiftUI

@main
struct FlightRiskApp: App {
    @State private var viewModel = SearchViewModel()
    @State private var gemmaModelManager = GemmaModelManager()
    private let config = FlightRiskConfig.shared

    // Pipeline dependencies — @State for reference types with mutable state
    // so SwiftUI manages their lifecycle correctly across view updates.
    @State private var llmSelector: LlmSelector
    @State private var alertManager: AlertManager
    @State private var locationProvider: LocationProvider
    @State private var personDetector: PersonDetector
    @State private var personReID: PersonReID
    @State private var frameSource = AVCaptureFrameSource()
    @State private var pipeline: SearchPipeline
    @State private var targetNotice: String?

    init() {
        let config = FlightRiskConfig.shared

        // Create vision components with config thresholds. Matching is by appearance only:
        // no face detection or face recognition model is loaded.
        let reid = PersonReID(threshold: config.vision.reidThreshold)
        _personReID = State(initialValue: reid)

        // Create pipeline dependencies — each created once
        let selector = LlmSelector()
        _llmSelector = State(initialValue: selector)
        let alert = AlertManager()
        _alertManager = State(initialValue: alert)
        let location = LocationProvider()
        _locationProvider = State(initialValue: location)
        // The bundled YOLOX-S, or the user's own detector while one is imported and switched on.
        let detector = PersonDetector(customStore: CustomDetectorStore.shared)
        _personDetector = State(initialValue: detector)

        // Create pipeline with the same instances
        let pipeline = SearchPipeline(
            config: config,
            llmSelector: selector,
            alertManager: alert,
            locationProvider: location
        )
        _pipeline = State(initialValue: pipeline)
    }

    var body: some Scene {
        WindowGroup {
            ContentView(
                viewModel: viewModel,
                cameraSession: frameSource.session,
                config: config,
                gemmaModelManager: gemmaModelManager,
                pipeline: pipeline,
                llmSelector: llmSelector
            )
            .task {
                await setupPipeline()
            }
        }
    }

    private func setupPipeline() async {
        // PersonDetector loaded its model in init. If the model is missing or
        // broken, say so on the Search tab instead of searching silently.
        refreshBanner()
        personDetector.preload()

        // The appearance embedder needs an explicit load.
        personReID.loadModel()

        // Set pipeline callbacks
        await pipeline.wireCallbacks(
            detection: personDetector,
            reid: personReID
        )

        // Set frame source for the pipeline
        await pipeline.setFrameSource(frameSource)

        // Publish every reasoning status change (including whether cloud
        // reasoning is active) to the views.
        await llmSelector.setStatusHandler { status in
            Task { @MainActor in
                ReasoningActivity.shared.update(status)
            }
        }

        // Register LLM backends: on-device Gemma first, cloud Claude second.
        // Claude starts without a key; the selector hands it the Keychain key
        // only while the user's explicit cloud opt-in is on.
        let gemmaBackend = LocalGemmaLlmBackend(modelManager: gemmaModelManager)
        let claudeBackend = CloudClaudeLlmBackend()
        await llmSelector.registerOnDeviceFirst(local: gemmaBackend, cloud: claudeBackend)
        await llmSelector.refreshAsync()

        // Start connectivity monitoring
        await llmSelector.startMonitoring()

        // Connect viewModel to pipeline
        await MainActor.run {
            viewModel.observe(pipeline: pipeline)
        }

        // Set initial backend status on the view model
        viewModel.updateBackendStatus(from: llmSelector)

        // Importing, switching or removing the user's own detector can turn detection on or off.
        // Dropping the user's detector after repeated failures, or a photo that cannot be used,
        // also show on that banner.
        Task { @MainActor in
            for await _ in NotificationCenter.default.notifications(named: PersonDetector.customDetectorFailed) {
                refreshBanner()
            }
        }
        Task { @MainActor in
            for await note in NotificationCenter.default.notifications(named: SearchPipeline.targetPhotoRejected) {
                targetNotice = note.userInfo?["message"] as? String
                refreshBanner()
            }
        }
        for await _ in NotificationCenter.default.notifications(named: CustomDetectorStore.didChange) {
            personDetector.preload()
            refreshBanner()
        }
    }

    /// Puts the most important message on the Search tab banner: detection off, then a dropped
    /// custom detector, then a photo that could not be used.
    @MainActor
    private func refreshBanner() {
        viewModel.detectionUnavailableReason = personDetector.unavailableReason
            ?? personDetector.customFailureNotice
            ?? targetNotice
        viewModel.isUsingCustomDetector = personDetector.isUsingCustomModel
    }
}

// MARK: - Pipeline Wiring Extension

extension SearchPipeline {
    /// Set both vision callbacks atomically within the actor's isolation.
    func wireCallbacks(
        detection: PipelineDetectionCallback,
        reid: PipelineReidCallback
    ) {
        self.detectionCallback = detection
        self.reidCallback = reid
    }

    /// Set the frame source for camera frame acquisition.
    func setFrameSource(_ source: FrameSource) {
        self.frameSource = source
    }

    /// Set the target reference photo and compute its appearance embedding.
    ///
    /// When the photo cannot be used, no target is kept and the Search tab banner says so.
    @discardableResult
    func setTargetPhoto(_ image: CGImage) -> Bool {
        guard reidCallback?.setTarget(photo: image) == true else {
            self.targetPhoto = nil
            let message = SearchPipeline.targetPhotoRejectedMessage
            DispatchQueue.main.async {
                NotificationCenter.default.post(name: SearchPipeline.targetPhotoRejected, object: nil,
                                                userInfo: ["message": message])
            }
            return false
        }
        self.targetPhoto = image
        return true
    }

    static let targetPhotoRejected = Notification.Name("FlightRiskTargetPhotoRejected")
    static let targetPhotoRejectedMessage = "This photo could not be used for matching. Try another photo."
}
