import Foundation
import XCTest
@testable import SocketIOMini

// MARK: - Fixtures

/// Loads the shared contract fixtures from `tests/fixtures/mobile_contract/`
/// at the repository root, found by walking up from this source file.
enum Fixtures {
    static let directory: URL? = {
        var url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        while url.path != "/" {
            let candidate = url.appendingPathComponent("tests/fixtures/mobile_contract", isDirectory: true)
            if FileManager.default.fileExists(atPath: candidate.appendingPathComponent("socketio_frames.json").path) {
                return candidate
            }
            url.deleteLastPathComponent()
        }
        return nil
    }()

    static func cases(_ file: String, testFile: StaticString = #filePath, line: UInt = #line) throws -> [String: [String: Any]] {
        guard let directory else {
            XCTFail(
                "The contract fixtures directory tests/fixtures/mobile_contract was not found above \(#filePath). "
                    + "Run swift test from a FlightRisk checkout, where the package lives in ios/FlightRiskGround.",
                file: testFile, line: line
            )
            throw XCTSkip("fixtures missing")
        }
        let data = try Data(contentsOf: directory.appendingPathComponent(file))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any], file: testFile, line: line)
        return try XCTUnwrap(root["cases"] as? [String: [String: Any]], file: testFile, line: line)
    }

    static func frame(_ name: String, file: StaticString = #filePath, line: UInt = #line) throws -> String {
        let all = try cases("socketio_frames.json", testFile: file, line: line)
        return try XCTUnwrap(all[name]?["frame"] as? String, "missing frame case \(name)", file: file, line: line)
    }
}

// MARK: - Manual clock

/// A clock that only moves when a test calls `advance(by:)`.
final class ManualClock: GroundClock, @unchecked Sendable {
    private struct Sleeper {
        let deadline: Duration
        let continuation: CheckedContinuation<Void, Error>
    }

    private let lock = NSLock()
    private var current: Duration = .zero
    private var sleepers: [Int: Sleeper] = [:]
    private var cancelled: Set<Int> = []
    private var nextID = 0

    var now: Duration { lock.withLock { current } }

    var sleeperCount: Int { lock.withLock { sleepers.count } }

    func sleep(until deadline: Duration) async throws {
        let id: Int = lock.withLock {
            nextID += 1
            return nextID
        }
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                lock.lock()
                if cancelled.remove(id) != nil {
                    lock.unlock()
                    continuation.resume(throwing: CancellationError())
                    return
                }
                if deadline <= current {
                    lock.unlock()
                    continuation.resume()
                    return
                }
                sleepers[id] = Sleeper(deadline: deadline, continuation: continuation)
                lock.unlock()
            }
        } onCancel: {
            lock.lock()
            if let sleeper = sleepers.removeValue(forKey: id) {
                lock.unlock()
                sleeper.continuation.resume(throwing: CancellationError())
            } else {
                cancelled.insert(id)
                lock.unlock()
            }
        }
    }

    func advance(by duration: Duration) {
        lock.lock()
        current += duration
        let due = sleepers.filter { $0.value.deadline <= current }
        for id in due.keys {
            sleepers.removeValue(forKey: id)
        }
        lock.unlock()
        for sleeper in due.values {
            sleeper.continuation.resume()
        }
    }
}

// MARK: - Fake transport

struct FakeTransportError: Error, Equatable {
    let reason: String
}

/// A scripted WebSocket. The test plays the server with `push(_:)` and
/// `serverDrop()`, and reads what the client sent from `sent`.
final class FakeTransport: GroundTransport, @unchecked Sendable {
    let url: URL
    let maxFrameBytes: Int
    private let lock = NSLock()
    private var inbox: [GroundTransportMessage] = []
    private var waiter: CheckedContinuation<GroundTransportMessage, Error>?
    private var sentFrames: [String] = []
    private var isClosed = false
    private var closeError: Error = FakeTransportError(reason: "closed")
    var connectError: Error?
    private var sendError: Error?

