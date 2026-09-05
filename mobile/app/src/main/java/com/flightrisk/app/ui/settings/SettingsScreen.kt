package com.flightrisk.app.ui.settings

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.flightrisk.app.config.SensitivityPreset
import com.flightrisk.app.drone.FrameSourceMode
import com.flightrisk.app.drone.TelloConnectionState
import com.flightrisk.app.ui.theme.AlertOrange
import com.flightrisk.app.ui.theme.AlertRed
import com.flightrisk.app.ui.theme.DetectionBlue
import com.flightrisk.app.ui.theme.MatchGreen

/**
 * UI state for the settings screen.
 *
 * @property activePreset Currently selected sensitivity preset, or null
 *   if manual thresholds override a preset.
 * @property reidThreshold Current ReID threshold (0.0-1.0).
 * @property faceThreshold Current face match threshold (0.0-1.0).
 * @property scorerThreshold Current scorer match threshold (0.0-1.0).
 * @property llmBackend Currently selected LLM backend name.
 * @property llmApiKey Current API key for cloud LLM.
 * @property llmAvailable Whether the selected LLM backend is available.
 * @property frameSourceMode Current frame source (camera or drone).
 * @property droneConnectionState Current Tello connection state.
 * @property droneBattery Current Tello battery percentage (0-100).
 */
data class SettingsScreenState(
    val activePreset: SensitivityPreset? = SensitivityPreset.BALANCED,
    val reidThreshold: Float = 0.55f,
    val faceThreshold: Float = 0.45f,
    val scorerThreshold: Float = 0.45f,
    val llmBackend: String = "cloud_claude",
    val llmApiKey: String = "",
    val llmAvailable: Boolean = false,
    val frameSourceMode: FrameSourceMode = FrameSourceMode.CAMERA,
    val droneConnectionState: TelloConnectionState = TelloConnectionState.DISCONNECTED,
    val droneBattery: Int = 0,
)

/**
 * Settings screen with sensitivity presets, LLM backend selection,
 * and advanced threshold controls.
 *
 * @param state Current settings state.
 * @param onPresetSelected Callback when a sensitivity preset is selected.
 * @param onThresholdChanged Callback when a raw threshold is changed.
 *   Params: (name, value) where name is "reid", "face", or "scorer".
 * @param onLlmBackendChanged Callback when the LLM backend is changed.
 * @param onApiKeyChanged Callback when the API key is changed.
 * @param onFrameSourceChanged Callback when the frame source mode is changed.
 * @param modifier Modifier for the root container.
 */
@Composable
fun SettingsScreen(
    state: SettingsScreenState,
    onPresetSelected: (SensitivityPreset) -> Unit,
    onThresholdChanged: (String, Float) -> Unit,
    onLlmBackendChanged: (String) -> Unit,
    onApiKeyChanged: (String) -> Unit,
    onFrameSourceChanged: (FrameSourceMode) -> Unit = {},
    modifier: Modifier = Modifier,
) {
    Scaffold(modifier = modifier) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(innerPadding)
                .padding(24.dp),
        ) {
            // Title
            Text(
                text = "Settings",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.semantics { heading() },
            )

            Spacer(modifier = Modifier.height(24.dp))

            // ----- Sensitivity section -----
            SensitivitySection(
                activePreset = state.activePreset,
                onPresetSelected = onPresetSelected,
            )

            Spacer(modifier = Modifier.height(24.dp))
            HorizontalDivider()
            Spacer(modifier = Modifier.height(24.dp))

            // ----- LLM Backend section -----
            LlmBackendSection(
                selectedBackend = state.llmBackend,
                apiKey = state.llmApiKey,
                isAvailable = state.llmAvailable,
                onBackendChanged = onLlmBackendChanged,
                onApiKeyChanged = onApiKeyChanged,
            )

            Spacer(modifier = Modifier.height(24.dp))
            HorizontalDivider()
            Spacer(modifier = Modifier.height(24.dp))

            // ----- Drone section -----
            DroneSection(
                frameSourceMode = state.frameSourceMode,
                connectionState = state.droneConnectionState,
                battery = state.droneBattery,
                onFrameSourceChanged = onFrameSourceChanged,
            )

            Spacer(modifier = Modifier.height(24.dp))
            HorizontalDivider()
            Spacer(modifier = Modifier.height(24.dp))

            // ----- Advanced section -----
            AdvancedSection(
                reidThreshold = state.reidThreshold,
                faceThreshold = state.faceThreshold,
                scorerThreshold = state.scorerThreshold,
                onThresholdChanged = onThresholdChanged,
            )
        }
    }
}

