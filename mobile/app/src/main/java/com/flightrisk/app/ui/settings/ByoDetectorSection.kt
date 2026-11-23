package com.flightrisk.app.ui.settings

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalInspectionMode
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.flightrisk.app.byo.ByoDetectorImporter
import com.flightrisk.app.byo.ByoDetectorStore
import com.flightrisk.app.byo.ByoDetectorText
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Settings: use a person detector the user imports instead of YOLOX-S. Off by
 * default. Turning it on shows the licence notice ([ByoDetectorText.GATE_BODY],
 * AGPL-3.0 and the Ultralytics Enterprise License, with a link to
 * ultralytics.com/license) and asks the user to tick the attestation before the
 * system file picker (`ACTION_OPEN_DOCUMENT`) opens. Nothing is pre-filled: the
 * app offers no model and no address to download one from. The chosen file is
 * checked by [ByoDetectorImporter]; its SHA-256 is shown. Turning it off deletes
 * the file and searches use YOLOX-S again.
 */
@Composable
fun ByoDetectorSection(
    searchActive: Boolean,
    modifier: Modifier = Modifier,
) {
    if (LocalInspectionMode.current) return
    val context = LocalContext.current.applicationContext
    val store = remember { ByoDetectorStore(context) }
    val uriHandler = LocalUriHandler.current
    val scope = rememberCoroutineScope()

    var current by remember { mutableStateOf(store.current()) }
    var showGate by remember { mutableStateOf(false) }
    var attested by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        busy = true
        message = ByoDetectorText.CHECKING
        scope.launch {
            val result = withContext(Dispatchers.IO) { ByoDetectorImporter(context).import(uri) }
            busy = false
            when (result) {
                is ByoDetectorImporter.Result.Imported -> {
                    current = result.record
                    message = null
                }
                is ByoDetectorImporter.Result.Failed -> {
                    current = withContext(Dispatchers.IO) { store.current() }
                    message = ByoDetectorText.failed(result.reason)
                }
            }
        }
    }

    Column(modifier = modifier) {
        Text(
            text = ByoDetectorText.TITLE,
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )
        Spacer(modifier = Modifier.height(8.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .sizeIn(minHeight = 48.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = if (current != null) ByoDetectorText.ON_SUMMARY else ByoDetectorText.OFF_SUMMARY,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.weight(1f).padding(end = 12.dp),
            )
            Switch(
                checked = current != null,
                enabled = !busy,
                onCheckedChange = { on ->
                    if (on) {
                        attested = false
                        showGate = true
                    } else {
                        current = null
                        message = null
                        scope.launch(Dispatchers.IO) { store.turnOff() }
                    }
                },
            )
        }
        current?.let { imported ->
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = ByoDetectorText.imported(imported.sizeBytes, imported.layout.name.lowercase(), imported.sha256),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        message?.let {
            Spacer(modifier = Modifier.height(4.dp))
            Text(text = it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
        }
        if (searchActive) {
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = ByoDetectorText.APPLIES_NEXT_SEARCH,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }

    if (showGate) {
        AlertDialog(
            onDismissRequest = { showGate = false },
            title = { Text(ByoDetectorText.GATE_TITLE) },
            text = {
                Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                    Text(ByoDetectorText.GATE_BODY, style = MaterialTheme.typography.bodyMedium)
                    TextButton(onClick = { runCatching { uriHandler.openUri(ByoDetectorText.LICENSE_URL) } }) {
                        Text(ByoDetectorText.LICENSE_LINK_LABEL)
                    }
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .sizeIn(minHeight = 48.dp)
                            .toggleable(value = attested, role = Role.Checkbox, onValueChange = { attested = it }),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Checkbox(checked = attested, onCheckedChange = null)
                        Text(
                            text = ByoDetectorText.ATTESTATION,
                            style = MaterialTheme.typography.bodyMedium,
                            modifier = Modifier.padding(start = 8.dp),
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(
                    enabled = attested,
                    onClick = {
                        showGate = false
                        // Any file type: .onnx has no registered MIME type. The importer checks the name.
                        picker.launch(arrayOf("*/*"))
                    },
                ) { Text(ByoDetectorText.CHOOSE_FILE) }
            },
            dismissButton = {
                TextButton(onClick = { showGate = false }) { Text(ByoDetectorText.CANCEL) }
            },
        )
    }
}
