import Foundation
import Network
import Security
import os

// MARK: - Transport

/// One message received from the WebSocket.
public enum GroundTransportMessage: Sendable, Equatable {
    case text(String)
    case binary(Data)
}

/// The WebSocket under the client. Tests use a fake; the app uses
/// `NWGroundTransport`. One transport serves one connection attempt.
public protocol GroundTransport: Sendable {
    /// Opens the WebSocket. Failures may also surface on the first `receive()`.
    func connect() async throws
    /// Sends one text frame.
    func send(_ text: String) async throws
    /// Waits for the next frame. Throws when the WebSocket closes or fails.
    /// A frame larger than the transport's cap throws
    /// `GroundTransportError.messageTooLarge` before it is buffered whole.
    func receive() async throws -> GroundTransportMessage
    /// Closes the WebSocket. Idempotent; a pending `receive()` then throws.
    func close() async
}

/// Makes one transport for one connection attempt: the URL and the frame cap.
public typealias GroundTransportFactory = @Sendable (_ url: URL, _ maxFrameBytes: Int) -> any GroundTransport

/// Typed transport failures.
public enum GroundTransportError: Error, Equatable, Sendable, CustomStringConvertible {
    /// A received WebSocket message grew past the cap; the connection was closed
    /// as soon as the cap was crossed.
    case messageTooLarge(limit: Int)
    /// A text message was not valid UTF-8.
    case invalidText
    /// The WebSocket was closed (by either side), with the close code when known.
    case closed(code: Int?)
    /// The connection could not be opened or failed.
    case failed(String)

    public var description: String {
        switch self {
        case let .messageTooLarge(limit): return "a WebSocket message exceeded the limit of \(limit) bytes"
        case .invalidText: return "a WebSocket text message was not valid UTF-8"
        case let .closed(code): return "the WebSocket closed" + (code.map { " (code \($0))" } ?? "")
        case let .failed(detail): return "the WebSocket failed: \(detail)"
        }
    }
}

/// A WebSocket transport over Network.framework (`NWConnection` with
/// `NWProtocolWebSocket`).
///
/// Memory, two independent bounds (both tested over a loopback socket with a
/// 40 MB frame and a 1 MiB cap):
/// 1. `NWProtocolWebSocket.Options.maximumMessageSize` is set to `maxFrameBytes`.
///    Network.framework checks the length in the frame header and fails the
///    receive with `EMSGSIZE` before reading the payload (verified on macOS 27,
///    not yet on an iOS device).
/// 2. Independently, a message is read in chunks of at most 64 KiB, and the
///    connection is reset as soon as the bytes of one message pass the cap,
///    so at most about `maxFrameBytes + 64 KiB` of one message is ever held.
/// Either way the receive throws `GroundTransportError.messageTooLarge`.
/// Network.framework does not offer the permessage-deflate extension, so a
/// server cannot send compressed frames that expand after these checks.
///
/// Address policy: `connect()` resolves the host with `getaddrinfo` on a
/// background queue, refuses cleartext (`ws://`) unless every resolved address
/// is a local network address (`LanAddressPolicy`), and then connects to the
/// checked IP address, not to the name. A `wss://` connection is encrypted and
/// not subject to the rule; this app does not pin certificates.
///
/// Headers: the upgrade request carries no `Origin` (the dashboard's
/// Engine.IO refuses a foreign origin and checks none when the header is
/// absent) and no credentials; the API key travels only inside the Socket.IO
/// connect frame.
public final class NWGroundTransport: GroundTransport, @unchecked Sendable {
    /// The largest chunk read from the connection at a time.
    public static let chunkBytes = 64 * 1024

    private let queue = DispatchQueue(label: "app.flightrisk.ground.websocket")
    private let maxFrameBytes: Int
    private let url: URL
    private let openTimeout: TimeInterval
    private let networkMessageLimit: Bool
    private let resolver: GroundHostResolver
    private let lock = NSLock()
    private var storedConnection: NWConnection?
    private var isClosed = false

    public convenience init(url: URL, maxFrameBytes: Int, openTimeout: TimeInterval = 10) {
        self.init(url: url, maxFrameBytes: maxFrameBytes, openTimeout: openTimeout, networkMessageLimit: true)
    }