// -----------------------------------------------------------------------
// Sensitivity section
// -----------------------------------------------------------------------

/**
 * Three large pill buttons for sensitivity presets.
 */
@Composable
private fun SensitivitySection(
    activePreset: SensitivityPreset?,
    onPresetSelected: (SensitivityPreset) -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier) {
        Text(
            text = "Sensitivity",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )

        Spacer(modifier = Modifier.height(4.dp))

        Text(
            text = "Controls how aggressively the system alerts on potential matches.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(modifier = Modifier.height(16.dp))

        Column(
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            SensitivityPill(
                title = "More Alerts",
                description = "Lower thresholds. More potential matches, but higher false-positive rate.",
                isActive = activePreset == SensitivityPreset.MORE_ALERTS,
                onClick = { onPresetSelected(SensitivityPreset.MORE_ALERTS) },
            )

            SensitivityPill(
                title = "Balanced (Recommended)",
                description = "Default thresholds. Good balance between recall and precision.",
                isActive = activePreset == SensitivityPreset.BALANCED,
                onClick = { onPresetSelected(SensitivityPreset.BALANCED) },
            )

            SensitivityPill(
                title = "Fewer Alerts",
                description = "Higher thresholds. Fewer alerts, but stronger confidence per match.",
                isActive = activePreset == SensitivityPreset.FEWER_ALERTS,
                onClick = { onPresetSelected(SensitivityPreset.FEWER_ALERTS) },
            )
        }
    }
}

/**
 * A single sensitivity preset pill button.
 */
