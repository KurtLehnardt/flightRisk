import Foundation

// Pure geometry for the read-only plan grid on the phone. It turns a `fleet_plan` payload into
// shapes in the plan's centimetre frame and maps that frame onto a canvas. It never decides
// whether a plan is valid: the ground is the authority, and odd data is skipped and counted,
// never repaired.
//
// Frame and conventions, taken from the Python planner (`flightrisk/drone/plan_paths.py`,
// `trace_lanes` and its direction table) and the dashboard (`static/ui/plan_geom.js`,
// `static/ui/ui.js` `lanePath`):
// - x grows to the right and y grows forward ("up" on the dashboard map).
// - forward is (0, +1), back is (0, -1), right is (+1, 0), left is (-1, 0).
// - A drone's lanes start at its takeoff point: `takeoff_x_cm`/`takeoff_y_cm` when the zone
//   carries both, otherwise the zone's x_cm/y_cm corner (`PlanZone.origin` in `coordinator.py`).
// - A zero-distance lane adds a repeated point, exactly like the Python trace.

public struct PlanPoint: Equatable, Sendable {
    public var x: Double
    public var y: Double
    public init(x: Double, y: Double) {
        self.x = x
        self.y = y
    }
}

/// An axis-aligned box in the plan frame, `minX <= maxX` and `minY <= maxY`.
public struct PlanBox: Equatable, Sendable {
    public var minX: Double
    public var minY: Double
    public var maxX: Double
    public var maxY: Double

    public init(minX: Double, minY: Double, maxX: Double, maxY: Double) {
        self.minX = minX
        self.minY = minY
        self.maxX = maxX
        self.maxY = maxY
    }

    public var width: Double { maxX - minX }
    public var height: Double { maxY - minY }

    func union(_ other: PlanBox) -> PlanBox {
        PlanBox(minX: min(minX, other.minX), minY: min(minY, other.minY),
                maxX: max(maxX, other.maxX), maxY: max(maxY, other.maxY))
    }

    func including(_ p: PlanPoint) -> PlanBox {
        PlanBox(minX: min(minX, p.x), minY: min(minY, p.y), maxX: max(maxX, p.x), maxY: max(maxY, p.y))
    }
}

/// One lane of a zone drawn as a straight segment.
public struct PlanSegment: Equatable, Sendable {
    public var from: PlanPoint
    public var to: PlanPoint
    public var direction: LaneDirection
    public init(from: PlanPoint, to: PlanPoint, direction: LaneDirection) {
        self.from = from
        self.to = to
        self.direction = direction
    }
    /// True when the segment has no length (a zero-distance lane): no arrow is drawn for it.
    public var isZeroLength: Bool { from == to }
}

public struct ZoneDrawing: Equatable, Sendable {
    public var droneId: String
    public var altCm: Double?
    public var lost: Bool
    /// The zone rectangle, or nil when it has no usable size.
    public var rect: PlanBox?
    /// The takeoff point where the lanes start, or nil when the zone has no usable origin.
    public var start: PlanPoint?
    /// The start point followed by the end of every drawn lane.
    public var points: [PlanPoint]
    public var segments: [PlanSegment]
    /// Lanes not drawn: unknown direction, or a distance that is missing, not finite, negative
    /// or absurdly large.
    public var skippedLanes: Int
    /// Lanes cut off by the per-zone lane cap. The picture shows only part of the path.
    public var droppedLanes: Int
    /// True when the zone carried a takeoff point that is not usable (not finite or out of range),
    /// so the path is drawn from the zone corner instead.
    public var takeoffFellBack: Bool
}

public struct UncoveredLaneDrawing: Equatable, Sendable {
    public var droneId: String?
    public var index: Int?
    public var from: PlanPoint
    public var to: PlanPoint
    public var reason: String?
}

public struct UncoveredAreaDrawing: Equatable, Sendable {
    public var rect: PlanBox
    public var reason: String?
}

