package com.flightrisk.app.ui.search

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.util.Log
import androidx.camera.core.CameraSelector
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.LocationOn
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.TextButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.flightrisk.app.R
import com.flightrisk.app.alert.AlertManager
import com.flightrisk.app.drone.FrameSourceMode
import com.flightrisk.app.drone.TelloConnectionState
import com.flightrisk.app.drone.TelloState
import com.flightrisk.app.pipeline.MatchEntry
import com.flightrisk.app.ui.theme.AlertOrange
import com.flightrisk.app.ui.theme.AlertRed
import com.flightrisk.app.ui.theme.AlertRedDark
import com.flightrisk.app.ui.theme.DetectionBlue
import com.flightrisk.app.ui.theme.HudWhite
import com.flightrisk.app.ui.theme.MatchGreen
import kotlinx.coroutines.delay

private const val TAG = "SearchScreen"

// -----------------------------------------------------------------------
// Search screen state (passed from ViewModel / parent)
// -----------------------------------------------------------------------

/**
 * UI state for the search screen.
 *
 * @property isSearching Whether the search pipeline is actively running.
 * @property fps Current frames per second from the pipeline.
 * @property personsDetected Number of persons detected in the last frame.
 * @property highestMatchScore Highest match score seen in this session.
 * @property confidenceFrames Current frame count toward corroboration.
 * @property confidenceNeeded Total frames needed for corroboration.
 * @property activeAlert The current active match alert, or null.
 * @property boxes Bounding boxes for the detection overlay.
 * @property cameraFrame The latest camera frame bitmap for preview.
 * @property frameWidth Source frame width for overlay scaling.
 * @property frameHeight Source frame height for overlay scaling.
 * @property droneState Current drone connection/telemetry state, or null if drone not active.
 * @property frameSourceMode Whether frames come from the device camera or drone.
 * @property latestDroneFrame The most recent video frame from the drone, or null.
 * @property droneConnectionMessage User-facing status/error message for the drone connection
 *   attempt (e.g. troubleshooting guidance after a failed connect), or null when there is
 *   nothing to report.
 */
data class SearchScreenState(
    val isSearching: Boolean = false,
    val fps: Float = 0f,
    val personsDetected: Int = 0,
    val highestMatchScore: Float = 0f,
    val confidenceFrames: Int = 0,
    val confidenceNeeded: Int = 3,
    val activeAlert: MatchEntry? = null,
    val boxes: List<BoundingBox> = emptyList(),
    val cameraFrame: Bitmap? = null,
    val frameWidth: Int = 1920,
    val frameHeight: Int = 1080,
    val droneState: TelloState? = null,
    val frameSourceMode: FrameSourceMode = FrameSourceMode.CAMERA,
    val latestDroneFrame: Bitmap? = null,
    val droneConnectionMessage: String? = null,
)

// -----------------------------------------------------------------------
// Search screen composable
// -----------------------------------------------------------------------

/**
 * Main search screen with camera preview, detection overlay, HUD, and
 * match alerts.
 *
 * Layout from top to bottom:
 * 1. HUD overlay (FPS, person count, match score pills)
 * 2. Confidence progress indicator
 * 3. Full-screen camera preview with detection overlay
 * 4. Match alert banner + alert card (when active)
 * 5. Bottom action bar (start/stop search)
 * 6. Persistent disclaimer footer
 *
 * All critical actions (dismiss, "not my child", start/stop) are in the
 * bottom 60% of the screen for one-handed operation.
 *
 * @param state Current search screen state.
 * @param onStartSearch Callback to start the search pipeline.
 * @param onStopSearch Callback to stop the search pipeline.
 * @param onDismissAlert Callback to dismiss the current alert.
 * @param onNotMyChild Callback for "Not My Child" action on an alert.
 * @param onNavigateToMatch Callback to open Maps for a match location.
 * @param onDroneConnect Callback to initiate drone connection.
 * @param onDroneDisconnect Callback to disconnect from the drone.
 * @param onTakeoff Callback to command drone takeoff.
 * @param onLand Callback to command drone landing.
 * @param onDroneMove Callback for drone directional movement (direction, distanceCm).
 * @param onDroneRotate Callback for drone rotation (degrees).
 * @param modifier Modifier for the root container.
 */
