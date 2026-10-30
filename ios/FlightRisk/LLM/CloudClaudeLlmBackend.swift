import CoreGraphics
import Foundation
import ImageIO
import OSLog
import os
import UniformTypeIdentifiers

/// LLM backend that calls the Anthropic Messages API (Claude) for
/// visual reasoning.
///
/// Ports the prompt logic from `FlightRiskAgent.analyze_match()` and
/// `FlightRiskAgent.match_description()` in the Python codebase, but
/// targets the Claude REST API instead of local Ollama.
///
/// Images are JPEG-encoded and sent as base64 `image` content blocks.
/// The response is parsed for the structured MATCH / CONFIDENCE /
/// REASONING format that the Python agent also uses.
///
/// Cloud use is controlled by a ``CloudGate`` that reads the saved opt-in,
/// the selected provider and the Keychain key at call time. The gate is
/// checked before encoding and again immediately before the request is
/// sent, and requests in flight are cancelled as soon as the gate closes.
final class CloudClaudeLlmBackend: LlmBackend {

    // MARK: - Constants

    private static let apiURL = URL(string: "https://api.anthropic.com/v1/messages")!
    private static let apiVersion = "2023-06-01"
    private static let maxTokens = 256
    private static let jpegQuality: CGFloat = 0.85

    private let logger = Logger(subsystem: "com.flightrisk.app", category: "CloudClaudeLlm")

    // MARK: - Properties

    let name: String = "claude"

    /// Claude model identifier.
    private let model: String

    /// HTTP timeout in seconds.
    private let timeoutInterval: TimeInterval

    /// Dedicated session, so requests in flight can be cancelled when the
    /// cloud gate closes without touching any other network traffic.
    private let session: URLSession

    /// Mutable state shared between the selector, the search pipeline and
    /// the revocation observer. Every access goes through the lock.
    private struct State {
        var gate: CloudGate = { nil }
        var isOnline = true
        /// Incremented whenever the gate is found closed; a request started
        /// under an older generation is cancelled.
        var generation = 0
        var inFlight: [UUID: Task<(Data, URLResponse), Error>] = [:]
    }

    private let state = OSAllocatedUnfairLock(initialState: State())
    private var revocationObserver: NSObjectProtocol?

    /// True only while the gate yields a key (opt-in on, Cloud Claude
    /// selected, key saved) and the device is online. Evaluated at call time.
    var isAvailable: Bool {
        let (gate, online) = state.withLock { ($0.gate, $0.isOnline) }
        return online && gate() != nil
    }

    // MARK: - Init

    /// - Parameters:
    ///   - model: Claude model identifier.
    ///   - timeoutInterval: HTTP timeout in seconds.
    ///   - gate: Returns the API key only while cloud use is allowed. The
    ///     default is closed; ``LlmSelector`` installs the real gate when the
    ///     backend is registered.
    ///   - sessionConfiguration: Configuration for the dedicated session.
    ///     Tests pass one with a stub `URLProtocol`.
    init(
        model: String = "claude-sonnet-4-20250514",
        timeoutInterval: TimeInterval = 30,
        gate: @escaping CloudGate = { nil },
        sessionConfiguration: URLSessionConfiguration = .ephemeral
    ) {
        self.model = model
        self.timeoutInterval = timeoutInterval
        self.session = URLSession(configuration: sessionConfiguration)
        state.withLock { $0.gate = gate }
        // Settings changes that can close the gate (opt-out, selecting
        // another provider, removing the key) post this notification
        // synchronously, so requests in flight are cancelled at once.
        revocationObserver = NotificationCenter.default.addObserver(
            forName: ReasoningSettings.cloudGateMayHaveClosed,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.cancelIfGateClosed()
        }
    }

    deinit {
        if let revocationObserver {
            NotificationCenter.default.removeObserver(revocationObserver)
        }
        session.invalidateAndCancel()
    }

    // MARK: - Gate control