@Composable
private fun SensitivityPill(
    title: String,
    description: String,
    isActive: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val borderColor = if (isActive) MatchGreen else MaterialTheme.colorScheme.outline
    val bgColor = if (isActive) {
        MatchGreen.copy(alpha = 0.1f)
    } else {
        Color.Transparent
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .sizeIn(minHeight = 64.dp)
            .border(
                width = if (isActive) 2.dp else 1.dp,
                color = borderColor,
                shape = RoundedCornerShape(12.dp),
            )
            .background(
                color = bgColor,
                shape = RoundedCornerShape(12.dp),
            )
            .clickable(onClick = onClick)
            .padding(16.dp)
            .semantics {
                contentDescription = "$title preset" +
                    if (isActive) " (currently selected)" else ""
            },
    ) {
        Column {
            Text(
                text = title,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold,
                color = if (isActive) MatchGreen else MaterialTheme.colorScheme.onSurface,
            )
            Spacer(modifier = Modifier.height(2.dp))
            Text(
                text = description,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

// -----------------------------------------------------------------------
// LLM Backend section
// -----------------------------------------------------------------------

/**
 * LLM backend selection with radio buttons and API key input.
 */
@Composable
private fun LlmBackendSection(
    selectedBackend: String,
    apiKey: String,
    isAvailable: Boolean,
    onBackendChanged: (String) -> Unit,
    onApiKeyChanged: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier) {
        Text(
            text = "LLM Backend",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )

        Spacer(modifier = Modifier.height(4.dp))

        Text(
            text = "Select the reasoning backend for match verification.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(modifier = Modifier.height(16.dp))

        // Cloud Claude
        LlmBackendOption(
            name = "Cloud Claude",
            backendId = "cloud_claude",
            isSelected = selectedBackend == "cloud_claude",
            onSelect = { onBackendChanged("cloud_claude") },
        )

        // None (vision only)
        LlmBackendOption(
            name = "None (Vision Only)",
            backendId = "none",
            isSelected = selectedBackend == "none",
            onSelect = { onBackendChanged("none") },
        )

        // API key input (only for cloud)
        if (selectedBackend == "cloud_claude") {
            Spacer(modifier = Modifier.height(12.dp))

            OutlinedTextField(
                value = apiKey,
                onValueChange = onApiKeyChanged,
                label = { Text("Claude API Key") },
                placeholder = { Text("sk-ant-...") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                modifier = Modifier
                    .fillMaxWidth()
                    .sizeIn(minHeight = 48.dp),
            )

            Spacer(modifier = Modifier.height(8.dp))

            // Status indicator
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(8.dp)
                        .background(
                            color = if (isAvailable) MatchGreen else AlertRed,
                            shape = RoundedCornerShape(4.dp),
                        ),
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = if (isAvailable) "Available" else "Unavailable",
                    style = MaterialTheme.typography.bodySmall,
                    color = if (isAvailable) MatchGreen else AlertRed,
                    modifier = Modifier.semantics {
                        contentDescription = "LLM status: ${if (isAvailable) "available" else "unavailable"}"
                    },
                )
            }
        }
    }
}

/**
 * A single LLM backend radio option.
 */
@Composable
private fun LlmBackendOption(
    name: String,
    backendId: String,
    isSelected: Boolean,
    onSelect: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .sizeIn(minHeight = 48.dp)
            .clickable(onClick = onSelect)
            .padding(vertical = 4.dp)
            .semantics {
                contentDescription = "$name" +
                    if (isSelected) " (selected)" else ""
            },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(
            selected = isSelected,
            onClick = onSelect,
        )
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            text = name,
            style = MaterialTheme.typography.bodyLarge,
        )
    }
}

// -----------------------------------------------------------------------
// Drone section
// -----------------------------------------------------------------------

/**
 * Frame source selection and drone connection status.
 */
@Composable
private fun DroneSection(
    frameSourceMode: FrameSourceMode,
    connectionState: TelloConnectionState,
    battery: Int,
    onFrameSourceChanged: (FrameSourceMode) -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier) {
        Text(
            text = "Drone",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )

        Spacer(modifier = Modifier.height(4.dp))

        Text(
            text = "Select the video source for detection.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(modifier = Modifier.height(16.dp))

        // Phone Camera option
        FrameSourceOption(
            name = "Phone Camera",
            mode = FrameSourceMode.CAMERA,
            isSelected = frameSourceMode == FrameSourceMode.CAMERA,
            onSelect = { onFrameSourceChanged(FrameSourceMode.CAMERA) },
        )

        // Tello Drone option
        FrameSourceOption(
            name = "Tello Drone",
            mode = FrameSourceMode.DRONE,
            isSelected = frameSourceMode == FrameSourceMode.DRONE,
            onSelect = { onFrameSourceChanged(FrameSourceMode.DRONE) },
        )

        // Show connection status when drone mode is selected
        if (frameSourceMode == FrameSourceMode.DRONE) {
            Spacer(modifier = Modifier.height(12.dp))

            // Connection state indicator
            Row(verticalAlignment = Alignment.CenterVertically) {
                val statusColor = when (connectionState) {
                    TelloConnectionState.CONNECTED,
                    TelloConnectionState.STREAMING -> MatchGreen
                    TelloConnectionState.CONNECTING -> AlertOrange
                    TelloConnectionState.ERROR -> AlertRed
                    TelloConnectionState.DISCONNECTED -> MaterialTheme.colorScheme.onSurfaceVariant
                }
                val statusText = when (connectionState) {
                    TelloConnectionState.DISCONNECTED -> "Disconnected"
                    TelloConnectionState.CONNECTING -> "Connecting..."
                    TelloConnectionState.CONNECTED -> "Connected"
                    TelloConnectionState.STREAMING -> "Streaming"
                    TelloConnectionState.ERROR -> "Error"
                }

                Box(
                    modifier = Modifier
                        .size(8.dp)
                        .background(
                            color = statusColor,
                            shape = RoundedCornerShape(4.dp),
                        ),
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = statusText,
                    style = MaterialTheme.typography.bodySmall,
                    color = statusColor,
                    modifier = Modifier.semantics {
                        contentDescription = "Drone status: $statusText"
                    },
                )
            }

            // Battery level when connected
            if (connectionState == TelloConnectionState.CONNECTED ||
                connectionState == TelloConnectionState.STREAMING
            ) {
                Spacer(modifier = Modifier.height(4.dp))

                val batteryColor = when {
                    battery > 50 -> MatchGreen
                    battery > 20 -> AlertOrange
                    else -> AlertRed
                }

                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = "Battery: $battery%",
                        style = MaterialTheme.typography.bodySmall,
                        color = batteryColor,
                        modifier = Modifier.semantics {
                            contentDescription = "Drone battery: $battery percent"
                        },
                    )
                }
            }

            Spacer(modifier = Modifier.height(8.dp))

            Text(
                text = "Connect to the Tello's WiFi network, then use the drone button on the Search screen to connect.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * A single frame source radio option.
 */
@Composable
private fun FrameSourceOption(
    name: String,
    mode: FrameSourceMode,
    isSelected: Boolean,
    onSelect: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .sizeIn(minHeight = 48.dp)
            .clickable(onClick = onSelect)
            .padding(vertical = 4.dp)
            .semantics {
                contentDescription = "$name" +
                    if (isSelected) " (selected)" else ""
            },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(
            selected = isSelected,
            onClick = onSelect,
        )
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            text = name,
            style = MaterialTheme.typography.bodyLarge,
        )
    }
}

// -----------------------------------------------------------------------
// Advanced section (collapsible)
// -----------------------------------------------------------------------

/**
 * Advanced settings with raw threshold sliders. Collapsed by default.
 */
@Composable
private fun AdvancedSection(
    reidThreshold: Float,
    faceThreshold: Float,
    scorerThreshold: Float,
    onThresholdChanged: (String, Float) -> Unit,
    modifier: Modifier = Modifier,
) {
    var expanded by remember { mutableStateOf(false) }

    Column(modifier = modifier) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable { expanded = !expanded }
                .sizeIn(minHeight = 48.dp)
                .padding(vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = "Advanced",
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.semantics { heading() },
            )

            IconButton(
                onClick = { expanded = !expanded },
                modifier = Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp),
            ) {
                Icon(
                    imageVector = if (expanded) {
                        Icons.Default.KeyboardArrowUp
                    } else {
                        Icons.Default.KeyboardArrowDown
                    },
                    contentDescription = if (expanded) {
                        "Collapse advanced settings"
                    } else {
                        "Expand advanced settings"
                    },
                )
            }
        }

        AnimatedVisibility(visible = expanded) {
            Column {
                Text(
                    text = "Manual threshold overrides. Changing these deselects the active preset.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )

                Spacer(modifier = Modifier.height(16.dp))

                ThresholdSlider(
                    label = "ReID Threshold",
                    value = reidThreshold,
                    onValueChange = { onThresholdChanged("reid", it) },
                )

                Spacer(modifier = Modifier.height(12.dp))

                ThresholdSlider(
                    label = "Face Threshold",
                    value = faceThreshold,
                    onValueChange = { onThresholdChanged("face", it) },
                )

                Spacer(modifier = Modifier.height(12.dp))

                ThresholdSlider(
                    label = "Scorer Threshold",
                    value = scorerThreshold,
                    onValueChange = { onThresholdChanged("scorer", it) },
                )

                Spacer(modifier = Modifier.height(16.dp))

                // Developer info
                Text(
                    text = "Debug Info",
                    style = MaterialTheme.typography.labelMedium,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.semantics { heading() },
                )
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = "ReID: ${"%.2f".format(reidThreshold)} | " +
                        "Face: ${"%.2f".format(faceThreshold)} | " +
                        "Scorer: ${"%.2f".format(scorerThreshold)}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

/**
 * Labeled threshold slider with value display.
 */
@Composable
private fun ThresholdSlider(
    label: String,
    value: Float,
    onValueChange: (Float) -> Unit,
    modifier: Modifier = Modifier,
) {
    var sliderValue by remember(value) { mutableFloatStateOf(value) }

    Column(modifier = modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = label,
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(
                text = "%.2f".format(sliderValue),
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.semantics {
                    contentDescription = "$label: ${"%.2f".format(sliderValue)}"
                },
            )
        }

        Slider(
            value = sliderValue,
            onValueChange = { sliderValue = it },
            onValueChangeFinished = { onValueChange(sliderValue) },
            valueRange = 0.1f..0.9f,
            steps = 15,
            modifier = Modifier
                .fillMaxWidth()
                .sizeIn(minHeight = 48.dp),
            colors = SliderDefaults.colors(
                thumbColor = MaterialTheme.colorScheme.primary,
                activeTrackColor = MaterialTheme.colorScheme.primary,
            ),
        )
    }
}
