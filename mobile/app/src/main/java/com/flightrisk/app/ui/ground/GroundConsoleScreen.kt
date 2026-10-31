package com.flightrisk.app.ui.ground

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.flightrisk.app.ui.theme.AlertOrange
import com.flightrisk.app.ui.theme.AlertRed
import com.flightrisk.app.ui.theme.MatchGreen

/** The callbacks of the Ground screen, so the content can be drawn (and tested) without a ViewModel. */
class GroundActions(
    val onLandAll: () -> Unit = {},
    val onLandDrone: (String) -> Unit = {},
    val onResume: (String) -> Unit = {},
    val onRetry: () -> Unit = {},
    val onDismissLandAll: () -> Unit = {},
    val onDismissBanner: (Long) -> Unit = {},
    val onDismissToast: (Long) -> Unit = {},
    val onDismissDroneAlert: (String) -> Unit = {},
    val onDismissDroneFeedback: (String, Boolean) -> Unit = { _, _ -> },
    val onSaveAddress: (String) -> Unit = {},
    val onSaveKey: (String) -> Unit = {},
    val onRemoveKey: () -> Unit = {},
    val onTestConnection: () -> Unit = {},
    val onSetEnabled: (Boolean) -> Unit = {},
)

/** The Ground console: an operator console for one FlightRisk ground station. */
@Composable
fun GroundConsoleScreen(viewModel: GroundConsoleViewModel, modifier: Modifier = Modifier) {
    val state by viewModel.uiState.collectAsState()
    val actions = GroundActions(
        onLandAll = { viewModel.landAll() },
        onLandDrone = { viewModel.landDrone(it) },
        onResume = { viewModel.resumeHeldDrone(it) },
        onRetry = viewModel::retryConnection,
        onDismissLandAll = viewModel::dismissLandAll,
        onDismissBanner = viewModel::dismissBanner,
        onDismissToast = viewModel::dismissToast,
        onDismissDroneAlert = viewModel::dismissDroneAlert,
        onDismissDroneFeedback = viewModel::dismissDroneFeedback,
        onSaveAddress = viewModel::saveAddress,
        onSaveKey = viewModel::saveKey,
        onRemoveKey = viewModel::removeKey,
        onTestConnection = viewModel::testConnection,
        onSetEnabled = viewModel::setEnabled,
    )
    GroundConsoleContent(state, actions, modifier)
}

/**
 * The pinned top bar with LAND ALL sits outside the scrolling list, so it is
 * visible in every state. Everything below is a pure function of [state].
 */
