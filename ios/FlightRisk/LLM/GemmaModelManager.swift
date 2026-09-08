import Foundation
import Network
import os
#if canImport(UIKit)
import UIKit
#endif
#if canImport(MLX)
import MLX
import MLXLLM
import MLXLMCommon
#endif

/// Manages the lifecycle of a local Gemma 2 model via MLX: download,
/// load, inference, and cleanup.
///
/// Wraps the MLXLLM framework so the rest of the app can treat the
/// local model as a simple async service. Heavy operations (download,
/// load, generate) are isolated inside the actor and will not block
/// the main thread.
///
/// All MLX-specific code is behind `#if canImport(MLX)` so the
/// project compiles on the iOS Simulator where MLX is unavailable.
actor GemmaModelManager: GemmaModelManaging {

    private let logger = Logger(subsystem: "com.flightrisk.app", category: "GemmaModelManager")

    // MARK: - State

    private(set) var state: GemmaDownloadState = .idle
    var isReady: Bool { state == .ready }
    private(set) var modelSizeBytes: UInt64? = nil
    static let modelId = "mlx-community/gemma-2-2b-it-4bit"

    // MARK: - MLX model references

    #if canImport(MLX)
    private var model: (any LanguageModel)?
    private var tokenizer: (any Tokenizer)?
    #endif

    // MARK: - Download state

    private var downloadTask: URLSessionDownloadTask?
    private var resumeData: Data?

    /// Minimum free disk space required for the model download (2 GB).
    private static let requiredDiskSpaceBytes: UInt64 = 2_000_000_000

    // MARK: - Directories

    private var modelDirectory: URL {
        let appSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first!
        return appSupport
            .appendingPathComponent("FlightRisk", isDirectory: true)
            .appendingPathComponent("models", isDirectory: true)
            .appendingPathComponent("gemma-2-2b-it-4bit", isDirectory: true)
    }

    private var resumeDataURL: URL {
        modelDirectory.appendingPathComponent(".resume_data")
    }

    // MARK: - Init / Deinit

    init() {
        setupNotifications()
        loadPersistedResumeData()
        detectExistingDownload()
    }

    // MARK: - GemmaModelManaging

    func download() async throws {
        guard state == .idle || state.isError else {
            logger.debug("Download called in state \(String(describing: self.state)); ignoring")
            return
        }

        // Disk space check
        try checkDiskSpace()

        // Cellular guard
        try checkNotCellular()

        state = .downloading(progress: 0)

        #if canImport(MLX)
        do {
            // Use MLXLLM built-in download via ModelConfiguration
            let config = ModelConfiguration(id: Self.modelId)
            let hub = HubApi()

            // Download to the default hub cache; MLXLLM handles
            // incremental / resumable downloads internally.
            logger.info("Starting model download: \(Self.modelId)")
            _ = try await hub.snapshot(from: config.name, matching: ["*.safetensors", "*.json", "tokenizer*"])

            // Calculate downloaded size
            modelSizeBytes = directorySize(modelDirectory)
            state = .downloaded
            clearPersistedResumeData()
            logger.info("Model download complete")
        } catch {
            state = .error(message: "Download failed: \(error.localizedDescription)")
            logger.error("Model download failed: \(error.localizedDescription)")
            throw error
        }
        #else
        state = .error(message: "MLX not available on this platform")
        throw GemmaError.modelNotAvailable
        #endif
    }

    func cancelDownload() async {
        downloadTask?.cancel()
        downloadTask = nil
        state = .idle
        logger.info("Download cancelled")
    }

    func loadModel() async throws {
        guard state == .downloaded || state == .idle else {
            if state == .ready {
                logger.debug("Model already loaded")
                return
            }
            logger.warning("Cannot load model in state \(String(describing: self.state))")
            return
        }

        state = .loading
        logger.info("Loading model into memory...")

        #if canImport(MLX)
        do {
            let config = ModelConfiguration(id: Self.modelId)
            let container = try await LLMModelFactory.shared.loadContainer(configuration: config)
            self.model = container.model
            self.tokenizer = container.tokenizer
            state = .ready
            logger.info("Model loaded and ready")
        } catch {
            state = .error(message: "Load failed: \(error.localizedDescription)")
            logger.error("Model load failed: \(error.localizedDescription)")
            throw error
        }
        #else
        state = .error(message: "MLX not available on this platform")
        throw GemmaError.modelNotAvailable
        #endif
    }

    func unloadModel() async {
        #if canImport(MLX)
        model = nil
        tokenizer = nil
        MLX.GPU.clearCache()
        #endif

        if state == .ready || state == .loading {
            state = .downloaded
        }

        logger.info("Model unloaded")
    }

    func deleteModel() async throws {
        await unloadModel()

        let fm = FileManager.default
        if fm.fileExists(atPath: modelDirectory.path) {
            try fm.removeItem(at: modelDirectory)
            logger.info("Model directory removed")
        }

        clearPersistedResumeData()
        modelSizeBytes = nil
        state = .idle
        logger.info("Model deleted")
    }

    func generate(prompt: String, maxTokens: Int) async throws -> String {
        guard state == .ready else {
            throw GemmaError.modelNotAvailable
        }

        #if canImport(MLX)
        guard let model, let tokenizer else {
            throw GemmaError.modelNotAvailable
        }

        let input = MLXLMInput(tokens: MLXArray(tokenizer.encode(text: prompt)))
        var output = [Int]()

        for try await token in try MLXLMCommon.generate(input: input, model: model, tokenizer: tokenizer) {
            output.append(token.tokens.first ?? 0)
            if output.count >= maxTokens {
                break
            }
        }

        MLX.GPU.clearCache()

        let text = tokenizer.decode(tokens: output)
        return text
        #else
        throw GemmaError.modelNotAvailable
        #endif
    }

    // MARK: - Disk Space

    private func checkDiskSpace() throws {
        let fm = FileManager.default
        let homeDir = fm.urls(for: .documentDirectory, in: .userDomainMask).first!
        if let values = try? homeDir.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
           let available = values.volumeAvailableCapacityForImportantUsage {
            let availableBytes = UInt64(available)
            if availableBytes < Self.requiredDiskSpaceBytes {
                throw GemmaError.insufficientDiskSpace(
                    available: availableBytes,
                    required: Self.requiredDiskSpaceBytes
                )
            }
        }
    }

    // MARK: - Cellular Guard

    private func checkNotCellular() throws {
        let semaphore = DispatchSemaphore(value: 0)
        var isCellular = false

        let monitor = NWPathMonitor()
        let queue = DispatchQueue(label: "com.flightrisk.app.cellularCheck")
        monitor.pathUpdateHandler = { path in
            isCellular = path.usesInterfaceType(.cellular)
            semaphore.signal()
            monitor.cancel()
        }
        monitor.start(queue: queue)

        // Wait briefly for the path update
        _ = semaphore.wait(timeout: .now() + 2)

        if isCellular {
            throw GemmaError.cellularDownloadBlocked
        }
    }

    // MARK: - Resume Data Persistence

    private func loadPersistedResumeData() {
        let fm = FileManager.default
        let url = resumeDataURL
        if fm.fileExists(atPath: url.path) {
            resumeData = try? Data(contentsOf: url)
            if resumeData != nil {
                logger.debug("Loaded persisted resume data")
            }
        }
    }

    private func persistResumeData(_ data: Data) {
        let fm = FileManager.default
        let dirURL = modelDirectory
        try? fm.createDirectory(at: dirURL, withIntermediateDirectories: true)
        try? data.write(to: resumeDataURL)
        logger.debug("Persisted resume data")
    }

    private func clearPersistedResumeData() {
        resumeData = nil
        try? FileManager.default.removeItem(at: resumeDataURL)
    }

    // MARK: - Existing Download Detection

    private func detectExistingDownload() {
        let fm = FileManager.default
        if fm.fileExists(atPath: modelDirectory.path) {
            let contents = (try? fm.contentsOfDirectory(atPath: modelDirectory.path)) ?? []
            let hasSafetensors = contents.contains { $0.hasSuffix(".safetensors") }
            if hasSafetensors {
                modelSizeBytes = directorySize(modelDirectory)
                state = .downloaded
                logger.info("Found existing model download")
            }
        }
    }

    // MARK: - Notifications

    private nonisolated func setupNotifications() {
        #if canImport(UIKit)
        NotificationCenter.default.addObserver(
            forName: UIApplication.didReceiveMemoryWarningNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            guard let self else { return }
            Task { await self.handleMemoryWarning() }
        }
        #endif

        NotificationCenter.default.addObserver(
            forName: ProcessInfo.thermalStateDidChangeNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            guard let self else { return }
            Task { await self.handleThermalChange() }
        }
    }

    private func handleMemoryWarning() async {
        logger.warning("Memory warning received — unloading model")
        await unloadModel()
    }

    private func handleThermalChange() async {
        let thermalState = ProcessInfo.processInfo.thermalState
        if thermalState >= .serious {
            logger.warning("Thermal state \(String(describing: thermalState)) — unloading model")
            await unloadModel()
        }
    }

    // MARK: - Utilities

    private func directorySize(_ url: URL) -> UInt64 {
        let fm = FileManager.default
        guard let enumerator = fm.enumerator(
            at: url,
            includingPropertiesForKeys: [.fileSizeKey],
            options: [.skipsHiddenFiles]
        ) else {
            return 0
        }

        var total: UInt64 = 0
        for case let fileURL as URL in enumerator {
            if let values = try? fileURL.resourceValues(forKeys: [.fileSizeKey]),
               let size = values.fileSize {
                total += UInt64(size)
            }
        }
        return total
    }
}

// MARK: - GemmaDownloadState Helpers

private extension GemmaDownloadState {
    var isError: Bool {
        if case .error = self { return true }
        return false
    }
}
