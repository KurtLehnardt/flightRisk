import CoreGraphics
import os

/// IoU-based detection tracker for temporal vote accumulation.
///
/// Port of Android `DetectionTracker.kt` / Python `flightrisk/vision/tracker.py`.
/// Matches detections across frames by bounding box overlap (IoU), maintains a
/// rolling window of match scores per tracked person, and keeps the best crop
/// (highest detection confidence) for each track.
///
/// Thread safety: all mutations are guarded by `os_unfair_lock` for hot-path
/// performance. This class is `@unchecked Sendable` because all mutable state
/// is protected by the lock.
final class DetectionTracker: @unchecked Sendable {

    // MARK: - Internal track state

    private struct Track {
        let id: Int
        var bbox: [Int]
        var confidence: Float
        var crop: CGImage?
        var reidScores: [Float]
        var faceScores: [Float]
        var framesSeen: Int
        var missedFrames: Int
        var bestCrop: CGImage?
        var bestConfidence: Float
    }

    // MARK: - Configuration

    private let iouThreshold: Float
    private let maxMissing: Int
    private let scoreWindow: Int

    // MARK: - Mutable state (lock-guarded)

    private var lock = os_unfair_lock()
    private var tracks: [Int: Track] = [:]
    private var nextTrackId: Int = 0

    // MARK: - Init

    /// - Parameters:
    ///   - iouThreshold: Minimum IoU to match a detection to an existing track.
    ///   - maxMissing: Frames a track survives without a match before removal.
    ///   - scoreWindow: Number of recent scores to keep for averaging.
    init(iouThreshold: Float = 0.3, maxMissing: Int = 15, scoreWindow: Int = 8) {
        self.iouThreshold = iouThreshold
        self.maxMissing = maxMissing
        self.scoreWindow = scoreWindow
    }

    // MARK: - Public API

    /// Match new detections to existing tracks and return tracked detections.
    ///
    /// Uses greedy IoU matching: compute all pairwise IoU values between
    /// existing tracks and new detections, then greedily assign from
    /// highest IoU down. Unmatched detections become new tracks. Tracks
    /// without a match age and are removed after `maxMissing` frames.
    ///
    /// - Parameter detections: Detections from the person detector.
    /// - Returns: List of ``TrackedDetection`` with stable track IDs.
    func update(detections: [Detection]) -> [TrackedDetection] {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }

        if detections.isEmpty && tracks.isEmpty {
            return []
        }

        var matchedTrackIds = Set<Int>()
        var matchedDetIndices = Set<Int>()

        // Build IoU matrix and do greedy matching
        if !tracks.isEmpty && !detections.isEmpty {
            let trackIds = Array(tracks.keys)

            // Compute all IoU pairs above threshold
            struct IouPair {
                let iou: Float
                let detIdx: Int
                let trackId: Int
            }
            var iouPairs: [IouPair] = []

            for (detIdx, det) in detections.enumerated() {
                for tid in trackIds {
                    guard let track = tracks[tid] else { continue }
                    let iou = Self.computeIou(box1: det.bbox, box2: track.bbox)
                    if iou >= iouThreshold {
                        iouPairs.append(IouPair(iou: iou, detIdx: detIdx, trackId: tid))
                    }
                }
            }

            // Sort by IoU descending for greedy matching
            iouPairs.sort { $0.iou > $1.iou }

            for pair in iouPairs {
                if matchedDetIndices.contains(pair.detIdx) || matchedTrackIds.contains(pair.trackId) {
                    continue
                }
                // Match this detection to this track
                let det = detections[pair.detIdx]
                tracks[pair.trackId]!.bbox = det.bbox
                tracks[pair.trackId]!.confidence = det.confidence
                tracks[pair.trackId]!.crop = det.crop
                tracks[pair.trackId]!.framesSeen += 1
                tracks[pair.trackId]!.missedFrames = 0

                // Update best crop if this detection has higher confidence
                if det.confidence > tracks[pair.trackId]!.bestConfidence {
                    tracks[pair.trackId]!.bestCrop = det.crop
                    tracks[pair.trackId]!.bestConfidence = det.confidence
                }

                matchedTrackIds.insert(pair.trackId)
                matchedDetIndices.insert(pair.detIdx)
            }
        }

