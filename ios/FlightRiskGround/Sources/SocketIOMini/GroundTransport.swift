import Foundation
import os

// MARK: - Transport

/// One message received from the WebSocket.
public enum GroundTransportMessage: Sendable, Equatable {
    case text(String)
    case binary(Data)
}

/// The WebSocket under the client. Tests use a fake; the app uses
/// `URLSessionGroundTransport`. One transport serves one connection attempt.
public protocol GroundTransport: Sendable {
    /// Opens the WebSocket. Failures may also surface on the first `receive()`.
    func connect() async throws
    /// Sends one text frame.
    func send(_ text: String) async throws
    /// Waits for the next frame. Throws when the WebSocket closes or fails.
    func receive() async throws -> GroundTransportMessage
    /// Closes the WebSocket. Idempotent; a pending `receive()` then throws.
    func close() async
}

/// Makes one transport for one connection attempt: the URL and the frame cap.
public typealias GroundTransportFactory = @Sendable (_ url: URL, _ maxFrameBytes: Int) -> any GroundTransport

/// A WebSocket transport over `URLSessionWebSocketTask`.
///
/// It sends no `Origin` header (the dashboard's Engine.IO refuses a foreign
/// origin and checks none when the header is absent) and no credentials: the
/// API key travels only inside the Socket.IO connect frame.
public final class URLSessionGroundTransport: GroundTransport, @unchecked Sendable {
    private let session: URLSession
    private let task: URLSessionWebSocketTask
    private let lock = NSLock()
    private var closed = false

    public init(url: URL, maxFrameBytes: Int, openTimeout: TimeInterval = 10) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = openTimeout
        session = URLSession(configuration: configuration)
        var request = URLRequest(url: url)
        request.timeoutInterval = openTimeout
        // Deliberately no Origin header and no Authorization header.
        task = session.webSocketTask(with: request)
        task.maximumMessageSize = maxFrameBytes
    }

    /// The factory the app passes to `GroundClient`.
    public static let factory: GroundTransportFactory = { url, maxFrameBytes in
        URLSessionGroundTransport(url: url, maxFrameBytes: maxFrameBytes)
    }

    public func connect() async throws {
        task.resume()
    }

    public func send(_ text: String) async throws {
        try await task.send(.string(text))
    }

    public func receive() async throws -> GroundTransportMessage {
        switch try await task.receive() {
        case let .string(text):
            return .text(text)
        case let .data(data):
            return .binary(data)
        @unknown default:
            throw URLError(.cannotParseResponse)
        }
    }

    public func close() async {
        let shouldClose: Bool = lock.withLock {
            if closed { return false }
            closed = true
            return true
        }
        guard shouldClose else { return }
        task.cancel(with: .normalClosure, reason: nil)
        session.invalidateAndCancel()
    }
}

// MARK: - Clock

/// A monotonic clock the client measures timeouts and backoff with, so tests
/// can drive time by hand.
public protocol GroundClock: Sendable {
    /// Time since an arbitrary fixed origin.
    var now: Duration { get }
    /// Sleeps until `now` reaches `deadline`; throws `CancellationError` when cancelled.
    func sleep(until deadline: Duration) async throws
}

/// The real clock (`ContinuousClock`, which keeps counting while the device sleeps).
public struct SystemGroundClock: GroundClock {
    private let origin: ContinuousClock.Instant

    public init() {
        origin = ContinuousClock.now
    }

    public var now: Duration { ContinuousClock.now - origin }

    public func sleep(until deadline: Duration) async throws {
        try await Task.sleep(until: origin + deadline, clock: .continuous)
    }
}

// MARK: - HTTP

/// The HTTP GET the health probe needs. Tests use a fake.
public protocol GroundHTTPClient: Sendable {
    func get(_ url: URL, headers: [String: String], timeout: TimeInterval) async throws -> (status: Int, body: Data)
}

/// `GroundHTTPClient` over an ephemeral `URLSession`.
public struct URLSessionGroundHTTPClient: GroundHTTPClient {
    public init() {}

    public func get(_ url: URL, headers: [String: String], timeout: TimeInterval) async throws -> (status: Int, body: Data) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        let session = URLSession(configuration: configuration)
        defer { session.finishTasksAndInvalidate() }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        for (name, value) in headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        return (http.statusCode, data)
    }
}

// MARK: - Default log sink

public enum GroundLogging {
    private static let logger = Logger(subsystem: "app.flightrisk.ground", category: "SocketIOMini")

    /// Writes to the unified log. The client has already removed the key.
    public static let unifiedLog: GroundLogSink = { level, message in
        switch level {
        case .debug: logger.debug("\(message, privacy: .public)")
        case .info: logger.info("\(message, privacy: .public)")
        case .warning: logger.warning("\(message, privacy: .public)")
        case .error: logger.error("\(message, privacy: .public)")
        }
    }
}
