package com.flightrisk.app.ui.settings

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.sizeIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalInspectionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.flightrisk.app.transport.DisclosureAccepted
import com.flightrisk.app.transport.GroundLink
import com.flightrisk.app.transport.GroundLinkController
import com.flightrisk.app.transport.GroundLinkIndicator
import com.flightrisk.app.transport.GroundLinkText
import com.flightrisk.app.transport.GroundLinkUiState
import com.flightrisk.app.ui.theme.AlertOrange
import com.flightrisk.app.ui.theme.AlertRed
import com.flightrisk.app.ui.theme.MatchGreen

/**
 * Settings: the ground station link. Off by default. Turning it on shows what
 * is sent and where, and only the dialog's confirm button turns it on. The
 * edge token is typed into a hidden field, cleared after Save and never shown
 * again; "Remove saved token" is always available.
 */
@Composable
fun GroundLinkSection(modifier: Modifier = Modifier) {
    if (LocalInspectionMode.current) return
    val controller = GroundLink.controller(LocalContext.current)
    val state by controller.state.collectAsStateWithLifecycle()
    GroundLinkSectionContent(state = state, controller = controller, modifier = modifier)
}

@Composable
private fun GroundLinkSectionContent(
    state: GroundLinkUiState,
    controller: GroundLinkController,
    modifier: Modifier = Modifier,
) {
    var addressInput by remember { mutableStateOf("") }
    var tokenInput by remember { mutableStateOf("") }
    var showDisclosure by remember { mutableStateOf(false) }
    // Show the saved address once it is known; later edits stay in the field.
    LaunchedEffect(state.address) { if (addressInput.isEmpty()) addressInput = state.address }

    if (showDisclosure) {
        AlertDialog(
            onDismissRequest = { showDisclosure = false },
            title = { Text(GroundLinkText.DISCLOSURE_TITLE) },
            text = {
                Column {
                    Text(GroundLinkText.DISCLOSURE)
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("Address: ${addressInput.trim()}", fontWeight = FontWeight.Bold)
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    showDisclosure = false
                    controller.enable(addressInput, DisclosureAccepted(addressInput))
                }) { Text("Send detections") }
            },
            dismissButton = { TextButton(onClick = { showDisclosure = false }) { Text("Cancel") } },
        )
    }

    Column(modifier = modifier.fillMaxWidth()) {
        Text(
            text = "Ground station link",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { heading() },
        )
        Spacer(modifier = Modifier.height(8.dp))
        Text(
            text = "Optional. Lets this phone act as a camera for a FlightRisk ground station on your network. It is off until you turn it on.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(modifier = Modifier.height(12.dp))

        Row(
            modifier = Modifier.fillMaxWidth().sizeIn(minHeight = 48.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = "Send detections to the ground station",
                style = MaterialTheme.typography.bodyLarge,
                modifier = Modifier.weight(1f),
            )
            Switch(
                checked = state.enabled,
                onCheckedChange = { on ->
                    if (on) {
                        // Validate first so a bad address shows its reason instead of a dialog.
                        if (com.flightrisk.app.transport.EdgeEndpoint.parse(addressInput) is com.flightrisk.app.ground.EndpointParseResult.Ok) {
                            showDisclosure = true
                        } else {
                            // Refused by the parser: show its reason (no consent is involved).
                            controller.checkAddress(addressInput)
                        }
                    } else {
                        controller.disable()
                    }
                },
                modifier = Modifier.semantics {
                    contentDescription = "Send detections to the ground station: ${if (state.enabled) "on" else "off"}"
                },
            )
        }

        OutlinedTextField(
            value = addressInput,
            onValueChange = { addressInput = it },
            label = { Text("Ground station address") },
            placeholder = { Text("192.168.1.20:9000") },
            singleLine = true,
            isError = state.addressProblem != null,
            supportingText = {
                Text(
                    state.addressProblem
                        ?: if (addressInput.trim().endsWith(":5555")) GroundLinkText.PORT_HINT
                        else "Plain address on your local network. The port defaults to 9000.",
                )
            },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier.fillMaxWidth().sizeIn(minHeight = 48.dp),
        )
        TextButton(
            onClick = { controller.changeAddress(addressInput) },
            enabled = addressInput.isNotBlank(),
            modifier = Modifier.sizeIn(minHeight = 48.dp),
        ) { Text("Save address") }

        Spacer(modifier = Modifier.height(8.dp))
        OutlinedTextField(
            value = tokenInput,
            onValueChange = { tokenInput = it },
            label = { Text("Edge token") },
            placeholder = { Text(if (state.hasToken) "Replace saved token" else "Token from the ground station") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
            modifier = Modifier.fillMaxWidth().sizeIn(minHeight = 48.dp),
        )
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(
                onClick = {
                    val token = tokenInput
                    tokenInput = ""
                    controller.saveToken(token)
                },
                enabled = tokenInput.isNotBlank(),
                modifier = Modifier.sizeIn(minHeight = 48.dp),
            ) { Text("Save token") }
            Spacer(modifier = Modifier.width(8.dp))
            TextButton(
                onClick = { controller.removeToken() },
                modifier = Modifier.sizeIn(minHeight = 48.dp),
            ) { Text("Remove saved token") }
        }
        Text(
            text = if (state.hasToken) "A token is saved. It is never shown again." else "No token saved.",
            style = MaterialTheme.typography.bodySmall,
            color = if (state.hasToken) MatchGreen else MaterialTheme.colorScheme.onSurfaceVariant,
        )
        state.tokenMessage?.let {
            Text(text = it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(modifier = Modifier.height(12.dp))
        val problem = state.enabled && (state.status is com.flightrisk.app.transport.EdgeLinkStatus.TokenRejected ||
            state.status is com.flightrisk.app.transport.EdgeLinkStatus.AddressRefused ||
            state.status is com.flightrisk.app.transport.EdgeLinkStatus.NeedsToken ||
            state.status is com.flightrisk.app.transport.EdgeLinkStatus.StorageUnavailable)
        Text(
            text = GroundLinkText.statusLine(state),
            style = MaterialTheme.typography.bodyMedium,
            color = if (problem) AlertRed else MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.semantics { contentDescription = "Ground station link status: ${GroundLinkText.statusLine(state)}" },
        )
        Spacer(modifier = Modifier.height(8.dp))
        Text(
            text = GroundLinkText.DISCLOSURE,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/**
 * The Search screen's indicator for the ground station link: "Sending
 * detections to <host>" while detections go out, and the reason when the
 * ground rejected the token or the address was refused. Shows nothing when
 * the link is off.
 */
@Composable
fun GroundLinkIndicatorBanner(modifier: Modifier = Modifier) {
    if (LocalInspectionMode.current) return
    val controller = GroundLink.controller(LocalContext.current)
    val state by controller.state.collectAsStateWithLifecycle()
    val indicator: GroundLinkIndicator = GroundLinkText.indicator(state) ?: return
    Text(
        text = indicator.text,
        style = MaterialTheme.typography.bodySmall,
        color = Color.White,
        modifier = modifier
            .fillMaxWidth()
            .background(
                (if (indicator.sending) AlertOrange else AlertRed).copy(alpha = 0.85f),
                RoundedCornerShape(8.dp),
            )
            .padding(horizontal = 12.dp, vertical = 8.dp)
            .semantics { contentDescription = indicator.text },
    )
}