public struct PlanDrawing: Equatable, Sendable {
    /// The grid outline, or nil when the grid is missing or has no usable size.
    public var grid: PlanBox?
    /// Everything that must fit on screen: the grid plus every drawn zone, lane and uncovered
    /// shape, so a zone outside the grid is still visible.
    public var bounds: PlanBox
    public var zones: [ZoneDrawing]
    public var uncoveredLanes: [UncoveredLaneDrawing]
    public var uncoveredAreas: [UncoveredAreaDrawing]
    public var gapCm: Double?
    public var stripCm: Double?
    /// Zone lanes not drawn because of unusable data, summed over all zones.
    public var skippedLanes: Int
    /// Zone lanes not drawn because of the per-zone lane cap, summed over all zones.
    public var droppedLanes: Int
    /// Drone ids whose unusable takeoff point was replaced by the zone corner.
    public var takeoffFallbackDroneIds: [String]
    /// Uncovered entries not drawn (unusable numbers or an unknown shape).
    public var skippedUncovered: Int
    /// True when a cap cut some zones, lanes or uncovered entries off the drawing.
    public var truncated: Bool

    /// True when there is nothing to draw: no zone and no uncovered entry.
    public var isEmpty: Bool { zones.isEmpty && uncoveredLanes.isEmpty && uncoveredAreas.isEmpty }
    public var hasLostZone: Bool { zones.contains { $0.lost } }
    public var hasUncovered: Bool { !uncoveredLanes.isEmpty || !uncoveredAreas.isEmpty }
}

/// A scale-to-fit mapping from the plan frame to canvas points. The plan's y axis points up, the
/// canvas's points down, so y is flipped.
public struct PlanTransform: Equatable, Sendable {
    public var bounds: PlanBox
    public var scale: Double
    public var originX: Double
    public var originY: Double

    public func map(_ p: PlanPoint) -> (x: Double, y: Double) {
        (originX + (p.x - bounds.minX) * scale, originY + (bounds.maxY - p.y) * scale)
    }

    public func unmap(x: Double, y: Double) -> PlanPoint {
        guard scale > 0 else { return PlanPoint(x: bounds.minX, y: bounds.maxY) }
        return PlanPoint(x: bounds.minX + (x - originX) / scale, y: bounds.maxY - (y - originY) / scale)
    }
}

public enum PlanGeometry {
    /// Largest magnitude of a centimetre value that is drawn (100 km). Anything else is hostile.
    public static let maxAbsCm = 1.0e7
    public static let maxZones = 64
    public static let maxLanesPerZone = 1000
    public static let maxUncoveredEntries = 500
    /// Smallest extent the view fits, so a point-sized plan still gets a sane scale.
    public static let minExtentCm = 100.0
    public static let fallbackBounds = PlanBox(minX: 0, minY: 0, maxX: 400, maxY: 400)

    /// The unit step of a lane direction, or nil for a direction this build does not know.
    public static func unitVector(_ direction: LaneDirection) -> (dx: Double, dy: Double)? {
        switch direction {
        case .forward: return (0, 1)
        case .back: return (0, -1)
        case .right: return (1, 0)
        case .left: return (-1, 0)
        case .unknown: return nil
        }
    }

    static func clean(_ v: Double?) -> Double? {
        guard let v, v.isFinite, abs(v) <= maxAbsCm else { return nil }
        return v
    }

    static func box(x: Double?, y: Double?, w: Double?, h: Double?) -> PlanBox? {
        guard let x = clean(x), let y = clean(y), let w = clean(w), let h = clean(h), w > 0, h > 0,
              clean(x + w) != nil, clean(y + h) != nil else { return nil }
        return PlanBox(minX: x, minY: y, maxX: x + w, maxY: y + h)
    }