@Composable
fun SearchScreen(
    state: SearchScreenState,
    onStartSearch: () -> Unit,
    onStopSearch: () -> Unit,
    onDismissAlert: () -> Unit,
    onNotMyChild: () -> Unit,
    onNavigateToMatch: (latitude: Double, longitude: Double) -> Unit,
    onDroneConnect: () -> Unit = {},
    onDroneDisconnect: () -> Unit = {},
    onTakeoff: () -> Unit = {},
    onLand: () -> Unit = {},
    onDroneMove: (String, Int) -> Unit = { _, _ -> },
    onDroneRotate: (Int) -> Unit = {},
    onEmergencyStop: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    // Finding 8: delay banner to avoid flash on every start
    var showBanner by remember { mutableStateOf(false) }
    LaunchedEffect(state.isSearching) {
        if (state.isSearching) {
            delay(2000)
            showBanner = true
        } else {
            showBanner = false
        }
    }

    Box(
        modifier = modifier.fillMaxSize(),
    ) {
        // ----- Camera / drone preview (full screen) -----
        when (state.frameSourceMode) {
            FrameSourceMode.DRONE -> {
                DroneVideoPreview(
                    droneState = state.droneState,
                    latestFrame = state.latestDroneFrame,
                    cameraFrame = state.cameraFrame,
                    modifier = Modifier.fillMaxSize(),
                )
            }
            FrameSourceMode.CAMERA -> {
                if (state.isSearching) {
                    LiveCameraPreview(modifier = Modifier.fillMaxSize())
                } else {
                    CameraPreviewPlaceholder(
                        frame = state.cameraFrame,
                        modifier = Modifier.fillMaxSize(),
                    )
                }
            }
        }

        // ----- Models-not-loaded banner -----
        if (state.isSearching && showBanner && state.fps < 0.5f) {
            ModelsNotLoadedBanner(
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .padding(
                        top = WindowInsets.statusBars
                            .asPaddingValues()
                            .calculateTopPadding() + 56.dp,
                    ),
            )
        }

        // ----- Detection overlay -----
        if (state.boxes.isNotEmpty()) {
            DetectionOverlay(
                boxes = state.boxes,
                frameWidth = state.frameWidth,
                frameHeight = state.frameHeight,
            )
        }

        // ----- HUD overlay at top -----
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.TopCenter)
                .padding(WindowInsets.statusBars.asPaddingValues())
                .padding(horizontal = 16.dp, vertical = 8.dp),
        ) {
            if (state.isSearching) {
                HudOverlay(
                    fps = state.fps,
                    personsDetected = state.personsDetected,
                    matchScore = state.highestMatchScore,
                )

                // Confidence progress
                if (state.confidenceFrames > 0 && state.confidenceFrames < state.confidenceNeeded) {
                    Spacer(modifier = Modifier.height(8.dp))
                    ConfidenceProgressBar(
                        framesMatched = state.confidenceFrames,
                        framesNeeded = state.confidenceNeeded,
                    )
                }
            }

            // Drone status badge
            val droneState = state.droneState
            if (droneState != null &&
                droneState.connectionState != TelloConnectionState.DISCONNECTED
            ) {
                Spacer(modifier = Modifier.height(8.dp))
                TelloStatusBadge(droneState = droneState)

                // Battery warnings
                val battery = droneState.telemetry.battery ?: 100  // no reading yet = suppress warning
                if (battery in 0..10) {
                    Spacer(modifier = Modifier.height(4.dp))
                    BatteryWarningPill(
                        text = stringResource(R.string.drone_battery_critical, battery),
                        color = AlertRed,
                    )
                } else if (battery in 11..20) {
                    Spacer(modifier = Modifier.height(4.dp))
                    BatteryWarningPill(
                        text = stringResource(R.string.drone_battery_warning, battery),
                        color = AlertOrange,
                    )
                }
            }
        }

        // ----- Match alert banner + card -----
        AnimatedVisibility(
            visible = state.activeAlert != null,
            enter = slideInVertically { -it } + fadeIn(),
            exit = slideOutVertically { -it } + fadeOut(),
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.Center),
        ) {
            state.activeAlert?.let { alert ->
                Column(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    MatchAlertBanner(alertLevel = alert.alertLevel)

                    Spacer(modifier = Modifier.height(8.dp))

                    MatchAlertCard(
                        matchEntry = alert,
                        onDismiss = onDismissAlert,
                        onNotMyChild = onNotMyChild,
                        onNavigate = onNavigateToMatch,
                    )
                }
            }
        }

        // ----- Reconnecting video overlay -----
        if (state.frameSourceMode == FrameSourceMode.DRONE &&
            state.droneState?.connectionState == TelloConnectionState.CONNECTED &&
            state.latestDroneFrame == null
        ) {
            ReconnectingOverlay(
                modifier = Modifier.align(Alignment.Center),
            )
        }

        // ----- Bottom section: flight controls OR action bar (never both) -----
        if (state.droneState?.connectionState == TelloConnectionState.STREAMING) {
            // Show flight controls instead of action bar when streaming
            FlightControlsOverlay(
                isFlying = state.droneState.telemetry.isFlying,
                onTakeoff = onTakeoff,
                onLand = onLand,
                onMove = onDroneMove,
                onRotate = onDroneRotate,
                onStopSearch = onStopSearch,
                onEmergencyStop = onEmergencyStop,
                modifier = Modifier.align(Alignment.BottomCenter),
            )
        } else {
            // Normal bottom section
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .align(Alignment.BottomCenter)
                    .padding(bottom = 16.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                // Drone connection status card
                DroneConnectionCard(
                    droneState = state.droneState,
                    droneConnectionMessage = state.droneConnectionMessage,
                    onConnect = onDroneConnect,
                    onDisconnect = onDroneDisconnect,
                )

                Spacer(modifier = Modifier.height(12.dp))

                // Action bar
                BottomActionBar(
                    isSearching = state.isSearching,
                    onStartSearch = onStartSearch,
                    onStopSearch = onStopSearch,
                )

                Spacer(modifier = Modifier.height(8.dp))

                // Persistent disclaimer
                DisclaimerFooter()
            }
        }
    }
}

