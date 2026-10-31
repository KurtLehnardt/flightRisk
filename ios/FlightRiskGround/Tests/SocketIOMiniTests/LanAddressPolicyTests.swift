import Foundation
import XCTest
@testable import SocketIOMini

/// The LAN address policy: no network and no DNS. Resolvers are injected.
final class LanAddressPolicyTests: XCTestCase {
    private func address(_ text: String, file: StaticString = #filePath, line: UInt = #line) -> GroundResolvedAddress {
        guard let value = LanAddressPolicy.numericAddress(text) else {
            XCTFail("not a numeric address: \(text)", file: file, line: line)
            return GroundResolvedAddress(bytes: [0, 0, 0, 0])!
        }
        return value
    }

    // MARK: - Address table (mirrors the Android tests, plus IPv4-mapped IPv6)

    func testLocalAddressesAreAllowed() {
        let allowed = [
            "127.0.0.1", "127.1.2.3", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.254",
            "192.168.0.1", "192.168.10.20", "169.254.1.1", "::1", "fe80::1", "febf::1", "fd12:3456::1", "fc00::1",
            "::ffff:10.0.0.1", "::ffff:192.168.1.20", "::ffff:127.0.0.1", "::ffff:172.16.0.1",
        ]
        for text in allowed {
            XCTAssertTrue(LanAddressPolicy.isAllowed(address(text)), text)
        }
    }

    func testPublicAndSpecialAddressesAreRefused() {
        let refused = [
            "8.8.8.8", "172.15.255.255", "172.32.0.1", "192.169.0.1", "192.167.255.255", "11.0.0.1", "9.255.255.255",
            "100.64.0.1", "100.127.255.255", "0.0.0.0", "224.0.0.1", "239.255.255.255", "255.255.255.255",
            "169.253.0.1", "126.255.255.255", "128.0.0.1",
            "2001:4860:4860::8888", "::", "ff02::1", "fec0::1", "fbff::1", "fe00::1", "64:ff9b::808:808", "2002:808:808::",
            "::ffff:8.8.8.8", "::ffff:100.64.0.1", "::ffff:0.0.0.0", "::ffff:255.255.255.255", "::8.8.8.8", "::10.0.0.1",
        ]
        for text in refused {
            XCTAssertFalse(LanAddressPolicy.isAllowed(address(text)), text)
        }
    }

    func testNumericSpellingsAreJudgedByTheirBytes() {
        // Each spelling parses to the same bytes the address has, so it cannot bypass the rule.
        XCTAssertEqual(address("0x7f.1").bytes, [127, 0, 0, 1])
        XCTAssertEqual(address("2130706433").bytes, [127, 0, 0, 1])
        XCTAssertEqual(address("0177.0.0.1").bytes, [127, 0, 0, 1])
        XCTAssertTrue(LanAddressPolicy.isAllowed(address("0x7f.1")))
        XCTAssertTrue(LanAddressPolicy.isAllowed(address("2130706433")))
        XCTAssertTrue(LanAddressPolicy.isAllowed(address("0177.0.0.1")))
        // Public addresses in the same spellings are refused.
        XCTAssertFalse(LanAddressPolicy.isAllowed(address("0x08080808")))
        XCTAssertFalse(LanAddressPolicy.isAllowed(address("134744072")))
        XCTAssertFalse(LanAddressPolicy.isAllowed(address("010.8.8.8")), "octal 010 is 8, so this is 8.8.8.8")
        XCTAssertFalse(LanAddressPolicy.isAllowed(address("0x8.0x8.0x8.0x8")))
        // A decimal spelling of a private address is allowed because its bytes are private.
        XCTAssertTrue(LanAddressPolicy.isAllowed(address("3232235777")), "192.168.1.1")
        XCTAssertNil(LanAddressPolicy.numericAddress("ground.local"))
        XCTAssertNil(LanAddressPolicy.numericAddress("example.com"))
    }

    // MARK: - Names

