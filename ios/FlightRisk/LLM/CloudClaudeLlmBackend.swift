import CoreGraphics
import Foundation
import ImageIO
import OSLog
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
final class CloudClaudeLlmBackend: LlmBackend {

    // MARK: - Constants

    private static let apiURL = URL(string: "https://api.anthropic.com/v1/messages")!
    private static let apiVersion = "2023-06-01"
    private static let maxTokens = 256
    private static let jpegQuality: CGFloat = 0.85

    private let logger = Logger(subsystem: "com.flightrisk.app", category: "CloudClaudeLlm")

    // MARK: - Properties

    let name: String = "claude"

    /// Anthropic API key. Set externally by the caller (e.g. AppState
    /// loading from Keychain). When empty, ``isAvailable`` is false.
    var apiKey: String

    /// Claude model identifier.
    private let model: String

    /// HTTP timeout in seconds.
    private let timeoutInterval: TimeInterval

    /// Connectivity check callback. Injected by ``LlmSelector`` so the
    /// backend doesn't own its own NWPathMonitor.
    var connectivityCheck: () -> Bool = { true }

    var isAvailable: Bool {
        !apiKey.isEmpty && connectivityCheck()
    }

    // MARK: - Init

    /// - Parameters:
    ///   - apiKey: Anthropic API key. Pass empty string if not yet loaded.
    ///   - model: Claude model identifier.
    ///   - timeoutInterval: HTTP timeout in seconds.
    init(
        apiKey: String = "",
        model: String = "claude-sonnet-4-20250514",
        timeoutInterval: TimeInterval = 30
    ) {
        self.apiKey = apiKey
        self.model = model
        self.timeoutInterval = timeoutInterval
    }

    // MARK: - Public API

    func analyzeMatch(
        referenceImage: CGImage,
        candidateImage: CGImage,
        description: String?
    ) async -> ReasoningResult {
        guard isAvailable else {
            return ReasoningResult(
                isMatch: false,
                confidence: "unavailable",
                reasoning: "Claude API not available (no API key or no internet)"
            )
        }

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
        guard isAvailable else {
            return ReasoningResult(
                isMatch: false,
                confidence: "unavailable",
                reasoning: "Claude API not available (no API key or no internet)"
            )
        }

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

        var request = URLRequest(url: Self.apiURL)
        request.httpMethod = "POST"
        request.timeoutInterval = timeoutInterval
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(apiKey, forHTTPHeaderField: "x-api-key")
        request.setValue(Self.apiVersion, forHTTPHeaderField: "anthropic-version")
        request.httpBody = jsonData

        do {
            let (data, response) = try await URLSession.shared.data(for: request)

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
    /// Mirrors `FlightRiskAgent._parse_match_response()` from the Python
    /// codebase.
    func parseMatchResponse(_ text: String) -> ReasoningResult {
        var isMatch = false
        var confidence = "unknown"
        var reasoning = text

        for line in text.split(separator: "\n") {
            let upper = line.trimmingCharacters(in: .whitespaces).uppercased()
            if upper.hasPrefix("MATCH:") {
                isMatch = upper.contains("YES")
            } else if upper.hasPrefix("CONFIDENCE:") {
                confidence = upper
                    .split(separator: ":", maxSplits: 1)
                    .last
                    .map { $0.trimmingCharacters(in: .whitespaces).lowercased() }
                    ?? "unknown"
            } else if upper.hasPrefix("REASONING:") {
                reasoning = String(line)
                    .trimmingCharacters(in: .whitespaces)
                    .split(separator: ":", maxSplits: 1)
                    .last
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    ?? text
            }
        }

        return ReasoningResult(
            isMatch: isMatch,
            confidence: confidence,
            reasoning: reasoning
        )
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