    /// The points a drone flies through, starting at `start` and applying `lanes` in order.
    /// Returns the points, the segments and the number of lanes skipped.
    public static func trace(start: PlanPoint, lanes: [PlanZoneLane]) -> (points: [PlanPoint], segments: [PlanSegment], skipped: Int) {
        var points = [start]
        var segments: [PlanSegment] = []
        var skipped = 0
        var cur = start
        for lane in lanes {
            guard let direction = lane.direction, let u = unitVector(direction),
                  let d = clean(lane.distanceCm), d >= 0 else {
                skipped += 1
                continue
            }
            let next = PlanPoint(x: cur.x + u.dx * d, y: cur.y + u.dy * d)
            guard abs(next.x) <= maxAbsCm, abs(next.y) <= maxAbsCm else {
                skipped += 1
                continue
            }
            points.append(next)
            segments.append(PlanSegment(from: cur, to: next, direction: direction))
            cur = next
        }
        return (points, segments, skipped)
    }

    public static func zoneDrawing(_ zone: PlanZone) -> ZoneDrawing {
        let rect = box(x: zone.xCm, y: zone.yCm, w: zone.wCm, h: zone.hCm)
        var start: PlanPoint?
        var fellBack = false
        if let tx = clean(zone.takeoffXCm), let ty = clean(zone.takeoffYCm) {
            start = PlanPoint(x: tx, y: ty)
        } else if let x = clean(zone.xCm), let y = clean(zone.yCm) {
            start = PlanPoint(x: x, y: y)
            fellBack = [zone.takeoffXCm, zone.takeoffYCm].contains { $0 != nil && clean($0) == nil }
        }
        let allLanes = zone.lanes ?? []
        let lanes = Array(allLanes.prefix(maxLanesPerZone))
        var points: [PlanPoint] = []
        var segments: [PlanSegment] = []
        let dropped = allLanes.count - lanes.count
        var skipped = 0
        if let start {
            let t = trace(start: start, lanes: lanes)
            points = t.points
            segments = t.segments
            skipped += t.skipped
        } else {
            skipped += lanes.count
        }
        return ZoneDrawing(droneId: zone.droneId ?? "", altCm: clean(zone.altCm), lost: zone.lost ?? false,
                           rect: rect, start: start, points: points, segments: segments, skippedLanes: skipped,
                           droppedLanes: dropped, takeoffFellBack: fellBack)
    }

    /// Everything the plan grid draws, in the plan frame. Never fails: unusable data is skipped
    /// and counted.
    public static func drawing(_ plan: FleetPlanPayload) -> PlanDrawing {
        let grid = plan.grid.flatMap { box(x: $0.xCm, y: $0.yCm, w: $0.wCm, h: $0.hCm) }
        let allZones = plan.zones ?? []
        var truncated = allZones.count > maxZones
        let zones = allZones.prefix(maxZones).map(zoneDrawing)
        if zones.contains(where: { $0.droppedLanes > 0 }) { truncated = true }

        var lanes: [UncoveredLaneDrawing] = []
        var areas: [UncoveredAreaDrawing] = []
        var skippedUncovered = 0
        let allUncovered = plan.uncovered ?? []
        if allUncovered.count > maxUncoveredEntries { truncated = true }
        for entry in allUncovered.prefix(maxUncoveredEntries) {
            if entry.isArea {
                if let r = box(x: entry.xCm, y: entry.yCm, w: entry.wCm, h: entry.hCm) {
                    areas.append(UncoveredAreaDrawing(rect: r, reason: entry.reason))
                } else {
                    skippedUncovered += 1
                }
            } else if entry.kind == nil {
                if let lane = uncoveredLane(entry) { lanes.append(lane) } else { skippedUncovered += 1 }
            } else {
                skippedUncovered += 1
            }
        }

        var bounds: PlanBox? = grid
        func add(_ b: PlanBox) { bounds = bounds.map { $0.union(b) } ?? b }
        for z in zones {
            if let r = z.rect { add(r) }
            for p in z.points { add(PlanBox(minX: p.x, minY: p.y, maxX: p.x, maxY: p.y)) }
        }
        for l in lanes {
            add(PlanBox(minX: l.from.x, minY: l.from.y, maxX: l.from.x, maxY: l.from.y).including(l.to))
        }
        for a in areas { add(a.rect) }

        return PlanDrawing(
            grid: grid, bounds: bounds ?? fallbackBounds, zones: zones,
            uncoveredLanes: lanes, uncoveredAreas: areas,
            gapCm: clean(plan.gapCm), stripCm: clean(plan.stripCm),
            skippedLanes: zones.reduce(0) { $0 + $1.skippedLanes },
            droppedLanes: zones.reduce(0) { $0 + $1.droppedLanes },
            takeoffFallbackDroneIds: zones.filter(\.takeoffFellBack).map(\.droneId),
            skippedUncovered: skippedUncovered, truncated: truncated)
    }

