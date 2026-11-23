package com.flightrisk.app.ui.settings

import android.content.Context
import androidx.compose.foundation.clickable
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import com.flightrisk.app.R
import com.mikepenz.aboutlibraries.Libs
import com.mikepenz.aboutlibraries.entity.Library
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
        "The person detector (YOLOX-S, Apache-2.0) and the appearance embedder (open_clip ViT-B-16, MIT) " +
            "that run on this phone, then every third-party library in this app, with their licences in full."
    const val OPEN = "Licences and notices"
    const val CLOSE = "Close"
    const val NOT_INSTALLED =
        "This build has no on-device models, so there are no model notices to show. Release builds " +
            "install the models and their notices with scripts/mobile_models/make_models.py."
    const val BYO_NOTE =
        "A detector you import yourself (Settings, \"Bring your own detector\") is not covered here: " +
            "FlightRisk does not supply or license it."

    /** The asset `make_models.py install` writes next to the models (a copy of scripts/mobile_models/NOTICES.md). */
    const val NOTICES_ASSET = "model-notices.md"

    const val LIBRARIES_TITLE = "Libraries"
    const val LIBRARIES_SUMMARY =
        "The third-party libraries built into this app, listed from the build itself. Tap one for its licence."
    const val LIBRARIES_UNAVAILABLE = "This build has no generated library list."
    const val NO_LICENCE_TEXT = "The licence text is not bundled with this library. Its licence is published at:"
    const val UNKNOWN_LICENCE = "Licence not stated"
    const val BACK = "Back"

    /** The libraries the build generated ([R.raw.aboutlibraries]), sorted by name; empty when unreadable. */
    fun readLibraries(context: Context): List<Library> =
        try {
            val json = context.resources.openRawResource(R.raw.aboutlibraries).bufferedReader().use { it.readText() }
            Libs.Builder().withJson(json).build().libraries.sortedBy { (it.name.ifBlank { it.uniqueId }).lowercase() }
        } catch (e: Exception) {
            emptyList()
        }

    /** A library's display name: its name, or its coordinates when the POM has none. */
    fun nameOf(library: Library): String = library.name.ifBlank { library.uniqueId }

    /** The licence names of a library, joined, or [UNKNOWN_LICENCE]. */
    fun licenceNamesOf(library: Library): String =
        library.licenses.map { it.name }.filter { it.isNotBlank() }.joinToString(", ").ifBlank { UNKNOWN_LICENCE }

    /** The notices, or null when this build has none. */
    fun read(context: Context): String? =
        try {
            context.assets.open(NOTICES_ASSET).bufferedReader().use { it.readText() }
        } catch (e: IOException) {
            null
        }
}

/** Settings: opens the model notices ([LicencesText.NOTICES_ASSET]) in full, then the build-generated library list. */
@Composable
fun LicencesSection(modifier: Modifier = Modifier) {
    if (LocalInspectionMode.current) return
    val context = LocalContext.current.applicationContext
    var open by remember { mutableStateOf(false) }
    var selected by remember { mutableStateOf<Library?>(null) }

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
        val libraries by produceState<List<Library>?>(initialValue = null) {
            value = withContext(Dispatchers.IO) { LicencesText.readLibraries(context) }
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
                    LazyColumn(modifier = Modifier.fillMaxSize()) {
                        item {
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
                            Spacer(modifier = Modifier.height(24.dp))
                            Text(
                                text = LicencesText.LIBRARIES_TITLE,
                                style = MaterialTheme.typography.titleLarge,
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.semantics { heading() },
                            )
                            Spacer(modifier = Modifier.height(8.dp))
                            Text(
                                text = if (libraries?.isEmpty() == true) LicencesText.LIBRARIES_UNAVAILABLE else LicencesText.LIBRARIES_SUMMARY,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            Spacer(modifier = Modifier.height(8.dp))
                        }
                        items(libraries.orEmpty(), key = { it.uniqueId }) { library ->
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { selected = library }
                                    .sizeIn(minHeight = 48.dp)
                                    .padding(vertical = 8.dp),
                            ) {
                                Text(
                                    text = LicencesText.nameOf(library),
                                    style = MaterialTheme.typography.bodyMedium,
                                    fontWeight = FontWeight.Medium,
                                )
                                Text(
                                    text = listOfNotNull(library.artifactVersion, LicencesText.licenceNamesOf(library))
                                        .filter { it.isNotBlank() }.joinToString("  |  "),
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                    }
                }
            }
        }
    }

    selected?.let { library ->
        Dialog(
            onDismissRequest = { selected = null },
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
                            text = LicencesText.nameOf(library),
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.weight(1f).semantics { heading() },
                        )
                        TextButton(onClick = { selected = null }) { Text(LicencesText.BACK) }
                    }
                    LazyColumn(modifier = Modifier.fillMaxSize()) {
                        item {
                            Text(
                                text = listOf(library.uniqueId, library.artifactVersion.orEmpty()).filter { it.isNotBlank() }.joinToString(" "),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        items(library.licenses.toList(), key = { it.hash }) { licence ->
                            Spacer(modifier = Modifier.height(16.dp))
                            Text(
                                text = licence.name.ifBlank { LicencesText.UNKNOWN_LICENCE },
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold,
                            )
                            Spacer(modifier = Modifier.height(8.dp))
                            val text = licence.licenseContent
                            if (!text.isNullOrBlank()) {
                                Text(text = text, style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace)
                            } else {
                                Text(text = LicencesText.NO_LICENCE_TEXT, style = MaterialTheme.typography.bodySmall)
                                Text(text = licence.url.orEmpty(), style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace)
                            }
                        }
                    }
                }
            }
        }
    }
}
