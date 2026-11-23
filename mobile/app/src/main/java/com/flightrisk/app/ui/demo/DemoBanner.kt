package com.flightrisk.app.ui.demo

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
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
            .windowInsetsPadding(WindowInsets.statusBars)
            .padding(horizontal = 16.dp, vertical = 6.dp)
            .semantics(mergeDescendants = true) {
                contentDescription = DemoText.BANNER + ". " + DemoText.BANNER_DETAIL
                liveRegion = LiveRegionMode.Polite
            }
            .testTag("demo.banner"),
    ) {
        Text(DemoText.BANNER, color = DemoInk, fontWeight = FontWeight.ExtraBold, style = MaterialTheme.typography.titleSmall)
        Text(DemoText.BANNER_DETAIL, color = DemoInk, style = MaterialTheme.typography.labelSmall)
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

/** Asks before demo mode closes a real ground link. */
@Composable
fun DemoDisconnectDialog(host: String, onConfirm: () -> Unit, onDismiss: () -> Unit) {
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
