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
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
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
import com.flightrisk.app.BuildConfig
import com.flightrisk.app.config.MatchingAlgorithmConfig
import com.flightrisk.app.config.SensitivityPreset
import com.flightrisk.app.config.SignalCategory
import com.flightrisk.app.config.SignalConfig
import com.flightrisk.app.config.SignalMetadata
import com.flightrisk.app.config.SignalStatus
import com.flightrisk.app.drone.FrameSourceMode
import com.flightrisk.app.drone.TelloConnectionState
import com.flightrisk.app.drone.TelloState
import com.flightrisk.app.llm.ReasoningAction
import com.flightrisk.app.llm.ReasoningProvider
import com.flightrisk.app.llm.ReasoningReason
import com.flightrisk.app.llm.ReasoningSettings
import com.flightrisk.app.llm.ReasoningUiState
import com.flightrisk.app.llm.local.ModelState
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
 * @property reasoning The reasoning section: selected and active provider, cloud
 *   opt-in, saved-key state, pending consent and messages. It holds no key.
 */
data class SettingsScreenState(
    val activePreset: SensitivityPreset? = SensitivityPreset.BALANCED,
    val reidThreshold: Float = 0.55f,
    val faceThreshold: Float = 0.45f,
    val scorerThreshold: Float = 0.45f,
    val reasoning: ReasoningUiState = ReasoningUiState(),
    val signalConfigs: Map<String, SignalConfig> = emptyMap(),
    val signalStatuses: Map<String, SignalStatus> = emptyMap(),
    val localModelState: ModelState = ModelState.Idle,
    val localModelName: String = "",
    val localModelSizeMb: Int = 0,
)

/**
 * Settings screen with sensitivity presets, LLM backend selection,
 * and advanced threshold controls.
 *
 * @param state Current settings state.
 * @param onPresetSelected Callback when a sensitivity preset is selected.
 * @param onThresholdChanged Callback when a raw threshold is changed.
 *   Params: (name, value) where name is "reid", "face", or "scorer".
 * @param onReasoningAction Callback for every action in the reasoning section.
 * @param droneState Current drone connection and telemetry state, or null.
 * @param frameSourceMode Current frame source (camera or drone).
 * @param modifier Modifier for the root container.
 */
