package com.flightrisk.app.ui.ground

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/**
 * Ceiling and altitude slots, read only. The ground decides every value; this card has no edit
 * controls and sends nothing.
 */
@Composable
fun CeilingCard(card: MissionFormat.CeilingCardUi, isStateStale: Boolean = false) {
    MissionCard("Ceiling and altitude", "ground.ceiling", MissionFormat.notLiveNote(isStateStale)) {
        Text(card.headline, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
        Text(card.maxSlotText, style = MaterialTheme.typography.bodySmall)
        card.lockedText?.let { Text(it, style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("ground.ceiling.locked")) }
        card.loadErrorText?.let { WarningText(it, Modifier.testTag("ground.ceiling.loadError"), bold = true) }
        for (row in card.drones) {
            Column(verticalArrangement = Arrangement.spacedBy(2.dp), modifier = Modifier.testTag("ground.ceiling.drone.${row.id}")) {
                Text("${row.displayId}: ${row.slotText}", style = MaterialTheme.typography.bodyMedium)
                row.problem?.let { WarningText(it, bold = true) }
                StatusLine(row.edgeText, row.edgeSeverity)
            }
        }
    }
}
