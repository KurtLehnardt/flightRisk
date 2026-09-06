package com.flightrisk.app.pipeline

import android.graphics.Bitmap
import android.util.Log
import com.flightrisk.app.alert.AlertManager
import com.flightrisk.app.config.FlightRiskConfig
import com.flightrisk.app.llm.LlmBackend
import com.flightrisk.app.llm.LlmSelector
import com.flightrisk.app.location.LocationProvider
import com.flightrisk.app.vision.Detection
import com.flightrisk.app.vision.DetectionTracker
import com.flightrisk.app.vision.MatchScorer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Main search pipeline orchestrator.
 *
 * Ports the per-frame processing loop from Python's
 * `flightrisk/dashboard/pipeline.py` to Android coroutines. The
 * pipeline runs on [Dispatchers.Default] and emits events via a
 * [SharedFlow] that the UI layer observes.
 *
 * ## Pipeline sequence per frame
 *
 * 1. Acquire frame (from camera/drone feed)
 * 2. Detect persons (via YOLO / ONNX)
 * 3. ReID match (cosine similarity against target embedding)
 * 4. Face match (face recognition against target face)
 * 5. Score (multi-feature weighted combination)
 * 6. Track (multi-frame corroboration via IoU tracker)
 * 7. (async) LLM reasoning (rate-limited, non-blocking)
 *
 * ## Key behaviors ported from Python
 *
 * - **Alert throttling**: per-track cooldown (10s) prevents repeated
 *   alerts for the same spatial region.
 * - **Multi-frame corroboration**: 3+ frames with avg ReID score
 *   above threshold upgrades to `confirmed_match`.
 * - **LLM rate limiting**: at most one LLM call per 5 seconds
 *   ([REASONING_INTERVAL]).
 * - **Face-only promotion**: if face score exceeds threshold but
 *   combined score is below, promote to `possible_match`.
 * - **Battery critical auto-land**: if drone is connected and battery
 *   drops below critical threshold.
 *
 * @param config App configuration.
 * @param llmSelector LLM backend selector for async reasoning.
 * @param alertManager Alert manager for audio/haptic/visual alerts.
 * @param locationProvider GPS provider for match tagging.
 */