// -----------------------------------------------------------------------
// Camera preview placeholder
// -----------------------------------------------------------------------

/**
 * Full-screen camera preview. Displays the latest frame bitmap when
 * available, or a dark placeholder when the camera is not active.
 *
 * The actual CameraX binding is handled by [CameraXFrameSource]; this
 * composable just renders the frame it receives.
 */
@Composable
private fun CameraPreviewPlaceholder(
    frame: Bitmap?,
    modifier: Modifier = Modifier,
) {
    val previewDesc = stringResource(R.string.camera_preview)
    val inactiveDesc = stringResource(R.string.camera_preview_inactive)
    if (frame != null) {
        Image(
            bitmap = frame.asImageBitmap(),
            contentDescription = previewDesc,
            contentScale = ContentScale.Crop,
            modifier = modifier,
        )
    } else {
        Box(
            modifier = modifier
                .background(Color(0xFF121212))
                .semantics { contentDescription = inactiveDesc },
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = previewDesc,
                color = Color.Gray,
                style = MaterialTheme.typography.bodyLarge,
            )
        }
    }
}

// -----------------------------------------------------------------------
// Live camera preview (CameraX)
// -----------------------------------------------------------------------

/**
 * Live camera preview using CameraX [PreviewView] wrapped in [AndroidView].
 *
 * Binds CameraX to the composable's lifecycle owner and unbinds on dispose.
 */