@Composable
fun SettingsScreen(
    state: SettingsScreenState,
    onPresetSelected: (SensitivityPreset) -> Unit,
    onThresholdChanged: (String, Float) -> Unit,
    onReasoningAction: (ReasoningAction) -> Unit,
    onSignalEnabledChanged: (String, Boolean) -> Unit = { _, _ -> },
    onSignalWeightChanged: (String, Float) -> Unit = { _, _ -> },
    onLocalModelDownload: () -> Unit = {},
    onLocalModelDelete: () -> Unit = {},
    droneState: TelloState? = null,
    frameSourceMode: FrameSourceMode = FrameSourceMode.CAMERA,
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

            // ----- Matching Algorithms section -----
            MatchingAlgorithmsSection(
                signalConfigs = state.signalConfigs,
                signalStatuses = state.signalStatuses,
                onSignalEnabledChanged = onSignalEnabledChanged,
                onSignalWeightChanged = onSignalWeightChanged,
            )

            Spacer(modifier = Modifier.height(24.dp))
            HorizontalDivider()
            Spacer(modifier = Modifier.height(24.dp))

            // ----- LLM Backend section -----
            LlmBackendSection(
                reasoning = state.reasoning,
                onAction = onReasoningAction,
                localModelState = state.localModelState,
                localModelName = state.localModelName,
                localModelSizeMb = state.localModelSizeMb,
                onLocalModelDownload = onLocalModelDownload,
                onLocalModelDelete = onLocalModelDelete,
            )

            Spacer(modifier = Modifier.height(24.dp))
            HorizontalDivider()
            Spacer(modifier = Modifier.height(24.dp))

            // ----- Drone section -----
            DroneSection(
                frameSourceMode = frameSourceMode,
                connectionState = droneState?.connectionState
                    ?: TelloConnectionState.DISCONNECTED,
                battery = droneState?.telemetry?.battery ?: 0,
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
 * Reasoning provider selection (Local, Cloud Claude, None), the explicit
 * "Allow cloud reasoning" switch with its consent dialog, the Claude key field
 * (only after opt-in), "Remove saved key" (whenever a key is saved) and the
 * "Cloud active" indicator. Decisions are made by `ReasoningSettings` through
 * [onAction]; this composable only shows the state.
 */
@Composable
private fun LlmBackendSection(
    reasoning: ReasoningUiState,
    onAction: (ReasoningAction) -> Unit,
    localModelState: ModelState = ModelState.Idle,
    localModelName: String = "",
    localModelSizeMb: Int = 0,
    onLocalModelDownload: () -> Unit = {},
    onLocalModelDelete: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val status = reasoning.status
    val selected = reasoning.selected

    reasoning.consent?.let {
        CloudConsentDialog(
            onConfirm = { onAction(ReasoningAction.ConfirmConsent) },
            onDismiss = { onAction(ReasoningAction.DismissConsent) },
        )
    }

    Column(modifier = modifier) {
        Text(
            text = "AI Reasoning",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )

        Spacer(modifier = Modifier.height(4.dp))

        Text(
            text = "Cloud reasoning is off by default. Local and None keep every image on this phone.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(modifier = Modifier.height(16.dp))

        for (provider in ReasoningProvider.entries) {
            val unavailable = provider != ReasoningProvider.OFF && status.unavailable.containsKey(provider)
            LlmBackendOption(
                name = if (unavailable) "${provider.displayName} (unavailable)" else provider.displayName,
                isSelected = selected == provider,
                onSelect = { onAction(ReasoningAction.PickProvider(provider)) },
            )
        }

        Spacer(modifier = Modifier.height(8.dp))

        // Active backend, with the "Cloud active" indicator whenever cloud is in use.
        if (status.isCloudActive) {
            CloudActiveIndicator()
        } else {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(color = if (status.active == ReasoningProvider.OFF) MaterialTheme.colorScheme.onSurfaceVariant else MatchGreen)
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = "Active: ${status.active.displayName}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.semantics { contentDescription = "Active reasoning: ${status.active.displayName}" },
                )
            }
        }

        status.notice?.let { notice ->
            Spacer(modifier = Modifier.height(4.dp))
            Text(text = notice, style = MaterialTheme.typography.bodySmall, color = AlertOrange)
        }

        Spacer(modifier = Modifier.height(12.dp))

        // Explicit opt-in. Turning it on opens the consent dialog; the switch
        // shows on while the dialog is open and returns to off on Cancel. This
        // matches iOS and enables nothing until the dialog is confirmed.
        val pendingOptIn = reasoning.consent?.selectCloud == false
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .sizeIn(minHeight = 48.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = "Allow cloud reasoning",
                style = MaterialTheme.typography.bodyLarge,
                modifier = Modifier.weight(1f),
            )
            Switch(
                checked = reasoning.cloudOptIn || pendingOptIn,
                onCheckedChange = { onAction(ReasoningAction.SetCloudOptIn(it)) },
                modifier = Modifier.semantics {
                    contentDescription = "Allow cloud reasoning: ${if (reasoning.cloudOptIn) "on" else "off"}. " +
                        "Asks for confirmation before any image can be sent to Anthropic."
                },
            )
        }

        if (status.checkingKey) {
            Text(
                text = ReasoningReason.CHECKING_KEY,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        status.unavailable[ReasoningProvider.CLOUD_CLAUDE]?.let { reason ->
            Text(
                text = "Cloud Claude unavailable: $reason",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        if (reasoning.secureStorageUnavailable) {
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = "${ReasoningReason.SECURE_STORAGE_UNAVAILABLE}. Cloud reasoning stays off.",
                style = MaterialTheme.typography.bodySmall,
                color = AlertRed,
            )
        }

        if (reasoning.showKeyField) {
            Spacer(modifier = Modifier.height(12.dp))
            ClaudeKeyField(
                hasSavedKey = reasoning.hasSavedKey,
                onSave = { onAction(ReasoningAction.SaveKey(it)) },
            )
        }

        // Removing a saved key is always possible, opted in or not.
        if (reasoning.showRemoveKey) {
            TextButton(
                onClick = { onAction(ReasoningAction.RemoveKey) },
                modifier = Modifier.sizeIn(minHeight = 48.dp),
            ) {
                Text("Remove saved key", color = AlertRed)
            }
        }

        reasoning.message?.let { message ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = message,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = { onAction(ReasoningAction.DismissMessage) }) { Text("OK") }
            }
        }

        // Local model management (shown when local is selected)
        if (selected == ReasoningProvider.LOCAL) {
            Spacer(modifier = Modifier.height(12.dp))
            LocalModelCard(
                modelState = localModelState,
                modelName = localModelName,
                modelSizeMb = localModelSizeMb,
                onDownload = onLocalModelDownload,
                onDelete = onLocalModelDelete,
            )
        } else if (selected == ReasoningProvider.OFF) {
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = "Reasoning is off. Matches are scored without a language model.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * The Claude key field. It starts empty every time (a saved key is never
 * loaded back), trims what was pasted, and is cleared after Save. The text
 * lives only in this composable's state.
 */
@Composable
private fun ClaudeKeyField(hasSavedKey: Boolean, onSave: (String) -> Unit) {
    var input by remember { mutableStateOf("") }
    OutlinedTextField(
        value = input,
        onValueChange = { input = it },
        label = { Text("Claude API Key") },
        placeholder = { Text(if (hasSavedKey) "Replace saved key" else "sk-ant-...") },
        singleLine = true,
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
        modifier = Modifier
            .fillMaxWidth()
            .sizeIn(minHeight = 48.dp),
    )
    Row(verticalAlignment = Alignment.CenterVertically) {
        TextButton(
            onClick = {
                val key = ReasoningSettings.normalizedKey(input)
                input = ""
                if (key != null) onSave(key)
            },
            enabled = ReasoningSettings.normalizedKey(input) != null,
            modifier = Modifier.sizeIn(minHeight = 48.dp),
        ) {
            Text("Save API key")
        }
        Spacer(modifier = Modifier.width(8.dp))
        StatusDot(color = if (hasSavedKey) MatchGreen else AlertRed)
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            text = if (hasSavedKey) "Key saved" else "No API key saved",
            style = MaterialTheme.typography.bodySmall,
            color = if (hasSavedKey) MatchGreen else AlertRed,
        )
    }
}

@Composable
private fun LocalModelCard(
    modelState: ModelState,
    modelName: String,
    modelSizeMb: Int,
    onDownload: () -> Unit,
    onDelete: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val borderColor = when (modelState) {
        is ModelState.Ready -> MatchGreen
        is ModelState.Downloaded, is ModelState.Loading -> DetectionBlue
        is ModelState.Downloading -> AlertOrange
        is ModelState.Error -> AlertRed
        is ModelState.Idle -> MaterialTheme.colorScheme.outline
    }

    val bgColor = when (modelState) {
        is ModelState.Ready -> MatchGreen.copy(alpha = 0.05f)
        is ModelState.Downloaded, is ModelState.Loading -> DetectionBlue.copy(alpha = 0.05f)
        else -> Color.Transparent
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .border(1.dp, borderColor, RoundedCornerShape(8.dp))
            .background(bgColor, RoundedCornerShape(8.dp))
            .padding(12.dp),
    ) {
        Column {
            Text(
                text = modelName.ifBlank { "Local LLM" },
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Bold,
            )

            if (modelSizeMb > 0) {
                Spacer(modifier = Modifier.height(2.dp))
                Text(
                    text = "~${modelSizeMb}MB download",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            Spacer(modifier = Modifier.height(4.dp))

            Text(
                text = "Slower than cloud but works offline. " +
                    "Algorithm-based signals carry more weight when using local models.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            Spacer(modifier = Modifier.height(8.dp))

            // Status + action
            when (modelState) {
                is ModelState.Idle -> {
                    TextButton(
                        onClick = onDownload,
                        modifier = Modifier.sizeIn(minHeight = 48.dp),
                    ) {
                        Text("Download Model")
                    }
                }

                is ModelState.Downloading -> {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "Downloading... ${(modelState.progress * 100).toInt()}%",
                                style = MaterialTheme.typography.bodySmall,
                                color = AlertOrange,
                            )
                            Spacer(modifier = Modifier.height(4.dp))
                            androidx.compose.material3.LinearProgressIndicator(
                                progress = { modelState.progress },
                                modifier = Modifier.fillMaxWidth(),
                            )
                        }
                    }
                }

                is ModelState.Downloaded -> {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        StatusDot(color = DetectionBlue)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = "Downloaded — will load on first use",
                            style = MaterialTheme.typography.bodySmall,
                            color = DetectionBlue,
                        )
                    }
                    TextButton(
                        onClick = onDelete,
                        modifier = Modifier.sizeIn(minHeight = 48.dp),
                    ) {
                        Text("Delete Model", color = AlertRed)
                    }
                }

                is ModelState.Loading -> {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        StatusDot(color = AlertOrange)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = "Loading model...",
                            style = MaterialTheme.typography.bodySmall,
                            color = AlertOrange,
                        )
                    }
                }

                is ModelState.Ready -> {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        StatusDot(color = MatchGreen)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = "Ready",
                            style = MaterialTheme.typography.bodySmall,
                            color = MatchGreen,
                        )
                    }
                    TextButton(
                        onClick = onDelete,
                        modifier = Modifier.sizeIn(minHeight = 48.dp),
                    ) {
                        Text("Delete Model", color = AlertRed)
                    }
                }

                is ModelState.Error -> {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        StatusDot(color = AlertRed)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = modelState.message,
                            style = MaterialTheme.typography.bodySmall,
                            color = AlertRed,
                        )
                    }
                    TextButton(
                        onClick = onDownload,
                        modifier = Modifier.sizeIn(minHeight = 48.dp),
                    ) {
                        Text("Retry Download")
                    }
                }
            }
        }
    }
}