    /// Replace the gate. Cancels requests in flight if the new gate is closed.
    func setGate(_ gate: @escaping CloudGate) {
        state.withLock { $0.gate = gate }
        cancelIfGateClosed()
    }

    /// Update the connectivity flag (set by ``LlmSelector``).
    func setOnline(_ online: Bool) {
        state.withLock { $0.isOnline = online }
    }

    /// Cancel every request in flight if the gate no longer yields a key.
    func cancelIfGateClosed() {
        let gate = state.withLock { $0.gate }
        if gate() == nil {
            cancelAllRequests()
        }
    }

    /// Cancel every request in flight and any request that is about to start.
    func cancelAllRequests() {
        let tasks = state.withLock { s -> [Task<(Data, URLResponse), Error>] in
            s.generation += 1
            let tasks = Array(s.inFlight.values)
            s.inFlight.removeAll()
            return tasks
        }
        tasks.forEach { $0.cancel() }
        session.getAllTasks { $0.forEach { $0.cancel() } }
    }

    /// The key from the gate, or nil when cloud use is not allowed right now.
    private func currentKey() -> String? {
        let gate = state.withLock { $0.gate }
        guard let key = gate(), !key.isEmpty else { return nil }
        return key
    }

    private static let blockedResult = ReasoningResult(
        isMatch: false,
        confidence: "unavailable",
        reasoning: "Cloud reasoning is not allowed right now (not opted in, not selected, no API key, or offline)"
    )

    // MARK: - Public API

    func analyzeMatch(
        referenceImage: CGImage,
        candidateImage: CGImage,
        description: String?
    ) async -> ReasoningResult {
        // First gate check: nothing is encoded unless cloud use is allowed.
        guard isAvailable else { return Self.blockedResult }

        guard let refB64 = cgImageToBase64(referenceImage),
              let candB64 = cgImageToBase64(candidateImage) else {
            return ReasoningResult(
                isMatch: false,
                confidence: "error",
                reasoning: "Failed to encode images"
            )
        }

        var prompt = """
            You are helping find a missing person. \
            Image 1 is the reference photo of the person we are looking for. \
            Image 2 is a person detected by a drone camera.

            Compare the two people. Consider: clothing color/type, hair, \
            build, height, backpack/accessories, and any distinguishing features.

            """

        if let description, !description.isEmpty {
            prompt += "Additional description of the person: \(description)\n\n"
        }

        prompt += """
            Respond in this exact format:
            MATCH: yes or no
            CONFIDENCE: high, medium, or low
            REASONING: one sentence explaining why
            """

        let contentBlocks: [[String: Any]] = [
            imageBlock(refB64),
            imageBlock(candB64),
            textBlock(prompt),
        ]

        return await callApi(contentBlocks: contentBlocks)
    }

    func describeMatch(
        candidateImage: CGImage,
        description: String
    ) async -> ReasoningResult {
        // First gate check: nothing is encoded unless cloud use is allowed.
        guard isAvailable else { return Self.blockedResult }

        guard let candB64 = cgImageToBase64(candidateImage) else {
            return ReasoningResult(
                isMatch: false,
                confidence: "error",
                reasoning: "Failed to encode image"
            )
        }

        let prompt = """
            You are helping find a missing person. \
            The person's description: \(description)

            Look at this image of a person detected by a drone camera. \
            Does this person match the description above?

            Consider: clothing color/type, hair color/style, approximate age, \
            build, backpack/accessories, and any distinguishing features.

            Respond in this exact format:
            MATCH: yes or no
            CONFIDENCE: high, medium, or low
            REASONING: one sentence explaining why
            """

        let contentBlocks: [[String: Any]] = [
            imageBlock(candB64),
            textBlock(prompt),
        ]

        return await callApi(contentBlocks: contentBlocks)
    }

    // MARK: - Internals

