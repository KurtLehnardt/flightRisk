import CoreGraphics
import Foundation
import Observation
import os

/// ViewModel bridging the ``SearchPipeline`` event stream to SwiftUI.
///
/// Consumes the pipeline's `AsyncStream<PipelineEvent>` and updates
/// `@Observable` properties that SwiftUI views bind to. Also exposes
/// convenience methods for starting/stopping search, dismissing alerts,
/// and configuring drone-related state.
///
/// Must run on `@MainActor` since all property mutations drive SwiftUI updates.
@MainActor @Observable
final class SearchViewModel {

    private let logger = Logger(subsystem: "com.flightrisk", category: "SearchViewModel")

    // MARK: - Search State

    /// Whether the search is actively running.
    var isSearching = false

    /// Current frames-per-second from the pipeline.
    var fps: Float = 0

    /// Number of persons detected in the most recent frame.
    var personsDetected: Int = 0

    /// Highest match score seen during this search session.
    var highestMatchScore: Float = 0

    // MARK: - Confidence Progress

    /// Number of frames the current track has been corroborated.
    var confidenceFrames: Int = 0

    /// Number of frames needed for corroboration (default 3).
    var confidenceNeeded: Int = 3

    // MARK: - Alert State

    /// The currently active match alert, or nil if none.
    var activeAlert: MatchEntry?

    /// Drone-specific alert message (e.g. "Battery low"), or nil.
    var droneAlert: String?

    // MARK: - Detection Boxes

    /// Bounding boxes for overlay rendering in SwiftUI.
    /// Each entry contains the bbox coordinates, whether it is a match,
    /// and the stable track ID.
    var boxes: [(bbox: [Int], isMatch: Bool, trackId: Int)] = []

    // MARK: - Camera Frame

    /// The most recent annotated camera frame for display.
    var cameraFrame: CGImage?

    /// Width of the most recent frame in pixels.
    var frameWidth: Int = 0

    /// Height of the most recent frame in pixels.
    var frameHeight: Int = 0

    // MARK: - Drone State

    /// Current Tello drone telemetry and connection state.
    var droneState: TelloState?

    /// Whether frames come from the on-device camera or Tello video stream.
    var frameSourceMode: FrameSourceMode = .camera

    /// Latest raw frame from the Tello drone video feed.
    var latestDroneFrame: CGImage?

    /// Status message about drone connection (e.g. "Connecting...", "Connected").
    var droneConnectionMessage: String?

    // MARK: - Target Photo

    /// The selected target reference photo, or nil if none chosen.
    var targetPhoto: CGImage?

    /// Quality report for the target photo.
    var targetReport: QualityReport?

    /// Whether a target photo has been set.
    var hasTarget: Bool { targetPhoto != nil }

    // MARK: - Search Pattern

    /// The currently selected search pattern for autonomous drone flight.
    var selectedPattern: PatternType = .expandingSquare

    // MARK: - Match History

    /// All match entries from the current search session.
    var matchHistory: [MatchEntry] = []

    // MARK: - Pipeline Reference

    /// The active search pipeline, or nil if not configured.
    private var pipeline: SearchPipeline?

    /// Task consuming the pipeline event stream.
    private var eventTask: Task<Void, Never>?

    // MARK: - Event Stream Consumption

    /// Begin observing a pipeline's event stream.
    ///
    /// Cancels any previous observation. Updates SwiftUI-bound properties
    /// as events arrive.
    ///
    /// - Parameter pipeline: The search pipeline to observe.
    func observe(pipeline: SearchPipeline) {
        // Cancel any existing observation
        eventTask?.cancel()
        self.pipeline = pipeline

        eventTask = Task { [weak self] in
            for await event in pipeline.events {
                guard let self, !Task.isCancelled else { break }
                self.handleEvent(event)
            }
        }
    }

    /// Start a search using the configured pipeline.
    func startSearch() {
        guard let pipeline else {
            logger.warning("Cannot start search: no pipeline configured")
            return
        }

        isSearching = true
        highestMatchScore = 0
        confidenceFrames = 0
        activeAlert = nil
        matchHistory = []
        boxes = []

        Task {
            await pipeline.start()
        }

        logger.info("Search started")
    }

    /// Stop the active search.
    ///
    /// - Parameter reason: Why the search was stopped.
    func stopSearch(reason: String = "user_stopped") {
        guard let pipeline else { return }

        Task {
            await pipeline.stop(reason: reason)
        }

        isSearching = false
        logger.info("Search stopped: \(reason)")
    }

    /// Dismiss the active match alert and resume pipeline processing.
    func dismissAlert() {
        activeAlert = nil

        guard let pipeline else { return }
        Task {
            await pipeline.resumeAfterMatch()
        }
    }

    /// Dismiss the active alert and suppress the track from future alerts
    /// ("Not My Child" action).
    ///
    /// - Parameter trackKey: The spatial grid key of the track to suppress.
    func dismissAndSuppressTrack(trackKey: String) {
        activeAlert = nil

        guard let pipeline else { return }
        Task {
            await pipeline.suppressTrack(trackKey: trackKey)
            await pipeline.resumeAfterMatch()
        }
    }

    /// Stop observing and release the pipeline reference.
    func teardown() {
        eventTask?.cancel()
        eventTask = nil
        pipeline = nil
        isSearching = false
    }

    // MARK: - Event Handling

    /// Process a single pipeline event and update published properties.
    private func handleEvent(_ event: PipelineEvent) {
        switch event {
        case .frameProcessed(let annotatedFrame, let currentFps, let count):
            cameraFrame = annotatedFrame
            fps = currentFps
            personsDetected = count
            frameWidth = annotatedFrame.width
            frameHeight = annotatedFrame.height

        case .matchAlert(let entry):
            activeAlert = entry
            matchHistory.append(entry)
            if entry.score > highestMatchScore {
                highestMatchScore = entry.score
            }

        case .confidenceProgress(_, let framesMatched, let framesNeeded, _):
            confidenceFrames = framesMatched
            confidenceNeeded = framesNeeded

        case .searchComplete(let reason, _):
            if reason == "user_stopped" || reason == "battery_critical" {
                isSearching = false
            }
            // "match_found" does not stop the search -- it pauses until
            // the user confirms or dismisses via the alert overlay.
        }
    }
}
