import Foundation
import os.log

// MARK: - DroneAlert

/// Observable alerts emitted by DroneManager for UI consumption.
enum DroneAlert: Equatable {
    case connectionLost
    case batteryWarning(Int)
    case batteryCritical(Int)
    case crashDetected
    case streamFrozen
    case obstacleDetected(action: String, confidence: Float)
    case matchPause
}

// MARK: - DroneManager

/// Facade that owns `TelloConnection`, `TelloFrameSource`, `TelloWifiChecker`,
/// `SearchPattern`, and `ObstacleGuard` and coordinates them into a single
/// lifecycle-aware entry point.
///
/// Port of Android `DroneManager.kt` to Swift actor. Monitors telemetry for
/// critical conditions (low battery, crash, connection loss, frozen stream)
/// and emits `DroneAlert`s through an `AsyncStream`.
actor DroneManager {

    // MARK: - Constants

    private static let logger = Logger(subsystem: "com.flightrisk", category: "DroneManager")
    private static let maxAvoidanceRetries = 5
    private static let avoidanceSideCm = 30
    private static let avoidanceReverseCm = 50

    // MARK: - Sub-components

    let wifiChecker: TelloWifiChecker
    let connection: TelloConnection
    let frameSource: TelloFrameSource
    private var obstacleGuard: ObstacleGuard?

    // MARK: - Configuration

    private let config: DroneConfig

    // MARK: - State streams

    /// Async stream of drone state changes, forwarded from TelloConnection.
    nonisolated let stateStream: AsyncStream<TelloState>

    /// Async stream of alerts for UI consumption.
    nonisolated let alertStream: AsyncStream<DroneAlert>
    private let alertContinuation: AsyncStream<DroneAlert>.Continuation

    // MARK: - Internal state

    private var monitorTask: Task<Void, Never>?
    private var searchTask: Task<Void, Never>?
    private var commandedLanding = false

    private(set) var searchActive = false
    private(set) var searchPaused = false
    private(set) var searchProgress: (current: Int, total: Int) = (0, 0)

    // MARK: - Init

    init(config: DroneConfig = DroneConfig()) {
        self.config = config
        self.wifiChecker = TelloWifiChecker(
            telloHost: config.telloDefaultHost,
            telloPort: config.telloCommandPort
        )
        self.connection = TelloConnection(config: config)
        self.frameSource = TelloFrameSource()

        // Forward connection's stateStream
        self.stateStream = connection.stateStream

        // Create alert stream
        var continuation: AsyncStream<DroneAlert>.Continuation!
        self.alertStream = AsyncStream { continuation = $0 }
        self.alertContinuation = continuation
    }

    deinit {
        alertContinuation.finish()
    }

    // MARK: - Connect / Disconnect

    /// Connect to the Tello, start video stream, begin telemetry monitoring,
    /// and initialize obstacle avoidance in the background.
    ///
    /// - Returns: `true` on successful connection and stream start.
    func connectAndStream() async -> Bool {
        // Step 1: Check WiFi
        let wifiStatus = await wifiChecker.check()
        guard case .onTelloWifi = wifiStatus else {
            let guidance = await wifiChecker.getGuidanceMessage(wifiStatus)
            Self.logger.warning("Not on Tello WiFi: \(guidance)")
            return false
        }

        // Step 2: Connect
        let connected = await connection.connect()
        guard connected else {
            Self.logger.error("Connection failed")
            return false
        }

        // Step 3: Start stream
        let streaming = await connection.startStream()
        guard streaming else {
            Self.logger.error("Stream start failed")
            await connection.disconnect()
            return false
        }

        // Step 4: Wire up frozen-stream recovery and start frame source
        frameSource.onStreamFrozen = { [weak self] in
            guard let self else { return }
            Task { await self.recoverStream() }
        }
        frameSource.start()

        // Step 5: Start telemetry monitoring
        startMonitoring()

        // Step 6: Initialize obstacle guard in background
        Task { [weak self] in
            guard let self else { return }
            await self.initObstacleGuard()
        }

        Self.logger.info("Connected and streaming")
        return true
    }

    /// Disconnect from the Tello. Lands the drone first if still flying
    /// (with a 3-second timeout).
    func disconnect() async {
        monitorTask?.cancel()
        monitorTask = nil

        // Land if flying
        let currentState = await connection.state
        if currentState.telemetry.isFlying {
            Self.logger.warning("Disconnecting while flying -- attempting to land first")
            do {
                try await withThrowingTaskGroup(of: Bool.self) { group in
                    group.addTask {
                        return await self.connection.land()
                    }
                    group.addTask {
                        try await Task.sleep(nanoseconds: 3_000_000_000)
                        throw CancellationError()
                    }
                    // Take whichever finishes first
                    _ = try await group.next()
                    group.cancelAll()
                }
            } catch {
                Self.logger.error("Land attempt during disconnect failed: \(error.localizedDescription)")
            }
        }

        // Tear down frame source
        frameSource.onStreamFrozen = nil
        frameSource.stop()

        // Stop stream (ignore errors)
        await connection.stopStream()

        // Disconnect connection
        await connection.disconnect()

        // Clean up obstacle guard
        obstacleGuard = nil

        Self.logger.info("Disconnected")
    }

    // MARK: - Obstacle Guard

    /// Initialize the obstacle avoidance system.
    private func initObstacleGuard() async {
        guard obstacleGuard == nil else { return }

        let guard_ = ObstacleGuard()
        await guard_.initialize()

        if await guard_.isAvailable {
            obstacleGuard = guard_
            Self.logger.info("Obstacle avoidance enabled")
        } else {
            Self.logger.warning("Obstacle avoidance: MiDaS model not available")
        }
    }

    // MARK: - Stream Recovery

    /// Recover a frozen video stream by cycling streamoff/streamon and
    /// restarting the frame source.
    private func recoverStream() async {
        Self.logger.warning("Recovering frozen stream")

        frameSource.stop()
        _ = await connection.sendCommand("streamoff")

        try? await Task.sleep(nanoseconds: 500_000_000) // 500ms

        _ = await connection.sendCommand("streamon")

        try? await Task.sleep(nanoseconds: 200_000_000) // 200ms

        frameSource.onStreamFrozen = { [weak self] in
            guard let self else { return }
            Task { await self.recoverStream() }
        }
        frameSource.start()

        alertContinuation.yield(.streamFrozen)
    }

    // MARK: - Lifecycle Hooks

    /// Called when the app enters the background. Hovers the drone if flying.
    func onScenePhaseBackground() async {
        let currentState = await connection.state
        if currentState.telemetry.isFlying {
            Self.logger.info("Scene phase background while flying -- hovering")
            await connection.hover()
        }
    }

    /// Called on app termination. Emergency lands and disconnects.
    func onTermination() async {
        monitorTask?.cancel()
        monitorTask = nil
        searchTask?.cancel()
        searchTask = nil

        frameSource.stop()

        let currentState = await connection.state
        if currentState.telemetry.isFlying {
            Self.logger.warning("Termination while flying -- emergency landing")
            // Attempt landing with short timeout
            _ = await withTaskGroup(of: Bool.self) { group in
                group.addTask {
                    return await self.connection.land()
                }
                group.addTask {
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    return false
                }
                let result = await group.next() ?? false
                group.cancelAll()
                return result
            }
        }

        await connection.disconnect()
        obstacleGuard = nil
        Self.logger.info("Terminated")
    }

    // MARK: - Flight Command Delegates

    func takeoff() async -> Bool {
        await connection.takeoff()
    }

    func land() async -> Bool {
        commandedLanding = true
        return await connection.land()
    }

    func move(direction: String, distanceCm: Int) async {
        await connection.move(direction: direction, distanceCm: distanceCm)
    }

    func rotate(degrees: Int) async {
        await connection.rotate(degrees: degrees)
    }

    func rcControl(lr: Int, fb: Int, ud: Int, yaw: Int) async {
        await connection.rcControl(lr: lr, fb: fb, ud: ud, yaw: yaw)
    }

    func hover() async {
        await connection.hover()
    }

    nonisolated func emergencyStop() async -> Bool {
        // Set commandedLanding inside actor isolation first
        await setCommandedLanding(true)
        return await connection.emergencyStop()
    }

    /// Actor-isolated helper for emergencyStop.
    private func setCommandedLanding(_ value: Bool) {
        commandedLanding = value
    }

    // MARK: - Pause / Resume for Match Detection

    /// Pause the active search pattern and hover in place.
    /// Called when the vision pipeline detects a confident match.
    func pauseSearchForMatch() async {
        guard searchActive, !searchPaused else { return }
        searchPaused = true
        Self.logger.info("Search paused -- match detected, hovering")
        await connection.hover()
        alertContinuation.yield(.matchPause)
    }

    /// Resume the search pattern after the operator dismisses a match alert.
    func resumeSearch() {
        guard searchPaused else { return }
        searchPaused = false
        Self.logger.info("Search resumed")
    }

    // MARK: - Search Pattern Execution

    /// Start executing a search pattern with obstacle avoidance.
    ///
    /// Generates waypoints from `SearchPattern.generate(type:)` and executes
    /// them sequentially. Before each forward move, checks for obstacles and
    /// attempts up to 5 evasive maneuvers per waypoint.
    func startSearchPattern(_ pattern: PatternType = .expandingSquare) {
        guard !searchActive else { return }

        let waypoints = SearchPattern.generate(type: pattern)
        searchActive = true
        searchPaused = false
        searchProgress = (0, waypoints.count)
        Self.logger.info("Starting search pattern: \(pattern.rawValue), \(waypoints.count) waypoints")

        searchTask = Task { [weak self] in
            guard let self else { return }

            for (index, wp) in waypoints.enumerated() {
                let active = await self.isSearchActive()
                if !active { break }

                // Wait while paused for match inspection
                while true {
                    let paused = await self.isSearchPaused()
                    let active2 = await self.isSearchActive()
                    guard paused && active2 else { break }
                    try? await Task.sleep(nanoseconds: 200_000_000)
                }
                let stillActive = await self.isSearchActive()
                if !stillActive { break }

                await self.updateSearchProgress(current: index + 1, total: waypoints.count)

                do {
                    if wp.distanceCm > 0 {
                        // Obstacle check before each forward move
                        let pathClear = await self.checkAndAvoidObstacles(waypointIndex: index)
                        if !pathClear {
                            Self.logger.warning("Skipping waypoint \(index + 1) -- obstacle avoidance exhausted")
                            continue
                        }

                        await self.connection.move(direction: wp.direction, distanceCm: wp.distanceCm)
                        try await Task.sleep(nanoseconds: 500_000_000) // 500ms
                    }
                    if wp.rotateDegrees != 0 {
                        await self.connection.rotate(degrees: wp.rotateDegrees)
                        try await Task.sleep(nanoseconds: 500_000_000) // 500ms
                    }
                } catch is CancellationError {
                    break
                } catch {
                    Self.logger.error("Search pattern error at waypoint \(index + 1): \(error.localizedDescription)")
                    break
                }
            }

            await self.clearSearchState()
            Self.logger.info("Search pattern complete")
        }
    }

    /// Stop the active search pattern and hover.
    func stopSearchPattern() async {
        searchActive = false
        searchPaused = false
        searchTask?.cancel()
        searchTask = nil
        searchProgress = (0, 0)
        await connection.hover()
        Self.logger.info("Search pattern stopped")
    }

    // MARK: - Search State Helpers

    private func isSearchActive() -> Bool { searchActive }
    private func isSearchPaused() -> Bool { searchPaused }

    private func updateSearchProgress(current: Int, total: Int) {
        searchProgress = (current, total)
    }

    private func clearSearchState() {
        searchActive = false
        searchPaused = false
        searchProgress = (0, 0)
    }

    // MARK: - Obstacle Avoidance

    /// Check for obstacles before moving. If an obstacle is detected,
    /// perform evasive maneuvers up to `maxAvoidanceRetries` times.
    ///
    /// - Returns: `true` if the path is now clear, `false` if retries exhausted.
    private func checkAndAvoidObstacles(waypointIndex: Int) async -> Bool {
        guard let guard_ = obstacleGuard else { return true }

        for retry in 1...Self.maxAvoidanceRetries {
            guard searchActive else { return false }

            guard let frame = frameSource.getLatestFrame() else { return true }

            let check = await guard_.checkPath(frame: frame)
            if check.safe { return true }

            Self.logger.warning("Obstacle at waypoint \(waypointIndex + 1): action=\(check.action), depth=\(check.centerDepth), retry=\(retry)")
            alertContinuation.yield(.obstacleDetected(action: check.action, confidence: check.confidence))

            if retry >= Self.maxAvoidanceRetries {
                Self.logger.warning("Max avoidance retries at waypoint \(waypointIndex + 1)")
                return false
            }

            do {
                switch check.action {
                case "go_left":
                    await connection.move(direction: "left", distanceCm: Self.avoidanceSideCm)
                    try await Task.sleep(nanoseconds: 500_000_000)

                case "go_right":
                    await connection.move(direction: "right", distanceCm: Self.avoidanceSideCm)
                    try await Task.sleep(nanoseconds: 500_000_000)

                case "reverse":
                    await connection.move(direction: "back", distanceCm: Self.avoidanceReverseCm)
                    try await Task.sleep(nanoseconds: 500_000_000)
                    await connection.rotate(degrees: 90)
                    try await Task.sleep(nanoseconds: 500_000_000)

                default:
                    break
                }
            } catch is CancellationError {
                return false
            } catch {
                Self.logger.error("Avoidance maneuver failed: \(error.localizedDescription)")
                return false
            }
        }
        return false
    }

    // MARK: - Telemetry Monitoring

    /// Start monitoring telemetry for battery, crash, and connection-loss alerts.
    ///
    /// Mirrors the Android `startMonitoring()` logic:
    /// - Connection state changes trigger `.connectionLost`
    /// - Battery at/below `batteryWarnThreshold` triggers `.batteryWarning`
    /// - Battery at/below `batteryCriticalThreshold` triggers `.batteryCritical` + auto-land
    /// - isFlying going false while connected (not from commanded landing) triggers `.crashDetected`
    private func startMonitoring() {
        monitorTask?.cancel()
        monitorTask = Task { [weak self] in
            guard let self else { return }

            var wasConnected = true
            var wasFlying = false
            var batteryWarnSent = false
            var batteryCriticalSent = false

            for await state in await self.connection.stateStream {
                guard !Task.isCancelled else { break }

                // Connection loss detection
                let isConnected = state.connectionState != .disconnected &&
                    state.connectionState != .error
                if wasConnected && !isConnected {
                    Self.logger.error("Connection lost")
                    await self.emitAlert(.connectionLost)
                }
                wasConnected = isConnected

                // Battery monitoring
                if let battery = state.telemetry.battery {
                    let warnThreshold = await self.getWarnThreshold()
                    let criticalThreshold = await self.getCriticalThreshold()

                    if battery > warnThreshold {
                        batteryWarnSent = false
                        batteryCriticalSent = false
                    } else if battery > criticalThreshold {
                        batteryCriticalSent = false
                        if !batteryWarnSent {
                            Self.logger.warning("Battery warning: \(battery)%")
                            await self.emitAlert(.batteryWarning(battery))
                            batteryWarnSent = true
                        }
                    } else {
                        // battery <= criticalThreshold
                        if !batteryWarnSent {
                            Self.logger.warning("Battery warning: \(battery)%")
                            await self.emitAlert(.batteryWarning(battery))
                            batteryWarnSent = true
                        }
                        if !batteryCriticalSent {
                            Self.logger.warning("Battery critical: \(battery)%")
                            await self.emitAlert(.batteryCritical(battery))
                            batteryCriticalSent = true
                            if state.telemetry.isFlying {
                                Self.logger.warning("Auto-landing due to critical battery")
                                _ = await self.land()
                            }
                        }
                    }
                }

                // Crash detection
                if wasFlying && !state.telemetry.isFlying {
                    let didCommandLanding = await self.didCommandLanding()
                    if state.telemetry.height == 0 && isConnected && !didCommandLanding {
                        Self.logger.error("Crash detected")
                        await self.emitAlert(.crashDetected)
                    }
                    await self.resetCommandedLanding()
                }
                wasFlying = state.telemetry.isFlying
            }
        }
    }

    // MARK: - Monitoring Helpers

    private func emitAlert(_ alert: DroneAlert) {
        alertContinuation.yield(alert)
    }

    private func getWarnThreshold() -> Int { config.batteryWarnThreshold }
    private func getCriticalThreshold() -> Int { config.batteryCriticalThreshold }
    private func didCommandLanding() -> Bool { commandedLanding }
    private func resetCommandedLanding() { commandedLanding = false }
}
