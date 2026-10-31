import Foundation

// MARK: - API key

/// The dashboard API key (`FLIGHTRISK_DASHBOARD_API_KEY` on the phone, the
/// ground's `FLIGHTRISK_API_KEY`).
///
/// The value is reachable only inside this module. Its `description`,
/// `debugDescription` and mirror never show it, so an accidental string
/// interpolation, `print`, or `dump` cannot leak the key into a log.
public struct GroundAPIKey: Sendable, Equatable, CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    let secret: String

    public init(_ secret: String) {
        self.secret = secret
    }

    public var isEmpty: Bool { secret.isEmpty }

    public var description: String { "GroundAPIKey(<redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: [], displayStyle: .struct) }
}

// MARK: - Endpoint

/// Why a ground endpoint or client configuration is not usable.
public enum GroundConfigurationError: Error, Equatable, Sendable, CustomStringConvertible {
    case invalidHost(String)
    case invalidPort(Int)
    case invalidURL(String)
    /// A cleartext address that is not on the local network (the reason is a sentence).
    case cleartextNotAllowed(String)

    public var description: String {
        switch self {
        case let .invalidHost(host):
            if LanAddressPolicy.isLegacyIPv4Spelling(host) {
                return "\"\(host)\" is not written as four decimal numbers. Write the address as four decimal numbers like 192.168.1.20."
            }
            return "The host \"\(host)\" is not a valid host name or IP address."
        case let .invalidPort(port):
            return "The port \(port) is outside 1 to 65535."
        case let .invalidURL(text):
            return "\"\(text)\" is not a ground station address of the form ws://HOST:PORT."
        case let .cleartextNotAllowed(reason):
            return "This address is refused: \(reason)."
        }
    }
}

/// The address of a FlightRisk dashboard (default port 5555).
public struct GroundEndpoint: Sendable, Equatable, CustomStringConvertible {
    public let host: String
    public let port: Int
    /// True for `wss://` and `https://`.
    public let secure: Bool

    public static let defaultPort = 5555

    public init(host: String, port: Int = GroundEndpoint.defaultPort, secure: Bool = false) throws {
        var trimmed = host.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("["), trimmed.hasSuffix("]") {
            trimmed = String(trimmed.dropFirst().dropLast())
        }
        guard Self.isValidHost(trimmed) else {
            throw GroundConfigurationError.invalidHost(host)
        }
        guard (1...65535).contains(port) else {
            throw GroundConfigurationError.invalidPort(port)
        }
        self.host = trimmed
        self.port = port
        self.secure = secure
    }

    /// Parses `ws://HOST:PORT`, `wss://HOST:PORT`, `http://HOST:PORT`, `https://HOST:PORT`
    /// or a bare `HOST[:PORT]`. Any path, query, or user info is refused.
    public static func parse(_ text: String) throws -> GroundEndpoint {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let withScheme = trimmed.contains("://") ? trimmed : "ws://" + trimmed
        guard let components = URLComponents(string: withScheme),
              let scheme = components.scheme?.lowercased(),
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.path.isEmpty || components.path == "/",
              components.query == nil, components.fragment == nil
        else {
            throw GroundConfigurationError.invalidURL(text)
        }
        let secure: Bool
        switch scheme {
        case "ws", "http": secure = false
        case "wss", "https": secure = true
        default: throw GroundConfigurationError.invalidURL(text)
        }
        // Only plain dotted decimal IPv4 is accepted (the system reads leading zeros, hex and
        // short forms differently in different places).
        if LanAddressPolicy.isLegacyIPv4Spelling(host) {
            throw GroundConfigurationError.invalidHost(host)
        }
        // A numeric public address is refused at once; a name is checked when connecting.
        if let refusal = LanAddressPolicy.earlyRefusal(host: host, secure: secure) {
            throw GroundConfigurationError.cleartextNotAllowed(refusal.reason)
        }
        return try GroundEndpoint(host: host, port: components.port ?? defaultPort, secure: secure)
    }