@Composable
private fun StatusDot(color: Color, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .size(8.dp)
            .background(color, RoundedCornerShape(4.dp)),
    )
}

/**
 * A single LLM backend radio option.
 */
@Composable
private fun LlmBackendOption(
    name: String,
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
 * Read-only drone connection status display. Frame source is toggled
 * via the drone button on the Search screen, not here.
 */
@Composable
private fun DroneSection(
    frameSourceMode: FrameSourceMode,
    connectionState: TelloConnectionState,
    battery: Int,
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
            text = "Tello drone connection status. Use the drone button on the Search screen to connect or disconnect.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(modifier = Modifier.height(16.dp))

        // Current source indicator
        Text(
            text = "Video source: ${if (frameSourceMode == FrameSourceMode.DRONE) "Tello Drone" else "Phone Camera"}",
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.semantics {
                contentDescription = "Video source: ${if (frameSourceMode == FrameSourceMode.DRONE) "Tello Drone" else "Phone Camera"}"
            },
        )

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
    }
}

// -----------------------------------------------------------------------
// Matching Algorithms section
// -----------------------------------------------------------------------

/**
 * Matching algorithm configuration with per-signal toggles and weight sliders.
 * Signals are grouped by category: Face Recognition, Person Re-ID, Appearance, Other.
 */
