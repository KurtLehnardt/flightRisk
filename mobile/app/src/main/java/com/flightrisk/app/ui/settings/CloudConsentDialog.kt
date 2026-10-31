package com.flightrisk.app.ui.settings

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.flightrisk.app.llm.CloudConsentText
import com.flightrisk.app.ui.theme.AlertOrange

/**
 * The consent dialog shown before cloud reasoning can be turned on, from the
 * "Allow cloud reasoning" switch and when Cloud Claude is picked before opting
 * in. The words are [CloudConsentText], the same as iOS. Dismissing it (Cancel,
 * back, or tapping outside) changes nothing.
 */
@Composable
fun CloudConsentDialog(
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(CloudConsentText.TITLE) },
        text = { Text(CloudConsentText.MESSAGE) },
        confirmButton = {
            TextButton(onClick = onConfirm) { Text(CloudConsentText.CONFIRM) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text(CloudConsentText.CANCEL) }
        },
    )
}

/** The "Cloud active" indicator, shown wherever images can leave the phone. */
const val CLOUD_ACTIVE_TEXT = "Cloud active: images are sent to Anthropic"
private const val CLOUD_ACTIVE_DESCRIPTION =
    "Cloud reasoning active. Person crops, the reference photo and the typed description are sent to Anthropic."

@Composable
fun CloudActiveIndicator(modifier: Modifier = Modifier, onDark: Boolean = false) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier.semantics { contentDescription = CLOUD_ACTIVE_DESCRIPTION },
    ) {
        Icon(
            imageVector = Icons.Filled.Warning,
            contentDescription = null,
            tint = AlertOrange,
        )
        Spacer(modifier = Modifier.width(6.dp))
        Text(
            text = CLOUD_ACTIVE_TEXT,
            style = MaterialTheme.typography.bodySmall,
            fontWeight = FontWeight.SemiBold,
            color = if (onDark) Color.White else AlertOrange,
        )
    }
}

/**
 * The reasoning banner for the Search screen: the "Cloud active" indicator
 * while cloud reasoning is active, and the fallback notice when the selected
 * provider could not be used. Shows nothing otherwise.
 */
@Composable
fun ReasoningStatusBanner(cloudActive: Boolean, notice: String?, modifier: Modifier = Modifier) {
    if (!cloudActive && notice == null) return
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(Color.Black.copy(alpha = 0.7f), RoundedCornerShape(8.dp))
            .padding(horizontal = 12.dp, vertical = 8.dp),
    ) {
        if (cloudActive) CloudActiveIndicator(onDark = true)
        if (cloudActive && notice != null) Spacer(modifier = Modifier.height(4.dp))
        if (notice != null) {
            Text(text = notice, style = MaterialTheme.typography.bodySmall, color = Color.White)
        }
    }
}
