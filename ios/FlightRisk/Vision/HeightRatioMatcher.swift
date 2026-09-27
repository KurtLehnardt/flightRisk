import CoreGraphics
import os

/// Bounding box height ratio matcher.
///
/// Compares the aspect ratio (width/height) of a target person's bounding box
/// against detection bounding boxes. Useful as a lightweight signal to filter
/// out detections with significantly different body proportions (e.g. children
/// vs adults, or crouching vs standing).
///
/// Registered as MatchScorer signal `heightRatio`.
final class HeightRatioMatcher {

    private let logger = Logger(subsystem: "com.flightrisk", category: "heightRatio")

    /// Stored target bbox aspect ratio (width / height).
    private var targetRatio: Float?

    /// Whether a target ratio has been set.
    var hasTarget: Bool { targetRatio != nil }

    // MARK: - Target Management

    /// Set the reference person's bounding box aspect ratio from a photo.
    ///
    /// The target photo is treated as a single-person crop, so the entire
    /// image dimensions define the aspect ratio.
    ///
    /// - Parameter photo: CGImage of the target person.
    /// - Returns: `true` if the ratio was computed successfully.
    func setTarget(photo: CGImage) -> Bool {
        let width = Float(photo.width)
        let height = Float(photo.height)
        guard height > 0 else {
            logger.warning("Target photo has zero height")
            return false
        }
        targetRatio = width / height
        logger.debug("Target height ratio set: \(self.targetRatio!)")
        return true
    }

    /// Set the target ratio from a bounding box.
    ///
    /// - Parameter bbox: `[x1, y1, x2, y2]` pixel coordinates.
    /// - Returns: `true` if the ratio was computed successfully.
    func setTarget(bbox: [Int]) -> Bool {
        let width = Float(bbox[2] - bbox[0])
        let height = Float(bbox[3] - bbox[1])
        guard height > 0 else {
            logger.warning("Target bbox has zero height")
            return false
        }
        targetRatio = width / height
        logger.debug("Target height ratio set from bbox: \(self.targetRatio!)")
        return true
    }

    /// Clear the current target ratio.
    func clearTarget() {
        targetRatio = nil
    }

    // MARK: - Comparison

    /// Compare a detection's aspect ratio against the target.
    ///
    /// Score formula: `1.0 - abs(target - detection) / max(target, detection)`
    ///
    /// - Parameter detection: A `Detection` from the person detector.
    /// - Returns: Similarity score clamped to [0, 1].
    func compare(detection: Detection) -> Float {
        return compare(bbox: detection.bbox)
    }

    /// Compare a bounding box's aspect ratio against the target.
    ///
    /// - Parameter bbox: `[x1, y1, x2, y2]` pixel coordinates.
    /// - Returns: Similarity score clamped to [0, 1].
    func compare(bbox: [Int]) -> Float {
        guard let target = targetRatio else { return 0.0 }

        let width = Float(bbox[2] - bbox[0])
        let height = Float(bbox[3] - bbox[1])
        guard height > 0 else { return 0.0 }

        let detectionRatio = width / height
        let maxRatio = max(target, detectionRatio)
        guard maxRatio > 0 else { return 0.0 }

        let score = 1.0 - abs(target - detectionRatio) / maxRatio
        return max(0.0, min(1.0, score))
    }

    /// Compare a person crop's dimensions against the target.
    ///
    /// - Parameter crop: CGImage of a detected person.
    /// - Returns: Similarity score clamped to [0, 1].
    func compare(crop: CGImage) -> Float {
        guard let target = targetRatio else { return 0.0 }

        let width = Float(crop.width)
        let height = Float(crop.height)
        guard height > 0 else { return 0.0 }

        let detectionRatio = width / height
        let maxRatio = max(target, detectionRatio)
        guard maxRatio > 0 else { return 0.0 }

        let score = 1.0 - abs(target - detectionRatio) / maxRatio
        return max(0.0, min(1.0, score))
    }
}
