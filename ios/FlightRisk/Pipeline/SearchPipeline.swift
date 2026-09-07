import CoreGraphics
import Foundation
import os

// MARK: - Pipeline Callback Protocols

/// Detection callback for person detection and frame annotation.
///
/// Implemented by `PersonDetector` (or an adapter) and set on the pipeline
/// before calling ``SearchPipeline/start()``.
protocol PipelineDetectionCallback: AnyObject {
    /// Detect persons in a camera frame.
    func detect(frame: CGImage) -> [Detection]

    /// Annotate a frame with detection bounding boxes.
    func annotate(frame: CGImage, detections: [Detection], matchIdx: Int?) -> CGImage?
}

/// ReID callback for body re-identification matching.
///
/// Implemented by `PersonReID` (or an adapter) and set on the pipeline
/// before calling ``SearchPipeline/start()``.
protocol PipelineReidCallback: AnyObject {
    /// ReID match threshold.
    var matchThreshold: Float { get }

    /// Find the best ReID match among detections.
    /// - Returns: Tuple of (best index or nil, similarity score).
    func findMatch(detections: [Detection]) -> (index: Int?, score: Float)

    /// Compare a single crop against the target embedding.
    /// - Returns: Cosine similarity score.
    func compare(crop: CGImage) -> Float
}

/// Face recognition callback for face matching.
///
/// Implemented by `FaceRecognizer` (or an adapter) and set on the pipeline
/// before calling ``SearchPipeline/start()``.
protocol PipelineFaceCallback: AnyObject {
    /// Whether a target face embedding has been set.
    var hasTarget: Bool { get }

    /// Face match threshold.
    var matchThreshold: Float { get }

    /// Find the best face match among detections.
    /// - Returns: Tuple of (best index or nil, similarity score).
    func findMatch(detections: [Detection]) -> (index: Int?, score: Float)

    /// Compare a single crop against the target face.
    /// - Returns: Face similarity score.
    func compare(crop: CGImage) -> Float
}

// MARK: - Pipeline Events

/// Events emitted by the pipeline for UI consumption.
///
/// The ``SearchViewModel`` observes these via an `AsyncStream` and
/// updates SwiftUI-bound properties accordingly.
enum PipelineEvent: Sendable {
    /// A frame has been processed with detection annotations.
    case frameProcessed(annotatedFrame: CGImage, fps: Float, personsDetected: Int)

    /// A match has been detected and an alert fired.
    case matchAlert(entry: MatchEntry)

    /// Progress toward multi-frame corroboration for a track.
    case confidenceProgress(trackId: Int, framesMatched: Int, framesNeeded: Int, avgScore: Float)

    /// The search has completed or paused for a match.
    case searchComplete(reason: String, alertLevel: String)
}

// MARK: - Reasoning Work Items

/// Work items for the async LLM reasoning queue.
///
/// `.analyze` compares a reference image against a candidate crop.
/// `.describe` matches a candidate crop against a text description.
/// Both are processed by the background reasoning worker.
enum ReasoningWorkItem: Sendable {
    /// Compare reference + candidate images via LLM vision.
    case analyze(trackKey: String, candidateCrop: CGImage, referenceImage: CGImage)

    /// Match candidate against text description via LLM.
    case describe(trackKey: String, candidateCrop: CGImage, description: String)
}

// MARK: - SearchPipeline

