import Foundation
import XCTest
@testable import SocketIOMini

/// R-6: the API key never appears in a log line, a state, an error
/// description, or a URL. It appears on the wire only in the connect frame.
final class RedactionTests: XCTestCase {
    private static let key = "test-key-redaction-sentinel"

    private struct LeakyError: Error, CustomStringConvertible {
        var description: String { "the server echoed \(RedactionTests.key) back" }
    }

    func testTheKeyNeverLeaksOutsideTheConnectFrame() async throws {
        let http = FakeHTTP()
        http.error = LeakyError()
        let harness = try Harness(key: Self.key, http: http)
        var texts: [String] = []

        await harness.client.connect()
        let first = try await harness.handshake(0)
        first.push(#"42["status",{"echo":"\#(Self.key)"}]"#)
        first.push("42[" + Self.key)
        first.push("2")
        try await eventually("pong") { first.sent.contains("3") }

        // Emits that are refused or fail to encode.
        do {
            _ = try await harness.client.emit(Self.key)
        } catch {
            texts.append(String(describing: error))
            texts.append(error.localizedDescription)
        }
        do {
            _ = try await harness.client.emit("drone_command", payload: Data("{\"\(Self.key)\"".utf8))
        } catch {
            texts.append(String(describing: error))
        }
        let sent = try await harness.client.emit("get_fleet_status")
        XCTAssertEqual(sent, .sent)

        // A drop, then a hostile server that echoes the key in its connect_error.
        first.serverDrop()
        try await eventually("reconnecting") {
            if case .reconnecting = await harness.client.state { return true } else { return false }
        }
        harness.clock.advance(by: .seconds(1))
        let second = try await harness.transport(1)
        second.push(Harness.openFrame)
        try await eventually("connect frame") { second.sent.count == 1 }
        second.push(#"44{"message":"bad key \#(Self.key)"}"#)
        try await eventually("failed") { if case .failed = await harness.client.state { return true } else { return false } }
        let failedState = await harness.client.state
        XCTAssertEqual(failedState, .failed(.authenticationRejected(serverMessage: "bad key <redacted>", retryIn: nil)))

        // The health probe with an error that mentions the key.
        let health = await harness.client.healthProbe()
        texts.append(health.description)
        XCTAssertEqual(health, .unreachable("LeakyError"))

        await harness.client.shutdown()

        // Collect every text the client produced.
        texts += harness.logs.lines
        texts += harness.recorder.states.map(\.description)
        texts += harness.recorder.states.map { String(reflecting: $0) }
        texts += harness.network.transports.map(\.url.absoluteString)
        texts += http.requests.map(\.url.absoluteString)
        let configuration = GroundClient.Configuration(endpoint: try GroundEndpoint(host: "h"), apiKey: GroundAPIKey(Self.key))
        texts.append(String(describing: configuration))
        texts.append(String(reflecting: configuration))
        var dumped = ""
        dump(configuration, to: &dumped)
        texts.append(dumped)
        texts.append("\(GroundAPIKey(Self.key))")
        texts.append(String(reflecting: GroundAPIKey(Self.key)))

        XCTAssertFalse(harness.logs.lines.isEmpty)
        for text in texts {
            XCTAssertFalse(text.contains(Self.key), "the key leaked into: \(text)")
        }

        // On the wire, the key is only in the namespace connect frames.
        let frames = harness.network.transports.flatMap(\.sent)
        let withKey = frames.filter { $0.contains(Self.key) }
        XCTAssertEqual(withKey, Array(repeating: #"40{"api_key":"\#(Self.key)"}"#, count: 2))
        // REST gets it only in the Authorization header.
        for request in http.requests {
            XCTAssertEqual(request.headers["Authorization"], "Bearer \(Self.key)")
            XCTAssertFalse(request.url.absoluteString.contains(Self.key))
        }
    }

    func testRedactorReplacesEveryOccurrence() {
        let redact = GroundRedactor(key: GroundAPIKey("abc"))
        XCTAssertEqual(redact("abc and abcabc"), "<redacted> and <redacted><redacted>")
        XCTAssertEqual(GroundRedactor(key: GroundAPIKey(""))("abc"), "abc")
    }
}
