package com.flightrisk.app.ui.ground

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import android.graphics.Bitmap
import kotlin.math.max

private val DetectionBlue = Color(0xFF38BDF8)
private val MatchGreen = Color(0xFF4ADE80)

/**
 * The Ground screen's video section: one tile per drone with detection boxes, side by side on a wide or
 * landscape screen. Read only: the phone shows the frames the ground sends and never asks the ground to
 * start or stop video. Nothing is drawn until the ground has sent a frame or said that video is off.
 */
@Composable
fun VideoTilesSection(state: GroundUiState) {
    val tiles = state.videoTiles
    if (tiles.isEmpty() && !state.videoOffAtGround) return
    val config = LocalConfiguration.current
    val columns = VideoTileLogic.columns(tiles.size, config.screenWidthDp.toFloat(), config.screenHeightDp.toFloat())
    Column(modifier = Modifier.fillMaxWidth().testTag("ground.video"), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Video", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
        if (tiles.isEmpty()) {
            Text(
                VideoTileLogic.VIDEO_OFF_TEXT,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.testTag("ground.video.off"),
            )
        } else {
            for (row in tiles.chunked(columns)) {
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
                    for (tile in row) {
                        key(tile.id) { Box(modifier = Modifier.weight(1f)) { VideoTile(tile) } }
                    }
                    // An odd last row keeps its tile at the same width as the others.
                    repeat(columns - row.size) { Box(modifier = Modifier.weight(1f)) }
                }
            }
        }
    }
}

@Composable
private fun VideoTile(tile: VideoTileModel) {
    val scope = rememberCoroutineScope()
    val decoder = remember { LatestFrameDecoder<Bitmap>(scope, Dispatchers.Default, ::decodeFrameBitmap, release = { it.recycle() }) }
    // Dropping the tile (scrolled away, drone gone, screen left) cancels any decode and drops the bitmap.
    DisposableEffect(decoder) { onDispose { decoder.close() } }
    val decoded by decoder.state.collectAsState()
    val density = LocalDensity.current

    BoxWithConstraints(
        modifier = Modifier
            .fillMaxWidth()
            .aspectRatio(tile.aspect.toFloat())
            .clip(RoundedCornerShape(8.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clearAndSetSemantics {
                contentDescription = tile.accessibilityText
            }
            .testTag("ground.video.tile.${tile.id}"),
    ) {
        val widthPx = with(density) { maxWidth.toPx() }
        val target = VideoTileLogic.targetPixelSize(widthPx)
        LaunchedEffect(tile.imageBase64, tile.mode, target) {
            // A stale tile has no image; a newer frame replaces a decode that is still running.
            decoder.submit(if (tile.mode == VideoTileMode.NO_VIDEO) null else tile.imageBase64, target)
        }
        val bitmap = if (tile.mode == VideoTileMode.NO_VIDEO) null else decoded.image
        if (bitmap != null) {
            Image(bitmap = bitmap.asImageBitmap(), contentDescription = null, contentScale = ContentScale.Fit, modifier = Modifier.fillMaxSize())
        }
        if (tile.mode != VideoTileMode.NO_VIDEO) {
            BoxLayer(tile, bitmap?.width, bitmap?.height)
        }
        Placeholder(tile, hasImage = bitmap != null, failed = decoded.failed)
        Badge(tile, Modifier.align(Alignment.TopStart))
    }
}

@Composable
private fun BoxLayer(tile: VideoTileModel, imageWidth: Int?, imageHeight: Int?) {
    if (tile.boxes.isEmpty()) return
    val measurer = rememberTextMeasurer(cacheSize = 32)
    Canvas(modifier = Modifier.fillMaxSize()) {
        val rect = VideoTileLogic.imageRect(imageWidth, imageHeight, size.width, size.height)
        for (box in tile.boxes) {
            val color = if (box.matched) MatchGreen else DetectionBlue
            val left = rect[0] + (box.x1 * rect[2]).toFloat()
            val top = rect[1] + (box.y1 * rect[3]).toFloat()
            val w = ((box.x2 - box.x1) * rect[2]).toFloat()
            val h = ((box.y2 - box.y1) * rect[3]).toFloat()
            drawRect(color, Offset(left, top), Size(w, h), style = Stroke(width = if (box.matched) 4f else 2f))
            val parts = buildList {
                if (box.matched) add("MATCH")
                box.trackId?.let { add("#$it") }
            }
            if (parts.isNotEmpty()) {
                val layout = measurer.measure(parts.joinToString(" "), TextStyle(color = color, fontSize = 11.sp, fontWeight = FontWeight.Bold), maxLines = 1)
                val pad = 3f
                val bgTop = max(top - layout.size.height - 2 * pad, rect[1])
                val bgLeft = left.coerceIn(rect[0], max(rect[0] + rect[2] - layout.size.width - 2 * pad, rect[0]))
                drawRect(Color.Black.copy(alpha = 0.7f), Offset(bgLeft, bgTop), Size(layout.size.width + 2 * pad, layout.size.height + 2 * pad))
                drawText(layout, topLeft = Offset(bgLeft + pad, bgTop + pad))
            }
        }
    }
}

@Composable
private fun Placeholder(tile: VideoTileModel, hasImage: Boolean, failed: Boolean) {
    val text: String? = when {
        tile.mode == VideoTileMode.NO_VIDEO -> null
        hasImage -> null
        tile.videoOffAtGround -> VideoTileLogic.VIDEO_OFF_TEXT
        failed -> VideoTileLogic.UNREADABLE_TEXT
        tile.imageBase64 == null -> VideoTileLogic.NO_IMAGE_TEXT
        else -> null
    }
    Column(
        modifier = Modifier.fillMaxSize().padding(8.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        if (tile.mode == VideoTileMode.NO_VIDEO) {
            Text(VideoTileLogic.NO_VIDEO_TITLE, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
            tile.noVideoReason?.let {
                Text(it.replaceFirstChar { c -> c.uppercase() }, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        } else if (text != null) {
            Text(text, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun Badge(tile: VideoTileModel, modifier: Modifier) {
    Row(
        modifier = modifier.padding(6.dp).background(Color.Black.copy(alpha = 0.65f), RoundedCornerShape(50)).padding(horizontal = 8.dp, vertical = 2.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Text(tile.droneLabel, style = MaterialTheme.typography.labelMedium, color = Color.White, fontWeight = FontWeight.SemiBold)
        if (tile.mode == VideoTileMode.NOT_LIVE) {
            Text(VideoTileLogic.NOT_LIVE_TAG, style = MaterialTheme.typography.labelMedium, color = Color.White, modifier = Modifier.testTag("ground.video.notLive.${tile.id}"))
        }
        if (tile.hiddenBoxCount > 0) {
            Text("+${tile.hiddenBoxCount} more", style = MaterialTheme.typography.labelMedium, color = Color.White)
        }
    }
}
