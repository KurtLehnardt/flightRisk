import Foundation
import GroundProtocol
import XCTest
@testable import SocketIOMini

/// Demo commands never touch the network. Every command goes through `GroundCommandDispatch`
/// with the destination `GroundCommandDestination.choose` returns, as the Ground tab sends them.
/// The live link here is a transport that fails the test as soon as a command reaches it.
final class DemoNetworkIsolationTests: XCTestCase {
    /// Every command the phone can build.
    private let everyCommand: [GroundCommand] = [
        .landAll, .landDrone(droneId: DemoGround.spotterDroneId), .landDrone(droneId: DemoGround.cutDroneId),
        .resumeHeldDrone(droneId: DemoGround.spotterDroneId),
        .setDetectionProfile(droneId: DemoGround.spotterDroneId, profile: "top_down"), .setFleetDetectionProfile(profile: "level"),
    ] + GroundCommand.stateRequests

    private func makeClient(_ network: TripwireNetwork, http: any GroundHTTPClient) throws -> GroundClient {
        GroundClient(
            configuration: GroundClient.Configuration(endpoint: try GroundEndpoint(host: "127.0.0.1", port: 6200), apiKey: GroundAPIKey("test-key")),
            transportFactory: network.factory,
            clock: ManualClock(),
            random: { 0 },
            http: http,
            logSink: { _, _ in }
        )
    }

    /// The demo with a live client handed to `choose`, as a client that is closing could still
    /// be: the client is never connected and never used. A transport or HTTP request made at all
    /// fails the test.
    func testDemoCommandsOpenNoConnection() async throws {
        let network = TripwireNetwork(mode: .failOnAnyUse)
        let http = TripwireHTTP()
        let client = try makeClient(network, http: http)
        let demo = DemoGroundSession(ground: DemoGround(startedAt: Date(timeIntervalSince1970: 0)))
        _ = demo.advance(to: 6)
        for command in everyCommand {
            let destination = GroundCommandDestination.choose(demo: demo, elapsed: 6, live: client)
            XCTAssertTrue(destination.isDemo)
            let outcome = await GroundCommandDispatch.send(command, to: destination)
            XCTAssertEqual(outcome, .answeredByDemo, "\(command)")
        }
        XCTAssertEqual(demo.receivedCommands, everyCommand)
        let answers = demo.advance(to: 8).map(Self.name)
        for expected in ["land_all_ack", "manual_ack", "manual_result", "detection_profiles", "fleet_status",
                         "ceiling_settings", "reasoning_provider", "manual_status"] {
            XCTAssertTrue(answers.contains(expected), "no local \(expected) in \(answers)")
        }
        XCTAssertEqual(network.made, 0)
        XCTAssertEqual(http.requests, 0)
        await client.shutdown()
    }

    /// Demo wins over a live client that is connected right now: the commands are answered by the
    /// demo and not one reaches the live socket.
    func testDemoCommandsNeverReachAConnectedLiveClient() async throws {
        let network = TripwireNetwork(mode: .handshakeThenFailOnEvents)
        let client = try makeClient(network, http: TripwireHTTP())
        await client.connect()
        try await eventually("live client connected") { await client.state.isConnected }
        let demo = DemoGroundSession(ground: DemoGround(startedAt: Date(timeIntervalSince1970: 0)))
        for command in everyCommand {
            let destination = GroundCommandDestination.choose(demo: demo, elapsed: 1, live: client)
            XCTAssertTrue(destination.isDemo, "the demo must win over the live client")
            let outcome = await GroundCommandDispatch.send(command, to: destination)
            XCTAssertEqual(outcome, .answeredByDemo, "\(command)")
        }
        await settle()
        XCTAssertEqual(network.eventFramesSent, [], "a demo command reached the live socket")
        XCTAssertEqual(demo.receivedCommands, everyCommand)
        await client.shutdown()
    }

