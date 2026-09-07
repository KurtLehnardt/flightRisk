import Foundation

// MARK: - Pattern Type

enum PatternType: String, CaseIterable, Identifiable {
    case expandingSquare = "Expanding Square"
    case sector = "Sector/Radial"
    case parallelTrack = "Parallel Track"
    case trackLine = "Track Line"
    case spiral = "Spiral"

    var id: String { rawValue }

    var description: String {
        switch self {
        case .expandingSquare:
            return "Searches outward in expanding squares from the current position"
        case .sector:
            return "Searches in pie-slice sectors radiating from center"
        case .parallelTrack:
            return "Searches in parallel strips across an area"
        case .trackLine:
            return "Searches along a line with perpendicular sweeps"
        case .spiral:
            return "Searches in an outward spiral from center"
        }
    }
}

// MARK: - Waypoint

struct Waypoint {
    let direction: String  // "forward", "back", "left", "right"
    let distanceCm: Int
    let rotateDegrees: Int

    init(direction: String, distanceCm: Int, rotateDegrees: Int = 0) {
        self.direction = direction
        self.distanceCm = distanceCm
        self.rotateDegrees = rotateDegrees
    }
}

// MARK: - Search Pattern Generator

enum SearchPattern {

    // MARK: - Helpers

    private static func clampDistance(_ cm: Int) -> Int {
        max(20, min(500, cm))
    }

    private static func splitLongMove(direction: String, distanceCm totalCm: Int) -> [Waypoint] {
        var waypoints: [Waypoint] = []
        var remaining = totalCm
        while remaining > 0 {
            let segment = clampDistance(min(remaining, 500))
            waypoints.append(Waypoint(direction: direction, distanceCm: segment))
            remaining -= segment
        }
        return waypoints
    }

    // MARK: - Expanding Square

    /// Searches outward in expanding squares.
    /// Each expansion adds `growthCm` to the side length every two sides.
    static func generateExpandingSquare(
        initialSideCm: Int = 100,
        growthCm: Int = 100,
        numExpansions: Int = 4
    ) -> [Waypoint] {
        var waypoints: [Waypoint] = []
        var side = initialSideCm

        for _ in 0..<numExpansions {
            for i in 0..<4 {
                waypoints.append(contentsOf: splitLongMove(direction: "forward", distanceCm: side))
                waypoints.append(Waypoint(direction: "forward", distanceCm: 0, rotateDegrees: 90))
                if i % 2 == 1 {
                    side += growthCm
                }
            }
        }
        return waypoints
    }

    // MARK: - Sector / Radial

    /// Searches in pie-slice sectors radiating from center.
    /// Flies out along a radius, returns, then rotates to the next sector.
    static func generateSector(
        radiusCm: Int = 300,
        numSectors: Int = 6
    ) -> [Waypoint] {
        var waypoints: [Waypoint] = []
        let sectorAngle = 360 / numSectors

        for _ in 0..<numSectors {
            waypoints.append(contentsOf: splitLongMove(direction: "forward", distanceCm: radiusCm))
            waypoints.append(Waypoint(direction: "forward", distanceCm: 0, rotateDegrees: 180))
            waypoints.append(contentsOf: splitLongMove(direction: "forward", distanceCm: radiusCm))
            waypoints.append(Waypoint(direction: "forward", distanceCm: 0, rotateDegrees: 180 + sectorAngle))
        }
        return waypoints
    }

    // MARK: - Parallel Track

    /// Searches in parallel strips (lawnmower pattern) across an area.
    static func generateParallelTrack(
        widthCm: Int = 400,
        depthCm: Int = 400,
        stripWidthCm: Int = 150
    ) -> [Waypoint] {
        var waypoints: [Waypoint] = []
        let numStrips = max(1, widthCm / stripWidthCm)
        var goingForward = true

        for i in 0..<numStrips {
            let direction = goingForward ? "forward" : "back"
            waypoints.append(contentsOf: splitLongMove(direction: direction, distanceCm: depthCm))
            if i < numStrips - 1 {
                waypoints.append(contentsOf: splitLongMove(direction: "right", distanceCm: stripWidthCm))
            }
            goingForward = !goingForward
        }
        return waypoints
    }

    // MARK: - Track Line

    /// Searches along a line with perpendicular sweeps left and right.
    static func generateTrackLine(
        lengthCm: Int = 500,
        sweepWidthCm: Int = 100,
        numSweeps: Int = 3
    ) -> [Waypoint] {
        var waypoints: [Waypoint] = []
        let segment = lengthCm / (numSweeps * 2 + 1)

        for _ in 0..<numSweeps {
            waypoints.append(contentsOf: splitLongMove(direction: "forward", distanceCm: segment))
            waypoints.append(contentsOf: splitLongMove(direction: "left", distanceCm: sweepWidthCm))
            waypoints.append(contentsOf: splitLongMove(direction: "forward", distanceCm: segment))
            waypoints.append(contentsOf: splitLongMove(direction: "right", distanceCm: sweepWidthCm * 2))
            waypoints.append(contentsOf: splitLongMove(direction: "forward", distanceCm: segment))
            waypoints.append(contentsOf: splitLongMove(direction: "left", distanceCm: sweepWidthCm))
        }
        waypoints.append(contentsOf: splitLongMove(direction: "forward", distanceCm: segment))
        return waypoints
    }

    // MARK: - Spiral

    /// Searches in an outward spiral, each turn growing in radius.
    static func generateSpiral(
        radiusCm: Int = 50,
        growthPerTurnCm: Int = 100,
        numTurns: Int = 3,
        segmentsPerTurn: Int = 8
    ) -> [Waypoint] {
        var waypoints: [Waypoint] = []
        let rotationPerSegment = 360 / segmentsPerTurn

        for turn in 0..<numTurns {
            let currentRadius = radiusCm + (turn * growthPerTurnCm)
            let segmentLength = clampDistance(
                Int(2.0 * Double.pi * Double(currentRadius) / Double(segmentsPerTurn))
            )
            for _ in 0..<segmentsPerTurn {
                waypoints.append(Waypoint(
                    direction: "forward",
                    distanceCm: segmentLength,
                    rotateDegrees: rotationPerSegment
                ))
            }
        }
        return waypoints
    }

    // MARK: - Dispatch

    /// Generate waypoints for the given pattern type using default parameters.
    static func generate(type: PatternType) -> [Waypoint] {
        switch type {
        case .expandingSquare:
            return generateExpandingSquare()
        case .sector:
            return generateSector()
        case .parallelTrack:
            return generateParallelTrack()
        case .trackLine:
            return generateTrackLine()
        case .spiral:
            return generateSpiral()
        }
    }
}
