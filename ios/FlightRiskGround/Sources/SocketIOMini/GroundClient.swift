import Foundation

/// A Socket.IO client for one FlightRisk dashboard, as `docs/mobile-contract.md` describes.
///
/// - `connect()` starts a connection loop that runs until `shutdown()`. The
///   loop keeps the client alive: dropping the last reference without calling
///   `shutdown()` leaves the WebSocket open and the loop running, so always
///   call `shutdown()` when the client is no longer needed.
/// - Every state change, every established session and every server event
///   except video frames arrives on `events`, in order; `frame` events (the
///   `lossyEventNames`) arrive on `frames`. Iterate each from one place only.
/// - Both streams are bounded (see `Configuration`). `events` never drops
///   silently: when it is full it drops incoming elements and later delivers
///   `.eventsDropped(count:)` at the gap, after which the app must re-read
///   `state` and request the full state again. `frames` keeps the newest frames.
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
        /// Server events delivered on `frames` instead of `events`.
        public var lossyEventNames: Set<String>
        /// The most elements, and payload bytes, `events` buffers for a slow consumer.
        public var eventBufferLimit: Int
        public var eventBufferBytes: Int
        /// The most frames, and payload bytes, `frames` buffers (oldest dropped first).
        public var frameBufferLimit: Int
        public var frameBufferBytes: Int
        /// The most events, and payload bytes, held between the namespace connect
        /// and its acknowledgement.
        public var pendingEventLimit: Int
        public var pendingEventBytes: Int

        public init(
            endpoint: GroundEndpoint,
            apiKey: GroundAPIKey,
            backoff: GroundBackoff = GroundBackoff(),
            handshakeTimeout: Duration = .seconds(10),
            authFailureRetryDelay: Duration? = nil,
            maxFrameBytes: Int = SocketIOCodec.defaultMaxFrameBytes,
            maxPingWindow: Duration = .seconds(120),
            lossyEventNames: Set<String> = ["frame"],
            eventBufferLimit: Int = 1024,
            eventBufferBytes: Int = 16 * 1024 * 1024,
            frameBufferLimit: Int = 8,
            frameBufferBytes: Int = 16 * 1024 * 1024,
            pendingEventLimit: Int = 64,
            pendingEventBytes: Int = 1024 * 1024
        ) {
            self.endpoint = endpoint
            self.apiKey = apiKey
            self.backoff = backoff
            self.handshakeTimeout = handshakeTimeout
            self.authFailureRetryDelay = authFailureRetryDelay
            self.maxFrameBytes = maxFrameBytes
            self.maxPingWindow = maxPingWindow
            self.lossyEventNames = lossyEventNames
            self.eventBufferLimit = eventBufferLimit
            self.eventBufferBytes = eventBufferBytes
            self.frameBufferLimit = frameBufferLimit
            self.frameBufferBytes = frameBufferBytes
            self.pendingEventLimit = pendingEventLimit
            self.pendingEventBytes = pendingEventBytes
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

    /// Everything the client reports except video frames, in order. Finishes after `shutdown()`.
    public nonisolated let events: GroundEventStream<GroundEvent>

    /// `frame` events (and any other `lossyEventNames`), newest kept when the
    /// consumer falls behind. Finishes after `shutdown()`.
    public nonisolated let frames: GroundEventStream<GroundServerEvent>

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
        var connectedAt: Duration?
        var hadConnected = false
        var dropReason: GroundDropReason?
        /// Events that arrived between the namespace connect and its acknowledgement.
        var pendingEvents: [GroundServerEvent] = []
        var pendingBytes = 0
    }

    private enum SessionOutcome {
        case stopped
        case rejected(String?)
        /// `stable` is true when the session stayed connected for at least one
        /// ping interval; only then does the backoff start again from the beginning.
        case dropped(GroundDropReason, stable: Bool)
    }

    private var configuration: Configuration
    private let eventBuffer: GroundEventBuffer<GroundEvent>
    private let frameBuffer: GroundEventBuffer<GroundServerEvent>
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

    public init(
        configuration: Configuration,
        transportFactory: @escaping GroundTransportFactory = NWGroundTransport.factory,
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
        eventBuffer = GroundEventBuffer(
            maxCount: configuration.eventBufferLimit,
            maxBytes: configuration.eventBufferBytes,
            overflow: .dropIncoming(marker: { GroundEvent.eventsDropped(count: $0) })
        )
        frameBuffer = GroundEventBuffer(
            maxCount: configuration.frameBufferLimit,
            maxBytes: configuration.frameBufferBytes,
            overflow: .dropOldest
        )
        events = eventBuffer.stream
        frames = frameBuffer.stream
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
        eventBuffer.finish()
        frameBuffer.finish()
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
            case let .dropped(reason, stable):
                // A link that drops right after connecting (a flapping server)
                // keeps backing off; only a link that stayed up resets the backoff.
                if stable { failures = 0 }
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
            let reason: GroundDropReason
            if let watchdogReason = session?.dropReason {
                reason = watchdogReason
            } else if case let .messageTooLarge(limit) = error as? GroundTransportError {
                reason = .frameTooLarge(limit: limit)
            } else {
                reason = .transportError(redact(Self.describe(error)))
            }
            return dropped(reason)
        }
    }

    private func dropped(_ reason: GroundDropReason) -> SessionOutcome {
        log(.warning, "connection lost: \(reason)")
        var stable = false
        if let current = session, let connectedAt = current.connectedAt {
            stable = clock.now - connectedAt >= max(current.pingInterval, .seconds(1))
        }
        return .dropped(reason, stable: stable)
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
                if case let .frameTooLarge(_, limit) = error as? SocketIOCodecError {
                    return dropped(.frameTooLarge(limit: limit))
                }
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
            session?.connectedAt = clock.now
            session?.pendingEvents = []
            session?.pendingBytes = 0
            startWatchdog(sessionID: current.id)
            publish(.connected(info))
            eventBuffer.push(.reconnected(info), bytes: 0)
            // python-socketio runs the connect handler before it acknowledges the
            // connect, so `status` arrives before `40{"sid":...}`. Deliver such
            // events now, after the session is announced, in their original order.
            for event in current.pendingEvents {
                deliver(event)
            }
            return nil
        case .connectError:
            // Redact first, then truncate: truncating first could cut the key in
            // half and leave its first characters unredacted.
            let message = codec.connectErrorMessage(packet).map { String(redact($0).prefix(Self.maxServerMessageLength)) }
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
                let size = Self.payloadBytes(event)
                if phase == .connected {
                    log(.debug, "received \(event.name)")
                    deliver(event)
                } else if let current = session,
                          current.pendingEvents.count < configuration.pendingEventLimit,
                          current.pendingBytes + size <= configuration.pendingEventBytes {
                    log(.debug, "received \(event.name) before the connect acknowledgement; holding it")
                    session?.pendingEvents.append(event)
                    session?.pendingBytes += size
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

    private func deliver(_ event: GroundServerEvent) {
        if configuration.lossyEventNames.contains(event.name) {
            frameBuffer.push(event, bytes: Self.payloadBytes(event))
        } else if !eventBuffer.push(.server(event), bytes: Self.payloadBytes(event)) {
            log(.warning, "dropped \(event.name): the event buffer is full")
        }
    }

    private static func payloadBytes(_ event: GroundServerEvent) -> Int {
        (event.payload?.count ?? 0) + event.extraArguments.reduce(0) { $0 + $1.count } + event.name.utf8.count
    }

    private func publish(_ newState: GroundConnectionState) {
        state = newState
        eventBuffer.push(.stateChanged(newState), bytes: 0)
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
        if let transportError = error as? GroundTransportError {
            return transportError.description
        }
        let nsError = error as NSError
        return "\(nsError.domain) \(nsError.code)"
    }
}