    /// Makes every later `send` fail, as a dead socket would.
    func failSends() {
        lock.withLock { sendError = FakeTransportError(reason: "send failed") }
    }

    init(url: URL, maxFrameBytes: Int) {
        self.url = url
        self.maxFrameBytes = maxFrameBytes
    }

    var sent: [String] { lock.withLock { sentFrames } }
    var closed: Bool { lock.withLock { isClosed } }

    func connect() async throws {
        if let connectError { throw connectError }
    }

    func send(_ text: String) async throws {
        try lock.withLock {
            if isClosed { throw FakeTransportError(reason: "send after close") }
            if let sendError { throw sendError }
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
                let error = closeError
                lock.unlock()
                continuation.resume(throwing: error)
            } else {
                waiter = continuation
                lock.unlock()
            }
        }
    }

    func close() async {
        finish(with: FakeTransportError(reason: "closed by client"))
    }

    /// The server sends one text frame.
    func push(_ text: String) {
        deliver(.text(text))
    }

    func pushBinary(_ data: Data) {
        deliver(.binary(data))
    }

    /// The network drops the connection.
    func serverDrop() {
        finish(with: FakeTransportError(reason: "connection reset"))
    }

    private func deliver(_ message: GroundTransportMessage) {
        lock.lock()
        if isClosed {
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

    private func finish(with error: Error) {
        lock.lock()
        if isClosed {
            lock.unlock()
            return
        }
        isClosed = true
        closeError = error
        let pending = waiter
        waiter = nil
        lock.unlock()
        pending?.resume(throwing: error)
    }
}

/// Hands out fake transports and remembers them in order.
final class FakeNetwork: @unchecked Sendable {
    private let lock = NSLock()
    private var made: [FakeTransport] = []
    /// When set, transports fail to connect while this returns true for their index (0-based).
    var failConnect: (@Sendable (Int) -> Bool)?

    var transports: [FakeTransport] { lock.withLock { made } }

    var factory: GroundTransportFactory {
        { [self] url, maxFrameBytes in
            let transport = FakeTransport(url: url, maxFrameBytes: maxFrameBytes)
            lock.withLock {
                if let failConnect, failConnect(made.count) {
                    transport.connectError = FakeTransportError(reason: "connection refused")
                }
                made.append(transport)
            }
            return transport
        }
    }
}

// MARK: - Event and log capture

final class EventRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [GroundEvent] = []
    private var task: Task<Void, Never>?

    init(_ stream: GroundEventStream<GroundEvent>) {
        task = Task { [weak self] in
            for await event in stream {
                self?.append(event)
            }
        }
    }

    private func append(_ event: GroundEvent) {
        lock.withLock { recorded.append(event) }
    }

    var events: [GroundEvent] { lock.withLock { recorded } }

    var states: [GroundConnectionState] {
        events.compactMap { if case let .stateChanged(state) = $0 { return state } else { return nil } }
    }

    var serverEvents: [GroundServerEvent] {
        events.compactMap { if case let .server(event) = $0 { return event } else { return nil } }
    }

    var reconnects: [GroundSessionInfo] {
        events.compactMap { if case let .reconnected(info) = $0 { return info } else { return nil } }
    }
}

final class FrameRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [GroundServerEvent] = []
    private var task: Task<Void, Never>?

    init(_ stream: GroundEventStream<GroundServerEvent>) {
        task = Task { [weak self] in
            for await frame in stream {
                self?.append(frame)
            }
        }
    }

    private func append(_ frame: GroundServerEvent) {
        lock.withLock { recorded.append(frame) }
    }

    var frames: [GroundServerEvent] { lock.withLock { recorded } }
}

final class LogRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [String] = []

    var sink: GroundLogSink {
        { [self] _, message in lock.withLock { recorded.append(message) } }
    }

    var lines: [String] { lock.withLock { recorded } }
}

// MARK: - Waiting

