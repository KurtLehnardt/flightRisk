import AVFoundation
import CoreHaptics
import os
import UIKit

/// Tiered alert manager for match events.
///
/// Mirrors the Android `AlertManager` and Python `alerts.py` throttle logic
/// with iOS-native audio, haptic, and visual notification actions:
///
/// | Alert Level       | Audio | Haptic | Visual |
/// |-------------------|-------|--------|--------|
/// | confirmed_match   |   X   |   X    |   X    |
/// | possible_match    |       |   X    |   X    |
/// | weak_signal       |       |        |   X    |
///
/// A per-track cooldown (default 10 seconds, matching Python's
/// `ALERT_COOLDOWN`) prevents repeated alerts for the same spatial
/// track key within the cooldown window.
@MainActor
final class AlertManager {

    // MARK: - Alert Level Constants

    /// Alert level constants matching the Python pipeline.
    static let confirmedMatch = "confirmed_match"
    static let possibleMatch  = "possible_match"
    static let weakSignal     = "weak_signal"
    static let noMatch        = "no_match"

    // MARK: - Types

    /// Result of a ``fireAlert(level:trackKey:)`` call, indicating which
    /// actions were taken.
    struct AlertActions {
        let audio: Bool
        let haptic: Bool
        let visual: Bool
    }

    // MARK: - Properties

    private let logger = Logger(subsystem: "com.flightrisk", category: "alert")

    /// Cooldown interval between alerts for the same track key.
    private let cooldownInterval: TimeInterval

    /// Tracks the last alert time per track key (for cooldown).
    private var lastAlertTime: [String: Date] = [:]

    /// Active audio player (one at a time).
    private var activePlayer: AVAudioPlayer?

    /// Core Haptics engine for confirmed-match repeating pattern.
    private var hapticEngine: CHHapticEngine?

    /// Notification feedback generator for confirmed-match fallback.
    private let notificationGenerator = UINotificationFeedbackGenerator()

    /// Impact feedback generator for possible-match haptic.
    private let impactGenerator = UIImpactFeedbackGenerator(style: .heavy)

    // MARK: - Initialization

    /// Creates an alert manager.
    ///
    /// - Parameter cooldownSeconds: Cooldown in seconds between alerts for
    ///   the same track key. Default 10 (matching config.reasoning.alertCooldown).
    init(cooldownSeconds: TimeInterval = 10.0) {
        self.cooldownInterval = cooldownSeconds
        notificationGenerator.prepare()
        impactGenerator.prepare()
        setupHapticEngine()
    }

    // MARK: - Public API

    /// Fire an alert for the given level and track key.
    ///
    /// Respects the per-track cooldown: if the same `trackKey` was alerted
    /// within `cooldownInterval`, this is a no-op and returns `false`.
    ///
    /// - Parameters:
    ///   - level: One of ``confirmedMatch``, ``possibleMatch``,
    ///     ``weakSignal``, or ``noMatch``.
    ///   - trackKey: Spatial track identifier (grid-cell key).
    /// - Returns: `true` if an alert was fired, `false` if suppressed or
    ///   no-match.
    @discardableResult
    func fireAlert(level: String, trackKey: String) -> Bool {
        guard level != Self.noMatch else { return false }

        let now = Date()
        if let last = lastAlertTime[trackKey],
           now.timeIntervalSince(last) < cooldownInterval {
            logger.debug("Alert for \(trackKey) suppressed (cooldown)")
            return false
        }

        lastAlertTime[trackKey] = now
        logger.info("Firing alert: level=\(level) track=\(trackKey)")

        switch level {
        case Self.confirmedMatch:
            playAlarmSound()
            playConfirmedHapticPattern()
            return true

        case Self.possibleMatch:
            impactGenerator.impactOccurred()
            return true

        case Self.weakSignal:
            // Visual only — no audio or haptic
            return true

        default:
            return false
        }
    }

    /// Check whether a track key is currently within its cooldown window.
    func isWithinCooldown(trackKey: String) -> Bool {
        guard let last = lastAlertTime[trackKey] else { return false }
        return Date().timeIntervalSince(last) < cooldownInterval
    }

    /// Dismiss alerts for a specific track key.
    /// Stops audio and haptic feedback if active.
    func dismiss(trackKey: String) {
        lastAlertTime.removeValue(forKey: trackKey)
        stopAudio()
        stopHaptics()
        logger.debug("Dismissed alert for \(trackKey)")
    }

    /// Dismiss all active alerts. Stops all audio and haptic feedback
    /// and clears the cooldown tracker.
    func dismissAll() {
        lastAlertTime.removeAll()
        stopAudio()
        stopHaptics()
        logger.debug("All alerts dismissed")
    }