    /// `networkMessageLimit: false` exists only so tests can prove the chunked
    /// guard on its own. `resolver` is replaced only by tests.
    init(
        url: URL,
        maxFrameBytes: Int,
        openTimeout: TimeInterval,
        networkMessageLimit: Bool,
        resolver: @escaping GroundHostResolver = GroundSystemResolver.resolve
    ) {
        self.url = url
        self.maxFrameBytes = maxFrameBytes
        self.openTimeout = openTimeout
        self.networkMessageLimit = networkMessageLimit
        self.resolver = resolver
    }

    /// The factory the app passes to `GroundClient` (the default).
    public static let factory: GroundTransportFactory = { url, maxFrameBytes in
        NWGroundTransport(url: url, maxFrameBytes: maxFrameBytes)
    }

    /// True once a connection object exists (tests assert none is made for a refused address).
    var hasConnection: Bool { connection != nil }

    private var connection: NWConnection? {
        lock.withLock { storedConnection }
    }

    /// Builds the connection for a target the policy already passed. The
    /// connection goes to the checked IP address, never to the name.
    private func makeConnection(_ target: GroundConnectTarget) throws -> NWConnection {
        let webSocket = NWProtocolWebSocket.Options()
        webSocket.autoReplyPing = true
        if networkMessageLimit {
            webSocket.maximumMessageSize = maxFrameBytes
        }
        // Deliberately no additional headers: no Origin, no Authorization.
        let tcp = NWProtocolTCP.Options()
        tcp.connectionTimeout = max(1, Int(openTimeout.rounded(.up)))
        let parameters: NWParameters
        if target.url.scheme?.lowercased() == "wss" {
            let tls = NWProtocolTLS.Options()
            if let name = target.tlsServerName {
                sec_protocol_options_set_tls_server_name(tls.securityProtocolOptions, name)
            }
            parameters = NWParameters(tls: tls, tcp: tcp)
        } else {
            parameters = NWParameters(tls: nil, tcp: tcp)
        }
        parameters.defaultProtocolStack.applicationProtocols.insert(webSocket, at: 0)
        let connection = NWConnection(to: .url(target.url), using: parameters)
        let accepted = lock.withLock { () -> Bool in
            if isClosed { return false }
            storedConnection = connection
            return true
        }
        guard accepted else {
            connection.cancel()
            throw GroundTransportError.closed(code: nil)
        }
        return connection
    }