@Composable
private fun MatchingAlgorithmsSection(
    signalConfigs: Map<String, SignalConfig>,
    signalStatuses: Map<String, SignalStatus>,
    onSignalEnabledChanged: (String, Boolean) -> Unit,
    onSignalWeightChanged: (String, Float) -> Unit,
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
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = "Matching Algorithms",
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.semantics { heading() },
                )
                Spacer(modifier = Modifier.height(2.dp))
                Text(
                    text = "Configure which signals contribute to match scoring.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

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
                        "Collapse matching algorithms"
                    } else {
                        "Expand matching algorithms"
                    },
                )
            }
        }

        // Summary: count of active signals
        val activeCount = signalConfigs.count { it.value.enabled }
        val totalCount = MatchingAlgorithmConfig.SIGNALS.size
        Text(
            text = "$activeCount of $totalCount signals active",
            style = MaterialTheme.typography.bodySmall,
            color = if (activeCount > 0) MatchGreen else MaterialTheme.colorScheme.onSurfaceVariant,
        )

        AnimatedVisibility(visible = expanded) {
            Column {
                Spacer(modifier = Modifier.height(16.dp))

                // Group signals by category
                val signalsByCategory = MatchingAlgorithmConfig.SIGNALS.groupBy { it.category }

                for (category in SignalCategory.entries) {
                    val signals = signalsByCategory[category] ?: continue

                    Text(
                        text = category.displayName,
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier
                            .padding(vertical = 8.dp)
                            .semantics { heading() },
                    )

                    for (signal in signals) {
                        val config = signalConfigs[signal.key] ?: SignalConfig(
                            enabled = signal.defaultEnabled,
                            weight = signal.defaultWeight,
                        )
                        val status = signalStatuses[signal.key] ?: SignalStatus.AVAILABLE

                        SignalConfigCard(
                            metadata = signal,
                            config = config,
                            status = status,
                            onEnabledChanged = { enabled ->
                                onSignalEnabledChanged(signal.key, enabled)
                            },
                            onWeightChanged = { weight ->
                                onSignalWeightChanged(signal.key, weight)
                            },
                        )

                        Spacer(modifier = Modifier.height(8.dp))
                    }

                    Spacer(modifier = Modifier.height(8.dp))
                }
            }
        }
    }
}

