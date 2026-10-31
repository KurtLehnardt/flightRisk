package com.flightrisk.app.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.flightrisk.app.persistence.SessionRepository
import com.flightrisk.app.privacy.PrivacySettingsText
import com.flightrisk.app.privacy.SavedDataEraser
import com.flightrisk.app.privacy.SharedPrefsPrivacyPrefs
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Settings: the "Record search video" switch (off by default) and the action
 * that deletes every saved session, recording and match picture. Deleting
 * asks for confirmation first and is not offered while a search is running.
 */
@Composable
fun PrivacyDataSection(
    searchActive: Boolean,
    modifier: Modifier = Modifier,
) {
    if (LocalInspectionMode.current) return
    val context = LocalContext.current.applicationContext
    val prefs = remember { SharedPrefsPrivacyPrefs(context) }
    var record by remember { mutableStateOf(prefs.recordSearchVideo) }
    var confirming by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    Column(modifier = modifier) {
        Text(
            text = "Saved data",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )
        Spacer(modifier = Modifier.height(12.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = PrivacySettingsText.RECORD_TITLE,
                style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.weight(1f).padding(end = 12.dp),
            )
            Switch(
                checked = record,
                onCheckedChange = {
                    record = it
                    prefs.recordSearchVideo = it
                },
            )
        }
        Text(
            text = PrivacySettingsText.RECORD_EXPLANATION,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(modifier = Modifier.height(16.dp))

        OutlinedButton(
            onClick = { confirming = true },
            enabled = !searchActive && !busy,
            modifier = Modifier.fillMaxWidth().sizeIn(minHeight = 48.dp),
        ) {
            Text(PrivacySettingsText.DELETE_TITLE)
        }
        if (searchActive) {
            Text(
                text = PrivacySettingsText.DELETE_BLOCKED_WHILE_SEARCHING,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        message?.let {
            Spacer(modifier = Modifier.height(8.dp))
            Text(text = it, style = MaterialTheme.typography.bodyMedium)
        }
    }

    if (confirming) {
        AlertDialog(
            onDismissRequest = { confirming = false },
            title = { Text(PrivacySettingsText.DELETE_CONFIRM_TITLE) },
            text = { Text(PrivacySettingsText.DELETE_CONFIRM_BODY) },
            confirmButton = {
                TextButton(
                    onClick = {
                        confirming = false
                        busy = true
                        scope.launch {
                            message = try {
                                val result = withContext(Dispatchers.IO) {
                                    SavedDataEraser(
                                        SessionRepository(context),
                                        listOf(
                                            File(context.filesDir, "recordings"),
                                            File(context.filesDir, "snapshots"),
                                        ),
                                    ).eraseAll()
                                }
                                PrivacySettingsText.deleteResult(result)
                            } catch (e: Exception) {
                                PrivacySettingsText.DELETE_FAILED
                            }
                            busy = false
                        }
                    },
                ) { Text(PrivacySettingsText.DELETE_CONFIRM_BUTTON) }
            },
            dismissButton = {
                TextButton(onClick = { confirming = false }) { Text(PrivacySettingsText.DELETE_CANCEL_BUTTON) }
            },
        )
    }
}
