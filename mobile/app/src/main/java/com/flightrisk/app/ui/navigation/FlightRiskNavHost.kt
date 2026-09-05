package com.flightrisk.app.ui.navigation

import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.outlined.Face
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.flightrisk.app.config.SensitivityPreset
import com.flightrisk.app.drone.FrameSourceMode
import com.flightrisk.app.drone.TelloState
import com.flightrisk.app.ui.quality.QualityReport
import com.flightrisk.app.ui.search.SearchScreen
import com.flightrisk.app.ui.search.SearchScreenState
import com.flightrisk.app.ui.settings.SettingsScreen
import com.flightrisk.app.ui.settings.SettingsScreenState
import com.flightrisk.app.ui.target.TargetPickerScreen

// -----------------------------------------------------------------------
// Navigation destinations
// -----------------------------------------------------------------------

/**
 * Navigation destination definitions for the main app shell.
 */
sealed class NavDestination(
    val route: String,
    val label: String,
    val icon: ImageVector,
    val contentDesc: String,
) {
    data object Search : NavDestination(
        route = "search",
        label = "Search",
        icon = Icons.Default.Search,
        contentDesc = "Search screen",
    )

    data object Target : NavDestination(
        route = "target",
        label = "Target",
        icon = Icons.Outlined.Face,
        contentDesc = "Target photo picker",
    )

    data object Settings : NavDestination(
        route = "settings",
        label = "Settings",
        icon = Icons.Default.Settings,
        contentDesc = "Settings",
    )
}

private val destinations = listOf(
    NavDestination.Search,
    NavDestination.Target,
    NavDestination.Settings,
)

// -----------------------------------------------------------------------
// Navigation shell
// -----------------------------------------------------------------------

/**
 * Main navigation shell with bottom navigation bar and NavHost.
 *
 * Three destinations:
 * - Search (home): camera preview with detection overlay
 * - Target: photo selection and quality report
 * - Settings: sensitivity presets, LLM backend, advanced thresholds
 *
 * @param searchState Current search screen state.
 * @param settingsState Current settings screen state.
 * @param onStartSearch Callback to start the search pipeline.
 * @param onStopSearch Callback to stop the search pipeline.
 * @param onDismissAlert Callback to dismiss the current alert.
 * @param onNotMyChild Callback for "Not My Child" action.
 * @param onPhotoSelected Callback when a target photo is confirmed.
 * @param onPresetSelected Callback when a sensitivity preset is selected.
 * @param onThresholdChanged Callback for raw threshold changes.
 * @param onLlmBackendChanged Callback for LLM backend changes.
 * @param onApiKeyChanged Callback for API key changes.
 * @param droneState Current drone connection/telemetry state, or null.
 * @param frameSourceMode Whether frames come from device camera or drone.
 * @param latestDroneFrame The most recent video frame from the drone, or null.
 * @param onDroneConnect Callback to initiate drone connection.
 * @param onDroneDisconnect Callback to disconnect from the drone.
 * @param onTakeoff Callback to command drone takeoff.
 * @param onLand Callback to command drone landing.
 * @param onDroneMove Callback for drone directional movement (direction, distanceCm).
 * @param onDroneRotate Callback for drone rotation (degrees).
 * @param modifier Modifier for the root container.
 */
@Composable
fun FlightRiskNavHost(
    searchState: SearchScreenState,
    settingsState: SettingsScreenState,
    targetBitmap: Bitmap? = null,
    targetQualityReport: QualityReport? = null,
    onStartSearch: () -> Unit,
    onStopSearch: () -> Unit,
    onDismissAlert: () -> Unit,
    onNotMyChild: () -> Unit,
    onPhotoSelected: (Bitmap, QualityReport) -> Unit,
    onPresetSelected: (SensitivityPreset) -> Unit,
    onThresholdChanged: (String, Float) -> Unit,
    onLlmBackendChanged: (String) -> Unit,
    onApiKeyChanged: (String) -> Unit,
    droneState: TelloState? = null,
    frameSourceMode: FrameSourceMode = FrameSourceMode.CAMERA,
    latestDroneFrame: Bitmap? = null,
    onDroneConnect: () -> Unit = {},
    onDroneDisconnect: () -> Unit = {},
    onTakeoff: () -> Unit = {},
    onLand: () -> Unit = {},
    onDroneMove: (String, Int) -> Unit = { _, _ -> },
    onDroneRotate: (Int) -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val navController = rememberNavController()
    val navBackStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = navBackStackEntry?.destination?.route
    val context = LocalContext.current

    Scaffold(
        modifier = modifier,
        bottomBar = {
            NavigationBar(
                modifier = Modifier.semantics {
                    contentDescription = "Main navigation"
                },
            ) {
                for (dest in destinations) {
                    NavigationBarItem(
                        selected = currentRoute == dest.route,
                        onClick = {
                            navController.navigate(dest.route) {
                                popUpTo(navController.graph.findStartDestination().id) {
                                    saveState = true
                                }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                        icon = {
                            Icon(
                                imageVector = dest.icon,
                                contentDescription = dest.contentDesc,
                            )
                        },
                        label = { Text(dest.label) },
                    )
                }
            }
        },
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = NavDestination.Search.route,
            modifier = Modifier.padding(innerPadding),
        ) {
            composable(NavDestination.Search.route) {
                SearchScreen(
                    state = searchState.copy(
                        droneState = droneState,
                        frameSourceMode = frameSourceMode,
                        latestDroneFrame = latestDroneFrame,
                    ),
                    onStartSearch = onStartSearch,
                    onStopSearch = onStopSearch,
                    onDismissAlert = onDismissAlert,
                    onNotMyChild = onNotMyChild,
                    onNavigateToMatch = { lat, lon ->
                        val uri = Uri.parse("geo:$lat,$lon?q=$lat,$lon")
                        val intent = Intent(Intent.ACTION_VIEW, uri).apply {
                            setPackage("com.google.android.apps.maps")
                        }
                        if (intent.resolveActivity(context.packageManager) != null) {
                            context.startActivity(intent)
                        } else {
                            // Fallback to any map app
                            context.startActivity(
                                Intent(Intent.ACTION_VIEW, uri)
                            )
                        }
                    },
                    onDroneConnect = onDroneConnect,
                    onDroneDisconnect = onDroneDisconnect,
                    onTakeoff = onTakeoff,
                    onLand = onLand,
                    onDroneMove = onDroneMove,
                    onDroneRotate = onDroneRotate,
                )
            }

            composable(NavDestination.Target.route) {
                TargetPickerScreen(
                    onPhotoSelected = onPhotoSelected,
                    savedBitmap = targetBitmap,
                    savedQualityReport = targetQualityReport,
                )
            }

            composable(NavDestination.Settings.route) {
                SettingsScreen(
                    state = settingsState,
                    onPresetSelected = onPresetSelected,
                    onThresholdChanged = onThresholdChanged,
                    onLlmBackendChanged = onLlmBackendChanged,
                    onApiKeyChanged = onApiKeyChanged,
                    droneState = droneState,
                    frameSourceMode = frameSourceMode,
                )
            }
        }
    }
}
