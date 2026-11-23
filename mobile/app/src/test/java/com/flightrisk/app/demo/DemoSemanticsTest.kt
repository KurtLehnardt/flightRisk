package com.flightrisk.app.demo

import com.flightrisk.app.ground.eventually
import com.flightrisk.app.ui.ground.GroundLink
import com.flightrisk.app.ui.ground.LandAllSemantics
import com.flightrisk.app.ui.ground.harnessTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * What TalkBack reads in demo mode: LAND ALL and every drone card say that only simulated drones on this
 * phone are landed and nothing is sent. The Compose side of this (the nodes on screen) is in the
 * instrumented `DemoLayoutTest`.
 */
class DemoSemanticsTest {

    @Test
    fun `LAND ALL says it is a demo in its text and its description`() {
        assertEquals("LAND ALL (demo)", LandAllSemantics.label(demo = true))
        assertEquals(
            "LAND ALL (demo). Demo: lands only the simulated drones on this phone. Nothing is sent.",
            LandAllSemantics.description(demo = true),
        )
        assertEquals("Send LAND ALL (demo) again", LandAllSemantics.againLabel(demo = true))
        assertEquals("LAND ALL", LandAllSemantics.label(demo = false))
        assertEquals("Land all drones", LandAllSemantics.description(demo = false))
        assertEquals("Send LAND ALL again", LandAllSemantics.againLabel(demo = false))
    }

    @Test
    fun `demo drone cards say they are simulated and real ones do not`() = harnessTest(demo = true) {
        vm.enterDemo(disconnectConfirmed = true).join()
        eventually("demo fleet") { ui.demo && ui.cards.size == 2 }
        for (card in ui.cards) {
            assertTrue(card.isDemo)
            assertEquals("DEMO: simulated drone ${card.displayId}", card.titleDescription)
            assertEquals(
                "DEMO: Land ${card.displayId}. Lands only the simulated drone on this phone. Nothing is sent.",
                card.landDescription,
            )
            assertTrue(card.resumeDescription.startsWith("DEMO: Resume the search of ${card.displayId}"))
        }
        onMain { vm.exitDemo() }
        onMain { vm.setEnabled(true) }
        val real = connectWithFleet()
        eventually("real fleet") { ui.link == GroundLink.Connected && ui.cards.size == 2 }
        for (card in ui.cards) {
            assertFalse(card.isDemo)
            assertEquals("Land ${card.displayId}", card.landDescription)
            assertEquals(card.displayId, card.titleDescription)
            assertFalse(card.landDescription.contains("DEMO"))
        }
        assertFalse(real.closed)
    }
}
