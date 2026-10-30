import Foundation
import XCTest
@testable import SocketIOMini

/// Tests over real loopback sockets: the Network.framework transport against a
/// hand-written WebSocket server that misbehaves, and the health probe against
/// HTTP servers that redirect or answer with the wrong body.
final class LoopbackSocketTests: XCTestCase {
    private static let oversized = 40 * 1024 * 1024
    private static let cap = 1024 * 1024

    private final class Box<Value>: @unchecked Sendable {
        private let lock = NSLock()
        private var stored: Value

        init(_ value: Value) {
            stored = value
        }

        var value: Value {
            get { lock.withLock { stored } }
            set { lock.withLock { stored = newValue } }
        }
    }

    private static func socketURL(_ port: UInt16) -> URL {
        URL(string: "ws://127.0.0.1:\(port)/socket.io/?EIO=4&transport=websocket")!
    }

    // MARK: - Frame cap (finding 1)

    private func assertOversizedMessageIsRefused(networkMessageLimit: Bool) async throws {
        let streamedAll = Box<Bool?>(nil)
        let serverSide = Box<LoopbackConnection?>(nil)
        let server = try LoopbackServer { head, connection in
            serverSide.value = connection
            try? await connection.send(LoopbackServer.upgradeResponse(for: head))
            try? await connection.send(LoopbackServer.textFrameHeader(length: Self.oversized))
            streamedAll.value = await connection.stream(total: Self.oversized)
        }
        let port = try await server.start()
        defer { server.stop() }

        let transport = NWGroundTransport(
            url: Self.socketURL(port), maxFrameBytes: Self.cap, openTimeout: 10, networkMessageLimit: networkMessageLimit
        )
        let before = physicalFootprint()
        try await transport.connect()
        do {
            let message = try await transport.receive()
            XCTFail("an oversized message was delivered: \(message)")
        } catch {
            XCTAssertEqual(error as? GroundTransportError, .messageTooLarge(limit: Self.cap))
        }
        let after = physicalFootprint()
        // The client has closed its socket. (The test server's pending send is
        // not told about the reset, so it is not waited for.)
        try await Task.sleep(nanoseconds: 500_000_000)
        XCTAssertNotEqual(streamedAll.value, true, "the whole 40 MB message was accepted")
        let sent = try XCTUnwrap(serverSide.value).bytesSent
        XCTAssertLessThan(sent, 16 * 1024 * 1024, "the kernel accepted \(sent) bytes before the client closed")
        XCTAssertLessThan(after - before, 16 * 1024 * 1024, "memory grew by \(after - before) bytes")
        await transport.close()
    }

    func testAnOversizedMessageIsRefusedBeforeItIsBufferedWhole() async throws {
        try await assertOversizedMessageIsRefused(networkMessageLimit: true)
    }

    func testTheChunkedGuardAloneAlsoRefusesAnOversizedMessage() async throws {
        try await assertOversizedMessageIsRefused(networkMessageLimit: false)
    }

    func testTheUpgradeRequestCarriesNoOriginNoCredentialsAndNoCompression() async throws {
        let server = try LoopbackServer { head, connection in
            try? await connection.send(LoopbackServer.upgradeResponse(for: head))
            try? await connection.send(LoopbackServer.textFrame("6"))
        }
        let port = try await server.start()
        defer { server.stop() }
        let transport = NWGroundTransport(url: Self.socketURL(port), maxFrameBytes: Self.cap)
        try await transport.connect()
        let message = try await transport.receive()
        XCTAssertEqual(message, .text("6"))
        await transport.close()

        let head = try XCTUnwrap(server.requestHeads.first)
        let lines = head.split(separator: "\r\n").map { $0.lowercased() }
        XCTAssertEqual(lines.first, "get /socket.io/?eio=4&transport=websocket http/1.1")
        XCTAssertFalse(lines.contains { $0.hasPrefix("origin:") }, head)
        XCTAssertFalse(lines.contains { $0.hasPrefix("authorization:") }, head)
        XCTAssertFalse(lines.contains { $0.hasPrefix("cookie:") }, head)
        // No permessage-deflate offer, so a server cannot send compressed frames
        // that expand after the size check.
        XCTAssertFalse(lines.contains { $0.hasPrefix("sec-websocket-extensions:") }, head)
        XCTAssertFalse(head.contains("test-key"))
    }

