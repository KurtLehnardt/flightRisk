import Foundation

/// Download and lifecycle state for the on-device Gemma model.
enum GemmaDownloadState: Sendable, Equatable {
    case idle
    case downloading(progress: Double)
    case downloaded
    case loading
    case ready
    case error(message: String)
}

/// Protocol abstracting Gemma model lifecycle for testability.
protocol GemmaModelManaging: AnyObject, Sendable {
    var state: GemmaDownloadState { get async }
    var isReady: Bool { get async }
    var modelSizeBytes: UInt64? { get async }
    static var modelId: String { get }

    func download() async throws
    func cancelDownload() async
    func loadModel() async throws
    func unloadModel() async
    func deleteModel() async throws
    func generate(prompt: String, maxTokens: Int) async throws -> String
}

extension GemmaModelManaging {
    static var modelId: String { "mlx-community/gemma-2-2b-it-4bit" }
}
