package com.flightrisk.app.ui.demo

import android.graphics.Bitmap
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.flightrisk.app.demo.DemoText

private val DemoAmber = Color(0xFFFFB300)
private val DemoInk = Color(0xFF1A1A1A)

/**
 * The banner shown above every screen while demo mode is on. It takes the status bar's space itself,
 * so the screens below it are pushed down, never covered: the Ground screen's LAND ALL bar stays in view.
 */
@Composable
fun DemoBanner(modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(DemoAmber)
            // The status bar and a display cutout at the top or the sides (landscape) never cover the text.
            .windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Top + WindowInsetsSides.Horizontal))
            .padding(horizontal = 16.dp, vertical = 6.dp)
            .semantics(mergeDescendants = true) {
                contentDescription = DemoText.BANNER + ". " + DemoText.BANNER_DETAIL
                liveRegion = LiveRegionMode.Polite
            }
            .testTag("demo.banner"),
    ) {
        Text(DemoText.BANNER, color = DemoInk, fontWeight = FontWeight.ExtraBold, style = MaterialTheme.typography.titleSmall)
        // At most two lines, so at a large font size the banner never pushes LAND ALL far down. The whole
        // text is in the banner's description for TalkBack.
        Text(
            DemoText.BANNER_DETAIL,
            color = DemoInk,
            style = MaterialTheme.typography.labelSmall,
            maxLines = BANNER_DETAIL_MAX_LINES,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/** The most lines the banner's detail takes. */
const val BANNER_DETAIL_MAX_LINES = 2

/**
 * The app's frame: the demo banner (while [demo] is on) above [content], which it pushes down and never
 * covers. The banner takes the top of the safe drawing area (status bar and cutout) itself, so the content
 * then skips that inset.
 */
@Composable
fun DemoFrame(demo: Boolean, modifier: Modifier = Modifier, content: @Composable BoxScope.() -> Unit) {
    Column(modifier = modifier.fillMaxSize()) {
        if (demo) DemoBanner()
        Box(
            modifier = Modifier
                .weight(1f)
                .then(if (demo) Modifier.consumeWindowInsets(WindowInsets.safeDrawing.only(WindowInsetsSides.Top)) else Modifier),
            content = content,
        )
    }
}

/**
 * The Target tab in demo mode: the drawn placeholder target and "Leave demo mode to choose a photo." It
 * has no photo picker and no camera button, so nothing outside the demo can be opened from it.
 */
@Composable
fun DemoTargetPlaceholder(target: Bitmap?, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp)
            .testTag("target.demoPlaceholder"),
        verticalArrangement = Arrangement.spacedBy(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        if (target != null) {
            Image(
                bitmap = target.asImageBitmap(),
                contentDescription = "Demo target: a drawn placeholder figure",
                modifier = Modifier.size(200.dp),
            )
        }
        Text(
            DemoText.TARGET_PLACEHOLDER,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
            textAlign = TextAlign.Center,
            modifier = Modifier.testTag("target.demoPlaceholder.text"),
        )
        Text(
            DemoText.TARGET_PLACEHOLDER_DETAIL,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }
}

/** The Settings switch for demo mode. */
@Composable
fun DemoModeSection(active: Boolean, onToggle: (Boolean) -> Unit, modifier: Modifier = Modifier) {
    Column(modifier = modifier.fillMaxWidth().testTag("settings.demo"), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(
            DemoText.SETTINGS_TITLE,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Switch(checked = active, onCheckedChange = onToggle, modifier = Modifier.testTag("settings.demo.switch"))
            Text(DemoText.SETTINGS_SWITCH, style = MaterialTheme.typography.bodyMedium)
        }
        Text(DemoText.SETTINGS_DETAIL, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** Asks before demo mode switches a real ground link off and closes it. */
@Composable
fun DemoDisconnectDialog(host: String?, onConfirm: () -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(DemoText.CONFIRM_TITLE) },
        text = { Text(DemoText.confirmText(host)) },
        confirmButton = { TextButton(onClick = onConfirm, modifier = Modifier.testTag("demo.confirm")) { Text(DemoText.CONFIRM_ACCEPT) } },
        dismissButton = { TextButton(onClick = onDismiss, modifier = Modifier.testTag("demo.cancel")) { Text(DemoText.CONFIRM_CANCEL) } },
    )
}

/** Says why demo mode did not start. */
@Composable
fun DemoBlockedDialog(text: String, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(DemoText.BLOCKED_TITLE) },
        text = { Text(text) },
        confirmButton = { TextButton(onClick = onDismiss) { Text("OK") } },
    )
}