/**
 * A single signal configuration card with toggle, status, weight slider,
 * and description.
 */
@Composable
private fun SignalConfigCard(
    metadata: SignalMetadata,
    config: SignalConfig,
    status: SignalStatus,
    onEnabledChanged: (Boolean) -> Unit,
    onWeightChanged: (Float) -> Unit,
    modifier: Modifier = Modifier,
) {
    val borderColor = when {
        config.enabled && status == SignalStatus.ACTIVE -> MatchGreen
        config.enabled && status == SignalStatus.AVAILABLE -> DetectionBlue
        status == SignalStatus.NOT_INSTALLED -> AlertOrange
        else -> MaterialTheme.colorScheme.outline
    }

    val bgColor = when {
        config.enabled && status == SignalStatus.ACTIVE -> MatchGreen.copy(alpha = 0.05f)
        config.enabled && status == SignalStatus.AVAILABLE -> DetectionBlue.copy(alpha = 0.05f)
        else -> Color.Transparent
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .border(
                width = 1.dp,
                color = borderColor,
                shape = RoundedCornerShape(8.dp),
            )
            .background(
                color = bgColor,
                shape = RoundedCornerShape(8.dp),
            )
            .padding(12.dp),
    ) {
        Column {
            // Header row: name + toggle
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = metadata.displayName,
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Bold,
                    )
                }

                Switch(
                    checked = config.enabled,
                    onCheckedChange = onEnabledChanged,
                    enabled = status != SignalStatus.NOT_INSTALLED,
                    colors = SwitchDefaults.colors(
                        checkedThumbColor = MatchGreen,
                        checkedTrackColor = MatchGreen.copy(alpha = 0.5f),
                    ),
                    modifier = Modifier.semantics {
                        contentDescription = "${metadata.displayName}: ${if (config.enabled) "enabled" else "disabled"}"
                    },
                )
            }

            // Status badge
            Spacer(modifier = Modifier.height(4.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                val statusColor = when (status) {
                    SignalStatus.ACTIVE -> MatchGreen
                    SignalStatus.AVAILABLE -> DetectionBlue
                    SignalStatus.NOT_INSTALLED -> AlertOrange
                }
                val statusText = when (status) {
                    SignalStatus.ACTIVE -> "Active"
                    SignalStatus.AVAILABLE -> "Available"
                    SignalStatus.NOT_INSTALLED -> "Not Installed"
                }

                Box(
                    modifier = Modifier
                        .size(6.dp)
                        .background(
                            color = statusColor,
                            shape = RoundedCornerShape(3.dp),
                        ),
                )
                Spacer(modifier = Modifier.width(6.dp))
                Text(
                    text = statusText,
                    style = MaterialTheme.typography.labelSmall,
                    color = statusColor,
                )
            }

            // Description
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = metadata.description,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            // Weight slider (only when enabled)
            if (config.enabled && status != SignalStatus.NOT_INSTALLED) {
                Spacer(modifier = Modifier.height(8.dp))
                WeightSlider(
                    label = "Weight",
                    value = config.weight,
                    onValueChange = onWeightChanged,
                )
            }
        }
    }
}

/**
 * Weight slider for a signal (0.0 to 1.0, step 0.05).
 */
@Composable
private fun WeightSlider(
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
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                text = "%.2f".format(sliderValue),
                style = MaterialTheme.typography.labelSmall,
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
            valueRange = 0.0f..1.0f,
            steps = 19, // 0.05 increments
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
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = "Version: ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})",
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