    /// `ws://HOST:PORT/socket.io/?EIO=4&transport=websocket`. It never carries the key.
    public var socketURL: URL {
        // The host and port were validated, so the URL always parses.
        URL(string: "\(secure ? "wss" : "ws")://\(hostForURL):\(port)/socket.io/?EIO=4&transport=websocket")!
    }

    /// `http://HOST:PORT`, the base of the REST routes.
    public var httpBaseURL: URL {
        URL(string: "\(secure ? "https" : "http")://\(hostForURL):\(port)")!
    }

    public var description: String { "\(secure ? "wss" : "ws")://\(hostForURL):\(port)" }

    private var hostForURL: String {
        host.contains(":") ? "[\(host)]" : host
    }

    private static func isValidHost(_ host: String) -> Bool {
        guard !host.isEmpty, host.utf8.count <= 253 else { return false }
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:")
        guard host.unicodeScalars.allSatisfy({ allowed.contains($0) }) else { return false }
        // A host with a colon must be a well-formed IPv6 literal.
        if host.contains(":") {
            var address = in6_addr()
            return inet_pton(AF_INET6, host, &address) == 1
        }
        return true
    }
}

// MARK: - Backoff

/// Bounded exponential backoff with jitter: 1, 2, 4, 8, 10, 10, ... seconds by
/// default, each reduced by up to `jitterFraction` at random so that many
/// phones do not reconnect in step.
public struct GroundBackoff: Sendable, Equatable {
    public var initialDelay: Duration
    public var maximumDelay: Duration
    /// The share of each delay that is randomised away, from 0 (none) to 1.
    public var jitterFraction: Double

    public init(initialDelay: Duration = .seconds(1), maximumDelay: Duration = .seconds(10), jitterFraction: Double = 0.2) {
        self.initialDelay = initialDelay
        self.maximumDelay = maximumDelay
        self.jitterFraction = min(max(jitterFraction, 0), 1)
    }

    /// The delay before reconnect attempt `attempt` (1-based); `random` is in 0..<1.
    /// The longest delay `delay(forAttempt:random:)` ever returns, whatever the settings.
    public static let longestDelaySeconds: Double = 3600

    public func delay(forAttempt attempt: Int, random: Double) -> Duration {
        let exponent = min(max(attempt - 1, 0), 30)
        let initial = Self.clamped(Self.seconds(initialDelay))
        let maximum = Self.clamped(Self.seconds(maximumDelay))
        let capped = min(initial * pow(2, Double(exponent)), maximum)
        let unit = random.isFinite ? min(max(random, 0), 1) : 0
        let fraction = jitterFraction.isFinite ? min(max(jitterFraction, 0), 1) : 0
        let jittered = capped * (1 - fraction * unit)
        return .milliseconds(Int64((max(jittered, 0) * 1000).rounded()))
    }

    private static func clamped(_ seconds: Double) -> Double {
        guard seconds.isFinite else { return seconds > 0 ? longestDelaySeconds : 0 }
        return min(max(seconds, 0), longestDelaySeconds)
    }

    static func seconds(_ duration: Duration) -> Double {
        let parts = duration.components
        return Double(parts.seconds) + Double(parts.attoseconds) / 1e18
    }
}

// MARK: - Connection state

/// Why an established or attempted connection ended. All of these are
/// network-level problems and lead to a reconnect with backoff.
public enum GroundDropReason: Sendable, Equatable, CustomStringConvertible {
    /// The WebSocket failed or could not be opened (no route, refused, reset).
    case transportError(String)
    /// No server ping arrived within pingInterval + pingTimeout.
    case pingTimeout
    /// The server did not complete the Engine.IO or Socket.IO handshake in time.
    case handshakeTimeout
    /// The server closed the Engine.IO session.
    case serverClosed
    /// The server removed the client from the namespace (`41`).
    case serverDisconnected
    /// The server sent something the protocol does not allow at that point.
    case protocolError(String)
    /// The server sent a frame larger than the cap; the link was closed before
    /// the frame was buffered whole.
    case frameTooLarge(limit: Int)

