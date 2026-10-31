package com.flightrisk.app.ui.ground

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/** The frame every mission card shares: a titled, rounded block. */
@Composable
internal fun MissionCard(title: String, tag: String, content: @Composable () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f), RoundedCornerShape(12.dp))
            .padding(12.dp)
            .testTag(tag),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
        content()
    }
}

/** A status line: plain for information, on the error container for warnings and failures. */
@Composable
internal fun StatusLine(text: String, severity: Severity, modifier: Modifier = Modifier) {
    when (severity) {
        Severity.INFO -> Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = modifier)
        Severity.WARN -> WarningText(text, modifier)
        Severity.BAD -> WarningText(text, modifier, bold = true)
    }
}

/**
 * Detection profiles: the fleet default and each drone's choice. Selecting sends exactly one
 * command to the ground; the ground's answer (switching, active, fallback, refusal) shows in the
 * status line under each drone. The buttons are disabled while the state is not live or the
 * ground says its controls are off.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun ProfilesCard(
    card: ProfilesCardUi,
    feedback: ProfileFeedback?,
    onSelectDrone: (String, String) -> Unit,
    onSelectFleet: (String) -> Unit,
    onDismissFeedback: () -> Unit,
) {
    MissionCard("Detection profiles", "ground.profiles") {
        card.blockedNote?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("ground.profiles.blocked"))
        }
        Text(card.fleetDefaultText, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
        OptionRow(card.fleetOptions, card.canSelect, "ground.profiles.fleet", "Fleet default") { onSelectFleet(it) }
        for (row in card.drones) {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.testTag("ground.profiles.drone.${row.id}")) {
                Text(row.displayId, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.SemiBold)
                if (!row.supported) {
                    Text(MissionFormat.NOT_SUPPORTED, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
                } else {
                    OptionRow(row.options, row.canSelect, "ground.profiles.drone.${row.id}", row.displayId) { onSelectDrone(row.id, it) }
                }
                StatusLine(row.statusText, row.severity, Modifier.testTag("ground.profiles.status.${row.id}"))
            }
        }
        if (card.partial) {
            Text("Only part of the list is shown.", style = MaterialTheme.typography.bodySmall)
        }
        feedback?.let {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                StatusLine(it.text, if (it.isFailure) Severity.BAD else Severity.INFO, Modifier.testTag("ground.profiles.feedback"))
                if (it.isFailure) {
                    TextButton(onClick = onDismissFeedback, modifier = Modifier.testTag("ground.profiles.feedbackDismiss")) { Text("Dismiss") }
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun OptionRow(
    options: List<ProfileOption>,
    enabled: Boolean,
    tagPrefix: String,
    who: String,
    onSelect: (String) -> Unit,
) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        for (o in options) {
            val mod = Modifier
                .heightIn(min = 48.dp)
                .testTag("$tagPrefix.${o.value.ifEmpty { "none" }}")
                .semantics { selected = o.selected; contentDescription = "$who: ${o.label}" + if (o.selected) ", selected" else "" }
            if (o.selected) {
                Button(onClick = { onSelect(o.value) }, enabled = enabled, modifier = mod) { Text(o.label) }
            } else {
                OutlinedButton(onClick = { onSelect(o.value) }, enabled = enabled, modifier = mod) { Text(o.label) }
            }
        }
    }
}