    func testEveryResolvedAddressMustBeAllowed() throws {
        let mixed: GroundHostResolver = { _ in [self.address("192.168.1.20"), self.address("8.8.8.8")] }
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddress(host: "ground.example", cleartext: true, resolver: mixed)) { error in
            let refusal = error as? GroundAddressRefusal
            XCTAssertTrue(refusal?.reason.contains("only allowed to local network addresses") == true)
            XCTAssertTrue(refusal?.reason.contains("8.8.8.8") == true)
        }
        // The public address first: still refused.
        let reversed: GroundHostResolver = { _ in [self.address("8.8.8.8"), self.address("10.0.0.5")] }
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddress(host: "ground.example", cleartext: true, resolver: reversed))
        // An IPv6 public address among private IPv4 ones.
        let v6: GroundHostResolver = { _ in [self.address("10.0.0.5"), self.address("2001:4860:4860::8888")] }
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddress(host: "ground.example", cleartext: true, resolver: v6))
        let allPrivate: GroundHostResolver = { _ in [self.address("192.168.1.20"), self.address("fd00::1")] }
        XCTAssertEqual(try LanAddressPolicy.checkedAddress(host: "ground.local", cleartext: true, resolver: allPrivate), address("192.168.1.20"))
    }

    func testAnEmptyAnswerAndAResolverFailureAreErrors() {
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddress(host: "x.example", cleartext: true, resolver: { _ in [] })) { error in
            XCTAssertEqual(error as? GroundTransportError, .failed("the host name did not resolve"))
        }
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddress(host: "x.example", cleartext: true, resolver: { _ in
            throw GroundTransportError.failed("the host name did not resolve")
        }))
    }

    func testALiteralNeedsNoLookup() throws {
        let resolver: GroundHostResolver = { _ in
            XCTFail("a literal must not be looked up")
            return []
        }
        XCTAssertEqual(try LanAddressPolicy.checkedAddress(host: "10.1.2.3", cleartext: true, resolver: resolver), address("10.1.2.3"))
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddress(host: "8.8.8.8", cleartext: true, resolver: resolver))
        // A hostile name that spells a public address numerically is judged by its bytes.
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddress(host: "0x08080808", cleartext: true, resolver: resolver))
    }

    // MARK: - Encrypted connections

    func testEncryptedAddressesAreNotSubjectToTheRule() throws {
        let publicOnly: GroundHostResolver = { _ in [self.address("8.8.8.8")] }
        XCTAssertEqual(try LanAddressPolicy.checkedAddress(host: "ground.example", cleartext: false, resolver: publicOnly), address("8.8.8.8"))
        let secure = URL(string: "wss://ground.example:5555/socket.io/?EIO=4&transport=websocket")!
        let target = try GroundConnectPlanner.plan(url: secure, resolver: publicOnly)
        XCTAssertEqual(target.url.host, "8.8.8.8")
        XCTAssertEqual(target.tlsServerName, "ground.example", "TLS is checked against the name, not the IP")
        let cleartext = URL(string: "ws://ground.example:5555/socket.io/")!
        XCTAssertThrowsError(try GroundConnectPlanner.plan(url: cleartext, resolver: publicOnly))
        // A secure public IP literal parses; the same literal in cleartext does not.
        XCTAssertNoThrow(try GroundEndpoint.parse("wss://8.8.8.8:5555"))
        XCTAssertNoThrow(try GroundEndpoint.parse("https://8.8.8.8"))
    }

    // MARK: - Rebinding

    func testTheConnectionGoesToTheFirstCheckedAddress() throws {
        // First answer: private. Any later answer: public. The plan resolves once.
        let calls = CallCounter()
        let resolver: GroundHostResolver = { _ in
            calls.increment() == 1 ? [self.address("192.168.1.20")] : [self.address("8.8.8.8")]
        }
        let url = URL(string: "ws://ground.example:5555/socket.io/?EIO=4&transport=websocket")!
        let target = try GroundConnectPlanner.plan(url: url, resolver: resolver)
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(target.url.absoluteString, "ws://192.168.1.20:5555/socket.io/?EIO=4&transport=websocket")
        XCTAssertEqual(target.address, address("192.168.1.20"))
        XCTAssertNil(target.tlsServerName)
    }

    func testIPv6TargetsKeepTheirBrackets() throws {
        let url = URL(string: "ws://ground.local:5555/x")!
        let target = try GroundConnectPlanner.plan(url: url, resolver: { _ in [self.address("fd00::7")] })
        XCTAssertEqual(target.url.absoluteString, "ws://[fd00::7]:5555/x")
    }

    // MARK: - Endpoint parsing

    func testParseRefusesPublicCleartextLiteralsEarly() {
        for text in ["ws://8.8.8.8:5555", "8.8.8.8", "http://1.1.1.1", "ws://100.64.0.1:5555", "ws://[2001:4860:4860::8888]:5555",
                     "ws://0x08080808:5555", "ws://134744072", "ws://0.0.0.0", "ws://255.255.255.255", "ws://224.0.0.1"] {
            XCTAssertThrowsError(try GroundEndpoint.parse(text), text) { error in
                guard case let .cleartextNotAllowed(reason)? = error as? GroundConfigurationError else {
                    return XCTFail("wrong error for \(text): \(error)")
                }
                XCTAssertEqual(reason, GroundAddressRefusal.cleartextMessage)
            }
        }
    }

    func testParseAcceptsLocalLiteralsAndNames() throws {
        for text in ["ws://192.168.1.20:5555", "10.0.0.1", "ws://172.16.0.9", "ws://[fd00::1]:5555", "ws://127.0.0.1:5555",
                     "ws://ground.local:5555", "ws://ground.example.com:5555", "ws://localhost", "ws://0x7f.1:5555"] {
            XCTAssertNoThrow(try GroundEndpoint.parse(text), text)
        }
        // A name is checked at connect time.
        XCTAssertThrowsError(try GroundEndpoint.parse("ws://172.15.0.1"))
        XCTAssertThrowsError(try GroundEndpoint.parse("ws://172.32.0.1"))
    }

    func testRefusalTextNeverContainsTheKey() {
        let reasons = [
            GroundAddressRefusal.cleartextMessage,
            GroundAddressRefusal.cleartext(address: address("8.8.8.8")).reason,
            GroundConfigurationError.cleartextNotAllowed(GroundAddressRefusal.cleartextMessage).description,
            GroundHealthResult.addressNotAllowed(GroundAddressRefusal.cleartextMessage).description,
        ]
        for reason in reasons {
            XCTAssertFalse(reason.contains("test-key"), reason)
        }
    }

    // MARK: - Health probe

    private func probe(_ text: String, resolver: @escaping GroundHostResolver, http: FakeHTTP) throws -> GroundHealthProbe {
        GroundHealthProbe(endpoint: try GroundEndpoint.parse(text), apiKey: GroundAPIKey("test-key"), http: http, resolver: resolver)
    }

    func testTheProbeSendsNothingToAPublicAddress() async throws {
        let http = FakeHTTP()
        // Built with the throwing initialiser, which (unlike parse) does not refuse early.
        let publicProbe = GroundHealthProbe(
            endpoint: try GroundEndpoint(host: "8.8.8.8"), apiKey: GroundAPIKey("test-key"), http: http,
            resolver: { _ in XCTFail("a literal needs no lookup"); return [] }
        )
        let result = await publicProbe.run()
        XCTAssertEqual(result, .addressNotAllowed(GroundAddressRefusal.cleartext(address: address("8.8.8.8")).reason))
        XCTAssertEqual(http.requests.count, 0, "no request, so the key was never sent")
        XCTAssertFalse(result.description.contains("test-key"))
    }

    func testTheProbeSendsNothingWhenOneResolvedAddressIsPublic() async throws {
        let http = FakeHTTP()
        let p = try probe("ws://ground.example:5555", resolver: { _ in [self.address("192.168.1.20"), self.address("1.1.1.1")] }, http: http)
        let result = await p.run()
        guard case .addressNotAllowed = result else { return XCTFail("expected a refusal, got \(result)") }
        XCTAssertEqual(http.requests.count, 0)
    }

    func testTheProbeConnectsToTheCheckedAddressWithTheOriginalHost() async throws {
        let http = FakeHTTP()
        http.responses["/api/health"] = (200, Data(#"{"status":"ok","version":"1.0"}"#.utf8))
        http.responses["/api/status"] = (200, Data(#"{"running":false,"source":"sim"}"#.utf8))
        let calls = CallCounter()
        let resolver: GroundHostResolver = { _ in
            calls.increment() == 1 ? [self.address("192.168.1.20")] : [self.address("8.8.8.8")]
        }
        let result = await (try probe("ws://ground.local:5555", resolver: resolver, http: http)).run()
        XCTAssertEqual(result, .ok(version: "1.0"))
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(http.requests.map(\.url.absoluteString), ["http://192.168.1.20:5555/api/health", "http://192.168.1.20:5555/api/status"])
        XCTAssertTrue(http.requests.allSatisfy { $0.headers["Host"] == "ground.local:5555" })
    }

    func testTheProbeKeepsTheNameForHttps() async throws {
        let http = FakeHTTP()
        http.responses["/api/health"] = (200, Data(#"{"status":"ok"}"#.utf8))
        http.responses["/api/status"] = (200, Data(#"{"running":false,"source":"sim"}"#.utf8))
        let result = await (try probe("https://ground.example:5555", resolver: { _ in [self.address("8.8.8.8")] }, http: http)).run()
        XCTAssertEqual(result, .ok(version: nil))
        XCTAssertEqual(http.requests.first?.url.absoluteString, "https://ground.example:5555/api/health")
    }

    // MARK: - Transport

    private final class Hits: @unchecked Sendable {
        private let lock = NSLock()
        private var n = 0
        func add() { lock.withLock { n += 1 } }
        var count: Int { lock.withLock { n } }
    }

    func testTheTransportOpensNoSocketForAPublicAddress() async throws {
        let transport = NWGroundTransport(
            url: URL(string: "ws://8.8.8.8:5555/socket.io/?EIO=4&transport=websocket")!,
            maxFrameBytes: 1024, openTimeout: 2, networkMessageLimit: true,
            resolver: { _ in XCTFail("a literal needs no lookup"); return [] }
        )
        do {
            try await transport.connect()
            XCTFail("a public cleartext address was connected")
        } catch let refusal as GroundAddressRefusal {
            XCTAssertTrue(refusal.reason.hasPrefix(GroundAddressRefusal.cleartextMessage))
        }
        XCTAssertFalse(transport.hasConnection, "no NWConnection was ever made")
        // And nothing can be sent afterwards.
        do {
            try await transport.send("never")
            XCTFail("sent without a connection")
        } catch {
            XCTAssertEqual(error as? GroundTransportError, .closed(code: nil))
        }
    }

    func testTheTransportWritesZeroBytesWhenOneAddressIsPublic() async throws {
        let hits = Hits()
        let server = try LoopbackServer { _, _ in hits.add() }
        let port = try await server.start()
        defer { server.stop() }
        // The name resolves to loopback (where the test server listens) and to a public address.
        let mixed: GroundHostResolver = { _ in [self.address("127.0.0.1"), self.address("8.8.8.8")] }
        let transport = NWGroundTransport(
            url: URL(string: "ws://ground.example:\(port)/socket.io/?EIO=4&transport=websocket")!,
            maxFrameBytes: 1024, openTimeout: 2, networkMessageLimit: true, resolver: mixed
        )
        do {
            try await transport.connect()
            XCTFail("connected despite a public address")
        } catch {
            XCTAssertTrue(error is GroundAddressRefusal)
        }
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(hits.count, 0, "the listener saw no connection")
    }

    func testTheTransportConnectsToTheCheckedAddressNotTheName() async throws {
        let hits = Hits()
        let server = try LoopbackServer { head, connection in
            hits.add()
            try? await connection.send(LoopbackServer.upgradeResponse(for: head))
        }
        let port = try await server.start()
        defer { server.stop() }
        let calls = CallCounter()
        let resolver: GroundHostResolver = { _ in
            calls.increment() == 1 ? [self.address("127.0.0.1")] : [self.address("8.8.8.8")]
        }
        let transport = NWGroundTransport(
            url: URL(string: "ws://ground.example:\(port)/socket.io/?EIO=4&transport=websocket")!,
            maxFrameBytes: 1024, openTimeout: 5, networkMessageLimit: true, resolver: resolver
        )
        try await transport.connect()
        await transport.close()
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(hits.count, 1)
    }

    func testClientReportsTheRefusalWithoutTheKey() async throws {
        let transport = NWGroundTransport(
            url: URL(string: "ws://8.8.8.8:5555/socket.io/?EIO=4&transport=websocket")!,
            maxFrameBytes: 1024, openTimeout: 2, networkMessageLimit: true
        )
        do {
            try await transport.connect()
            XCTFail("connected")
        } catch {
            let text = GroundClient.describe(error)
            XCTAssertTrue(text.contains("cleartext is only allowed to local network addresses"))
            XCTAssertTrue(text.contains("wss:// or https://"))
            XCTAssertFalse(text.contains("test-key"))
        }
    }
}

final class CallCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    /// Adds one and returns the new count.
    func increment() -> Int { lock.withLock { n += 1; return n } }
    var count: Int { lock.withLock { n } }
}
