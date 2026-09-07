import CoreGraphics

/// Shared data types for the vision pipeline.
///
/// These mirror the data classes in the Android `Models.kt` and the
/// Python reference implementations in `flightrisk/vision/`.

/// A single person detection from YOLO.
struct Detection {
    /// Bounding box as [x1, y1, x2, y2] pixel coordinates.
    let bbox: [Int]
    /// Detection confidence 0-1.
    let confidence: Float
    /// Cropped person image from the source frame.
    let crop: CGImage
}

/// A detection with a stable track ID assigned by `DetectionTracker`.
struct TrackedDetection {
    /// Stable identifier for this tracked person across frames.
    let trackId: Int
    /// Current bounding box [x1, y1, x2, y2].
    let bbox: [Int]
    /// Latest detection confidence.
    let confidence: Float
    /// Latest cropped person image.
    let crop: CGImage?
}

/// Combined match score from `MatchScorer`.
struct ScoredResult {
    /// Weighted score 0-1.
    let combinedScore: Float
    /// Whether the score exceeds the match threshold.
    let isMatch: Bool
    /// "high", "medium", "low", or "none".
    let confidenceLevel: String
    /// Number of signals that contributed.
    let signalsUsed: Int
}

/// Summary of a tracked person's accumulated scores.
struct TrackSummary {
    /// The track's stable identifier.
    let trackId: Int
    /// Rolling window of ReID scores.
    let reidScores: [Float]
    /// Rolling window of face scores.
    let faceScores: [Float]
    /// Mean of `reidScores`.
    let avgReidScore: Float
    /// Mean of `faceScores`.
    let avgFaceScore: Float
    /// Highest-confidence crop seen for this track.
    let bestCrop: CGImage?
}