@Composable
private fun LiveCameraPreview(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current

    // Finding 4: guard against missing camera permission
    val hasPermission = remember {
        ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
    }

    if (!hasPermission) {
        Box(
            modifier = modifier.background(Color(0xFF121212)),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = stringResource(R.string.camera_permission_required),
                color = Color.Gray,
                style = MaterialTheme.typography.bodyLarge,
            )
        }
        return
    }

    // Finding 10: PreviewView created in AndroidView factory, not in remember
    var previewView by remember { mutableStateOf<PreviewView?>(null) }

    DisposableEffect(lifecycleOwner) {
        // Finding 1: cache provider reference from callback
        var cameraProvider: ProcessCameraProvider? = null
        // Finding 2: track the specific preview use case
        var preview: Preview? = null
        // Finding 3: disposed flag to guard against race conditions
        var disposed = false

        val cameraProviderFuture = ProcessCameraProvider.getInstance(context)
        cameraProviderFuture.addListener({
            if (disposed) return@addListener
            val provider = cameraProviderFuture.get()
            cameraProvider = provider
            val p = Preview.Builder().build().also { prev ->
                previewView?.let { pv -> prev.setSurfaceProvider(pv.surfaceProvider) }
            }
            preview = p
            val cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA
            try {
                // Finding 2: bind without unbindAll — only our use case
                provider.bindToLifecycle(lifecycleOwner, cameraSelector, p)
            } catch (e: IllegalStateException) {
                // Finding 6: catch specific exceptions
                Log.e(TAG, "Camera bind failed: illegal state", e)
            } catch (e: SecurityException) {
                Log.e(TAG, "Camera bind failed: security exception", e)
            }
        }, ContextCompat.getMainExecutor(context))

        onDispose {
            disposed = true
            try {
                // Finding 1 & 2: unbind only our preview, using cached provider
                preview?.let { cameraProvider?.unbind(it) }
            } catch (e: Exception) {
                Log.w(TAG, "Camera cleanup failed", e)
            }
        }
    }

    AndroidView(
        factory = { ctx ->
            PreviewView(ctx).also { pv -> previewView = pv }
        },
        modifier = modifier,
    )
}

// -----------------------------------------------------------------------
// Models-not-loaded banner
// -----------------------------------------------------------------------

/**
 * Translucent pill banner displayed when the camera is active but
 * the AI detection pipeline is not running (fps == 0).
 */
@Composable
private fun ModelsNotLoadedBanner(modifier: Modifier = Modifier) {
    val bannerText = stringResource(R.string.camera_active_models_not_loaded)
    Box(
        modifier = modifier
            .background(
                color = Color(0xCC000000),
                shape = RoundedCornerShape(16.dp),
            )
            .padding(horizontal = 16.dp, vertical = 8.dp)
            .semantics {
                contentDescription = bannerText
            },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = bannerText,
            color = Color(0xFFCCCCCC),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.Medium,
        )
    }
}

// -----------------------------------------------------------------------
// HUD overlay
// -----------------------------------------------------------------------

/**
 * Heads-up display overlay showing real-time pipeline metrics as
 * translucent pills at the top of the screen.
 */
@Composable
private fun HudOverlay(
    fps: Float,
    personsDetected: Int,
    matchScore: Float,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .semantics { contentDescription = "Search metrics" },
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        HudPill(
            label = "FPS",
            value = "%.1f".format(fps),
            contentDesc = "Frames per second: ${"%.1f".format(fps)}",
        )
        HudPill(
            label = "Persons",
            value = "$personsDetected",
            contentDesc = "$personsDetected person${if (personsDetected != 1) "s" else ""} detected",
        )
        if (matchScore > 0f) {
            HudPill(
                label = "Match",
                value = "${(matchScore * 100).toInt()}%",
                contentDesc = "Best match score: ${(matchScore * 100).toInt()} percent",
            )
        }
    }
}

/**
 * A single translucent pill for the HUD overlay.
 */
