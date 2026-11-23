package com.flightrisk.app.ui.ground

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

/**
 * Match alerts the ground reported, read only: plain rows in the list, never a dialog or an overlay, so
 * nothing covers the pinned LAND ALL bar or a drone's Land button. There is no Confirm or Dismiss button
 * because the phone cannot answer an alert; the operator does that on the laptop.
 *
 * A row with an AI check shows the verdict and a short reason; tapping it opens the detail in place
 * (confidence, the longer reason and whether the check raised the level). An error or a skipped check
 * is a muted note, never drawn as an error.
 */
@Composable
fun GroundAlertsCard(ui: GroundAlertsUi) {
    if (ui.rows.isEmpty()) return
    var expanded by remember { mutableStateOf(emptySet<String>()) }
    MissionCard("Match alerts", "ground.alerts", ui.notLiveNote) {
        Text(GroundAlertsLog.NOTE, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        ui.rows.forEachIndexed { index, row ->
            val open = row.id in expanded
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .then(
                        if (row.hasDetail) {
                            Modifier.clickable { expanded = if (open) expanded - row.id else expanded + row.id }
                        } else {
                            Modifier
                        },
                    )
                    .clearAndSetSemantics {
                        contentDescription = row.accessibilitySummary
                        if (row.hasDetail) {
                            stateDescription = if (open) "Details shown" else "Tap for details"
                            onClick(label = if (open) "Hide details" else "Show details") {
                                expanded = if (open) expanded - row.id else expanded + row.id
                                true
                            }
                        }
                    }
                    .testTag("ground.alerts.row.$index"),
                verticalArrangement = Arrangement.spacedBy(2.dp),
            ) {
                Row(horizontalArrangement = Arrangement.SpaceBetween, modifier = Modifier.fillMaxWidth()) {
                    Text(row.levelText, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                    Text(row.timeText, style = MaterialTheme.typography.labelMedium)
                }
                row.trackText?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                Text(row.scoreText, style = MaterialTheme.typography.bodySmall)
                row.reasonText?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                row.verdictText?.let {
                    Text(
                        it,
                        style = MaterialTheme.typography.bodySmall,
                        fontWeight = if (row.verdictIsNote) FontWeight.Normal else FontWeight.SemiBold,
                        color = if (row.verdictIsNote) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface,
                        modifier = Modifier.testTag("ground.alerts.row.$index.verdict"),
                    )
                }
                if (!open) {
                    row.rationaleText?.let {
                        Text(it, style = MaterialTheme.typography.bodySmall, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    }
                    row.levelRaisedText?.let { Text(it, style = MaterialTheme.typography.labelMedium) }
                } else {
                    Column(
                        modifier = Modifier.testTag("ground.alerts.row.$index.detail"),
                        verticalArrangement = Arrangement.spacedBy(2.dp),
                    ) {
                        row.confidenceText?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                        row.detailRationaleText?.let { Text("Reason given: $it", style = MaterialTheme.typography.bodySmall) }
                        row.levelRaisedText?.let { Text(it, style = MaterialTheme.typography.labelMedium) }
                        Text(
                            "The AI check is advice to the operator. Answer the alert on the laptop.",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                Text(row.statusText, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold)
                row.reminderText?.let { Text(it, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold) }
            }
        }
    }
}