    func testTheClientDropsTheLinkWithATypedReasonOnAnOversizedFrame() async throws {
        let server = try LoopbackServer { head, connection in
            try? await connection.send(LoopbackServer.upgradeResponse(for: head))
            try? await connection.send(LoopbackServer.textFrame(Harness.openFrame))
            try? await Task.sleep(nanoseconds: 100_000_000)
            try? await connection.send(LoopbackServer.textFrame(#"40{"sid":"s1"}"#))
            try? await connection.send(LoopbackServer.textFrame(#"42["status",{"connected":true}]"#))
            try? await Task.sleep(nanoseconds: 100_000_000)
            try? await connection.send(LoopbackServer.textFrameHeader(length: Self.oversized))
            _ = await connection.stream(total: Self.oversized)
        }
        let port = try await server.start()
        defer { server.stop() }
        let logs = LogRecorder()
        let client = GroundClient(
            configuration: GroundClient.Configuration(
                endpoint: try GroundEndpoint(host: "127.0.0.1", port: Int(port)),
                apiKey: GroundAPIKey("test-key"),
                backoff: GroundBackoff(initialDelay: .seconds(60), maximumDelay: .seconds(60)),
                maxFrameBytes: Self.cap
            ),
            logSink: logs.sink
        )
        let recorder = EventRecorder(client.events)
        await client.connect()
        try await eventually("status over the real socket", timeout: 10) {
            recorder.serverEvents.contains { $0.name == "status" }
        }
        try await eventually("frame too large", timeout: 20) {
            if case .reconnecting(1, _, .frameTooLarge(Self.cap)) = await client.state { return true }
            return false
        }
        await client.shutdown()
    }

    // MARK: - Health probe redirects (finding 4)

    private func probe(port: UInt16) async throws -> GroundHealthResult {
        let endpoint = try GroundEndpoint(host: "127.0.0.1", port: Int(port))
        return await GroundHealthProbe(endpoint: endpoint, apiKey: GroundAPIKey("test-key"), http: URLSessionGroundHTTPClient()).run()
    }

    private func elsewhere() async throws -> (LoopbackServer, UInt16) {
        let target = try LoopbackServer { _, connection in
            try? await connection.send(LoopbackServer.httpResponse(
                status: "200 OK", headers: ["Content-Type: application/json"],
                body: #"{"status":"healthy","running":true,"source":"sim","version":"evil"}"#
            ))
            connection.close()
        }
        return (target, try await target.start())
    }

    func testTheHealthProbeNeverFollowsARedirect() async throws {
        let (target, targetPort) = try await elsewhere()
        defer { target.stop() }
        let server = try LoopbackServer { _, connection in
            try? await connection.send(LoopbackServer.httpResponse(
                status: "302 Found", headers: ["Location: http://127.0.0.1:\(targetPort)/api/status"]
            ))
            connection.close()
        }
        let port = try await server.start()
        defer { server.stop() }
        let result = try await probe(port: port)
        XCTAssertEqual(result, .unexpectedStatus(302))
        XCTAssertTrue(target.requestHeads.isEmpty, "the redirect target was contacted")
    }

    func testARedirectedStatusRouteIsNotAcceptedAsTheKeyCheck() async throws {
        let (target, targetPort) = try await elsewhere()
        defer { target.stop() }
        let server = try LoopbackServer { head, connection in
            if head.hasPrefix("GET /api/health ") {
                try? await connection.send(LoopbackServer.httpResponse(
                    status: "200 OK", body: #"{"status":"healthy","version":"1.0.0"}"#
                ))
            } else {
                try? await connection.send(LoopbackServer.httpResponse(
                    status: "307 Temporary Redirect", headers: ["Location: http://127.0.0.1:\(targetPort)/api/status"]
                ))
            }
            connection.close()
        }
        let port = try await server.start()
        defer { server.stop() }
        let result = try await probe(port: port)
        XCTAssertEqual(result, .unexpectedStatus(307))
        XCTAssertTrue(target.requestHeads.isEmpty)
        // The key went only to the probed host, in the Authorization header.
        for head in server.requestHeads {
            XCTAssertTrue(head.contains("Authorization: Bearer test-key") || head.lowercased().contains("authorization: bearer test-key"))
            XCTAssertFalse(head.split(separator: "\r\n").first!.contains("test-key"))
        }
    }

    func testAStatusRouteWithoutTheDashboardJSONIsNotAcceptedAsTheKeyCheck() async throws {
        let server = try LoopbackServer { head, connection in
            if head.hasPrefix("GET /api/health ") {
                try? await connection.send(LoopbackServer.httpResponse(status: "200 OK", body: #"{"status":"healthy"}"#))
            } else {
                try? await connection.send(LoopbackServer.httpResponse(status: "200 OK", body: "<html>captive portal</html>"))
            }
            connection.close()
        }
        let port = try await server.start()
        defer { server.stop() }
        let result = try await probe(port: port)
        XCTAssertEqual(result, .invalidResponse("/api/status did not return the dashboard status object"))
    }

    func testAHugeHealthBodyIsRefused() async throws {
        let server = try LoopbackServer { _, connection in
            try? await connection.send(LoopbackServer.httpResponse(
                status: "200 OK", body: String(repeating: "x", count: 400 * 1024)
            ))
            connection.close()
        }
        let port = try await server.start()
        defer { server.stop() }
        let result = try await probe(port: port)
        XCTAssertEqual(result, .unreachable("response body larger than \(URLSessionGroundHTTPClient.maxBodyBytes) bytes"))
    }
}
