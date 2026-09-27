import CoreGraphics

/// Common interface for a single matching signal used by `MatchScorer`.
///
/// Each signal (clothing color, height ratio, ReID embedding, face
/// embedding, ...) can independently store a target reference and score
/// detection crops against it.
protocol MatchingSignal: AnyObject {
    /// Whether a target has been set for this signal.
    var hasTarget: Bool { get }

    /// Set the reference target from a photo.
    ///
    /// - Parameter photo: CGImage of the target.
    /// - Returns: `true` if the target was set successfully.
    func setTarget(photo: CGImage) -> Bool

    /// Compare a detected crop against the stored target.
    ///
    /// - Parameter crop: CGImage of a detection.
    /// - Returns: Similarity score, typically in [0, 1].
    func compare(crop: CGImage) -> Float

    /// Clear the current target.
    func clearTarget()
}