@Composable
private fun HudPill(
    label: String,
    value: String,
    contentDesc: String,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .background(
                color = Color(0xCC000000),
                shape = RoundedCornerShape(16.dp),
            )
            .padding(horizontal = 12.dp, vertical = 6.dp)
            .semantics { contentDescription = contentDesc },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = "$label: ",
            color = Color(0xFFAAAAAA),
            style = MaterialTheme.typography.labelSmall,
        )
        Text(
            text = value,
            color = HudWhite,
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.Bold,
        )
    }
}

// -----------------------------------------------------------------------
// Confidence progress
// -----------------------------------------------------------------------

/**
 * Progress bar showing multi-frame corroboration status.
 */
@Composable
private fun ConfidenceProgressBar(
    framesMatched: Int,
    framesNeeded: Int,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(
                color = Color(0xCC000000),
                shape = RoundedCornerShape(8.dp),
            )
            .padding(horizontal = 12.dp, vertical = 8.dp)
            .semantics {
                contentDescription =
                    "Confidence building: $framesMatched of $framesNeeded frames matched"
            },
    ) {
        Text(
            text = "Confidence building... $framesMatched/$framesNeeded frames",
            color = HudWhite,
            style = MaterialTheme.typography.labelMedium,
        )
        Spacer(modifier = Modifier.height(4.dp))
        LinearProgressIndicator(
            progress = { framesMatched.toFloat() / framesNeeded.toFloat() },
            modifier = Modifier
                .fillMaxWidth()
                .height(4.dp)
                .clip(RoundedCornerShape(2.dp)),
            color = MaterialTheme.colorScheme.tertiary,
            trackColor = Color(0xFF444444),
        )
    }
}

// -----------------------------------------------------------------------
// Match alert banner
// -----------------------------------------------------------------------

/**
 * Full-width alert banner displayed when a match is detected.
 *
 * - Confirmed match: dark red background, "POSSIBLE MATCH -- VERIFY IN PERSON"
 * - Possible match: orange background, "POSSIBLE MATCH -- VERIFY"
 */
@Composable
private fun MatchAlertBanner(
    alertLevel: String,
    modifier: Modifier = Modifier,
) {
    val isConfirmed = alertLevel == AlertManager.CONFIRMED_MATCH
    val backgroundColor = if (isConfirmed) AlertRedDark else AlertOrange
    val text = if (isConfirmed) {
        "POSSIBLE MATCH — VERIFY IN PERSON"
    } else {
        "POSSIBLE MATCH — VERIFY"
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .background(backgroundColor)
            .padding(vertical = 16.dp)
            .semantics {
                heading()
                contentDescription = text
            },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = text,
            color = HudWhite,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.ExtraBold,
            textAlign = TextAlign.Center,
        )
    }
}

// -----------------------------------------------------------------------
// Match alert card
// -----------------------------------------------------------------------

/**
 * Alert card with match details, snapshot, scores, GPS coordinates,
 * and action buttons.
 *
 * Action buttons are placed in the bottom portion of the card for
 * one-handed reachability.
 */
