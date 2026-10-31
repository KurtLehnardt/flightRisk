package com.flightrisk.contract

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

// Pure geometry for the read-only plan canvas on the phone. It turns a `fleet_plan` payload into
// shapes in the plan's centimetre frame and maps that frame onto a canvas. It never decides
// whether a plan is valid: the ground is the authority, and odd data is skipped and counted,
// never repaired. JDK only, the same rules as the iOS file `PlanGeometry.swift`.
//
// Frame and conventions, taken from the Python planner (`flightrisk/drone/plan_paths.py`,
// `trace_lanes` and its direction table) and the dashboard (`static/ui/plan_geom.js`,
// `static/ui/ui.js` `lanePath`):
// - x grows to the right and y grows forward ("up" on the dashboard map).
// - forward is (0, +1), back is (0, -1), right is (+1, 0), left is (-1, 0).
// - A drone's lanes start at its takeoff point: `takeoff_x_cm`/`takeoff_y_cm` when the zone
//   carries both, otherwise the zone's x_cm/y_cm corner (`PlanZone.origin` in `coordinator.py`).
// - A zero-distance lane adds a repeated point, exactly like the Python trace.

/** A point in the plan frame, in centimetres. */
data class PlanPoint(val x: Double, val y: Double)

/** An axis-aligned box in the plan frame, `minX <= maxX` and `minY <= maxY`. */
data class PlanBox(val minX: Double, val minY: Double, val maxX: Double, val maxY: Double) {
    val width: Double get() = maxX - minX
    val height: Double get() = maxY - minY

    internal fun union(other: PlanBox): PlanBox =
        PlanBox(min(minX, other.minX), min(minY, other.minY), max(maxX, other.maxX), max(maxY, other.maxY))

    internal fun including(p: PlanPoint): PlanBox =
        PlanBox(min(minX, p.x), min(minY, p.y), max(maxX, p.x), max(maxY, p.y))
}

/** One lane of a zone drawn as a straight segment. */
data class PlanSegment(val from: PlanPoint, val to: PlanPoint, val direction: LaneDirection) {
    /** True when the segment has no length (a zero-distance lane): no arrow is drawn for it. */
    val isZeroLength: Boolean get() = from == to
}

data class ZoneDrawing(
    val droneId: String,
    val altCm: Double?,
    val lost: Boolean,
    /** The zone rectangle, or null when it has no usable size. */
    val rect: PlanBox?,
    /** The takeoff point where the lanes start, or null when the zone has no usable origin. */
    val start: PlanPoint?,
    /** The start point followed by the end of every drawn lane. */
    val points: List<PlanPoint>,
    val segments: List<PlanSegment>,
    /** Lanes not drawn: unknown direction, or a distance that is missing, not finite, negative or absurdly large. */
    val skippedLanes: Int,
    /** Lanes cut off by the per-zone lane cap. The picture shows only part of the path. */
    val droppedLanes: Int,
    /** True when the zone carried a takeoff point that is not usable (not finite or out of range), so the path starts at the zone corner. */
    val takeoffFellBack: Boolean,
)

data class UncoveredLaneDrawing(
    val droneId: String?,
    val index: Int?,
    val from: PlanPoint,
    val to: PlanPoint,
    val reason: String?,
)

data class UncoveredAreaDrawing(val rect: PlanBox, val reason: String?)

