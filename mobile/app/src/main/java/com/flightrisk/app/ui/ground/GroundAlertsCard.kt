package com.flightrisk.app.ui.ground

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.layout.fillMaxWidth

/**
 * Match alerts the ground reported, read only: plain rows in the list, never a dialog or an overlay, so
 * nothing covers the pinned LAND ALL bar or a drone's Land button. There is no Confirm or Dismiss button
 * because the phone cannot answer an alert; the operator does that on the laptop.
 */
@Composable
fun GroundAlertsCard(ui: GroundAlertsUi) {
    if (ui.rows.isEmpty()) return
    MissionCard("Match alerts", "ground.alerts", ui.notLiveNote) {
        Text(GroundAlertsLog.NOTE, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        ui.rows.forEachIndexed { index, row ->
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .clearAndSetSemantics { contentDescription = row.accessibilitySummary }
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
                Text(row.statusText, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold)
                row.reminderText?.let { Text(it, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold) }
            }
        }
    }
}
