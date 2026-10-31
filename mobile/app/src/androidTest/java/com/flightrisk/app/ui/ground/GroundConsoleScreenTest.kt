package com.flightrisk.app.ui.ground

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.assertTextContains
import com.flightrisk.app.ui.theme.FlightRiskTheme
import com.flightrisk.contract.ConnectionState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

/**
 * Compose UI tests of the Ground console. They draw [GroundConsoleContent] from plain state,
 * so no network or ViewModel is involved. They need a device or emulator
 * (`./gradlew :app:connectedDebugAndroidTest`); they compile without one.
 */
class GroundConsoleScreenTest {

    @get:Rule
    val compose = createComposeRule()

    private fun card(
        id: String = "drone-1",
        connection: ConnectionState = ConnectionState.CONNECTED,
        stale: Boolean = false,
        canResume: Boolean = false,
        linkNotice: String? = null,
        landFeedback: CommandFeedback? = null,
    ) = DroneCardUi(
        id = id,
        connection = connection,
        linkChipText = if (stale) "LAST: ${connection.label}" else connection.label,
        linkChipGreyed = stale,
        controlChip = null,
        controlText = "",
        isHeldWithCountdown = false,
        batteryText = if (stale) "87% (last value)" else "87%",
        altitudeText = "100 cm",
        laneText = null,
        linkNotice = linkNotice,
        isPartial = false,
        isStateStale = stale,
        isValuesStale = stale || connection.isStale,
        staleNote = null,
        canResume = canResume,
        landFeedback = landFeedback,
        resumeFeedback = null,
        alert = null,
        landFeedbackText = landFeedback?.text,
        resumeFeedbackText = null,
        alertText = null,
    )

    private fun show(state: GroundUiState, actions: GroundActions = GroundActions()) {
        compose.setContent { FlightRiskTheme { GroundConsoleContent(state, actions) } }
    }

    @Test
    fun landAllIsVisibleAndEnabledInEveryLinkState() {
        val links = listOf(
            GroundLink.NotConfigured, GroundLink.StorageUnavailable, GroundLink.Off, GroundLink.Paused,
            GroundLink.Connecting(1), GroundLink.Connected, GroundLink.Reconnecting(2, 4.0, "the ground stopped answering"),
            GroundLink.WrongKey, GroundLink.AddressRefused("not private"), GroundLink.Stopped,
        )
        for (link in links) {
            compose.setContent { FlightRiskTheme { GroundConsoleContent(GroundUiState(link = link), GroundActions()) } }
            compose.onNodeWithTag("ground.landAll").assertIsDisplayed().assertIsEnabled()
        }
    }

    @Test
    fun landAllIsOneTapWithNoConfirmation() {
        var taps = 0
        show(GroundUiState(link = GroundLink.Off), GroundActions(onLandAll = { taps++ }))
        compose.onNodeWithTag("ground.landAll").performClick()
        assertEquals(1, taps)
    }

    @Test
    fun landAllResultIsShownPlainly() {
        val failed = CommandFeedback(1, CommandPhase.FAILED, DisplayTextForTest.NOT_SENT, startedAtMs = 0, updatedAtMs = 0)
        show(GroundUiState(link = GroundLink.Reconnecting(1, 2.0, "x"), landAll = failed, landAllText = failed.text))
        compose.onNodeWithTag("ground.landAllResult").assertTextContains("LAND ALL was NOT sent", substring = true)
        compose.onNodeWithTag("ground.landAllAgain").assertIsDisplayed()
    }

    @Test
    fun landIsEnabledOnADownDroneAndShowsTheLinkNotice() {
        var landed: String? = null
        val down = card(connection = ConnectionState.DOWN, linkNotice = "Drone link down: no state. Land still works.")
        show(GroundUiState(link = GroundLink.Connected, cards = listOf(down)), GroundActions(onLandDrone = { landed = it }))
        compose.onNodeWithTag("ground.card.drone-1.land").assertIsEnabled().performClick()
        assertEquals("drone-1", landed)
        compose.onNodeWithTag("ground.card.drone-1.linkNotice").assertTextContains("Land still works", substring = true)
    }

    @Test
    fun landStaysEnabledWhileTheStateIsStaleAndTheChipIsLast() {
        val stale = card(stale = true)
        show(GroundUiState(link = GroundLink.Reconnecting(1, 2.0, "x"), isStateStale = true, staleNote = GroundText.STALE_AFTER_DISCONNECT, cards = listOf(stale)))
        compose.onNodeWithTag("ground.staleBanner").assertIsDisplayed()
        compose.onNodeWithTag("ground.card.drone-1.link").assertTextContains("LAST: CONNECTED")
        compose.onNodeWithTag("ground.card.drone-1.land").assertIsEnabled()
    }

    @Test
    fun resumeIsShownOnlyWhenOffered() {
        show(GroundUiState(link = GroundLink.Connected, cards = listOf(card("drone-1"), card("drone-2", canResume = true))))
        compose.onNodeWithTag("ground.card.drone-2.resume").assertIsDisplayed()
        compose.onNodeWithTag("ground.card.drone-1.resume").assertDoesNotExist()
    }

    @Test
    fun wrongKeyHasARetryButton() {
        var retried = false
        show(GroundUiState(link = GroundLink.WrongKey), GroundActions(onRetry = { retried = true }))
        compose.onNodeWithTag("ground.retry").performClick()
        assertTrue(retried)
    }

    @Test
    fun theKeyFieldIsClearedAfterSavingAndNeverShown() {
        var saved: String? = null
        show(
            GroundUiState(connection = ConnectionCardState(keyState = KeyState.SAVED)),
            GroundActions(onSaveKey = { saved = it }),
        )
        compose.onNodeWithTag("ground.connection.key").performTextInput("test-key")
        compose.onNodeWithTag("ground.connection.saveKey").performClick()
        assertEquals("test-key", saved)
        compose.onNodeWithText("test-key").assertDoesNotExist()
        compose.onNodeWithTag("ground.connection.keyState").assertTextContains("saved (not shown)", substring = true)
    }

    @Test
    fun thePrivacyLineIsShown() {
        show(GroundUiState())
        compose.onNodeWithText("Connects only to the ground station address you enter.").assertIsDisplayed()
    }
}

private object DisplayTextForTest {
    const val NOT_SENT = com.flightrisk.contract.DisplayText.LAND_ALL_NOT_SENT
}