data class PlanDrawing(
    /** The grid outline, or null when the grid is missing or has no usable size. */
    val grid: PlanBox?,
    /**
     * Everything that must fit on screen: the grid plus every drawn zone, lane and uncovered
     * shape, so a zone outside the grid is still visible.
     */
    val bounds: PlanBox,
    val zones: List<ZoneDrawing>,
    val uncoveredLanes: List<UncoveredLaneDrawing>,
    val uncoveredAreas: List<UncoveredAreaDrawing>,
    val gapCm: Double?,
    val stripCm: Double?,
    /** Zone lanes not drawn because of unusable data, summed over all zones. */
    val skippedLanes: Int,
    /** Zone lanes not drawn because of the per-zone lane cap, summed over all zones. */
    val droppedLanes: Int,
    /** Drone ids whose unusable takeoff point was replaced by the zone corner. */
    val takeoffFallbackDroneIds: List<String>,
    /** Uncovered entries not drawn (unusable numbers or an unknown shape). */
    val skippedUncovered: Int,
    /** True when a cap cut some zones, lanes or uncovered entries off the drawing. */
    val truncated: Boolean,
) {
    /** True when there is nothing to draw: no zone and no uncovered entry. */
    val isEmpty: Boolean get() = zones.isEmpty() && uncoveredLanes.isEmpty() && uncoveredAreas.isEmpty()
    val hasLostZone: Boolean get() = zones.any { it.lost }
    val hasUncovered: Boolean get() = uncoveredLanes.isNotEmpty() || uncoveredAreas.isNotEmpty()
}

/**
 * A scale-to-fit mapping from the plan frame to canvas points. The plan's y axis points up, the
 * canvas's points down, so y is flipped.
 */
data class PlanTransform(val bounds: PlanBox, val scale: Double, val originX: Double, val originY: Double) {
    fun map(p: PlanPoint): PlanPoint =
        PlanPoint(originX + (p.x - bounds.minX) * scale, originY + (bounds.maxY - p.y) * scale)

    fun unmap(x: Double, y: Double): PlanPoint {
        if (!(scale > 0)) return PlanPoint(bounds.minX, bounds.maxY)
        return PlanPoint(bounds.minX + (x - originX) / scale, bounds.maxY - (y - originY) / scale)
    }
}

object PlanGeometry {
    /** Largest magnitude of a centimetre value that is drawn (100 km). Anything else is hostile. */
    const val MAX_ABS_CM = 1.0e7
    const val MAX_ZONES = 64
    const val MAX_LANES_PER_ZONE = 1000
    const val MAX_UNCOVERED_ENTRIES = 500

    /** Smallest extent the view fits, so a point-sized plan still gets a sane scale. */
    const val MIN_EXTENT_CM = 100.0
    val fallbackBounds = PlanBox(0.0, 0.0, 400.0, 400.0)

    /** The unit step (dx, dy) of a lane direction, or null for a direction this build does not know. */
    fun unitVector(direction: LaneDirection): Pair<Double, Double>? = when (direction) {
        LaneDirection.Forward -> 0.0 to 1.0
        LaneDirection.Back -> 0.0 to -1.0
        LaneDirection.Right -> 1.0 to 0.0
        LaneDirection.Left -> -1.0 to 0.0
        is LaneDirection.Unknown -> null
    }

    internal fun clean(v: Double?): Double? = if (v != null && v.isFinite() && abs(v) <= MAX_ABS_CM) v else null

    internal fun box(x: Double?, y: Double?, w: Double?, h: Double?): PlanBox? {
        val cx = clean(x) ?: return null
        val cy = clean(y) ?: return null
        val cw = clean(w) ?: return null
        val ch = clean(h) ?: return null
        if (!(cw > 0) || !(ch > 0)) return null
        if (clean(cx + cw) == null || clean(cy + ch) == null) return null
        return PlanBox(cx, cy, cx + cw, cy + ch)
    }

    /** The result of [trace]. */
    class Trace(val points: List<PlanPoint>, val segments: List<PlanSegment>, val skipped: Int)

    /**
     * The points a drone flies through, starting at [start] and applying [lanes] in order.
     * Unusable lanes are skipped and counted.
     */
    fun trace(start: PlanPoint, lanes: List<PlanLane>): Trace {
        val points = arrayListOf(start)
        val segments = ArrayList<PlanSegment>()
        var skipped = 0
        var cur = start
        for (lane in lanes) {
            val direction = lane.direction
            val u = direction?.let(::unitVector)
            val d = clean(lane.distanceCm)
            if (direction == null || u == null || d == null || d < 0) {
                skipped++
                continue
            }
            val next = PlanPoint(cur.x + u.first * d, cur.y + u.second * d)
            if (abs(next.x) > MAX_ABS_CM || abs(next.y) > MAX_ABS_CM) {
                skipped++
                continue
            }
            points += next
            segments += PlanSegment(cur, next, direction)
            cur = next
        }
        return Trace(points, segments, skipped)
    }