    /// Send a request to the Anthropic Messages API and parse the
    /// structured response.
    private func callApi(contentBlocks: [[String: Any]]) async -> ReasoningResult {
        let body: [String: Any] = [
            "model": model,
            "max_tokens": Self.maxTokens,
            "messages": [
                [
                    "role": "user",
                    "content": contentBlocks,
                ] as [String: Any],
            ],
        ]

        guard let jsonData = try? JSONSerialization.data(withJSONObject: body) else {
            return ReasoningResult(isMatch: false, confidence: "error", reasoning: "Failed to serialize request")
        }

        // Second gate check, immediately before sending. One copy of the key
        // is used for both the check and the header, so the header can never
        // be empty, and the generation is read first so a revocation that
        // happens from here on cancels this request.
        let generation = state.withLock { $0.generation }
        guard state.withLock({ $0.isOnline }), let key = currentKey() else {
            return Self.blockedResult
        }

        var request = URLRequest(url: Self.apiURL)
        request.httpMethod = "POST"
        request.timeoutInterval = timeoutInterval
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(key, forHTTPHeaderField: "x-api-key")
        request.setValue(Self.apiVersion, forHTTPHeaderField: "anthropic-version")
        request.httpBody = jsonData

        let id = UUID()
        let session = self.session
        let task = Task { try await session.data(for: request) }
        let registered = state.withLock { s -> Bool in
            guard s.generation == generation else { return false }
            s.inFlight[id] = task
            return true
        }
        if !registered {
            task.cancel()
        }
        defer { state.withLock { _ = $0.inFlight.removeValue(forKey: id) } }

        do {
            let (data, response) = try await task.value

            guard let httpResponse = response as? HTTPURLResponse else {
                return ReasoningResult(isMatch: false, confidence: "error", reasoning: "Invalid response")
            }

            guard httpResponse.statusCode == 200 else {
                let errorBody = String(data: data, encoding: .utf8) ?? "unknown"
                logger.warning("API error \(httpResponse.statusCode): \(errorBody)")
                return ReasoningResult(
                    isMatch: false,
                    confidence: "error",
                    reasoning: "API error \(httpResponse.statusCode)"
                )
            }

            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let contentArray = json["content"] as? [[String: Any]] else {
                return ReasoningResult(isMatch: false, confidence: "error", reasoning: "Failed to parse response")
            }

            let fullText = contentArray
                .filter { ($0["type"] as? String) == "text" }
                .compactMap { $0["text"] as? String }
                .joined()

            return parseMatchResponse(fullText)

        } catch {
            if error is CancellationError || (error as? URLError)?.code == .cancelled {
                logger.info("Cloud request cancelled because cloud reasoning was turned off")
                return Self.blockedResult
            }
            logger.error("API call failed: \(error.localizedDescription)")
            return ReasoningResult(
                isMatch: false,
                confidence: "error",
                reasoning: error.localizedDescription
            )
        }
    }

    /// Parse the structured MATCH / CONFIDENCE / REASONING response.
    ///
    /// Delegates to ``ReasoningResult.parse(_:confidenceDiscount:)`` which
    /// centralises the parsing logic shared by all LLM backends.
    func parseMatchResponse(_ text: String) -> ReasoningResult {
        return ReasoningResult.parse(text)
    }

    /// Encode a `CGImage` as a base64 JPEG string.
    private func cgImageToBase64(_ image: CGImage) -> String? {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(
            data as CFMutableData,
            UTType.jpeg.identifier as CFString,
            1,
            nil
        ) else {
            return nil
        }

        let options: [CFString: Any] = [
            kCGImageDestinationLossyCompressionQuality: Self.jpegQuality,
        ]
        CGImageDestinationAddImage(destination, image, options as CFDictionary)

        guard CGImageDestinationFinalize(destination) else {
            return nil
        }

        return (data as Data).base64EncodedString()
    }

    /// Build a Messages API image content block.
    private func imageBlock(_ base64Data: String) -> [String: Any] {
        [
            "type": "image",
            "source": [
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64Data,
            ] as [String: Any],
        ]
    }

    /// Build a Messages API text content block.
    private func textBlock(_ text: String) -> [String: Any] {
        [
            "type": "text",
            "text": text,
        ]
    }
}
