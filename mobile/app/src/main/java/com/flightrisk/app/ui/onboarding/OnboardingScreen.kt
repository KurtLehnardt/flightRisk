package com.flightrisk.app.ui.onboarding

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import androidx.compose.animation.AnimatedContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.flightrisk.app.ui.quality.QualityReport
import com.flightrisk.app.ui.target.TargetPickerScreen
import com.flightrisk.app.ui.theme.AlertRed

/** SharedPreferences key for privacy notice acknowledgment. */
const val PREF_PRIVACY_ACKNOWLEDGED = "flightrisk_privacy_notice_acknowledged"

/** SharedPreferences key for onboarding completion. */
const val PREF_ONBOARDING_COMPLETE = "flightrisk_onboarding_complete"

/** SharedPreferences file name. */
const val PREFS_NAME = "flightrisk_prefs"

/**
 * First-launch onboarding flow.
 *
 * Steps:
 * 1. Privacy notice (camera use, on-device processing)
 * 2. Emergency callout (call 911 if child in danger)
 * 3. "My child is missing" CTA
 * 4. Photo selection (inline TargetPickerScreen)
 * 5. Quality check with retry
 * 6. Transition to search
 *
 * Stores acknowledgment in SharedPreferences so the flow is only shown
 * once.
 *
 * @param onComplete Callback when onboarding is complete. Receives the
 *   selected target bitmap.
 */
@Composable
fun OnboardingScreen(
    onComplete: (Bitmap) -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    var currentStep by remember { mutableIntStateOf(0) }
    var selectedBitmap by remember { mutableStateOf<Bitmap?>(null) }
    var qualityReport by remember { mutableStateOf<QualityReport?>(null) }

    Box(
        modifier = modifier.fillMaxSize(),
    ) {
        AnimatedContent(
            targetState = currentStep,
            label = "onboarding_step",
        ) { step ->
            when (step) {
                0 -> PrivacyNoticeStep(
                    onAccept = {
                        savePrivacyAcknowledgment(context)
                        currentStep = 1
                    },
                )
                1 -> EmergencyCalloutStep(
                    onContinue = { currentStep = 2 },
                    onCall911 = {
                        val intent = Intent(Intent.ACTION_DIAL, Uri.parse("tel:911"))
                        context.startActivity(intent)
                    },
                )
                2 -> MissingChildStep(
                    onContinue = { currentStep = 3 },
                )
                3 -> PhotoSelectionStep(
                    onPhotoSelected = { bitmap, report ->
                        selectedBitmap = bitmap
                        qualityReport = report
                        currentStep = 4
                    },
                )
                4 -> QualityCheckStep(
                    bitmap = selectedBitmap,
                    qualityReport = qualityReport,
                    onAccept = {
                        saveOnboardingComplete(context)
                        selectedBitmap?.let { onComplete(it) }
                    },
                    onRetry = { currentStep = 3 },
                )
            }
        }
    }
}

// -----------------------------------------------------------------------
// Step 1: Privacy notice
// -----------------------------------------------------------------------