struct WaitTimeout: Error, CustomStringConvertible {
    let what: String
    var description: String { "timed out waiting for \(what)" }
}

/// Polls `condition` in real time until it holds or `timeout` passes.
func eventually(
    _ what: String,
    timeout: TimeInterval = 3,
    file: StaticString = #filePath,
    line: UInt = #line,
    _ condition: () async -> Bool
) async throws {
    let end = Date().addingTimeInterval(timeout)
    while Date() < end {
        if await condition() { return }
        try await Task.sleep(nanoseconds: 2_000_000)
    }
    if await condition() { return }
    XCTFail("timed out waiting for \(what)", file: file, line: line)
    throw WaitTimeout(what: what)
}

/// Lets the client run for a short real-time moment (for "nothing happens" checks).
func settle() async {
    try? await Task.sleep(nanoseconds: 50_000_000)
}

// MARK: - Client harness

/// A client wired to fakes, plus helpers that play the server.
struct Harness {
    static let openFrame = #"0{"sid":"eio-1","upgrades":[],"pingTimeout":20000,"pingInterval":25000,"maxPayload":10485760}"#

    let client: GroundClient
    let network: FakeNetwork
    let clock: ManualClock
    let recorder: EventRecorder!
    let logs: LogRecorder

    init(
        key: String = "test-key",
        backoff: GroundBackoff = GroundBackoff(),
        authFailureRetryDelay: Duration? = nil,
        emitPolicy: GroundEmitPolicy = .contract,
        random: @escaping @Sendable () -> Double = { 0 },
        http: any GroundHTTPClient = FakeHTTP(),
        recordEvents: Bool = true,
        adjust: (inout GroundClient.Configuration) -> Void = { _ in }
    ) throws {
        network = FakeNetwork()
        clock = ManualClock()
        logs = LogRecorder()
        var configuration = GroundClient.Configuration(
            endpoint: try GroundEndpoint(host: "127.0.0.1", port: 6100),
            apiKey: GroundAPIKey(key),
            backoff: backoff,
            authFailureRetryDelay: authFailureRetryDelay
        )
        adjust(&configuration)
        client = GroundClient(
            configuration: configuration,
            transportFactory: network.factory,
            emitPolicy: emitPolicy,
            clock: clock,
            random: random,
            http: http,
            logSink: logs.sink
        )
        recorder = recordEvents ? EventRecorder(client.events) : nil
    }

    func transport(_ index: Int, file: StaticString = #filePath, line: UInt = #line) async throws -> FakeTransport {
        try await eventually("transport \(index)", file: file, line: line) { network.transports.count > index }
        return network.transports[index]
    }

    /// Plays the server side of a successful handshake on transport `index`.
    @discardableResult
    func handshake(_ index: Int, sid: String = "sio-1", openFrame: String = Harness.openFrame, file: StaticString = #filePath, line: UInt = #line) async throws -> FakeTransport {
        let transport = try await transport(index, file: file, line: line)
        transport.push(openFrame)
        try await eventually("namespace connect frame", file: file, line: line) {
            transport.sent.contains { $0.hasPrefix("40") }
        }
        transport.push(#"40{"sid":"\#(sid)"}"#)
        try await eventually("connected state", file: file, line: line) { await client.state.isConnected }
        return transport
    }
}

/// A scripted HTTP client for the health probe.
final class FakeHTTP: GroundHTTPClient, @unchecked Sendable {
    struct Request: Equatable {
        let url: URL
        let headers: [String: String]
    }

    private let lock = NSLock()
    private var recorded: [Request] = []
    var responses: [String: (Int, Data)] = [:]
    var error: Error?

    var requests: [Request] { lock.withLock { recorded } }

    func get(_ url: URL, headers: [String: String], timeout: TimeInterval) async throws -> (status: Int, body: Data) {
        lock.withLock { recorded.append(Request(url: url, headers: headers)) }
        if let error { throw error }
        return responses[url.path] ?? (404, Data())
    }
}
