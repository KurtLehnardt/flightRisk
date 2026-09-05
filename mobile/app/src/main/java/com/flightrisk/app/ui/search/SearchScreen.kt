package com.flightrisk.app.ui.search

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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.LocationOn
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.flightrisk.app.alert.AlertManager
import com.flightrisk.app.pipeline.MatchEntry
import com.flightrisk.app.ui.theme.AlertOrange
import com.flightrisk.app.ui.theme.AlertRed
import com.flightrisk.app.ui.theme.AlertRedDark
import com.flightrisk.app.ui.theme.HudWhite

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
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier.fillMaxSize(),
    ) {
        // ----- Camera preview (full screen) -----
        if (state.isSearching) {
            LiveCameraPreview(modifier = Modifier.fillMaxSize())
        } else {
            CameraPreviewPlaceholder(
                frame = state.cameraFrame,
                modifier = Modifier.fillMaxSize(),
            )
        }

        // ----- Models-not-loaded banner -----
        if (state.isSearching && state.fps == 0f) {
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

        // ----- Bottom section (action bar + disclaimer) -----
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .align(Alignment.BottomCenter)
                .padding(bottom = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
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
    if (frame != null) {
        Image(
            bitmap = frame.asImageBitmap(),
            contentDescription = "Camera preview",
            contentScale = ContentScale.Crop,
            modifier = modifier,
        )
    } else {
        Box(
            modifier = modifier
                .background(Color(0xFF121212))
                .semantics { contentDescription = "Camera preview inactive" },
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = "Camera preview",
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
    val previewView = remember { PreviewView(context) }

    DisposableEffect(lifecycleOwner) {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(context)
        cameraProviderFuture.addListener({
            val cameraProvider = cameraProviderFuture.get()
            val preview = Preview.Builder().build().also {
                it.surfaceProvider = previewView.surfaceProvider
            }
            val cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA
            try {
                cameraProvider.unbindAll()
                cameraProvider.bindToLifecycle(lifecycleOwner, cameraSelector, preview)
            } catch (e: Exception) {
                Log.e("SearchScreen", "Camera bind failed", e)
            }
        }, ContextCompat.getMainExecutor(context))

        onDispose {
            cameraProviderFuture.get().unbindAll()
        }
    }

    AndroidView(factory = { previewView }, modifier = modifier)
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
    Box(
        modifier = modifier
            .background(
                color = Color(0xCC000000),
                shape = RoundedCornerShape(16.dp),
            )
            .padding(horizontal = 16.dp, vertical = 8.dp)
            .semantics {
                contentDescription = "Camera active, AI models not loaded"
            },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = "Camera active — AI models not loaded",
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
 * Bottom action bar with start/stop search button and drone connect
 * placeholder.
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

        Spacer(modifier = Modifier.width(16.dp))

        // Drone connect button (future - placeholder)
        OutlinedButton(
            onClick = { /* Future: drone connection */ },
            modifier = Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp),
            shape = CircleShape,
            enabled = false,
        ) {
            Text(
                text = "D",
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.semantics {
                    contentDescription = "Drone connect (coming soon)"
                },
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
