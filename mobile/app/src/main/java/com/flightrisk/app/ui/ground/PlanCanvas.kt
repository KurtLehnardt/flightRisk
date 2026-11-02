package com.flightrisk.app.ui.ground

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.unit.Density
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.layout.layout
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.testTag
import androidx.compose.ui.text.TextMeasurer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.flightrisk.contract.PlanBox
import com.flightrisk.contract.PlanDrawing
import com.flightrisk.contract.PlanGeometry
import com.flightrisk.contract.PlanPoint
import com.flightrisk.contract.ZoneDrawing
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin

private const val ARROW_BUDGET = 600
private const val MIN_ARROW_SEGMENT_DP = 16f
private val MAX_CANVAS_HEIGHT = 420.dp

/**
 * A read-only picture of the fleet plan: the grid, each drone's zone and lane path with
 * direction arrows and a start marker, lost zones greyed and dashed with a LOST label, and
 * uncovered lanes and areas in the warning colour. There are no taps and nothing is sent. The
 * geometry comes from the contract module's [PlanGeometry]; this file only paints it.
 *
 * The card paints its own background so every colour keeps 4.5:1 contrast in both themes
 * ([PlanColors]).
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun PlanCanvasCard(ui: MissionFormat.PlanCanvasUi, modifier: Modifier = Modifier) {
    val dark = MaterialTheme.colorScheme.surface.luminance() < 0.5f
    val text = PlanColors.text(dark)
    val secondary = PlanColors.secondaryText(dark)
    val warning = PlanColors.warning(dark)
    val measurer = rememberTextMeasurer(cacheSize = 64)
    val summary = ui.accessibilitySummary
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(PlanColors.background(dark), RoundedCornerShape(12.dp))
            .padding(12.dp)
            .clearAndSetSemantics {
                testTag = "ground.plan"
                contentDescription = summary
            },
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(ui.title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold, color = text)
            if (ui.isNotLive) Text("not live", style = MaterialTheme.typography.labelSmall, color = secondary)
        }
        val d = ui.drawing
        if (d != null && !ui.isEmpty) {
            val aspect = aspectOf(d.bounds)
            val notLive = ui.isNotLive
            // The paths and labels are built once per drawing, size and theme; a redraw only replays them.
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .fixedAspect(aspect, MAX_CANVAS_HEIGHT)
                    .drawWithCache {
                        val ops = buildPlanOps(d, dark, notLive, measurer, size)
                        onDrawBehind { for (op in ops) op() }
                    },
            )
            Legend(d, ui, text, secondary, warning, dark)
        } else {
            Text(MissionFormat.EMPTY_TEXT, style = MaterialTheme.typography.bodySmall, color = secondary)
        }
    }
}

/** Width over height of the bounds, kept within a range so a long thin plan stays readable. */
internal fun aspectOf(b: PlanBox): Float {
    val w = max(b.width, PlanGeometry.MIN_EXTENT_CM)
    val h = max(b.height, PlanGeometry.MIN_EXTENT_CM)
    return (w / h).coerceIn(0.4, 2.5).toFloat()
}