class SearchPipeline(
    private val config: FlightRiskConfig,
    private val llmSelector: LlmSelector,
    private val alertManager: AlertManager,
    private val locationProvider: LocationProvider,
) {

    companion object {
        private const val TAG = "SearchPipeline"

        /** Minimum interval between LLM reasoning calls (seconds). */
        private const val REASONING_INTERVAL = 5L

        /** Frame loop delay to yield CPU (milliseconds). */
        private const val FRAME_DELAY_MS = 50L

        /** Max match history entries to retain. */
        private const val MAX_HISTORY = 50
    }

    // ------------------------------------------------------------------
    // Events emitted to the UI layer
    // ------------------------------------------------------------------

    /** Events emitted by the pipeline for UI consumption. */
    sealed class PipelineEvent {
        /**
         * A frame has been processed.
         *
         * @property annotatedFrame The frame with detection annotations.
         * @property fps Current frames-per-second.
         * @property personsDetected Number of persons detected in this frame.
         */
        data class FrameProcessed(
            val annotatedFrame: Bitmap,
            val fps: Float,
            val personsDetected: Int,
        ) : PipelineEvent()

        /**
         * A match has been detected and an alert fired.
         *
         * @property matchEntry Full details of the match event.
         */
        data class MatchAlert(
            val matchEntry: MatchEntry,
        ) : PipelineEvent()

        /**
         * Progress toward multi-frame corroboration for a track.
         *
         * @property trackId The tracker ID for this detection.
         * @property framesMatched Number of frames this track has matched.
         * @property framesNeeded Threshold for corroboration (default 3).
         * @property avgScore Average ReID score across matched frames.
         */
        data class ConfidenceProgress(
            val trackId: Int,
            val framesMatched: Int,
            val framesNeeded: Int,
            val avgScore: Float,
        ) : PipelineEvent()

        /**
         * The search has completed (match found or battery critical).
         *
         * @property reason Why the search ended: "match_found",
         *   "battery_critical", "user_stopped".
         * @property alertLevel The alert level at time of completion.
         */
        data class SearchComplete(
            val reason: String,
            val alertLevel: String,
        ) : PipelineEvent()
    }

    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------

    private val _events = MutableSharedFlow<PipelineEvent>(extraBufferCapacity = 64)

    /** Observable event stream for the UI layer. */
    val events: SharedFlow<PipelineEvent> = _events.asSharedFlow()

    private var pipelineScope: CoroutineScope? = null
    private var pipelineJob: Job? = null
    private val running = AtomicBoolean(false)

    /** Target reference photo (set before starting search). */
    var targetPhoto: Bitmap? = null

    /** Target text description (used when no photo is available). */
    var targetDescription: String? = null

    /** Match history (most recent last, capped at [MAX_HISTORY]). */
    val matchHistory: MutableList<MatchEntry> = mutableListOf()
    private val matchHistoryLock = Any()

    /** Per-track alerted timestamps (for cooldown). */
    private val alertedTracks = ConcurrentHashMap<String, Long>()

    /** Tracks dismissed via "Not My Child" — suppressed from future alerts. */
    private val suppressedTracks = ConcurrentHashMap.newKeySet<String>()

    /** Per-track last LLM call timestamps (for rate limiting). */
    private val llmLastCall = ConcurrentHashMap<String, Long>()

    /** IoU-based detection tracker for stable multi-frame corroboration. */
    private val tracker = DetectionTracker(
        iouThreshold = config.vision.trackerIouThreshold.toFloat(),
        maxMissing = config.vision.trackerMaxMissing,
        scoreWindow = config.vision.trackerScoreWindow,
    )

    /** Multi-signal weighted scorer. */
    private val scorer = MatchScorer(
        reidWeight = config.vision.scorerReidWeight.toFloat(),
        faceWeight = config.vision.scorerFaceWeight.toFloat(),
        reasoningWeight = config.vision.scorerReasoningWeight.toFloat(),
        matchThreshold = config.vision.scorerMatchThreshold.toFloat(),
    )

    /** Async LLM reasoning queue. */
    private val reasoningChannel = Channel<ReasoningWorkItem>(capacity = config.reasoning.queueMaxSize)

    /** Whether the search is actively running. */
    val isRunning: Boolean get() = running.get()

    /**
     * Suppress a track from future alerts (called when user taps "Not My Child").
     * The track key is the spatial grid key (e.g. "11_7").
     */
    fun suppressTrack(trackKey: String) {
        suppressedTracks.add(trackKey)
        Log.i(TAG, "Track suppressed: $trackKey")
    }

    /**
     * Whether the pipeline is paused waiting for user to confirm/dismiss a match.
     * Frame processing halts while paused — the pipeline stays running but
     * skips detection until [resumeAfterMatch] is called.
     */
    @Volatile
    var matchPaused: Boolean = false
        private set

    fun pauseForMatch() {
        matchPaused = true
        Log.i(TAG, "Pipeline paused for match inspection")
    }

    fun resumeAfterMatch() {
        matchPaused = false
        Log.i(TAG, "Pipeline resumed after match inspection")
    }

    /** Frames-per-second (updated once per second). */
    @Volatile
    var fps: Float = 0f
        private set

    /** Number of persons detected in the last frame. */
    @Volatile
    var personsDetected: Int = 0
        private set

    // ------------------------------------------------------------------
    // Frame source callback (set by camera/drone layer)
    // ------------------------------------------------------------------

    /**
     * Callback interface for frame acquisition.
     *
     * The camera or drone layer implements this to supply frames
     * to the pipeline.
     */
    fun interface FrameSource {
        /**
         * Get the next frame from the source.
         * @return The frame bitmap, or null if no frame is available.
         */
        fun getFrame(): Bitmap?
    }

    /** The active frame source. Set this before calling [start]. */
    var frameSource: FrameSource? = null

    // ------------------------------------------------------------------
    // Detection / matching callbacks (set by vision layer)
    // ------------------------------------------------------------------

    /**
     * Callback for person detection results.
     *
     * The vision layer (ONNX/YOLO) implements this to provide
     * detection results and annotated frames.
     */
    interface DetectionCallback {
        /**
         * Detect persons in a frame.
         * @return List of detections, each with bbox, crop, and scores.
         */
        fun detect(frame: Bitmap): List<Detection>

        /**
         * Annotate a frame with detection bounding boxes.
         */
        fun annotate(frame: Bitmap, detections: List<Detection>, matchIdx: Int?): Bitmap
    }

    /**
     * Callback for ReID matching against the target.
     */
    interface ReidCallback {
        /** ReID match threshold. */
        val matchThreshold: Float

        /**
         * Find the best ReID match among detections.
         * @return Pair of (best index, similarity score) or null.
         */
        fun findMatch(detections: List<Detection>): Pair<Int, Float>?

        /**
         * Compare a single crop against the target embedding.
         * @return Cosine similarity score.
         */
        fun compare(crop: Bitmap): Float
    }

    /**
     * Callback for face recognition matching.
     */
    interface FaceCallback {
        /** Whether a target face embedding has been set. */
        val hasTarget: Boolean

        /** Face match threshold. */
        val matchThreshold: Float

        /**
         * Find the best face match among detections.
         * @return Pair of (best index, similarity score) or null.
         */
        fun findMatch(detections: List<Detection>): Pair<Int, Float>?

        /**
         * Compare a single crop against the target face.
         * @return Face similarity score.
         */
        fun compare(crop: Bitmap): Float
    }

    /** Set by the vision layer (T05). */
    var detectionCallback: DetectionCallback? = null
    var reidCallback: ReidCallback? = null
    var faceCallback: FaceCallback? = null

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    /**
     * Start the search pipeline.
     *
     * Launches the frame processing loop and the async LLM reasoning
     * worker on [Dispatchers.Default]. Location updates are started
     * for GPS tagging.
     */
    fun start() {
        if (!running.compareAndSet(false, true)) {
            Log.w(TAG, "Pipeline already running")
            return
        }

        val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())
        pipelineScope = scope

        // Start location updates for match tagging
        locationProvider.startUpdates()

        // Launch the async LLM reasoning worker
        scope.launch { reasoningWorker() }

        // Launch the main frame processing loop
        pipelineJob = scope.launch { frameLoop() }

        Log.i(TAG, "Search pipeline started")
    }

    /**
     * Stop the search pipeline.
     *
     * Cancels coroutines, stops location updates, and dismisses
     * all alerts.
     *
     * @param reason Why the pipeline was stopped.
     */
    fun stop(reason: String = "user_stopped") {
        if (!running.compareAndSet(true, false)) return

        pipelineScope?.cancel()
        pipelineScope = null
        pipelineJob = null

        tracker.clear()
        suppressedTracks.clear()
        locationProvider.stopUpdates()

        _events.tryEmit(
            PipelineEvent.SearchComplete(reason = reason, alertLevel = "no_match")
        )

        Log.i(TAG, "Search pipeline stopped: $reason")
    }

    // ------------------------------------------------------------------
    // Frame processing loop
    // ------------------------------------------------------------------

    /**
     * Main frame processing loop.
     *
     * Mirrors `_frame_loop()` from `pipeline.py`. Runs on
     * [Dispatchers.Default] and yields via [delay] between frames.
     */
    private suspend fun frameLoop() {
        var frameCount = 0
        var fpsStart = System.currentTimeMillis()
        var lastReasoningTime = 0L

        val faceThreshold = config.vision.faceMatchThreshold.toFloat()
        val corroborationThreshold = config.reasoning.corroborationThreshold

        val scope = pipelineScope ?: return

        while (running.get() && scope.isActive) {
            // Wait while paused for match inspection
            if (matchPaused) {
                delay(200)
                continue
            }

            try {
                // 1. Acquire frame
                val frame = frameSource?.getFrame()
                if (frame == null) {
                    delay(10)
                    continue
                }

                val detector = detectionCallback
                if (detector == null) {
                    delay(10)
                    continue
                }

                // 2. Detect persons
                val detections = detector.detect(frame)
                personsDetected = detections.size

                // 3. ReID match
                var matchIdx: Int? = null
                var reidScore = 0f
                var faceScore = 0f

                val reid = reidCallback
                if (reid != null && targetPhoto != null && detections.isNotEmpty()) {
                    val result = reid.findMatch(detections)
                    if (result != null) {
                        matchIdx = result.first
                        reidScore = result.second
                    }
                }

                // 4. Face match
                var faceMatchIdx: Int? = null
                val face = faceCallback
                if (face != null && face.hasTarget && detections.isNotEmpty()) {
                    val result = face.findMatch(detections)
                    if (result != null) {
                        faceMatchIdx = result.first
                        faceScore = result.second
                    }
                }

                // Merge: prefer face if ReID didn't find anything
                if (matchIdx == null && faceMatchIdx != null) {
                    matchIdx = faceMatchIdx
                } else if (matchIdx != null && faceMatchIdx != null && faceScore > reidScore) {
                    matchIdx = faceMatchIdx
                }

                // 5. Update tracker with all detections (IoU-based stable IDs)
                val trackedDetections = tracker.update(detections)

                // 6. Score + corroborate
                var matchScore = 0f
                var alertLevel = AlertManager.NO_MATCH

                if (matchIdx != null) {
                    val crop = detections[matchIdx].crop
                    val detReid = reid?.compare(crop) ?: 0f
                    val detFace = face?.compare(crop) ?: 0f

                    // Use MatchScorer for proper weighted combination with
                    // dynamic weight redistribution for missing signals
                    val scored = scorer.score(reidScore = detReid, faceScore = detFace)
                    matchScore = scored.combinedScore
                    alertLevel = scorer.alertLevel(scored)

                    // Face-only promotion: if face is strong but combined is weak
                    if (alertLevel == AlertManager.NO_MATCH && detFace >= faceThreshold) {
                        alertLevel = AlertManager.POSSIBLE_MATCH
                        matchScore = maxOf(matchScore, detFace)
                    }

                    // Find the tracked detection with highest IoU to our match
                    val matchedBbox = detections[matchIdx].bbox
                    val trackedMatch = trackedDetections
                        .map { td -> td to DetectionTracker.computeIou(td.bbox, matchedBbox) }
                        .filter { it.second > 0f }
                        .maxByOrNull { it.second }
                        ?.first

                    val trackId = trackedMatch?.trackId ?: computeTrackId(matchedBbox)

                    // Add scores to the tracker's rolling window
                    tracker.addScores(trackId, reidScore = detReid, faceScore = detFace)

                    // Multi-frame corroboration via tracker history
                    val trackSummary = tracker.getTrack(trackId)
                    val framesMatched = trackSummary?.reidScores?.size ?: 0
                    val avgReid = trackSummary?.avgReidScore ?: 0f

                    _events.tryEmit(
                        PipelineEvent.ConfidenceProgress(
                            trackId = trackId,
                            framesMatched = framesMatched,
                            framesNeeded = corroborationThreshold,
                            avgScore = avgReid,
                        )
                    )

                    // Upgrade to confirmed if corroborated across enough frames
                    if (framesMatched >= corroborationThreshold
                        && avgReid >= config.vision.reidThreshold.toFloat()
                        && alertLevel != AlertManager.CONFIRMED_MATCH
                    ) {
                        alertLevel = AlertManager.CONFIRMED_MATCH
                        matchScore = maxOf(matchScore, avgReid)
                    }

                    // Fire alert if match detected (skip suppressed tracks)
                    if (alertLevel in listOf(AlertManager.CONFIRMED_MATCH, AlertManager.POSSIBLE_MATCH)) {
                        val trackKey = computeTrackKey(matchedBbox)

                        if (trackKey in suppressedTracks) {
                            // User already dismissed this track — skip silently
                        } else {
                        val now = System.currentTimeMillis()
                        val lastAlert = alertedTracks[trackKey]
                        val cooldownMs = (config.reasoning.alertCooldown * 1000).toLong()

                        if (lastAlert == null || (now - lastAlert) >= cooldownMs) {
                            alertedTracks[trackKey] = now

                            alertManager.fireAlert(alertLevel, trackKey)

                            val location = locationProvider.getCurrentLocation()

                            val matchType = if (faceScore > reidScore && faceMatchIdx != null) "face" else "reid"
                            val timeStr = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
                            val llmAvailable = llmSelector.isLlmAvailable

                            val entry = MatchEntry(
                                time = timeStr,
                                score = matchScore,
                                reidScore = reidScore,
                                faceScore = faceScore,
                                alertLevel = alertLevel,
                                trackId = trackKey,
                                snapshot = crop,
                                gemmaMatch = null,
                                gemmaConfidence = if (llmAvailable) "pending" else null,
                                reasoning = if (llmAvailable) "Awaiting LLM reasoning..." else null,
                                matchType = matchType,
                                latitude = location?.latitude,
                                longitude = location?.longitude,
                                locationAccuracy = location?.accuracy,
                            )

                            synchronized(matchHistoryLock) {
                                matchHistory.add(entry)
                                while (matchHistory.size > MAX_HISTORY) {
                                    matchHistory.removeAt(0)
                                }
                            }

                            _events.tryEmit(PipelineEvent.MatchAlert(entry))

                            // Pause pipeline until user confirms or dismisses
                            pauseForMatch()

                            _events.tryEmit(
                                PipelineEvent.SearchComplete(
                                    reason = "match_found",
                                    alertLevel = alertLevel,
                                )
                            )

                            val llmCooldownMs = (config.reasoning.gemmaRateLimit * 1000).toLong()
                            val lastLlm = llmLastCall[trackKey]
                            if (llmAvailable && (lastLlm == null || (now - lastLlm) >= llmCooldownMs)) {
                                llmLastCall[trackKey] = now
                                val item = if (targetPhoto != null) {
                                    ReasoningWorkItem.Analyze(trackKey, crop, targetPhoto!!)
                                } else if (targetDescription != null) {
                                    ReasoningWorkItem.Describe(trackKey, crop, targetDescription!!)
                                } else null

                                item?.let { reasoningChannel.trySend(it) }
                            }
                        }
                        } // end suppression else
                    }
                }

                // Description-only matching (no target photo)
                if (matchIdx == null
                    && targetDescription != null
                    && llmSelector.isLlmAvailable
                    && detections.isNotEmpty()
                ) {
                    val now = System.currentTimeMillis()
                    val intervalMs = REASONING_INTERVAL * 1000
                    if (now - lastReasoningTime > intervalMs) {
                        // Pick the largest detection (most likely to be useful)
                        val bestIdx = detections.indices.maxByOrNull { i ->
                            val b = detections[i].bbox
                            (b[2] - b[0]) * (b[3] - b[1])
                        }
                        if (bestIdx != null) {
                            lastReasoningTime = now
                            val crop = detections[bestIdx].crop
                            val trackKey = computeTrackKey(detections[bestIdx].bbox)
                            reasoningChannel.trySend(
                                ReasoningWorkItem.Describe(trackKey, crop, targetDescription!!)
                            )
                        }
                    }
                }

                // Annotate frame
                val annotated = detector.annotate(frame, detections, matchIdx)

                // FPS calculation
                frameCount++
                val elapsed = System.currentTimeMillis() - fpsStart
                if (elapsed >= 1000) {
                    fps = frameCount * 1000f / elapsed
                    frameCount = 0
                    fpsStart = System.currentTimeMillis()
                }

                // Emit processed frame
                _events.tryEmit(
                    PipelineEvent.FrameProcessed(
                        annotatedFrame = annotated,
                        fps = fps,
                        personsDetected = detections.size,
                    )
                )

            } catch (e: Exception) {
                Log.e(TAG, "Frame loop error", e)
            }

            delay(FRAME_DELAY_MS)
        }
    }

    // ------------------------------------------------------------------
    // Async LLM reasoning worker
    // ------------------------------------------------------------------

    /** Work items for the async reasoning queue. */
    private sealed class ReasoningWorkItem {
        data class Analyze(
            val trackKey: String,
            val candidateCrop: Bitmap,
            val referenceImage: Bitmap,
        ) : ReasoningWorkItem()

        data class Describe(
            val trackKey: String,
            val candidateCrop: Bitmap,
            val description: String,
        ) : ReasoningWorkItem()
    }

    /**
     * Background worker that drains the LLM reasoning queue.
     *
     * Mirrors `_gemma_worker()` from `alerts.py`. Runs as a coroutine
     * and processes items from [reasoningChannel].
     */
    private suspend fun reasoningWorker() {
        for (item in reasoningChannel) {
            if (!running.get()) break

            try {
                val backend: LlmBackend = llmSelector.getActiveBackend()

                when (item) {
                    is ReasoningWorkItem.Analyze -> {
                        val result = backend.analyzeMatch(
                            referenceImage = item.referenceImage,
                            candidateImage = item.candidateCrop,
                        )

                        // Back-fill the match history entry for this track
                        synchronized(matchHistoryLock) {
                            matchHistory.lastOrNull { it.trackId == item.trackKey }?.let { existing ->
                                val idx = matchHistory.indexOf(existing)
                                if (idx >= 0) {
                                    matchHistory[idx] = existing.copy(
                                        gemmaMatch = result.isMatch,
                                        gemmaConfidence = result.confidence,
                                        reasoning = result.reasoning,
                                    )
                                }
                            }
                        }

                        // If reasoning confirms with high/medium confidence,
                        // upgrade alert level
                        if (result.isMatch && result.confidence in listOf("high", "medium")) {
                            val newLevel = if (result.confidence == "high") {
                                AlertManager.CONFIRMED_MATCH
                            } else {
                                AlertManager.POSSIBLE_MATCH
                            }
                            alertManager.fireAlert(newLevel, item.trackKey)
                        }
                    }

                    is ReasoningWorkItem.Describe -> {
                        val result = backend.describeMatch(
                            candidateImage = item.candidateCrop,
                            description = item.description,
                        )

                        // If description match confirmed, fire alert
                        if (result.isMatch) {
                            val alertLevel = when (result.confidence) {
                                "high" -> AlertManager.CONFIRMED_MATCH
                                else -> AlertManager.POSSIBLE_MATCH
                            }

                            val location = locationProvider.getCurrentLocation()
                            val timeStr = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())

                            val entry = MatchEntry(
                                time = timeStr,
                                score = 0.5f,
                                reidScore = 0f,
                                faceScore = 0f,
                                alertLevel = alertLevel,
                                trackId = item.trackKey,
                                snapshot = item.candidateCrop,
                                gemmaMatch = true,
                                gemmaConfidence = result.confidence,
                                reasoning = result.reasoning,
                                matchType = "description",
                                latitude = location?.latitude,
                                longitude = location?.longitude,
                                locationAccuracy = location?.accuracy,
                            )

                            synchronized(matchHistoryLock) {
                                matchHistory.add(entry)
                                while (matchHistory.size > MAX_HISTORY) {
                                    matchHistory.removeAt(0)
                                }
                            }

                            alertManager.fireAlert(alertLevel, item.trackKey)
                            _events.tryEmit(PipelineEvent.MatchAlert(entry))
                        }
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Reasoning worker error", e)
            }
        }
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /**
     * Compute a spatial grid key from a bounding box.
     *
     * Mirrors `_compute_track_key()` from Python `alerts.py`. Rounds
     * the bbox center to a 50px grid cell for consistent tracking.
     */
    private fun computeTrackKey(bbox: IntArray): String {
        val gridSize = config.reasoning.spatialGridSize
        val cx = ((bbox[0] + bbox[2]) / 2) / gridSize
        val cy = ((bbox[1] + bbox[3]) / 2) / gridSize
        return "${cx}_${cy}"
    }

    /**
     * Compute a numeric track ID from a bounding box (for
     * corroboration tracking).
     */
    private fun computeTrackId(bbox: IntArray): Int {
        val gridSize = config.reasoning.spatialGridSize
        val cx = ((bbox[0] + bbox[2]) / 2) / gridSize
        val cy = ((bbox[1] + bbox[3]) / 2) / gridSize
        return cx * 10000 + cy
    }
}