    /// An uncovered lane runs from (x_cm, y_cm) to (end_x_cm, end_y_cm). When the end is missing
    /// it is derived from the direction and distance.
    static func uncoveredLane(_ e: UncoveredEntry) -> UncoveredLaneDrawing? {
        guard let x = clean(e.xCm), let y = clean(e.yCm) else { return nil }
        let from = PlanPoint(x: x, y: y)
        var to: PlanPoint?
        if let ex = clean(e.endXCm), let ey = clean(e.endYCm) {
            to = PlanPoint(x: ex, y: ey)
        } else if let direction = e.direction, let u = unitVector(direction),
                  let d = clean(e.distanceCm), d >= 0 {
            let p = PlanPoint(x: x + u.dx * d, y: y + u.dy * d)
            if abs(p.x) <= maxAbsCm, abs(p.y) <= maxAbsCm { to = p }
        }
        guard let to else { return nil }
        return UncoveredLaneDrawing(droneId: e.droneId, index: e.index, from: from, to: to, reason: e.reason)
    }

    /// Fits `bounds` into a canvas of the given size with `padding` on every side, centred, with
    /// one scale on both axes. A degenerate box is widened to `minExtentCm`. Hostile canvas
    /// numbers give a zero scale, never NaN.
    public static func transform(bounds: PlanBox, canvasWidth: Double, canvasHeight: Double, padding: Double) -> PlanTransform {
        var b = bounds
        if b.width < minExtentCm {
            let c = (b.minX + b.maxX) / 2
            b.minX = c - minExtentCm / 2
            b.maxX = c + minExtentCm / 2
        }
        if b.height < minExtentCm {
            let c = (b.minY + b.maxY) / 2
            b.minY = c - minExtentCm / 2
            b.maxY = c + minExtentCm / 2
        }
        let cw = canvasWidth.isFinite ? max(canvasWidth, 0) : 0
        let ch = canvasHeight.isFinite ? max(canvasHeight, 0) : 0
        let pad = padding.isFinite ? max(padding, 0) : 0
        let availW = max(cw - 2 * pad, 0)
        let availH = max(ch - 2 * pad, 0)
        let scale = min(availW / b.width, availH / b.height)
        let safeScale = scale.isFinite && scale > 0 ? scale : 0
        return PlanTransform(bounds: b, scale: safeScale,
                             originX: pad + (availW - b.width * safeScale) / 2,
                             originY: pad + (availH - b.height * safeScale) / 2)
    }

    /// The canvas height that fits `bounds` at a given canvas width, within `[minHeight, maxHeight]`.
    public static func fittedHeight(bounds: PlanBox, canvasWidth: Double, padding: Double,
                                    minHeight: Double, maxHeight: Double) -> Double {
        let w = max(bounds.width, minExtentCm), h = max(bounds.height, minExtentCm)
        let pad = padding.isFinite ? max(padding, 0) : 0
        let availW = (canvasWidth.isFinite ? max(canvasWidth, 0) : 0) - 2 * pad
        let raw = availW > 0 ? availW * h / w + 2 * pad : minHeight
        guard raw.isFinite else { return minHeight }
        return min(max(raw, minHeight), maxHeight)
    }
}
