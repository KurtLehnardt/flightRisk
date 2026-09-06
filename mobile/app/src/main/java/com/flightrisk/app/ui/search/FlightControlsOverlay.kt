package com.flightrisk.app.ui.search

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.spring
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
import com.flightrisk.app.drone.PatternType
import com.flightrisk.app.ui.theme.AlertRed
import com.flightrisk.app.ui.theme.HudWhite
import com.flightrisk.app.ui.theme.MatchGreen

@Composable
fun FlightControlsOverlay(
    isFlying: Boolean,
    isSearching: Boolean,
    isExpanded: Boolean,
    selectedPattern: PatternType,
    onToggleExpanded: () -> Unit,
    onPatternSelected: (PatternType) -> Unit,
    onLand: () -> Unit,
    onMove: (direction: String, distanceCm: Int) -> Unit,
    onRotate: (degrees: Int) -> Unit,
    onStartSearch: () -> Unit,
    onStopSearch: () -> Unit,
    onEmergencyStop: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        // Collapse/expand toggle bar
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(
                    color = Color(0xCC1A1A1A),
                    shape = RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp),
                )
                .clickable(onClick = onToggleExpanded)
                .padding(horizontal = 16.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = if (isFlying) "Flight Controls" else "Drone Controls",
                color = HudWhite,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold,
            )
            Icon(
                imageVector = if (isExpanded) Icons.Default.KeyboardArrowDown
                    else Icons.Default.KeyboardArrowUp,
                contentDescription = if (isExpanded) "Collapse controls" else "Expand controls",
                tint = HudWhite,
            )
        }

        // Collapsible content
        AnimatedVisibility(
            visible = isExpanded,
            enter = expandVertically(
                animationSpec = spring(
                    dampingRatio = Spring.DampingRatioMediumBouncy,
                    stiffness = Spring.StiffnessMedium,
                ),
            ),
            exit = shrinkVertically(
                animationSpec = spring(stiffness = Spring.StiffnessMedium),
            ),
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(color = Color(0xAA000000))
                    .padding(16.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                // Search pattern selector (shown when not yet searching)
                if (!isSearching) {
                    SearchPatternSelector(
                        selectedPattern = selectedPattern,
                        onPatternSelected = onPatternSelected,
                    )
                    Spacer(modifier = Modifier.height(12.dp))
                }

                // Start/Stop Search button
                if (isSearching) {
                    Button(
                        onClick = onStopSearch,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = AlertRed,
                            contentColor = HudWhite,
                        ),
                        modifier = Modifier
                            .fillMaxWidth()
                            .sizeIn(minHeight = 48.dp),
                        shape = RoundedCornerShape(24.dp),
                    ) {
                        Text(
                            text = stringResource(R.string.drone_stop_search),
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                } else {
                    Button(
                        onClick = onStartSearch,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = MatchGreen,
                            contentColor = HudWhite,
                        ),
                        modifier = Modifier
                            .fillMaxWidth()
                            .sizeIn(minHeight = 48.dp),
                        shape = RoundedCornerShape(24.dp),
                    ) {
                        Text(
                            text = "Start Search",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }

                if (isFlying) {
                    Spacer(modifier = Modifier.height(12.dp))
                    // Full flight controls
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

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceEvenly,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
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

                        DirectionalDPad(onMove = onMove)
                    }

                    Spacer(modifier = Modifier.height(12.dp))

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
    }
}

@Composable
private fun SearchPatternSelector(
    selectedPattern: PatternType,
    onPatternSelected: (PatternType) -> Unit,
    modifier: Modifier = Modifier,
) {
    var expanded by remember { mutableStateOf(false) }

    Column(modifier = modifier.fillMaxWidth()) {
        Text(
            text = "Search Pattern",
            color = HudWhite,
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.Bold,
        )
        Spacer(modifier = Modifier.height(4.dp))
        Box {
            OutlinedButton(
                onClick = { expanded = true },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = HudWhite),
                shape = RoundedCornerShape(8.dp),
            ) {
                Text(
                    text = selectedPattern.displayName,
                    modifier = Modifier.weight(1f),
                )
                Icon(
                    imageVector = Icons.Default.KeyboardArrowDown,
                    contentDescription = null,
                    modifier = Modifier.size(18.dp),
                )
            }
            DropdownMenu(
                expanded = expanded,
                onDismissRequest = { expanded = false },
            ) {
                PatternType.entries.forEach { pattern ->
                    DropdownMenuItem(
                        text = {
                            Column {
                                Text(
                                    text = pattern.displayName,
                                    fontWeight = if (pattern == selectedPattern)
                                        FontWeight.Bold else FontWeight.Normal,
                                )
                                Text(
                                    text = patternDescription(pattern),
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        },
                        onClick = {
                            onPatternSelected(pattern)
                            expanded = false
                        },
                    )
                }
            }
        }
    }
}

private fun patternDescription(pattern: PatternType): String = when (pattern) {
    PatternType.EXPANDING_SQUARE -> "From last known position, expanding outward"
    PatternType.SECTOR -> "Pie-slice radial sweeps from center"
    PatternType.PARALLEL_TRACK -> "Lawnmower pattern, systematic coverage"
    PatternType.TRACK_LINE -> "Follow a path with side sweeps"
    PatternType.SPIRAL -> "Outward spiral from center point"
}

@Composable
private fun DirectionalDPad(
    onMove: (direction: String, distanceCm: Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier,
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            DirectionalButton(
                text = stringResource(R.string.drone_move_forward),
                onClick = { onMove("forward", 30) },
            )
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                DirectionalButton(
                    text = stringResource(R.string.drone_move_left),
                    onClick = { onMove("left", 30) },
                )
                Spacer(modifier = Modifier.size(56.dp))
                DirectionalButton(
                    text = stringResource(R.string.drone_move_right),
                    onClick = { onMove("right", 30) },
                )
            }
            DirectionalButton(
                text = stringResource(R.string.drone_move_back),
                onClick = { onMove("back", 30) },
            )
        }
    }
}

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
