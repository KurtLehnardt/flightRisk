import Foundation
import Network
import os.log

// MARK: - Thread-safe emergency connection holder

/// Sendable wrapper for the emergency NWConnection reference.
/// Uses os_unfair_lock for safe access from both actor-isolated and nonisolated contexts.
final class EmergencyConnectionRef: @unchecked Sendable {
    private var _connection: NWConnection?
    private var lock = os_unfair_lock()

    func get() -> NWConnection? {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        return _connection
    }

    func set(_ conn: NWConnection?) {
        os_unfair_lock_lock(&lock)
        _connection = conn
        os_unfair_lock_unlock(&lock)
    }
}

// MARK: - DroneConfig (inline until iOS config module lands)

/// Drone connection parameters matching Android DroneConfig / Python AmberConfig.
struct DroneConfig {
    var telloDefaultHost: String = "192.168.10.1"
    var telloCommandPort: UInt16 = 8889
    var telloVideoPort: UInt16 = 11111
    var telloStatePort: UInt16 = 8890
    var keepaliveIntervalSec: Int = 10
    var statePollingIntervalSec: Int = 2
    var commandTimeoutMs: Int = 7000
    var batteryWarnThreshold: Int = 20
    var batteryCriticalThreshold: Int = 10
}

// MARK: - TelloConnection

