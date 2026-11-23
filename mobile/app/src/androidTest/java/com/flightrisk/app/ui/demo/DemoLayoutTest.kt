package com.flightrisk.app.ui.demo

import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.getUnclippedBoundsInRoot
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.Density
import com.flightrisk.app.demo.DemoText
import com.flightrisk.app.ui.ground.DroneCardUi
import com.flightrisk.app.ui.ground.GroundActions
import com.flightrisk.app.ui.ground.GroundConsoleContent
import com.flightrisk.app.ui.ground.GroundLink
import com.flightrisk.app.ui.ground.GroundUiState
import com.flightrisk.app.ui.theme.FlightRiskTheme
import com.flightrisk.contract.ConnectionState
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

/**
 * The demo frame on a device: the banner above the Ground screen never covers LAND ALL, also at twice the
 * default font size, and TalkBack hears that LAND ALL and the drone cards are a demo. These need a device
 * or emulator (`./gradlew :app:connectedDebugAndroidTest`); they compile without one.
 */
class DemoLayoutTest {

    @get:Rule
    val compose = createComposeRule()

    private fun demoCard(id: String) = DroneCardUi(
        id = id,
        connection = ConnectionState.CONNECTED,
        linkChipText = ConnectionState.CONNECTED.label,
        linkChipGreyed = false,
        controlChip = null,
        controlText = "Flying",
        isHeldWithCountdown = false,
        batteryText = "87%",
        altitudeText = "100 cm",
        laneText = "1/3",
        linkNotice = null,
        isPartial = false,
        isStateStale = false,
        isValuesStale = false,
        staleNote = null,
        canResume = false,
        landFeedback = null,
        resumeFeedback = null,
        alert = null,
        landFeedbackText = null,
        resumeFeedbackText = null,
        alertText = null,
        isDemo = true,
    )

    private val demoState = GroundUiState(
        link = GroundLink.Connected,
        demo = true,
        cards = listOf(demoCard("demo-drone-1"), demoCard("demo-drone-2")),
    )

    private fun showDemo(fontScale: Float) {
        compose.setContent {
            val density = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(density.density, fontScale)) {
                FlightRiskTheme {
                    DemoFrame(demo = true) { GroundConsoleContent(demoState, GroundActions()) }
                }
            }
        }
    }

    private fun assertLandAllBelowTheBanner() {
        val banner = compose.onNodeWithTag("demo.banner").assertIsDisplayed().getUnclippedBoundsInRoot()
        val landAll = compose.onNodeWithTag("ground.landAll").assertIsDisplayed().assertIsEnabled().getUnclippedBoundsInRoot()
        assertTrue("LAND ALL ($landAll) overlaps the demo banner ($banner)", landAll.top >= banner.bottom)
        assertTrue("LAND ALL has no height", landAll.bottom > landAll.top)
    }

    @Test
    fun landAllIsBelowTheBannerAtTheDefaultFontSize() {
        showDemo(fontScale = 1f)
        assertLandAllBelowTheBanner()
    }

    @Test
    fun landAllIsBelowTheBannerAtTwiceTheFontSize() {
        showDemo(fontScale = 2f)
        assertLandAllBelowTheBanner()
    }

    @Test
    fun talkBackHearsThatLandAllAndTheCardsAreADemo() {
        showDemo(fontScale = 1f)
        compose.onNodeWithContentDescription(DemoText.LAND_ALL_DESCRIPTION).assertIsDisplayed().assertIsEnabled()
        compose.onNodeWithText(DemoText.LAND_ALL_LABEL, useUnmergedTree = true).assertExists()
        compose.onNodeWithContentDescription(DemoText.droneDescription("demo-drone-1")).assertExists()
        compose.onNodeWithContentDescription(DemoText.landDescription("Land", "demo-drone-1")).assertExists()
        compose.onNodeWithContentDescription(DemoText.BANNER + ". " + DemoText.BANNER_DETAIL).assertIsDisplayed()
    }

    @Test
    fun theTargetTabOffersNoPickerInDemoMode() {
        compose.setContent { FlightRiskTheme { DemoTargetPlaceholder(target = null) } }
        compose.onNodeWithText(DemoText.TARGET_PLACEHOLDER).assertIsDisplayed()
        compose.onNodeWithText("Select Photo").assertDoesNotExist()
        compose.onNodeWithText("Take Photo").assertDoesNotExist()
    }
}
