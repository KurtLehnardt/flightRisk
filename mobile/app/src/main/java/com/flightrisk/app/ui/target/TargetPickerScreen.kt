package com.flightrisk.app.ui.target

import android.graphics.Bitmap
import android.graphics.ImageDecoder
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
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
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
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
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.flightrisk.app.ui.quality.ImageQualityScorer
import com.flightrisk.app.ui.quality.QualityReport
import com.flightrisk.app.ui.theme.AlertRed
import com.flightrisk.app.ui.theme.MatchGreen

/**
 * Target photo selection screen.
 *
 * Allows the user to select or take a photo of the child they are
 * searching for. The selected photo is analyzed for quality (blur,
 * brightness, contrast, resolution, face detection) and a quality
 * report is displayed.
 *
 * The "Use This Photo" button is only enabled when quality is
 * acceptable (grade C or above).
 *
 * @param onPhotoSelected Callback when a photo is confirmed for use.
 *   Receives the bitmap and its quality report.
 * @param modifier Modifier for the root container.
 * @param inline Whether to render in inline mode (no Scaffold, for
 *   embedding in the onboarding flow).
 */
@Composable
fun TargetPickerScreen(
    onPhotoSelected: (Bitmap, QualityReport) -> Unit,
    modifier: Modifier = Modifier,
    inline: Boolean = false,
) {
    val context = LocalContext.current
    var selectedBitmap by remember { mutableStateOf<Bitmap?>(null) }
    var qualityReport by remember { mutableStateOf<QualityReport?>(null) }

    // Photo picker launcher
    val photoPickerLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.PickVisualMedia()
    ) { uri: Uri? ->
        uri?.let {
            val bitmap = loadBitmapFromUri(context, it)
            if (bitmap != null) {
                selectedBitmap = bitmap
                qualityReport = ImageQualityScorer.analyze(bitmap)
            }
        }
    }

    // Camera capture launcher
    var photoUri by remember { mutableStateOf<Uri?>(null) }
    val cameraLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.TakePicture()
    ) { success: Boolean ->
        if (success) {
            photoUri?.let { uri ->
                val bitmap = loadBitmapFromUri(context, uri)
                if (bitmap != null) {
                    selectedBitmap = bitmap
                    qualityReport = ImageQualityScorer.analyze(bitmap)
                }
            }
        }
    }

    val content: @Composable () -> Unit = {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            // Header
            Text(
                text = "Select Target Photo",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.semantics { heading() },
            )

            Spacer(modifier = Modifier.height(8.dp))

            Text(
                text = "Choose a clear, recent photo of the child you are searching for.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )

            Spacer(modifier = Modifier.height(24.dp))

            // Photo selection buttons
            Row(
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Button(
                    onClick = {
                        photoPickerLauncher.launch(
                            PickVisualMediaRequest(
                                ActivityResultContracts.PickVisualMedia.ImageOnly
                            )
                        )
                    },
                    modifier = Modifier.sizeIn(minHeight = 48.dp),
                ) {
                    Icon(
                        imageVector = Icons.Default.Add,
                        contentDescription = null,
                        modifier = Modifier.size(20.dp),
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    Text("Select Photo")
                }

                OutlinedButton(
                    onClick = {
                        val uri = createTempImageUri(context)
                        photoUri = uri
                        cameraLauncher.launch(uri)
                    },
                    modifier = Modifier.sizeIn(minHeight = 48.dp),
                ) {
                    Text("Take Photo")
                }
            }

            Spacer(modifier = Modifier.height(24.dp))

            // Selected photo preview
            if (selectedBitmap != null) {
                PhotoPreviewWithQuality(
                    bitmap = selectedBitmap!!,
                    qualityReport = qualityReport,
                )

                Spacer(modifier = Modifier.height(24.dp))

                // Use This Photo button
                Button(
                    onClick = {
                        selectedBitmap?.let { bitmap ->
                            qualityReport?.let { report ->
                                onPhotoSelected(bitmap, report)
                            }
                        }
                    },
                    enabled = qualityReport?.isAcceptable == true,
                    modifier = Modifier
                        .fillMaxWidth()
                        .sizeIn(minHeight = 56.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MatchGreen,
                        contentColor = Color.White,
                    ),
                ) {
                    Text(
                        text = "Use This Photo",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                }

                if (qualityReport?.isAcceptable == false) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Photo quality is too low. Please select a better photo.",
                        style = MaterialTheme.typography.bodySmall,
                        color = AlertRed,
                        textAlign = TextAlign.Center,
                    )
                }
            } else {
                // Empty state
                Box(
                    modifier = Modifier
                        .size(240.dp)
                        .background(
                            color = MaterialTheme.colorScheme.surfaceVariant,
                            shape = RoundedCornerShape(16.dp),
                        )
                        .semantics { contentDescription = "No photo selected" },
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = "No photo selected",
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }

    if (inline) {
        Box(modifier = modifier) {
            content()
        }
    } else {
        Scaffold(modifier = modifier) { innerPadding ->
            Box(modifier = Modifier.padding(innerPadding)) {
                content()
            }
        }
    }
}

// -----------------------------------------------------------------------
// Photo preview with quality overlay
// -----------------------------------------------------------------------

/**
 * Displays the selected photo with a quality report overlay including
 * grade badge and issue chips.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun PhotoPreviewWithQuality(
    bitmap: Bitmap,
    qualityReport: QualityReport?,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        // Photo with grade badge
        Box(
            modifier = Modifier
                .size(240.dp)
                .clip(RoundedCornerShape(16.dp))
                .border(
                    width = 2.dp,
                    color = when (qualityReport?.grade) {
                        "A", "B" -> MatchGreen
                        "C" -> MaterialTheme.colorScheme.outline
                        else -> AlertRed
                    },
                    shape = RoundedCornerShape(16.dp),
                ),
        ) {
            Image(
                bitmap = bitmap.asImageBitmap(),
                contentDescription = "Selected target photo",
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )

            // Grade badge
            if (qualityReport != null) {
                GradeBadge(
                    grade = qualityReport.grade,
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .padding(8.dp),
                )
            }
        }

        // Quality issues as chips
        if (qualityReport != null && qualityReport.issues.isNotEmpty()) {
            Spacer(modifier = Modifier.height(12.dp))
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
                modifier = Modifier.fillMaxWidth(),
            ) {
                for (issue in qualityReport.issues) {
                    AssistChip(
                        onClick = { },
                        label = { Text(issue) },
                        colors = AssistChipDefaults.assistChipColors(
                            containerColor = MaterialTheme.colorScheme.errorContainer,
                            labelColor = MaterialTheme.colorScheme.onErrorContainer,
                        ),
                        modifier = Modifier.semantics {
                            contentDescription = "Quality issue: $issue"
                        },
                    )
                }
            }
        }

        // Score text
        if (qualityReport != null) {
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = "Quality: ${(qualityReport.overallScore * 100).toInt()}%",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.semantics {
                    contentDescription =
                        "Photo quality score: ${(qualityReport.overallScore * 100).toInt()} percent, grade ${qualityReport.grade}"
                },
            )
        }
    }
}

// -----------------------------------------------------------------------
// Grade badge
// -----------------------------------------------------------------------

/**
 * Colored badge showing the quality grade (A-F).
 */
@Composable
private fun GradeBadge(
    grade: String,
    modifier: Modifier = Modifier,
) {
    val backgroundColor = when (grade) {
        "A" -> MatchGreen
        "B" -> Color(0xFF4CAF50)
        "C" -> Color(0xFFFFA726)
        "D" -> Color(0xFFEF5350)
        else -> AlertRed
    }

    Box(
        modifier = modifier
            .size(36.dp)
            .background(
                color = backgroundColor,
                shape = RoundedCornerShape(8.dp),
            )
            .semantics { contentDescription = "Quality grade: $grade" },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = grade,
            color = Color.White,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.ExtraBold,
        )
    }
}

// -----------------------------------------------------------------------
// Utilities
// -----------------------------------------------------------------------

/**
 * Load a bitmap from a content URI.
 */
private fun loadBitmapFromUri(
    context: android.content.Context,
    uri: Uri,
): Bitmap? {
    return try {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val source = ImageDecoder.createSource(context.contentResolver, uri)
            ImageDecoder.decodeBitmap(source) { decoder, _, _ ->
                decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
                decoder.isMutableRequired = false
            }
        } else {
            @Suppress("DEPRECATION")
            MediaStore.Images.Media.getBitmap(context.contentResolver, uri)
        }
    } catch (e: Exception) {
        null
    }
}

/**
 * Create a temporary URI for camera capture output.
 */
private fun createTempImageUri(context: android.content.Context): Uri {
    val file = java.io.File.createTempFile(
        "flightrisk_target_",
        ".jpg",
        context.cacheDir,
    )
    return androidx.core.content.FileProvider.getUriForFile(
        context,
        "${context.packageName}.fileprovider",
        file,
    )
}
