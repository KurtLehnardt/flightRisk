package com.flightrisk.app.ui.navigation

import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.widget.Toast
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.displayCutout
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.union
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Place
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
import com.flightrisk.app.camera.CameraXFrameSource
import com.flightrisk.app.llm.ReasoningAction
import com.flightrisk.app.config.SensitivityPreset
import com.flightrisk.app.drone.FrameSourceMode
import com.flightrisk.app.drone.PatternType
import com.flightrisk.app.drone.TelloState
import com.flightrisk.app.ui.demo.DemoTargetPlaceholder
import com.flightrisk.app.ui.ground.GroundConsoleRoute
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

    data object Ground : NavDestination(
        route = "ground",
        label = "Ground",
        icon = Icons.Default.Place,
        contentDesc = "Ground station console",
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
    NavDestination.Ground,
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
 * @param onReasoningAction Callback for actions in the Settings reasoning section.
 * @param droneState Current drone connection/telemetry state, or null.
 * @param frameSourceMode Whether frames come from device camera or drone.
 * @param latestDroneFrame The most recent video frame from the drone, or null.
 * @param onDroneConnect Callback to initiate drone connection.
 * @param onDroneDisconnect Callback to disconnect from the drone.
 * @param onTakeoff Callback to command drone takeoff.
 * @param onLand Callback to command drone landing.
 * @param onDroneMove Callback for drone directional movement (direction, distanceCm).
 * @param onDroneRotate Callback for drone rotation (degrees).
 * @param cameraFrameSource Receives the phone camera's frames for the search pipeline.
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
    onReasoningAction: (ReasoningAction) -> Unit,
    onSignalEnabledChanged: (String, Boolean) -> Unit = { _, _ -> },
    onSignalWeightChanged: (String, Float) -> Unit = { _, _ -> },
    onLocalModelDownload: () -> Unit = {},
    onLocalModelDelete: () -> Unit = {},
    droneState: TelloState? = null,
    frameSourceMode: FrameSourceMode = FrameSourceMode.CAMERA,
    latestDroneFrame: Bitmap? = null,
    onDismissDroneAlert: () -> Unit = {},
    onDroneConnect: () -> Unit = {},
    onDroneDisconnect: () -> Unit = {},
    onTakeoff: () -> Unit = {},
    onLand: () -> Unit = {},
    onDroneMove: (String, Int) -> Unit = { _, _ -> },
    onDroneRotate: (Int) -> Unit = {},
    onEmergencyStop: () -> Unit = {},
    selectedSearchPattern: PatternType = PatternType.EXPANDING_SQUARE,
    onSearchPatternChanged: (PatternType) -> Unit = {},
    demoActive: Boolean = false,
    onDemoToggle: (Boolean) -> Unit = {},
    cameraFrameSource: CameraXFrameSource? = null,
    modifier: Modifier = Modifier,
) {
    val navController = rememberNavController()
    val navBackStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = navBackStackEntry?.destination?.route
    val context = LocalContext.current
    val navigateTo: (String) -> Unit = { route ->
        navController.navigate(route) {
            popUpTo(navController.graph.findStartDestination().id) {
                saveState = true
            }
            launchSingleTop = true
            restoreState = true
        }
    }

    Scaffold(
        modifier = modifier,
        // Edge-to-edge is always on from Android 15 (target SDK 35+): pad the content for
        // the system bars and for a display cutout (a landscape notch), not only the bars.
        contentWindowInsets = WindowInsets.systemBars.union(WindowInsets.displayCutout),
        bottomBar = {
            NavigationBar(
                modifier = Modifier.semantics {
                    contentDescription = "Main navigation"
                },
            ) {
                for (dest in destinations) {
                    NavigationBarItem(
                        selected = currentRoute == dest.route,
                        onClick = { navigateTo(dest.route) },
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
            // Consume what this shell padded, so a screen's own Scaffold or
            // statusBarsPadding() does not add the status bar a second time.
            modifier = Modifier
                .padding(innerPadding)
                .consumeWindowInsets(innerPadding),
        ) {
            composable(NavDestination.Search.route) {
                SearchScreen(
                    state = searchState.copy(
                        droneState = droneState,
                        frameSourceMode = frameSourceMode,
                        latestDroneFrame = latestDroneFrame,
                        selectedPattern = selectedSearchPattern,
                        hasTarget = targetBitmap != null,
                    ),
                    onStartSearch = onStartSearch,
                    onStopSearch = onStopSearch,
                    onDismissAlert = onDismissAlert,
                    onNotMyChild = onNotMyChild,
                    onNavigateToMatch = { lat, lon ->
                        val uri = Uri.parse("geo:$lat,$lon?q=$lat,$lon")
                        // Google Maps first, then any map app. resolveActivity() cannot see
                        // other packages without a <queries> entry (package visibility), so
                        // the launch itself is tried and a missing app is caught.
                        val mapsIntent = Intent(Intent.ACTION_VIEW, uri).setPackage("com.google.android.apps.maps")
                        val opened = runCatching { context.startActivity(mapsIntent) }.isSuccess ||
                            runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, uri)) }.isSuccess
                        if (!opened) {
                            Toast.makeText(context, "No map app is installed", Toast.LENGTH_LONG).show()
                        }
                    },
                    onDismissDroneAlert = onDismissDroneAlert,
                    onDroneConnect = onDroneConnect,
                    onDroneDisconnect = onDroneDisconnect,
                    onTakeoff = onTakeoff,
                    onLand = onLand,
                    onDroneMove = onDroneMove,
                    onDroneRotate = onDroneRotate,
                    onEmergencyStop = onEmergencyStop,
                    onSearchPatternChanged = onSearchPatternChanged,
                    onSetUpTarget = { navigateTo(NavDestination.Target.route) },
                    cameraFrameSource = cameraFrameSource,
                )
            }

            composable(NavDestination.Target.route) {
                if (demoActive) {
                    // No photo picker and no camera in demo mode: only the drawn placeholder target.
                    DemoTargetPlaceholder(target = targetBitmap)
                } else {
                    TargetPickerScreen(
                        onPhotoSelected = onPhotoSelected,
                        savedBitmap = targetBitmap,
                        savedQualityReport = targetQualityReport,
                    )
                }
            }

            composable(NavDestination.Ground.route) {
                GroundConsoleRoute()
            }

            composable(NavDestination.Settings.route) {
                SettingsScreen(
                    state = settingsState,
                    onPresetSelected = onPresetSelected,
                    onThresholdChanged = onThresholdChanged,
                    onReasoningAction = onReasoningAction,
                    onSignalEnabledChanged = onSignalEnabledChanged,
                    onSignalWeightChanged = onSignalWeightChanged,
                    onLocalModelDownload = onLocalModelDownload,
                    onLocalModelDelete = onLocalModelDelete,
                    searchActive = searchState.isSearching,
                    droneState = droneState,
                    frameSourceMode = frameSourceMode,
                    demoActive = demoActive,
                    onDemoToggle = onDemoToggle,
                )
            }
        }
    }
}