@Composable
private fun MatchAlertCard(
    matchEntry: MatchEntry,
    onDismiss: () -> Unit,
    onNotMyChild: () -> Unit,
    onNavigate: (latitude: Double, longitude: Double) -> Unit,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant,
        ),
        elevation = CardDefaults.cardElevation(defaultElevation = 8.dp),
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.Top,
            ) {
                // Snapshot
                matchEntry.snapshot?.let { snapshot ->
                    Image(
                        bitmap = snapshot.asImageBitmap(),
                        contentDescription = "Match snapshot",
                        contentScale = ContentScale.Crop,
                        modifier = Modifier
                            .size(80.dp)
                            .clip(RoundedCornerShape(8.dp)),
                    )
                    Spacer(modifier = Modifier.width(12.dp))
                }

                // Score details
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = "Score: ${(matchEntry.score * 100).toInt()}%",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.semantics {
                            contentDescription =
                                "Match score: ${(matchEntry.score * 100).toInt()} percent"
                        },
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "ReID: ${(matchEntry.reidScore * 100).toInt()}% | " +
                            "Face: ${(matchEntry.faceScore * 100).toInt()}%",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.semantics {
                            contentDescription =
                                "ReID score: ${(matchEntry.reidScore * 100).toInt()} percent, " +
                                    "Face score: ${(matchEntry.faceScore * 100).toInt()} percent"
                        },
                    )

                    // GPS coordinates
                    if (matchEntry.latitude != null && matchEntry.longitude != null) {
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = "%.5f, %.5f".format(
                                matchEntry.latitude, matchEntry.longitude
                            ),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.semantics {
                                contentDescription = "GPS coordinates: " +
                                    "%.5f latitude, %.5f longitude".format(
                                        matchEntry.latitude, matchEntry.longitude
                                    )
                            },
                        )
                    }

                    // Time
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "Detected at ${matchEntry.time}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            // Action buttons (bottom of card for one-handed operation)
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                // Navigate button
                if (matchEntry.latitude != null && matchEntry.longitude != null) {
                    OutlinedButton(
                        onClick = { onNavigate(matchEntry.latitude, matchEntry.longitude) },
                        modifier = Modifier
                            .weight(1f)
                            .sizeIn(minHeight = 48.dp),
                    ) {
                        Icon(
                            imageVector = Icons.Default.LocationOn,
                            contentDescription = null,
                            modifier = Modifier.size(18.dp),
                        )
                        Spacer(modifier = Modifier.width(4.dp))
                        Text("Navigate")
                    }
                }

                // Not My Child button
                Button(
                    onClick = onNotMyChild,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.error,
                        contentColor = MaterialTheme.colorScheme.onError,
                    ),
                    modifier = Modifier
                        .weight(1f)
                        .sizeIn(minHeight = 48.dp),
                ) {
                    Text("NOT MY CHILD")
                }

                // Dismiss button
                IconButton(
                    onClick = onDismiss,
                    modifier = Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp),
                ) {
                    Icon(
                        imageVector = Icons.Default.Close,
                        contentDescription = "Dismiss alert",
                    )
                }
            }
        }
    }
}

// -----------------------------------------------------------------------
// Bottom action bar
// -----------------------------------------------------------------------

/**
 * Bottom action bar with the start/stop search button.
 *
 * The drone connection status/actions live in [DroneConnectionCard], rendered
 * above this bar — this bar only ever contains the search toggle.
 */
