import SwiftUI

@main
struct FlightRiskApp: App {
    @State private var viewModel = SearchViewModel()
    @State private var gemmaModelManager = GemmaModelManager()
    private let config = FlightRiskConfig.shared

    // Pipeline dependencies
    private let llmSelector = LlmSelector()
    @State private var alertManager = AlertManager()
    private let locationProvider = LocationProvider()
    private let personDetector = PersonDetector()
    private let personReID: PersonReID
    private let faceRecognizer: FaceRecognizer
    private let pipeline: SearchPipeline

    init() {
        let config = FlightRiskConfig.shared

        // Create vision components with config thresholds
        let reid = PersonReID(threshold: config.vision.reidThreshold)
        let face = FaceRecognizer(threshold: config.vision.faceMatchThreshold)
        self.personReID = reid
        self.faceRecognizer = face

        // Create pipeline
        let llmSelector = self.llmSelector
        let alertManager = AlertManager()
        let locationProvider = self.locationProvider

        let pipeline = SearchPipeline(
            config: config,
            llmSelector: llmSelector,
            alertManager: alertManager,
            locationProvider: locationProvider
        )
        self.pipeline = pipeline
        self._alertManager = State(initialValue: alertManager)
    }

    var body: some Scene {
        WindowGroup {
            ContentView(
                viewModel: viewModel,
                cameraSession: .init(),
                config: config,
                droneState: nil,
                frameSourceMode: .camera,
                gemmaModelManager: gemmaModelManager
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
}