    public var description: String {
        switch self {
        case let .transportError(detail): return "network error: \(detail)"
        case .pingTimeout: return "no ping from the ground station in time"
        case .handshakeTimeout: return "the ground station did not finish the handshake in time"
        case .serverClosed: return "the ground station closed the connection"
        case .serverDisconnected: return "the ground station ended the session"
        case let .protocolError(detail): return "protocol error: \(detail)"
        case let .frameTooLarge(limit): return "the ground station sent a frame larger than \(limit) bytes"
        }
    }
}

/// A state the client cannot leave without the user (or a slow retry).
public enum GroundFailure: Sendable, Equatable, CustomStringConvertible {
    /// The ground refused the namespace connect (`connect_error`): a wrong or
    /// missing API key. `retryIn` is nil when the client waits for the user.
    case authenticationRejected(serverMessage: String?, retryIn: Duration?)

    public var description: String {
        switch self {
        case let .authenticationRejected(message, retryIn):
            var text = "The ground station refused the API key"
            if let message { text += " (\(message))" }
            if let retryIn {
                text += "; retrying in \(GroundBackoff.seconds(retryIn)) s"
            } else {
                text += "; waiting for a new key or a manual reconnect"
            }
            return text + "."
        }
    }
}

/// Facts about one established Socket.IO session.
public struct GroundSessionInfo: Sendable, Equatable {
    /// The Socket.IO session id from the connect acknowledgement.
    public let sid: String
    /// 1 for the first successful connect of this client, then 2, 3, ...
    public let connectCount: Int
    public let pingInterval: Duration
    public let pingTimeout: Duration

    public var isReconnect: Bool { connectCount > 1 }

    public init(sid: String, connectCount: Int, pingInterval: Duration, pingTimeout: Duration) {
        self.sid = sid
        self.connectCount = connectCount
        self.pingInterval = pingInterval
        self.pingTimeout = pingTimeout
    }
}

/// The client's connection state, published on every change.
public enum GroundConnectionState: Sendable, Equatable, CustomStringConvertible {
    /// Created, `connect()` not called yet.
    case idle
    /// Opening the WebSocket and running the handshake. `attempt` counts from 1
    /// since the last successful connect.
    case connecting(attempt: Int)
    /// The namespace connect succeeded; events flow and emits are sent.
    case connected(GroundSessionInfo)
    /// The link dropped or an attempt failed; the next attempt starts after `retryIn`.
    case reconnecting(attempt: Int, retryIn: Duration, reason: GroundDropReason)
    /// The ground refused the key. Distinct from network loss.
    case failed(GroundFailure)
    /// `shutdown()` was called; the client is finished.
    case stopped

    public var isConnected: Bool {
        if case .connected = self { return true }
        return false
    }

    public var description: String {
        switch self {
        case .idle: return "idle"
        case let .connecting(attempt): return "connecting (attempt \(attempt))"
        case let .connected(info): return "connected (connect \(info.connectCount))"
        case let .reconnecting(attempt, retryIn, reason):
            return "reconnecting (attempt \(attempt), in \(GroundBackoff.seconds(retryIn)) s, after \(reason))"
        case let .failed(failure): return "failed: \(failure)"
        case .stopped: return "stopped"
        }
    }
}

// MARK: - Events

/// One event the server sent: its name and the raw JSON of its payload.
/// Decoding the payload into models belongs to the `GroundProtocol` target.
public struct GroundServerEvent: Sendable, Equatable {
    public let name: String
    /// The raw JSON of the first argument, or nil when the event has none.
    public let payload: Data?
    /// The raw JSON of any further arguments (the dashboard never sends them).
    public let extraArguments: [Data]

    public init(name: String, payload: Data?, extraArguments: [Data] = []) {
        self.name = name
        self.payload = payload
        self.extraArguments = extraArguments
    }
}

/// Everything the client reports, in the order it happened.
public enum GroundEvent: Sendable, Equatable {
    /// The connection state changed.
    case stateChanged(GroundConnectionState)
    /// A Socket.IO session was established, the first one or a later one. The
    /// app must now request the full state again (`GroundClient.stateRequestEvents`)
    /// and mark anything older as stale.
    case reconnected(GroundSessionInfo)
    /// A server event.
    case server(GroundServerEvent)
    /// The consumer fell behind and the bound of `events` dropped `count`
    /// elements (state changes and events) here. Re-read `GroundClient.state`
    /// and request the full state again.
    case eventsDropped(count: Int)
}