@Composable
private fun BottomActionBar(
    isSearching: Boolean,
    onStartSearch: () -> Unit,
    onStopSearch: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp),
        horizontalArrangement = Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // Start/Stop search button
        Button(
            onClick = if (isSearching) onStopSearch else onStartSearch,
            colors = if (isSearching) {
                ButtonDefaults.buttonColors(
                    containerColor = AlertRed,
                    contentColor = HudWhite,
                )
            } else {
                ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.primary,
                    contentColor = MaterialTheme.colorScheme.onPrimary,
                )
            },
            modifier = Modifier
                .sizeIn(minWidth = 200.dp, minHeight = 56.dp),
            shape = RoundedCornerShape(28.dp),
        ) {
            Icon(
                imageVector = if (isSearching) Icons.Default.Close else Icons.Default.PlayArrow,
                contentDescription = null,
                modifier = Modifier.size(24.dp),
            )
            Spacer(modifier = Modifier.width(8.dp))
            Text(
                text = if (isSearching) "Stop Search" else "Start Search",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

// -----------------------------------------------------------------------
// Drone connection card
// -----------------------------------------------------------------------

/**
 * Full-width status card describing the current drone connection state and
 * exposing the connect/disconnect/retry actions. Replaces the old cryptic
 * "D" icon button with an explicit, legible status.
 *
 * - DISCONNECTED (no [droneState], or [TelloConnectionState.DISCONNECTED]): banner with
 *   a "Connect to Drone" button, Wi-Fi hint text, and (if present) [droneConnectionMessage]
 *   shown as troubleshooting text.
 * - [TelloConnectionState.CONNECTING]: spinner + "Connecting to drone...".
 * - [TelloConnectionState.CONNECTED]: green "Connected to Drone" status + disconnect action.
 * - [TelloConnectionState.STREAMING]: green "Drone Connected — Streaming" status, battery
 *   (if known), + disconnect action.
 * - [TelloConnectionState.ERROR]: red "Connection Failed" status with [droneConnectionMessage]
 *   as troubleshooting text + a retry action.
 */
@Composable
private fun DroneConnectionCard(
    droneState: TelloState?,
    droneConnectionMessage: String?,
    onConnect: () -> Unit,
    onDisconnect: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val connectionState = droneState?.connectionState ?: TelloConnectionState.DISCONNECTED

    Card(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp),
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = Color(0xE61A1A1A)),
        elevation = CardDefaults.cardElevation(defaultElevation = 4.dp),
    ) {
        when (connectionState) {
            TelloConnectionState.CONNECTING -> {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp)
                        .semantics { contentDescription = "Connecting to drone" },
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(24.dp),
                        strokeWidth = 2.dp,
                        color = DetectionBlue,
                    )
                    Spacer(modifier = Modifier.width(12.dp))
                    Text(
                        text = "Connecting to drone...",
                        color = HudWhite,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(
                        onClick = onDisconnect,
                        modifier = Modifier.sizeIn(minHeight = 48.dp),
                    ) {
                        Text("Cancel", color = HudWhite)
                    }
                }
            }

            TelloConnectionState.CONNECTED -> {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                ) {
                    Text(
                        text = "Connected to Drone",
                        color = MatchGreen,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    Spacer(modifier = Modifier.height(12.dp))
                    OutlinedButton(
                        onClick = onDisconnect,
                        modifier = Modifier
                            .fillMaxWidth()
                            .sizeIn(minHeight = 48.dp),
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = HudWhite),
                    ) {
                        Text("Disconnect")
                    }
                }
            }

            // STREAMING is handled by FlightControlsOverlay in the parent;
            // DroneConnectionCard is never rendered during STREAMING.
            TelloConnectionState.STREAMING -> { }

            TelloConnectionState.ERROR -> {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                ) {
                    Text(
                        text = "Connection Failed",
                        color = AlertRed,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    if (droneConnectionMessage != null) {
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = droneConnectionMessage,
                            color = HudWhite,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    Spacer(modifier = Modifier.height(12.dp))
                    Button(
                        onClick = onConnect,
                        modifier = Modifier
                            .fillMaxWidth()
                            .sizeIn(minHeight = 48.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = AlertRed,
                            contentColor = HudWhite,
                        ),
                    ) {
                        Icon(
                            imageVector = Icons.Default.Refresh,
                            contentDescription = null,
                            modifier = Modifier.size(18.dp),
                        )
                        Spacer(modifier = Modifier.width(8.dp))
                        Text("Retry")
                    }
                }
            }

            TelloConnectionState.DISCONNECTED -> {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                ) {
                    Text(
                        text = "Connect to Drone",
                        color = HudWhite,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    Spacer(modifier = Modifier.height(12.dp))
                    Button(
                        onClick = onConnect,
                        modifier = Modifier
                            .fillMaxWidth()
                            .sizeIn(minHeight = 48.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = DetectionBlue,
                            contentColor = HudWhite,
                        ),
                    ) {
                        Text("Connect to Drone")
                    }
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Connect your phone to the Tello WiFi network first",
                        color = Color(0xFFAAAAAA),
                        style = MaterialTheme.typography.bodySmall,
                    )
                    if (droneConnectionMessage != null) {
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(
                            text = droneConnectionMessage,
                            color = AlertRed,
                            style = MaterialTheme.typography.bodySmall,
                            fontWeight = FontWeight.Medium,
                        )
                    }
                }
            }
        }
    }
}

// -----------------------------------------------------------------------
// Drone video preview
// -----------------------------------------------------------------------

