import Foundation
import Network
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

    /// What `getaddrinfo` reads for a numeric host (no DNS: AI_NUMERICHOST).
    private func systemReading(_ text: String) -> [UInt8]? {
        var hints = addrinfo()
        hints.ai_family = AF_INET
        hints.ai_flags = AI_NUMERICHOST
        var head: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(text, nil, &hints, &head) == 0, let first = head else { return nil }
        defer { freeaddrinfo(first) }
        let value = first.pointee.ai_addr.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
        return withUnsafeBytes(of: value) { Array($0) }
    }

    func testThePolicyReadsOnlyPlainDottedQuadsAndAgreesWithTheSystem() {
        // Every spelling the policy accepts is read exactly as getaddrinfo reads it.
        for text in ["127.0.0.1", "10.0.0.1", "192.168.1.10", "172.16.5.4", "8.8.8.8", "0.0.0.0", "255.255.255.255", "169.254.9.9"] {
            XCTAssertEqual(LanAddressPolicy.numericAddress(text)?.bytes, systemReading(text), text)
        }
        // Spellings the system reads in its own (and inconsistent) ways are not read by the
        // policy; they are refused when typed and judged by the resolver's bytes otherwise.
        for text in ["0x7f.1", "2130706433", "0177.0.0.1", "010.8.8.8", "192.168.001.010", "0x08080808", "127.1", "1.2.3", "1.2.3.4.5", "256.1.1.1", "1..1.1", "+1.2.3.4"] {
            XCTAssertNil(LanAddressPolicy.numericAddress(text), text)
        }
        for text in ["0x7f.1", "2130706433", "0177.0.0.1", "010.8.8.8", "192.168.001.010", "127.1"] {
            XCTAssertTrue(LanAddressPolicy.isLegacyIPv4Spelling(text), text)
        }
        for text in ["127.0.0.1", "ground.local", "example.com", "dead.beef", "10.0.0.1"] {
            XCTAssertFalse(LanAddressPolicy.isLegacyIPv4Spelling(text), text)
        }
    }

    func testALegacySpellingReachingThePlannerIsJudgedByTheResolversBytes() {
        // Not a literal, so it goes to the resolver, and the answer is checked.
        let resolver: GroundHostResolver = { _ in [self.address("8.8.8.8")] }
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddresses(host: "0x08080808", cleartext: true, resolver: resolver))
    }

    // MARK: - Names

    func testACleartextNameKeepsOnlyItsAllowedAddressesIPv4First() throws {
        // A dual-stack .local name: link-local v6, global v6, private v4.
        let dual: GroundHostResolver = { _ in [self.address("fe80::1"), self.address("2601:1:2::3"), self.address("192.168.1.20")] }
        let kept = try LanAddressPolicy.checkedAddresses(host: "ground.local", cleartext: true, resolver: dual)
        XCTAssertEqual(kept, [address("192.168.1.20"), address("fe80::1")])
        // A public address among private ones is never connected to.
        let mixed: GroundHostResolver = { _ in [self.address("8.8.8.8"), self.address("10.0.0.5")] }
        XCTAssertEqual(try LanAddressPolicy.checkedAddresses(host: "ground.lan", cleartext: true, resolver: mixed), [address("10.0.0.5")])
        // Only public answers: refused.
        let publicOnly: GroundHostResolver = { _ in [self.address("8.8.8.8"), self.address("2001:4860:4860::8888")] }
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddresses(host: "ground.example", cleartext: true, resolver: publicOnly)) { error in
            let refusal = error as? GroundAddressRefusal
            XCTAssertTrue(refusal?.reason.contains("only allowed to local network addresses") == true)
            XCTAssertTrue(refusal?.reason.contains("8.8.8.8") == true)
        }
        // localhost: [::1, 127.0.0.1] is tried as 127.0.0.1 first.
        let localhost: GroundHostResolver = { _ in [self.address("::1"), self.address("127.0.0.1")] }
        XCTAssertEqual(try LanAddressPolicy.checkedAddresses(host: "localhost", cleartext: true, resolver: localhost), [address("127.0.0.1"), address("::1")])
    }

    func testAnEmptyAnswerAndAResolverFailureAreReadableLookupErrors() {
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddresses(host: "x.example", cleartext: true, resolver: { _ in [] })) { error in
            XCTAssertEqual(error as? GroundLookupError, .notFound(host: "x.example"))
            XCTAssertEqual((error as? GroundLookupError)?.description, "Could not find x.example on the network")
        }
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddresses(host: "x.example", cleartext: true, resolver: { _ in
            throw GroundTransportError.failed("the host name did not resolve")
        })) { error in
            XCTAssertEqual(error as? GroundLookupError, .notFound(host: "x.example"))
        }
    }

    func testALiteralNeedsNoLookup() throws {
        let resolver: GroundHostResolver = { _ in
            XCTFail("a literal must not be looked up")
            return []
        }
        XCTAssertEqual(try LanAddressPolicy.checkedAddresses(host: "10.1.2.3", cleartext: true, resolver: resolver), [address("10.1.2.3")])
        XCTAssertEqual(try LanAddressPolicy.checkedAddresses(host: "[fd00::1]", cleartext: true, resolver: resolver), [address("fd00::1")])
        XCTAssertThrowsError(try LanAddressPolicy.checkedAddresses(host: "8.8.8.8", cleartext: true, resolver: resolver))
    }

    // MARK: - Encrypted connections

    func testEncryptedUrlsAreNotLookedUpOrRewritten() throws {
        let resolver: GroundHostResolver = { _ in
            XCTFail("an encrypted URL must not be looked up by the policy")
            return []
        }
        let secure = URL(string: "wss://ground.example:5555/socket.io/?EIO=4&transport=websocket")!
        let target = try GroundConnectPlanner.plan(url: secure, resolver: resolver)
        XCTAssertEqual(target.attempts, [secure], "the name stays, so TLS and the Host header use it")
        XCTAssertNil(target.hostHeader)
        XCTAssertFalse(target.isCleartext)
        let publicName: GroundHostResolver = { _ in [self.address("8.8.8.8")] }
        XCTAssertThrowsError(try GroundConnectPlanner.plan(url: URL(string: "ws://ground.example:5555/")!, resolver: publicName))
        XCTAssertNoThrow(try GroundEndpoint.parse("wss://8.8.8.8:5555"))
        XCTAssertNoThrow(try GroundEndpoint.parse("https://8.8.8.8"))
    }

    // MARK: - Rebinding

    func testTheConnectionGoesToTheFirstCheckedAddresses() throws {
        // First answer: private. Any later answer: public. The plan resolves once.
        let calls = CallCounter()
        let resolver: GroundHostResolver = { _ in
            calls.increment() == 1 ? [self.address("192.168.1.20")] : [self.address("8.8.8.8")]
        }
        let url = URL(string: "ws://ground.example:5555/socket.io/?EIO=4&transport=websocket")!
        let target = try GroundConnectPlanner.plan(url: url, resolver: resolver)
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(target.attempts.map(\.absoluteString), ["ws://192.168.1.20:5555/socket.io/?EIO=4&transport=websocket"])
        XCTAssertEqual(target.addresses, [address("192.168.1.20")])
        XCTAssertEqual(target.hostHeader, "ground.example:5555", "the original name is the Host header")
    }

    func testEveryAllowedAddressBecomesAnAttemptAndIPv6KeepsItsBrackets() throws {
        let url = URL(string: "ws://ground.local:5555/x")!
        let target = try GroundConnectPlanner.plan(url: url, resolver: { _ in
            [self.address("fd00::7"), self.address("8.8.8.8"), self.address("192.168.1.20")]
        })
        XCTAssertEqual(target.attempts.map(\.absoluteString), ["ws://192.168.1.20:5555/x", "ws://[fd00::7]:5555/x"])
        let v6 = try GroundConnectPlanner.plan(url: URL(string: "ws://[fd00::1]:5600/x")!, resolver: { _ in [] })
        XCTAssertEqual(v6.hostHeader, "[fd00::1]:5600", "an IPv6 Host header is bracketed")
    }

    // MARK: - Endpoint parsing

    func testParseRefusesPublicCleartextLiteralsEarly() {
        for text in ["ws://8.8.8.8:5555", "8.8.8.8", "http://1.1.1.1", "ws://100.64.0.1:5555", "ws://[2001:4860:4860::8888]:5555",
                     "ws://0.0.0.0", "ws://255.255.255.255", "ws://224.0.0.1", "ws://172.15.0.1", "ws://172.32.0.1"] {
            XCTAssertThrowsError(try GroundEndpoint.parse(text), text) { error in
                guard case let .cleartextNotAllowed(reason)? = error as? GroundConfigurationError else {
                    return XCTFail("wrong error for \(text): \(error)")
                }
                XCTAssertEqual(reason, GroundAddressRefusal.cleartextMessage)
            }
        }
    }

    func testParseRefusesNonDecimalIPv4Spellings() {
        for text in ["ws://0x7f.1:5555", "ws://0x08080808:5555", "ws://134744072", "ws://2130706433", "ws://0177.0.0.1",
                     "ws://192.168.001.010", "ws://010.8.8.8", "ws://127.1", "wss://0x08080808"] {
            XCTAssertThrowsError(try GroundEndpoint.parse(text), text) { error in
                XCTAssertEqual(error as? GroundConfigurationError, .invalidHost(String(text.split(separator: "/").last!.split(separator: ":").first!)), text)
                XCTAssertTrue((error as? GroundConfigurationError)?.description.contains("four decimal numbers like 192.168.1.20") == true)
            }
        }
    }

    func testParseAcceptsLocalLiteralsAndNames() throws {
        for text in ["ws://192.168.1.20:5555", "10.0.0.1", "ws://172.16.0.9", "ws://[fd00::1]:5555", "ws://127.0.0.1:5555",
                     "ws://ground.local:5555", "ws://ground.example.com:5555", "ws://localhost"] {
            XCTAssertNoThrow(try GroundEndpoint.parse(text), text)
        }
    }

    // MARK: - Health probe

    private func probe(_ text: String, resolver: @escaping GroundHostResolver, http: FakeHTTP, timeout: TimeInterval = 5) throws -> GroundHealthProbe {
        GroundHealthProbe(endpoint: try GroundEndpoint.parse(text), apiKey: GroundAPIKey("test-key"), http: http, timeout: timeout, resolver: resolver)
    }

    private func okResponses(_ http: FakeHTTP) {
        http.responses["/api/health"] = (200, Data(#"{"status":"ok","version":"1.0"}"#.utf8))
        http.responses["/api/status"] = (200, Data(#"{"running":false,"source":"sim"}"#.utf8))
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
    }

    func testTheProbeSendsNothingWhenOnlyPublicAnswersExist() async throws {
        let http = FakeHTTP()
        let p = try probe("ws://ground.example:5555", resolver: { _ in [self.address("1.1.1.1"), self.address("2606:4700::1111")] }, http: http)
        let result = await p.run()
        guard case .addressNotAllowed = result else { return XCTFail("expected a refusal, got \(result)") }
        XCTAssertEqual(http.requests.count, 0)
    }

    func testTheProbeUsesOnlyTheAllowedAnswersOfADualStackName() async throws {
        let http = FakeHTTP()
        okResponses(http)
        let result = await (try probe("ws://ground.local:5555", resolver: { _ in
            [self.address("2601:1:2::3"), self.address("192.168.1.20")]
        }, http: http)).run()
        XCTAssertEqual(result, .ok(version: "1.0"))
        XCTAssertTrue(http.requests.allSatisfy { $0.url.host == "192.168.1.20" }, "never the public address")
        XCTAssertTrue(http.requests.allSatisfy { $0.headers["Host"] == "ground.local:5555" })
    }

    func testTheProbeConnectsToTheCheckedAddressAndLooksUpOnce() async throws {
        let http = FakeHTTP()
        okResponses(http)
        let calls = CallCounter()
        let resolver: GroundHostResolver = { _ in
            calls.increment() == 1 ? [self.address("192.168.1.20")] : [self.address("8.8.8.8")]
        }
        let result = await (try probe("ws://ground.local:5555", resolver: resolver, http: http)).run()
        XCTAssertEqual(result, .ok(version: "1.0"))
        XCTAssertEqual(calls.count, 1)
        XCTAssertEqual(http.requests.map(\.url.absoluteString), ["http://192.168.1.20:5555/api/health", "http://192.168.1.20:5555/api/status"])
    }

    func testTheProbeTriesTheNextAddressWhenOneIsUnreachable() async throws {
        let http = FakeHTTP()
        okResponses(http)
        http.failingHosts = ["10.0.0.9"]
        let result = await (try probe("ws://ground.lan:5555", resolver: { _ in [self.address("10.0.0.9"), self.address("fd00::7")] }, http: http)).run()
        XCTAssertEqual(result, .ok(version: "1.0"))
        XCTAssertEqual(http.requests.first?.url.host, "10.0.0.9")
        XCTAssertEqual(http.requests.last?.url.absoluteString, "http://[fd00::7]:5555/api/status")
        // All unreachable: one typed result.
        http.failingHosts = ["10.0.0.9", "fd00::7", "[fd00::7]"]
        let failed = await (try probe("ws://ground.lan:5555", resolver: { _ in [self.address("10.0.0.9"), self.address("fd00::7")] }, http: http)).run()
        XCTAssertEqual(failed, .unreachable("URLError \(URLError.Code.cannotConnectToHost.rawValue)"))
    }

    func testTheProbeKeepsTheNameForHttpsAndDoesNotLookItUp() async throws {
        let http = FakeHTTP()
        okResponses(http)
        let result = await (try probe("https://ground.example:5555", resolver: { _ in
            XCTFail("an https URL needs no lookup"); return []
        }, http: http)).run()
        XCTAssertEqual(result, .ok(version: "1.0"))
        XCTAssertEqual(http.requests.first?.url.absoluteString, "https://ground.example:5555/api/health")
        XCTAssertNil(http.requests.first?.headers["Host"])
    }

    func testAFailedLookupIsReadable() async throws {
        let http = FakeHTTP()
        let result = await (try probe("ws://ground.local:5555", resolver: { _ in throw GroundTransportError.failed("nope") }, http: http)).run()
        XCTAssertEqual(result, .unreachable("Could not find ground.local on the network"))
        XCTAssertEqual(http.requests.count, 0)
    }

    func testTheProbeGivesUpWhenTheLookupHangs() async throws {
        let gate = DispatchSemaphore(value: 0)
        defer { gate.signal() }
        let http = FakeHTTP()
        let started = Date()
        let result = await (try probe("ws://ground.local:5555", resolver: { _ in gate.wait(); return [] }, http: http, timeout: 0.3)).run()
        XCTAssertEqual(result, .unreachable("Looking up ground.local timed out"))
        XCTAssertLessThan(Date().timeIntervalSince(started), 2)
        XCTAssertEqual(http.requests.count, 0)
    }

    // MARK: - Proxies and cellular

    func testCleartextProbeConfigurationBypassesProxiesAndCellular() {
        let cleartext = URLSessionGroundHTTPClient.makeConfiguration(for: URL(string: "http://192.168.1.20:5555/api/status")!)
        XCTAssertEqual(cleartext.connectionProxyDictionary?.count, 0, "an empty dictionary overrides system, PAC and MDM proxies")
        XCTAssertFalse(cleartext.allowsCellularAccess)
        let encrypted = URLSessionGroundHTTPClient.makeConfiguration(for: URL(string: "https://ground.example/api/status")!)
        XCTAssertNil(encrypted.connectionProxyDictionary)
        XCTAssertTrue(encrypted.allowsCellularAccess)
    }

    func testCleartextSocketParametersPreferNoProxiesAndAvoidCellular() {
        let cleartext = NWGroundTransport.makeParameters(cleartext: true, hostHeader: "ground.local:5555", maxFrameBytes: 1024, networkMessageLimit: true, connectTimeout: 3)
        XCTAssertTrue(cleartext.preferNoProxies)
        XCTAssertTrue(cleartext.prohibitedInterfaceTypes?.contains(.cellular) == true)
        let encrypted = NWGroundTransport.makeParameters(cleartext: false, hostHeader: nil, maxFrameBytes: 1024, networkMessageLimit: true, connectTimeout: 3)
        XCTAssertFalse(encrypted.preferNoProxies)
    }

    func testTheKeyIsSentOnlyAfterTheEngineOpenFrame() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.transport(0)
        try await Task.sleep(nanoseconds: 150_000_000)
        XCTAssertEqual(transport.sent, [], "nothing, and so not the key, is sent before the server's open frame")
        try await harness.handshake(0)
        XCTAssertEqual(transport.sent.count, 1)
        await harness.client.shutdown()
    }

    // MARK: - Transport

    private final class Hits: @unchecked Sendable {
        private let lock = NSLock()
        private var n = 0
        func add() { lock.withLock { n += 1 } }
        var count: Int { lock.withLock { n } }
    }

    private func socketURL(_ host: String, _ port: UInt16) -> URL {
        URL(string: "ws://\(host):\(port)/socket.io/?EIO=4&transport=websocket")!
    }

    private func transport(_ url: URL, resolver: @escaping GroundHostResolver = GroundSystemResolver.resolve, timeout: TimeInterval = 5) -> NWGroundTransport {
        NWGroundTransport(url: url, maxFrameBytes: 1024, openTimeout: timeout, networkMessageLimit: true, resolver: resolver)
    }

    func testTheTransportOpensNoSocketForAPublicAddress() async throws {
        let t = transport(socketURL("8.8.8.8", 5555), resolver: { _ in XCTFail("a literal needs no lookup"); return [] }, timeout: 2)
        do {
            try await t.connect()
            XCTFail("a public cleartext address was connected")
        } catch let refusal as GroundAddressRefusal {
            XCTAssertTrue(refusal.reason.hasPrefix(GroundAddressRefusal.cleartextMessage))
        }
        XCTAssertFalse(t.hasConnection, "no NWConnection was ever made")
        do {
            try await t.send("never")
            XCTFail("sent without a connection")
        } catch {
            XCTAssertEqual(error as? GroundTransportError, .closed(code: nil))
        }
    }

    func testPublicOnlyAnswersReachNoListener() async throws {
        let hits = Hits()
        let server = try LoopbackServer { _, _ in hits.add() }
        let port = try await server.start()
        defer { server.stop() }
        let t = transport(socketURL("ground.example", port), resolver: { _ in [self.address("8.8.8.8"), self.address("1.1.1.1")] }, timeout: 2)
        do {
            try await t.connect()
            XCTFail("connected despite public answers")
        } catch {
            XCTAssertTrue(error is GroundAddressRefusal)
        }
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(hits.count, 0, "the listener saw no connection")
    }

    func testTheTransportConnectsToTheCheckedAddressWithTheOriginalHost() async throws {
        let server = try LoopbackServer { head, connection in
            try? await connection.send(LoopbackServer.upgradeResponse(for: head))
        }
        let port = try await server.start()
        defer { server.stop() }
        let calls = CallCounter()
        // The public answer is skipped; the second lookup would be public, but there is none.
        let resolver: GroundHostResolver = { _ in
            calls.increment() == 1 ? [self.address("8.8.8.8"), self.address("127.0.0.1")] : [self.address("8.8.8.8")]
        }
        let t = transport(socketURL("ground.example", port), resolver: resolver)
        try await t.connect()
        await t.close()
        XCTAssertEqual(calls.count, 1)
        let head = try XCTUnwrap(server.requestHeads.first)
        let hostLines = head.split(separator: "\r\n").filter { $0.lowercased().hasPrefix("host:") }
        XCTAssertEqual(hostLines.map { $0.lowercased() }, ["host: ground.example:\(port)"], "exactly one Host header, the original name")
    }

    func testLocalhostConnectsThroughTheSystemResolverToAnIPv4OnlyListener() async throws {
        let hits = Hits()
        let server = try LoopbackServer { head, connection in
            hits.add()
            try? await connection.send(LoopbackServer.upgradeResponse(for: head))
        }
        let port = try await server.start()
        defer { server.stop() }
        // getaddrinfo("localhost") returns ::1 and 127.0.0.1; the listener is on 127.0.0.1 only.
        let t = transport(socketURL("localhost", port))
        try await t.connect()
        await t.close()
        XCTAssertEqual(hits.count, 1)
    }

    func testTheTransportTriesTheNextAddressWhenTheFirstIsUnreachable() async throws {
        // A listener on ::1 only; the IPv4 address (tried first) has nothing listening on that port.
        let hits = Hits()
        let server: LoopbackServer
        let port: UInt16
        do {
            server = try LoopbackServer(host: "::1") { head, connection in
                hits.add()
                try? await connection.send(LoopbackServer.upgradeResponse(for: head))
            }
            port = try await server.start()
        } catch {
            throw XCTSkip("no IPv6 loopback here: \(error)")
        }
        defer { server.stop() }
        let t = transport(socketURL("ground.local", port), resolver: { _ in [self.address("::1"), self.address("127.0.0.1")] })
        try await t.connect()
        await t.close()
        XCTAssertEqual(hits.count, 1)
    }

    func testAllAddressesUnreachableGivesOneTypedError() async throws {
        let server = try LoopbackServer { _, _ in }
        let port = try await server.start()
        server.stop()
        try await Task.sleep(nanoseconds: 200_000_000)
        let t = transport(socketURL("ground.local", port), resolver: { _ in [self.address("127.0.0.1"), self.address("::1")] }, timeout: 4)
        do {
            try await t.connect()
            XCTFail("nothing listens")
        } catch let error as GroundTransportError {
            guard case .failed = error else { return XCTFail("wrong error \(error)") }
        }
    }

    func testClosingWhileTheLookupHangsReturnsAtOnce() async throws {
        let gate = DispatchSemaphore(value: 0)
        defer { gate.signal() }
        let t = transport(socketURL("ground.local", 5555), resolver: { _ in gate.wait(); return [] }, timeout: 30)
        let task = Task { try await t.connect() }
        try await Task.sleep(nanoseconds: 200_000_000)
        let started = Date()
        await t.close()
        do {
            try await task.value
            XCTFail("connected")
        } catch {
            XCTAssertEqual(error as? GroundTransportError, .closed(code: nil))
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 2)
    }

    // MARK: - The whole client

    /// A client with the real transport and a resolver that answers only public addresses,
    /// next to a listener: nothing reaches the listener, and the key is in no state, event,
    /// log or error text.
    func testTheClientNeverSendsTheKeyToAPublicAnswer() async throws {
        let hits = Hits()
        let server = try LoopbackServer { _, _ in hits.add() }
        let port = try await server.start()
        defer { server.stop() }
        let resolver: GroundHostResolver = { _ in [self.address("8.8.8.8"), self.address("2606:4700::1111")] }
        let logs = LogRecorder()
        let client = GroundClient(
            configuration: GroundClient.Configuration(endpoint: try GroundEndpoint(host: "ground.example", port: Int(port)), apiKey: GroundAPIKey("test-key")),
            transportFactory: { url, max in NWGroundTransport(url: url, maxFrameBytes: max, openTimeout: 2, networkMessageLimit: true, resolver: resolver) },
            logSink: logs.sink
        )
        let recorder = EventRecorder(client.events)
        await client.connect()
        try await eventually("a refusal in the state") {
            if case let .reconnecting(_, _, reason) = await client.state { return reason.description.contains("cleartext is only allowed") }
            return false
        }
        let state = await client.state
        await client.shutdown()
        XCTAssertEqual(hits.count, 0, "zero connections, so zero bytes, reached the listener")
        var texts = [String(describing: state)] + logs.lines + recorder.events.map { String(describing: $0) }
        texts.append(GroundClient.describe(GroundAddressRefusal.cleartext(address: address("8.8.8.8"))))
        for text in texts {
            XCTAssertFalse(text.contains("test-key"), text)
        }
        XCTAssertTrue(texts.contains { $0.contains("wss:// or https://") })
    }
}

final class CallCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    /// Adds one and returns the new count.
    func increment() -> Int { lock.withLock { n += 1; return n } }
    var count: Int { lock.withLock { n } }
}