    fun zoneDrawing(zone: PlanZone): ZoneDrawing {
        val rect = box(zone.xCm, zone.yCm, zone.wCm, zone.hCm)
        var start: PlanPoint? = null
        var fellBack = false
        val tx = clean(zone.takeoffXCm)
        val ty = clean(zone.takeoffYCm)
        val x = clean(zone.xCm)
        val y = clean(zone.yCm)
        if (tx != null && ty != null) {
            start = PlanPoint(tx, ty)
        } else if (x != null && y != null) {
            start = PlanPoint(x, y)
            fellBack = (zone.takeoffXCm != null && tx == null) || (zone.takeoffYCm != null && ty == null)
        }
        val allLanes = zone.lanes
        val lanes = allLanes.take(MAX_LANES_PER_ZONE)
        val dropped = allLanes.size - lanes.size
        var points: List<PlanPoint> = emptyList()
        var segments: List<PlanSegment> = emptyList()
        var skipped = 0
        if (start != null) {
            val t = trace(start, lanes)
            points = t.points
            segments = t.segments
            skipped += t.skipped
        } else {
            skipped += lanes.size
        }
        return ZoneDrawing(
            droneId = zone.droneId ?: "", altCm = clean(zone.altCm), lost = zone.lost ?: false,
            rect = rect, start = start, points = points, segments = segments, skippedLanes = skipped,
            droppedLanes = dropped, takeoffFellBack = fellBack,
        )
    }

    /** Everything the plan canvas draws, in the plan frame. Never fails: unusable data is skipped and counted. */
    fun drawing(plan: FleetPlan): PlanDrawing {
        val grid = plan.grid?.let { box(it.xCm, it.yCm, it.wCm, it.hCm) }
        var truncated = plan.zones.size > MAX_ZONES
        val zones = plan.zones.take(MAX_ZONES).map(::zoneDrawing)
        if (zones.any { it.droppedLanes > 0 }) truncated = true

        val lanes = ArrayList<UncoveredLaneDrawing>()
        val areas = ArrayList<UncoveredAreaDrawing>()
        var skippedUncovered = 0
        if (plan.uncovered.size > MAX_UNCOVERED_ENTRIES) truncated = true
        for (entry in plan.uncovered.take(MAX_UNCOVERED_ENTRIES)) {
            when (entry) {
                is UncoveredEntry.Area -> {
                    val r = box(entry.rect.xCm, entry.rect.yCm, entry.rect.wCm, entry.rect.hCm)
                    if (r != null) areas += UncoveredAreaDrawing(r, entry.reason) else skippedUncovered++
                }
                is UncoveredEntry.Lane -> {
                    val lane = uncoveredLane(entry.lane)
                    if (lane != null) lanes += lane else skippedUncovered++
                }
                is UncoveredEntry.Unknown -> skippedUncovered++
            }
        }

        var bounds: PlanBox? = grid
        fun add(b: PlanBox) {
            bounds = bounds?.union(b) ?: b
        }
        for (z in zones) {
            z.rect?.let(::add)
            for (p in z.points) add(PlanBox(p.x, p.y, p.x, p.y))
        }
        for (l in lanes) add(PlanBox(l.from.x, l.from.y, l.from.x, l.from.y).including(l.to))
        for (a in areas) add(a.rect)

        return PlanDrawing(
            grid = grid, bounds = bounds ?: fallbackBounds, zones = zones,
            uncoveredLanes = lanes, uncoveredAreas = areas,
            gapCm = clean(plan.gapCm), stripCm = clean(plan.stripCm),
            skippedLanes = zones.sumOf { it.skippedLanes },
            droppedLanes = zones.sumOf { it.droppedLanes },
            takeoffFallbackDroneIds = zones.filter { it.takeoffFellBack }.map { it.droneId },
            skippedUncovered = skippedUncovered, truncated = truncated,
        )
    }