    public func connect() async throws {
        // Resolve off the main thread, check every address, and only then open a
        // socket, to the checked IP address.
        let target = try await GroundConnectPlanner.planOffMainThread(url: url, resolver: resolver)
        final class Once: @unchecked Sendable { var done = false }
        let once = Once()
        let connection = try makeConnection(target)
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            // The handler runs on `queue`, a serial queue, so `once` needs no lock.
            connection.stateUpdateHandler = { state in
                guard !once.done else { return }
                switch state {
                case .ready:
                    once.done = true
                    continuation.resume()
                case let .failed(error), let .waiting(error):
                    once.done = true
                    connection.cancel()
                    continuation.resume(throwing: GroundTransportError.failed(Self.describe(error)))
                case .cancelled:
                    once.done = true
                    continuation.resume(throwing: GroundTransportError.closed(code: nil))
                default:
                    break
                }
            }
            connection.start(queue: queue)
        }
    }

    public func send(_ text: String) async throws {
        let metadata = NWProtocolWebSocket.Metadata(opcode: .text)
        let context = NWConnection.ContentContext(identifier: "text", metadata: [metadata])
        guard let connection = self.connection else { throw GroundTransportError.closed(code: nil) }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(content: Data(text.utf8), contentContext: context, isComplete: true, completion: .contentProcessed { error in
                if let error {
                    continuation.resume(throwing: GroundTransportError.failed(Self.describe(error)))
                } else {
                    continuation.resume()
                }
            })
        }
    }

    public func receive() async throws -> GroundTransportMessage {
        try await withCheckedThrowingContinuation { continuation in
            receiveChunk(accumulated: Data(), opcode: nil) { result in
                continuation.resume(with: result)
            }
        }
    }

    private func receiveChunk(
        accumulated: Data,
        opcode: NWProtocolWebSocket.Opcode?,
        completion: @escaping @Sendable (Result<GroundTransportMessage, Error>) -> Void
    ) {
        let limit = maxFrameBytes
        guard let connection else {
            completion(.failure(GroundTransportError.closed(code: nil)))
            return
        }
        connection.receive(minimumIncompleteLength: 1, maximumLength: Self.chunkBytes) { [weak self] content, context, isComplete, error in
            guard let self else {
                completion(.failure(GroundTransportError.closed(code: nil)))
                return
            }
            if let error {
                if case let .posix(code) = error, code == .EMSGSIZE {
                    self.connection?.forceCancel()
                    completion(.failure(GroundTransportError.messageTooLarge(limit: limit)))
                } else {
                    completion(.failure(GroundTransportError.failed(Self.describe(error))))
                }
                return
            }
            var data = accumulated
            if let content {
                if data.count + content.count > limit {
                    // Stop reading this message at once; never buffer it whole.
                    self.connection?.forceCancel()
                    completion(.failure(GroundTransportError.messageTooLarge(limit: limit)))
                    return
                }
                data.append(content)
            }
            let metadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition) as? NWProtocolWebSocket.Metadata
            let currentOpcode = metadata?.opcode ?? opcode
            guard isComplete else {
                self.receiveChunk(accumulated: data, opcode: currentOpcode, completion: completion)
                return
            }
            switch currentOpcode {
            case .text?:
                guard let text = String(data: data, encoding: .utf8) else {
                    self.connection?.forceCancel()
                    completion(.failure(GroundTransportError.invalidText))
                    return
                }
                completion(.success(.text(text)))
            case .binary?:
                completion(.success(.binary(data)))
            case .close?:
                completion(.failure(GroundTransportError.closed(code: Self.closeCode(metadata))))
            case nil where content == nil && (context?.isFinal ?? true):
                completion(.failure(GroundTransportError.closed(code: nil)))
            default:
                // Ping, pong and continuation bookkeeping: read the next message.
                self.receiveChunk(accumulated: Data(), opcode: nil, completion: completion)
            }
        }
    }

    public func close() async {
        let connection = lock.withLock { () -> NWConnection? in
            isClosed = true
            return storedConnection
        }
        connection?.cancel()
    }

    private static func closeCode(_ metadata: NWProtocolWebSocket.Metadata?) -> Int? {
        guard let metadata else { return nil }
        switch metadata.closeCode {
        case let .protocolCode(code): return Int(code.rawValue)
        case let .applicationCode(code): return Int(code)
        case let .privateCode(code): return Int(code)
        @unknown default: return nil
        }
    }

    static func describe(_ error: NWError) -> String {
        switch error {
        case let .posix(code): return "POSIX \(code.rawValue)"
        case let .dns(code): return "DNS \(code)"
        case let .tls(code): return "TLS \(code)"
        default: return "network error"
        }
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

/// Typed HTTP failures of `URLSessionGroundHTTPClient`.
public enum GroundHTTPError: Error, Equatable, Sendable {
    /// The response body was larger than the cap.
    case bodyTooLarge(limit: Int)
    /// The response was not HTTP.
    case notHTTP
}

/// `GroundHTTPClient` over an ephemeral `URLSession` that never follows a
/// redirect (a 3xx comes back as the response, so a redirect to another host
/// can never answer for the ground) and reads at most `maxBodyBytes` of a body.
public struct URLSessionGroundHTTPClient: GroundHTTPClient {
    public static let maxBodyBytes = 256 * 1024

    public init() {}

    private final class RefuseRedirects: NSObject, URLSessionTaskDelegate {
        func urlSession(
            _ session: URLSession,
            task: URLSessionTask,
            willPerformHTTPRedirection response: HTTPURLResponse,
            newRequest request: URLRequest,
            completionHandler: @escaping (URLRequest?) -> Void
        ) {
            completionHandler(nil)
        }
    }

    public func get(_ url: URL, headers: [String: String], timeout: TimeInterval) async throws -> (status: Int, body: Data) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        let session = URLSession(configuration: configuration, delegate: RefuseRedirects(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        for (name, value) in headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        let (bytes, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw GroundHTTPError.notHTTP
        }
        var body = Data()
        for try await byte in bytes {
            if body.count >= Self.maxBodyBytes {
                throw GroundHTTPError.bodyTooLarge(limit: Self.maxBodyBytes)
            }
            body.append(byte)
        }
        return (http.statusCode, body)
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
