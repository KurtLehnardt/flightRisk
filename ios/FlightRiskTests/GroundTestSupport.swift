import Foundation
import GroundProtocol
import SocketIOMini
import XCTest
@testable import FlightRisk

// Test doubles for the Ground tab. The view model runs against the real `GroundClient` with a
// scripted WebSocket, so every test exercises the same emit and event paths as the app. The
// only key used anywhere is the fake "test-key".

enum GroundTestFrames {
    static let open = #"0{"sid":"eio-sid","upgrades":[],"pingTimeout":20000,"pingInterval":25000,"maxPayload":1000000}"#
    static let connectAck = #"40{"sid":"sio-sid"}"#
    static let connectError = #"44{"message":"Connection rejected by server"}"#

    static func event(_ name: String, _ json: String) -> String { "42[\"\(name)\",\(json)]" }
}

struct FakeTransportError: Error {}

/// A scripted WebSocket: the test plays the server with `push(_:)` and `serverDrop()` and reads
/// what the client sent from `sent`.
/// Its mirror is empty because `sent` holds the connect frame with the fake key, and a test
/// dumps the view model (and through it the client) to prove the key is not reachable there.
final class FakeGroundTransport: GroundTransport, @unchecked Sendable, CustomReflectable {
    var customMirror: Mirror { Mirror(self, children: []) }
    private let lock = NSLock()
    private var inbox: [GroundTransportMessage] = []
    private var waiter: CheckedContinuation<GroundTransportMessage, Error>?
    private var sentFrames: [String] = []
    private var isClosed = false
    private var sendsFail = false

    var sent: [String] { lock.withLock { sentFrames } }
    var closed: Bool { lock.withLock { isClosed } }

    /// The Socket.IO event names the client sent (`42["name",...]` frames), in order.
    var sentEventNames: [String] {
        sent.compactMap { frame in
            guard frame.hasPrefix("42"),
                  let data = frame.dropFirst(2).data(using: .utf8),
                  let array = try? JSONSerialization.jsonObject(with: data) as? [Any],
                  let name = array.first as? String
            else { return nil }
            return name
        }
    }

    func failSends() { lock.withLock { sendsFail = true } }

    func connect() async throws {}

    func send(_ text: String) async throws {
        try lock.withLock {
            if isClosed || sendsFail { throw FakeTransportError() }
            sentFrames.append(text)
        }
    }

    func receive() async throws -> GroundTransportMessage {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if !inbox.isEmpty {
                let message = inbox.removeFirst()
                lock.unlock()
                continuation.resume(returning: message)
            } else if isClosed {
                lock.unlock()
                continuation.resume(throwing: FakeTransportError())
            } else {
                waiter = continuation
                lock.unlock()
            }
        }
    }

    func close() async { finish() }

    func push(_ text: String) {
        lock.lock()
        if isClosed {
            lock.unlock()
            return
        }
        if let waiter {
            self.waiter = nil
            lock.unlock()
            waiter.resume(returning: .text(text))
        } else {
            inbox.append(.text(text))
            lock.unlock()
        }
    }

    func serverDrop() { finish() }

    private func finish() {
        lock.lock()
        isClosed = true
        let pending = waiter
        waiter = nil
        lock.unlock()
        pending?.resume(throwing: FakeTransportError())
    }
}

/// Collects every transport the clients create.
final class TransportRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var made: [FakeGroundTransport] = []

    var transports: [FakeGroundTransport] { lock.withLock { made } }
    var last: FakeGroundTransport? { lock.withLock { made.last } }

    var factory: GroundTransportFactory {
        { [self] _, _ in
            let transport = FakeGroundTransport()
            lock.withLock { made.append(transport) }
            return transport
        }
    }
}

/// An in-memory key store. Its mirror is empty so a `dump` of the view model cannot show it.
final class InMemoryGroundKeyStore: GroundKeyStore, @unchecked Sendable, CustomReflectable {
    private let lock = NSLock()
    private var stored: String?

