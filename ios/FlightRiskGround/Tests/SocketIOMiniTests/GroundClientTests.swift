import Foundation
import XCTest
@testable import SocketIOMini

/// R-4 to R-6 and the safety properties, with a fake transport and a manual clock.
final class GroundClientTests: XCTestCase {
    // MARK: - Connect and auth

    func testConnectOpensTheWebSocketURLAndSendsTheKeyOnlyInTheConnectFrame() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)

        XCTAssertEqual(transport.url.absoluteString, "ws://127.0.0.1:6100/socket.io/?EIO=4&transport=websocket")
        XCTAssertEqual(transport.sent, [try Fixtures.frame("socketio_connect_with_auth")])
        XCTAssertEqual(transport.maxFrameBytes, SocketIOCodec.defaultMaxFrameBytes)
        try await eventually("reconnected event") { harness.recorder.reconnects.count == 1 }
        let info = try XCTUnwrap(harness.recorder.reconnects.first)
        XCTAssertEqual(info.sid, "sio-1")
        XCTAssertEqual(info.connectCount, 1)
        XCTAssertFalse(info.isReconnect)
        XCTAssertEqual(info.pingInterval, .milliseconds(25000))
        XCTAssertEqual(info.pingTimeout, .milliseconds(20000))
        XCTAssertEqual(harness.recorder.states.first, .connecting(attempt: 1))
        await harness.client.shutdown()
    }

    func testServerEventsArriveInOrderWithTheirRawPayloads() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        let status = #"{"appearance_only":true,"connected":true,"fleet":{"drones":2,"net":"station","synced":false},"source":"edge","tiled":false}"#
        transport.push(#"42["status","# + status + "]")
        transport.push(try Fixtures.frame("socketio_event_server_to_client"))
        try await eventually("two server events") { harness.recorder.serverEvents.count == 2 }
        let events = harness.recorder.serverEvents
        XCTAssertEqual(events[0].name, "status")
        XCTAssertEqual(events[0].payload, Data(status.utf8))
        XCTAssertEqual(events[1].name, "land_all_ack")
        XCTAssertEqual(events[1].payload, Data(#"{"coordinator":2,"local":null,"reached_any":true,"transport":2}"#.utf8))
        // The reconnected signal comes before any event of that session.
        let firstServerIndex = try XCTUnwrap(harness.recorder.events.firstIndex { if case .server = $0 { return true } else { return false } })
        let reconnectIndex = try XCTUnwrap(harness.recorder.events.firstIndex { if case .reconnected = $0 { return true } else { return false } })
        XCTAssertLessThan(reconnectIndex, firstServerIndex)
        await harness.client.shutdown()
    }

    func testUnknownKeyPayloadsReachTheAppUnchanged() async throws {
        let harness = try Harness()
        let frames = FrameRecorder(harness.client.frames)
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        let cases = try Fixtures.cases("unknown_keys.json")
            .filter { $0.value["direction"] as? String == "server_to_client" }
            .sorted(by: { $0.key < $1.key })
        for (_, fixture) in cases {
            let array = try JSONSerialization.data(withJSONObject: [fixture["event"]!, fixture["payload"]!])
            transport.push("42" + String(decoding: array, as: UTF8.self))
        }
        let isFrame: ((String, [String: Any])) -> Bool = { $0.1["event"] as? String == "frame" }
        let reliable = cases.filter { !isFrame($0) }
        let lossy = cases.filter(isFrame)
        XCTAssertEqual(lossy.count, 1)
        try await eventually("all unknown-key events") {
            harness.recorder.serverEvents.count == reliable.count && frames.frames.count == lossy.count
        }
        for (event, (_, fixture)) in zip(harness.recorder.serverEvents + frames.frames, reliable + lossy) {
            XCTAssertEqual(event.name, fixture["event"] as? String)
            let payload = try JSONSerialization.jsonObject(with: try XCTUnwrap(event.payload))
            XCTAssertEqual(payload as? NSObject, fixture["payload"] as? NSObject)
        }
        let connected = await harness.client.state.isConnected
        XCTAssertTrue(connected)
        await harness.client.shutdown()
    }

    func testServerEventAskingForAnAckIsAnswered() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        transport.push(#"427["ping_me",{}]"#)
        try await eventually("ack frame") { transport.sent.contains("437[]") }
        await harness.client.shutdown()
    }

    func testJunkIsIgnoredWithoutDroppingTheLink() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        transport.push("42[")
        transport.push("9")
        transport.push(#"42["connect"]"#)
        transport.pushBinary(Data([0xFF, 0x00]))
        transport.push(#"42/admin,["status",{}]"#)
        transport.push("431[]")
        transport.push("6")
        transport.push(#"42["status",{}]"#)
        try await eventually("the valid event") { harness.recorder.serverEvents.count == 1 }
        XCTAssertEqual(harness.recorder.serverEvents.map(\.name), ["status"])
        let observed = await harness.client.state.isConnected
        XCTAssertTrue(observed)
        XCTAssertEqual(harness.network.transports.count, 1)
        await harness.client.shutdown()
    }

    func testEventsBeforeTheConnectAckAreDeliveredAfterItInOrder() async throws {
        // python-socketio runs the connect handler, which emits `status`, before
        // it sends the connect acknowledgement.
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.transport(0)
        transport.push(Harness.openFrame)
        transport.push(#"42["status",{"connected":true}]"#)
        transport.push(#"42["warning",{"message":"m"}]"#)
        try await eventually("connect frame") { transport.sent.count == 1 }
        await settle()
        XCTAssertTrue(harness.recorder.serverEvents.isEmpty, "nothing is delivered before the session is announced")
        transport.push(#"40{"sid":"s"}"#)
        transport.push(#"42["fleet_status",{}]"#)
        try await eventually("three events") { harness.recorder.serverEvents.count == 3 }
        XCTAssertEqual(harness.recorder.serverEvents.map(\.name), ["status", "warning", "fleet_status"])
        let kinds = harness.recorder.events.compactMap { event -> String? in
            switch event {
            case .reconnected: return "reconnected"
            case let .server(server): return server.name
            case .stateChanged(.connected): return "connected"
            default: return nil
            }
        }
        XCTAssertEqual(kinds, ["connected", "reconnected", "status", "warning", "fleet_status"])
        await harness.client.shutdown()
    }

    func testEventsBeforeARefusedConnectAreDiscardedAndCapped() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.transport(0)
        transport.push(Harness.openFrame)
        for _ in 0..<200 {
            transport.push(#"42["status",{}]"#)
        }
        transport.push(try Fixtures.frame("socketio_connect_error"))
        try await eventually("failed") { if case .failed = await harness.client.state { return true } else { return false } }
        await settle()
        XCTAssertTrue(harness.recorder.serverEvents.isEmpty)
        await harness.client.retryNow()
        let second = try await harness.transport(1)
        second.push(Harness.openFrame)
        for _ in 0..<200 {
            second.push(#"42["status",{}]"#)
        }
        second.push(#"40{"sid":"s"}"#)
        try await eventually("capped events") { harness.recorder.serverEvents.count == 64 }
        await settle()
        XCTAssertEqual(harness.recorder.serverEvents.count, 64)
        await harness.client.shutdown()
    }

    func testAFirstFrameThatIsNotOpenIsAProtocolError() async throws {
        let harness = try Harness()
        await harness.client.connect()
        try await harness.transport(0).push(#"42["status",{}]"#)
        try await eventually("protocol error") {
            if case .reconnecting(_, _, .protocolError) = await harness.client.state { return true }
            return false
        }
        await harness.client.shutdown()
    }

    // MARK: - R-4 ping and pong

    func testEveryServerPingIsAnsweredWithAPongAtOnce() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        for index in 1...5 {
            transport.push(try Fixtures.frame("engineio_ping"))
            try await eventually("pong \(index)") { transport.sent.filter { $0 == "3" }.count == index }
        }
        // No clock time passed: the pong does not wait for a timer.
        XCTAssertEqual(harness.clock.now, .zero)
        XCTAssertEqual(transport.sent.filter { $0 == "3" }.count, 5)
        await harness.client.shutdown()
    }

    func testNoPingWithinIntervalPlusTimeoutIsADroppedLink() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        harness.clock.advance(by: .milliseconds(44_999))
        await settle()
        let observed = await harness.client.state.isConnected
        XCTAssertTrue(observed)
        harness.clock.advance(by: .milliseconds(2))
        try await eventually("ping timeout") {
            await harness.client.state == .reconnecting(attempt: 1, retryIn: .seconds(1), reason: .pingTimeout)
        }
        XCTAssertTrue(transport.closed)
        await harness.client.shutdown()
    }

    func testPingsKeepTheLinkAliveAndTheWindowRestartsAtEachPing() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        harness.clock.advance(by: .seconds(30))
        transport.push("2")
        try await eventually("pong") { transport.sent.contains("3") }
        harness.clock.advance(by: .seconds(30))
        await settle()
        let observed = await harness.client.state.isConnected
        XCTAssertTrue(observed, "60 s after open but 30 s after the last ping")
        harness.clock.advance(by: .milliseconds(15_001))
        try await eventually("ping timeout") {
            if case .reconnecting(_, _, .pingTimeout) = await harness.client.state { return true }
            return false
        }
        await harness.client.shutdown()
    }

    func testThePingWindowComesFromTheServersOpenPacket() async throws {
        let harness = try Harness()
        await harness.client.connect()
        try await harness.handshake(0, openFrame: #"0{"sid":"e","upgrades":[],"pingTimeout":500,"pingInterval":1000}"#)
        harness.clock.advance(by: .milliseconds(1_499))
        await settle()
        let observed = await harness.client.state.isConnected
        XCTAssertTrue(observed)
        harness.clock.advance(by: .milliseconds(2))
        try await eventually("ping timeout") {
            if case .reconnecting(_, _, .pingTimeout) = await harness.client.state { return true }
            return false
        }
        await harness.client.shutdown()
    }

    func testAHostileOpenPacketCannotSwitchDeadLinkDetectionOff() async throws {
        let harness = try Harness()
        await harness.client.connect()
        try await harness.handshake(0, openFrame: #"0{"sid":"e","pingTimeout":999999999,"pingInterval":999999999}"#)
        harness.clock.advance(by: .seconds(121))
        try await eventually("ping timeout at the cap") {
            if case .reconnecting(_, _, .pingTimeout) = await harness.client.state { return true }
            return false
        }
        await harness.client.shutdown()
    }

    func testAMissingOpenPacketOrConnectAckIsAHandshakeTimeout() async throws {
        let harness = try Harness()
        await harness.client.connect()
        _ = try await harness.transport(0)
        harness.clock.advance(by: .seconds(10))
        try await eventually("handshake timeout without open") {
            await harness.client.state == .reconnecting(attempt: 1, retryIn: .seconds(1), reason: .handshakeTimeout)
        }
        harness.clock.advance(by: .seconds(1))
        let second = try await harness.transport(1)
        second.push(Harness.openFrame)
        try await eventually("connect frame") { second.sent.count == 1 }
        harness.clock.advance(by: .seconds(10))
        try await eventually("handshake timeout without connect ack") {
            await harness.client.state == .reconnecting(attempt: 2, retryIn: .seconds(2), reason: .handshakeTimeout)
        }
        await harness.client.shutdown()
    }

    // MARK: - R-5 reconnect with backoff

    func testReconnectBackoffIs1248ThenCappedAt10() async throws {
        let harness = try Harness()
        harness.network.failConnect = { _ in true }
        await harness.client.connect()
        let expected: [Duration] = [.seconds(1), .seconds(2), .seconds(4), .seconds(8), .seconds(10), .seconds(10), .seconds(10)]
        for (index, delay) in expected.enumerated() {
            let attempt = index + 1
            try await eventually("reconnecting \(attempt)") {
                if case let .reconnecting(a, d, .transportError) = await harness.client.state {
                    return a == attempt && d == delay
                }
                return false
            }
            XCTAssertEqual(harness.network.transports.count, attempt)
            harness.clock.advance(by: delay - .milliseconds(1))
            await settle()
            XCTAssertEqual(harness.network.transports.count, attempt, "no attempt before the delay ends")
            harness.clock.advance(by: .milliseconds(1))
            _ = try await harness.transport(attempt)
        }
        await harness.client.shutdown()
    }

    func testJitterOnlyShortensTheDelayWithinItsBound() {
        let backoff = GroundBackoff()
        for attempt in 1...12 {
            let full = backoff.delay(forAttempt: attempt, random: 0)
            for random in [0.0, 0.25, 0.5, 0.75, 0.999_999] {
                let delay = backoff.delay(forAttempt: attempt, random: random)
                XCTAssertLessThanOrEqual(delay, full)
                XCTAssertGreaterThanOrEqual(GroundBackoff.seconds(delay), GroundBackoff.seconds(full) * 0.8 - 0.001)
                XCTAssertLessThanOrEqual(delay, .seconds(10))
            }
        }
        XCTAssertEqual(backoff.delay(forAttempt: 3, random: 0.5), .milliseconds(3600))
        XCTAssertEqual(backoff.delay(forAttempt: Int.max, random: 0), .seconds(10))
        XCTAssertEqual(backoff.delay(forAttempt: 0, random: 0), .seconds(1))
        XCTAssertEqual(GroundBackoff(jitterFraction: 5).jitterFraction, 1)
    }

    func testTheClientUsesTheInjectedRandomForJitter() async throws {
        let harness = try Harness(random: { 0.5 })
        harness.network.failConnect = { _ in true }
        await harness.client.connect()
        try await eventually("jittered delay") {
            if case let .reconnecting(1, delay, _) = await harness.client.state { return delay == .milliseconds(900) }
            return false
        }
        await harness.client.shutdown()
    }

    func testEveryConnectAndReconnectSignalsTheAppAndBackoffResetsAfterSuccess() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let first = try await harness.handshake(0, sid: "a")
        harness.clock.advance(by: .seconds(25))
        first.serverDrop()
        try await eventually("reconnecting after drop") {
            if case let .reconnecting(1, d, .transportError) = await harness.client.state { return d == .seconds(1) }
            return false
        }
        harness.clock.advance(by: .seconds(1))
        let second = try await harness.handshake(1, sid: "b")
        try await eventually("second reconnected") { harness.recorder.reconnects.count == 2 }
        XCTAssertEqual(harness.recorder.reconnects.map(\.sid), ["a", "b"])
        XCTAssertEqual(harness.recorder.reconnects.map(\.connectCount), [1, 2])
        XCTAssertTrue(harness.recorder.reconnects[1].isReconnect)
        // The new session authenticates again.
        XCTAssertEqual(second.sent.first, #"40{"api_key":"test-key"}"#)
        // The client itself requests nothing: the app does, after .reconnected.
        XCTAssertFalse(second.sent.contains { $0.hasPrefix("42") })

        second.push("2")
        harness.clock.advance(by: .seconds(25))
        second.push("41")
        try await eventually("server disconnect") {
            await harness.client.state == .reconnecting(attempt: 1, retryIn: .seconds(1), reason: .serverDisconnected)
        }
        harness.clock.advance(by: .seconds(1))
        let third = try await harness.handshake(2)
        harness.clock.advance(by: .seconds(25))
        third.push("1")
        try await eventually("server close") {
            await harness.client.state == .reconnecting(attempt: 1, retryIn: .seconds(1), reason: .serverClosed)
        }
        await harness.client.shutdown()
    }

    func testTheStateRequestEventsAreTheFiveTheContractNamesAndAllAllowed() throws {
        XCTAssertEqual(
            GroundClient.stateRequestEvents,
            ["get_fleet_status", "get_detection_profiles", "get_ceiling_settings", "get_reasoning_provider", "get_manual_status"]
        )
        let cases = try Fixtures.cases("client_emits.json")
        for name in GroundClient.stateRequestEvents {
            XCTAssertEqual(cases[name]?["allowed"] as? Bool, true, name)
            XCTAssertTrue(GroundEmitPolicy.contract.allows(name))
        }
    }

    // MARK: - connect_error

    func testARefusedKeyIsAFailedStateThatWaitsForTheUser() async throws {
        let harness = try Harness(key: "wrong-key")
        await harness.client.connect()
        let transport = try await harness.transport(0)
        transport.push(Harness.openFrame)
        try await eventually("connect frame") { transport.sent.count == 1 }
        transport.push(try Fixtures.frame("socketio_connect_error"))
        let failed = GroundConnectionState.failed(.authenticationRejected(serverMessage: "Connection rejected by server", retryIn: nil))
        try await eventually("failed state") { await harness.client.state == failed }
        XCTAssertTrue(transport.closed)

        // No reconnect loop, however long we wait.
        harness.clock.advance(by: .seconds(3600 * 48))
        await settle()
        XCTAssertEqual(harness.network.transports.count, 1)
        let observed = await harness.client.state
        XCTAssertEqual(observed, failed)
        XCTAssertFalse(harness.recorder.states.contains { if case .reconnecting = $0 { return true } else { return false } })

        // A new key from the user tries again at once, with the new key.
        await harness.client.setAPIKey(GroundAPIKey("test-key"))
        let second = try await harness.handshake(1)
        XCTAssertEqual(second.sent.first, #"40{"api_key":"test-key"}"#)
        await harness.client.shutdown()
    }

    func testRetryNowEndsTheAuthWait() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.transport(0)
        transport.push(Harness.openFrame)
        transport.push(#"44{"message":"Connection rejected by server"}"#)
        try await eventually("failed") { if case .failed = await harness.client.state { return true } else { return false } }
        await harness.client.retryNow()
        _ = try await harness.transport(1)
        await harness.client.shutdown()
    }

    func testAnOptionalSlowRetryAfterARefusedKey() async throws {
        let harness = try Harness(authFailureRetryDelay: .seconds(60))
        await harness.client.connect()
        let transport = try await harness.transport(0)
        transport.push(Harness.openFrame)
        transport.push(try Fixtures.frame("socketio_connect_error"))
        try await eventually("failed with retry") {
            await harness.client.state == .failed(.authenticationRejected(serverMessage: "Connection rejected by server", retryIn: .seconds(60)))
        }
        harness.clock.advance(by: .seconds(59))
        await settle()
        XCTAssertEqual(harness.network.transports.count, 1)
        harness.clock.advance(by: .seconds(1))
        _ = try await harness.transport(1)
        await harness.client.shutdown()
    }

    func testNetworkLossIsNeverReportedAsAKeyProblem() async throws {
        let harness = try Harness()
        harness.network.failConnect = { $0 < 3 }
        await harness.client.connect()
        for attempt in 1...3 {
            try await eventually("reconnecting \(attempt)") {
                if case let .reconnecting(a, _, .transportError) = await harness.client.state { return a == attempt }
                return false
            }
            harness.clock.advance(by: .seconds(10))
        }
        try await harness.handshake(3)
        XCTAssertFalse(harness.recorder.states.contains { if case .failed = $0 { return true } else { return false } })
        await harness.client.shutdown()
    }

    // MARK: - Emit allow-list (safety)

    func testTheDefaultAllowListIsExactlyTheContractsTenEvents() throws {
        let cases = try Fixtures.cases("client_emits.json")
        let allowed = Set(cases.values.filter { $0["allowed"] as? Bool == true }.compactMap { $0["event"] as? String })
        let forbidden = Set(cases.values.filter { $0["allowed"] as? Bool == false }.compactMap { $0["event"] as? String })
        XCTAssertEqual(allowed.count, 10)
        XCTAssertEqual(GroundEmitPolicy.contractEvents, allowed)
        for name in forbidden {
            XCTAssertFalse(GroundEmitPolicy.contract.allows(name), name)
        }
        XCTAssertFalse(GroundEmitPolicy.contract.allows("match_alert_response"))
    }

    func testEveryAllowedFixtureEmitIsSentAndEveryForbiddenOneThrows() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        let codec = SocketIOCodec()
        for (name, fixture) in try Fixtures.cases("client_emits.json").sorted(by: { $0.key < $1.key }) {
            let event = try XCTUnwrap(fixture["event"] as? String)
            let payloadObject = fixture["payload"].flatMap { $0 is NSNull ? nil : $0 }
            let payload = try payloadObject.map { try JSONSerialization.data(withJSONObject: $0, options: [.sortedKeys]) }
            let before = transport.sent.count
            if fixture["allowed"] as? Bool == true {
                let result = try await harness.client.emit(event, payload: payload)
                XCTAssertEqual(result, .sent, name)
                XCTAssertEqual(transport.sent.count, before + 1, name)
                guard case let .message(packet) = try codec.decode(transport.sent.last!) else { return XCTFail(name) }
                let decoded = try codec.decodeEvent(packet)
                XCTAssertEqual(decoded.name, event, name)
                if let payloadObject {
                    let sentPayload = try JSONSerialization.jsonObject(with: try XCTUnwrap(decoded.arguments.first))
                    XCTAssertEqual(sentPayload as? NSObject, payloadObject as? NSObject, name)
                } else {
                    XCTAssertTrue(decoded.arguments.isEmpty, name)
                }
            } else {
                do {
                    _ = try await harness.client.emit(event, payload: payload)
                    XCTFail("\(event) was not refused")
                } catch let error as GroundEmitError {
                    XCTAssertEqual(error, .forbiddenEvent(event), name)
                }
                XCTAssertEqual(transport.sent.count, before, "\(name) must not reach the wire")
            }
        }
        await harness.client.shutdown()
    }

    func testNoEventOutsideTheAllowListEverReachesTheWire() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        var rng = SeededGenerator(seed: 42)
        let candidates = Array(GroundEmitPolicy.contractEvents) + [
            "plan_fleet", "start_fleet", "start_search", "stop_search", "manual_take_control", "manual_command",
            "set_ceiling_settings", "set_reasoning_provider", "set_stream_video", "set_target", "register_drone",
            "deregister_drone", "remove_all_drones", "restart_dashboard", "match_alert_response", "connect",
            "disconnect", "LAND_ALL", "land_all ", "get_fleet_status\u{0}", "", "x",
        ]
        var refused = 0
        for _ in 0..<500 {
            let name = candidates.randomElement(using: &rng)!
            do {
                _ = try await harness.client.emit(name)
            } catch {
                refused += 1
            }
        }
        XCTAssertGreaterThan(refused, 0)
        let codec = SocketIOCodec()
        for frame in transport.sent where frame.hasPrefix("42") {
            guard case let .message(packet) = try codec.decode(frame) else { return XCTFail(frame) }
            XCTAssertTrue(GroundEmitPolicy.contractEvents.contains(try codec.decodeEvent(packet).name), frame)
        }
        await harness.client.shutdown()
    }

    func testAnAppPolicyCanNarrowTheListButNeverTakeLandAllAway() async throws {
        let harness = try Harness(emitPolicy: GroundEmitPolicy.contract.restricted(by: { $0.hasPrefix("get_") }))
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        let sent = try await harness.client.emit("get_fleet_status")
        XCTAssertEqual(sent, .sent)
        do {
            _ = try await harness.client.emit("drone_command", value: ["command": "land", "drone_id": "drone-1"])
            XCTFail("drone_command was not refused")
        } catch {
            XCTAssertEqual(error as? GroundEmitError, .forbiddenEvent("drone_command"))
        }
        let landAll = try await harness.client.emit("land_all")
        XCTAssertEqual(landAll, .sent)
        XCTAssertEqual(transport.sent.last, #"42["land_all"]"#)
        XCTAssertTrue(GroundEmitPolicy(narrowing: { _ in false }).allows("land_all"))
        XCTAssertFalse(GroundEmitPolicy(narrowing: { _ in false }).allows("get_fleet_status"))
        await harness.client.shutdown()
    }

    func testEncodablePayloadsAreSentWithSortedKeys() async throws {
        struct Profile: Encodable, Sendable {
            let profile: String
            let drone_id: String
        }
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        let result = try await harness.client.emit("set_detection_profile", value: Profile(profile: "top_down", drone_id: "drone-1"))
        XCTAssertEqual(result, .sent)
        XCTAssertEqual(transport.sent.last, #"42["set_detection_profile",{"drone_id":"drone-1","profile":"top_down"}]"#)
        do {
            _ = try await harness.client.emit("drone_command", payload: Data("{".utf8))
            XCTFail("invalid JSON was sent")
        } catch {
            guard case .encodingFailed = error as? GroundEmitError else { return XCTFail("\(error)") }
        }
        await harness.client.shutdown()
    }

    // MARK: - LAND ALL

    func testLandAllIsSentWhenConnected() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        transport.push("2")
        try await eventually("pong") { transport.sent.contains("3") }
        let result = try await harness.client.emit("land_all")
        XCTAssertEqual(result, .sent)
        XCTAssertEqual(transport.sent.last, #"42["land_all"]"#)
        await harness.client.shutdown()
    }

    func testLandAllWhileNotConnectedIsReportedNotSentAndNeverQueued() async throws {
        let harness = try Harness()
        // Before connect() was ever called.
        let idleResult = try await harness.client.emit("land_all")
        XCTAssertEqual(idleResult, .notSent(.notConnected))

        await harness.client.connect()
        let first = try await harness.handshake(0)
        first.serverDrop()
        try await eventually("reconnecting") {
            if case .reconnecting = await harness.client.state { return true } else { return false }
        }
        let result = try await harness.client.emit("land_all")
        XCTAssertEqual(result, .notSent(.notConnected))
        XCTAssertEqual(GroundNotSentReason.notConnected.description, "not sent: not connected to the ground station")

        // LAND ALL while disconnected starts a reconnect without waiting for the backoff.
        let second = try await harness.handshake(1)
        XCTAssertEqual(harness.clock.now, .zero)
        await settle()
        // Nothing was queued: the new session carries only the handshake.
        XCTAssertFalse(second.sent.contains { $0.contains("land_all") })
        XCTAssertFalse(first.sent.contains { $0.contains("land_all") })
        await harness.client.shutdown()
    }

    func testOtherEmitsWhileDisconnectedAreNotSentEither() async throws {
        let harness = try Harness()
        await harness.client.connect()
        _ = try await harness.transport(0)
        let result = try await harness.client.emit("get_fleet_status")
        XCTAssertEqual(result, .notSent(.notConnected))
        await harness.client.shutdown()
        let afterShutdown = try await harness.client.emit("land_all")
        XCTAssertEqual(afterShutdown, .notSent(.shutDown))
    }

    // MARK: - Shutdown

    func testShutdownLeavesTheNamespaceClosesAndFinishesTheStream() async throws {
        let harness = try Harness()
        await harness.client.connect()
        let transport = try await harness.handshake(0)
        await harness.client.shutdown()
        XCTAssertEqual(transport.sent.last, "41")
        XCTAssertTrue(transport.closed)
        let observed = await harness.client.state
        XCTAssertEqual(observed, .stopped)
        try await eventually("stopped event") { harness.recorder.states.last == .stopped }
        // The stream is finished: a new iterator ends at once.
        var iterator = harness.client.events.makeAsyncIterator()
        let next = await iterator.next()
        XCTAssertNil(next)
        // A second shutdown and a connect after shutdown are harmless.
        await harness.client.shutdown()
        await harness.client.connect()
        await settle()
        XCTAssertEqual(harness.network.transports.count, 1)
    }

    func testShutdownDuringBackoffStopsReconnecting() async throws {
        let harness = try Harness()
        harness.network.failConnect = { _ in true }
        await harness.client.connect()
        try await eventually("reconnecting") {
            if case .reconnecting = await harness.client.state { return true } else { return false }
        }
        await harness.client.shutdown()
        harness.clock.advance(by: .seconds(60))
        await settle()
        XCTAssertEqual(harness.network.transports.count, 1)
        let observed = await harness.client.state
        XCTAssertEqual(observed, .stopped)
    }

    // MARK: - Health probe

    func testHealthProbeReportsVersionAndKeyStatus() async throws {
        let http = FakeHTTP()
        http.responses["/api/health"] = (200, Data(#"{"status":"ok","version":"1.4.0","x_future_field":1}"#.utf8))
        http.responses["/api/status"] = (200, Data(#"{"running":true,"source":"sim","x_future_field":1}"#.utf8))
        let harness = try Harness(http: http)
        let ok = await harness.client.healthProbe()
        XCTAssertEqual(ok, .ok(version: "1.4.0"))
        XCTAssertEqual(http.requests.map(\.url.absoluteString), ["http://127.0.0.1:6100/api/health", "http://127.0.0.1:6100/api/status"])
        XCTAssertEqual(http.requests.last?.headers["Authorization"], "Bearer test-key")

        http.responses["/api/status"] = (401, Data(#"{"error":"unauthorized"}"#.utf8))
        let wrongKey = await harness.client.healthProbe()
        XCTAssertEqual(wrongKey, .wrongKey(version: "1.4.0"))

        http.responses["/api/health"] = (500, Data())
        let broken = await harness.client.healthProbe()
        XCTAssertEqual(broken, .unexpectedStatus(500))

        http.error = URLError(.cannotConnectToHost)
        let unreachable = await harness.client.healthProbe()
        XCTAssertEqual(unreachable, .unreachable("URLError \(URLError.Code.cannotConnectToHost.rawValue)"))
        await harness.client.shutdown()
    }

    // MARK: - Endpoint

    func testEndpointBuildsTheContractURLAndRefusesBadInput() throws {
        XCTAssertEqual(try GroundEndpoint(host: "192.168.1.20").socketURL.absoluteString,
                       "ws://192.168.1.20:5555/socket.io/?EIO=4&transport=websocket")
        XCTAssertEqual(try GroundEndpoint.parse("ws://ground.local:6100").socketURL.absoluteString,
                       "ws://ground.local:6100/socket.io/?EIO=4&transport=websocket")
        XCTAssertEqual(try GroundEndpoint.parse("https://ground.local").socketURL.absoluteString,
                       "wss://ground.local:5555/socket.io/?EIO=4&transport=websocket")
        XCTAssertEqual(try GroundEndpoint.parse("10.0.0.5:7000").httpBaseURL.absoluteString, "http://10.0.0.5:7000")
        XCTAssertEqual(try GroundEndpoint(host: "[fe80::1]", port: 5555).socketURL.absoluteString,
                       "ws://[fe80::1]:5555/socket.io/?EIO=4&transport=websocket")
        for bad in ["", " ", "a b", "user@host", "host/path", "host?x=1", "a:b", "ex%61mple"] {
            XCTAssertThrowsError(try GroundEndpoint(host: bad), bad)
        }
        XCTAssertThrowsError(try GroundEndpoint(host: "h", port: 0))
        XCTAssertThrowsError(try GroundEndpoint(host: "h", port: 70000))
        for bad in ["ftp://h:1", "ws://user:pw@h:1", "ws://h:1/path", "ws://h:1/?api_key=x", "ws://h:1#f", "ws://"] {
            XCTAssertThrowsError(try GroundEndpoint.parse(bad), bad)
        }
    }
}
