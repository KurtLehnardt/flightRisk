package com.flightrisk.app.ui.settings

import android.content.Context
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalInspectionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException

/** The words of the Licences section, kept apart from the screen for tests. */
object LicencesText {
    const val TITLE = "Licences"
    const val SUMMARY =
        "The person detector (YOLOX-S, Apache-2.0), the appearance embedder (open_clip ViT-B-16, MIT) " +
            "and the libraries in this app, with their licence notices in full."
    const val OPEN = "Model licences and notices"
    const val CLOSE = "Close"
    const val NOT_INSTALLED =
        "This build has no on-device models, so there are no model notices to show. Release builds " +
            "install the models and their notices with scripts/mobile_models/make_models.py."
    const val BYO_NOTE =
        "A detector you import yourself (Settings, \"Bring your own detector\") is not covered here: " +
            "FlightRisk does not supply or license it."

    /** The asset `make_models.py install` writes next to the models (a copy of scripts/mobile_models/NOTICES.md). */
    const val NOTICES_ASSET = "model-notices.md"

    /** The notices, or null when this build has none. */
    fun read(context: Context): String? =
        try {
            context.assets.open(NOTICES_ASSET).bufferedReader().use { it.readText() }
        } catch (e: IOException) {
            null
        }
}

/** Settings: opens the licence notices of the on-device models ([LicencesText.NOTICES_ASSET]) in full. */
@Composable
fun LicencesSection(modifier: Modifier = Modifier) {
    if (LocalInspectionMode.current) return
    val context = LocalContext.current.applicationContext
    var open by remember { mutableStateOf(false) }

    Column(modifier = modifier) {
        Text(
            text = LicencesText.TITLE,
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )
        Spacer(modifier = Modifier.height(8.dp))
        Text(
            text = LicencesText.SUMMARY,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(modifier = Modifier.height(8.dp))
        OutlinedButton(onClick = { open = true }, modifier = Modifier.sizeIn(minHeight = 48.dp)) {
            Text(LicencesText.OPEN)
        }
    }

    if (open) {
        val notices by produceState<String?>(initialValue = null) {
            value = withContext(Dispatchers.IO) { LicencesText.read(context) } ?: LicencesText.NOT_INSTALLED
        }
        Dialog(
            onDismissRequest = { open = false },
            properties = DialogProperties(usePlatformDefaultWidth = false),
        ) {
            Surface(modifier = Modifier.fillMaxSize()) {
                Column(modifier = Modifier.systemBarsPadding().padding(16.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween,
                    ) {
                        Text(
                            text = LicencesText.OPEN,
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.weight(1f).semantics { heading() },
                        )
                        TextButton(onClick = { open = false }) { Text(LicencesText.CLOSE) }
                    }
                    Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                        Text(
                            text = LicencesText.BYO_NOTE,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Spacer(modifier = Modifier.height(12.dp))
                        Text(
                            text = notices ?: "",
                            style = MaterialTheme.typography.bodySmall,
                            fontFamily = FontFamily.Monospace,
                        )
                    }
                }
            }
        }
    }
}