// MARK: - Emitting

/// What happened to one `emit`.
public enum GroundEmitResult: Sendable, Equatable {
    /// The frame was handed to the WebSocket. This is not proof of delivery;
    /// the server's answer event is.
    case sent
    /// The frame was not sent and was not queued.
    case notSent(GroundNotSentReason)
}

/// Why an allowed event was not sent. Nothing is ever queued for later.
public enum GroundNotSentReason: Sendable, Equatable, CustomStringConvertible {
    /// The client is not connected. For `land_all` this also starts a reconnect.
    case notConnected
    /// The WebSocket refused the frame.
    case sendFailed(String)
    /// The client was shut down.
    case shutDown

    public var description: String {
        switch self {
        case .notConnected: return "not sent: not connected to the ground station"
        case let .sendFailed(detail): return "not sent: \(detail)"
        case .shutDown: return "not sent: the client was shut down"
        }
    }
}

/// Errors `emit` throws. A refused event is always an error, never a silent drop.
public enum GroundEmitError: Error, Sendable, Equatable, CustomStringConvertible {
    /// The event is not on the emit allow-list.
    case forbiddenEvent(String)
    /// The event name or payload cannot be encoded.
    case encodingFailed(String)

    public var description: String {
        switch self {
        case let .forbiddenEvent(name): return "The event \"\(name)\" is not on the allow-list and was not sent."
        case let .encodingFailed(detail): return "The event could not be encoded: \(detail)"
        }
    }
}

/// Decides which events the app may emit.
///
/// A policy can only narrow the contract's allow-list, never widen it: an event
/// is allowed when it is one of `contractEvents` and the app's rule accepts it.
/// `land_all` is always allowed, whatever the rule says: no policy can take
/// LAND ALL away from the operator.
public struct GroundEmitPolicy: Sendable {
    private let predicate: @Sendable (String) -> Bool

    /// The ten events `client_emits.json` allows (cases with `allowed: true`).
    public static let contractEvents: Set<String> = [
        "get_fleet_status",
        "get_detection_profiles",
        "get_ceiling_settings",
        "get_reasoning_provider",
        "get_manual_status",
        "land_all",
        "drone_command",
        "set_detection_profile",
        "set_fleet_detection_profile",
        "manual_resume_search",
    ]

    /// The event that can never be refused by a policy.
    public static let landAll = "land_all"

    /// The default: exactly the contract's allow-list.
    public static let contract = GroundEmitPolicy(narrowing: { _ in true })

    /// A policy that narrows the contract's allow-list by `rule`. Events outside
    /// `contractEvents` stay forbidden whatever `rule` returns; `land_all` stays allowed.
    public init(narrowing rule: @escaping @Sendable (String) -> Bool) {
        self.predicate = { Self.contractEvents.contains($0) && rule($0) }
    }

    /// This policy narrowed by another rule: an event must pass both.
    public func restricted(by rule: @escaping @Sendable (String) -> Bool) -> GroundEmitPolicy {
        let current = predicate
        return GroundEmitPolicy(narrowing: { current($0) && rule($0) })
    }

    public func allows(_ event: String) -> Bool {
        event == Self.landAll || (Self.contractEvents.contains(event) && predicate(event))
    }
}

// MARK: - Logging

public enum GroundLogLevel: Int, Sendable, Comparable {
    case debug, info, warning, error

    public static func < (lhs: GroundLogLevel, rhs: GroundLogLevel) -> Bool { lhs.rawValue < rhs.rawValue }
}

/// Receives the client's log lines. Lines name the host and event names only;
/// the client also removes the API key from every line before it gets here.
public typealias GroundLogSink = @Sendable (GroundLogLevel, String) -> Void

/// Replaces every occurrence of the API key in a string.
struct GroundRedactor: Sendable {
    let key: GroundAPIKey

    static let placeholder = "<redacted>"

    func callAsFunction(_ text: String) -> String {
        guard !key.isEmpty else { return text }
        return text.replacingOccurrences(of: key.secret, with: Self.placeholder)
    }
}
