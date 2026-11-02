package com.flightrisk.app.ui.ground

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight

/**
 * The reasoning provider the ground uses, read only. The phone cannot switch it and cannot opt
 * the ground into cloud use; only the ground's own setting does that.
 */
@Composable
fun ReasoningCard(card: MissionFormat.ReasoningCardUi, isStateStale: Boolean = false) {
    MissionCard("Reasoning", "ground.reasoning", MissionFormat.notLiveNote(isStateStale)) {
        Text(card.statusText, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
        Text(card.cloudBadge, style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.Bold, modifier = Modifier.testTag("ground.reasoning.cloud"))
        card.availableText?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        for (note in card.unavailableNotes) {
            Text(note, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
