import Foundation

/// A Socket.IO client for one FlightRisk dashboard, as `docs/mobile-contract.md` describes.
///
/// - `connect()` starts a connection loop that runs until `shutdown()`.
/// - Every state change, every established session and every server event
///   arrives on `events`, in order. Iterate it from one place only.
/// - After every connect and every reconnect the client yields
///   `.reconnected`; the app must then emit `stateRequestEvents` and treat all
///   older state as stale. The client itself sends nothing but the handshake,
///   pongs, acks and what the app emits.
/// - `emit` refuses any event outside the emit policy with a thrown
///   `GroundEmitError.forbiddenEvent`, never sends while disconnected (it
///   returns `.notSent(.notConnected)` and queues nothing), and never retries.
/// - A refused key (`connect_error`) is a distinct `.failed` state with no
///   reconnect loop; network loss is `.reconnecting` with bounded backoff.
/// - The API key appears only in the Socket.IO connect frame and in the health
///   probe's `Authorization` header: never in a URL, a log line, a state, or
///   an error description.
public actor GroundClient {
    /// Settings for one client.
    public struct Configuration: Sendable {
        public var endpoint: GroundEndpoint
        public var apiKey: GroundAPIKey
        public var backoff: GroundBackoff
        /// How long the Engine.IO open and the namespace connect may each take.
        public var handshakeTimeout: Duration
        /// After a refused key, retry this slowly; nil (the default, as the
        /// contract asks) waits for `setAPIKey(_:)` or `retryNow()`.
        public var authFailureRetryDelay: Duration?
        /// The largest frame accepted or sent, in bytes.
        public var maxFrameBytes: Int
        /// Upper bound for the server's pingInterval + pingTimeout, so a hostile
        /// open packet cannot switch dead-link detection off.
        public var maxPingWindow: Duration

        public init(
            endpoint: GroundEndpoint,
            apiKey: GroundAPIKey,
            backoff: GroundBackoff = GroundBackoff(),
            handshakeTimeout: Duration = .seconds(10),
            authFailureRetryDelay: Duration? = nil,
            maxFrameBytes: Int = SocketIOCodec.defaultMaxFrameBytes,
            maxPingWindow: Duration = .seconds(120)
        ) {
            self.endpoint = endpoint
            self.apiKey = apiKey
            self.backoff = backoff
            self.handshakeTimeout = handshakeTimeout
            self.authFailureRetryDelay = authFailureRetryDelay
            self.maxFrameBytes = maxFrameBytes
            self.maxPingWindow = maxPingWindow
        }
    }

    /// The five requests that rebuild the full state, to emit after every `.reconnected`.
    public static let stateRequestEvents: [String] = [
        "get_fleet_status",
        "get_detection_profiles",
        "get_ceiling_settings",
        "get_reasoning_provider",
        "get_manual_status",
    ]

    /// Everything the client reports, in order. Finishes after `shutdown()`.
    public nonisolated let events: AsyncStream<GroundEvent>

    /// The current connection state.
    public private(set) var state: GroundConnectionState = .idle

    private enum Phase: Equatable {
        case awaitingOpen
        case awaitingConnectAck
        case connected
    }

    private struct Session {
        let id: Int
        let transport: any GroundTransport
        let startedAt: Duration
        var phase: Phase = .awaitingOpen
        var openedAt: Duration?
        var lastPingAt: Duration?
        var pingInterval: Duration = .zero
        var pingTimeout: Duration = .zero
        var pingWindow: Duration?
        var hadConnected = false
        var dropReason: GroundDropReason?
        /// Events that arrived between the namespace connect and its acknowledgement.
        var pendingEvents: [GroundServerEvent] = []
    }

    private enum SessionOutcome {
        case stopped
        case rejected(String?)
        case dropped(GroundDropReason, hadConnected: Bool)
    }

    private var configuration: Configuration
    private let continuation: AsyncStream<GroundEvent>.Continuation
    private let transportFactory: GroundTransportFactory
    private let emitPolicy: GroundEmitPolicy
    private let clock: any GroundClock
    private let random: @Sendable () -> Double
    private let http: any GroundHTTPClient
    private let logSink: GroundLogSink
    private let codec: SocketIOCodec

    private var knownKeys: [GroundAPIKey]
    private var runTask: Task<Void, Never>?
    private var pauseTask: Task<Void, Never>?
    private var watchdogTask: Task<Void, Never>?
    private var session: Session?
    private var sessionCounter = 0
    private var connectCount = 0
    private var stopping = false

    static let maxServerMessageLength = 200
    /// The most events kept from before the connect acknowledgement.
    static let maxPendingEvents = 64

    public init(
        configuration: Configuration,
        transportFactory: @escaping GroundTransportFactory = URLSessionGroundTransport.factory,
        emitPolicy: GroundEmitPolicy = .contract,
        clock: any GroundClock = SystemGroundClock(),
        random: @escaping @Sendable () -> Double = { Double.random(in: 0..<1) },
        http: any GroundHTTPClient = URLSessionGroundHTTPClient(),
        logSink: @escaping GroundLogSink = GroundLogging.unifiedLog
    ) {
        self.configuration = configuration
        self.transportFactory = transportFactory
        self.emitPolicy = emitPolicy
        self.clock = clock
        self.random = random
        self.http = http
        self.logSink = logSink
        self.codec = SocketIOCodec(maxFrameBytes: configuration.maxFrameBytes)
        self.knownKeys = [configuration.apiKey]
        (events, continuation) = AsyncStream.makeStream(of: GroundEvent.self, bufferingPolicy: .unbounded)
    }

    // MARK: - Public API

    /// Starts the connection loop. Calling it again while running does nothing.
    public func connect() {
        guard runTask == nil, !stopping else { return }
        log(.info, "starting the connection to \(configuration.endpoint)")
        runTask = Task { await self.runLoop() }
    }

    /// Ends a backoff or auth-failure wait at once and tries to connect.
    public func retryNow() {
        pauseTask?.cancel()
    }

    /// Replaces the API key. The next connect uses it; a client waiting after a
    /// refused key tries again at once.
    public func setAPIKey(_ key: GroundAPIKey) {
        configuration.apiKey = key
        if !knownKeys.contains(key) {
            knownKeys.append(key)
        }
        if case .failed = state {
            pauseTask?.cancel()
        }
    }

    /// Emits an event with an optional raw JSON payload (one complete JSON value).
    ///
    /// - Throws: `GroundEmitError.forbiddenEvent` for an event outside the emit
    ///   policy, `GroundEmitError.encodingFailed` for a bad name or payload.
    /// - Returns: `.sent`, or `.notSent` with the reason. Nothing is queued:
    ///   an event that is not sent now is never sent later. `land_all` while
    ///   disconnected also cuts any backoff wait short.
    public func emit(_ event: String, payload: Data? = nil) async throws -> GroundEmitResult {
        guard emitPolicy.allows(event) else {
            log(.warning, "refused to emit \(event): not on the allow-list")
            throw GroundEmitError.forbiddenEvent(redact(event))
        }
        let frame: String
        do {
            frame = try codec.encodeEvent(name: event, payload: payload)
        } catch {
            throw GroundEmitError.encodingFailed(redact(String(describing: error)))
        }
        if stopping {
            log(.warning, "\(event) not sent: the client is shut down")
            return .notSent(.shutDown)
        }
        guard let current = session, current.phase == .connected else {
            if event == GroundEmitPolicy.landAll {
                log(.error, "land_all NOT sent: not connected; reconnecting now")
                pauseTask?.cancel()
            } else {
                log(.warning, "\(event) not sent: not connected")
            }
            return .notSent(.notConnected)
        }
        do {
            try await current.transport.send(frame)
        } catch {
            let detail = redact(Self.describe(error))
            log(.error, "\(event) not sent: \(detail)")
            return .notSent(.sendFailed(detail))
        }
        log(.info, "emitted \(event)")
        return .sent
    }

    /// Emits an event with an `Encodable` payload, encoded as JSON with sorted keys.
    /// (A distinct label from `payload:`, so a `Data` value is never re-encoded.)
    public func emit<Value: Encodable & Sendable>(_ event: String, value: Value) async throws -> GroundEmitResult {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let data: Data
        do {
            data = try encoder.encode(value)
        } catch {
            throw GroundEmitError.encodingFailed(redact(String(describing: error)))
        }
        return try await emit(event, payload: data)
    }

    /// Checks reachability and the key over REST (`/api/health`, `/api/status`)
    /// without touching the socket.
    public func healthProbe() async -> GroundHealthResult {
        await GroundHealthProbe(endpoint: configuration.endpoint, apiKey: configuration.apiKey, http: http).run()
    }

    /// Leaves the namespace (`41`), closes the WebSocket, stops every timer,
    /// publishes `.stopped` and finishes `events`. The client cannot be restarted.
    public func shutdown() async {
        if stopping {
            await runTask?.value
            return
        }
        stopping = true
        log(.info, "shutting down")
        if let current = session {
            if current.phase == .connected, let leave = try? codec.encode(.message(SocketIOPacket(type: .disconnect))) {
                try? await current.transport.send(leave)
            }
            await current.transport.close()
        }
        pauseTask?.cancel()
        watchdogTask?.cancel()
        await runTask?.value
        runTask = nil
        publish(.stopped)
        continuation.finish()
    }

    // MARK: - Connection loop

    private func runLoop() async {
        var failures = 0
        while !stopping {
            publish(.connecting(attempt: failures + 1))
            let outcome = await runSession()
            if stopping { break }
            switch outcome {
            case .stopped:
                return
            case let .rejected(message):
                failures = 0
                let retry = configuration.authFailureRetryDelay
                publish(.failed(.authenticationRejected(serverMessage: message, retryIn: retry)))
                await pause(retry)
            case let .dropped(reason, hadConnected):
                if hadConnected { failures = 0 }
                failures += 1
                let delay = configuration.backoff.delay(forAttempt: failures, random: random())
                publish(.reconnecting(attempt: failures, retryIn: delay, reason: reason))
                await pause(delay)
            }
        }
    }

    /// Waits for `duration`, or until `retryNow()`, `setAPIKey(_:)`, a
    /// disconnected `land_all` or `shutdown()` cuts it short. Nil waits for one of those.
    private func pause(_ duration: Duration?) async {
        guard !stopping else { return }
        let clock = self.clock
        let deadline = duration.map { clock.now + $0 }
        let task = Task<Void, Never> {
            if let deadline {
                try? await clock.sleep(until: deadline)
            } else {
                while !Task.isCancelled {
                    try? await clock.sleep(until: clock.now + .seconds(86_400))
                }
            }
        }
        pauseTask = task
        await task.value
        pauseTask = nil
    }

    private func runSession() async -> SessionOutcome {
        sessionCounter += 1
        let id = sessionCounter
        let transport = transportFactory(configuration.endpoint.socketURL, configuration.maxFrameBytes)
        session = Session(id: id, transport: transport, startedAt: clock.now)
        startWatchdog(sessionID: id)
        let outcome = await driveSession(transport: transport)
        watchdogTask?.cancel()
        watchdogTask = nil
        session = nil
        await transport.close()
        return outcome
    }

    private func driveSession(transport: any GroundTransport) async -> SessionOutcome {
        do {
            try await transport.connect()
            while true {
                if stopping { return .stopped }
                let message = try await transport.receive()
                if stopping { return .stopped }
                if let outcome = try await handle(message, transport: transport) {
                    return outcome
                }
            }
        } catch {
            if stopping { return .stopped }
            let reason = session?.dropReason ?? .transportError(redact(Self.describe(error)))
            return dropped(reason)
        }
    }

    private func dropped(_ reason: GroundDropReason) -> SessionOutcome {
        log(.warning, "connection lost: \(reason)")
        return .dropped(reason, hadConnected: session?.hadConnected ?? false)
    }

    // MARK: - Frames

    private func handle(_ message: GroundTransportMessage, transport: any GroundTransport) async throws -> SessionOutcome? {
        guard let phase = session?.phase else { return .stopped }
        let packet: EngineIOPacket
        switch message {
        case let .binary(data):
            log(.debug, "ignored a binary frame of \(data.count) bytes")
            return nil
        case let .text(text):
            do {
                packet = try codec.decode(text)
            } catch {
                let detail = redact(String(describing: error))
                if phase == .awaitingOpen {
                    return dropped(.protocolError(detail))
                }
                log(.warning, "ignored a malformed frame: \(detail)")
                return nil
            }
        }

        switch packet {
        case let .open(open):
            guard phase == .awaitingOpen else {
                log(.warning, "ignored a second Engine.IO open packet")
                return nil
            }
            let now = clock.now
            let interval = Duration.milliseconds(open.pingIntervalMs)
            let timeout = Duration.milliseconds(open.pingTimeoutMs)
            session?.phase = .awaitingConnectAck
            session?.openedAt = now
            session?.lastPingAt = now
            session?.pingInterval = interval
            session?.pingTimeout = timeout
            session?.pingWindow = min(interval + timeout, configuration.maxPingWindow)
            // The deadline may now be earlier than the one the watchdog sleeps towards.
            if let id = session?.id { startWatchdog(sessionID: id) }
            try await transport.send(try codec.encodeConnect(apiKey: configuration.apiKey))
            log(.info, "Engine.IO open (pingInterval \(open.pingIntervalMs) ms, pingTimeout \(open.pingTimeoutMs) ms); sent the namespace connect")
            return nil
        case _ where phase == .awaitingOpen:
            return dropped(.protocolError("the first frame was not the Engine.IO open packet"))
        case let .ping(data):
            session?.lastPingAt = clock.now
            try await transport.send(try codec.encode(.pong(data)))
            return nil
        case .close:
            return dropped(.serverClosed)
        case .pong, .upgrade, .noop:
            return nil
        case let .message(socketPacket):
            return try await handleSocketIO(socketPacket, transport: transport)
        }
    }

    private func handleSocketIO(_ packet: SocketIOPacket, transport: any GroundTransport) async throws -> SessionOutcome? {
        guard packet.namespace == "/" else {
            log(.debug, "ignored a packet for another namespace")
            return nil
        }
        switch packet.type {
        case .connect:
            guard let current = session, current.phase == .awaitingConnectAck else {
                log(.warning, "ignored an unexpected namespace connect")
                return nil
            }
            connectCount += 1
            let info = GroundSessionInfo(
                sid: codec.connectSID(packet) ?? "",
                connectCount: connectCount,
                pingInterval: current.pingInterval,
                pingTimeout: current.pingTimeout
            )
            session?.phase = .connected
            session?.hadConnected = true
            session?.pendingEvents = []
            startWatchdog(sessionID: current.id)
            publish(.connected(info))
            continuation.yield(.reconnected(info))
            // python-socketio runs the connect handler before it acknowledges the
            // connect, so `status` arrives before `40{"sid":...}`. Deliver such
            // events now, after the session is announced, in their original order.
            for event in current.pendingEvents {
                continuation.yield(.server(event))
            }
            return nil
        case .connectError:
            let message = codec.connectErrorMessage(packet).map { redact(String($0.prefix(Self.maxServerMessageLength))) }
            log(.error, "the ground station refused the key")
            return .rejected(message)
        case .disconnect:
            return dropped(.serverDisconnected)
        case .event:
            guard let phase = session?.phase, phase != .awaitingOpen else { return nil }
            do {
                let decoded = try codec.decodeEvent(packet)
                let event = GroundServerEvent(
                    name: decoded.name,
                    payload: decoded.arguments.first,
                    extraArguments: Array(decoded.arguments.dropFirst())
                )
                if phase == .connected {
                    log(.debug, "received \(event.name)")
                    continuation.yield(.server(event))
                } else if (session?.pendingEvents.count ?? 0) < Self.maxPendingEvents {
                    log(.debug, "received \(event.name) before the connect acknowledgement; holding it")
                    session?.pendingEvents.append(event)
                } else {
                    log(.warning, "dropped \(event.name): too many events before the connect acknowledgement")
                }
            } catch {
                log(.warning, "ignored a malformed event: \(redact(String(describing: error)))")
            }
            if let id = packet.ackID {
                try await transport.send(try codec.encodeAck(id: id))
            }
            return nil
        case .ack, .binaryEvent, .binaryAck:
            log(.debug, "ignored a Socket.IO packet of type \(packet.type.rawValue)")
            return nil
        }
    }

    // MARK: - Dead-link detection

    private func startWatchdog(sessionID: Int) {
        watchdogTask?.cancel()
        watchdogTask = Task { [weak self] in
            await self?.watchdog(sessionID: sessionID)
        }
    }

    /// Closes the transport when the handshake takes too long or the server's
    /// pings stop for longer than pingInterval + pingTimeout.
    private func watchdog(sessionID: Int) async {
        while !Task.isCancelled {
            guard let current = session, current.id == sessionID else { return }
            let (deadline, reason) = Self.deadline(for: current, handshakeTimeout: configuration.handshakeTimeout)
            if clock.now >= deadline {
                session?.dropReason = reason
                log(.warning, "closing the link: \(reason)")
                await current.transport.close()
                return
            }
            do {
                try await clock.sleep(until: deadline)
            } catch {
                return
            }
        }
    }

    private static func deadline(for session: Session, handshakeTimeout: Duration) -> (Duration, GroundDropReason) {
        switch session.phase {
        case .awaitingOpen:
            return (session.startedAt + handshakeTimeout, .handshakeTimeout)
        case .awaitingConnectAck:
            let opened = session.openedAt ?? session.startedAt
            let handshake = opened + handshakeTimeout
            let ping = (session.lastPingAt ?? opened) + (session.pingWindow ?? handshakeTimeout)
            return handshake <= ping ? (handshake, .handshakeTimeout) : (ping, .pingTimeout)
        case .connected:
            let last = session.lastPingAt ?? session.openedAt ?? session.startedAt
            return (last + (session.pingWindow ?? handshakeTimeout), .pingTimeout)
        }
    }

    // MARK: - Helpers

    private func publish(_ newState: GroundConnectionState) {
        state = newState
        continuation.yield(.stateChanged(newState))
        log(.info, "state: \(newState)")
    }

    private func redact(_ text: String) -> String {
        knownKeys.reduce(text) { GroundRedactor(key: $1)($0) }
    }

    private func log(_ level: GroundLogLevel, _ message: String) {
        logSink(level, redact(message))
    }

    static func describe(_ error: any Error) -> String {
        if let urlError = error as? URLError {
            return "URLError \(urlError.code.rawValue)"
        }
        if let codecError = error as? SocketIOCodecError {
            return codecError.description
        }
        let nsError = error as NSError
        return "\(nsError.domain) \(nsError.code)"
    }
}