    var hasKey: Bool { lock.withLock { stored?.isEmpty == false } }
    func loadKey() -> GroundAPIKey? { lock.withLock { stored.flatMap { $0.isEmpty ? nil : GroundAPIKey($0) } } }
    func saveKey(_ key: String) throws { lock.withLock { stored = key } }
    func deleteKey() { lock.withLock { stored = nil } }
    var customMirror: Mirror { Mirror(self, children: []) }
}

/// A clock the test moves by hand.
final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var current = Date(timeIntervalSince1970: 1_800_000_000)

    var now: Date { lock.withLock { current } }
    func advance(_ seconds: TimeInterval) { lock.withLock { current = current.addingTimeInterval(seconds) } }
}

/// Every log line of the client and the view model.
final class LogCapture: @unchecked Sendable {
    private let lock = NSLock()
    private var collected: [String] = []

    var lines: [String] { lock.withLock { collected } }
    var sink: GroundLogSink { { [self] _, line in lock.withLock { collected.append(line) } } }
}

/// A scripted REST client for the health probe. Records the headers it was given; its mirror
/// is empty because those headers carry the fake key and a test dumps the view model.
final class FakeGroundHTTP: GroundHTTPClient, @unchecked Sendable, CustomReflectable {
    var customMirror: Mirror { Mirror(self, children: []) }
    private let lock = NSLock()
    private var recordedHeaders: [[String: String]] = []
    let healthStatus: Int
    let statusStatus: Int

    init(healthStatus: Int = 200, statusStatus: Int = 200) {
        self.healthStatus = healthStatus
        self.statusStatus = statusStatus
    }

    var headers: [[String: String]] { lock.withLock { recordedHeaders } }

