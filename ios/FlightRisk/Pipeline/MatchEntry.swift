import CoreGraphics
import Foundation

/// Data structure representing a match event from the search pipeline.
///
/// Mirrors the Android `MatchEntry` data class and the Python `match_entry`
/// dict emitted in `pipeline.py` and `alerts.py`. Every match -- whether from
/// ReID, face recognition, or LLM description matching -- is captured as a
/// `MatchEntry` with scores, alert level, and optional GPS coordinates.
struct MatchEntry: Identifiable, Sendable {

    /// Unique identifier for SwiftUI list diffing.
    let id = UUID()

    /// Human-readable timestamp (HH:mm:ss).
    let time: String

    /// Combined match score from the multi-feature scorer.
    let score: Float

    /// ReID (re-identification) cosine similarity score.
    let reidScore: Float

    /// Face recognition similarity score.
    let faceScore: Float

    /// One of "confirmed_match", "possible_match", "weak_signal", or "no_match".
    let alertLevel: String

    /// Spatial track identifier (grid-cell key, e.g. "11_7").
    let trackId: String

    /// Cropped candidate image (may be nil if unavailable).
    let snapshot: CGImage?

    /// Whether LLM reasoning confirmed the match (nil if pending).
    var gemmaMatch: Bool?

    /// LLM confidence: "high", "medium", "low", "pending", or nil.
    var gemmaConfidence: String?

    /// Free-text reasoning from the LLM, or nil if not yet available.
    var reasoning: String?

    /// How the match was detected: "reid", "face", or "description".
    let matchType: String

    /// GPS latitude at time of match, or nil if unavailable.
    let latitude: Double?

    /// GPS longitude at time of match, or nil if unavailable.
    let longitude: Double?

    /// GPS accuracy in meters, or nil if unavailable.
    let locationAccuracy: Float?
}