/**
 * Full-screen preview area for drone video feed.
 *
 * Shows different content based on the drone connection state:
 * - STREAMING with frame: renders the drone frame as an Image
 * - CONNECTING: shows a centered spinner with "Connecting to drone..." text
 * - CONNECTED but no frame: shows "Waiting for video feed..." text
 * - DISCONNECTED: falls back to CameraPreviewPlaceholder behavior
 */
@Composable
private fun DroneVideoPreview(
    droneState: TelloState?,
    latestFrame: Bitmap?,
    cameraFrame: Bitmap?,
    modifier: Modifier = Modifier,
) {
    val connectionState = droneState?.connectionState ?: TelloConnectionState.DISCONNECTED

    when {
        connectionState == TelloConnectionState.STREAMING && latestFrame != null -> {
            Image(
                bitmap = latestFrame.asImageBitmap(),
                contentDescription = stringResource(R.string.drone_streaming),
                contentScale = ContentScale.Fit,
                modifier = modifier,
            )
        }
        connectionState == TelloConnectionState.CONNECTING -> {
            Box(
                modifier = modifier.background(Color(0xFF121212)),
                contentAlignment = Alignment.Center,
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    CircularProgressIndicator()
                    Spacer(modifier = Modifier.height(16.dp))
                    Text(
                        text = stringResource(R.string.drone_connecting),
                        color = Color.Gray,
                        style = MaterialTheme.typography.bodyLarge,
                    )
                }
            }
        }
        connectionState == TelloConnectionState.CONNECTED ||
            (connectionState == TelloConnectionState.STREAMING && latestFrame == null) -> {
            Box(
                modifier = modifier.background(Color(0xFF121212)),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = stringResource(R.string.drone_no_video),
                    color = Color.Gray,
                    style = MaterialTheme.typography.bodyLarge,
                )
            }
        }
        else -> {
            // DISCONNECTED or ERROR: show camera placeholder
            CameraPreviewPlaceholder(
                frame = cameraFrame,
                modifier = modifier,
            )
        }
    }
}

// -----------------------------------------------------------------------
// Battery warning pill
// -----------------------------------------------------------------------

/**
 * Small pill showing a battery warning or critical message.
 */
@Composable
private fun BatteryWarningPill(
    text: String,
    color: Color,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .background(
                color = color,
                shape = RoundedCornerShape(16.dp),
            )
            .padding(horizontal = 12.dp, vertical = 6.dp)
            .semantics { contentDescription = text },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = text,
            color = HudWhite,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Bold,
        )
    }
}

// -----------------------------------------------------------------------
// Reconnecting overlay
// -----------------------------------------------------------------------

/**
 * Semi-transparent overlay shown when the drone stream appears frozen
 * (CONNECTED but no new frames).
 */
@Composable
private fun ReconnectingOverlay(
    modifier: Modifier = Modifier,
) {
    val reconnectingText = stringResource(R.string.drone_reconnecting)
    Box(
        modifier = modifier
            .background(
                color = Color(0x99000000),
                shape = RoundedCornerShape(16.dp),
            )
            .padding(horizontal = 24.dp, vertical = 16.dp)
            .semantics { contentDescription = reconnectingText },
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            CircularProgressIndicator(
                modifier = Modifier.size(24.dp),
                strokeWidth = 2.dp,
                color = HudWhite,
            )
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = reconnectingText,
                color = HudWhite,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

// -----------------------------------------------------------------------
// Disclaimer footer
// -----------------------------------------------------------------------

/**
 * Persistent footer disclaimer text shown at the bottom of the search
 * screen at all times.
 */
@Composable
private fun DisclaimerFooter(
    modifier: Modifier = Modifier,
) {
    Text(
        text = "FlightRisk is an assistive search tool. " +
            "Always verify matches visually before approaching anyone.",
        style = MaterialTheme.typography.labelSmall,
        color = Color(0xFFAAAAAA),
        textAlign = TextAlign.Center,
        modifier = modifier
            .fillMaxWidth()
            .background(
                color = Color(0x99000000),
                shape = RoundedCornerShape(4.dp),
            )
            .padding(horizontal = 16.dp, vertical = 6.dp),
    )
}