/// Core Tello UDP command protocol implementation.
///
/// Port of Android `TelloConnection.kt` to Swift. Uses `NWConnection` (Network.framework)
/// for UDP and Swift actor isolation to serialize commands (replacing Kotlin's `commandMutex`).
///
/// Safety-critical: `emergencyStop()` is `nonisolated` and bypasses actor isolation so it
/// can fire even when a long-running command holds the actor.
actor TelloConnection {

    // MARK: - Constants

    private static let logger = Logger(subsystem: "com.flightrisk", category: "TelloConnection")
    private static let rcMinIntervalMs: UInt64 = 50

    // MARK: - Configuration

    private let config: DroneConfig

    /// Tello IP from config.
    private let telloHost: NWEndpoint.Host

    /// Tello command port from config.
    private let telloPort: NWEndpoint.Port

    // MARK: - State

    /// Current observable state. Consumers observe via `stateStream`.
    private(set) var state = TelloState()

    /// Continuation-based stream for state updates.
    private var stateContinuation: AsyncStream<TelloState>.Continuation?

    /// Public async stream of state changes.
    nonisolated let stateStream: AsyncStream<TelloState>

    // MARK: - Networking

    /// UDP connection to the Tello command port (8889).
    private var connection: NWConnection?

    /// Direct reference kept for `emergencyStop` bypass.
    /// Protected by `emergencyLock` since it's accessed from both actor-isolated and nonisolated contexts.
    private let emergencyRef = EmergencyConnectionRef()

    private var emergencyConnection: NWConnection? {
        get { emergencyRef.get() }
        set { emergencyRef.set(newValue) }
    }

    // MARK: - Connection tracking

    private var isConnected = false

    // MARK: - Background tasks

    private var keepaliveTask: Task<Void, Never>?
    private var statePollingTask: Task<Void, Never>?

    // MARK: - RC rate limiting

    private var lastRcTime: UInt64 = 0

    // MARK: - Init

    init(config: DroneConfig = DroneConfig()) {
        self.config = config
        self.telloHost = NWEndpoint.Host(config.telloDefaultHost)
        self.telloPort = NWEndpoint.Port(rawValue: config.telloCommandPort)!

        var continuation: AsyncStream<TelloState>.Continuation?
        self.stateStream = AsyncStream { continuation = $0 }
        self.stateContinuation = continuation
    }

    deinit {
        stateContinuation?.finish()
    }

    // MARK: - Connect / Disconnect

    /// Connect to the Tello and enter SDK mode.
    ///
    /// Creates an NWConnection to `192.168.10.1:8889`, sends "command" to enter SDK mode,
    /// and starts keepalive + state polling on success.
    ///
    /// - Returns: `true` on successful connection.
    func connect() async -> Bool {
        if isConnected {
            Self.logger.warning("connect() called while already connected")
            return true
        }

        updateState(connectionState: .connecting)

        let conn = NWConnection(
            host: telloHost,
            port: telloPort,
            using: .udp
        )

        // Wait for the connection to become ready.
        let ready = await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            var resumed = false
            conn.stateUpdateHandler = { newState in
                guard !resumed else { return }
                switch newState {
                case .ready:
                    resumed = true
                    cont.resume(returning: true)
                case .failed, .cancelled:
                    resumed = true
                    cont.resume(returning: false)
                default:
                    break
                }
            }
            conn.start(queue: .global(qos: .userInitiated))
        }

        guard ready else {
            Self.logger.error("NWConnection failed to reach ready state")
            updateState(connectionState: .error, errorMessage: "UDP connection failed")
            return false
        }

        self.connection = conn
        self.emergencyConnection = conn
        Self.logger.info("UDP connection ready to \(self.config.telloDefaultHost):\(self.config.telloCommandPort)")

        // Send "command" to enter SDK mode.
        let response = await sendCommandInternal("command")
        let cleaned = response?
            .filter { $0.isLetter || $0.isNumber || $0.isWhitespace || ".,;:!?-_()".contains($0) }
            .trimmingCharacters(in: .whitespacesAndNewlines)

        guard let cleaned, cleaned.caseInsensitiveCompare("ok") == .orderedSame else {
            Self.logger.error("SDK mode failed, response: \(response ?? "nil") (cleaned: \(cleaned ?? "nil"))")
            conn.cancel()
            self.connection = nil
            self.emergencyConnection = nil
            updateState(connectionState: .error, errorMessage: "SDK mode failed -- tap Retry to try again")
            return false
        }

        isConnected = true
        updateState(connectionState: .connected)
        Self.logger.info("Connected to Tello")

        startKeepalive()
        startStatePolling()
        return true
    }

    /// Disconnect from the Tello.
    ///
    /// Cancels keepalive and state polling, closes the connection, and resets
    /// state to disconnected.
    func disconnect() {
        keepaliveTask?.cancel()
        keepaliveTask = nil
        statePollingTask?.cancel()
        statePollingTask = nil
        isConnected = false

        connection?.cancel()
        connection = nil
        emergencyConnection = nil

        updateState(
            connectionState: .disconnected,
            telemetry: TelloTelemetry()
        )
        Self.logger.info("Disconnected")
    }

    // MARK: - Command Protocol

    /// Send a command string to the Tello and wait for a response.
    ///
    /// Actor isolation serializes commands. The Tello can only process one
    /// command at a time over its single UDP channel.
    ///
    /// - Parameters:
    ///   - command: The SDK command string (e.g., "takeoff", "battery?").
    ///   - timeoutMs: Response timeout in milliseconds.
    /// - Returns: The response string, or nil on timeout/error.
    func sendCommand(_ command: String, timeoutMs: Int? = nil) async -> String? {
        await sendCommandInternal(command, timeoutMs: timeoutMs ?? config.commandTimeoutMs)
    }

    /// Internal command sender. Caller is already inside the actor.
    private func sendCommandInternal(_ command: String, timeoutMs: Int? = nil) async -> String? {
        let timeout = timeoutMs ?? config.commandTimeoutMs

        guard let conn = connection else {
            Self.logger.warning("sendCommand(\(command)): no connection")
            return nil
        }

        guard let data = command.data(using: .utf8) else {
            Self.logger.error("sendCommand(\(command)): failed to encode UTF-8")
            return nil
        }

        // Send the command.
        do {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                conn.send(content: data, completion: .contentProcessed { error in
                    if let error {
                        cont.resume(throwing: error)
                    } else {
                        cont.resume()
                    }
                })
            }
            Self.logger.debug(">> \(command)")
        } catch {
            Self.logger.error("Send failed for '\(command)': \(error.localizedDescription)")
            return nil
        }

        // Receive response with timeout.
        return await withTaskGroup(of: String?.self) { group in
            group.addTask {
                do {
                    let (responseData, _, _) = try await self.receiveOnce(conn)
                    return String(data: responseData, encoding: .utf8)
                } catch {
                    Self.logger.warning("Receive failed for '\(command)': \(error.localizedDescription)")
                    return nil
                }
            }

            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(timeout) * 1_000_000)
                return nil
            }

            // First result wins.
            if let result = await group.next() {
                group.cancelAll()
                if let response = result {
                    Self.logger.debug("<< \(response ?? "nil")")
                }
                return result
            }
            group.cancelAll()
            Self.logger.warning("Timeout waiting for response to: \(command)")
            return nil
        }
    }

    /// Receive a single UDP message from the connection.
    private func receiveOnce(_ conn: NWConnection) async throws -> (Data, NWEndpoint?, Bool) {
        try await withCheckedThrowingContinuation { cont in
            conn.receiveMessage { data, context, isComplete, error in
                if let error {
                    cont.resume(throwing: error)
                } else if let data {
                    // NWConnection for UDP validates the peer address internally;
                    // no need for manual address filtering like in DatagramSocket.
                    cont.resume(returning: (data, nil, isComplete))
                } else {
                    cont.resume(throwing: TelloError.emptyResponse)
                }
            }
        }
    }

    // MARK: - Stream Control

    /// Enable the Tello video stream.
    ///
    /// - Returns: `true` if "streamon" succeeded.
    func startStream() async -> Bool {
        let response = await sendCommand("streamon")
        if let response, response.trimmingCharacters(in: .whitespacesAndNewlines)
            .caseInsensitiveCompare("ok") == .orderedSame {
            updateState(connectionState: .streaming)
            Self.logger.info("Video stream started")
            return true
        }
        Self.logger.warning("startStream failed: \(response ?? "nil")")
        return false
    }

    /// Disable the Tello video stream.
    func stopStream() async {
        _ = await sendCommand("streamoff")
        if isConnected {
            updateState(connectionState: .connected)
        }
        Self.logger.info("Video stream stopped")
    }

    // MARK: - Flight Commands

    /// Command the Tello to take off.
    func takeoff() async -> Bool {
        let response = await sendCommand("takeoff")
        if let response, response.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased().hasPrefix("ok") {
            updateState(isFlying: true)
            Self.logger.info("Takeoff")
            return true
        }
        Self.logger.warning("Takeoff failed: \(response ?? "nil")")
        return false
    }

    /// Command the Tello to land.
    func land() async -> Bool {
        let response = await sendCommand("land")
        if let response, response.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased().hasPrefix("ok") {
            updateState(isFlying: false)
            Self.logger.info("Landing")
            return true
        }
        Self.logger.warning("Land failed: \(response ?? "nil")")
        return false
    }

    /// Emergency motor kill -- immediately stops all motors.
    ///
    /// Unlike `land()`, which performs a controlled descent, this command
    /// cuts power instantly. The drone will fall from whatever altitude
    /// it is at. Use only when a controlled landing is not possible
    /// (e.g., flyaway, entanglement, imminent collision with a person).
    ///
    /// **SAFETY-CRITICAL**: This method is `nonisolated` to bypass actor
    /// isolation. It sends "emergency" directly on the NWConnection without
    /// waiting for pending commands to complete.
    ///
    /// - Returns: `true` if the send completed without error.
    nonisolated func emergencyStop() async -> Bool {
        guard let conn = emergencyRef.get() else {
            Self.logger.error("emergencyStop: no connection")
            return false
        }

        guard let data = "emergency".data(using: .utf8) else { return false }

        do {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                conn.send(content: data, completion: .contentProcessed { error in
                    if let error {
                        cont.resume(throwing: error)
                    } else {
                        cont.resume()
                    }
                })
            }
            Self.logger.warning("EMERGENCY STOP executed (bypass actor)")
            // Update state inside actor isolation.
            await setIsFlying(false)
            return true
        } catch {
            Self.logger.error("Emergency stop failed: \(error.localizedDescription)")
            return false
        }
    }

    /// Actor-isolated helper for `emergencyStop` to update flying state.
    private func setIsFlying(_ flying: Bool) {
        updateState(isFlying: flying)
    }

    /// Move the drone in a cardinal direction.
    ///
    /// Fire-and-forget: returns immediately. If the actor is busy with another
    /// command, the move is dropped (matching Android's tryLock behavior).
    ///
    /// - Parameters:
    ///   - direction: One of "forward", "back", "left", "right", "up", "down".
    ///   - distanceCm: Distance in centimeters, clamped to 20...500.
    func move(direction: String, distanceCm: Int) async {
        let validDirections: Set<String> = ["forward", "back", "left", "right", "up", "down"]
        guard validDirections.contains(direction) else {
            Self.logger.warning("Invalid direction: \(direction)")
            return
        }

        let clamped = min(max(distanceCm, 20), 500)
        _ = await sendCommandInternal("\(direction) \(clamped)")
    }

    /// Rotate the drone clockwise or counter-clockwise.
    ///
    /// - Parameter degrees: Positive for clockwise, negative for CCW. Clamped to -360...360.
    func rotate(degrees: Int) async {
        let clamped = min(max(degrees, -360), 360)
        let command: String
        if clamped >= 0 {
            command = "cw \(clamped)"
        } else {
            command = "ccw \(-clamped)"
        }
        _ = await sendCommandInternal(command)
    }

    /// Send RC joystick control values.
    ///
    /// Rate-limited to one call every 50ms to avoid flooding the Tello.
    /// Fire-and-forget -- no response expected.
    ///
    /// - Parameters:
    ///   - lr: Left/right (-100 to 100).
    ///   - fb: Forward/backward (-100 to 100).
    ///   - ud: Up/down (-100 to 100).
    ///   - yaw: Yaw (-100 to 100).
    func rcControl(lr: Int, fb: Int, ud: Int, yaw: Int) {
        let now = DispatchTime.now().uptimeNanoseconds / 1_000_000
        guard now - lastRcTime >= Self.rcMinIntervalMs else { return }
        lastRcTime = now

        guard let conn = connection else { return }

        let clampedLr = min(max(lr, -100), 100)
        let clampedFb = min(max(fb, -100), 100)
        let clampedUd = min(max(ud, -100), 100)
        let clampedYaw = min(max(yaw, -100), 100)

        let command = "rc \(clampedLr) \(clampedFb) \(clampedUd) \(clampedYaw)"
        guard let data = command.data(using: .utf8) else { return }

        conn.send(content: data, completion: .contentProcessed { error in
            if let error {
                Self.logger.warning("RC control send failed: \(error.localizedDescription)")
            }
        })
    }

    /// Stop all movement and hover in place.
    func hover() {
        rcControl(lr: 0, fb: 0, ud: 0, yaw: 0)
    }

    // MARK: - Keepalive

    /// Start the keepalive timer.
    ///
    /// Sends "rc 0 0 0 0" (hover) every `keepaliveIntervalSec` seconds to reset
    /// the Tello's 15-second auto-land timer. Uses a direct UDP send that bypasses
    /// actor command serialization -- rc commands are fire-and-forget.
    private func startKeepalive() {
        keepaliveTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(self.config.keepaliveIntervalSec) * 1_000_000_000)
                guard !Task.isCancelled else { break }
                guard await self.isStillConnected() else { break }
                await self.sendKeepalivePacket()
            }
        }
    }

    /// Check if still connected (actor-isolated accessor).
    private func isStillConnected() -> Bool {
        return isConnected
    }

    /// Send a keepalive packet directly without acquiring the actor's command serialization.
    private func sendKeepalivePacket() {
        guard let conn = connection else { return }
        guard let data = "rc 0 0 0 0".data(using: .utf8) else { return }

        conn.send(content: data, completion: .contentProcessed { error in
            if let error {
                Self.logger.warning("Keepalive packet failed: \(error.localizedDescription)")
            }
        })
    }

    // MARK: - State Polling

    /// Start the state polling loop.
    ///
    /// Queries battery and height every `statePollingIntervalSec` seconds.
    /// Tracks consecutive failures for disconnect detection and zero-height
    /// polls for crash detection.
    private func startStatePolling() {
        statePollingTask = Task { [weak self] in
            guard let self else { return }
            var pollFailures = 0
            var zeroHeightCount = 0

            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(self.config.statePollingIntervalSec) * 1_000_000_000)
                guard !Task.isCancelled else { break }
                guard await self.isStillConnected() else { break }

                do {
                    let (battery, height) = await self.pollBatteryAndHeight()

                    if battery == nil && height == nil {
                        Self.logger.warning("State poll: both queries failed")
                        pollFailures += 1
                    } else {
                        let current = await self.getState()
                        var isFlying = current.telemetry.isFlying

                        if let height {
                            if isFlying && height == 0 {
                                zeroHeightCount += 1
                                if zeroHeightCount >= 3 {
                                    isFlying = false
                                    Self.logger.warning("Crash detected -- height 0 for \(zeroHeightCount) polls")
                                    zeroHeightCount = 0
                                }
                            } else {
                                zeroHeightCount = 0
                            }
                        }

                        await self.applyPolledTelemetry(
                            battery: battery,
                            height: height,
                            isFlying: isFlying,
                            current: current
                        )
                        pollFailures = 0
                    }

                    if pollFailures >= 5 {
                        Self.logger.error("Connection lost -- \(pollFailures) consecutive poll failures")
                        await self.handleConnectionLost()
                        break
                    }
                }
            }
        }
    }

    /// Query battery and height from the Tello.
    private func pollBatteryAndHeight() async -> (Int?, Int?) {
        let battery = await queryInt("battery?", timeoutMs: 3000)
        let height = await queryInt("height?", timeoutMs: 3000)
        return (battery, height)
    }

    /// Get current state snapshot.
    private func getState() -> TelloState {
        return state
    }

    /// Apply polled telemetry values into state.
    private func applyPolledTelemetry(battery: Int?, height: Int?, isFlying: Bool, current: TelloState) {
        let telemetry = TelloTelemetry(
            battery: battery ?? current.telemetry.battery,
            height: height ?? current.telemetry.height,
            temperature: current.telemetry.temperature,
            flightTime: current.telemetry.flightTime,
            isFlying: isFlying
        )
        updateState(telemetry: telemetry)
    }

    /// Handle connection loss detected by polling.
    private func handleConnectionLost() {
        isConnected = false
        updateState(connectionState: .disconnected, errorMessage: "Connection lost")
    }

    /// Query an integer value from the Tello.
    ///
    /// Handles Tello SDK 2.0 tilde-delimited ranges (e.g., "56~58" for temp?)
    /// by taking the first number in the range.
    ///
    /// - Parameters:
    ///   - query: The query command (e.g., "battery?").
    ///   - timeoutMs: Response timeout in milliseconds.
    /// - Returns: The integer value, or nil on failure or unparseable response.
    private func queryInt(_ query: String, timeoutMs: Int) async -> Int? {
        guard let response = await sendCommandInternal(query, timeoutMs: timeoutMs) else { return nil }
        let trimmed = response.trimmingCharacters(in: .whitespacesAndNewlines)
        let firstPart = trimmed.split(separator: "~").first.map(String.init)
        return firstPart.flatMap { Int($0) }
    }

    // MARK: - State Management

    /// Update the observable TelloState, merging non-nil parameters with the current state.
    private func updateState(
        connectionState: TelloConnectionState? = nil,
        telemetry: TelloTelemetry? = nil,
        errorMessage: String? = nil,
        isOnTelloWifi: Bool? = nil,
        isFlying: Bool? = nil
    ) {
        let baseTelemetry = telemetry ?? state.telemetry
        var finalTelemetry = baseTelemetry
        if let isFlying {
            finalTelemetry.isFlying = isFlying
        }

        let resolvedError: String?
        if let errorMessage {
            resolvedError = errorMessage
        } else if let connectionState, connectionState != .error {
            resolvedError = nil
        } else {
            resolvedError = state.errorMessage
        }

        state = TelloState(
            connectionState: connectionState ?? state.connectionState,
            telemetry: finalTelemetry,
            errorMessage: resolvedError,
            isOnTelloWifi: isOnTelloWifi ?? state.isOnTelloWifi
        )

        stateContinuation?.yield(state)
    }
}

// MARK: - Errors

enum TelloError: Error {
    case emptyResponse
    case connectionFailed(String)
    case timeout
}
