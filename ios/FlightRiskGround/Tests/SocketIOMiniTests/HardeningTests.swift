import Foundation
import XCTest
@testable import SocketIOMini

/// Bounded delivery, redaction at the truncation boundary, flapping backoff,
/// the narrowing-only emit policy, LAND ALL edge cases and input hygiene.
final class HardeningTests: XCTestCase {
    // MARK: - Bounded delivery (finding 2)

    func testAStalledFrameConsumerKeepsOnlyTheNewestFramesWhileRepliesStillFlow() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        // Nobody reads `frames`.
        for index in 0..<100 {
            transport.push(#"42["frame",{"drone_id":"drone-1","n":\#(index)}]"#)
        }
        transport.push(#"42["land_all_ack",{"coordinator":2,"local":null,"reached_any":true,"transport":2}]"#)
        try await eventually("the ack, not stuck behind frames") {
            harness.recorder.serverEvents.contains { $0.name == "land_all_ack" }
        }
        XCTAssertFalse(harness.recorder.serverEvents.contains { $0.name == "frame" })
        try await eventually("old frames dropped") { harness.client.frames.droppedCount == 92 }
        let frames = FrameRecorder(harness.client.frames)
        try await eventually("the newest frames") { frames.frames.count == 8 }
        let numbers = try frames.frames.map { frame -> Int in
            let object = try JSONSerialization.jsonObject(with: try XCTUnwrap(frame.payload)) as? [String: Any]
            return try XCTUnwrap(object?["n"] as? Int)
        }
        XCTAssertEqual(numbers, Array(92..<100))
        await harness.client.shutdown()
    }

    func testAStalledEventConsumerGetsAnExplicitDropMarkerAtTheGap() async throws {
        let harness = try Harness(recordEvents: false, adjust: { $0.eventBufferLimit = 32 })
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        for index in 0..<100 {
            transport.push(#"42["status",{"n":\#(index)}]"#)
        }
        // connecting, connected and reconnected take three places; 29 events fit.
        try await eventually("drops counted") { harness.client.events.droppedCount == 71 }

        var iterator = harness.client.events.makeAsyncIterator()
        var received: [GroundEvent] = []
        for _ in 0..<33 {
            received.append(try XCTUnwrapAsync(await iterator.next()))
        }
        XCTAssertEqual(received.prefix(3).map(Self.kind), ["state", "state", "reconnected"])
        let numbers = received[3..<32].compactMap(Self.number)
        XCTAssertEqual(numbers, Array(0..<29))
        XCTAssertEqual(received[32], .eventsDropped(count: 71))
        // Delivery resumes once the consumer catches up.
        transport.push(#"42["status",{"n":100}]"#)
        let next = await iterator.next()
        XCTAssertEqual(next.flatMap(Self.number), 100)
        await harness.client.shutdown()
    }

    func testTheEventBufferIsBoundedByBytesAndPlacesTheMarkerAtTheGap() async {
        let buffer = GroundEventBuffer<Int>(maxCount: 4, maxBytes: 1000, overflow: .dropIncoming(marker: { -$0 }))
        for value in 1...4 {
            XCTAssertTrue(buffer.push(value, bytes: 10))
        }
        XCTAssertFalse(buffer.push(5, bytes: 10))
        var iterator = buffer.stream.makeAsyncIterator()
        let first = await iterator.next()
        XCTAssertEqual(first, 1)
        XCTAssertFalse(buffer.push(6, bytes: 10), "the marker needs a place too")
        let second = await iterator.next()
        XCTAssertEqual(second, 2)
        XCTAssertTrue(buffer.push(7, bytes: 10))
        var rest: [Int] = []
        for _ in 0..<4 {
            rest.append(await iterator.next() ?? 0)
        }
        XCTAssertEqual(rest, [3, 4, -2, 7])
        XCTAssertEqual(buffer.droppedTotal, 2)

        let bytes = GroundEventBuffer<Int>(maxCount: 100, maxBytes: 100, overflow: .dropIncoming(marker: { -$0 }))
        XCTAssertTrue(bytes.push(1, bytes: 40))
        XCTAssertTrue(bytes.push(2, bytes: 40))
        XCTAssertFalse(bytes.push(3, bytes: 40), "120 bytes exceed the 100-byte bound")

        let lossy = GroundEventBuffer<Int>(maxCount: 100, maxBytes: 100, overflow: .dropOldest)
        for value in 1...5 {
            lossy.push(value, bytes: 40)
        }
        XCTAssertFalse(lossy.push(6, bytes: 400), "one element larger than the bound is dropped")
        lossy.finish()
        var kept: [Int] = []
        for await value in lossy.stream {
            kept.append(value)
        }
        XCTAssertEqual(kept, [4, 5])
    }

    func testAWaitingConsumerIsReleasedByCancellationAndFinish() async throws {
        let buffer = GroundEventBuffer<Int>(maxCount: 4, maxBytes: 100, overflow: .dropOldest)
        let waiting = Task { await buffer.stream.makeAsyncIterator().nextValue() }
        try await Task.sleep(nanoseconds: 20_000_000)
        waiting.cancel()
        let cancelled = await waiting.value
        XCTAssertNil(cancelled)
        let second = Task { await buffer.stream.makeAsyncIterator().nextValue() }
        try await Task.sleep(nanoseconds: 20_000_000)
        buffer.finish()
        let finished = await second.value
        XCTAssertNil(finished)
    }

    func testPreAckEventsAreBoundedByBytes() async throws {
        let harness = try Harness(adjust: { $0.pendingEventBytes = 300 })
        await harness.client.connect()
        let transport = try await harness.transport(0)
        transport.push(Harness.openFrame)
        let padding = String(repeating: "x", count: 100)
        for index in 0..<10 {
            transport.push(#"42["status",{"n":\#(index),"p":"\#(padding)"}]"#)
        }
        try await eventually("connect frame") { transport.sent.count == 1 }
        transport.push(#"40{"sid":"s"}"#)
        transport.push(#"42["fleet_status",{}]"#)
        try await eventually("fleet_status") { harness.recorder.serverEvents.contains { $0.name == "fleet_status" } }
        // Each held event is about 130 bytes, so two fit in 300 bytes.
        XCTAssertEqual(harness.recorder.serverEvents.map(\.name), ["status", "status", "fleet_status"])
        await harness.client.shutdown()
    }

    // MARK: - Redact before truncating (finding 3)

    func testAKeyCrossingTheTruncationBoundaryIsFullyRedacted() async throws {
        let key = "test-key-redaction-sentinel"
        let harness = try Harness(key: key)
        await harness.client.connect()
        let transport = try await harness.transport(0)
        transport.push(Harness.openFrame)
        try await eventually("connect frame") { transport.sent.count == 1 }
        let lead = String(repeating: "x", count: 190)
        transport.push(#"44{"message":"\#(lead)\#(key) and more"}"#)
        try await eventually("failed") { if case .failed = await harness.client.state { return true } else { return false } }
        let state = await harness.client.state
        XCTAssertEqual(state, .failed(.authenticationRejected(serverMessage: lead + "<redacted>", retryIn: nil)))
        let fragment = String(key.prefix(6))
        let texts = harness.logs.lines + harness.recorder.states.map(\.description)
        for text in texts {
            XCTAssertFalse(text.contains(fragment), "part of the key leaked into: \(text)")
        }
        await harness.client.shutdown()
    }

    // MARK: - Flapping backoff (finding 5)

    func testAFlappingServerKeepsBackingOffUntilALinkStaysUp() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let expected: [Duration] = [.seconds(1), .seconds(2), .seconds(4)]
        for (index, delay) in expected.enumerated() {
            let transport = try await harness.handshake(index)
            transport.serverDrop()
            try await eventually("reconnecting \(index + 1)") {
                await harness.client.state == .reconnecting(attempt: index + 1, retryIn: delay, reason: .transportError("SocketIOMiniTests.FakeTransportError 1"))
            }
            harness.clock.advance(by: delay)
        }
        // A link that stays up for one ping interval (25 s) resets the backoff.
        let stable = try await harness.handshake(3)
        harness.clock.advance(by: .seconds(25))
        stable.serverDrop()
        try await eventually("reset backoff") {
            if case .reconnecting(1, .seconds(1), _) = await harness.client.state { return true }
            return false
        }
        await harness.client.shutdown()
    }

    // MARK: - Narrowing-only policy (finding 6)

    func testACustomPolicyCannotAllowAForbiddenEvent() async throws {
        let permissive = GroundEmitPolicy(narrowing: { _ in true })
        let forbidden = try Fixtures.cases("client_emits.json").values
            .filter { $0["allowed"] as? Bool == false }
            .compactMap { $0["event"] as? String }
        XCTAssertFalse(forbidden.isEmpty)
        for name in forbidden + ["match_alert_response", "anything_else"] {
            XCTAssertFalse(permissive.allows(name), name)
            XCTAssertFalse(permissive.restricted(by: { _ in true }).allows(name), name)
        }
        for name in GroundEmitPolicy.contractEvents {
            XCTAssertTrue(permissive.allows(name), name)
        }

        let harness = try Harness(emitPolicy: permissive)
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        do {
            _ = try await harness.client.emit("start_fleet")
            XCTFail("start_fleet was not refused")
        } catch {
            XCTAssertEqual(error as? GroundEmitError, .forbiddenEvent("start_fleet"))
        }
        XCTAssertFalse(transport.sent.contains { $0.contains("start_fleet") })
        await harness.client.shutdown()
    }

    // MARK: - LAND ALL edge cases (finding 7)

    func testLandAllBetweenOpenAndConnectAckIsNotSentAndNotQueued() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.transport(0)
        transport.push(Harness.openFrame)
        try await eventually("connect frame") { transport.sent.count == 1 }
        let result = try await harness.client.emit("land_all")
        XCTAssertEqual(result, .notSent(.notConnected))
        transport.push(#"40{"sid":"s"}"#)
        try await eventually("connected") { await harness.client.state.isConnected }
        await settle()
        XCTAssertEqual(transport.sent, [#"40{"api_key":"test-key"}"#])
        await harness.client.shutdown()
    }

    func testLandAllOnADeadSocketReportsSendFailed() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        transport.failSends()
        let result = try await harness.client.emit("land_all")
        guard case .notSent(.sendFailed) = result else {
            return XCTFail("expected sendFailed, got \(result)")
        }
        XCTAssertFalse(transport.sent.contains { $0.contains("land_all") })
        await harness.client.shutdown()
    }

    func testUnicodeLookalikesOfAllowedEventsAreRefused() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        let lookalikes = [
            "l\u{0430}nd_all",          // Cyrillic a
            "land_all\u{200B}",         // zero-width space
            "\u{FF4C}\u{FF41}\u{FF4E}\u{FF44}_\u{FF41}\u{FF4C}\u{FF4C}", // fullwidth letters
            "land\u{2010}all",          // Unicode hyphen
            "LAND_ALL",
            " land_all",
            "get_fleet_statu\u{015B}",  // s with acute accent
            "drone_command\u{0}",
            "drone_comm\u{0430}nd",
        ]
        for name in lookalikes {
            do {
                _ = try await harness.client.emit(name)
                XCTFail("\(name.unicodeScalars.map { String($0.value, radix: 16) }) was not refused")
            } catch {
                XCTAssertEqual(error as? GroundEmitError, .forbiddenEvent(name))
            }
        }
        XCTAssertEqual(transport.sent, [#"40{"api_key":"test-key"}"#])
        await harness.client.shutdown()
    }

    // MARK: - Input hygiene (finding 8)

    func testPayloadsMustBeUTF8AndFramesMustNotCarryRawNULs() throws {
        let codec = SocketIOCodec()
        let utf16 = try XCTUnwrap(#"{"a":1}"#.data(using: .utf16))
        let utf16LE = try XCTUnwrap(#"{"a":1}"#.data(using: .utf16LittleEndian))
        for payload in [utf16, utf16LE] {
            XCTAssertThrowsError(try codec.encodeEvent(name: "drone_command", payload: payload)) { error in
                XCTAssertEqual(error as? SocketIOCodecError, .malformedJSON("the payload is not UTF-8 JSON text"))
            }
        }
        XCTAssertThrowsError(try codec.decode("42[\"a\",\u{0}1]")) { error in
            XCTAssertEqual(error as? SocketIOCodecError, .malformedJSON("raw NUL byte"))
        }
        XCTAssertThrowsError(try codec.decode(data: Data([0x30, 0x7B, 0x00, 0x7D])))
    }

    func testOnlyWellFormedIPv6LiteralsAreAccepted() throws {
        for bad in [":::", "g::", "::g", "1::2::3", "::1::", "12345::1"] {
            XCTAssertThrowsError(try GroundEndpoint(host: bad), bad)
        }
        for good in ["::1", "fe80::1", "[2001:db8::7]"] {
            XCTAssertNoThrow(try GroundEndpoint(host: good), good)
        }
    }

    func testExtremeBackoffSettingsCannotCrash() {
        let huge = GroundBackoff(initialDelay: .seconds(Int64.max), maximumDelay: .seconds(Int64.max))
        XCTAssertEqual(huge.delay(forAttempt: 50, random: 0), .seconds(3600))
        var weird = GroundBackoff(initialDelay: .seconds(-5), maximumDelay: .seconds(-1))
        XCTAssertEqual(weird.delay(forAttempt: 3, random: .nan), .zero)
        weird.jitterFraction = .nan
        XCTAssertEqual(GroundBackoff().delay(forAttempt: 1, random: .infinity), .seconds(1))
        _ = weird.delay(forAttempt: Int.max, random: .nan)
    }

    // MARK: - Helpers

    private static func kind(_ event: GroundEvent) -> String {
        switch event {
        case .stateChanged: return "state"
        case .reconnected: return "reconnected"
        case .server: return "server"
        case .eventsDropped: return "dropped"
        }
    }

    private static func number(_ event: GroundEvent) -> Int? {
        guard case let .server(server) = event, let payload = server.payload,
              let object = try? JSONSerialization.jsonObject(with: payload) as? [String: Any]
        else { return nil }
        return object["n"] as? Int
    }
}

private extension GroundEventStream.Iterator {
    func nextValue() async -> Element? {
        var copy = self
        return await copy.next()
    }
}

private func XCTUnwrapAsync<T>(_ value: T?, file: StaticString = #filePath, line: UInt = #line) throws -> T {
    try XCTUnwrap(value, file: file, line: line)
}
