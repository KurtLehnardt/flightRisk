package com.flightrisk.app.vision

import android.graphics.Bitmap
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * IoU-based detection tracker for temporal vote accumulation.
 *
 * Port of `flightrisk/vision/tracker.py`. Matches detections across
 * frames by bounding box overlap (IoU), maintains a rolling window
 * of match scores per tracked person, and keeps the best crop
 * (highest detection confidence) for each track.
 *
 * @param iouThreshold Minimum IoU to match a detection to an existing track.
 * @param maxMissing Number of frames a track survives without a match before removal.
 * @param scoreWindow Number of recent scores to keep for averaging.
 */
class DetectionTracker(
    private val iouThreshold: Float = 0.3f,
    private val maxMissing: Int = 15,
    private val scoreWindow: Int = 8,
) {

    /**
     * Internal mutable track state.
     */
    private data class Track(
        val trackId: Int,
        var bbox: IntArray,
        var confidence: Float,
        var crop: Bitmap?,
        val reidScores: MutableList<Float>,
        val faceScores: MutableList<Float>,
        var framesSeen: Int,
        var age: Int, // frames since last match
        var bestCrop: Bitmap?,
        var bestCropConfidence: Float,
    ) {
        override fun equals(other: Any?): Boolean {
            if (this === other) return true
            if (other !is Track) return false
            return trackId == other.trackId
        }

        override fun hashCode(): Int = trackId
    }

    private val tracks = mutableMapOf<Int, Track>()
    private var nextId = 0
    private val lock = ReentrantLock()

    /**
     * Match new detections to existing tracks and return tracked detections.
     *
     * Uses greedy IoU matching: compute all pairwise IoU values between
     * existing tracks and new detections, then greedily assign from
     * highest IoU down. Unmatched detections become new tracks.
     * Tracks without a match age and are removed after [maxMissing] frames.
     *
     * @param detections List of [Detection] from [PersonDetector].
     * @return List of [TrackedDetection] with stable track IDs.
     */
    fun update(detections: List<Detection>): List<TrackedDetection> {
        lock.withLock {
            if (detections.isEmpty() && tracks.isEmpty()) {
                return emptyList()
            }

            val matchedTrackIds = mutableSetOf<Int>()
            val matchedDetIndices = mutableSetOf<Int>()

            // Build IoU matrix and do greedy matching
            if (tracks.isNotEmpty() && detections.isNotEmpty()) {
                val trackIds = tracks.keys.toList()

                // Compute all IoU pairs above threshold
                data class IouPair(val iou: Float, val detIdx: Int, val trackId: Int)
                val iouPairs = mutableListOf<IouPair>()

                for ((detIdx, det) in detections.withIndex()) {
                    for (tid in trackIds) {
                        val track = tracks[tid] ?: continue
                        val iou = computeIou(det.bbox, track.bbox)
                        if (iou >= iouThreshold) {
                            iouPairs.add(IouPair(iou, detIdx, tid))
                        }
                    }
                }

                // Sort by IoU descending for greedy matching
                iouPairs.sortByDescending { it.iou }

                for (pair in iouPairs) {
                    if (pair.detIdx in matchedDetIndices || pair.trackId in matchedTrackIds) {
                        continue
                    }
                    // Match this detection to this track
                    val det = detections[pair.detIdx]
                    val track = tracks[pair.trackId]!!
                    track.bbox = det.bbox.copyOf()
                    track.confidence = det.confidence
                    track.crop = det.crop
                    track.framesSeen++
                    track.age = 0

                    // Update best crop if this detection has higher confidence
                    if (det.confidence > track.bestCropConfidence) {
                        track.bestCrop = det.crop
                        track.bestCropConfidence = det.confidence
                    }

                    matchedTrackIds.add(pair.trackId)
                    matchedDetIndices.add(pair.detIdx)
                }
            }

            // Create new tracks for unmatched detections
            for ((detIdx, det) in detections.withIndex()) {
                if (detIdx in matchedDetIndices) continue
                val newTrack = Track(
                    trackId = nextId,
                    bbox = det.bbox.copyOf(),
                    confidence = det.confidence,
                    crop = det.crop,
                    reidScores = mutableListOf(),
                    faceScores = mutableListOf(),
                    framesSeen = 1,
                    age = 0,
                    bestCrop = det.crop,
                    bestCropConfidence = det.confidence,
                )
                tracks[nextId] = newTrack
                matchedTrackIds.add(nextId)
                nextId++
            }

            // Age unmatched tracks and remove expired ones
            val expired = mutableListOf<Int>()
            for ((tid, track) in tracks) {
                if (tid !in matchedTrackIds) {
                    track.age++
                    if (track.age > maxMissing) {
                        expired.add(tid)
                    }
                }
            }
            for (tid in expired) {
                tracks.remove(tid)
            }

            return buildTrackedDetections()
        }
    }

    /**
     * Add match scores to a track's rolling history.
     *
     * A score of 0.0 is a valid "no match" signal and is recorded;
     * only null means "no score was available for this frame" and is skipped.
     *
     * @param trackId The track to update.
     * @param reidScore ReID similarity score, or null if unavailable.
     * @param faceScore Face recognition score, or null if unavailable.
     */
    fun addScores(
        trackId: Int,
        reidScore: Float? = null,
        faceScore: Float? = null,
    ) {
        lock.withLock {
            val track = tracks[trackId] ?: return

            if (reidScore != null) {
                track.reidScores.add(reidScore)
                if (track.reidScores.size > scoreWindow) {
                    val excess = track.reidScores.size - scoreWindow
                    repeat(excess) { track.reidScores.removeAt(0) }
                }
            }

            if (faceScore != null) {
                track.faceScores.add(faceScore)
                if (track.faceScores.size > scoreWindow) {
                    val excess = track.faceScores.size - scoreWindow
                    repeat(excess) { track.faceScores.removeAt(0) }
                }
            }
        }
    }

    /**
     * Get current state of a specific track.
     *
     * @param trackId The track ID to look up.
     * @return [TrackSummary] if the track exists, null otherwise.
     */
    fun getTrack(trackId: Int): TrackSummary? {
        lock.withLock {
            val track = tracks[trackId] ?: return null
            return trackToSummary(track)
        }
    }

    /** Return all active (non-expired) tracks. */
    val activeTracks: List<TrackedDetection>
        get() = lock.withLock { buildTrackedDetections() }

    /** Remove all tracks and reset the ID counter. */
    fun clear() {
        lock.withLock {
            tracks.clear()
            nextId = 0
        }
    }

    // -- Internal helpers --

    private fun buildTrackedDetections(): List<TrackedDetection> {
        return tracks.values.map { track ->
            TrackedDetection(
                trackId = track.trackId,
                bbox = track.bbox.copyOf(),
                confidence = track.confidence,
                crop = track.crop,
            )
        }
    }

    private fun trackToSummary(track: Track): TrackSummary {
        val reidScores = track.reidScores.toList()
        val faceScores = track.faceScores.toList()
        return TrackSummary(
            trackId = track.trackId,
            reidScores = reidScores,
            faceScores = faceScores,
            avgReidScore = if (reidScores.isNotEmpty()) reidScores.average().toFloat() else 0f,
            avgFaceScore = if (faceScores.isNotEmpty()) faceScores.average().toFloat() else 0f,
            bestCrop = track.bestCrop,
        )
    }

    companion object {
        /**
         * Compute Intersection over Union between two bounding boxes.
         *
         * @param box1 [x1, y1, x2, y2] coordinates.
         * @param box2 [x1, y1, x2, y2] coordinates.
         * @return IoU value in [0, 1].
         */
        fun computeIou(box1: IntArray, box2: IntArray): Float {
            // Guard against malformed boxes
            if (box1[2] <= box1[0] || box1[3] <= box1[1]) return 0f
            if (box2[2] <= box2[0] || box2[3] <= box2[1]) return 0f

            val x1 = maxOf(box1[0], box2[0])
            val y1 = maxOf(box1[1], box2[1])
            val x2 = minOf(box1[2], box2[2])
            val y2 = minOf(box1[3], box2[3])
            val intersection = maxOf(0, x2 - x1) * maxOf(0, y2 - y1)
            val area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
            val area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
            val union = area1 + area2 - intersection
            return if (union > 0) intersection.toFloat() / union else 0f
        }
    }
}
