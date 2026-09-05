package com.flightrisk.app.vision

import android.graphics.Bitmap

/**
 * Shared data types for the vision pipeline.
 *
 * These mirror the dicts/dataclasses used by the Python reference
 * implementations in `flightrisk/vision/`.
 */

/**
 * A single person detection from YOLO.
 *
 * @property bbox Bounding box as [x1, y1, x2, y2] pixel coordinates.
 * @property confidence Detection confidence 0-1.
 * @property crop Cropped person image from the source frame.
 */
data class Detection(
    val bbox: IntArray,
    val confidence: Float,
    val crop: Bitmap,
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is Detection) return false
        return bbox.contentEquals(other.bbox) &&
            confidence == other.confidence &&
            crop.sameAs(other.crop)
    }

    override fun hashCode(): Int {
        var result = bbox.contentHashCode()
        result = 31 * result + confidence.hashCode()
        return result
    }
}

/**
 * A detection with a stable track ID assigned by [DetectionTracker].
 *
 * @property trackId Stable identifier for this tracked person across frames.
 * @property bbox Current bounding box [x1, y1, x2, y2].
 * @property confidence Latest detection confidence.
 * @property crop Latest cropped person image.
 */
data class TrackedDetection(
    val trackId: Int,
    val bbox: IntArray,
    val confidence: Float,
    val crop: Bitmap?,
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is TrackedDetection) return false
        return trackId == other.trackId &&
            bbox.contentEquals(other.bbox) &&
            confidence == other.confidence
    }

    override fun hashCode(): Int {
        var result = trackId
        result = 31 * result + bbox.contentHashCode()
        result = 31 * result + confidence.hashCode()
        return result
    }
}

/**
 * Combined match score from [MatchScorer].
 *
 * @property combinedScore Weighted score 0-1.
 * @property isMatch Whether the score exceeds the match threshold.
 * @property confidenceLevel "high", "medium", "low", or "none".
 * @property signalsUsed Number of signals that contributed.
 */
data class ScoredResult(
    val combinedScore: Float,
    val isMatch: Boolean,
    val confidenceLevel: String,
    val signalsUsed: Int,
)

/**
 * Summary of a tracked person's accumulated scores.
 *
 * @property trackId The track's stable identifier.
 * @property reidScores Rolling window of ReID scores.
 * @property faceScores Rolling window of face scores.
 * @property avgReidScore Mean of [reidScores].
 * @property avgFaceScore Mean of [faceScores].
 * @property bestCrop Highest-confidence crop seen for this track.
 */
data class TrackSummary(
    val trackId: Int,
    val reidScores: List<Float>,
    val faceScores: List<Float>,
    val avgReidScore: Float,
    val avgFaceScore: Float,
    val bestCrop: Bitmap?,
)