    /**
     * An uncovered lane runs from (x_cm, y_cm) to (end_x_cm, end_y_cm). When the end is missing
     * it is derived from the direction and distance.
     */
    internal fun uncoveredLane(e: RetaskLane): UncoveredLaneDrawing? {
        val x = clean(e.xCm) ?: return null
        val y = clean(e.yCm) ?: return null
        val from = PlanPoint(x, y)
        var to: PlanPoint? = null
        val ex = clean(e.endXCm)
        val ey = clean(e.endYCm)
        if (ex != null && ey != null) {
            to = PlanPoint(ex, ey)
        } else {
            val u = e.direction?.let(::unitVector)
            val d = clean(e.distanceCm)
            if (u != null && d != null && d >= 0) {
                val p = PlanPoint(x + u.first * d, y + u.second * d)
                if (abs(p.x) <= MAX_ABS_CM && abs(p.y) <= MAX_ABS_CM) to = p
            }
        }
        return to?.let { UncoveredLaneDrawing(e.droneId, e.index, from, it, e.reason) }
    }

    /**
     * Fits [bounds] into a canvas of the given size with [padding] on every side, centred, with
     * one scale on both axes. A degenerate box is widened to [MIN_EXTENT_CM]. Hostile canvas
     * numbers give a zero scale, never NaN.
     */
    fun transform(bounds: PlanBox, canvasWidth: Double, canvasHeight: Double, padding: Double): PlanTransform {
        var minX = bounds.minX
        var maxX = bounds.maxX
        var minY = bounds.minY
        var maxY = bounds.maxY
        if (maxX - minX < MIN_EXTENT_CM) {
            val c = (minX + maxX) / 2
            minX = c - MIN_EXTENT_CM / 2
            maxX = c + MIN_EXTENT_CM / 2
        }
        if (maxY - minY < MIN_EXTENT_CM) {
            val c = (minY + maxY) / 2
            minY = c - MIN_EXTENT_CM / 2
            maxY = c + MIN_EXTENT_CM / 2
        }
        val b = PlanBox(minX, minY, maxX, maxY)
        val cw = if (canvasWidth.isFinite()) max(canvasWidth, 0.0) else 0.0
        val ch = if (canvasHeight.isFinite()) max(canvasHeight, 0.0) else 0.0
        val pad = if (padding.isFinite()) max(padding, 0.0) else 0.0
        val availW = max(cw - 2 * pad, 0.0)
        val availH = max(ch - 2 * pad, 0.0)
        val scale = min(availW / b.width, availH / b.height)
        val safeScale = if (scale.isFinite() && scale > 0) scale else 0.0
        return PlanTransform(
            bounds = b, scale = safeScale,
            originX = pad + (availW - b.width * safeScale) / 2,
            originY = pad + (availH - b.height * safeScale) / 2,
        )
    }

    /** The canvas height that fits [bounds] at a given canvas width, within `[minHeight, maxHeight]`. */
    fun fittedHeight(bounds: PlanBox, canvasWidth: Double, padding: Double, minHeight: Double, maxHeight: Double): Double {
        val w = max(bounds.width, MIN_EXTENT_CM)
        val h = max(bounds.height, MIN_EXTENT_CM)
        val pad = if (padding.isFinite()) max(padding, 0.0) else 0.0
        val availW = (if (canvasWidth.isFinite()) max(canvasWidth, 0.0) else 0.0) - 2 * pad
        val raw = if (availW > 0) availW * h / w + 2 * pad else minHeight
        if (!raw.isFinite()) return minHeight
        return min(max(raw, minHeight), maxHeight)
    }
}
