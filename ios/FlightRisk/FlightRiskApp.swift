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
    @State private var faceRecognizer: FaceRecognizer
    @State private var frameSource = AVCaptureFrameSource()
    @State private var pipeline: SearchPipeline

    init() {
        let config = FlightRiskConfig.shared

        // Create vision components with config thresholds
        let reid = PersonReID(threshold: config.vision.reidThreshold)
        let face = FaceRecognizer(threshold: config.vision.faceMatchThreshold)
        _personReID = State(initialValue: reid)
        _faceRecognizer = State(initialValue: face)

        // Create pipeline dependencies — each created once
        let selector = LlmSelector()
        _llmSelector = State(initialValue: selector)
        let alert = AlertManager()
        _alertManager = State(initialValue: alert)
        let location = LocationProvider()
        _locationProvider = State(initialValue: location)
        let detector = PersonDetector()
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
                droneState: nil,
                frameSourceMode: .camera,
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
        // Load vision models (PersonDetector loads in init; ReID and Face need explicit load)
        _ = personReID.loadModel()
        _ = faceRecognizer.loadModels()

        // Set pipeline callbacks
        await pipeline.wireCallbacks(
            detection: personDetector,
            reid: personReID,
            face: faceRecognizer
        )

        // Set frame source for the pipeline
        await pipeline.setFrameSource(frameSource)

        // Register LLM backends
        // Cloud Claude (loaded from Keychain)
        let claudeBackend = CloudClaudeLlmBackend(
            apiKey: KeychainHelper.loadApiKey(provider: "cloud_claude") ?? ""
        )
        await llmSelector.registerBackend(claudeBackend)

        // Local Gemma
        let gemmaBackend = LocalGemmaLlmBackend(modelManager: gemmaModelManager)
        await llmSelector.registerBackend(gemmaBackend)

        // Start connectivity monitoring
        await llmSelector.startMonitoring()

        // Connect viewModel to pipeline
        await MainActor.run {
            viewModel.observe(pipeline: pipeline)
        }

        // Set initial backend status on the view model
        viewModel.updateBackendStatus(from: llmSelector)
    }
}

// MARK: - Pipeline Wiring Extension

extension SearchPipeline {
    /// Set all three vision callbacks atomically within the actor's isolation.
    func wireCallbacks(
        detection: PipelineDetectionCallback,
        reid: PipelineReidCallback,
        face: PipelineFaceCallback
    ) {
        self.detectionCallback = detection
        self.reidCallback = reid
        self.faceCallback = face
    }

    /// Set the frame source for camera frame acquisition.
    func setFrameSource(_ source: FrameSource) {
        self.frameSource = source
    }

    /// Set the target reference photo and compute ReID + face embeddings.
    func setTargetPhoto(_ image: CGImage) {
        self.targetPhoto = image
        _ = reidCallback?.setTarget(photo: image)
        _ = faceCallback?.setTarget(photo: image)
    }
}