/** Takes the full width, then a height of width / [aspect] capped at [maxHeight]. */
private fun Modifier.fixedAspect(aspect: Float, maxHeight: androidx.compose.ui.unit.Dp): Modifier =
    this.layout { measurable, constraints ->
        val w = if (constraints.hasBoundedWidth) constraints.maxWidth else 0
        val h = min((w / aspect).toInt(), maxHeight.roundToPx())
        val p = measurable.measure(androidx.compose.ui.unit.Constraints.fixed(w, max(h, 0)))
        layout(p.width, p.height) { p.placeRelative(0, 0) }
    }

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun Legend(d: PlanDrawing, ui: MissionFormat.PlanCanvasUi, text: Color, secondary: Color, warning: Color, dark: Boolean) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            for (z in d.zones.take(8)) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    Box(Modifier.size(8.dp).background(zoneColor(z, dark), CircleShape))
                    Text(if (z.droneId.isEmpty()) "zone" else GroundText.shortId(z.droneId), style = MaterialTheme.typography.labelSmall, color = text)
                }
            }
            if (d.zones.size > 8) Text("+${d.zones.size - 8}", style = MaterialTheme.typography.labelSmall, color = secondary)
        }
        if (d.hasLostZone) Text(MissionFormat.LOST_LEGEND, style = MaterialTheme.typography.labelSmall, color = secondary)
        if (d.uncoveredLanes.isNotEmpty()) {
            Text(MissionFormat.UNCOVERED_LEGEND, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold, color = warning)
            for (r in ui.laneReasons) Text(r, style = MaterialTheme.typography.labelSmall, color = secondary)
        }
        if (d.uncoveredAreas.isNotEmpty()) {
            Text(MissionFormat.AREA_LEGEND, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold, color = warning)
            for (r in ui.areaReasons) Text(r, style = MaterialTheme.typography.labelSmall, color = secondary)
        }
        ui.gapNote?.let { Text(it, style = MaterialTheme.typography.labelMedium, color = secondary) }
        ui.skippedNote?.let { Text(it, style = MaterialTheme.typography.labelMedium, color = secondary) }
    }
}

internal fun zoneColor(z: ZoneDrawing, dark: Boolean): Color =
    if (z.lost) PlanColors.lost(dark) else PlanColors.drone(MissionFormat.colorIndex(z.droneId), dark)

