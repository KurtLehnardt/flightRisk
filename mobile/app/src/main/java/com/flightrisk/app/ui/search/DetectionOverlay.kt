package com.flightrisk.app.ui.search

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.flightrisk.app.ui.theme.DetectionBlue
import com.flightrisk.app.ui.theme.MatchGreen

/**
 * A single bounding box to draw on the detection overlay.
 *
 * @property bbox Pixel coordinates [x1, y1, x2, y2] in the source frame.
 * @property trackId Optional stable track identifier for labelling.
 * @property isMatch Whether this detection is a match (green) or not (blue).
 */
data class BoundingBox(
    val bbox: IntArray,
    val trackId: Int? = null,
    val isMatch: Boolean = false,
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is BoundingBox) return false
        return bbox.contentEquals(other.bbox) &&
            trackId == other.trackId &&
            isMatch == other.isMatch
    }

    override fun hashCode(): Int {
        var result = bbox.contentHashCode()
        result = 31 * result + (trackId ?: 0)
        result = 31 * result + isMatch.hashCode()
        return result
    }
}

/**
 * Overlay composable that draws bounding boxes over the camera preview.
 *
 * Blue rectangles indicate detected persons; green rectangles with a
 * thicker stroke indicate matched persons. Track ID labels are drawn
 * above each box.
 *
 * The overlay scales bounding box coordinates from the source frame
 * dimensions ([frameWidth] x [frameHeight]) to the composable's layout
 * size, so boxes align correctly regardless of preview scaling.
 *
 * @param boxes List of bounding boxes to draw.
 * @param frameWidth Width of the source camera frame in pixels.
 * @param frameHeight Height of the source camera frame in pixels.
 * @param modifier Modifier for the canvas.
 */
@Composable
fun DetectionOverlay(
    boxes: List<BoundingBox>,
    frameWidth: Int,
    frameHeight: Int,
    modifier: Modifier = Modifier,
) {
    val detectedCount = boxes.count { !it.isMatch }
    val matchedCount = boxes.count { it.isMatch }
    val description = buildString {
        append("Detection overlay: ")
        append("$detectedCount person${if (detectedCount != 1) "s" else ""} detected")
        if (matchedCount > 0) {
            append(", $matchedCount match${if (matchedCount != 1) "es" else ""}")
        }
    }

    Canvas(
        modifier = modifier
            .fillMaxSize()
            .semantics { contentDescription = description }
    ) {
        if (frameWidth <= 0 || frameHeight <= 0) return@Canvas

        val scaleX = size.width / frameWidth.toFloat()
        val scaleY = size.height / frameHeight.toFloat()

        for (box in boxes) {
            val (x1, y1, x2, y2) = box.bbox.map { it.toFloat() }

            val left = x1 * scaleX
            val top = y1 * scaleY
            val right = x2 * scaleX
            val bottom = y2 * scaleY
            val boxWidth = right - left
            val boxHeight = bottom - top

            val color: Color
            val strokeWidth: Float

            if (box.isMatch) {
                color = MatchGreen
                strokeWidth = 4.dp.toPx()
            } else {
                color = DetectionBlue
                strokeWidth = 2.dp.toPx()
            }

            // Draw bounding box rectangle
            drawRect(
                color = color,
                topLeft = Offset(left, top),
                size = Size(boxWidth, boxHeight),
                style = Stroke(width = strokeWidth),
            )

            // Draw track ID label above the box
            if (box.trackId != null) {
                val labelText = "#${box.trackId}"
                val paint = android.graphics.Paint().apply {
                    this.color = if (box.isMatch) {
                        android.graphics.Color.parseColor("#16A34A")
                    } else {
                        android.graphics.Color.parseColor("#2563EB")
                    }
                    textSize = 14.dp.toPx()
                    isAntiAlias = true
                    typeface = android.graphics.Typeface.DEFAULT_BOLD
                }

                // Background for readability
                val bgPaint = android.graphics.Paint().apply {
                    this.color = android.graphics.Color.argb(180, 0, 0, 0)
                    style = android.graphics.Paint.Style.FILL
                }

                val textWidth = paint.measureText(labelText)
                val textHeight = paint.textSize
                val padding = 4.dp.toPx()

                drawContext.canvas.nativeCanvas.drawRect(
                    left,
                    top - textHeight - padding * 2,
                    left + textWidth + padding * 2,
                    top,
                    bgPaint,
                )

                drawContext.canvas.nativeCanvas.drawText(
                    labelText,
                    left + padding,
                    top - padding,
                    paint,
                )
            }
        }
    }
}