@Composable
fun GroundConsoleContent(state: GroundUiState, actions: GroundActions, modifier: Modifier = Modifier) {
    Column(modifier = modifier.fillMaxSize()) {
        GroundTopBar(state, actions)
        HorizontalDivider()
        LazyColumn(
            modifier = Modifier.weight(1f).fillMaxWidth().testTag("ground.list"),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            if (state.isStateStale && state.cards.isNotEmpty() && state.staleNote != null) {
                // First in the list: nothing below is live.
                item(key = "stale") {
                    Text(
                        state.staleNote + " Values below are not live.",
                        style = MaterialTheme.typography.titleSmall,
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(8.dp))
                            .padding(10.dp)
                            .testTag("ground.staleBanner"),
                    )
                }
            }
            items(state.toasts, key = { "toast-${it.id}" }) { toast -> ToastRow(toast, actions) }
            items(state.banners, key = { "banner-${it.id}" }) { banner -> BannerRow(banner, actions) }
            item(key = "readout") {
                Text(
                    state.readout,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.testTag("ground.readout"),
                )
            }
            items(state.cards, key = { "card-${it.id}" }) { card ->
                DroneStatusCard(
                    card = card,
                    onLand = { actions.onLandDrone(card.id) },
                    onResume = { actions.onResume(card.id) },
                    onDismissAlert = { actions.onDismissDroneAlert(card.id) },
                    onDismissLandFeedback = { actions.onDismissDroneFeedback(card.id, true) },
                    onDismissResumeFeedback = { actions.onDismissDroneFeedback(card.id, false) },
                )
            }
            if (state.cards.isEmpty()) {
                item(key = "empty") {
                    Text(
                        if (state.link.isConnected) "The ground reports no drones." else "No drone state yet.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            // ---- Slots for the cards later units add. Each reads its state from GroundUiState
            // (state.snapshot for plan, profiles, ceiling and reasoning; state.board for drones).
            // U10 mission cards: detection profiles, ceiling and reasoning provider (read only).
            item(key = "slot-mission-cards") { MissionCardsSlot(state) }
            // U9/U8 plan canvas (and video tiles): state.snapshot.fleetPlan, client frames.
            item(key = "slot-plan-canvas") { PlanCanvasSlot(state) }

            item(key = "connection") {
                ConnectionCard(
                    state = state.connection,
                    onSaveAddress = actions.onSaveAddress,
                    onSaveKey = actions.onSaveKey,
                    onRemoveKey = actions.onRemoveKey,
                    onTest = actions.onTestConnection,
                    onSetEnabled = actions.onSetEnabled,
                )
            }
        }
    }
}

/** Empty slot for the mission cards (detection profiles, ceiling, reasoning provider). A later unit fills it. */
@Composable
@Suppress("UNUSED_PARAMETER")
fun MissionCardsSlot(state: GroundUiState) {
}

/** Empty slot for the plan canvas. A later unit fills it. */
@Composable
@Suppress("UNUSED_PARAMETER")
fun PlanCanvasSlot(state: GroundUiState) {
}

@Composable
private fun GroundTopBar(state: GroundUiState, actions: GroundActions) {
    Surface(color = MaterialTheme.colorScheme.surface, tonalElevation = 3.dp) {
        Column(
            modifier = Modifier.statusBarsPadding().padding(horizontal = 16.dp, vertical = 10.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Chip(state.link.pillText, linkPillColor(state.link), Modifier.testTag("ground.pill"))
                if (state.appearanceOnly) {
                    Text("APPEARANCE ONLY", style = MaterialTheme.typography.labelSmall, fontWeight = FontWeight.Bold)
                }
                Row(modifier = Modifier.weight(1f)) {}
                // Always visible, always enabled, one tap, no confirmation.
                Button(
                    onClick = actions.onLandAll,
                    enabled = true,
                    colors = ButtonDefaults.buttonColors(containerColor = AlertRed, contentColor = Color.White),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier
                        .heightIn(min = 52.dp)
                        .testTag("ground.landAll")
                        .semantics { contentDescription = "Land all drones" },
                ) { Text("LAND ALL", fontWeight = FontWeight.ExtraBold, style = MaterialTheme.typography.titleMedium) }
            }
            ConnectionBanner(state.link, actions)
            state.landAll?.let { LandAllFeedbackView(it, state.landAllText ?: it.text, actions) }
        }
    }
}

@Composable
private fun ConnectionBanner(link: GroundLink, actions: GroundActions) {
    when (link) {
        GroundLink.Connected -> Unit
        GroundLink.WrongKey -> BannerWithRetry(link.text, "Retry", AlertOrange, actions, "ground.retry")
        is GroundLink.Reconnecting, is GroundLink.Connecting -> BannerWithRetry(link.text, "Retry now", MaterialTheme.colorScheme.onSurfaceVariant, actions, "ground.retry")
        is GroundLink.AddressRefused, GroundLink.StorageUnavailable ->
            Text(link.text, style = MaterialTheme.typography.bodySmall, color = AlertOrange, modifier = Modifier.testTag("ground.connectionBanner"))
        else ->
            Text(link.text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.testTag("ground.connectionBanner"))
    }
}

@Composable
private fun BannerWithRetry(text: String, button: String, color: Color, actions: GroundActions, tag: String) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(text, style = MaterialTheme.typography.bodySmall, color = color, modifier = Modifier.weight(1f).testTag("ground.connectionBanner"))
        OutlinedButton(onClick = actions.onRetry, modifier = Modifier.testTag(tag)) { Text(button) }
    }
}

private fun linkPillColor(link: GroundLink): Color = when (link) {
    GroundLink.Connected -> MatchGreen
    is GroundLink.Connecting, is GroundLink.Reconnecting, GroundLink.WrongKey, GroundLink.StorageUnavailable, is GroundLink.AddressRefused -> AlertOrange
    else -> Color.Gray
}

/**
 * The result of the latest LAND ALL. Failures and partial results stay until
 * dismissed; "no reply yet" and failures offer sending LAND ALL again (a new
 * command, never an automatic retry).
 */
@Composable
private fun LandAllFeedbackView(feedback: CommandFeedback, text: String, actions: GroundActions) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f), RoundedCornerShape(8.dp))
            .padding(8.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Text(
            text,
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = if (feedback.isFailure) FontWeight.Bold else FontWeight.Normal,
            color = if (feedback.isWarning) AlertOrange else MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.testTag("ground.landAllResult"),
        )
        feedback.detail?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.testTag("ground.landAllDetail"))
        }
        if (feedback.offersRetry) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = actions.onLandAll, modifier = Modifier.testTag("ground.landAllAgain")) { Text("Send LAND ALL again") }
                if (feedback.canDismiss) TextButton(onClick = actions.onDismissLandAll) { Text("Dismiss") }
            }
        }
    }
}

@Composable
private fun ToastRow(toast: GroundToast, actions: GroundActions) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f), RoundedCornerShape(8.dp))
            .padding(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            toast.text,
            style = MaterialTheme.typography.bodySmall,
            color = if (toast.isError) AlertOrange else MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(1f).testTag("ground.toast"),
        )
        TextButton(onClick = { actions.onDismissToast(toast.id) }) { Text("Dismiss") }
    }
}

@Composable
private fun BannerRow(banner: GroundBanner, actions: GroundActions) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.4f), RoundedCornerShape(8.dp))
            .padding(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            banner.text,
            style = MaterialTheme.typography.bodySmall,
            fontWeight = if (banner.isFailure) FontWeight.Bold else FontWeight.Normal,
            color = if (banner.isFailure) AlertOrange else MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(1f).testTag("ground.banner"),
        )
        TextButton(onClick = { actions.onDismissBanner(banner.id) }) { Text("Dismiss") }
    }
}