        // Create new tracks for unmatched detections
        for (detIdx, det) in detections.enumerated() {
            if matchedDetIndices.contains(detIdx) { continue }
            let newTrack = Track(
                id: nextTrackId,
                bbox: det.bbox,
                confidence: det.confidence,
                crop: det.crop,
                reidScores: [],
                faceScores: [],
                framesSeen: 1,
                missedFrames: 0,
                bestCrop: det.crop,
                bestConfidence: det.confidence
            )
            tracks[nextTrackId] = newTrack
            matchedTrackIds.insert(nextTrackId)
            nextTrackId += 1
        }

        // Age unmatched tracks and remove expired ones
        var expired: [Int] = []
        for (tid, _) in tracks {
            if !matchedTrackIds.contains(tid) {
                tracks[tid]!.missedFrames += 1
                if tracks[tid]!.missedFrames > maxMissing {
                    expired.append(tid)
                }
            }
        }
        for tid in expired {
            tracks.removeValue(forKey: tid)
        }

        return buildTrackedDetections()
    }

    /// Add match scores to a track's rolling history.
    ///
    /// A score of 0.0 is a valid "no match" signal and is recorded;
    /// pass `nil` to indicate "no score was available for this frame".
    ///
    /// - Parameters:
    ///   - trackId: The track to update.
    ///   - reidScore: ReID similarity score, or nil if unavailable.
    ///   - faceScore: Face recognition score, or nil if unavailable.
    func addScores(trackId: Int, reidScore: Float? = nil, faceScore: Float? = nil) {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }

        guard tracks[trackId] != nil else { return }

        if let score = reidScore {
            tracks[trackId]!.reidScores.append(score)
            if tracks[trackId]!.reidScores.count > scoreWindow {
                let excess = tracks[trackId]!.reidScores.count - scoreWindow
                tracks[trackId]!.reidScores.removeFirst(excess)
            }
        }

        if let score = faceScore {
            tracks[trackId]!.faceScores.append(score)
            if tracks[trackId]!.faceScores.count > scoreWindow {
                let excess = tracks[trackId]!.faceScores.count - scoreWindow
                tracks[trackId]!.faceScores.removeFirst(excess)
            }
        }
    }

    /// Get current state of a specific track.
    ///
    /// - Parameter trackId: The track ID to look up.
    /// - Returns: ``TrackSummary`` if the track exists, nil otherwise.
    func getTrack(_ trackId: Int) -> TrackSummary? {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }

        guard let track = tracks[trackId] else { return nil }
        return trackToSummary(track)
    }

    /// All active (non-expired) tracks as tracked detections.
    var activeTracks: [TrackedDetection] {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        return buildTrackedDetections()
    }

    /// Remove all tracks and reset the ID counter.
    func clear() {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        tracks.removeAll()
        nextTrackId = 0
    }

    // MARK: - Static helpers

    /// Compute Intersection over Union between two bounding boxes.
    ///
    /// - Parameters:
    ///   - box1: `[x1, y1, x2, y2]` pixel coordinates.
    ///   - box2: `[x1, y1, x2, y2]` pixel coordinates.
    /// - Returns: IoU value in [0, 1].
    static func computeIou(box1: [Int], box2: [Int]) -> Float {
        // Guard against malformed boxes
        guard box1.count >= 4 && box2.count >= 4 else { return 0 }
        if box1[2] <= box1[0] || box1[3] <= box1[1] { return 0 }
        if box2[2] <= box2[0] || box2[3] <= box2[1] { return 0 }

        let x1 = max(box1[0], box2[0])
        let y1 = max(box1[1], box2[1])
        let x2 = min(box1[2], box2[2])
        let y2 = min(box1[3], box2[3])
        let intersection = max(0, x2 - x1) * max(0, y2 - y1)
        let area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
        let area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
        let union = area1 + area2 - intersection
        return union > 0 ? Float(intersection) / Float(union) : 0
    }

    // MARK: - Private helpers

    private func buildTrackedDetections() -> [TrackedDetection] {
        tracks.values.map { track in
            TrackedDetection(
                trackId: track.id,
                bbox: track.bbox,
                confidence: track.confidence,
                crop: track.crop
            )
        }
    }

    private func trackToSummary(_ track: Track) -> TrackSummary {
        let reidScores = track.reidScores
        let faceScores = track.faceScores
        return TrackSummary(
            trackId: track.id,
            reidScores: reidScores,
            faceScores: faceScores,
            avgReidScore: reidScores.isEmpty ? 0 : reidScores.reduce(0, +) / Float(reidScores.count),
            avgFaceScore: faceScores.isEmpty ? 0 : faceScores.reduce(0, +) / Float(faceScores.count),
            bestCrop: track.bestCrop
        )
    }
}