    /// Release all resources. Call when the alert manager is no longer
    /// needed (e.g. when the view is dismissed).
    func release() {
        dismissAll()
        hapticEngine?.stop(completionHandler: { _ in })
        hapticEngine = nil
        activePlayer = nil
    }

    // MARK: - Audio

    /// Play a repeating system alert sound.
    private func playAlarmSound() {
        stopAudio()

        // Try to play a system alert sound via AudioServicesPlayAlertSound
        // which will also vibrate on devices without haptic support.
        AudioServicesPlayAlertSound(SystemSoundID(kSystemSoundID_Vibrate))

        // Additionally try to play a looping alarm via AVAudioPlayer
        // using a bundled sound or falling back to system sound.
        do {
            // Configure audio session for alarm playback
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .default, options: [.duckOthers])
            try session.setActive(true)

            // Attempt to use a bundled alarm sound
            if let url = Bundle.main.url(forResource: "alarm", withExtension: "caf")
                       ?? Bundle.main.url(forResource: "alarm", withExtension: "wav")
                       ?? Bundle.main.url(forResource: "alarm", withExtension: "mp3") {
                let player = try AVAudioPlayer(contentsOf: url)
                player.numberOfLoops = -1 // Loop indefinitely
                player.volume = 1.0
                player.play()
                activePlayer = player
            } else {
                // No bundled sound; use repeated system alert as fallback.
                // The initial AudioServicesPlayAlertSound above already fired once.
                logger.info("No bundled alarm sound; using system alert only")
            }
        } catch {
            logger.error("Failed to play alarm sound: \(error.localizedDescription)")
        }
    }

    /// Stop any active audio playback.
    private func stopAudio() {
        activePlayer?.stop()
        activePlayer = nil

        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    // MARK: - Haptic

    /// Set up the Core Haptics engine.
    private func setupHapticEngine() {
        guard CHHapticEngine.capabilitiesForHardware().supportsHaptics else {
            logger.info("Device does not support Core Haptics")
            return
        }

        do {
            let engine = try CHHapticEngine()
            engine.resetHandler = { [weak self] in
                self?.logger.info("Haptic engine reset")
                try? self?.hapticEngine?.start()
            }
            engine.stoppedHandler = { [weak self] reason in
                self?.logger.info("Haptic engine stopped: \(reason.rawValue)")
            }
            try engine.start()
            hapticEngine = engine
        } catch {
            logger.error("Failed to create haptic engine: \(error.localizedDescription)")
        }
    }

    /// Play the confirmed-match haptic pattern.
    ///
    /// Pattern: [0ms start, 500ms vibrate, 200ms pause, 500ms vibrate,
    /// 200ms pause, 500ms vibrate] — matching the Android vibration pattern
    /// `longArrayOf(0, 500, 200, 500, 200, 500)`.
    private func playConfirmedHapticPattern() {
        guard let engine = hapticEngine else {
            // Fallback to UIKit notification feedback
            notificationGenerator.notificationOccurred(.warning)
            return
        }

        do {
            // Build pattern: three 500ms bursts separated by 200ms pauses
            let events: [CHHapticEvent] = [
                // First burst at t=0, duration 0.5s
                CHHapticEvent(
                    eventType: .hapticContinuous,
                    parameters: [
                        CHHapticEventParameter(parameterID: .hapticIntensity, value: 1.0),
                        CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.5),
                    ],
                    relativeTime: 0,
                    duration: 0.5
                ),
                // Second burst at t=0.7 (0.5 + 0.2 pause), duration 0.5s
                CHHapticEvent(
                    eventType: .hapticContinuous,
                    parameters: [
                        CHHapticEventParameter(parameterID: .hapticIntensity, value: 1.0),
                        CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.5),
                    ],
                    relativeTime: 0.7,
                    duration: 0.5
                ),
                // Third burst at t=1.4 (0.7 + 0.5 + 0.2 pause), duration 0.5s
                CHHapticEvent(
                    eventType: .hapticContinuous,
                    parameters: [
                        CHHapticEventParameter(parameterID: .hapticIntensity, value: 1.0),
                        CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.5),
                    ],
                    relativeTime: 1.4,
                    duration: 0.5
                ),
            ]

            let pattern = try CHHapticPattern(events: events, parameters: [])
            let player = try engine.makeAdvancedPlayer(with: pattern)
            player.loopEnabled = true
            try player.start(atTime: CHHapticTimeImmediate)
        } catch {
            logger.error("Failed to play haptic pattern: \(error.localizedDescription)")
            // Fallback
            notificationGenerator.notificationOccurred(.warning)
        }
    }

    /// Stop any active haptic playback.
    private func stopHaptics() {
        hapticEngine?.stop(completionHandler: { _ in })
        // Re-start engine so it's ready for next alert
        try? hapticEngine?.start()
    }
}
