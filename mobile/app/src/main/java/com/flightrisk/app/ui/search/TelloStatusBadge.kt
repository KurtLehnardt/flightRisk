package com.flightrisk.app.ui.search

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.flightrisk.app.R
import com.flightrisk.app.drone.TelloConnectionState
import com.flightrisk.app.drone.TelloState
import com.flightrisk.app.ui.theme.HudWhite

/**
 * HUD pill showing drone connection state, matching the existing [HudPill]
 * styling in SearchScreen.
 *
 * Displays a colored status dot, connection state name, and when streaming,
 * battery percentage and height. When in error state, shows the error message.
 *
 * @param droneState Current drone state.
 * @param modifier Modifier for the root container.
 */
@Composable
fun TelloStatusBadge(
    droneState: TelloState,
    modifier: Modifier = Modifier,
) {
    val dotColor = when (droneState.connectionState) {
        TelloConnectionState.DISCONNECTED -> Color.Red
        TelloConnectionState.CONNECTING -> Color.Yellow
        TelloConnectionState.CONNECTED -> Color(0xFF2563EB) // Blue
        TelloConnectionState.STREAMING -> Color(0xFF16A34A) // Green
        TelloConnectionState.ERROR -> Color.Red
    }

    val stateText = when (droneState.connectionState) {
        TelloConnectionState.DISCONNECTED -> stringResource(R.string.drone_disconnected)
        TelloConnectionState.CONNECTING -> stringResource(R.string.drone_connecting)
        TelloConnectionState.CONNECTED -> stringResource(R.string.drone_connected)
        TelloConnectionState.STREAMING -> stringResource(R.string.drone_streaming)
        TelloConnectionState.ERROR -> stringResource(R.string.drone_error)
    }

    val detailText = buildString {
        append(stateText)
        when (droneState.connectionState) {
            TelloConnectionState.STREAMING -> {
                append(" | ")
                append(stringResource(R.string.drone_battery, droneState.telemetry.battery))
                append(" | ")
                append(stringResource(R.string.drone_height, droneState.telemetry.height))
            }
            TelloConnectionState.ERROR -> {
                droneState.errorMessage?.let { msg ->
                    append(": $msg")
                }
            }
            else -> { /* no extra detail */ }
        }
    }

    Row(
        modifier = modifier
            .background(
                color = Color(0xCC000000),
                shape = RoundedCornerShape(16.dp),
            )
            .padding(horizontal = 12.dp, vertical = 6.dp)
            .semantics { contentDescription = "Drone status: $detailText" },
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        // Status dot
        Box(
            modifier = Modifier
                .size(8.dp)
                .clip(CircleShape)
                .background(dotColor),
        )

        // State text
        Text(
            text = detailText,
            color = HudWhite,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Medium,
        )
    }
}
