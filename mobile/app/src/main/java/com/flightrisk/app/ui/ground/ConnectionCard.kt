package com.flightrisk.app.ui.ground

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.flightrisk.app.ui.theme.AlertOrange

/**
 * The connection settings of the Ground screen: the ground address, the
 * dashboard API key, "Test connection" and the connect switch.
 *
 * The key field is write-only. Its text is handed to [onSaveKey] and cleared
 * at once; the saved key is never shown again, only "saved" or "none".
 */
@Composable
fun ConnectionCard(
    state: ConnectionCardState,
    onSaveAddress: (String) -> Unit,
    onSaveKey: (String) -> Unit,
    onRemoveKey: () -> Unit,
    onTest: () -> Unit,
    onSetEnabled: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
) {
    var address by remember(state.savedAddress) { mutableStateOf(state.savedAddress.orEmpty()) }
    var key by remember { mutableStateOf("") }
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f), RoundedCornerShape(12.dp))
            .padding(12.dp)
            .testTag("ground.connection"),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("Connection", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        Text(GroundText.PRIVACY_NOTE, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)

        OutlinedTextField(
            value = address,
            onValueChange = { address = it },
            label = { Text("Ground station address") },
            placeholder = { Text("192.168.1.20:5555") },
            singleLine = true,
            isError = state.addressError != null,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier.fillMaxWidth().testTag("ground.connection.address"),
        )
        state.addressError?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = AlertOrange, modifier = Modifier.testTag("ground.connection.addressError"))
        }
        Button(onClick = { onSaveAddress(address) }, modifier = Modifier.testTag("ground.connection.saveAddress")) { Text("Save address") }

        Text(
            when (state.keyState) {
                KeyState.SAVED -> "API key: saved (not shown)"
                KeyState.NONE -> "API key: none saved"
                KeyState.UNAVAILABLE -> GroundText.SECURE_STORAGE_UNAVAILABLE
                KeyState.UNKNOWN -> "API key: checking the secure storage…"
            },
            style = MaterialTheme.typography.bodyMedium,
            color = if (state.keyState == KeyState.UNAVAILABLE) AlertOrange else MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.testTag("ground.connection.keyState"),
        )
        OutlinedTextField(
            value = key,
            onValueChange = { key = it },
            label = { Text("Dashboard API key") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, autoCorrect = false),
            modifier = Modifier.fillMaxWidth().testTag("ground.connection.key"),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = {
                    val typed = key
                    key = "" // The key is never echoed back.
                    onSaveKey(typed)
                },
                modifier = Modifier.testTag("ground.connection.saveKey"),
            ) { Text("Save key") }
            if (state.keyState == KeyState.SAVED) {
                OutlinedButton(onClick = onRemoveKey, modifier = Modifier.testTag("ground.connection.removeKey")) { Text("Remove saved key") }
            }
        }

        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Switch(
                checked = state.enabled,
                onCheckedChange = onSetEnabled,
                modifier = Modifier.testTag("ground.connection.enabled"),
            )
            Text("Connect to the ground station", style = MaterialTheme.typography.bodyMedium)
        }
        OutlinedButton(
            onClick = onTest,
            enabled = !state.testing,
            modifier = Modifier.testTag("ground.connection.test"),
        ) { Text(if (state.testing) "Testing…" else "Test connection") }
        state.testResult?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("ground.connection.testResult"))
        }
        state.message?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.testTag("ground.connection.message"))
        }
    }
}
