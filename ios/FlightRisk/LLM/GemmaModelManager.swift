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

actor GemmaModelManager: GemmaModelManaging {

    private let logger = Logger(subsystem: "com.flightrisk.app", category: "GemmaModelManager")

    // MARK: - State

    private(set) var state: GemmaDownloadState = .idle
    var isReady: Bool { state == .ready }
    private(set) var modelSizeBytes: UInt64? = nil
    static let modelId = "mlx-community/gemma-2-2b-it-4bit"

    // MARK: - MLX model references

    #if canImport(MLX)
    private var container: ModelContainer?
    #endif

    // MARK: - Download state

    private var isCancelled = false
    private static let requiredDiskSpaceBytes: UInt64 = 2_000_000_000

    // MARK: - Configuration

    #if canImport(MLX)
    private static let modelConfig = ModelConfiguration(
        id: modelId,
        overrideTokenizer: "PreTrainedTokenizer",
        defaultPrompt: "What is the difference between lettuce and cabbage?"
    )
    #endif

    // MARK: - Directories

    private var modelDirectory: URL {
        #if canImport(MLX)
        return Self.modelConfig.modelDirectory()
        #else
        let appSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first!
        return appSupport
            .appendingPathComponent("FlightRisk", isDirectory: true)
            .appendingPathComponent("models", isDirectory: true)
            .appendingPathComponent("gemma-2-2b-it-4bit", isDirectory: true)
        #endif
    }

    private func updateProgress(_ progress: Progress) {
        let fraction = progress.fractionCompleted
        state = .downloading(progress: fraction)
    }

    // MARK: - Init / Deinit

    init() {
        setupNotifications()
        detectExistingDownload()
    }

    // MARK: - GemmaModelManaging

    func download() async throws {
        guard state == .idle || state.isError else {
            logger.debug("Download called in state \(String(describing: self.state)); ignoring")
            return
        }
        isCancelled = false

        do {
            try checkDiskSpace()
        } catch {
            state = .error(message: error.localizedDescription)
            throw error
        }

        do {
            try await checkNotCellular()
        } catch {
            state = .error(message: error.localizedDescription)
            throw error
        }

        state = .downloading(progress: 0)

        #if canImport(MLX)
        do {
            let config = Self.modelConfig
            logger.info("Starting model download: \(Self.modelId)")
            _ = try await downloadModel(
                hub: defaultHubApi,
                configuration: config
            ) { [weak self] progress in
                guard let self else { return }
                Task { await self.updateProgress(progress) }
            }

            guard !isCancelled else {
                state = .idle
                isCancelled = false
                return
            }

            modelSizeBytes = directorySize(config.modelDirectory())
            state = .downloaded
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
        isCancelled = true
        state = .idle
        logger.info("Download cancelled")
    }

    func loadModel() async throws {
        guard state == .downloaded else {
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
            let config = Self.modelConfig
            self.container = try await LLMModelFactory.shared.loadContainer(
                configuration: config)
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
        container = nil
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

        modelSizeBytes = nil
        state = .idle
        logger.info("Model deleted")
    }

    func generate(prompt: String, maxTokens: Int) async throws -> String {
        guard state == .ready else {
            throw GemmaError.modelNotAvailable
        }

        #if canImport(MLX)
        guard let container else {
            throw GemmaError.modelNotAvailable
        }

        let config = Self.modelConfig
        let result = try await container.perform { model, tokenizer in
            let promptTokens = tokenizer.encode(text: prompt)
            let parameters = GenerateParameters(temperature: 0.6)
            return try MLXLMCommon.generate(
                promptTokens: promptTokens,
                parameters: parameters,
                model: model,
                tokenizer: tokenizer,
                extraEOSTokens: config.extraEOSTokens
            ) { tokens in
                if tokens.count >= maxTokens || Task.isCancelled {
                    return .stop
                }
                return .more
            }
        }

        MLX.GPU.clearCache()
        return result.output
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

    private func checkNotCellular() async throws {
        let isCellular = await withCheckedContinuation { continuation in
            let monitor = NWPathMonitor()
            let queue = DispatchQueue(label: "com.flightrisk.app.cellularCheck")
            monitor.pathUpdateHandler = { path in
                continuation.resume(returning: path.usesInterfaceType(.cellular))
                monitor.cancel()
            }
            monitor.start(queue: queue)
        }
        if isCellular {
            throw GemmaError.cellularDownloadBlocked
        }
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
        if thermalState.rawValue >= ProcessInfo.ThermalState.serious.rawValue {
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