@Composable
private fun PrivacyNoticeStep(
    onAccept: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = "Privacy Notice",
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )

        Spacer(modifier = Modifier.height(24.dp))

        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(12.dp),
                )
                .padding(20.dp),
        ) {
            PrivacyBullet(
                title = "Camera Access",
                description = "FlightRisk uses your camera to scan for people " +
                    "who match the target photo you provide.",
            )

            Spacer(modifier = Modifier.height(16.dp))

            PrivacyBullet(
                title = "On-Device Processing",
                description = "All person detection and matching runs entirely on " +
                    "your device. Camera frames are never uploaded or stored.",
            )

            Spacer(modifier = Modifier.height(16.dp))

            PrivacyBullet(
                title = "Optional Cloud Features",
                description = "If you enable Cloud Claude, match snapshots may be sent " +
                    "to Anthropic's API for reasoning verification. This is optional " +
                    "and can be disabled in Settings.",
            )

            Spacer(modifier = Modifier.height(16.dp))

            PrivacyBullet(
                title = "Location Data",
                description = "GPS coordinates are attached to match alerts so you can " +
                    "navigate to the location. Location data stays on your device.",
            )

            Spacer(modifier = Modifier.height(16.dp))

            PrivacyBullet(
                title = "No Data Collection",
                description = "FlightRisk does not collect analytics, telemetry, or " +
                    "personal data. There is no account or sign-in.",
            )
        }

        Spacer(modifier = Modifier.height(32.dp))

        Button(
            onClick = onAccept,
            modifier = Modifier
                .fillMaxWidth()
                .sizeIn(minHeight = 56.dp),
        ) {
            Text(
                text = "I Understand",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun PrivacyBullet(
    title: String,
    description: String,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier) {
        Text(
            text = title,
            style = MaterialTheme.typography.titleSmall,
            fontWeight = FontWeight.Bold,
        )
        Text(
            text = description,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

// -----------------------------------------------------------------------
// Step 2: Emergency callout
// -----------------------------------------------------------------------

@Composable
private fun EmergencyCalloutStep(
    onContinue: () -> Unit,
    onCall911: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = "Important",
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.Bold,
            color = AlertRed,
            modifier = Modifier.semantics { heading() },
        )

        Spacer(modifier = Modifier.height(24.dp))

        Text(
            text = "If your child is in immediate danger, call 911 first.",
            style = MaterialTheme.typography.titleLarge,
            textAlign = TextAlign.Center,
            fontWeight = FontWeight.Bold,
        )

        Spacer(modifier = Modifier.height(32.dp))

        // Large 911 button
        Button(
            onClick = onCall911,
            colors = ButtonDefaults.buttonColors(
                containerColor = AlertRed,
                contentColor = Color.White,
            ),
            modifier = Modifier
                .size(120.dp)
                .semantics { contentDescription = "Call 911 emergency services" },
            shape = CircleShape,
        ) {
            Text(
                text = "911",
                style = MaterialTheme.typography.headlineLarge,
                fontWeight = FontWeight.ExtraBold,
            )
        }

        Spacer(modifier = Modifier.height(48.dp))

        OutlinedButton(
            onClick = onContinue,
            modifier = Modifier
                .fillMaxWidth()
                .sizeIn(minHeight = 56.dp),
        ) {
            Text(
                text = "Continue to FlightRisk",
                style = MaterialTheme.typography.titleMedium,
            )
        }
    }
}

// -----------------------------------------------------------------------
// Step 3: "My child is missing"
// -----------------------------------------------------------------------

@Composable
private fun MissingChildStep(
    onContinue: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(modifier = Modifier.weight(1f))

        Text(
            text = "FlightRisk",
            style = MaterialTheme.typography.headlineLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )

        Spacer(modifier = Modifier.height(8.dp))

        Text(
            text = "AI-powered lost child finder",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(modifier = Modifier.weight(1f))

        Button(
            onClick = onContinue,
            modifier = Modifier
                .fillMaxWidth()
                .sizeIn(minHeight = 72.dp),
            colors = ButtonDefaults.buttonColors(
                containerColor = MaterialTheme.colorScheme.primary,
                contentColor = MaterialTheme.colorScheme.onPrimary,
            ),
            shape = RoundedCornerShape(16.dp),
        ) {
            Text(
                text = "My child is missing",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold,
            )
        }

        Spacer(modifier = Modifier.weight(0.5f))
    }
}

// -----------------------------------------------------------------------
// Step 4: Photo selection (inline TargetPicker)
// -----------------------------------------------------------------------

@Composable
private fun PhotoSelectionStep(
    onPhotoSelected: (Bitmap, QualityReport) -> Unit,
    modifier: Modifier = Modifier,
) {
    TargetPickerScreen(
        onPhotoSelected = onPhotoSelected,
        inline = true,
        modifier = modifier,
    )
}

// -----------------------------------------------------------------------
// Step 5: Quality check
// -----------------------------------------------------------------------

@Composable
private fun QualityCheckStep(
    bitmap: Bitmap?,
    qualityReport: QualityReport?,
    onAccept: () -> Unit,
    onRetry: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        if (qualityReport?.isAcceptable == true) {
            Text(
                text = "Photo accepted",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.semantics { heading() },
            )

            Spacer(modifier = Modifier.height(8.dp))

            Text(
                text = "Quality grade: ${qualityReport.grade} " +
                    "(${(qualityReport.overallScore * 100).toInt()}%)",
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            Spacer(modifier = Modifier.height(32.dp))

            Button(
                onClick = onAccept,
                modifier = Modifier
                    .fillMaxWidth()
                    .sizeIn(minHeight = 56.dp),
            ) {
                Text(
                    text = "Start Searching",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold,
                )
            }
        } else {
            Text(
                text = "Photo quality is too low",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                color = AlertRed,
                modifier = Modifier.semantics { heading() },
            )

            Spacer(modifier = Modifier.height(8.dp))

            if (qualityReport != null) {
                Text(
                    text = "Issues: ${qualityReport.issues.joinToString(", ")}",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                )

                Spacer(modifier = Modifier.height(8.dp))

                for (suggestion in qualityReport.suggestions) {
                    Text(
                        text = "- $suggestion",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            Spacer(modifier = Modifier.height(32.dp))

            Button(
                onClick = onRetry,
                modifier = Modifier
                    .fillMaxWidth()
                    .sizeIn(minHeight = 56.dp),
            ) {
                Text(
                    text = "Try Another Photo",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold,
                )
            }
        }
    }
}

// -----------------------------------------------------------------------
// SharedPreferences helpers
// -----------------------------------------------------------------------

private fun savePrivacyAcknowledgment(context: Context) {
    context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        .edit()
        .putBoolean(PREF_PRIVACY_ACKNOWLEDGED, true)
        .apply()
}

private fun saveOnboardingComplete(context: Context) {
    context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        .edit()
        .putBoolean(PREF_ONBOARDING_COMPLETE, true)
        .apply()
}

/**
 * Check whether onboarding has been completed previously.
 */
fun isOnboardingComplete(context: Context): Boolean {
    return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        .getBoolean(PREF_ONBOARDING_COMPLETE, false)
}