private fun Density.buildPlanOps(d: PlanDrawing, dark: Boolean, notLive: Boolean, measurer: TextMeasurer, size: Size): List<DrawScope.() -> Unit> {
    val ops = ArrayList<DrawScope.() -> Unit>()
    val pad = 18.dp.toPx()
    val t = PlanGeometry.transform(d.bounds, size.width.toDouble(), size.height.toDouble(), pad.toDouble())
    if (!(t.scale > 0)) return ops
    // Strokes and fills are dimmed while the plan is not live; labels stay opaque so they keep their contrast.
    fun dim(c: Color) = if (notLive) c.copy(alpha = c.alpha * PlanColors.DIM_ALPHA) else c
    val warning = dim(PlanColors.warning(dark))
    val lostGrey = dim(PlanColors.lost(dark))
    val background = PlanColors.background(dark)
    fun pt(p: PlanPoint): Offset {
        val m = t.map(p)
        return Offset(m.x.toFloat(), m.y.toFloat())
    }
    fun topLeftSize(b: PlanBox): Pair<Offset, Size> {
        val a = pt(PlanPoint(b.minX, b.maxY))
        val c = pt(PlanPoint(b.maxX, b.minY))
        return a to Size(c.x - a.x, c.y - a.y)
    }
    val dash = PathEffect.dashPathEffect(floatArrayOf(5.dp.toPx(), 4.dp.toPx()))
    val fineDash = PathEffect.dashPathEffect(floatArrayOf(3.dp.toPx(), 3.dp.toPx()))
    val minArrow = MIN_ARROW_SEGMENT_DP.dp.toPx()

    d.grid?.let {
        val (o, s) = topLeftSize(it)
        val c = dim(PlanColors.secondaryText(dark))
        ops += { drawRect(c, o, s, style = Stroke(1.5.dp.toPx())) }
    }
    for (area in d.uncoveredAreas) {
        val (o, s) = topLeftSize(area.rect)
        ops += {
            drawRect(warning.copy(alpha = warning.alpha * 0.22f), o, s, style = Fill)
            drawRect(warning, o, s, style = Stroke(1.dp.toPx(), pathEffect = fineDash))
        }
    }
    var arrows = 0
    for (z in d.zones) {
        val full = zoneColor(z, dark)
        val color = dim(full)
        z.rect?.let {
            val (o, s) = topLeftSize(it)
            if (z.lost) {
                ops += {
                    drawRect(lostGrey.copy(alpha = lostGrey.alpha * 0.15f), o, s, style = Fill)
                    drawRect(lostGrey, o, s, style = Stroke(1.5.dp.toPx(), pathEffect = dash))
                }
            } else {
                ops += {
                    drawRect(color.copy(alpha = color.alpha * 0.08f), o, s, style = Fill)
                    drawRect(color, o, s, style = Stroke(1.dp.toPx()))
                }
            }
        }
        if (z.points.size > 1) {
            val path = Path()
            val first = pt(z.points[0])
            path.moveTo(first.x, first.y)
            for (i in 1 until z.points.size) {
                val p = pt(z.points[i])
                path.lineTo(p.x, p.y)
            }
            val stroke = Stroke(
                2.5.dp.toPx(), cap = androidx.compose.ui.graphics.StrokeCap.Round, join = androidx.compose.ui.graphics.StrokeJoin.Round,
                pathEffect = if (z.lost) dash else null,
            )
            ops += { drawPath(path, color, style = stroke) }
            val arrowPath = Path()
            for (s in z.segments) {
                if (s.isZeroLength || arrows >= ARROW_BUDGET) continue
                val a = pt(s.from)
                val b = pt(s.to)
                if (hypot(b.x - a.x, b.y - a.y) < minArrow) continue
                addArrow(arrowPath, a, b, 5.dp.toPx())
                arrows++
            }
            ops += { drawPath(arrowPath, color) }
        }
        z.start?.let {
            val c = pt(it)
            val r = 6.dp.toPx()
            val w = Stroke(2.5.dp.toPx())
            ops += {
                drawCircle(background, r, c)
                drawCircle(color, r, c, style = w)
            }
        }
        z.rect?.let {
            val corner = pt(PlanPoint(it.minX, it.maxY))
            val id = GroundText.shortId(z.droneId)
            val label = if (z.lost) "$id LOST" else id
            val layout = measurer.measure(
                label, TextStyle(color = PlanColors.label(full), fontSize = 11.sp, fontWeight = FontWeight.SemiBold),
            )
            val at = Offset(corner.x + 3.dp.toPx(), corner.y + 3.dp.toPx())
            ops += { drawText(layout, topLeft = at) }
        }
    }
    for (lane in d.uncoveredLanes) {
        val from = pt(lane.from)
        val to = pt(lane.to)
        val w = 4.dp.toPx()
        val fx = PathEffect.dashPathEffect(floatArrayOf(2.dp.toPx(), 5.dp.toPx()))
        ops += { drawLine(warning, from, to, strokeWidth = w, cap = androidx.compose.ui.graphics.StrokeCap.Round, pathEffect = fx) }
    }
    return ops
}

private fun addArrow(p: Path, a: Offset, b: Offset, size: Float) {
    val mid = Offset((a.x + b.x) / 2, (a.y + b.y) / 2)
    val angle = atan2((b.y - a.y).toDouble(), (b.x - a.x).toDouble())
    p.moveTo(mid.x + (cos(angle) * size).toFloat(), mid.y + (sin(angle) * size).toFloat())
    p.lineTo(mid.x + (cos(angle + 2.5) * size).toFloat(), mid.y + (sin(angle + 2.5) * size).toFloat())
    p.lineTo(mid.x + (cos(angle - 2.5) * size).toFloat(), mid.y + (sin(angle - 2.5) * size).toFloat())
    p.close()
}

/** The plan canvas of the Ground screen: remembers the geometry for the plan value, so a new frame does not recompute it. */
@Composable
fun PlanCanvasFor(state: GroundUiState) {
    val plan = state.snapshot.fleetPlan
    val drawing = remember(plan) { plan?.let(PlanGeometry::drawing) }
    val ui = remember(drawing, state.isStateStale, state.planIsCurrent) {
        MissionFormat.planCanvas(plan, state.isStateStale, state.planIsCurrent, drawing)
    }
    PlanCanvasCard(ui)
}