    /// The control: without a demo the same path does reach the transport, so the tripwire above
    /// would have caught a leak.
    func testWithoutADemoCommandsGoToTheLiveClient() async throws {
        let network = TripwireNetwork(mode: .handshakeThenRecordEvents)
        let client = try makeClient(network, http: TripwireHTTP())
        await client.connect()
        try await eventually("live client connected") { await client.state.isConnected }
        let destination = GroundCommandDestination.choose(demo: nil, elapsed: 0, live: client)
        XCTAssertFalse(destination.isDemo)
        let outcome = await GroundCommandDispatch.send(.landAll, to: destination)
        XCTAssertEqual(outcome, .sent)
        XCTAssertEqual(network.eventFramesSent, [#"42["land_all"]"#])
        await client.shutdown()
    }

    func testAnUnsendableCommandGoesNowhereNotEvenToTheDemo() async {
        let demo = DemoGroundSession()
        let outcome = await GroundCommandDispatch.send(.landDrone(droneId: "  "), to: .choose(demo: demo, elapsed: 0, live: nil))
        XCTAssertEqual(outcome, .notSendable)
        XCTAssertEqual(demo.receivedCommands, [])
        let none = await GroundCommandDispatch.send(.landAll, to: .choose(demo: nil, elapsed: 0, live: nil))
        XCTAssertEqual(none, .noDestination)
    }

    private static func name(_ e: GroundProtocol.GroundEvent) -> String {
        switch e {
        case .landAllAck: return "land_all_ack"
        case .manualAck: return "manual_ack"
        case .manualResult: return "manual_result"
        case .detectionProfiles: return "detection_profiles"
        case .fleetStatus: return "fleet_status"
        case .ceilingSettings: return "ceiling_settings"
        case .reasoningProvider: return "reasoning_provider"
        case .manualStatus: return "manual_status"
        default: return "other"
        }
    }
}

// MARK: - Tripwires

/// Hands out `TripwireTransport`s and counts them.
private final class TripwireNetwork: @unchecked Sendable {
    enum Mode {
        /// Making a transport at all fails the test.
        case failOnAnyUse
        /// The transport answers the Engine.IO and Socket.IO handshake; any event frame fails the test.
        case handshakeThenFailOnEvents
        /// As above, but event frames are only recorded (for the control test).
        case handshakeThenRecordEvents
    }

    let mode: Mode
    private let lock = NSLock()
    private var count = 0
    private var events: [String] = []

    init(mode: Mode) { self.mode = mode }

    var made: Int { lock.withLock { count } }
    var eventFramesSent: [String] { lock.withLock { events } }

    func recordEvent(_ frame: String) { lock.withLock { events.append(frame) } }

    var factory: GroundTransportFactory {
        { [self] url, _ in
            lock.withLock { count += 1 }
            if mode == .failOnAnyUse { XCTFail("the demo opened a network transport to \(url)") }
            return TripwireTransport(network: self)
        }
    }
}

/// A WebSocket that plays a ground's handshake and treats every event frame (`42...`) as a
/// command that reached the network.
private final class TripwireTransport: GroundTransport, @unchecked Sendable {
    private let network: TripwireNetwork
    private let lock = NSLock()
    private var inbox: [GroundTransportMessage] = []
    private var waiter: CheckedContinuation<GroundTransportMessage, Error>?
    private var closed = false

    init(network: TripwireNetwork) { self.network = network }

    func connect() async throws {
        if network.mode == .failOnAnyUse { XCTFail("the demo connected a network transport") }
        deliver(.text(#"0{"sid":"eio-1","upgrades":[],"pingTimeout":20000,"pingInterval":25000,"maxPayload":10485760}"#))
    }

    func send(_ text: String) async throws {
        if text.hasPrefix("40") {
            deliver(.text(#"40{"sid":"sio-1"}"#))
        } else if text.hasPrefix("42") {
            network.recordEvent(text)
            if network.mode != .handshakeThenRecordEvents { XCTFail("a command reached the network: \(text)") }
        }
    }

    func receive() async throws -> GroundTransportMessage {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if !inbox.isEmpty {
                let message = inbox.removeFirst()
                lock.unlock()
                continuation.resume(returning: message)
            } else if closed {
                lock.unlock()
                continuation.resume(throwing: FakeTransportError(reason: "closed"))
            } else {
                waiter = continuation
                lock.unlock()
            }
        }
    }

    func close() async {
        lock.lock()
        closed = true
        let pending = waiter
        waiter = nil
        lock.unlock()
        pending?.resume(throwing: FakeTransportError(reason: "closed"))
    }

    private func deliver(_ message: GroundTransportMessage) {
        lock.lock()
        if closed {
            lock.unlock()
            return
        }
        if let waiter {
            self.waiter = nil
            lock.unlock()
            waiter.resume(returning: message)
        } else {
            inbox.append(message)
            lock.unlock()
        }
    }
}

/// An HTTP client that fails the test when called.
private final class TripwireHTTP: GroundHTTPClient, @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    var requests: Int { lock.withLock { count } }

    func get(_ url: URL, headers: [String: String], timeout: TimeInterval) async throws -> (status: Int, body: Data) {
        lock.withLock { count += 1 }
        XCTFail("the demo made an HTTP request to \(url)")
        throw URLError(.notConnectedToInternet)
    }
}
