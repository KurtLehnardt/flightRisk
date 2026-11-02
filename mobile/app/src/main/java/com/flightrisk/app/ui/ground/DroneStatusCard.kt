package com.flightrisk.app.ui.ground

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.flightrisk.contract.ConnectionState

/**
 * One drone's status card: link chip, control chip with the HELD countdown,
 * battery, height, lane progress, the drone-link notice, stale and partial
 * markers, failures the ground reported for the drone, Land and (only when
 * the ground allows it) Resume search.
 *
 * Land is always enabled while the card exists, whatever the drone's state,
 * and it is never dimmed: only the values are greyed when they are not live.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun DroneStatusCard(
    card: DroneCardUi,
    onLand: () -> Unit,
    onResume: () -> Unit,
    onDismissAlert: () -> Unit,
    onDismissLandFeedback: () -> Unit,
    onDismissResumeFeedback: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f), RoundedCornerShape(12.dp))
            .padding(12.dp)
            .testTag("ground.card.${card.id}"),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Column(
            modifier = Modifier.alpha(if (card.isValuesStale) 0.75f else 1f),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(card.displayId, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold, maxLines = 1)
                Chip(card.linkChipText, linkColor(card), Modifier.testTag("ground.card.${card.id}.link"))
                card.controlChip?.let { Chip(it, if (card.isStateStale) GroundColors.ChipGrey else GroundColors.ChipOrange) }
            }
            FlowRow(horizontalArrangement = Arrangement.spacedBy(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Value("Battery", card.batteryText)
                Value("Height", card.altitudeText)
                card.laneText?.let { Value("Lane", it) }
            }
            if (card.controlText.isNotEmpty()) {
                Text(
                    card.controlText,
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = if (card.isHeldWithCountdown) FontWeight.SemiBold else FontWeight.Normal,
                    modifier = Modifier.testTag("ground.card.${card.id}.control"),
                )
            }
            card.linkNotice?.let {
                WarningText(it, Modifier.testTag("ground.card.${card.id}.linkNotice"))
            }
            if (card.isPartial) {
                Text(GroundText.PARTIAL_RECORD, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            card.staleNote?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, fontStyle = FontStyle.Italic, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        card.alertText?.let { text ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                WarningText(text, Modifier.weight(1f).testTag("ground.card.${card.id}.alert"), bold = true)
                TextButton(onClick = onDismissAlert) { Text("Dismiss") }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
            // Always enabled, never dimmed: Land works in every drone state.
            Button(
                onClick = onLand,
                enabled = true,
                colors = ButtonDefaults.buttonColors(containerColor = GroundColors.ChipBlue, contentColor = Color.White),
                modifier = Modifier
                    .testTag("ground.card.${card.id}.land")
                    .semantics { contentDescription = "${card.landTitle} ${card.displayId}" },
            ) { Text(card.landTitle) }
            if (card.canResume) {
                OutlinedButton(
                    onClick = onResume,
                    modifier = Modifier
                        .testTag("ground.card.${card.id}.resume")
                        .semantics { contentDescription = "Resume the search of ${card.id}" },
                ) { Text("Resume search") }
            }
        }
        FeedbackLine(card.landFeedback, card.landFeedbackText, "ground.card.${card.id}.landFeedback", onDismissLandFeedback)
        FeedbackLine(card.resumeFeedback, card.resumeFeedbackText, "ground.card.${card.id}.resumeFeedback", onDismissResumeFeedback)
    }
}

@Composable
private fun FeedbackLine(feedback: CommandFeedback?, text: String?, tag: String, onDismiss: () -> Unit) {
    if (feedback == null || text == null) return
    Row(verticalAlignment = Alignment.CenterVertically) {
        if (feedback.isWarning) {
            WarningText(text, Modifier.weight(1f).testTag(tag), bold = feedback.isFailure)
        } else {
            Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f).testTag(tag))
        }
        if (feedback.canDismiss) TextButton(onClick = onDismiss) { Text("Dismiss") }
    }
}

/** Grey while the whole state is stale: a live colour would claim a live link. */
private fun linkColor(card: DroneCardUi): Color = when {
    card.linkChipGreyed -> GroundColors.ChipGrey
    card.connection == ConnectionState.CONNECTED -> GroundColors.ChipGreen
    card.connection == ConnectionState.DOWN || card.connection == ConnectionState.LOST -> GroundColors.ChipOrange
    else -> GroundColors.ChipGrey
}

@Composable
internal fun Chip(text: String, color: Color, modifier: Modifier = Modifier) {
    Text(
        text,
        color = GroundColors.ChipText,
        style = MaterialTheme.typography.labelMedium,
        fontWeight = FontWeight.Bold,
        modifier = modifier
            .background(color, RoundedCornerShape(50))
            .padding(horizontal = 8.dp, vertical = 3.dp),
    )
}

@Composable
private fun Value(label: String, text: String) {
    Column(modifier = Modifier.semantics(mergeDescendants = true) {}) {
        Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(text, style = MaterialTheme.typography.bodyMedium)
    }
}

/**
 * A warning or failure text on the theme's error container, which keeps at
 * least 4.5:1 contrast in both light and dark mode (orange text on the grey
 * card background did not).
 */
@Composable
internal fun WarningText(text: String, modifier: Modifier = Modifier, bold: Boolean = false) {
    Text(
        text,
        style = MaterialTheme.typography.bodySmall,
        fontWeight = if (bold) FontWeight.Bold else FontWeight.Normal,
        color = MaterialTheme.colorScheme.onErrorContainer,
        modifier = modifier
            .background(MaterialTheme.colorScheme.errorContainer, RoundedCornerShape(6.dp))
            .padding(6.dp),
    )
}