    func get(_ url: URL, headers: [String: String], timeout: TimeInterval) async throws -> (status: Int, body: Data) {
        lock.withLock { recordedHeaders.append(headers) }
        if url.path.hasSuffix("/api/health") {
            return (healthStatus, Data(#"{"status":"ok","version":"9.9.9"}"#.utf8))
        }
        if statusStatus == 401 { return (401, Data(#"{"error":"unauthorized"}"#.utf8)) }
        return (statusStatus, Data(#"{"running":true,"source":"edge"}"#.utf8))
    }
}

/// A view model wired to fakes, plus helpers to play the ground station.
@MainActor
final class GroundHarness {
    static let testKey = "test-key"
    static let address = "ws://127.0.0.1:5999"

    let recorder = TransportRecorder()
    let keyStore = InMemoryGroundKeyStore()
    let clock = TestClock()
    let logs = LogCapture()
    let http: FakeGroundHTTP
    let suiteName = "GroundViewModelTests.\(UUID().uuidString)"
    let defaults: UserDefaults
    private(set) var clients: [GroundClient] = []
    private(set) var viewModel: GroundViewModel!

    init(http: FakeGroundHTTP = FakeGroundHTTP()) {
        self.http = http
        defaults = UserDefaults(suiteName: suiteName)!
        viewModel = makeViewModel()
    }

    func makeViewModel() -> GroundViewModel {
        let recorder = recorder
        let logs = logs
        let clock = clock
        return GroundViewModel(
            keyStore: keyStore,
            defaults: defaults,
            http: http,
            clock: { clock.now },
            logSink: logs.sink,
            clientFactory: { [weak self] endpoint, key in
                let client = GroundClient(
                    configuration: GroundClient.Configuration(
                        endpoint: endpoint,
                        apiKey: key,
                        // Long waits, so a test decides when a reconnect happens.
                        backoff: GroundBackoff(initialDelay: .seconds(60), maximumDelay: .seconds(60), jitterFraction: 0)
                    ),
                    transportFactory: recorder.factory,
                    emitPolicy: GroundViewModel.emitPolicy,
                    logSink: logs.sink
                )
                self?.clients.append(client)
                return client
            }
        )
    }

    func tearDown() async {
        for client in clients { await client.shutdown() }
        defaults.removePersistentDomain(forName: suiteName)
    }

    /// Saves the address and the fake key (the link stays off).
    func configure() {
        XCTAssertNil(viewModel.saveAddress(Self.address))
        XCTAssertNil(viewModel.saveKey(Self.testKey))
    }

    /// Configures, switches the link on and completes the handshake on a new transport.
    @discardableResult
    func connect(file: StaticString = #filePath, line: UInt = #line) async -> FakeGroundTransport {
        configure()
        let before = recorder.transports.count
        viewModel.setEnabled(true)
        return await handshake(after: before, file: file, line: line)
    }

    /// Waits for transport number `after + 1` and completes its handshake.
    func handshake(after before: Int, file: StaticString = #filePath, line: UInt = #line) async -> FakeGroundTransport {
        await waitUntil("a new transport", file: file, line: line) { self.recorder.transports.count > before }
        let transport = recorder.transports[before]
        transport.push(GroundTestFrames.open)
        await waitUntil("the namespace connect", file: file, line: line) { transport.sent.contains { $0.hasPrefix("40") } }
        transport.push(GroundTestFrames.connectAck)
        await waitUntil("connected and the five state requests", file: file, line: line) {
            self.viewModel.linkStatus == .connected && transport.sentEventNames.filter { $0.hasPrefix("get_") }.count >= 5
        }
        return transport
    }

    func push(_ transport: FakeGroundTransport, _ name: String, _ json: String, until condition: @escaping @MainActor () -> Bool, file: StaticString = #filePath, line: UInt = #line) async {
        transport.push(GroundTestFrames.event(name, json))
        await waitUntil("\(name) applied", file: file, line: line, condition)
    }

    func waitUntil(_ what: String, timeout: TimeInterval = 5, file: StaticString = #filePath, line: UInt = #line, _ condition: @MainActor () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() > deadline {
                XCTFail("Timed out waiting for \(what)", file: file, line: line)
                return
            }
            try? await Task.sleep(for: .milliseconds(5))
        }
    }

    /// Lets queued main-actor work run, for checks that something did NOT happen.
    func settle() async {
        for _ in 0..<20 {
            try? await Task.sleep(for: .milliseconds(5))
        }
    }
}

enum GroundSamples {
    /// drone-1 connected and flying a lane; drone-2 DOWN, link down, held with the orphan countdown.
    static let fleetTwo = #"""
    {"drones":{"drone-1":{"drone_id":"drone-1","connected":true,"lost":false,"battery":87,"height_cm":100,"lane_index":1,"lanes_total":4,"drone_link_ok":true,"control_mode":"autonomous","is_flying":true,"x_future_field":1},"drone-2":{"drone_id":"drone-2","connected":false,"lost":false,"battery":50,"drone_link_ok":false,"drone_link_detail":"no telemetry from the drone for 3 s","control_mode":"held","orphan_land_in_s":120}},"net":"station","synced":true,"manual_controlled":null}
    """#

    /// drone-1 held, nobody controls it.
    static let heldNobody = #"""
    {"drones":{"drone-1":{"drone_id":"drone-1","connected":true,"lost":false,"control_mode":"held","drone_link_ok":true}},"manual_controlled":null}
    """#

    /// drone-1 held, the laptop controls drone-2.
    static let heldButControlled = #"""
    {"drones":{"drone-1":{"drone_id":"drone-1","connected":true,"control_mode":"held"},"drone-2":{"drone_id":"drone-2","connected":true,"control_mode":"manual"}},"manual_controlled":"drone-2"}
    """#

    static let manualHeld = #"{"controlled":null,"drones":{"drone-3":{"control_mode":"held","orphan_land_in_s":90}}}"#

    static let ackReachedAll = #"{"coordinator":2,"local":null,"reached_any":true,"transport":2}"#
    static let ackReachedNone = #"{"coordinator":null,"local":null,"reached_any":false,"transport":null}"#
    static let fleetEventLandAllFailed = #"{"detail":"failed: drone-2","event":"land_all"}"#
    static let fleetEventPlanRefused = #"{"detail":"drone-2: not connected.","event":"plan_refused"}"#
}