/// Main search pipeline orchestrator.
///
/// Port of Android `SearchPipeline.kt` (~794 lines) and Python's
/// `flightrisk/dashboard/pipeline.py` to Swift structured concurrency.
/// Runs detection, ReID, face matching, scoring, tracking, and async
/// LLM reasoning in a continuous frame loop.
///
/// ## Pipeline sequence per frame
///
/// 1. Check match-pause state (sleep 200ms if paused)
/// 2. Acquire frame from frame source
/// 3. Detect persons via YOLO / CoreML
/// 4. ReID match (cosine similarity against target embedding)
/// 5. Face match (face recognition against target face)
/// 6. Merge best match (prefer face if ReID found nothing, or if face > reid)
/// 7. Update IoU-based detection tracker
/// 8. Score via weighted MatchScorer + multi-frame corroboration
/// 9. Face-only promotion check
/// 10. Fire alert if match (skip suppressed, respect cooldown)
/// 11. Pause pipeline on match alert
/// 12. Enqueue LLM reasoning (rate-limited)
/// 13. Description-only matching path (when no target photo)
/// 14. Annotate frame, calculate FPS, emit event
///
/// ## Six critical behaviors
///
/// - **(a) Match-pause flow**: `matchPaused` flag halts frame processing.
/// - **(b) Track suppression**: dismissed tracks are excluded from future alerts.
/// - **(c) Face-only promotion**: strong face score promotes "no_match" to "possible_match".
/// - **(d) Description-only matching**: LLM-only path when no reference photo.
/// - **(e) LLM result back-fill**: reasoning worker updates match history entries.
/// - **(f) Two reasoning work item types**: `.analyze` and `.describe`.
actor SearchPipeline {

    private let logger = Logger(subsystem: "com.flightrisk", category: "SearchPipeline")

    // MARK: - Constants

    /// Minimum interval between LLM reasoning calls (seconds).
    private static let reasoningIntervalSec: TimeInterval = 5.0

    /// Frame loop delay to yield CPU (milliseconds).
    private static let frameDelayMs: UInt64 = 50

    /// Max match history entries to retain.
    private static let maxHistory = 50

    /// Per-track alert cooldown (seconds).
    private static let alertCooldownDefault: TimeInterval = 10.0

    // MARK: - Captured Config Values

    private let faceThreshold: Float
    private let reidThreshold: Float
    private let corroborationThreshold: Int
    private let alertCooldownSec: TimeInterval
    private let spatialGridSize: Int
    private let gemmaRateLimitSec: TimeInterval

    // MARK: - Dependencies

    private let llmSelector: LlmSelector
    nonisolated(unsafe) let alertManager: AlertManager
    private let locationProvider: LocationProvider

    // MARK: - Vision Pipeline Components

    private let tracker: DetectionTracker
    private let scorer: MatchScorer

    // MARK: - Callbacks (set by caller before start via actor-isolated setters)

    /// The active frame source. Set before calling ``start()``.
    var frameSource: FrameSource?

    /// Detection callback (person detector + annotator). Set by the vision layer.
    var detectionCallback: PipelineDetectionCallback?

    /// ReID callback (body re-identification). Set by the vision layer.
    var reidCallback: PipelineReidCallback?

    /// Face recognition callback. Set by the vision layer.
    var faceCallback: PipelineFaceCallback?

    // MARK: - Target

    /// Target reference photo (set before starting search).
    var targetPhoto: CGImage?

    /// Target text description (used when no photo is available).
    var targetDescription: String?

    // MARK: - Mutable State

    /// Match history (most recent last, capped at ``maxHistory``).
    private(set) var matchHistory: [MatchEntry] = []

    /// Per-track alerted timestamps (for cooldown).
    private var alertedTracks: [String: Date] = [:]

    /// Tracks dismissed via "Not My Child" -- suppressed from future alerts.
    private var suppressedTracks: Set<String> = []

    /// Per-track last LLM call timestamps (for rate limiting).
    private var llmLastCall: [String: Date] = [:]

    /// Whether the search is actively running.
    private(set) var isRunning = false

    /// Whether the pipeline is paused waiting for user to confirm/dismiss a match.
    /// Frame processing halts while paused -- the pipeline stays running but
    /// skips detection until ``resumeAfterMatch()`` is called.
    private(set) var matchPaused = false

    /// Frames-per-second (updated once per second).
    private(set) var fps: Float = 0

    /// Number of persons detected in the last frame.
    private(set) var personsDetected: Int = 0

    /// Total frames processed in the current search.
    private(set) var totalFrames: Int = 0

    /// Total detections in the current search.
    private(set) var totalDetections: Int = 0

    /// Total matches in the current search.
    private(set) var totalMatches: Int = 0

    // MARK: - Event Stream

    /// Observable event stream for the UI layer.
    nonisolated let events: AsyncStream<PipelineEvent>
    private let eventContinuation: AsyncStream<PipelineEvent>.Continuation

    // MARK: - Reasoning Channel

    private let reasoningStream: AsyncStream<ReasoningWorkItem>
    private let reasoningContinuation: AsyncStream<ReasoningWorkItem>.Continuation

    // MARK: - Tasks

    private var frameLoopTask: Task<Void, Never>?
    private var reasoningTask: Task<Void, Never>?

    // MARK: - Init

    /// Creates a search pipeline.
    ///
    /// - Parameters:
    ///   - config: App configuration.
    ///   - llmSelector: LLM backend selector for async reasoning.
    ///   - alertManager: Alert manager for audio/haptic/visual alerts.
    ///   - locationProvider: GPS provider for match tagging.
    init(
        config: FlightRiskConfig,
        llmSelector: LlmSelector,
        alertManager: AlertManager,
        locationProvider: LocationProvider
    ) {
        // Capture config values to avoid storing non-Sendable config reference
        self.faceThreshold = config.vision.faceMatchThreshold
        self.reidThreshold = config.vision.reidThreshold
        self.corroborationThreshold = config.reasoning.corroborationThreshold
        self.alertCooldownSec = config.reasoning.alertCooldown
        self.spatialGridSize = config.reasoning.spatialGridSize
        self.gemmaRateLimitSec = config.reasoning.gemmaRateLimit

        self.llmSelector = llmSelector
        self.alertManager = alertManager
        self.locationProvider = locationProvider

        self.tracker = DetectionTracker(
            iouThreshold: config.vision.trackerIouThreshold,
            maxMissing: config.vision.trackerMaxMissing,
            scoreWindow: config.vision.trackerScoreWindow
        )

        self.scorer = MatchScorer(
            reidWeight: config.vision.scorerReidWeight,
            faceWeight: config.vision.scorerFaceWeight,
            reasoningWeight: config.vision.scorerReasoningWeight,
            matchThreshold: config.vision.scorerMatchThreshold
        )

        // Create event stream (buffered for UI consumption)
        let (eStream, eContinuation) = AsyncStream<PipelineEvent>.makeStream(
            bufferingPolicy: .bufferingNewest(64)
        )
        self.events = eStream
        self.eventContinuation = eContinuation

        // Create reasoning channel (bounded queue matching Android Channel capacity)
        let (rStream, rContinuation) = AsyncStream<ReasoningWorkItem>.makeStream(
            bufferingPolicy: .bufferingNewest(config.reasoning.queueMaxSize)
        )
        self.reasoningStream = rStream
        self.reasoningContinuation = rContinuation
    }

    // MARK: - Lifecycle

    /// Start the search pipeline.
    ///
    /// Launches the frame processing loop and the async LLM reasoning
    /// worker. Location updates are started for GPS tagging.
    func start() {
        guard !isRunning else {
            logger.warning("Pipeline already running")
            return
        }

        isRunning = true
        totalFrames = 0
        totalDetections = 0
        totalMatches = 0

        // Start location updates for match tagging
        Task { await locationProvider.startUpdates() }

        // Launch the async LLM reasoning worker
        reasoningTask = Task { [weak self] in
            await self?.reasoningWorker()
        }

        // Launch the main frame processing loop
        frameLoopTask = Task { [weak self] in
            await self?.frameLoop()
        }

        logger.info("Pipeline started")
    }

    /// Stop the search pipeline.
    ///
    /// Cancels tasks, stops location updates, and emits a search-complete event.
    ///
    /// - Parameter reason: Why the pipeline was stopped.
    func stop(reason: String = "user_stopped") {
        guard isRunning else { return }
        isRunning = false

        frameLoopTask?.cancel()
        frameLoopTask = nil

        reasoningTask?.cancel()
        reasoningTask = nil
        reasoningContinuation.finish()

        tracker.clear()
        suppressedTracks.removeAll()

        Task { await locationProvider.stopUpdates() }

        eventContinuation.yield(
            .searchComplete(reason: reason, alertLevel: AlertManager.noMatch)
        )

        logger.info("Pipeline stopped: reason=\(reason)")
    }

    // MARK: - Match Pause Flow (Behavior A)

    /// Pause the pipeline for match inspection.
    ///
    /// Frame processing halts until ``resumeAfterMatch()`` is called.
    /// The pipeline stays running but skips detection.
    func pauseForMatch() {
        matchPaused = true
        logger.info("Pipeline paused for match inspection")
    }

    /// Resume the pipeline after match inspection.
    func resumeAfterMatch() {
        matchPaused = false
        logger.info("Pipeline resumed after match inspection")
    }

    // MARK: - Track Suppression (Behavior B)

    /// Suppress a track from future alerts (called when user taps "Not My Child").
    ///
    /// - Parameter trackKey: The spatial grid key (e.g. "11_7").
    func suppressTrack(trackKey: String) {
        suppressedTracks.insert(trackKey)
        logger.info("Track suppressed: \(trackKey)")
    }

    // MARK: - Frame Processing Loop

    /// Main frame processing loop.
    ///
    /// Mirrors `_frame_loop()` from `pipeline.py` and Android's `frameLoop()`.
    /// Runs continuously, yielding via `Task.sleep` between frames.
    private func frameLoop() async {
        var frameCount = 0
        var fpsStart = ContinuousClock.now
        var lastReasoningTime = Date.distantPast

        while isRunning && !Task.isCancelled {

            // -------------------------------------------------------
            // Step 1: Check match-pause state (Behavior A)
            // -------------------------------------------------------
            if matchPaused {
                try? await Task.sleep(for: .milliseconds(200))
                continue
            }

            do {
                // ---------------------------------------------------
                // Step 2: Acquire frame
                // ---------------------------------------------------
                guard let frame = frameSource?.getLatestFrame() else {
                    try? await Task.sleep(for: .milliseconds(10))
                    continue
                }

                guard let detector = detectionCallback else {
                    try? await Task.sleep(for: .milliseconds(10))
                    continue
                }

                // ---------------------------------------------------
                // Step 3: Detect persons
                // ---------------------------------------------------
                let detections = detector.detect(frame: frame)
                personsDetected = detections.count
                totalDetections += detections.count

                // ---------------------------------------------------
                // Step 4: ReID match
                // ---------------------------------------------------
                var matchIdx: Int? = nil
                var reidScore: Float = 0
                var faceScore: Float = 0

                let reid = reidCallback
                if reid != nil && targetPhoto != nil && !detections.isEmpty {
                    let result = reid!.findMatch(detections: detections)
                    if let idx = result.index {
                        matchIdx = idx
                        reidScore = result.score
                    }
                }

                // ---------------------------------------------------
                // Step 5: Face match
                // ---------------------------------------------------
                var faceMatchIdx: Int? = nil
                let face = faceCallback
                if let face = face, face.hasTarget, !detections.isEmpty {
                    let result = face.findMatch(detections: detections)
                    if let idx = result.index {
                        faceMatchIdx = idx
                        faceScore = result.score
                    }
                }

                // ---------------------------------------------------
                // Step 6: Merge best match
                // Prefer face if ReID found nothing, or if face > reid
                // ---------------------------------------------------
                if matchIdx == nil && faceMatchIdx != nil {
                    matchIdx = faceMatchIdx
                } else if matchIdx != nil && faceMatchIdx != nil && faceScore > reidScore {
                    matchIdx = faceMatchIdx
                }

                // ---------------------------------------------------
                // Step 7: Update tracker with all detections
                // ---------------------------------------------------
                let trackedDetections = tracker.update(detections: detections)

                // ---------------------------------------------------
                // Step 8: Score + corroborate
                // ---------------------------------------------------
                var matchScore: Float = 0
                var alertLevel = AlertManager.noMatch

                if let matchIdx = matchIdx {
                    let crop = detections[matchIdx].crop

                    // Get per-detection scores via compare()
                    let detReid = reid?.compare(crop: crop) ?? 0
                    let detFace = face?.compare(crop: crop) ?? 0

                    // Use MatchScorer for weighted combination with
                    // dynamic weight redistribution for missing signals
                    let scored = scorer.score(reidScore: detReid, faceScore: detFace)
                    matchScore = scored.combinedScore
                    alertLevel = scorer.alertLevel(scored)

                    // -------------------------------------------
                    // Step 9: Face-only promotion (Behavior C)
                    // If face is strong but combined score is weak,
                    // promote to possible_match.
                    // -------------------------------------------
                    if alertLevel == AlertManager.noMatch && detFace >= faceThreshold {
                        alertLevel = AlertManager.possibleMatch
                        matchScore = max(matchScore, detFace)
                    }

                    // Find the tracked detection with highest IoU to our match
                    let matchedBbox = detections[matchIdx].bbox
                    let trackedMatch = trackedDetections
                        .map { td in (td, DetectionTracker.computeIou(box1: td.bbox, box2: matchedBbox)) }
                        .filter { $0.1 > 0 }
                        .max(by: { $0.1 < $1.1 })
                        .map { $0.0 }

                    let trackId = trackedMatch?.trackId ?? computeTrackId(bbox: matchedBbox)

                    // Add scores to the tracker's rolling window
                    tracker.addScores(trackId: trackId, reidScore: detReid, faceScore: detFace)

                    // Multi-frame corroboration via tracker history
                    let trackSummary = tracker.getTrack(trackId)
                    let framesMatched = trackSummary?.reidScores.count ?? 0
                    let avgReid = trackSummary?.avgReidScore ?? 0

                    eventContinuation.yield(
                        .confidenceProgress(
                            trackId: trackId,
                            framesMatched: framesMatched,
                            framesNeeded: corroborationThreshold,
                            avgScore: avgReid
                        )
                    )

                    // Upgrade to confirmed if corroborated across enough frames
                    if framesMatched >= corroborationThreshold
                        && avgReid >= reidThreshold
                        && alertLevel != AlertManager.confirmedMatch
                    {
                        alertLevel = AlertManager.confirmedMatch
                        matchScore = max(matchScore, avgReid)
                    }

                    // -------------------------------------------
                    // Step 10: Fire alert if match detected
                    // Skip suppressed tracks, respect cooldown.
                    // -------------------------------------------
                    if alertLevel == AlertManager.confirmedMatch
                        || alertLevel == AlertManager.possibleMatch
                    {
                        let trackKey = computeTrackKey(bbox: matchedBbox)

                        if suppressedTracks.contains(trackKey) {
                            // User already dismissed this track -- skip silently
                        } else {
                            let now = Date()
                            let lastAlert = alertedTracks[trackKey]
                            let cooldown = alertCooldownSec

                            let shouldAlert: Bool
                            if let lastAlert = lastAlert {
                                shouldAlert = now.timeIntervalSince(lastAlert) >= cooldown
                            } else {
                                shouldAlert = true
                            }

                            if shouldAlert {
                                alertedTracks[trackKey] = now

                                // Fire alert on MainActor
                                let am = alertManager
                                let level = alertLevel
                                Task { @MainActor in
                                    am.fireAlert(level: level, trackKey: trackKey)
                                }

                                // Get GPS location
                                let location = await locationProvider.getCurrentLocation()

                                let llmAvailable = await llmSelector.isLlmAvailable
                                let matchType = (faceScore > reidScore && faceMatchIdx != nil) ? "face" : "reid"
                                let timeStr = Self.currentTimeString()

                                let entry = MatchEntry(
                                    time: timeStr,
                                    score: matchScore,
                                    reidScore: reidScore,
                                    faceScore: faceScore,
                                    alertLevel: alertLevel,
                                    trackId: trackKey,
                                    snapshot: crop,
                                    gemmaMatch: nil,
                                    gemmaConfidence: llmAvailable ? "pending" : nil,
                                    reasoning: llmAvailable ? "Awaiting LLM reasoning..." : nil,
                                    matchType: matchType,
                                    latitude: location?.coordinate.latitude,
                                    longitude: location?.coordinate.longitude,
                                    locationAccuracy: location.map { Float($0.horizontalAccuracy) }
                                )

                                matchHistory.append(entry)
                                while matchHistory.count > Self.maxHistory {
                                    matchHistory.removeFirst()
                                }

                                eventContinuation.yield(.matchAlert(entry: entry))
                                totalMatches += 1

                                // -----------------------------------
                                // Step 11: Pause pipeline on match alert (Behavior A)
                                // -----------------------------------
                                pauseForMatch()

                                eventContinuation.yield(
                                    .searchComplete(reason: "match_found", alertLevel: alertLevel)
                                )

                                // -----------------------------------
                                // Step 12: Enqueue LLM reasoning (Behavior E, F)
                                // -----------------------------------
                                if llmAvailable {
                                    let lastLlm = llmLastCall[trackKey]
                                    let llmCooldownOk: Bool
                                    if let lastLlm = lastLlm {
                                        llmCooldownOk = now.timeIntervalSince(lastLlm) >= gemmaRateLimitSec
                                    } else {
                                        llmCooldownOk = true
                                    }

                                    if llmCooldownOk {
                                        llmLastCall[trackKey] = now

                                        if let targetPhoto = targetPhoto {
                                            reasoningContinuation.yield(
                                                .analyze(
                                                    trackKey: trackKey,
                                                    candidateCrop: crop,
                                                    referenceImage: targetPhoto
                                                )
                                            )
                                        } else if targetDescription != nil {
                                            reasoningContinuation.yield(
                                                .describe(
                                                    trackKey: trackKey,
                                                    candidateCrop: crop,
                                                    description: targetDescription!
                                                )
                                            )
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // ---------------------------------------------------
                // Step 13: Description-only matching (Behavior D)
                // When no target photo but description is set and LLM
                // is available, send largest detection for LLM matching.
                // Rate-limited to one per REASONING_INTERVAL.
                // ---------------------------------------------------
                if matchIdx == nil
                    && targetPhoto == nil
                    && targetDescription != nil
                    && !detections.isEmpty
                {
                    let llmAvailable = await llmSelector.isLlmAvailable
                    if llmAvailable {
                        let now = Date()
                        if now.timeIntervalSince(lastReasoningTime) > Self.reasoningIntervalSec {
                            // Pick the largest detection (most likely to be useful)
                            let bestIdx = detections.indices.max(by: { i, j in
                                let bi = detections[i].bbox
                                let bj = detections[j].bbox
                                let areaI = (bi[2] - bi[0]) * (bi[3] - bi[1])
                                let areaJ = (bj[2] - bj[0]) * (bj[3] - bj[1])
                                return areaI < areaJ
                            })
                            if let bestIdx = bestIdx {
                                lastReasoningTime = now
                                let crop = detections[bestIdx].crop
                                let trackKey = computeTrackKey(bbox: detections[bestIdx].bbox)
                                reasoningContinuation.yield(
                                    .describe(
                                        trackKey: trackKey,
                                        candidateCrop: crop,
                                        description: targetDescription!
                                    )
                                )
                            }
                        }
                    }
                }

                // ---------------------------------------------------
                // Step 14: Annotate frame, calculate FPS, emit event
                // ---------------------------------------------------
                let annotated = detector.annotate(
                    frame: frame,
                    detections: detections,
                    matchIdx: matchIdx
                )
                totalFrames += 1

                // FPS calculation (update once per second)
                frameCount += 1
                let elapsed = ContinuousClock.now - fpsStart
                if elapsed >= .seconds(1) {
                    let elapsedSec = Double(elapsed.components.seconds)
                        + Double(elapsed.components.attoseconds) / 1e18
                    fps = Float(frameCount) / max(Float(elapsedSec), 0.001)
                    frameCount = 0
                    fpsStart = ContinuousClock.now
                }

                // Emit processed frame
                if let annotated = annotated {
                    eventContinuation.yield(
                        .frameProcessed(
                            annotatedFrame: annotated,
                            fps: fps,
                            personsDetected: detections.count
                        )
                    )
                }

            } catch {
                logger.error("Frame loop error: \(error.localizedDescription)")
            }

            // Frame delay
            try? await Task.sleep(for: .milliseconds(Self.frameDelayMs))
        }
    }

    // MARK: - Async LLM Reasoning Worker (Behaviors E, F)

    /// Background worker that drains the LLM reasoning queue.
    ///
    /// Mirrors `_gemma_worker()` from `alerts.py` and Android's
    /// `reasoningWorker()`. Processes `.analyze` and `.describe` work items.
    private func reasoningWorker() async {
        for await item in reasoningStream {
            guard isRunning, !Task.isCancelled else { break }

            do {
                let backend = await llmSelector.getActiveBackend()

                switch item {
                case .analyze(let trackKey, let candidateCrop, let referenceImage):
                    // Call LLM to compare reference vs candidate
                    let result = await backend.analyzeMatch(
                        referenceImage: referenceImage,
                        candidateImage: candidateCrop,
                        description: nil
                    )

                    // Back-fill the match history entry for this track (Behavior E)
                    if let idx = matchHistory.lastIndex(where: { $0.trackId == trackKey }) {
                        matchHistory[idx].gemmaMatch = result.isMatch
                        matchHistory[idx].gemmaConfidence = result.confidence
                        matchHistory[idx].reasoning = result.reasoning
                    }

                    // If reasoning confirms with high/medium confidence, fire
                    // an upgraded alert
                    if result.isMatch && ["high", "medium"].contains(result.confidence) {
                        let newLevel = result.confidence == "high"
                            ? AlertManager.confirmedMatch
                            : AlertManager.possibleMatch

                        let am = alertManager
                        Task { @MainActor in
                            am.fireAlert(level: newLevel, trackKey: trackKey)
                        }
                    }

                case .describe(let trackKey, let candidateCrop, let description):
                    // Call LLM to match candidate against text description
                    let result = await backend.describeMatch(
                        candidateImage: candidateCrop,
                        description: description
                    )

                    // If description match confirmed, fire alert and create entry
                    if result.isMatch {
                        let alertLevel = result.confidence == "high"
                            ? AlertManager.confirmedMatch
                            : AlertManager.possibleMatch

                        let location = await locationProvider.getCurrentLocation()
                        let timeStr = Self.currentTimeString()

                        let entry = MatchEntry(
                            time: timeStr,
                            score: 0.5,
                            reidScore: 0,
                            faceScore: 0,
                            alertLevel: alertLevel,
                            trackId: trackKey,
                            snapshot: candidateCrop,
                            gemmaMatch: true,
                            gemmaConfidence: result.confidence,
                            reasoning: result.reasoning,
                            matchType: "description",
                            latitude: location?.coordinate.latitude,
                            longitude: location?.coordinate.longitude,
                            locationAccuracy: location.map { Float($0.horizontalAccuracy) }
                        )

                        matchHistory.append(entry)
                        while matchHistory.count > Self.maxHistory {
                            matchHistory.removeFirst()
                        }

                        let am = alertManager
                        Task { @MainActor in
                            am.fireAlert(level: alertLevel, trackKey: trackKey)
                        }
                        eventContinuation.yield(.matchAlert(entry: entry))
                    }
                }
            } catch {
                logger.error("Reasoning worker error: \(error.localizedDescription)")
            }
        }
    }

    // MARK: - Helpers

    /// Compute a spatial grid key from a bounding box.
    ///
    /// Mirrors `_compute_track_key()` from Python `alerts.py`. Rounds
    /// the bbox center to a grid cell for consistent tracking.
    ///
    /// - Parameter bbox: `[x1, y1, x2, y2]` pixel coordinates.
    /// - Returns: Grid key string, e.g. "11_7".
    private func computeTrackKey(bbox: [Int]) -> String {
        let gridSize = spatialGridSize
        let cx = ((bbox[0] + bbox[2]) / 2) / gridSize
        let cy = ((bbox[1] + bbox[3]) / 2) / gridSize
        return "\(cx)_\(cy)"
    }

    /// Compute a numeric track ID from a bounding box
    /// (for corroboration tracking when no tracker match found).
    ///
    /// - Parameter bbox: `[x1, y1, x2, y2]` pixel coordinates.
    /// - Returns: Numeric track ID derived from grid position.
    private func computeTrackId(bbox: [Int]) -> Int {
        let gridSize = spatialGridSize
        let cx = ((bbox[0] + bbox[2]) / 2) / gridSize
        let cy = ((bbox[1] + bbox[3]) / 2) / gridSize
        return cx * 10000 + cy
    }

    /// Current time as "HH:mm:ss" string.
    private static func currentTimeString() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter.string(from: Date())
    }
}
