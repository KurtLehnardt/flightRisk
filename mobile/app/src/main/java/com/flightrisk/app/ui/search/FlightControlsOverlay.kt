package com.flightrisk.app.ui.search

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.flightrisk.app.R
import com.flightrisk.app.ui.theme.AlertRed
import com.flightrisk.app.ui.theme.HudWhite
import com.flightrisk.app.ui.theme.MatchGreen

/**
 * Bottom-anchored flight controls overlay for drone piloting.
 *
 * When not flying, shows only a Takeoff button (simplified view).
 * After takeoff, expands to show full directional controls, altitude
 * controls, rotation buttons, and an emergency land button.
 *
 * All buttons meet the Material 3 minimum 48dp touch target guideline.
 * Directional buttons are 56dp for easier one-handed operation.
 * Move commands use 30cm increments; rotate commands use 45-degree increments.
 *
 * @param isFlying Whether the drone is currently airborne.
 * @param onTakeoff Callback to initiate takeoff.
 * @param onLand Callback to initiate landing.
 * @param onMove Callback for directional movement (direction, distanceCm).
 * @param onRotate Callback for rotation (degrees; positive=CW, negative=CCW).
 * @param onStopSearch Callback to stop the AI search pipeline.
 * @param onEmergencyStop Callback for emergency motor stop (kills motors immediately).
 * @param modifier Modifier for the root container.
 */
@Composable
fun FlightControlsOverlay(
    isFlying: Boolean,
    onTakeoff: () -> Unit,
    onLand: () -> Unit,
    onMove: (direction: String, distanceCm: Int) -> Unit,
    onRotate: (degrees: Int) -> Unit,
    onStopSearch: () -> Unit,
    onEmergencyStop: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(
                color = Color(0xAA000000),
                shape = RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp),
            )
            .padding(16.dp)
            .semantics { contentDescription = "Flight controls" },
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        // Stop Search button — always visible during streaming
        TextButton(
            onClick = onStopSearch,
            modifier = Modifier.sizeIn(minHeight = 48.dp),
        ) {
            Text(stringResource(R.string.drone_stop_search), color = AlertRed)
        }

        if (!isFlying) {
            // Simplified view: only Takeoff button
            Box(
                modifier = Modifier.fillMaxWidth(),
                contentAlignment = Alignment.Center,
            ) {
                val takeoffText = stringResource(R.string.drone_takeoff)
                Button(
                    onClick = onTakeoff,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MatchGreen,
                        contentColor = HudWhite,
                    ),
                    modifier = Modifier
                        .size(64.dp)
                        .semantics { contentDescription = takeoffText },
                    shape = CircleShape,
                ) {
                    Text(
                        text = takeoffText,
                        style = MaterialTheme.typography.labelSmall,
                        fontWeight = FontWeight.Bold,
                    )
                }
            }
        } else {
            // Full controls layout
            Column(
                modifier = Modifier.fillMaxWidth(),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                // Emergency Land button (top-right)
                Box(modifier = Modifier.fillMaxWidth()) {
                    val emergencyText = stringResource(R.string.drone_emergency_land)
                    Button(
                        onClick = onEmergencyStop,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = AlertRed,
                            contentColor = HudWhite,
                        ),
                        modifier = Modifier
                            .align(Alignment.TopEnd)
                            .sizeIn(minWidth = 48.dp, minHeight = 48.dp)
                            .semantics { contentDescription = emergencyText },
                        shape = RoundedCornerShape(8.dp),
                    ) {
                        Text(
                            text = emergencyText,
                            style = MaterialTheme.typography.labelSmall,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }

                Spacer(modifier = Modifier.height(12.dp))

                // Main controls row: Altitude | Land | D-pad
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceEvenly,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    // Left zone: Altitude controls (Up/Down stacked)
                    Column(
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        DirectionalButton(
                            text = stringResource(R.string.drone_altitude_up),
                            onClick = { onMove("up", 30) },
                        )
                        DirectionalButton(
                            text = stringResource(R.string.drone_altitude_down),
                            onClick = { onMove("down", 30) },
                        )
                    }

                    // Center: Land button
                    val landText = stringResource(R.string.drone_land)
                    Button(
                        onClick = onLand,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = AlertRed,
                            contentColor = HudWhite,
                        ),
                        modifier = Modifier
                            .size(64.dp)
                            .semantics { contentDescription = landText },
                        shape = CircleShape,
                    ) {
                        Text(
                            text = landText,
                            style = MaterialTheme.typography.labelSmall,
                            fontWeight = FontWeight.Bold,
                        )
                    }

                    // Right zone: Directional D-pad (cross pattern)
                    DirectionalDPad(
                        onMove = onMove,
                    )
                }

                Spacer(modifier = Modifier.height(12.dp))

                // Rotate buttons
                Row(
                    horizontalArrangement = Arrangement.spacedBy(16.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    DirectionalButton(
                        text = stringResource(R.string.drone_rotate_ccw),
                        onClick = { onRotate(-45) },
                    )
                    DirectionalButton(
                        text = stringResource(R.string.drone_rotate_cw),
                        onClick = { onRotate(45) },
                    )
                }
            }
        }

        // Safety disclaimer — always visible during streaming
        Text(
            text = stringResource(R.string.drone_search_disclaimer),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 4.dp),
        )
    }
}

/**
 * Cross-pattern D-pad for forward/back/left/right movement.
 */
@Composable
private fun DirectionalDPad(
    onMove: (direction: String, distanceCm: Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier,
        contentAlignment = Alignment.Center,
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            // Forward
            DirectionalButton(
                text = stringResource(R.string.drone_move_forward),
                onClick = { onMove("forward", 30) },
            )

            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                // Left
                DirectionalButton(
                    text = stringResource(R.string.drone_move_left),
                    onClick = { onMove("left", 30) },
                )

                // Spacer in the center (same size as a button for the cross layout)
                Spacer(modifier = Modifier.size(56.dp))

                // Right
                DirectionalButton(
                    text = stringResource(R.string.drone_move_right),
                    onClick = { onMove("right", 30) },
                )
            }

            // Back
            DirectionalButton(
                text = stringResource(R.string.drone_move_back),
                onClick = { onMove("back", 30) },
            )
        }
    }
}

/**
 * A single 56dp directional control button with accessible touch target.
 */
@Composable
private fun DirectionalButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    FilledTonalButton(
        onClick = onClick,
        modifier = modifier
            .sizeIn(minWidth = 56.dp, minHeight = 56.dp)
            .semantics { contentDescription = text },
        shape = RoundedCornerShape(8.dp),
        colors = ButtonDefaults.filledTonalButtonColors(
            containerColor = Color(0x66FFFFFF),
            contentColor = HudWhite,
        ),
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Medium,
        )
    }
}
